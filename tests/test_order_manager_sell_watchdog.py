"""
test_order_manager_sell_watchdog.py — 매도 미체결 watchdog(is_pending) 2단계 동작 검증

배경(2026-06-22): 매도 주문이 30초 미체결이면 즉시 pending을 해제하고 재주문을 허용했는데,
키움이 이전 주문을 여전히 처리 중인 상태에서 동일 rq_name("매도_{code}")으로 새 주문을
계속 내보내 미체결 주문이 누적됐다. 그 결과 포지션이 정상 체결로 청산된 뒤에도, 쌓여 있던
이전 주문들이 시차를 두고 800033(매도가능수량 부족) 거부 응답을 계속 반환해
[포지션청산] 로그 없이 수분간 거부 로그만 반복되는 문제가 있었다(서진시스템 4/30 11:18~11:38 사례).

수정: 30초 경과 시 즉시 재주문을 허용하지 않고 먼저 취소 주문을 발령한 뒤,
추가 유예(_SELL_CANCEL_GRACE_SEC)가 지나야 강제로 pending을 해제한다.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from order.order_manager import OrderManager, Position


def _make_om():
    kiwoom = MagicMock()
    kiwoom._ocx = MagicMock()
    kiwoom._ocx.dynamicCall = MagicMock(return_value=0)
    om = OrderManager(kiwoom, account="1234567890")
    return om


def test_is_pending_true_within_timeout():
    """30초 이내면 그냥 pending=True, 취소 발령 없음"""
    om = _make_om()
    code = "005930"
    om._pending.add(code)
    om._pending_sell_time[code] = datetime.now()

    assert om.is_pending(code) is True
    om._kiwoom._ocx.dynamicCall.assert_not_called()


def test_is_pending_sends_cancel_after_30s_but_stays_pending():
    """30초 초과 시 재주문 허용 대신 취소 주문을 발령하고 pending은 유지"""
    om = _make_om()
    code = "005930"
    om._pending.add(code)
    om._pending_sell_time[code] = datetime.now() - timedelta(seconds=31)

    result = om.is_pending(code)

    assert result is True  # 아직 재주문 허용 안 함
    assert code in om._pending_sell_cancel_at
    om._kiwoom._ocx.dynamicCall.assert_called_once()
    call_args = om._kiwoom._ocx.dynamicCall.call_args[0][1]
    assert call_args[3] == 3  # SendOrder 주문구분 3 = 취소


def test_is_pending_does_not_resend_cancel_twice():
    """취소를 이미 발령했으면 추가 경과해도 취소를 재발령하지 않음(grace 구간)"""
    om = _make_om()
    code = "005930"
    om._pending.add(code)
    om._pending_sell_time[code] = datetime.now() - timedelta(seconds=35)
    om._pending_sell_cancel_at[code] = datetime.now() - timedelta(seconds=5)

    result = om.is_pending(code)

    assert result is True
    om._kiwoom._ocx.dynamicCall.assert_not_called()


def test_is_pending_force_releases_after_cancel_grace():
    """취소 발령 후 grace 시간(30초)까지 추가 경과하면 강제로 pending 해제"""
    om = _make_om()
    code = "005930"
    om._pending.add(code)
    om._pending_sell_time[code] = datetime.now() - timedelta(seconds=65)
    om._pending_sell_cancel_at[code] = datetime.now() - timedelta(seconds=31)
    om._force_sell_issued.add(code)

    result = om.is_pending(code)

    assert result is False
    assert code not in om._pending
    assert code not in om._pending_sell_cancel_at
    assert code not in om._force_sell_issued
    assert om._pending_sell_retries[code] == 1


def test_orphan_800033_after_position_already_closed_does_not_crash():
    """포지션이 이미 사라진 뒤 도착한 800033 응답은 조용히 무시(예외 없음)"""
    om = _make_om()
    code = "005930"
    # 포지션 없음(이미 다른 경로로 청산됨)
    assert code not in om.positions

    om.on_order_msg(f"매도_{code}", "[800033] 모의투자 매도가능수량이 부족합니다.")

    assert code not in om.positions
    assert code not in om._pending


def test_orphan_sell_fill_after_position_already_closed_does_not_crash():
    """포지션이 이미 사라진 뒤 도착한 매도 체결 콜백도 예외 없이 무시"""
    om = _make_om()
    code = "005930"
    assert code not in om.positions

    om._handle_sell_fill(code, "삼성전자", filled_qty=10, filled_price=80_000, order_no="1")

    assert code not in om.positions
