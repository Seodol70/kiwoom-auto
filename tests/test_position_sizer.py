"""
test_position_sizer.py — PositionSizer(FIXED/RISK/EQUAL) 단위 테스트

배경(리팩토링 4단계, 2026-06-30): OrderManager.handle_signal()의 수량 계산
3모드(원래 order_manager.py:846-882)를 Strategy 패턴(order/position_sizer.py)으로
추출했다. 이 테스트는 추출된 각 Sizer가 원본 if/elif/else 블록과 정확히 같은 값을
계산하는지 검증한다(tests/test_handle_signal_characterization.py의 사이징 테스트와
동일한 기대값을 사용 — 양쪽 다 통과해야 회귀가 없다고 확신할 수 있다).
"""

from unittest.mock import MagicMock

import pytest

from order.position_sizer import FixedSizer, RiskSizer, EqualSizer, get_position_sizer


def _make_cfg(**kwargs):
    cfg = MagicMock()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def _make_order_mgr(cash=0, total_equity=0, max_positions=5, positions=None, pending=None):
    om = MagicMock()
    om.available_cash = cash
    om.total_equity = total_equity
    om.max_positions = max_positions
    om.positions = positions or {}
    om._pending = pending or set()
    return om


# ── FixedSizer ───────────────────────────────────────────────────────────

def test_fixed_sizer_basic():
    """budget // price (원래 order_manager.py:851-852)"""
    cfg = _make_cfg(fixed_order_amount=1_500_000)
    om = _make_order_mgr()
    qty = FixedSizer().calculate(price=70_000, scan_cfg=cfg, order_mgr=om)
    assert qty == 21  # 1_500_000 // 70_000


def test_fixed_sizer_zero_price_returns_zero():
    cfg = _make_cfg(fixed_order_amount=1_500_000)
    om = _make_order_mgr()
    qty = FixedSizer().calculate(price=0, scan_cfg=cfg, order_mgr=om)
    assert qty == 0


# ── RiskSizer ────────────────────────────────────────────────────────────

def test_risk_sizer_basic():
    """(총자산*risk%) / (진입가-손절가) (원래 order_manager.py:858-867)"""
    cfg = _make_cfg(risk_per_trade_pct=1.0, jdm_stop_loss_pct=-1.2)
    om = _make_order_mgr(total_equity=10_000_000)
    qty = RiskSizer().calculate(price=70_000, scan_cfg=cfg, order_mgr=om)
    # risk_amount = 10_000_000*0.01 = 100_000
    # stop_price = 70_000*(1-1.2/100) = 69_160, risk_per_share = max(1, 840) = 840
    # qty = 100_000 // 840 = 119
    assert qty == 119


def test_risk_sizer_zero_price_returns_zero():
    cfg = _make_cfg(risk_per_trade_pct=1.0, jdm_stop_loss_pct=-1.2)
    om = _make_order_mgr(total_equity=10_000_000)
    qty = RiskSizer().calculate(price=0, scan_cfg=cfg, order_mgr=om)
    assert qty == 0


# ── EqualSizer ───────────────────────────────────────────────────────────

def test_equal_sizer_basic():
    """available_cash // remaining_slots // price (원래 order_manager.py:877-881)"""
    cfg = _make_cfg()
    om = _make_order_mgr(cash=5_000_000, max_positions=5, positions={}, pending=set())
    qty = EqualSizer().calculate(price=70_000, scan_cfg=cfg, order_mgr=om)
    # remaining_slots = 5-0-0=5, budget = 5_000_000//5 = 1_000_000, qty = 1_000_000//70_000 = 14
    assert qty == 14


def test_equal_sizer_remaining_slots_floor_is_one():
    """포지션+pending이 max_positions를 넘어도 remaining_slots는 최소 1로 바닥 처리된다"""
    cfg = _make_cfg()
    om = _make_order_mgr(
        cash=1_000_000, max_positions=1,
        positions={"a": object(), "b": object()}, pending={"c"},
    )
    qty = EqualSizer().calculate(price=10_000, scan_cfg=cfg, order_mgr=om)
    # remaining_slots = max(1-2-1, 1) = 1, budget = 1_000_000//1 = 1_000_000, qty = 100
    assert qty == 100


def test_equal_sizer_zero_price_returns_zero():
    cfg = _make_cfg()
    om = _make_order_mgr(cash=1_000_000)
    qty = EqualSizer().calculate(price=0, scan_cfg=cfg, order_mgr=om)
    assert qty == 0


# ── get_position_sizer (모드 선택) ────────────────────────────────────────

def test_get_position_sizer_fixed():
    assert isinstance(get_position_sizer("FIXED"), FixedSizer)


def test_get_position_sizer_risk():
    assert isinstance(get_position_sizer("RISK"), RiskSizer)


def test_get_position_sizer_equal():
    assert isinstance(get_position_sizer("EQUAL"), EqualSizer)


def test_get_position_sizer_case_insensitive():
    assert isinstance(get_position_sizer("fixed"), FixedSizer)


def test_get_position_sizer_unknown_mode_falls_back_to_equal():
    """원래 if/elif/else 구조에서 FIXED/RISK가 아니면 항상 else=EQUAL이었던 동작을 보존"""
    assert isinstance(get_position_sizer("UNKNOWN_MODE"), EqualSizer)
