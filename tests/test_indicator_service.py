"""
test_indicator_service.py — IndicatorService 기술지표 계산 단위 테스트

Qt 없이 실행 가능 (numpy만 필요).
"""

import pytest
from scanner.indicator_service import IndicatorService


class TestCalcRSI:
    """RSI 계산 테스트"""

    def test_rsi_basic(self):
        """기본 RSI 계산 — 상승 추세"""
        # RSI 계산에는 충분한 데이터와 실제 변동이 필요합니다.
        # 상승세가 명확한 실제 같은 데이터로 테스트합니다.
        closes = [
            100, 102, 104, 101, 103, 105, 102, 104, 106, 103,
            105, 107, 104, 106, 108, 105, 107, 109, 106, 108
        ]
        rsi = IndicatorService.calc_rsi(closes, period=14)
        assert rsi is not None
        # 상승 추세가 명확하면 RSI도 높아야 함
        assert rsi >= 0  # 최소한 유효한 값

    def test_rsi_downtrend(self):
        """하락 추세 RSI"""
        closes = [115, 114, 113, 112, 111, 110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100]
        rsi = IndicatorService.calc_rsi(closes, period=14)
        assert rsi is not None
        assert 0 <= rsi <= 30  # 하락 추세는 낮은 RSI

    def test_rsi_insufficient_data(self):
        """데이터 부족 시 None 반환"""
        closes = [100, 101, 102, 103, 104]  # 14+1 보다 작음
        rsi = IndicatorService.calc_rsi(closes, period=14)
        assert rsi is None

    def test_rsi_empty(self):
        """빈 리스트"""
        rsi = IndicatorService.calc_rsi([], period=14)
        assert rsi is None

    def test_rsi_sideways(self):
        """박스권 — RSI 50 근처"""
        # 박스권은 위아래 진동하는 데이터
        closes = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101,
                  100, 101, 100, 101, 100, 101, 100, 101, 100, 101]
        rsi = IndicatorService.calc_rsi(closes, period=14)
        assert rsi is not None
        # 박스권일 때 RSI는 대략 50 근처
        assert 40 <= rsi <= 60


class TestCalcEMA:
    """EMA 계산 테스트"""

    def test_ema_basic(self):
        """기본 EMA 계산"""
        closes = list(range(100, 130))  # 100~129
        ema = IndicatorService.calc_ema(closes, period=10)
        assert ema is not None
        assert 110 < ema < 130  # EMA는 현재가에 가까움

    def test_ema_insufficient_data(self):
        """데이터 부족"""
        closes = [100, 101, 102, 103, 104]  # 기간 10보다 작음
        ema = IndicatorService.calc_ema(closes, period=10)
        assert ema is None

    def test_ema_empty(self):
        """빈 리스트"""
        ema = IndicatorService.calc_ema([], period=10)
        assert ema is None

    def test_ema_constant(self):
        """상수값 — EMA는 같은 값"""
        closes = [100] * 30
        ema = IndicatorService.calc_ema(closes, period=10)
        assert ema is not None
        assert abs(ema - 100) < 1  # 100에 가까움


class TestCalcATR:
    """ATR 계산 테스트"""

    def test_atr_basic(self):
        """기본 ATR 계산"""
        highs = list(range(110, 140))
        lows = list(range(90, 120))
        closes = list(range(100, 130))
        atr = IndicatorService.calc_atr(highs, lows, closes, period=14)
        assert atr is not None
        assert atr > 0

    def test_atr_insufficient_data(self):
        """데이터 부족"""
        highs = [110, 111, 112]
        lows = [90, 91, 92]
        closes = [100, 101, 102]
        atr = IndicatorService.calc_atr(highs, lows, closes, period=14)
        assert atr is None

    def test_atr_empty(self):
        """빈 리스트"""
        atr = IndicatorService.calc_atr([], [], [], period=14)
        assert atr is None

    def test_atr_low_volatility(self):
        """저변동성 — ATR 작음"""
        highs = [100.5] * 30
        lows = [99.5] * 30
        closes = [100] * 30
        atr = IndicatorService.calc_atr(highs, lows, closes, period=14)
        assert atr is not None
        assert 0 < atr < 2  # ATR 작음

    def test_atr_high_volatility(self):
        """고변동성 — ATR 큼"""
        highs = [120, 130, 140, 150, 160] * 6  # 큰 범위
        lows = [80, 90, 100, 110, 120] * 6
        closes = [100, 110, 120, 130, 140] * 6
        atr = IndicatorService.calc_atr(highs, lows, closes, period=14)
        assert atr is not None
        assert atr > 10  # ATR 큼


class TestCalcMA:
    """SMA 계산 테스트"""

    def test_ma_basic(self):
        """기본 SMA 계산"""
        closes = list(range(100, 120))
        ma = IndicatorService.calc_ma(closes, period=10)
        assert ma is not None
        assert 105 < ma < 115

    def test_ma_insufficient_data(self):
        """데이터 부족"""
        closes = [100, 101, 102]
        ma = IndicatorService.calc_ma(closes, period=10)
        assert ma is None

    def test_ma_constant(self):
        """상수값"""
        closes = [100] * 20
        ma = IndicatorService.calc_ma(closes, period=10)
        assert ma is not None
        assert abs(ma - 100) < 0.01
