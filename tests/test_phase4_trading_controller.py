"""
test_phase4_trading_controller.py — Phase 5 이후: ExitStrategy 청산 전략 단위 테스트

Phase 5에서 _check_* 메서드들이 TradingController → ExitStrategy로 통합됨.
테스트 대상:
1. should_partial_exit    — 분할익절
2. _should_breakeven_stop — 본절가 스탑
3. _should_ema20_exit     — EMA20 이탈
4. _should_trend_decay    — 추세소멸
"""

import pytest
from unittest.mock import MagicMock

from app.strategy import ExitStrategy, ExitContext
from order.order_manager import Position
from scanner.smart_scanner import SmartScannerConfig


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────

def _make_strategy(scan_cfg=None, order_mgr=None, snap_store=None):
    if scan_cfg is None:
        scan_cfg = SmartScannerConfig()
    return ExitStrategy(scan_cfg=scan_cfg, snap_store=snap_store, order_mgr=order_mgr)


def _make_ctx(**kwargs):
    defaults = dict(
        sl_pct=1.5,
        trail_activation=0.57,
        trail_tier1=1.1,
        trail_tier2=2.0,
        trail_tier3=3.0,
        time_cut_min=25,
        partial_profit_pct=3.0,
        atr_trail_enabled=False,
    )
    defaults.update(kwargs)
    return ExitContext(**defaults)


def _make_pos(avg_price=80_000, current_price=82_400, **attrs):
    pos = Position(
        code="005930",
        name="삼성전자",
        qty=10,
        avg_price=avg_price,
        current_price=current_price,
    )
    for k, v in attrs.items():
        setattr(pos, k, v)
    return pos


# ── 분할익절 (should_partial_exit) ────────────────────────────────────────

class TestPartialExit:

    def test_disabled(self):
        """partial_profit_enabled=False → (False, 0.0)"""
        cfg = SmartScannerConfig()
        cfg.partial_profit_enabled = False
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=82_400)  # +3%
        ok, ratio = es.should_partial_exit(pos, _make_ctx(partial_profit_pct=3.0))
        assert ok is False

    def test_already_sold(self):
        """partial_sold=True → skip"""
        cfg = SmartScannerConfig()
        cfg.partial_profit_enabled = True
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=82_400, partial_sold=True)
        ok, _ = es.should_partial_exit(pos, _make_ctx(partial_profit_pct=3.0))
        assert ok is False

    def test_target_not_reached(self):
        """+1.875% < 3% 목표 → skip"""
        cfg = SmartScannerConfig()
        cfg.partial_profit_enabled = True
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=81_500)  # +1.875%
        ok, _ = es.should_partial_exit(pos, _make_ctx(partial_profit_pct=3.0))
        assert ok is False

    def test_target_reached(self):
        """+3% 도달 → (True, sell_ratio)"""
        cfg = SmartScannerConfig()
        cfg.partial_profit_enabled = True
        cfg.partial_sell_ratio = 0.30
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=82_400)  # +3%
        ok, ratio = es.should_partial_exit(pos, _make_ctx(partial_profit_pct=3.0))
        assert ok is True
        assert abs(ratio - 0.30) < 1e-9


# ── 본절가 스탑 (_should_breakeven_stop) ────────────────────────────────

class TestBreakevenStop:

    def test_disabled(self):
        cfg = SmartScannerConfig()
        cfg.breakeven_stop_enabled = False
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=79_000, partial_sold=True)
        assert es._should_breakeven_stop(pos) is False

    def test_before_partial_sell(self):
        """partial_sold=False → 적용 안 함"""
        cfg = SmartScannerConfig()
        cfg.breakeven_stop_enabled = True
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=79_000, partial_sold=False)
        assert es._should_breakeven_stop(pos) is False

    def test_triggers_after_partial_sell(self):
        """partial_sold=True + 손실 → True"""
        cfg = SmartScannerConfig()
        cfg.breakeven_stop_enabled = True
        cfg.breakeven_stop_buffer_pct = 0.0
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=79_000, partial_sold=True)  # -1.25%
        assert es._should_breakeven_stop(pos) is True


# ── EMA20 이탈 (_should_ema20_exit) ──────────────────────────────────────

class TestEma20Exit:

    def test_disabled(self):
        cfg = SmartScannerConfig()
        cfg.ema20_exit_enabled = False
        es = _make_strategy(scan_cfg=cfg)
        pos = _make_pos(current_price=78_000)
        assert es._should_ema20_exit(pos) is False

    def test_no_snap_store(self):
        """snap_store 없으면 skip"""
        cfg = SmartScannerConfig()
        cfg.ema20_exit_enabled = True
        es = _make_strategy(scan_cfg=cfg, snap_store=None)
        pos = _make_pos(current_price=78_000)
        assert es._should_ema20_exit(pos) is False

    def test_above_ema20(self):
        """현재가 > EMA20 → False"""
        cfg = SmartScannerConfig()
        cfg.ema20_exit_enabled = True
        cfg.ema20_exit_buffer_pct = 0.0
        snap_store = MagicMock()
        snap = MagicMock()
        snap.closes_1min = [80_000] * 25
        snap_store.get_snapshot.return_value = snap
        es = _make_strategy(scan_cfg=cfg, snap_store=snap_store)
        pos = _make_pos(current_price=85_000)  # EMA ≈ 80000, 현재가 > EMA
        assert es._should_ema20_exit(pos) is False


# ── 추세소멸 (_should_trend_decay) ───────────────────────────────────────

class TestTrendDecay:

    def test_eod_skip(self):
        """EOD 포지션 → skip"""
        es = _make_strategy()
        pos = _make_pos(current_price=82_400, eod_trade=True)
        assert es._should_trend_decay(pos) is False

    def test_loss_skip(self):
        """손실 구간 → skip"""
        es = _make_strategy()
        pos = _make_pos(current_price=78_000)  # -2.5%
        assert es._should_trend_decay(pos) is False

    def test_no_signal(self):
        """should_exit_on_trend_decay=False → False"""
        mock_order_mgr = MagicMock()
        mock_order_mgr.should_exit_on_trend_decay.return_value = False
        es = _make_strategy(order_mgr=mock_order_mgr)
        pos = _make_pos(current_price=82_400)  # +3%
        assert es._should_trend_decay(pos) is False

    def test_triggers(self):
        """should_exit_on_trend_decay=True + 이익 구간 → True"""
        mock_order_mgr = MagicMock()
        mock_order_mgr.should_exit_on_trend_decay.return_value = True
        es = _make_strategy(order_mgr=mock_order_mgr)
        pos = _make_pos(current_price=82_400)  # +3%
        assert es._should_trend_decay(pos) is True


# ── ExitContext 기본 테스트 ───────────────────────────────────────────────

class TestExitContext:

    def test_creation(self):
        ctx = _make_ctx()
        assert ctx.sl_pct == 1.5
        assert ctx.trail_activation == 0.57
        assert ctx.partial_profit_pct == 3.0
        assert ctx.atr_trail_enabled is False

    def test_midday_values(self):
        ctx = _make_ctx(sl_pct=2.0, trail_activation=1.0, time_cut_min=15)
        assert ctx.sl_pct == 2.0
        assert ctx.time_cut_min == 15
