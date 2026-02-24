"""
Transformer 기반 패턴 인식 모듈 (Feeder)
입력: 60분봉 × (OHLCV + BB + RSI + VWAP + 거래량비율)
출력: 케이스 분류 확률 (A/B/C/D/E), 2차 상승 신호
"""

import os
import io
import logging
from typing import Optional, Tuple
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
import pandas as pd

logger = logging.getLogger(__name__)


class PennyPatternDataset(Dataset):
    """트레이딩 패턴 데이터셋"""

    def __init__(self, sequences: np.ndarray, case_labels: np.ndarray, surge_labels: np.ndarray):
        """
        sequences: (N, window_size, n_features)
        case_labels: (N,) - 0:A, 1:B, 2:C, 3:D, 4:E
        surge_labels: (N,) - 0:no_surge, 1:second_surge
        """
        self.sequences = torch.FloatTensor(sequences)
        self.case_labels = torch.LongTensor(case_labels)
        self.surge_labels = torch.FloatTensor(surge_labels)

    def __len__(self):
        return len(self.sequences)

    def __getitem__(self, idx):
        return self.sequences[idx], self.case_labels[idx], self.surge_labels[idx]


class PositionalEncoding(nn.Module):
    """Transformer 위치 인코딩"""

    def __init__(self, d_model: int, max_len: int = 500, dropout: float = 0.1):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # (1, max_len, d_model)
        self.register_buffer("pe", pe)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.pe[:, :x.size(1), :]
        return self.dropout(x)


class PennyFeeder(nn.Module):
    """
    Transformer 기반 패턴 인식 모델
    페니스탁 1차/2차 상승 패턴을 학습하여 케이스 분류
    """

    def __init__(
        self,
        n_features: int = 20,
        d_model: int = 128,
        n_heads: int = 8,
        n_layers: int = 4,
        d_ff: int = 256,
        dropout: float = 0.1,
        window_size: int = 60,
        n_cases: int = 5,
    ):
        super().__init__()

        self.n_features = n_features
        self.d_model = d_model
        self.window_size = window_size
        self.n_cases = n_cases

        # 입력 임베딩
        self.input_projection = nn.Linear(n_features, d_model)
        self.pos_encoding = PositionalEncoding(d_model, max_len=window_size + 10, dropout=dropout)

        # Transformer Encoder
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=n_heads,
            dim_feedforward=d_ff,
            dropout=dropout,
            batch_first=True,
            norm_first=True,  # Pre-LN (더 안정적)
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)

        # 출력 헤드
        self.case_head = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(d_model // 2, n_cases),
        )

        self.surge_head = nn.Sequential(
            nn.Linear(d_model, d_model // 4),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(d_model // 4, 1),
            nn.Sigmoid(),
        )

        self._init_weights()

    def _init_weights(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        x: (batch, window_size, n_features)
        return: case_logits (batch, n_cases), surge_prob (batch, 1)
        """
        # 입력 프로젝션
        x = self.input_projection(x)  # (batch, window, d_model)
        x = self.pos_encoding(x)

        # Transformer
        x = self.transformer(x)  # (batch, window, d_model)

        # CLS 토큰 대신 마지막 타임스텝 사용
        cls_repr = x[:, -1, :]  # (batch, d_model)

        # 출력
        case_logits = self.case_head(cls_repr)    # (batch, n_cases)
        surge_prob = self.surge_head(cls_repr)    # (batch, 1)

        return case_logits, surge_prob

    def predict(self, x: np.ndarray) -> Tuple[np.ndarray, float]:
        """
        단일 시퀀스 예측
        x: (window_size, n_features)
        return: case_probs (5,), surge_prob (float)
        """
        self.eval()
        with torch.no_grad():
            tensor = torch.FloatTensor(x).unsqueeze(0)  # (1, window, features)
            case_logits, surge_prob = self.forward(tensor)
            case_probs = torch.softmax(case_logits, dim=-1).squeeze(0).numpy()
            surge = float(surge_prob.squeeze())
        return case_probs, surge


class FeederTrainer:
    """Feeder 모델 학습기"""

    def __init__(
        self,
        model: PennyFeeder,
        lr: float = 1e-4,
        device: str = None,
    ):
        self.model = model
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = self.model.to(self.device)

        self.optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
        self.scheduler = optim.lr_scheduler.CosineAnnealingLR(
            self.optimizer, T_max=100, eta_min=1e-6
        )
        self.case_criterion = nn.CrossEntropyLoss()
        self.surge_criterion = nn.BCELoss()

    def train_epoch(self, dataloader: DataLoader) -> dict:
        self.model.train()
        total_loss = 0.0
        total_case_loss = 0.0
        total_surge_loss = 0.0
        correct = 0
        total = 0

        for sequences, case_labels, surge_labels in dataloader:
            sequences = sequences.to(self.device)
            case_labels = case_labels.to(self.device)
            surge_labels = surge_labels.to(self.device)

            self.optimizer.zero_grad()
            case_logits, surge_prob = self.model(sequences)

            case_loss = self.case_criterion(case_logits, case_labels)
            surge_loss = self.surge_criterion(surge_prob.squeeze(), surge_labels)
            loss = case_loss + 0.5 * surge_loss

            loss.backward()
            nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
            self.optimizer.step()

            total_loss += loss.item()
            total_case_loss += case_loss.item()
            total_surge_loss += surge_loss.item()

            preds = case_logits.argmax(dim=-1)
            correct += (preds == case_labels).sum().item()
            total += len(case_labels)

        self.scheduler.step()

        return {
            "loss": total_loss / len(dataloader),
            "case_loss": total_case_loss / len(dataloader),
            "surge_loss": total_surge_loss / len(dataloader),
            "accuracy": correct / total if total > 0 else 0,
        }

    def evaluate(self, dataloader: DataLoader) -> dict:
        self.model.eval()
        total_loss = 0.0
        correct = 0
        total = 0

        with torch.no_grad():
            for sequences, case_labels, surge_labels in dataloader:
                sequences = sequences.to(self.device)
                case_labels = case_labels.to(self.device)
                surge_labels = surge_labels.to(self.device)

                case_logits, surge_prob = self.model(sequences)
                case_loss = self.case_criterion(case_logits, case_labels)
                surge_loss = self.surge_criterion(surge_prob.squeeze(), surge_labels)
                loss = case_loss + 0.5 * surge_loss

                total_loss += loss.item()
                preds = case_logits.argmax(dim=-1)
                correct += (preds == case_labels).sum().item()
                total += len(case_labels)

        return {
            "loss": total_loss / len(dataloader),
            "accuracy": correct / total if total > 0 else 0,
        }

    def train(
        self,
        train_loader: DataLoader,
        val_loader: DataLoader,
        epochs: int = 100,
        patience: int = 15,
    ) -> dict:
        """학습 루프"""
        best_val_loss = float("inf")
        best_state = None
        patience_counter = 0
        history = {"train_loss": [], "val_loss": [], "train_acc": [], "val_acc": []}

        for epoch in range(epochs):
            train_metrics = self.train_epoch(train_loader)
            val_metrics = self.evaluate(val_loader)

            history["train_loss"].append(train_metrics["loss"])
            history["val_loss"].append(val_metrics["loss"])
            history["train_acc"].append(train_metrics["accuracy"])
            history["val_acc"].append(val_metrics["accuracy"])

            if epoch % 10 == 0 or epoch == epochs - 1:
                logger.info(
                    f"Epoch {epoch+1:3d}/{epochs} | "
                    f"Train Loss: {train_metrics['loss']:.4f} Acc: {train_metrics['accuracy']:.3f} | "
                    f"Val Loss: {val_metrics['loss']:.4f} Acc: {val_metrics['accuracy']:.3f}"
                )

            # Early stopping
            if val_metrics["loss"] < best_val_loss:
                best_val_loss = val_metrics["loss"]
                best_state = {k: v.clone() for k, v in self.model.state_dict().items()}
                patience_counter = 0
            else:
                patience_counter += 1
                if patience_counter >= patience:
                    logger.info(f"Early stopping at epoch {epoch+1}")
                    break

        # 최적 가중치 복원
        if best_state:
            self.model.load_state_dict(best_state)

        return history

    def save(self, path: str):
        """모델 저장"""
        torch.save({
            "model_state_dict": self.model.state_dict(),
            "model_config": {
                "n_features": self.model.n_features,
                "d_model": self.model.d_model,
                "window_size": self.model.window_size,
                "n_cases": self.model.n_cases,
            }
        }, path)
        logger.info(f"Feeder 모델 저장: {path}")

    def save_bytes(self) -> bytes:
        """모델을 bytes로 반환 (S3 저장용)"""
        buffer = io.BytesIO()
        torch.save({
            "model_state_dict": self.model.state_dict(),
            "model_config": {
                "n_features": self.model.n_features,
                "d_model": self.model.d_model,
                "window_size": self.model.window_size,
                "n_cases": self.model.n_cases,
            }
        }, buffer)
        return buffer.getvalue()

    @classmethod
    def load(cls, path: str, device: str = None) -> "FeederTrainer":
        """모델 로드"""
        checkpoint = torch.load(path, map_location="cpu")
        config = checkpoint["model_config"]
        model = PennyFeeder(**config)
        model.load_state_dict(checkpoint["model_state_dict"])
        trainer = cls(model, device=device)
        logger.info(f"Feeder 모델 로드: {path}")
        return trainer
