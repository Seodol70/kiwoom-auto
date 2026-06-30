"""
test_handle_signal_characterization.py — OrderManager.handle_signal() 캐릭터라이제이션 테스트

배경(리팩토링 1단계, 2026-06-30): handle_signal()은 273줄(order_manager.py:692-963)로
신호필터링(서킷브레이커/시세신선도/등락률상한/섹터쏠림/중복판정) + 수량계산(FIXED/RISK/EQUAL
3모드) + 주문실행+슬리피지가루 한 메서드에 혼재된 가장 심각한 핫스팟이다.

이 테스트는 향후(리팩토링 4단계) handle_signal()을 Strategy 패턴(수량계산)과 필터체인으로
분해할 때, "거래 로직을 단 한 줄도 바꾸지 않았다"는 것을 보증하는 안전망이다. 각 테스트는
buy()가 호출되는지/안 되는지와 호출됐다면 정확한 qty를 검증한다 — buy() 자체는 _send()를
거쳐 실제 주문을 내므로 buy를 MagicMock으로 패치해 실제 키움 API 호출은 막는다.

이 테스트들은 리팩토링 전후 모두 동일하게 통과해야 한다. 실패하면 거래 로직이 바뀐 것이다.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from order.order_manager import OrderManager, Position
from scanner.models import StockSnapshot


def _make_om(max_positions=5, max_order_amount=1_500_000):
    kiwoom = MagicMock()
    kiwoom._ocx = MagicMock()
    kiwoom._ocx.dynamicCall = MagicMock(return_value=0)
    om = OrderManager(kiwoom, account="1234567890", max_order_amount=max_order_amount, max_positions=max_positions)
    om.state = None  # _is_buy_allowed의 손절락/지수급락 체크를 스킵
    om.buy = MagicMock(return_value="1")  # 실제 주문 전송 차단, 호출 여부/인자만 검증
    return om


def _make_snap(code="005930", name="삼성전자", current_price=70_000, change_pct=2.0,
                total_ask_qty=0, sector="전기전자", updated_at=None):
    snap = StockSnapshot(
        code=code, name=name, current_price=current_price,
        open_price=current_price - 500, high_price=current_price + 500,
        low_price=current_price - 1000, prev_close=current_price - 1000,
        volume=100_000, trade_amount=10_000_000_000, change_pct=change_pct,
        total_ask_qty=total_ask_qty,
    )
    snap.sector = sector
    snap.updated_at = updated_at or datetime.now()
    return snap


class _Signal:
    """ScanSignal 대역 — handle_signal()이 읽는 속성만 최소 구현"""
    def __init__(self, code="005930", name="삼성전자", price=70_000, signal_type="JDM_ENTRY",
                 trend_level=2, trend_prev_level=0, entry_phase=2, values=None):
        self.code = code
        self.name = name
        self.price = price
        self.signal_type = signal_type
        self.trend_level = trend_level
        self.trend_prev_level = trend_prev_level
        self.entry_phase = entry_phase
        self.values = values or {}


def _snap_store_with(snap):
    store = MagicMock()
    store.get_snapshot = MagicMock(return_value=snap)
    return store


# ── 필터링 분기 ──────────────────────────────────────────────────────────

def test_circuit_breaker_blocks_when_daily_stoplosses_reach_limit():
    """daily_max_stoplosses 한도 도달 시 신규 진입 전면 차단"""
    om = _make_om()
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 3
    om._stop_loss_today = {"a", "b", "c"}  # 3건 이미 손절
    om._snap_store = _snap_store_with(_make_snap())

    om.handle_signal(_Signal())

    om.buy.assert_not_called()


def test_stale_snapshot_blocks_entry():
    """시세 갱신 지연 3초 초과 시 거절"""
    om = _make_om()
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    stale_snap = _make_snap(updated_at=datetime.now() - timedelta(seconds=5))
    om._snap_store = _snap_store_with(stale_snap)

    om.handle_signal(_Signal())

    om.buy.assert_not_called()


def test_change_pct_above_cap_blocks_entry():
    """등락률이 cfg.RISK.max_change_pct(기본 22.0) 이상이면 차단"""
    om = _make_om()
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    snap = _make_snap(change_pct=25.0)  # 22.0% 상한 초과
    om._snap_store = _snap_store_with(snap)

    om.handle_signal(_Signal())

    om.buy.assert_not_called()


def test_sector_overweight_blocks_entry():
    """동일 섹터 보유종목이 sector_max_positions(기본 2) 이상이면 차단"""
    om = _make_om()
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    snap = _make_snap(sector="전기전자")
    om._snap_store = _snap_store_with(snap)
    om.positions = {
        "111111": Position(code="111111", name="A", qty=1, avg_price=1000, current_price=1000, sector="전기전자"),
        "222222": Position(code="222222", name="B", qty=1, avg_price=1000, current_price=1000, sector="전기전자"),
    }

    om.handle_signal(_Signal(code="333333", name="C종목"))

    om.buy.assert_not_called()


def test_duplicate_pending_order_blocks_entry():
    """이미 같은 종목이 주문 중(_pending)이면 차단"""
    om = _make_om()
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._snap_store = _snap_store_with(_make_snap())
    om._pending.add("005930")

    om.handle_signal(_Signal())

    om.buy.assert_not_called()


def test_existing_position_without_pyramid_blocks_entry():
    """이미 보유 중이고 피라미딩 조건(수익 미충족) 미충족이면 차단"""
    om = _make_om()
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._snap_store = _snap_store_with(_make_snap(current_price=70_000))
    # avg_price == current_price → 수익률 0%, 피라미딩 최소수익 기준 미충족
    om.positions["005930"] = Position(code="005930", name="삼성전자", qty=1, avg_price=70_000, current_price=70_000)

    om.handle_signal(_Signal())

    om.buy.assert_not_called()


def test_max_positions_reached_queues_signal_and_blocks():
    """포지션+pending 합이 max_positions 이상이면 차단하고 큐에 저장"""
    om = _make_om(max_positions=1)
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._snap_store = _snap_store_with(_make_snap())
    om.positions["999999"] = Position(code="999999", name="기존종목", qty=1, avg_price=1000, current_price=1000)

    sig = _Signal()
    om.handle_signal(sig)

    om.buy.assert_not_called()
    assert om._queued_signal is sig


# ── 수량 계산 3모드 ──────────────────────────────────────────────────────

def test_sizing_mode_equal_default():
    """EQUAL(기본) 모드: 가용예수금 / 남은슬롯 으로 수량 산출"""
    om = _make_om(max_positions=5, max_order_amount=10_000_000)
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._scan_cfg.position_sizing_mode = "EQUAL"
    om.cash = 5_000_000
    om._snap_store = _snap_store_with(_make_snap(current_price=70_000))

    om.handle_signal(_Signal(price=70_000))

    # remaining_slots = 5 - 0 - 0 = 5, budget = 5_000_000 // 5 = 1_000_000, qty = 1_000_000 // 70_000 = 14
    om.buy.assert_called_once()
    _, kwargs_or_args = om.buy.call_args, om.buy.call_args
    called_qty = om.buy.call_args.args[2] if len(om.buy.call_args.args) > 2 else om.buy.call_args.kwargs.get("qty")
    assert called_qty == 14


def test_sizing_mode_fixed():
    """FIXED 모드: fixed_order_amount // price"""
    om = _make_om(max_positions=5, max_order_amount=10_000_000)
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._scan_cfg.position_sizing_mode = "FIXED"
    om._scan_cfg.fixed_order_amount = 1_500_000
    om.cash = 10_000_000
    om._snap_store = _snap_store_with(_make_snap(current_price=70_000))

    om.handle_signal(_Signal(price=70_000))

    # qty = 1_500_000 // 70_000 = 21
    om.buy.assert_called_once()
    called_qty = om.buy.call_args.args[2] if len(om.buy.call_args.args) > 2 else om.buy.call_args.kwargs.get("qty")
    assert called_qty == 21


def test_sizing_mode_risk():
    """RISK 모드: (총자산 * risk_pct/100) / (진입가 - 손절가)"""
    om = _make_om(max_positions=5, max_order_amount=100_000_000)
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._scan_cfg.position_sizing_mode = "RISK"
    om._scan_cfg.risk_per_trade_pct = 1.0
    om._scan_cfg.jdm_stop_loss_pct = -1.2
    om.cash = 10_000_000
    om._snap_store = _snap_store_with(_make_snap(current_price=70_000))

    om.handle_signal(_Signal(price=70_000))

    # total_equity(cash=10_000_000, 포지션 없음) = 10_000_000
    # risk_amount = 10_000_000 * 0.01 = 100_000
    # stop_price = 70_000 * (1 - 1.2/100) = 69_160
    # risk_per_share = max(1, 70_000 - 69_160) = 840
    # qty = 100_000 // 840 = 119
    om.buy.assert_called_once()
    called_qty = om.buy.call_args.args[2] if len(om.buy.call_args.args) > 2 else om.buy.call_args.kwargs.get("qty")
    assert called_qty == 119


def test_qty_zero_blocks_entry():
    """계산된 수량이 0이면 거절 (예수금 부족 등)"""
    om = _make_om(max_positions=5, max_order_amount=10_000_000)
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._scan_cfg.position_sizing_mode = "EQUAL"
    om.cash = 100  # 가용예수금 100원 — 70,000원 종목 1주도 못 삼
    om._snap_store = _snap_store_with(_make_snap(current_price=70_000))

    om.handle_signal(_Signal(price=70_000))

    om.buy.assert_not_called()


def test_invalid_price_blocks_entry():
    """signal.price <= 0이면 즉시 거절 (snap_store 없을 때 fallback 경로)"""
    om = _make_om()
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._snap_store = None
    om._kiwoom.get_stock_info = MagicMock(return_value=None)

    om.handle_signal(_Signal(price=0))

    om.buy.assert_not_called()


# ── 주문한도/예수금 조정 ─────────────────────────────────────────────────

def test_order_amount_cap_reduces_qty():
    """1회 주문한도(max_order_amount)를 초과하면 수량이 줄어든다"""
    om = _make_om(max_positions=1, max_order_amount=500_000)  # 주문한도 50만원
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._scan_cfg.position_sizing_mode = "FIXED"
    om._scan_cfg.fixed_order_amount = 10_000_000  # 큰 예산이지만 주문한도가 더 작음
    om.cash = 50_000_000
    om._snap_store = _snap_store_with(_make_snap(current_price=10_000))

    om.handle_signal(_Signal(price=10_000))

    # FIXED 1차 qty = 10_000_000 // 10_000 = 1000
    # max_qty = 500_000 // 10_000 = 50 → 조정
    om.buy.assert_called_once()
    called_qty = om.buy.call_args.args[2] if len(om.buy.call_args.args) > 2 else om.buy.call_args.kwargs.get("qty")
    assert called_qty == 50


def test_pyramid_halves_qty():
    """피라미딩(기존 보유+수익조건 충족) 시 수량이 pyramid_order_ratio(기본 0.5)만큼 줄어든다"""
    om = _make_om(max_positions=5, max_order_amount=100_000_000)
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._scan_cfg.position_sizing_mode = "EQUAL"
    om.cash = 10_000_000
    # 평단 60,000 / 현재가 70,000 → 수익률 +16.7% → 피라미딩 최소수익(기본 3%) 충족
    om.positions["005930"] = Position(code="005930", name="삼성전자", qty=1, avg_price=60_000, current_price=70_000, entry_count=1)
    om._snap_store = _snap_store_with(_make_snap(current_price=70_000))

    om.handle_signal(_Signal(price=70_000))

    # remaining_slots = 5-1-0=4, budget = 10_000_000//4 = 2_500_000, qty = 2_500_000//70_000 = 35
    # 피라미딩 0.5배 -> max(1, int(35*0.5)) = 17
    om.buy.assert_called_once()
    called_qty = om.buy.call_args.args[2] if len(om.buy.call_args.args) > 2 else om.buy.call_args.kwargs.get("qty")
    assert called_qty == 17


def test_liquidity_guard_caps_qty_at_30pct_of_ask_qty():
    """매수 수량이 매도 총잔량의 30%를 넘지 않도록 제한"""
    om = _make_om(max_positions=5, max_order_amount=100_000_000)
    om._scan_cfg = MagicMock()
    om._scan_cfg.daily_max_stoplosses = 0
    om._scan_cfg.position_sizing_mode = "FIXED"
    om._scan_cfg.fixed_order_amount = 100_000_000  # 충분히 큰 예산
    om.cash = 200_000_000
    # price=1000, fixed_order_amount로 qty=100000주 계산되지만 ask_qty=100주라 30%=30주로 제한
    om._snap_store = _snap_store_with(_make_snap(current_price=1_000, total_ask_qty=100))

    om.handle_signal(_Signal(price=1_000))

    om.buy.assert_called_once()
    called_qty = om.buy.call_args.args[2] if len(om.buy.call_args.args) > 2 else om.buy.call_args.kwargs.get("qty")
    assert called_qty == 30
