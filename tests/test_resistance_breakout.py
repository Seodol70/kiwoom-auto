"""
test_resistance_breakout.py — RESISTANCE_BREAKOUT(③전략) 단위/통합 테스트

배경(2026-06-30): "오를 종목 포착 3가지 신호" ③번 — 직전 N분 구간 고점(저항선)
돌파 신호. 과거 BREAKOUT(전일종가+3%, 47% 노이즈로 2026-06-02 제거)과 달리
국소 저항선을 기준선으로 삼고, trend_level/거래량 게이트로 허위양성을 줄이는
설계. enabled_strategies에는 아직 미포함(관찰용) — 이 테스트는 신호 판정
로직만 검증한다.
"""
from unittest.mock import MagicMock

import pytest

from scanner.config import SmartScannerConfig
from scanner.evaluators.resistance_breakout import check_resistance_breakout
from scanner.models import StockSnapshot
from scanner.strategies.resistance_breakout import ResistanceBreakoutStrategy


def make_snap(
    code="005930", name="삼성전자",
    current_price=10_500, trend_level=2,
    closes=None, highs=None, volumes=None,
):
    """기본: 직전 20분 고점 10,400 → 현재가 10,500으로 돌파, 마지막 1분 거래량 급증."""
    if closes is None:
        # 21개 봉: [0..19]는 평탄한 흐름(고점 10,400), [20]은 현재 봉(돌파)
        closes = [10_000 + (i % 5) * 50 for i in range(20)] + [current_price]
    if highs is None:
        highs = [10_000 + (i % 5) * 50 + 30 for i in range(20)] + [current_price]
        highs[15] = 10_400  # 명시적 저항선 고점
    if volumes is None:
        volumes = [1_000] * 20 + [5_000]  # 마지막 봉 5배 급증

    return StockSnapshot(
        code=code, name=name, current_price=current_price,
        open_price=current_price - 500, high_price=current_price + 100,
        low_price=current_price - 1000, prev_close=current_price - 1000,
        volume=100_000, trade_amount=10_000_000_000, change_pct=2.0,
        closes_1min=closes, highs_1min=highs, volumes_1min=volumes,
        trend_level=trend_level,
    )


@pytest.fixture
def cfg():
    return SmartScannerConfig()


class TestCheckResistanceBreakout:
    def test_insufficient_data_returns_none(self, cfg):
        snap = make_snap(closes=[10_000] * 5, highs=[10_050] * 5, volumes=[1_000] * 5)
        assert check_resistance_breakout(snap, cfg) is None

    def test_below_resistance_returns_none(self, cfg):
        """저항선(10,400)을 못 넘으면 차단"""
        snap = make_snap(current_price=10_300)
        closes = [10_000 + (i % 5) * 50 for i in range(20)] + [10_300]
        highs = [10_000 + (i % 5) * 50 + 30 for i in range(20)] + [10_300]
        highs[15] = 10_400
        snap = make_snap(current_price=10_300, closes=closes, highs=highs)
        assert check_resistance_breakout(snap, cfg) is None

    def test_breakout_without_volume_surge_blocked(self, cfg):
        """저항선은 돌파했지만 거래량 동반이 없으면 차단"""
        closes = [10_000 + (i % 5) * 50 for i in range(20)] + [10_500]
        highs = [10_000 + (i % 5) * 50 + 30 for i in range(20)] + [10_500]
        highs[15] = 10_400
        volumes = [1_000] * 21  # 거래량 변화 없음
        snap = make_snap(current_price=10_500, closes=closes, highs=highs, volumes=volumes)
        assert check_resistance_breakout(snap, cfg) is None

    def test_breakout_below_min_trend_level_blocked(self, cfg):
        """저항선 돌파 + 거래량 동반해도 trend_level 미달이면 차단"""
        snap = make_snap(current_price=10_500, trend_level=1)
        assert check_resistance_breakout(snap, cfg) is None

    def test_breakout_passes_with_trend_and_volume(self, cfg):
        """저항선 돌파 + trend_lv>=2 + 거래량 급증 → 신호 발생"""
        snap = make_snap(current_price=10_500, trend_level=2)
        reason = check_resistance_breakout(snap, cfg)
        assert reason is not None
        assert "RESISTANCE_BREAKOUT" in reason
        assert "trend_lv2" in reason

    def test_min_trend_level_config_respected(self, cfg):
        """rb_min_trend_level을 3으로 올리면 lv2는 차단된다"""
        cfg.rb_min_trend_level = 3
        snap = make_snap(current_price=10_500, trend_level=2)
        assert check_resistance_breakout(snap, cfg) is None

        snap_lv3 = make_snap(current_price=10_500, trend_level=3)
        assert check_resistance_breakout(snap_lv3, cfg) is not None


class TestResistanceBreakoutStrategy:
    def test_strategy_emits_signal_with_expected_type(self, cfg):
        strat = ResistanceBreakoutStrategy()
        strat._last_signal_ts.clear()
        snap = make_snap(current_price=10_500, trend_level=2)

        sig = strat.evaluate(snap, cfg)

        assert sig is not None
        assert sig.signal_type == "RESISTANCE_BREAKOUT"
        assert sig.code == snap.code

    def test_strategy_cooldown_blocks_repeated_signal(self, cfg):
        strat = ResistanceBreakoutStrategy()
        strat._last_signal_ts.clear()
        snap = make_snap(current_price=10_500, trend_level=2)

        first = strat.evaluate(snap, cfg)
        second = strat.evaluate(snap, cfg)  # 즉시 재호출 — 쿨다운 안쪽

        assert first is not None
        assert second is None

    def test_strategy_returns_none_when_evaluator_rejects(self, cfg):
        strat = ResistanceBreakoutStrategy()
        strat._last_signal_ts.clear()
        snap = make_snap(current_price=10_300)  # 저항선 미돌파
        closes = [10_000 + (i % 5) * 50 for i in range(20)] + [10_300]
        highs = [10_000 + (i % 5) * 50 + 30 for i in range(20)] + [10_300]
        highs[15] = 10_400
        snap = make_snap(current_price=10_300, closes=closes, highs=highs)

        assert strat.evaluate(snap, cfg) is None


class TestEnabledStrategiesExclusion:
    """신규 도입 직후라 enabled_strategies/strategy_order에는 아직 없어야 한다 (관찰 전용)."""

    def test_resistance_breakout_not_in_enabled_strategies(self, cfg):
        assert "RESISTANCE_BREAKOUT" not in cfg.enabled_strategies

    def test_resistance_breakout_not_in_strategy_order(self, cfg):
        assert "RESISTANCE_BREAKOUT" not in cfg.strategy_order
