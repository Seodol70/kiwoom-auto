"""
IndicatorService — 기술지표 계산 고속화 및 AI 피처 생성
"""

from __future__ import annotations
import logging
import numpy as np
import pandas as pd
from typing import Optional, Any, TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class IndicatorService:
    """기술지표 계산 서비스 — 모든 지표 계산을 고속화하여 담당"""

    @staticmethod
    def calc_rsi(closes: list[float] | np.ndarray, period: int = 14) -> Optional[float]:
        """Wilder's Smoothing 방식의 RSI 고속 계산"""
        if closes is None or len(closes) < period + 1:
            return None
        try:
            if isinstance(closes, list):
                arr = np.array(closes, dtype=np.float64)
            else:
                arr = closes.astype(np.float64)
                
            deltas = np.diff(arr)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)

            if len(gains) < period:
                return None

            # Wilder's Smoothing (alpha = 1/period)
            # pandas ewm(alpha=1/period, adjust=False) 사용
            s_gains = pd.Series(gains)
            s_losses = pd.Series(losses)
            
            avg_gain = s_gains.ewm(alpha=1.0/period, adjust=False).mean().iloc[-1]
            avg_loss = s_losses.ewm(alpha=1.0/period, adjust=False).mean().iloc[-1]

            if avg_loss == 0: return 100.0
            rs = avg_gain / avg_loss
            return float(100.0 - (100.0 / (1.0 + rs)))
        except Exception:
            return None

    @staticmethod
    def calc_ema(closes: list[float] | np.ndarray, period: int) -> Optional[float]:
        """고속 EMA 계산"""
        if closes is None or len(closes) < period:
            return None
        try:
            s = pd.Series(closes)
            return float(s.ewm(span=period, adjust=False).mean().iloc[-1])
        except Exception:
            return None

    @staticmethod
    def calc_ma(closes: list[float] | np.ndarray, period: int) -> Optional[float]:
        """고속 MA 계산"""
        if closes is None or len(closes) < period:
            return None
        return float(np.mean(closes[-period:]))

    @staticmethod
    def calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
        """Wilder's Smoothing 방식의 ATR 고속 계산"""
        if not closes or len(closes) < period + 1: return None
        try:
            h = np.array(highs)
            l = np.array(lows)
            c = np.array(closes)
            
            tr1 = h[1:] - l[1:]
            tr2 = np.abs(h[1:] - c[:-1])
            tr3 = np.abs(l[1:] - c[:-1])
            
            tr = np.maximum.reduce([tr1, tr2, tr3])
            
            # Wilder's Smoothing
            s_tr = pd.Series(tr)
            atr = s_tr.ewm(alpha=1.0/period, adjust=False).mean().iloc[-1]
            
            return float(atr)
        except Exception:
            return None

    @staticmethod
    def calc_bollinger_bands(closes: list[float] | np.ndarray, period: int = 20, std_mult: float = 2.0) -> Optional[dict[str, float]]:
        if len(closes) < period: return None
        try:
            arr = np.array(closes[-period:], dtype=np.float64)
            mid = float(arr.mean())
            std = float(arr.std())
            return {
                "upper": mid + std_mult * std,
                "middle": mid,
                "lower": mid - std_mult * std
            }
        except Exception:
            return None

    @staticmethod
    def get_trend_status(closes: list[float], highs: list[float], lows: list[float], volumes: list[int], **kwargs) -> int:
        """추세 강도 판정 (0~3) 최적화"""
        ema_period = kwargs.get("ema_period", 20)
        atr_period = kwargs.get("atr_period", 14)
        
        if len(closes) < ema_period + 2: return 0
        
        ema_now = IndicatorService.calc_ema(closes, ema_period)
        ema_prev = IndicatorService.calc_ema(closes[:-1], ema_period)
        atr = IndicatorService.calc_atr(highs, lows, closes, atr_period)
        
        if not ema_now or not ema_prev or not atr: return 0
        
        cur_price = closes[-1]
        if cur_price <= ema_now or ema_now <= ema_prev: return 0
        
        dist_atr = (cur_price - ema_now) / atr
        
        if dist_atr >= 1.5: return 3
        if dist_atr >= 1.0: return 2
        if dist_atr >= 0.3: return 1
        return 0

    @staticmethod
    def check_daily_alignment(daily_closes: list[float], current_price: Optional[float] = None) -> dict:
        """일봉 정배열 확인"""
        res = {"is_aligned": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0}
        if len(daily_closes) < 20: return res
        
        closes = list(daily_closes)
        if current_price:
            closes.append(current_price)
            
        res["ma5"] = IndicatorService.calc_ma(closes, 5) or 0.0
        res["ma10"] = IndicatorService.calc_ma(closes, 10) or 0.0
        res["ma20"] = IndicatorService.calc_ma(closes, 20) or 0.0
        
        res["is_aligned"] = res["ma5"] > res["ma10"] > res["ma20"]
        return res

    @staticmethod
    def get_daily_context(
        daily_closes: list[float],
        current_price: float,
        near_high_threshold_pct: float = 3.0,
    ) -> dict:
        """일봉 데이터 기반 매매 맥락 정보 반환"""
        result = {
            "above_ma20": True, "near_high": False, 
            "daily_ma20": 0.0, "high_25d": 0.0,
            "above_ma60": True, "daily_ma60": 0.0,
            "ma20_slope_up": True
        }

        if len(daily_closes) < 20 or current_price <= 0:
            return result

        arr = np.array(daily_closes)
        # 최근 20일 이동평균
        daily_ma20 = np.mean(arr[-20:])
        result["daily_ma20"] = float(daily_ma20)
        result["above_ma20"] = current_price >= daily_ma20

        # MA20 기울기
        if len(arr) >= 23:
            ma20_prev = np.mean(arr[-23:-3])
            result["ma20_slope_up"] = daily_ma20 > ma20_prev

        # MA60
        if len(arr) >= 60:
            daily_ma60 = np.mean(arr[-60:])
            result["daily_ma60"] = float(daily_ma60)
            result["above_ma60"] = current_price >= daily_ma60

        # 25일 신고가
        n = min(25, len(arr))
        high_25d = np.max(arr[-n:])
        result["high_25d"] = float(high_25d)
        if high_25d > 0:
            result["near_high"] = current_price >= high_25d * (1.0 - near_high_threshold_pct / 100.0)

        return result

    @staticmethod
    def get_technical_summary(snap: 'StockSnapshot', cfg: 'SmartScannerConfig') -> dict[str, Any]:
        """종목의 모든 기술적 상태를 통합 반환"""
        closes = snap.closes_1min
        summary = {
            "rsi": IndicatorService.calc_rsi(closes, 14),
            "ema20": IndicatorService.calc_ema(closes, 20),
            "ma20": IndicatorService.calc_ma(closes, 20),
            "daily": IndicatorService.get_daily_context(snap.daily_closes, snap.current_price),
            "trend_level": snap.trend_level
        }
        bb = IndicatorService.calc_bollinger_bands(closes, 20)
        if bb: summary["bb"] = bb
        return summary

    @staticmethod
    def get_ai_features(snap: 'StockSnapshot') -> Dict[str, float]:
        """
        AI 학습용 정규화된 피처 벡터 생성.
        모든 값은 가능한 0~1 또는 -1~1 사이로 정규화되도록 함.
        """
        features = {}
        closes = snap.closes_1min
        if not closes or len(closes) < 20:
            return {}

        try:
            # 1. RSI (0~100 -> 0~1)
            rsi = IndicatorService.calc_rsi(closes, 14)
            features["f_rsi"] = (rsi / 100.0) if rsi is not None else 0.5
            
            # 2. 이평선 이격도 (현재가/EMA20 - 1.0)
            ema20 = IndicatorService.calc_ema(closes, 20)
            if ema20 and ema20 > 0:
                # -0.1 ~ 0.1 범위를 주로 가짐 -> 클리핑 후 매핑
                gap = (snap.current_price / ema20) - 1.0
                features["f_ema20_gap"] = np.clip(gap * 5.0, -1.0, 1.0) # 5배 증폭하여 변별력 확보
            else:
                features["f_ema20_gap"] = 0.0
                
            # 3. 볼린저 밴드 위치 (Percent B: (Price - Lower)/(Upper - Lower))
            bb = IndicatorService.calc_bollinger_bands(closes, 20)
            if bb and (bb["upper"] - bb["lower"]) > 0:
                pct_b = (snap.current_price - bb["lower"]) / (bb["upper"] - bb["lower"])
                features["f_pct_b"] = np.clip(pct_b, 0.0, 1.0)
            else:
                features["f_pct_b"] = 0.5

            # 4. 거래량 Surge (최근 1분 거래량 / 최근 20분 평균 거래량)
            vols = snap.volumes_1min
            if len(vols) >= 20:
                avg_vol = np.mean(vols[-20:-1])
                if avg_vol > 0:
                    features["f_vol_surge"] = min(vols[-1] / avg_vol, 10.0) / 10.0 # 10배 상한
                else:
                    features["f_vol_surge"] = 0.0
            else:
                features["f_vol_surge"] = 0.0

            # 5. 등락률 (당일 등락률 / 30% 상한)
            features["f_change_pct"] = np.clip(snap.change_pct / 30.0, -1.0, 1.0)
            
            # 6. 체결강도 (0~500 -> 0~1)
            features["f_strength"] = np.clip(snap.chejan_strength / 500.0, 0.0, 1.0)
            
            # 7. 추세 단계 (0~3 -> 0~1)
            features["f_trend"] = snap.trend_level / 3.0

        except Exception as e:
            logger.error(f"AI 피처 생성 실패: {snap.code}, {e}")
            
        return features
