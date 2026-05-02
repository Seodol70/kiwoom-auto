"""
IndicatorService — 기술지표 계산 단일 진입점

현재 jang_dong_min.py, smart_scanner.py 등에 분산된
calc_rsi, calc_atr, calc_ema, calc_ma 등을 통합.

SmartScanner, ScannerWorker, TradingEngine이 모두 이 모듈에서 import.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


class IndicatorService:
    """기술지표 계산 서비스 — 모든 지표 계산을 여기서 담당"""

    @staticmethod
    def calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
        """
        RSI(상대강도지수) 계산.

        Args:
            closes: 종가 리스트 (최신값이 마지막)
            period: RSI 기간 (기본 14)

        Returns:
            RSI 값 (0~100), 계산 불가 시 None
        """
        if not closes or len(closes) < period + 1:
            return None

        try:
            closes_arr = np.array(closes[-period - 1 :], dtype=np.float64)
            deltas = np.diff(closes_arr)
            seed = deltas[:period]

            up = seed[seed >= 0].sum() / period
            down = -seed[seed < 0].sum() / period

            rs = up / down if down else 0
            rsi = 100.0 - 100.0 / (1.0 + rs) if rs >= 0 else 0.0

            for d in deltas[period:]:
                up = (up * (period - 1) + (d if d >= 0 else 0)) / period
                down = (down * (period - 1) + (-d if d < 0 else 0)) / period
                rs = up / down if down else 0
                rsi = 100.0 - 100.0 / (1.0 + rs) if rs >= 0 else 0.0

            return rsi
        except Exception:
            return None

    @staticmethod
    def calc_ema(closes: list[float], period: int) -> Optional[float]:
        """
        EMA(지수이동평균) 계산 — 최신 값 반환.

        Args:
            closes: 종가 리스트 (최신값이 마지막)
            period: EMA 기간

        Returns:
            최신 EMA 값, 계산 불가 시 None
        """
        if not closes or len(closes) < period:
            return None

        try:
            closes_arr = np.array(closes[-period - 50 :], dtype=np.float64)
            ema = closes_arr[0]
            k = 2.0 / (period + 1)

            for close in closes_arr[1:]:
                ema = close * k + ema * (1 - k)

            return ema
        except Exception:
            return None

    @staticmethod
    def calc_atr(
        highs: list[float], lows: list[float], closes: list[float], period: int = 14
    ) -> Optional[float]:
        """
        ATR(평균진정범위) 계산.

        Args:
            highs: 고가 리스트
            lows: 저가 리스트
            closes: 종가 리스트
            period: ATR 기간 (기본 14)

        Returns:
            ATR 값, 계산 불가 시 None
        """
        if not closes or len(closes) < period:
            return None

        try:
            tr_values = []
            for i in range(1, min(len(closes), len(highs), len(lows))):
                high = highs[i]
                low = lows[i]
                prev_close = closes[i - 1]

                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                tr_values.append(tr)

            if len(tr_values) < period:
                return None

            atr = np.mean(tr_values[-period:])
            return atr
        except Exception:
            return None

    @staticmethod
    def calc_ma(closes: list[float], period: int) -> Optional[float]:
        """
        단순이동평균(SMA) 계산.

        Args:
            closes: 종가 리스트 (최신값이 마지막)
            period: 이동평균 기간

        Returns:
            MA 값, 계산 불가 시 None
        """
        if not closes or len(closes) < period:
            return None

        try:
            return np.mean(closes[-period:])
        except Exception:
            return None

    @staticmethod
    def calc_bollinger_bands(
        closes: list[float], period: int = 20, num_std: float = 2.0
    ) -> Optional[dict]:
        """
        볼린저 밴드 계산.

        Args:
            closes: 종가 리스트
            period: 이동평균 기간 (기본 20)
            num_std: 표준편차 배수 (기본 2.0)

        Returns:
            {'middle': MA, 'upper': 상단, 'lower': 하단}, 계산 불가 시 None
        """
        if not closes or len(closes) < period:
            return None

        try:
            closes_arr = np.array(closes[-period:], dtype=np.float64)
            middle = np.mean(closes_arr)
            std = np.std(closes_arr)
            upper = middle + (std * num_std)
            lower = middle - (std * num_std)
            return {"middle": middle, "upper": upper, "lower": lower}
        except Exception:
            return None

    @staticmethod
    def get_trend_status(
        ma7: Optional[float],
        ma15: Optional[float],
        ma20: Optional[float],
        current_price: int,
    ) -> dict:
        """
        추세 상태 판정.

        Args:
            ma7: 7일 이동평균
            ma15: 15일 이동평균
            ma20: 20일 이동평균
            current_price: 현재가

        Returns:
            {
                'is_uptrend': bool,
                'is_aligned': bool (정배열),
                'level': int (0=no trend, 1=weak, 2=normal, 3=strong)
            }
        """
        result = {"is_uptrend": False, "is_aligned": False, "level": 0}

        if not ma7 or not ma15 or not ma20:
            return result

        # 정배열 확인: ma7 > ma15 > ma20
        is_aligned = ma7 > ma15 > ma20
        result["is_aligned"] = is_aligned

        if not is_aligned:
            return result

        # 현재가가 모든 MA 위에 있는가
        is_above_all = current_price > ma7 > ma15 > ma20
        result["is_uptrend"] = is_uptrend = is_aligned and is_above_all

        # 추세 강도 판정
        if is_uptrend:
            diff_7_15 = (ma7 - ma15) / ma15 * 100
            diff_15_20 = (ma15 - ma20) / ma20 * 100

            if diff_7_15 > 3.0 and diff_15_20 > 2.0:
                result["level"] = 3  # 강한 상승
            elif diff_7_15 > 1.5 and diff_15_20 > 1.0:
                result["level"] = 2  # 보통 상승
            else:
                result["level"] = 1  # 약한 상승

        return result

    @staticmethod
    def check_daily_alignment(
        daily_closes: list[float],
        current_price: int,
        ma_periods: tuple[int, int, int] = (7, 15, 20),
    ) -> dict:
        """
        일봉 기반 정배열 확인.

        Args:
            daily_closes: 일봉 종가 리스트
            current_price: 현재 시간대 현재가
            ma_periods: (단기, 중기, 장기) MA 기간

        Returns:
            {
                'is_aligned': bool,
                'ma_short': float,
                'ma_mid': float,
                'ma_long': float,
            }
        """
        short_period, mid_period, long_period = ma_periods
        result = {
            "is_aligned": False,
            "ma_short": None,
            "ma_mid": None,
            "ma_long": None,
        }

        if not daily_closes:
            return result

        try:
            ma_short = IndicatorService.calc_ma(daily_closes, short_period)
            ma_mid = IndicatorService.calc_ma(daily_closes, mid_period)
            ma_long = IndicatorService.calc_ma(daily_closes, long_period)

            result["ma_short"] = ma_short
            result["ma_mid"] = ma_mid
            result["ma_long"] = ma_long

            if ma_short and ma_mid and ma_long:
                result["is_aligned"] = ma_short > ma_mid > ma_long

            return result
        except Exception:
            return result
