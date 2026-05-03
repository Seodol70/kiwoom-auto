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
            arr = np.array(closes, dtype=np.float64)
            k = 2.0 / (period + 1)
            ema = float(arr[:period].mean())  # 초기값: 첫 period개의 단순 평균
            for price in arr[period:]:
                ema = float(price) * k + ema * (1.0 - k)
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

            tr_arr = np.array(tr_values, dtype=np.float64)
            atr = float(tr_arr[:period].mean())  # 초기값: 첫 period개 단순 평균
            alpha = 1.0 / period
            for v in tr_arr[period:]:            # Wilder smoothing
                atr = (1.0 - alpha) * atr + alpha * float(v)
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
        closes: list[float],
        highs: list[float],
        lows: list[float],
        volumes: list[int],
        ema_period: int = 20,
        atr_period: int = 14,
        volume_lookback: int = 20,
    ) -> int:
        """
        [고도화] 추세 강도 판정 알고리즘 (Level 0~3).
        
        평가 요소:
        1. 가격 위치 (Distance): EMA 대비 ATR 단위 거리
        2. 기울기 (Slope): EMA의 단기 기울기
        3. 가속도 (Acceleration): 기울기의 변화량 (추세 강화 확인)
        4. 수급 (Volume): 최근 평균 대비 거래량 폭발력
        """
        need_price = max(ema_period + 5, atr_period + 1)
        if len(closes) < need_price or len(highs) < need_price or len(lows) < need_price:
            return 0

        # ── 지표 계산 ──
        ema_now = IndicatorService.calc_ema(closes, ema_period)
        ema_prev = IndicatorService.calc_ema(closes[:-1], ema_period)
        ema_prev2 = IndicatorService.calc_ema(closes[:-2], ema_period)
        atr = IndicatorService.calc_atr(highs, lows, closes, atr_period)
        
        if any(v is None for v in [ema_now, ema_prev, ema_prev2, atr]) or atr <= 0:
            return 0

        cur_price = float(closes[-1])
        
        # [필수] 상승 추세 기초 조건: 가격 > EMA AND EMA 기울기 > 0
        if cur_price <= ema_now or ema_now <= ema_prev:
            return 0

        # 1️⃣ 거리 점수 (Distance Score)
        dist_atr = (cur_price - ema_now) / atr
        
        # 2️⃣ 가속도 점수 (Slope Acceleration)
        slope_now = ema_now - ema_prev
        slope_prev = ema_prev - ema_prev2
        is_accelerating = (slope_now > slope_prev)

        # 3️⃣ 수급 점수 (Volume Score)
        need_vol = volume_lookback + 1
        has_vol = (len(volumes) >= need_vol and any(v > 0 for v in volumes[-need_vol:]))
        vol_ratio = 1.0
        if has_vol:
            avg_vol = float(np.mean(np.array(volumes[-(need_vol):-1], dtype=np.float64)))
            if avg_vol > 0:
                vol_ratio = float(volumes[-1]) / avg_vol
            else:
                has_vol = False

        # 4️⃣ 박스권 응축 확인 (Consolidation) - 변동성이 극도로 낮아진 상태
        # ATR이 가격의 0.4% 이하이면 응축으로 판단
        is_consolidating = (atr / cur_price < 0.004)

        # ── 종합 판정 ──
        # [Level 3: Strong] 강력한 추세 + 수급 + 가속 (또는 응축 후 돌파)
        if dist_atr >= 1.2 and vol_ratio >= 1.5 and is_accelerating:
            return 3
        if dist_atr >= 0.8 and vol_ratio >= 2.0 and is_consolidating:
            # 박스권 응축 후 거래량 실린 돌파는 거리가 짧아도 강력
            return 3
        if dist_atr >= 1.5 and vol_ratio >= 1.2:
            return 3
        
        # [Level 2: Medium] 안정적 추세 + 수급
        if dist_atr >= 0.7 and vol_ratio >= 1.1:
            return 2
        if dist_atr >= 1.0:
            return 2
            
        # [Level 1: Weak] 추세 시작 초기
        if dist_atr >= 0.2:
            return 1
            
        return 0

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
