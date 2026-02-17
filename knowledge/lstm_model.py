"""
LSTM 시계열 예측 모델
- Phase 3: 딥러닝 모델
- 30일 시계열 → 내일 상승/하락 확률
- 1000건+ 데이터 필요
"""
import os
import logging
from typing import Optional, Dict

import numpy as np

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "lstm_model.pt")
MIN_SAMPLES = 1000
SEQ_LENGTH = 30
# OHLCV(5) + RSI(1) + MACD(1) + EMA5(1) + EMA20(1) = 9
N_FEATURES = 9


class LSTMPredictor:
    def __init__(self):
        self.model = None
        self.scaler = None
        self.is_trained = False
        self._load_model()
    
    def train(self, data: np.ndarray) -> Optional[Dict]:
        """
        LSTM 훈련.
        
        Args:
            data: shape (n_days, N_FEATURES) - 시계열 데이터
                  columns: open, high, low, close, volume, rsi, macd, ema5, ema20
                  
        Returns:
            dict with loss, accuracy metrics or None
        """
        if len(data) < MIN_SAMPLES:
            logger.info(f"LSTM 훈련 데이터 부족: {len(data)}/{MIN_SAMPLES}")
            return None
        
        try:
            import torch
            import torch.nn as nn
            from torch.utils.data import DataLoader, TensorDataset
            from sklearn.preprocessing import StandardScaler
            
            # 정규화
            self.scaler = StandardScaler()
            scaled = self.scaler.fit_transform(data)
            
            # 시퀀스 생성
            X, y = [], []
            for i in range(SEQ_LENGTH, len(scaled) - 1):
                X.append(scaled[i - SEQ_LENGTH:i])
                # 내일 종가(idx=3)가 오늘보다 높으면 1
                y.append(1 if data[i + 1, 3] > data[i, 3] else 0)
            
            X = np.array(X, dtype=np.float32)
            y = np.array(y, dtype=np.float32)
            
            # Train/test split
            split = int(len(X) * 0.8)
            X_train, X_test = X[:split], X[split:]
            y_train, y_test = y[:split], y[split:]
            
            train_ds = TensorDataset(torch.from_numpy(X_train), torch.from_numpy(y_train))
            train_loader = DataLoader(train_ds, batch_size=32, shuffle=True)
            
            # 모델 생성
            model = _LSTMNet(N_FEATURES)
            criterion = nn.BCELoss()
            optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
            
            # 훈련
            model.train()
            for epoch in range(20):
                total_loss = 0
                for xb, yb in train_loader:
                    pred = model(xb).squeeze()
                    loss = criterion(pred, yb)
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    total_loss += loss.item()
            
            # 평가
            model.eval()
            with torch.no_grad():
                test_pred = model(torch.from_numpy(X_test)).squeeze()
                test_labels = torch.from_numpy(y_test)
                test_loss = criterion(test_pred, test_labels).item()
                accuracy = float(((test_pred > 0.5).float() == test_labels).float().mean())
            
            self.model = model
            self.is_trained = True
            self._save_model()
            
            metrics = {
                "test_loss": test_loss,
                "accuracy": accuracy,
                "train_size": len(X_train),
                "test_size": len(X_test),
            }
            logger.info(f"LSTM 훈련 완료: acc={accuracy:.3f}, loss={test_loss:.4f}")
            return metrics
            
        except Exception as e:
            logger.error(f"LSTM 훈련 실패: {e}")
            return None
    
    def predict(self, sequence: np.ndarray) -> Optional[float]:
        """
        시계열 데이터로 내일 상승 확률 예측.
        
        Args:
            sequence: shape (SEQ_LENGTH, N_FEATURES) - 최근 30일 데이터
            
        Returns:
            confidence 0-100 (상승 확률), or None
        """
        if not self.is_trained or self.model is None or self.scaler is None:
            return None
        
        try:
            import torch
            
            scaled = self.scaler.transform(sequence)
            x = torch.from_numpy(scaled.astype(np.float32)).unsqueeze(0)
            
            self.model.eval()
            with torch.no_grad():
                prob = self.model(x).squeeze().item()
            
            return float(prob) * 100
            
        except Exception as e:
            logger.error(f"LSTM 예측 실패: {e}")
            return None
    
    def _save_model(self):
        try:
            import torch
            os.makedirs(MODEL_DIR, exist_ok=True)
            state = {
                "model_state": self.model.state_dict(),
                "scaler": self.scaler,
            }
            torch.save(state, MODEL_PATH)
            logger.info(f"LSTM 모델 저장: {MODEL_PATH}")
        except Exception as e:
            logger.error(f"LSTM 저장 실패: {e}")
    
    def _load_model(self):
        try:
            import torch
            if os.path.exists(MODEL_PATH):
                state = torch.load(MODEL_PATH, map_location="cpu", weights_only=False)
                self.model = _LSTMNet(N_FEATURES)
                self.model.load_state_dict(state["model_state"])
                self.scaler = state["scaler"]
                self.is_trained = True
                logger.info("LSTM 모델 로드 완료")
        except Exception as e:
            logger.warning(f"LSTM 로드 실패: {e}")


class _LSTMNet:
    """PyTorch LSTM 모델 (lazy import 대응)"""
    def __new__(cls, n_features):
        import torch.nn as nn
        
        class Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(n_features, 64, num_layers=2, batch_first=True, dropout=0.2)
                self.fc = nn.Sequential(
                    nn.Linear(64, 32),
                    nn.ReLU(),
                    nn.Dropout(0.2),
                    nn.Linear(32, 1),
                    nn.Sigmoid(),
                )
            
            def forward(self, x):
                out, _ = self.lstm(x)
                return self.fc(out[:, -1, :])
        
        return Net()
