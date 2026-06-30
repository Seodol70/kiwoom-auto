"""
test_order_manager_partial_sell_pending.py — 매도 부분체결 중 pending 유지 + RC4007 처리 검증

배경(2026-06-25): 남화산업 사례 — 51주 매수가 분할체결(1주+49주)되는 동안 슬리피지초과
즉시매도가 1주만 매도 주문을 냈는데, 후속 매수체결 49주가 그대로 포지션에 합산됐다.
이후 49주 매도 주문이 부분체결(4주)되자 _finalize_fill이 즉시 _pending을 해제해,
같은 5초 틱 사이 check_and_exit_all()이 또 매도를 발사 → 800033/고아응답이 반복됐다.

수정: 매도 주문이 부분체결(remaining_qty > 0)인 동안은 pending을 유지해
같은 주문의 나머지 체결을 기다리게 한다.

또한 헝셩그룹 사례(모의투자 매매제한 종목 RC4007) — 매수 주문 자체가 거부됐는데
필터가 사전에 걸러내지 못해 동일 종목 재신호 시 또 거부당할 소지가 있었다.
수정: RC4007 응답 수신 시 해당 종목을 당일 재진입 차단 목록에 등록한다.

배경(2026-06-26): 키스트론 사례 — 트레일스탑 조건이 만족된 뒤 force_exit()가 호출되지만
_force_sell_issued 가드보다 앞서 있는 로그 출력이 먼저 실행돼, 매도 체결을 기다리는
4초 동안 동일한 "즉시 매도" 로그가 24회 반복 찍혔다(에코프로/차백신연구소 무한루프와
같은 계열의 증상, 다만 실제 중복 주문은 force_exit 내부 가드로 막혔음). 본절가스탑도
동일 구조였다. 수정: 두 경로 모두 _force_sell_issued에 이미 등록돼 있으면 로그를
찍지 않는다.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from order.order_manager import OrderManager, Position
from scanner.config import SmartScannerConfig

# [FIX] force_exit()는 datetime.now().time()으로 장 운영 시간(08:55~15:35) 외 매도를
# 차단한다. 테스트를 장 마감 후(예: 15:37) 실행하면 트레일스탑이 force_exit를 호출해도
# 매도 자체가 거부되어 _force_sell_issued가 채워지지 않는 flaky 실패가 난다. 장중
# 고정 시각으로 patch해 실행 시각과 무관하게 만든다.
_FAKE_INTRADAY_NOW = datetime(2026, 1, 5, 10, 0, 0)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FAKE_INTRADAY_NOW


def _freeze_intraday():
    return patch("order.order_manager.datetime", _FrozenDateTime)


def _make_om():
    kiwoom = MagicMock()
    kiwoom._ocx = MagicMock()
    kiwoom._ocx.dynamicCall = MagicMock(return_value=0)
    om = OrderManager(kiwoom, account="1234567890")
    return om


def test_partial_sell_fill_keeps_pending_until_fully_closed():
    """매도 주문이 부분체결되는 동안은 is_pending이 True를 유지해야 한다"""
    om = _make_om()
    code = "111710"
    om.positions[code] = Position(
        code=code, name="남화산업", qty=49, avg_price=5340, current_price=5340,
    )
    om._pending.add(code)
    om._pending_sell_time.pop(code, None)

    # 49주 중 4주만 체결 (부분체결)
    om._handle_sell_fill(code, "남화산업", filled_qty=4, filled_price=5170, order_no="1")

    assert code in om.positions
    assert om.positions[code].qty == 45
    assert om.is_pending(code) is True  # 잔량이 남아있으니 같은 주문 체결을 더 기다려야 함


def test_full_sell_fill_releases_pending():
    """매도 주문이 전량 체결되면 pending이 정상적으로 풀려야 한다"""
    om = _make_om()
    code = "111710"
    om.positions[code] = Position(
        code=code, name="남화산업", qty=45, avg_price=5340, current_price=5340,
    )
    om._pending.add(code)

    om._handle_sell_fill(code, "남화산업", filled_qty=45, filled_price=5170, order_no="1")

    assert code not in om.positions
    assert om.is_pending(code) is False


def test_rc4007_blocks_buy_rejects_and_marks_no_reentry():
    """RC4007(모의투자 매매제한) 매수 거부 시 pending 정리 + 당일 재진입 차단 등록"""
    om = _make_om()
    code = "900270"
    om._pending.add(code)
    om._app_pending_buys[code] = 222

    om.on_order_msg(f"매수_{code}", "[RC4007] 모의투자 매매제한 종목입니다.")

    assert code not in om._pending
    assert code not in om._app_pending_buys
    assert code in om._no_reentry_today


def test_rc4007_does_not_affect_sell_messages():
    """RC4007 처리가 매도 메시지 경로(800033 등)를 건드리지 않아야 한다"""
    om = _make_om()
    code = "005930"
    om.positions[code] = Position(code=code, name="삼성전자", qty=10, avg_price=80_000, current_price=80_000)
    om._pending.add(code)

    om.on_order_msg(f"매도_{code}", "[800033] 모의투자 매도가능수량이 부족합니다.")

    assert code not in om.positions
    assert code not in om._no_reentry_today


def test_trail_stop_does_not_relog_while_force_exit_already_issued(caplog):
    """force_exit 발령 중이면 트레일스탑 로그를 반복 찍지 않는다 (키스트론 24회 폭증 방지)"""
    import logging

    om = _make_om()
    om._scan_cfg = SmartScannerConfig()
    code = "475430"
    om.positions[code] = Position(
        code=code, name="키스트론", qty=5, avg_price=6800, current_price=7250,
        peak_price=7250, trend_level=1,
    )
    # force_exit가 이미 발령되어 매도 체결을 기다리는 상태를 시뮬레이션
    om._force_sell_issued.add(code)

    with _freeze_intraday(), caplog.at_level(logging.INFO):
        for _ in range(5):
            om._on_price_updated(code, 7030, -3.2, 1)

    trail_logs = [r for r in caplog.records if "트레일스탑" in r.message]
    assert len(trail_logs) == 0


def test_trail_stop_logs_once_before_force_exit_issued():
    """force_exit 발령 전에는 트레일스탑 로그가 정상적으로 찍혀야 한다"""
    om = _make_om()
    om._scan_cfg = SmartScannerConfig()
    code = "475430"
    om.positions[code] = Position(
        code=code, name="키스트론", qty=5, avg_price=6800, current_price=7250,
        peak_price=7250, trend_level=1,
    )

    with _freeze_intraday():
        om._on_price_updated(code, 7030, -3.2, 1)

    assert code in om._force_sell_issued
