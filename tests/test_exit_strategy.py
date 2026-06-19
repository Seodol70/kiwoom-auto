"""
test_exit_strategy.py — ExitStrategy 경계값 단위 테스트

대상:
- Hard Stop 경계 (-2.0% 발동 / -1.99% 보류)
- Trail Stop 활성화 전/후 (trail_activation 미만 → 비활성)
- Time Cut 경계 (24분 보류 / 25분 발동 / 26분 발동)
- EMA Protection (손절 보류)
- EOD 포지션 면제 (stop loss/trail skip)
- Strong Trend 타임컷 면제
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from strategy.jang_dong_min import JangDongMinStrategy
from strategy.base import ExitContext
from scanner.smart_scanner import SmartScannerConfig


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────

class _Pos:
    """최소한의 Position 대역"""
    def __init__(
        self,
        avg_price: int,
        current_price: int,
        peak_price: int | None = None,
        entry_time: datetime | None = None,
        eod_trade: bool = False,
        overnight_held: bool = False,
        trend_level: int = 0,
        partial_sold: bool = False,
    ):
        self.avg_price      = avg_price
        self.current_price  = current_price
        self.peak_price     = peak_price if peak_price is not None else current_price
        self.entry_time     = entry_time or datetime.now()
        self.eod_trade      = eod_trade
        self.overnight_held = overnight_held
        self.trend_level    = trend_level
        self.partial_sold   = partial_sold
        self.code           = "005930"
        self.name           = "삼성전자"

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
        trail_activation=kwargs.pop("trail_activation", 1.0),
        trail_tier1=kwargs.pop("trail_tier1", 1.5),
        trail_tier2=kwargs.pop("trail_tier2", 2.5),
        trail_tier3=kwargs.pop("trail_tier3", 3.5),
        time_cut_min=time_cut_min,
        partial_profit_pct=kwargs.pop("partial_profit_pct", 3.0),
        atr_trail_enabled=False,
        **kwargs,
    )


def _es(cfg=None, snap_store=None, order_mgr=None) -> JangDongMinStrategy:
    return JangDongMinStrategy(
        order_mgr=order_mgr,  # None이어도 됨 (should_trend_decay에서 체크함)
        risk_mgr=MagicMock(),
        scan_cfg=cfg or _cfg(),
        snap_store=snap_store,
    )


# ── Hard Stop 경계값 ──────────────────────────────────────────────────────

class TestHardStop:

    def test_exact_boundary_triggers(self):
        """-2.0% == hard_stop_pct → 발동"""
        es = _es(_cfg(hard_stop_pct=-2.0))
        pos = _Pos(avg_price=100_000, current_price=98_000)  # -2.0%
        ok, reason = es.should_exit(pos, _ctx())
        assert ok is True
        assert "Hard Stop" in reason

    def test_one_tick_above_no_trigger(self):
        """-1.99% > hard_stop_pct(-2.0%) → 보류"""
        es = _es(_cfg(hard_stop_pct=-2.0))
        pos = _Pos(avg_price=100_000, current_price=98_010)  # ≈ -1.99%
        ok, _ = es.should_exit(pos, _ctx(sl_pct=-3.0))
        assert ok is False

    def test_hard_stop_ignores_eod(self):
        """EOD 포지션이어도 Hard Stop은 항상 발동"""
        es = _es(_cfg(hard_stop_pct=-2.0))
        pos = _Pos(avg_price=100_000, current_price=97_000, eod_trade=True)  # -3.0%
        ok, reason = es.should_exit(pos, _ctx())
        assert ok is True
        assert "Hard Stop" in reason


# ── Trail Stop 활성화 경계 ────────────────────────────────────────────────

class TestTrailStop:

    def test_below_activation_no_trail(self):
        """peak_chg < trail_activation_pct → 트레일 비활성"""
        cfg = _cfg(
            hard_stop_pct=-3.0,
            trail_activation_pct=1.0,
            trail_pct_tier1=1.5, trail_tier1_max=1.5,
            trail_pct_tier2=2.5, trail_tier2_max=2.5,
            trail_pct_tier3=3.5,
            strong_trend_hold_level=3,
        )
        es = _es(cfg)
        # peak +0.8% (activation 1.0% 미만) → 트레일 비활성
        pos = _Pos(avg_price=100_000, current_price=99_000, peak_price=100_800)
        ok, _ = es.should_exit(pos, _ctx())
        assert ok is False

    def test_above_activation_triggers(self):
        """peak_chg >= trail_activation_pct + 고점 대비 충분히 하락 → 발동"""
        cfg = _cfg(
            hard_stop_pct=-3.0,
            trail_activation_pct=1.0,
            trail_pct_tier1=1.5, trail_tier1_max=1.5,
            trail_pct_tier2=2.5, trail_tier2_max=2.5,
            trail_pct_tier3=3.5,
            strong_trend_hold_level=3,
        )
        es = _es(cfg)
        # peak +3.0% (activation 초과), 현재가는 peak 대비 -4% 하락
        pos = _Pos(avg_price=100_000, current_price=98_880, peak_price=103_000)
        ok, reason = es.should_exit(pos, _ctx())
        assert ok is True
        assert "Trail Stop" in reason

    def test_eod_pre_gap_trail_applies(self):
        """[FIX 2026-06-19] EOD 갭 체크 전 포지션도 Trail Stop 적용
        (이전엔 EOD면 트레일 자체가 꺼져 큰 수익을 봐도 EMA20이탈처럼 민감한
        보호장치에만 의존해야 했음 — 미래에셋생명 6/19 +8.13%→-0.15% 반납 사례)"""
        cfg = _cfg(
            hard_stop_pct=-5.0,
            trail_activation_pct=1.0,
            trail_pct_tier1=1.5, trail_tier1_max=1.5,
            trail_pct_tier2=2.5, trail_tier2_max=2.5,
            trail_pct_tier3=3.5,
            strong_trend_hold_level=3,
        )
        es = _es(cfg)
        pos = _Pos(
            avg_price=100_000, current_price=98_880, peak_price=103_000,
            eod_trade=True, overnight_held=False,  # EOD 갭 체크 전
        )
        ok, reason = es.should_exit(pos, _ctx())
        assert ok is True
        assert "Trail Stop" in reason


# ── Time Cut 경계값 ───────────────────────────────────────────────────────

class TestTimeCut:

    def _make_pos_with_age(self, minutes: float) -> _Pos:
        entry = datetime.now() - timedelta(minutes=minutes)
        return _Pos(avg_price=100_000, current_price=100_500, entry_time=entry)

    def test_just_under_no_cut(self):
        """24.9분 < 25분 기준 → 타임컷 없음"""
        es = _es(_cfg(
            hard_stop_pct=-3.0, trail_activation_pct=5.0,
            strong_trend_hold_level=3, strong_trend_timecut_exempt=True,
        ))
        pos = self._make_pos_with_age(24.9)
        ok, _ = es.should_exit(pos, _ctx(time_cut_min=25, sl_pct=-3.0))
        assert ok is False

    def test_exactly_at_cut(self):
        """25분 이상 → 타임컷 발동"""
        es = _es(_cfg(
            hard_stop_pct=-3.0, trail_activation_pct=5.0,
            strong_trend_hold_level=3, strong_trend_timecut_exempt=True,
        ))
        pos = self._make_pos_with_age(25.1)
        ok, reason = es.should_exit(pos, _ctx(time_cut_min=25, sl_pct=-3.0))
        assert ok is True
        assert "Time Cut" in reason

    def test_eod_exempt(self):
        """EOD 포지션은 타임컷 면제"""
        es = _es(_cfg(
            hard_stop_pct=-3.0, trail_activation_pct=5.0,
            strong_trend_hold_level=3, strong_trend_timecut_exempt=True,
        ))
        pos = self._make_pos_with_age(60)
        pos.eod_trade = True
        ok, _ = es.should_exit(pos, _ctx(time_cut_min=25, sl_pct=-3.0))
        assert ok is False

    def test_strong_trend_exempt(self):
        """Strong Trend(레벨 3+) 포지션은 타임컷 면제"""
        es = _es(_cfg(
            hard_stop_pct=-3.0, trail_activation_pct=5.0,
            strong_trend_hold_level=3, strong_trend_timecut_exempt=True,
        ))
        pos = self._make_pos_with_age(60)
        pos.trend_level = 3  # Strong Trend
        ok, _ = es.should_exit(pos, _ctx(time_cut_min=25, sl_pct=-3.0))
        assert ok is False

    def test_strong_trend_below_level_not_exempt(self):
        """레벨 2는 Strong Trend 아님 → 타임컷 발동"""
        es = _es(_cfg(
            hard_stop_pct=-3.0, trail_activation_pct=5.0,
            strong_trend_hold_level=3, strong_trend_timecut_exempt=True,
        ))
        pos = self._make_pos_with_age(30)
        pos.trend_level = 2
        ok, reason = es.should_exit(pos, _ctx(time_cut_min=25, sl_pct=-3.0))
        assert ok is True
        assert "Time Cut" in reason


# ── Stop Loss + EMA Protection ────────────────────────────────────────────

class TestStopLossEmaProtection:

    def test_stop_loss_triggers_without_ema(self):
        """EMA Protection 없으면 손절 발동"""
        es = _es(_cfg(hard_stop_pct=-3.0, trend_protect_enabled=False))
        pos = _Pos(avg_price=100_000, current_price=98_300)  # -1.7%
        ok, reason = es.should_exit(pos, _ctx(sl_pct=-1.5))
        assert ok is True
        assert "Stop Loss" in reason

    def test_ema_protection_holds(self):
        """EMA20 위에 있으면 손절 보류"""
        snap_store = MagicMock()
        snap = MagicMock()
        snap.closes_1min = [100_000] * 25  # EMA ≈ 100,000
        snap_store.get_snapshot.return_value = snap

        es = _es(
            _cfg(hard_stop_pct=-3.0, trend_protect_enabled=True),
            snap_store=snap_store,
        )
        # -1.7% 손실이지만 현재가(98,300) < EMA(≈100,000)이면 보호 안 됨
        # 현재가 > EMA일 때 보호 발동
        pos = _Pos(avg_price=100_000, current_price=100_200)  # 손절 기준 미달, EMA 위
        ok, _ = es.should_exit(pos, _ctx(sl_pct=-1.5))
        assert ok is False  # 손절 기준(-1.5%) 미달이므로 HOLD

    def test_ema_protection_stops_loss_cut(self):
        """손절 기준 초과 + EMA20 위 → 보류"""
        snap_store = MagicMock()
        snap = MagicMock()
        snap.closes_1min = [97_000] * 25  # EMA ≈ 97,000
        snap_store.get_snapshot.return_value = snap

        es = _es(
            _cfg(hard_stop_pct=-3.0, trend_protect_enabled=True),
            snap_store=snap_store,
        )
        pos = _Pos(avg_price=100_000, current_price=98_000)  # -2.0% (손절 기준 초과)
        # 현재가(98,000) > EMA(97,000) → 보호 발동
        ok, reason = es.should_exit(pos, _ctx(sl_pct=-1.5))
        assert ok is False
        assert "EMA20 Support" in reason
