"""
test_smartscanner_prefilter_scheduling.py — Pre-Filter 예약이 QTimer(메인 스레드)로
실행되는지 검증

배경(2026-06-29): SmartScanner.start()가 장 시작 전(09:00 이전)에 호출되면
threading.Timer(secs, self._run_pre_filter)로 09:00:00 정각 실행을 예약했다.
threading.Timer는 별도(비-Qt) 스레드에서 콜백을 실행하므로, 그 안에서
_run_pre_filter -> fetch_opt10030_top_volume -> CommRqData(dynamicCall)까지
QAxWidget(메인 스레드 전용 GUI 객체)을 다른 스레드에서 호출하게 됐다. 09:00:00
정각 키움 서버 응답 지연과 겹쳐 OCX 콜백이 블로킹되자 메인 스레드의 QTimer
타임아웃(2초)조차 발동하지 못하고 대시보드 전체가 80분간 멈췄다(09:00~10:19).

수정: threading.Timer 대신 QTimer.singleShot을 사용해 Pre-Filter 예약 콜백이
항상 메인 스레드(Qt 이벤트 루프)에서 실행되도록 한다.
"""

import threading
from datetime import datetime, time as dtime
from unittest.mock import MagicMock, patch

import pytest

from scanner.smart_scanner import SmartScanner, SmartScannerConfig


class MockOcx:
    class _FakeSignal:
        def connect(self, fn):
            pass

    OnReceiveRealData = _FakeSignal()

    def dynamicCall(self, method, args):
        return ""


class MockKiwoom:
    def __init__(self):
        self._ocx = MockOcx()

    def get_tr_ban_status(self):
        return {}


def _make_scanner() -> SmartScanner:
    kiwoom = MockKiwoom()
    cfg = SmartScannerConfig()
    with patch("scanner.smart_scanner.PriorityWatchQueue") as mock_wq:
        mock_wq.return_value = MagicMock()
        scanner = SmartScanner(kiwoom, cfg)
    return scanner


def test_start_before_market_open_does_not_use_threading_timer():
    """장 시작 전에 start()를 호출하면 threading.Timer가 아닌 QTimer.singleShot으로
    Pre-Filter를 예약해야 한다 (별도 스레드에서 OCX를 두드리지 않도록)"""
    scanner = _make_scanner()
    scanner.store.load_1min_cache = MagicMock()
    scanner._fetch_all_codes = MagicMock(return_value=[])
    scanner._subscribe_market_indices = MagicMock()
    scanner._run_pre_filter = MagicMock()

    fake_now = datetime.combine(datetime.now().date(), dtime(8, 35, 0))

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch("scanner.smart_scanner.datetime", _FrozenDateTime), \
         patch("scanner.smart_scanner.threading.Timer") as mock_timer:
        scanner.start()

    # threading.Timer가 전혀 생성되지 않아야 한다 — Pre-Filter 예약은 QTimer로 위임됨
    mock_timer.assert_not_called()
    # _run_pre_filter는 QTimer.singleShot에 등록만 됐을 뿐 즉시 호출되지는 않음
    scanner._run_pre_filter.assert_not_called()


def test_start_during_market_hours_runs_pre_filter_immediately():
    """장중에 start()가 호출되면 Pre-Filter를 즉시 동기 실행한다(예약 불필요)"""
    scanner = _make_scanner()
    scanner.store.load_1min_cache = MagicMock()
    scanner._fetch_all_codes = MagicMock(return_value=[])
    scanner._subscribe_market_indices = MagicMock()
    scanner._run_pre_filter = MagicMock()

    fake_now = datetime.combine(datetime.now().date(), dtime(9, 30, 0))

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    with patch("scanner.smart_scanner.datetime", _FrozenDateTime), \
         patch("scanner.smart_scanner.threading.Timer") as mock_timer:
        scanner.start()

    mock_timer.assert_not_called()
    scanner._run_pre_filter.assert_called_once()
