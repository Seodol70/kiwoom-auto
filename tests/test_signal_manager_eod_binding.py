"""
test_signal_manager_eod_binding.py — SignalManager의 EOD 신호 연결 회귀 테스트

배경(2026-06-29): ui/signal_manager.py에서 MarketScheduler의 EOD 전용 신호 4개
(overnight_gap_check, eod_daytime_check, eod_trend_check, overnight_timecut)가
모두 tc.tick_exit_check에만 연결되어 있어 EOD 전용 청산 메서드가 한 번도
호출되지 않는 연결 결함이 있었다. 이를 각자의 전용 메서드로 재연결했는데,
앞으로 누군가 다시 모두 tick_exit_check로 되돌리는 회귀를 방지하기 위해
실제 emit() 후 올바른 메서드가 호출되는지 검증한다.
"""

from unittest.mock import MagicMock

from PyQt5.QtWidgets import QApplication

from app.market_scheduler import MarketScheduler
from ui.signal_manager import SignalManager


def _make_win():
    QApplication.instance() or QApplication([])
    win = MagicMock()
    win.market_scheduler = MarketScheduler(parent=None)
    win.trading_controller = MagicMock()
    win.order_mgr = MagicMock()
    win.login_mgr = MagicMock()
    win.state = MagicMock()
    win._port_worker = MagicMock()
    return win


def test_overnight_gap_check_routes_to_check_overnight_gap():
    win = _make_win()
    sm = SignalManager(win)
    sm._bind_background_workers()

    win.market_scheduler.overnight_gap_check.emit()

    win.trading_controller.check_overnight_gap.assert_called_once()
    win.trading_controller.tick_exit_check.assert_not_called()


def test_eod_daytime_check_routes_to_check_eod_daytime_targets():
    win = _make_win()
    sm = SignalManager(win)
    sm._bind_background_workers()

    win.market_scheduler.eod_daytime_check.emit()

    win.trading_controller.check_eod_daytime_targets.assert_called_once()
    win.trading_controller.tick_exit_check.assert_not_called()


def test_eod_trend_check_routes_to_check_overnight_trend_break():
    win = _make_win()
    sm = SignalManager(win)
    sm._bind_background_workers()

    win.market_scheduler.eod_trend_check.emit()

    win.trading_controller.check_overnight_trend_break.assert_called_once()
    win.trading_controller.tick_exit_check.assert_not_called()


def test_overnight_timecut_routes_to_check_overnight_timecut():
    win = _make_win()
    sm = SignalManager(win)
    sm._bind_background_workers()

    win.market_scheduler.overnight_timecut.emit()

    win.trading_controller.check_overnight_timecut.assert_called_once()
    win.trading_controller.tick_exit_check.assert_not_called()


def test_phase1_signals_still_route_to_tick_exit_check():
    """phase1_cutoff/phase1_trail은 EOD와 무관하므로 그대로 tick_exit_check 유지"""
    win = _make_win()
    sm = SignalManager(win)
    sm._bind_background_workers()

    win.market_scheduler.phase1_cutoff.emit()
    win.market_scheduler.phase1_trail.emit()

    assert win.trading_controller.tick_exit_check.call_count == 2
