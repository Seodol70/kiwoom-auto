from __future__ import annotations
import time
import logging
from datetime import datetime
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

logger = logging.getLogger("scanner.worker")

# ScannerWorker has been removed and consolidated into SmartScanner.






class PortfolioWorker(QObject):
    refresh_done = pyqtSignal(dict)
    log_message  = pyqtSignal(str)

    def __init__(self, order_manager, trading_controller=None, parent=None) -> None:
        super().__init__(parent)
        self._om = order_manager
        self._tc = trading_controller
        self._balance_result: dict = {}
        self._timers: list[QTimer] = []

    def _schedule_retry(self, delay_ms: int, fn) -> None:
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(fn)
        t.start(delay_ms)
        self._timers.append(t)

    @pyqtSlot()
    def sync(self) -> None:
        _kw = getattr(self._om, "_kiwoom", None)
        scan_busy = self._tc and getattr(self._tc, '_scan_in_progress', False)
        if (_kw and getattr(_kw, "_tr_busy", False)) or scan_busy:
            self._schedule_retry(3000, self.sync)
            return
        try:
            self._om._roll_daily_state_if_needed()
            balance = self._om._kiwoom.get_balance()
            if not balance: return
            self._balance_result = balance
            self._schedule_retry(350, self._sync_step2)
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step1] {e}")

    @pyqtSlot()
    def _sync_step2(self) -> None:
        _kw = getattr(self._om, "_kiwoom", None)
        if _kw and getattr(_kw, "_tr_busy", False):
            self._schedule_retry(1000, self._sync_step2)
            return
        try:
            cash = self._om._sync_with_balance(self._balance_result)
            if self._tc: self._tc.update_portfolio_prices()
            self.refresh_done.emit({"cash": cash, "positions": dict(self._om.positions)})
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step2] {e}")

    def stop(self) -> None:
        for t in self._timers: t.stop()
        self._timers.clear()
