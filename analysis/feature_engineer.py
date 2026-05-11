"""
feature_engineer — ML 모델용 특성 추출 브릿지

신호 데이터(ScanSignal) + 스냅샷(StockSnapshot)을 입력받아
IndicatorService의 19개 AI 피처를 생성하고 반환합니다.
"""
from typing import Optional
import logging
from scanner.indicator_service import IndicatorService

logger = logging.getLogger(__name__)

def extract_ml_features(sig, snap, cfg=None) -> dict:
    """
    ScanSignal, StockSnapshot, Config를 입력받아
    ML 모델 학습/추론용 정규화된 특성(19개 피처)을 추출합니다.

    Args:
        sig: ScanSignal — 거래 신호 (price, signal_type 등)
        snap: StockSnapshot — 종목 스냅샷 (OHLC, 지표, 기술 데이터)
        cfg: SmartScannerConfig — 스캐너 설정 (선택)

    Returns:
        dict — 19개의 정규화된 피처 (f_rsi, f_ema20_gap, ..., f_hoga_ratio)
               또는 {} (데이터 부족 시)

    설명:
        - IndicatorService.get_ai_features()에서 19개 피처 생성
        - ML Trainer의 RandomForest 모델과 동기화됨
        - 모든 피처는 -1.0 ~ 1.0 범위로 정규화됨
    """
    try:
        # IndicatorService의 정규화된 19개 피처 추출 로직 재사용
        features = IndicatorService.get_ai_features(snap, config=cfg)

        if not features:
            return {}

        # 추가 메타데이터 (선택적)
        features["signal_price"] = getattr(sig, "price", 0)

        return features
    except Exception as e:
        logger.error("[FeatureEngineer] 추출 실패: %s", e)
        return {}
