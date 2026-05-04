"""
IndicatorService — 기술지표 계산 단일 진입점
"""

from __future__ import annotations
import logging
import numpy as np
from typing import Optional, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class IndicatorService:
    """기술지표 계산 서비스 — 모든 지표 계산을 여기서 담당"""

    @staticmethod
    def calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
        if not closes or len(closes) < period + 1:
            return None
        try:
            arr = np.array(closes[-(period + 1):], dtype=np.float64)
            deltas = np.diff(arr)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)
            
            avg_gain = gains.mean()
            avg_loss = losses.mean()
            
            if avg_loss == 0: return 100.0
            rs = avg_gain / avg_loss
            return float(100.0 - (100.0 / (1.0 + rs)))
        except Exception:
            return None

    @staticmethod
    def calc_ema(closes: list[float], period: int) -> Optional[float]:
        if not closes or len(closes) < period:
            return None
        try:
            arr = np.array(closes, dtype=np.float64)
            k = 2.0 / (period + 1)
            ema = float(arr[:period].mean())
            for price in arr[period:]:
                ema = float(price) * k + ema * (1.0 - k)
            return ema
        except Exception:
            return None

    @staticmethod
    def calc_ma(closes: list[float], period: int) -> Optional[float]:
        if not closes or len(closes) < period:
            return None
        return float(np.mean(closes[-period:]))

    @staticmethod
    def calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
        if not closes or len(closes) < period: return None
        try:
            tr_values = []
            for i in range(1, len(closes)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                tr_values.append(tr)
            return float(np.mean(tr_values[-period:]))
        except Exception:
            return None

    @staticmethod
    def calc_bollinger_bands(closes: list[float], period: int = 20, std_mult: float = 2.0) -> Optional[dict[str, float]]:
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
        """추세 강도 판정 (0~3)"""
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
        """
        일봉 데이터 기반 매매 맥락 정보를 반환한다. (jang_dong_min.py 로직 복구)
        """
        result = {
            "above_ma20": True, "near_high": False, 
            "daily_ma20": 0.0, "high_25d": 0.0,
            "above_ma60": True, "daily_ma60": 0.0,
            "ma20_slope_up": True
        }

        if len(daily_closes) < 20 or current_price <= 0:
            return result

        # 최근 20일 이동평균
        daily_ma20 = sum(daily_closes[-20:]) / 20
        result["daily_ma20"] = daily_ma20
        result["above_ma20"] = current_price >= daily_ma20

        # MA20 기울기 (최근 3거래일 전 대비)
        if len(daily_closes) >= 23:
            ma20_prev = sum(daily_closes[-23:-3]) / 20
            result["ma20_slope_up"] = daily_ma20 > ma20_prev

        # MA60
        if len(daily_closes) >= 60:
            daily_ma60 = sum(daily_closes[-60:]) / 60
            result["daily_ma60"] = daily_ma60
            result["above_ma60"] = current_price >= daily_ma60

        # 25일 신고가 근처 판정
        n = min(25, len(daily_closes))
        high_25d = max(daily_closes[-n:])
        result["high_25d"] = high_25d
        if high_25d > 0:
            result["near_high"] = current_price >= high_25d * (1.0 - near_high_threshold_pct / 100.0)

        return result

    @staticmethod
    def get_technical_summary(snap: StockSnapshot, cfg: SmartScannerConfig) -> dict[str, Any]:
        """종목의 모든 기술적 상태를 한 번에 계산하여 반환"""
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
