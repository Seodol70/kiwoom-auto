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

    # [Option B 2026-05-27] watchdog: 마지막 성공 후 이 시간 초과 시 강제 재시도
    SILENCE_THRESHOLD_SEC = 90.0  # 60초 sync 주기 × 1.5

    def __init__(self, order_manager, trading_controller=None, parent=None) -> None:
        super().__init__(parent)
        self._om = order_manager
        self._tc = trading_controller
        self._balance_result: dict = {}
        self._timers: list[QTimer] = []
        # [Option B 2026-05-27] 마지막 성공 동기화 시각 (watchdog 용)
        self._last_sync_success: float = time.time()

    def _schedule_retry(self, delay_ms: int, fn) -> None:
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(fn)
        t.start(delay_ms)
        self._timers.append(t)

    @pyqtSlot()
    def sync(self) -> None:
        # [Option B 2026-05-27] watchdog: 침묵이 길어지면 WARNING 로그
        silence = time.time() - self._last_sync_success
        if silence > self.SILENCE_THRESHOLD_SEC:
            logger.warning(
                "[PortfolioWorker] 잔고 동기화 침묵 %.0fs (임계값 %.0fs) — 강제 재시도",
                silence, self.SILENCE_THRESHOLD_SEC
            )

        _kw = getattr(self._om, "_kiwoom", None)
        scan_busy = self._tc and getattr(self._tc, '_scan_in_progress', False)
        if (_kw and getattr(_kw, "_tr_busy", False)) or scan_busy:
            self._schedule_retry(3000, self.sync)
            return
        try:
            self._om._roll_daily_state_if_needed()
            balance = self._om._kiwoom.get_balance()
            if not balance:
                # [Option B 2026-05-27] 빈 응답 시 30초 후 재시도 (이전엔 return만 했음)
                # — 다음 60초 주기까지 기다리지 않고 빠르게 복구
                logger.warning("[PortfolioWorker] get_balance 빈 응답 — 30초 후 재시도")
                self._schedule_retry(30_000, self.sync)
                return
            self._balance_result = balance
            self._schedule_retry(350, self._sync_step2)
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step1] {e}")
            # 예외 시에도 watchdog가 작동하도록 30초 후 재시도
            self._schedule_retry(30_000, self.sync)

    @pyqtSlot()
    def _sync_step2(self) -> None:
        _kw = getattr(self._om, "_kiwoom", None)
        if _kw and getattr(_kw, "_tr_busy", False):
            self._schedule_retry(1000, self._sync_step2)
            return
        try:
            cash = self._om._sync_with_balance(self._balance_result)
            # [Option A 2026-05-27] update_portfolio_prices 호출 제거
            # — 청산 평가는 trading_controller.tick_exit_check (5초 타이머)가 담당
            # — 여기서는 잔고/포지션 동기화만 수행
            self.refresh_done.emit({"cash": cash, "positions": dict(self._om.positions)})
            # [Option B 2026-05-27] 성공 시각 기록 (watchdog 용)
            self._last_sync_success = time.time()
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step2] {e}")
            self._schedule_retry(30_000, self.sync)

    def stop(self) -> None:
        for t in self._timers: t.stop()
        self._timers.clear()
