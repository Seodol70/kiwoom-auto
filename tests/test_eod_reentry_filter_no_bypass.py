"""
test_eod_reentry_filter_no_bypass.py — EOD 신호도 재진입 필터를 우회하지 않는지 검증

배경(2026-06-29): 사용자가 종가매매(EOD) 도입 시 "쿨다운/재진입대기/약한신호 등 기존
필터를 14:50~15:20 시점에 실시간으로 재평가해서 통과해야 진입(우회 없음)"을 요구했다.
strategy/jang_dong_min.py의 should_entry()는 신호 타입을 구분하지 않고 항상
_today_entry_dict(90분 재진입 쿨다운)을 조회하므로 이미 구조적으로 우회가 없으나,
향후 누군가 "EOD는 특별하니 쿨다운 면제" 같은 코드를 추가하는 회귀를 막기 위해
명문화한다.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from scanner.models import ScanSignal
from strategy.jang_dong_min import JangDongMinStrategy


def _make_strategy() -> JangDongMinStrategy:
    order_mgr = MagicMock()
    order_mgr.positions = {}
    order_mgr.available_cash = 10_000_000
    risk_mgr = MagicMock()
    scan_cfg = MagicMock()
    scan_cfg.max_positions = 10
    scan_cfg.today_entry_cooldown_minutes = 90.0
    return JangDongMinStrategy(order_mgr=order_mgr, risk_mgr=risk_mgr, scan_cfg=scan_cfg)


def test_eod_signal_blocked_by_today_reentry_cooldown():
    """EOD 신호도 일반 신호와 동일하게 90분 재진입 쿨다운에 걸리면 차단된다"""
    strat = _make_strategy()
    code = "005930"
    strat._today_entry_dict[code] = datetime.now() - timedelta(minutes=30)  # 30분 전 진입(쿨다운 90분 미달)

    sig = ScanSignal(code=code, name="삼성전자", signal_type="EOD", reason="[EOD] 종가매매 진입", price=70_000)
    ok, reason = strat.should_entry(sig, auto_trading=True)

    assert ok is False
    assert "재진입 대기" in reason


def test_eod_signal_blocked_by_loss_exit_same_day():
    """EOD 신호도 당일 손절 종목 재진입 차단에 동일하게 걸린다"""
    strat = _make_strategy()
    code = "005930"
    strat._loss_exit_dict[code] = datetime.now()  # 오늘 손절 이력

    sig = ScanSignal(code=code, name="삼성전자", signal_type="EOD", reason="[EOD] 종가매매 진입", price=70_000)
    ok, reason = strat.should_entry(sig, auto_trading=True)

    assert ok is False
    assert "손절 종목" in reason


def test_eod_signal_passes_when_no_filter_blocks():
    """차단 사유가 없으면 EOD 신호도 정상 통과한다"""
    strat = _make_strategy()
    sig = ScanSignal(code="005930", name="삼성전자", signal_type="EOD", reason="[EOD] 종가매매 진입", price=70_000)

    ok, reason = strat.should_entry(sig, auto_trading=True)

    assert ok is True
