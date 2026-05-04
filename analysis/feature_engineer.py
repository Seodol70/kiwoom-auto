from datetime import datetime
from typing import Optional
import logging
from scanner.indicator_service import IndicatorService

logger = logging.getLogger(__name__)

def extract_ml_features(sig, snap, cfg=None) -> dict:
    """
    ScanSignal, StockSnapshot, Config를 바탕으로 
    ML 모델 학습 및 예측에 필요한 특성(Features)을 추출합니다.
    """
    try:
        closes = list(getattr(snap, "closes_1min", None) or [])
        volumes = list(getattr(snap, "volumes_1min", None) or [])
        
        # 기본 기간 설정
        ma_s_period = getattr(cfg, "jdm_ma_short", 7) if cfg else 7
        ma_l_period = getattr(cfg, "jdm_ma_long", 15) if cfg else 15
        ema_s_period = getattr(cfg, "ema_disp_short", 10) if cfg else 10
        ema_l_period = getattr(cfg, "ema_disp_long", 20) if cfg else 20
        vol_lookback = getattr(cfg, "volume_surge_lookback", 10) if cfg else 10

        # 지표 계산
        rsi = IndicatorService.calc_rsi(closes, 14)
        ma_s = IndicatorService.calc_ma(closes, ma_s_period)
        ma_l = IndicatorService.calc_ma(closes, ma_l_period)
        ema_s = IndicatorService.calc_ema(closes, ema_s_period)
        ema_l = IndicatorService.calc_ema(closes, ema_l_period)

        # 거래량 급증 배수
        vol_ratio = 1.0
        if len(volumes) >= vol_lookback + 1:
            avg_vol = sum(volumes[-(vol_lookback + 1):-1]) / vol_lookback
            if avg_vol > 0:
                vol_ratio = volumes[-1] / avg_vol

        # 지수 대비 강도 (RS Score) 계산
        # 005930 -> KOSPI(001), 247540 -> KOSDAQ(101)
        # 종목코드 6자리 중 앞자리가 0/1/2 등은 코스피, 나머지는 코스닥 (단순화)
        # 실제로는 Kiwoom API에서 알려주지만 여기서는 간단히 구분
        is_kosdaq = sig.code.startswith(('2', '3', '9')) or len(sig.code) > 6 # 대략적 구분
        idx_chg = getattr(cfg, 'kosdaq_chg_pct', 0) if is_kosdaq else getattr(cfg, 'kospi_chg_pct', 0)
        rs_score = round(getattr(snap, 'change_pct', 0) - idx_chg, 2)

        # 결과 딕셔너리 (DB 컬럼명과 일치)
        features = {
            "signal_price":             getattr(sig, "price", 0),
            "rsi_at_signal":            round(rsi, 2) if rsi is not None else 0,
            "ma_short_at_signal":       round(ma_s, 0) if ma_s is not None else 0,
            "ma_long_at_signal":        round(ma_l, 0) if ma_l is not None else 0,
            "ema_short_at_signal":      round(ema_s, 0) if ema_s is not None else 0,
            "ema_long_at_signal":       round(ema_l, 0) if ema_l is not None else 0,
            "chejan_strength_at_signal":round(getattr(snap, 'chejan_strength', 0), 1),
            "volume_ratio_at_signal":   round(vol_ratio, 2),
            "change_pct_at_signal":     round(getattr(snap, 'change_pct', 0), 2),
            "kospi_chg_at_signal":      round(getattr(cfg, 'kospi_chg_pct', 0), 2) if cfg else 0,
            "kosdaq_chg_at_signal":     round(getattr(cfg, 'kosdaq_chg_pct', 0), 2) if cfg else 0,
            "investor_score_at_signal": int(getattr(snap, "investor_score", 0)),
            "rs_score":                 rs_score,
        }
        return features
    except Exception as e:
        logger.error("[FeatureEngineer] 추출 실패: %s", e)
        return {}
