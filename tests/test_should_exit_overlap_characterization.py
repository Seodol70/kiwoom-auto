"""
test_should_exit_overlap_characterization.py — should_exit() 중첩 경계 케이스 캐릭터라이제이션

배경(리팩토링 1단계, 2026-06-30): JangDongMinStrategy.should_exit()(strategy/jang_dong_min.py:
276-387)은 _hard_stop/_gap_sl/_gap_tp/_early_relax를 함수 앞부분에서 한 번 계산해 이후
여러 단계(하드스탑/익절/일반손절)에 걸쳐 공유하는 절차형 파이프라인이다. 기존
test_exit_strategy.py는 각 청산사유를 독립적으로 검증하지만, "갭종목 동적손절(09:00~10:00
진입 + 갭>=2%) + early_hold_sec 초반완화가 동시에 적용되는" 중첩 경계는 다루지 않는다.

이 테스트는 향후(리팩토링 6단계) should_exit()을 사전계산 Extract + 순서보존 함수열거로
재구조화할 때, "중간 계산값(_hard_stop, _gap_sl 등)이 우연히 달라지는" 회귀를 막는 안전망이다.
"""

from datetime import datetime, time as dtime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from strategy.jang_dong_min import JangDongMinStrategy
from strategy.base import ExitContext
from scanner.smart_scanner import SmartScannerConfig


# 09:15 진입(갭 동적 구간 09:00~10:00 안쪽, OPENING 강화 구간 09:00~09:30과도 겹침)
_FAKE_NOW = datetime(2026, 1, 5, 9, 45, 0)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FAKE_NOW


def _freeze_now():
    return patch("strategy.jang_dong_min.datetime", _FrozenDateTime)


class _GapPos:
    """갭종목 경계 케이스 전용 Position 대역 — entry_gap_pct 포함"""

    def __init__(self, avg_price, current_price, entry_time=None, entry_gap_pct=0.0,
                 peak_price=None, eod_trade=False, overnight_held=False, trend_level=0,
                 vel_ratio=0.0, partial_sold=False):
        self.avg_price = avg_price
        self.current_price = current_price
        self.peak_price = peak_price if peak_price is not None else current_price
        self.entry_time = entry_time or _FAKE_NOW
        self.entry_gap_pct = entry_gap_pct
        self.eod_trade = eod_trade
        self.overnight_held = overnight_held
        self.trend_level = trend_level
        self.vel_ratio = vel_ratio
        self.partial_sold = partial_sold
        self.code = "005930"
        self.name = "삼성전자"

    @property
    def price_change_pct_vs_avg(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price * 100.0


def _cfg(**kwargs) -> SmartScannerConfig:
    cfg = SmartScannerConfig()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def _ctx(time_cut_min: int = 0, sl_pct: float = -1.5, **kwargs) -> ExitContext:
    return ExitContext(
        sl_pct=sl_pct,
        trail_activation=kwargs.pop("trail_activation", 99.0),  # 트레일 비활성화(기본 안 걸리게)
        trail_tier1=kwargs.pop("trail_tier1", 1.5),
        trail_tier2=kwargs.pop("trail_tier2", 2.5),
        trail_tier3=kwargs.pop("trail_tier3", 3.5),
        time_cut_min=time_cut_min,
        partial_profit_pct=kwargs.pop("partial_profit_pct", 3.0),
        atr_trail_enabled=False,
        **kwargs,
    )


class _FrozenStrategy:
    def __init__(self, strategy):
        self._strategy = strategy

    def should_exit(self, *args, **kwargs):
        with _freeze_now():
            return self._strategy.should_exit(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._strategy, name)


def _es(cfg=None) -> JangDongMinStrategy:
    strategy = JangDongMinStrategy(order_mgr=None, risk_mgr=MagicMock(), scan_cfg=cfg or _cfg())
    return _FrozenStrategy(strategy)


# ── 갭 동적 손절 단독 (early_hold 없음) ──────────────────────────────────

def test_gap_dynamic_tier1_hard_stop_overrides_default():
    """갭 3%(tier1: 2~5%) 진입 → Hard Stop = gap_sl_tier1_stop(-2.0) * 1.5 = -3.0%"""
    es = _es(_cfg(hard_stop_pct=-3.0, gap_dynamic_sl_enabled=True,
                   gap_sl_tier1_stop=-2.0, early_hold_sec=0))
    pos = _GapPos(avg_price=100_000, current_price=97_000, entry_gap_pct=3.0)  # -3.0%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-5.0))
    assert ok is True
    assert "Hard Stop" in reason


def test_gap_dynamic_tier1_general_stop_at_gap_sl():
    """갭 3% 진입, 하드스탑 미달이지만 갭 동적 일반손절(-2.0%)에는 도달"""
    es = _es(_cfg(hard_stop_pct=-5.0, gap_dynamic_sl_enabled=True,
                   gap_sl_tier1_stop=-2.0, early_hold_sec=0, trend_protect_enabled=False))
    pos = _GapPos(avg_price=100_000, current_price=98_000, entry_gap_pct=3.0)  # -2.0%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-5.0))
    assert ok is True
    assert "GAP동적" in reason


# ── early_hold_sec 단독 (갭 비활성) ───────────────────────────────────────

def test_early_hold_relaxes_hard_stop_within_window():
    """진입 직후(early_hold_sec 300초 이내)는 하드스탑이 early_sl_relax_pct만큼 완화된다.
    -3.3%는 완화된 하드스탑(-3.5%)에도, 일반손절(-5.0%, 미도달)에도 안 걸려 HOLD"""
    es = _es(_cfg(hard_stop_pct=-2.0, gap_dynamic_sl_enabled=False,
                   early_hold_sec=300, early_sl_relax_pct=1.5))
    # entry_time = _FAKE_NOW, now() - entry_time = 0초 (윈도우 안) -> hard_stop = -2.0-1.5 = -3.5%
    pos = _GapPos(avg_price=100_000, current_price=96_700, entry_time=_FAKE_NOW)  # -3.3%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-5.0))
    assert ok is False  # -3.3% > -3.5%(완화된 하드스탑)이므로 보류
    assert reason == "HOLD"


def test_early_hold_window_expired_uses_normal_hard_stop():
    """early_hold_sec 경과 후에는 완화가 적용되지 않는다"""
    es = _es(_cfg(hard_stop_pct=-2.0, gap_dynamic_sl_enabled=False,
                   early_hold_sec=300, early_sl_relax_pct=1.5))
    old_entry = _FAKE_NOW - timedelta(seconds=301)  # 윈도우 밖
    pos = _GapPos(avg_price=100_000, current_price=96_700, entry_time=old_entry)  # -3.3%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-5.0))
    assert ok is True  # 완화 없음, hard_stop=-2.0%이므로 -3.3% <= -2.0% 발동
    assert "Hard Stop" in reason


# ── 중첩: 갭 동적 + early_hold 동시 적용 ─────────────────────────────────

def test_gap_dynamic_and_early_hold_stack_on_hard_stop():
    """갭종목(tier1, hard_stop=-2.0*1.5=-3.0%) + early_hold 완화(-1.5%p) 동시 적용
    -> 최종 hard_stop = -3.0-1.5 = -4.5%. 단 -4.4%는 그 전에 갭동적 일반손절
    (_sl_pct = gap_sl(-2.0) - early_relax(1.5) = -3.5%)에 먼저 걸려 GAP동적 Stop Loss로 발동한다"""
    es = _es(_cfg(hard_stop_pct=-5.0, gap_dynamic_sl_enabled=True,
                   gap_sl_tier1_stop=-2.0, early_hold_sec=300, early_sl_relax_pct=1.5,
                   trend_protect_enabled=False))
    pos = _GapPos(avg_price=100_000, current_price=95_600, entry_gap_pct=3.0,
                   entry_time=_FAKE_NOW)  # -4.4%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-10.0))
    assert ok is True
    assert "GAP동적" in reason
    assert "(-3.5%)" in reason


def test_gap_dynamic_and_early_hold_stack_trigger_beyond_combined_threshold():
    """동일 조건에서 -4.6%는 하드스탑(-4.5%) 미달이지만, 갭동적 일반손절
    (_sl_pct = gap_sl(-2.0) - early_relax(1.5) = -3.5%)에 먼저 걸려 GAP동적 Stop Loss로 발동한다"""
    es = _es(_cfg(hard_stop_pct=-5.0, gap_dynamic_sl_enabled=True,
                   gap_sl_tier1_stop=-2.0, early_hold_sec=300, early_sl_relax_pct=1.5,
                   trend_protect_enabled=False))
    pos = _GapPos(avg_price=100_000, current_price=95_400, entry_gap_pct=3.0,
                   entry_time=_FAKE_NOW)  # -4.6%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-10.0))
    assert ok is True
    assert "GAP동적" in reason
    assert "(-3.5%)" in reason


def test_gap_dynamic_hard_stop_reachable_when_gap_sl_deeper_than_default():
    """[발견] 갭동적 하드스탑은 min(hard_stop_pct, gap_sl*1.5)로 "더 깊은" 쪽이 선택된다.
    hard_stop_pct(기본 전역값)가 gap_sl*1.5보다 얕으면(여기선 -1.0%) gap_sl*1.5(-3.0%)가
    선택되어 -3.0-early_relax(1.5)=-4.5%가 실제 하드스탑이 되고, 충분히 깊은 하락(-10%)에서는
    GAP동적 일반손절(-3.5%)보다 하드스탑(-4.5%)이 더 얕아 먼저 걸린다."""
    es = _es(_cfg(hard_stop_pct=-1.0, gap_dynamic_sl_enabled=True,
                   gap_sl_tier1_stop=-2.0, early_hold_sec=300, early_sl_relax_pct=1.5,
                   trend_protect_enabled=False))
    pos = _GapPos(avg_price=100_000, current_price=95_400, entry_gap_pct=3.0,
                   entry_time=_FAKE_NOW)  # -4.6%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-99.0))
    assert ok is True
    assert "Hard Stop" in reason
    assert "(-4.5%)" in reason


def test_gap_below_2pct_does_not_trigger_dynamic_sl():
    """갭이 2% 미만이면 갭 동적 손절 자체가 비활성 — 일반 sl_pct(-1.5%) 경로가
    하드스탑(기본 -5.0%)보다 먼저 걸려 Stop Loss로 발동한다"""
    es = _es(_cfg(hard_stop_pct=-5.0, gap_dynamic_sl_enabled=True,
                   gap_sl_tier1_stop=-2.0, early_hold_sec=0, trend_protect_enabled=False))
    pos = _GapPos(avg_price=100_000, current_price=98_300, entry_gap_pct=1.5,
                   entry_time=_FAKE_NOW)  # -1.7%, 갭<2%라 동적 비활성
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-1.5))
    assert ok is True
    assert "GAP동적" not in reason  # 일반 Stop Loss 경로(태그 없음)
    assert "Stop Loss" in reason


def test_gap_dynamic_inactive_outside_0900_1000_window():
    """갭>=2%여도 진입 시각이 09:00~10:00 밖이면 갭 동적 손절 비활성 —
    -2.0%는 하드스탑(-5.0%)에도, 일반손절(-1.5%)에도 안 걸려 HOLD로 보류된다"""
    es = _es(_cfg(hard_stop_pct=-5.0, gap_dynamic_sl_enabled=True,
                   gap_sl_tier1_stop=-2.0, early_hold_sec=0, trend_protect_enabled=False))
    late_entry = datetime(2026, 1, 5, 11, 0, 0)  # 11:00 진입 — 갭동적 구간 밖
    pos = _GapPos(avg_price=100_000, current_price=98_000, entry_gap_pct=3.0,
                   entry_time=late_entry)  # -2.0%
    ok, reason = es.should_exit(pos, _ctx(sl_pct=-1.5))
    assert ok is False
    assert reason == "HOLD"
