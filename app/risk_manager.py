"""Risk manager — 일일 손익 한도 체크"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)

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
        app_state=None,
    ):
        super().__init__(parent)
        self._order_mgr = order_mgr
        self._scan_cfg = scan_cfg
        self._state = app_state # AppState 주입

        # [NEW] 연속 손실 및 냉각기 상태
        self._consecutive_losses = 0
        self._cooling_off_until = None

        # 상태 복원 확인 로그
        if self._state:
            logger.info("[RiskManager] AppState 연동 완료 (ProfitLock=%s, LossCut=%s)",
                        self._state.profit_locked, self._state.loss_cut_locked)

        # 체결 신호 연결 (연속 손실 추적용)
        if hasattr(self._order_mgr, 'order_filled'):
            self._order_mgr.order_filled.connect(self._on_order_filled)

    def update_config(self, scan_cfg: SmartScannerConfig) -> None:
        """설정 객체 참조를 갱신한다."""
        self._scan_cfg = scan_cfg

    # ─── 공개 인터페이스 ──────────────────────────────────────────────

    @property
    def is_new_entry_locked(self) -> bool:
        """신규 매수 차단 여부"""
        return self._state.profit_locked if self._state else False

    @property
    def is_daily_loss_cut_done(self) -> bool:
        """손절 한도 도달 여부"""
        return self._state.loss_cut_locked if self._state else False

    def check(self) -> None:
        """매분 호출 — 손익 한도 체크"""
        if not self._state: return

        # 수익 락 체크
        daily_pnl = self._order_mgr.daily_realized_pnl
        if (daily_pnl >= self._scan_cfg.daily_profit_lock_won
                and not self._state.profit_locked
                and not self._manual_unlock_active):
            self._state.profit_locked = True
            self.daily_profit_locked.emit()

        # 손절 체크
        if (daily_pnl <= -self._scan_cfg.daily_loss_cut_won
                and not self._state.loss_cut_locked):
            self._state.loss_cut_locked = True
            self.daily_loss_cut.emit()

        # [NEW] 전체 포트폴리오 미실현 손익 체크
        total_cost = sum(p.avg_price * p.qty for p in self._order_mgr.positions.values())
        if total_cost > 0:
            total_unrealized_pnl = sum(p.pnl for p in self._order_mgr.positions.values())
            total_pnl_pct = (total_unrealized_pnl / total_cost) * 100.0
            
            max_loss_cut = float(getattr(self._scan_cfg, "max_portfolio_unrealized_loss_pct", 5.0))
            if total_pnl_pct <= -max_loss_cut and not self.is_daily_loss_cut_done:
                if self._state: self._state.loss_cut_locked = True
                self.daily_loss_cut.emit()
                from logging_config import order_log
                order_log.warning("[리스크] 포트폴리오 합산 손절 발동: %.2f%% (한도 -%.1f%%)", total_pnl_pct, max_loss_cut)

        # [NEW] 냉각기 상태 업데이트
        from datetime import datetime
        if self._cooling_off_until and datetime.now() >= self._cooling_off_until:
            self._cooling_off_until = None
            if self._state: self._state.profit_locked = False
            from logging_config import order_log
            order_log.info("[리스크] 냉각기 종료 — 신규 매수 차단 해제")

    def reset(self) -> None:
        """자정 리셋"""
        if self._state:
            self._state.profit_locked = False
            self._state.loss_cut_locked = False
        self._manual_unlock_active = False

    def unlock_entry_manual(self) -> None:
        """수동으로 신규 매수 락 해제 (사용자 버튼)"""
        self._manual_unlock_active = True
        if self._state: self._state.profit_locked = False

    def lock_entry_manual(self) -> None:
        """수동으로 신규 매수 락 활성화"""
        self._manual_unlock_active = False
        if self._state: self._state.profit_locked = True

    # ─── 내부 핸들러 ──────────────────────────────────────────────────

    @pyqtSlot(dict)
    def _on_order_filled(self, payload: dict) -> None:
        """체결 시 호출되어 연속 손실을 추적한다."""
        side = payload.get("side", "")
        if "매도" not in side:
            return

        # 실현 손익 확인
        realized = payload.get("realized_pnl", 0)
        # realized 가 없으면 (일부 체결 등) 계산 시도 (payload에 없으면 0으로 간주하거나 pass)
        # OrderManager.order_filled emit 시 realized_pnl이 포함되도록 수정 필요할 수 있음
        
        # realized 가 음수면 손절로 간주
        if realized < 0:
            self._consecutive_losses += 1
            limit = int(getattr(self._scan_cfg, "consecutive_loss_limit", 3))
            if self._consecutive_losses >= limit:
                # 냉각기 발동
                from datetime import datetime, timedelta
                from logging_config import order_log
                minutes = int(getattr(self._scan_cfg, "cooling_off_minutes", 30))
                self._cooling_off_until = datetime.now() + timedelta(minutes=minutes)
                if self._state: self._state.profit_locked = True
                order_log.warning("[리스크] %d회 연속 손절 발생 -> %d분간 매수 차단 (냉각기)", self._consecutive_losses, minutes)
        else:
            # 익절 시 연속 손실 카운트 초기화
            if realized > 0:
                self._consecutive_losses = 0
