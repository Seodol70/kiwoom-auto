from datetime import datetime
from typing import Optional
import logging
from scanner.indicator_service import IndicatorService

logger = logging.getLogger(__name__)

def extract_ml_features(sig, snap, cfg=None) -> dict:
    """
    ScanSignal, StockSnapshot, Config를 바탕으로 
    ML 모델 학습 및 예측에 필요한 정규화된 특성(Features)을 추출합니다.
    """
    try:
        # IndicatorService의 정규화된 피처 추출 로직을 재사용합니다.
        features = IndicatorService.get_ai_features(snap)
        
        # 추가적으로 필요한 정보가 있다면 업데이트 (예: 매수가격 등)
        if features:
            features["signal_price"] = getattr(sig, "price", 0)
            
        return features
    except Exception as e:
        logger.error("[FeatureEngineer] 추출 실패: %s", e)
        return {}
    except Exception as e:
        logger.error("[FeatureEngineer] 추출 실패: %s", e)
        return {}
