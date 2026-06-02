"""
test_h1_trend.py — 60분봉 추세 판정 단위 테스트
"""
import pytest
from scanner.indicator_service import IndicatorService


class TestGetH1Trend:

    def _up(self, n=15, start=100, step=2.0):
        return [start + i * step for i in range(n)]

    def _down(self, n=15, start=130, step=2.0):
        return [start - i * step for i in range(n)]

    def test_uptrend(self):
        closes = self._up(25)  # 20봉 이상이어야 EMA20 + above_ema20 계산 가능
        r = IndicatorService.get_h1_trend(closes)
        assert r["slope"] > 0
        assert r["direction"] == "UP"
        assert r["above_ema20"] is True

    def test_downtrend(self):
        closes = self._down(15)
        r = IndicatorService.get_h1_trend(closes)
        assert r["slope"] < 0
        assert r["direction"] == "DOWN"

    def test_too_few_bars(self):
        r = IndicatorService.get_h1_trend([100, 101])
        assert r["trend"] == 0
        assert r["direction"] == "FLAT"

    def test_rsi_calculated_when_enough_bars(self):
        closes = self._up(20)
        r = IndicatorService.get_h1_trend(closes)
        assert r["rsi"] is not None
        assert 0 <= r["rsi"] <= 100

    def test_rsi_none_when_insufficient(self):
        closes = self._up(8)
        r = IndicatorService.get_h1_trend(closes)
        assert r["rsi"] is None

    def test_above_ema20(self):
        closes = self._up(25)
        r = IndicatorService.get_h1_trend(closes)
        assert r["above_ema20"] is True

    def test_below_ema20(self):
        closes = self._down(25)
        r = IndicatorService.get_h1_trend(closes)
        assert r["above_ema20"] is False

    def test_with_highs_lows(self):
        closes = self._up(15)
        highs = [c + 1 for c in closes]
        lows  = [c - 1 for c in closes]
        r = IndicatorService.get_h1_trend(closes, highs, lows)
        assert r["trend"] >= 0

    def test_flat_market(self):
        closes = [100.0] * 15
        r = IndicatorService.get_h1_trend(closes)
        assert abs(r["slope"]) < 0.1
