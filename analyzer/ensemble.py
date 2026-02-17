"""
앙상블 예측기
- 규칙 기반 + XGBoost + LSTM 결합
- 데이터 양에 따라 활성 모델 자동 선택
"""
import logging
from typing import Dict, Optional, Tuple

import numpy as np

from knowledge.ml_model import XGBoostPredictor
from knowledge.lstm_model import LSTMPredictor
from knowledge.feature_engineer import extract_features

logger = logging.getLogger(__name__)


class EnsemblePredictor:
    def __init__(self):
        self.xgb = XGBoostPredictor()
        self.lstm = LSTMPredictor()
    
    def predict(self, stock_data: Dict, rule_signal: Optional[str] = None,
                rule_confidence: Optional[float] = None,
                data_count: int = 0) -> Tuple[str, float]:
        """
        앙상블 예측.
        
        Args:
            stock_data: 현재 시장 데이터 (지표 포함)
            rule_signal: 규칙 기반 시그널 (BUY/SELL/WATCH/STOP)
            rule_confidence: 규칙 기반 confidence (0-100)
            data_count: 축적된 포지션 데이터 수
            
        Returns:
            (signal_type, confidence) tuple
        """
        if rule_signal is None:
            rule_signal = "WATCH"
        if rule_confidence is None:
            rule_confidence = 50.0
        
        # 피처 추출
        features = extract_features(stock_data)
        
        # 가중치 결정
        if data_count >= 1000:
            weights = {"rule": 0.2, "xgb": 0.4, "lstm": 0.4}
        elif data_count >= 300:
            weights = {"rule": 0.4, "xgb": 0.6, "lstm": 0.0}
        else:
            weights = {"rule": 1.0, "xgb": 0.0, "lstm": 0.0}
        
        # 규칙 기반 점수 (BUY=confidence, SELL=100-confidence)
        rule_score = rule_confidence if rule_signal in ("BUY",) else (100 - rule_confidence) if rule_signal == "SELL" else 50.0
        
        total_weight = weights["rule"]
        weighted_score = rule_score * weights["rule"]
        
        # XGBoost 예측
        if weights["xgb"] > 0 and features is not None:
            xgb_conf = self.xgb.predict(features)
            if xgb_conf is not None:
                weighted_score += xgb_conf * weights["xgb"]
                total_weight += weights["xgb"]
                logger.debug(f"XGBoost confidence: {xgb_conf:.1f}")
            else:
                # fallback: 규칙에 가중치 재분배
                weighted_score += rule_score * weights["xgb"]
                total_weight += weights["xgb"]
        
        # LSTM 예측
        if weights["lstm"] > 0:
            sequence = stock_data.get("sequence")  # shape (30, 9)
            if sequence is not None:
                lstm_conf = self.lstm.predict(np.array(sequence))
                if lstm_conf is not None:
                    weighted_score += lstm_conf * weights["lstm"]
                    total_weight += weights["lstm"]
                    logger.debug(f"LSTM confidence: {lstm_conf:.1f}")
                else:
                    weighted_score += rule_score * weights["lstm"]
                    total_weight += weights["lstm"]
            else:
                weighted_score += rule_score * weights["lstm"]
                total_weight += weights["lstm"]
        
        # 최종 confidence
        final_confidence = weighted_score / total_weight if total_weight > 0 else 50.0
        final_confidence = max(0, min(100, final_confidence))
        
        # 시그널 결정
        if final_confidence >= 65:
            signal = "BUY"
        elif final_confidence <= 35:
            signal = "SELL"
        else:
            signal = rule_signal if rule_signal in ("WATCH", "STOP") else "WATCH"
        
        active = [k for k, v in weights.items() if v > 0]
        logger.info(f"앙상블 예측: {signal} ({final_confidence:.1f}%) | 활성모델: {active} | 데이터: {data_count}건")
        
        return signal, final_confidence
