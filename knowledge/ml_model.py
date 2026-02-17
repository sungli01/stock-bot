"""
XGBoost 기반 매매 승패 예측 모델
- Phase 2: ML 모델
- 300건 이상 포지션 데이터 필요
"""
import os
import logging
from typing import Dict, Optional, Tuple

import numpy as np
import joblib

logger = logging.getLogger(__name__)

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "xgboost_model.pkl")
MIN_SAMPLES = 300


class XGBoostPredictor:
    def __init__(self):
        self.model = None
        self.is_trained = False
        self._load_model()
    
    def train(self, features: np.ndarray, labels: np.ndarray) -> Optional[Dict]:
        """
        XGBoost 모델 훈련.
        
        Args:
            features: shape (n_samples, n_features)
            labels: shape (n_samples,) - 1=win, 0=loss
            
        Returns:
            dict with accuracy, f1 등 metrics, or None if insufficient data
        """
        if len(labels) < MIN_SAMPLES:
            logger.info(f"훈련 데이터 부족: {len(labels)}/{MIN_SAMPLES}")
            return None
        
        try:
            from xgboost import XGBClassifier
            from sklearn.model_selection import train_test_split
            from sklearn.metrics import accuracy_score, f1_score
            
            X_train, X_test, y_train, y_test = train_test_split(
                features, labels, test_size=0.2, random_state=42, stratify=labels
            )
            
            self.model = XGBClassifier(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                use_label_encoder=False,
                eval_metric="logloss",
                random_state=42,
            )
            self.model.fit(X_train, y_train)
            self.is_trained = True
            
            y_pred = self.model.predict(X_test)
            metrics = {
                "accuracy": float(accuracy_score(y_test, y_pred)),
                "f1": float(f1_score(y_test, y_pred, zero_division=0)),
                "train_size": len(y_train),
                "test_size": len(y_test),
            }
            
            self._save_model()
            logger.info(f"XGBoost 훈련 완료: acc={metrics['accuracy']:.3f}, f1={metrics['f1']:.3f}")
            return metrics
            
        except Exception as e:
            logger.error(f"XGBoost 훈련 실패: {e}")
            return None
    
    def predict(self, features: np.ndarray) -> Optional[float]:
        """
        예측 confidence 반환 (0-100).
        
        Args:
            features: shape (n_features,) or (1, n_features)
            
        Returns:
            confidence 0-100, or None if model not ready
        """
        if not self.is_trained or self.model is None:
            return None
        
        try:
            if features.ndim == 1:
                features = features.reshape(1, -1)
            
            proba = self.model.predict_proba(features)[0]
            # proba[1] = win 확률
            confidence = float(proba[1]) * 100
            return confidence
            
        except Exception as e:
            logger.error(f"XGBoost 예측 실패: {e}")
            return None
    
    def _save_model(self):
        try:
            os.makedirs(MODEL_DIR, exist_ok=True)
            joblib.dump(self.model, MODEL_PATH)
            logger.info(f"XGBoost 모델 저장: {MODEL_PATH}")
        except Exception as e:
            logger.error(f"모델 저장 실패: {e}")
    
    def _load_model(self):
        try:
            if os.path.exists(MODEL_PATH):
                self.model = joblib.load(MODEL_PATH)
                self.is_trained = True
                logger.info("XGBoost 모델 로드 완료")
        except Exception as e:
            logger.warning(f"모델 로드 실패: {e}")
            self.model = None
            self.is_trained = False
