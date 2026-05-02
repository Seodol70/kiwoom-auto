"""Risk manager — 일일 손익 한도 체크"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

if TYPE_CHECKING:
    from order.order_manager import OrderManager
    from scanner.smart_scanner import SmartScannerConfig


class RiskManager(QObject):
    """
    일일 손익 한도 관리.

    - 수익 목표 달성 시 신규 매수 차단
    - 손절 한도 도달 시 전량 청산 신호 발행
    """

    # ─── pyqtSignal ────────────────────────────────────────────────────

    daily_profit_locked = pyqtSignal()
    """수익 목표 달성 → 신규 매수 차단"""

    daily_loss_cut = pyqtSignal()
    """손절 한도 도달 → 전량 청산"""

    def __init__(
        self,
        order_mgr: OrderManager,
        scan_cfg: SmartScannerConfig,
        parent=None,
    ):
        super().__init__(parent)
        self._order_mgr = order_mgr
        self._scan_cfg = scan_cfg

        # 상태 플래그
        self._new_entry_locked = False
        self._daily_loss_cut_done = False
        self._manual_unlock_active = False

    # ─── 공개 인터페이스 ──────────────────────────────────────────────

    @property
    def is_new_entry_locked(self) -> bool:
        """신규 매수 차단 여부"""
        return self._new_entry_locked

    @property
    def is_daily_loss_cut_done(self) -> bool:
        """손절 한도 도달 여부"""
        return self._daily_loss_cut_done

    def check(self) -> None:
        """매분 호출 — 손익 한도 체크"""
        # 수익 락 체크
        daily_pnl = self._order_mgr.daily_realized_pnl
        if (daily_pnl >= self._scan_cfg.daily_profit_lock_won
                and not self._new_entry_locked
                and not self._manual_unlock_active):
            self._new_entry_locked = True
            self.daily_profit_locked.emit()

        # 손절 체크
        if (daily_pnl <= -self._scan_cfg.daily_loss_cut_won
                and not self._daily_loss_cut_done):
            self._daily_loss_cut_done = True
            self.daily_loss_cut.emit()

    def reset(self) -> None:
        """자정 리셋"""
        self._new_entry_locked = False
        self._daily_loss_cut_done = False
        self._manual_unlock_active = False

    def unlock_entry_manual(self) -> None:
        """수동으로 신규 매수 락 해제 (사용자 버튼)"""
        self._manual_unlock_active = True
        self._new_entry_locked = False

    def lock_entry_manual(self) -> None:
        """수동으로 신규 매수 락 활성화"""
        self._manual_unlock_active = False
        self._new_entry_locked = True
