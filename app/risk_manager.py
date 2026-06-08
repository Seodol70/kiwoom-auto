"""Risk manager — 일일 손익 한도 체크"""

from __future__ import annotations

import logging
import time
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
        self._manual_unlock_active = False

        # 로그인 후 손절·익절 보류 기간
        self._sl_tp_warmup_end: float = 0.0

        # 상태 복원 확인 로그
        if self._state:
            logger.info("[RiskManager] AppState 연동 완료 (ProfitLock=%s, LossCut=%s)",
                        self._state.profit_locked, self._state.loss_cut_locked)

        # 체결 신호 연결: 연속 손실 추적 + P&L 한도 체크
        # check()는 매도 체결 시마다 호출 → daily_realized_pnl이 갱신된 직후 동작
        if hasattr(self._order_mgr, 'order_filled'):
            self._order_mgr.order_filled.connect(self._on_order_filled)
            self._order_mgr.order_filled.connect(lambda *_: self.check())

    def update_config(self, scan_cfg: SmartScannerConfig) -> None:
        """설정 객체 참조를 갱신한다."""
        self._scan_cfg = scan_cfg

    def start_warmup(self, duration_sec: float) -> None:
        """로그인 후 손절·익절 보류 기간을 시작한다 (잔고·시세 안정화용)."""
        self._sl_tp_warmup_end = time.monotonic() + max(0.0, duration_sec)
        if duration_sec > 0:
            logger.info("[RiskManager] SL/TP 손절익절 보류 시작 (%d초)", int(duration_sec))

    @property
    def is_sl_tp_warmup_active(self) -> bool:
        """손절·익절 보류 기간 중인지 확인."""
        return time.monotonic() < self._sl_tp_warmup_end

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

        # 손절 체크 (daily_loss_cut_won은 음수로 저장됨, 예: -50_000)
        if (daily_pnl <= self._scan_cfg.daily_loss_cut_won
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

        # 분할 체결 시 포지션이 아직 남아 있으면 카운트하지 않는다.
        # qty > 0 이면 아직 잔여 수량이 있는 분할 체결이므로 건너뜀.
        remaining_qty = payload.get("remaining_qty", -1)
        if remaining_qty > 0:
            return

        # 실현 손익 확인
        realized = payload.get("realized_pnl", 0)

        # realized 가 음수면 손절로 간주
        if realized < 0:
            self._consecutive_losses += 1
            limit = int(getattr(self._scan_cfg, "consecutive_loss_limit", 3))
            if self._consecutive_losses >= limit:
                # 냉각기 발동 (동적 시간: 1회 5분, 2회 10분, 3회 15분... 최대 30분)
                from datetime import datetime, timedelta
                from logging_config import order_log

                # [2026-05-20] 냉각기 조정: 최대 20분으로 축소 (거래 기회 손실 30% 감소)
                # 선형 증가: (손절 횟수 - 임계값 + 1) * 5분, 최대 20분
                cooloff_steps = min(self._consecutive_losses - limit + 1, 4)
                minutes = cooloff_steps * 5  # 5, 10, 15, 20

                self._cooling_off_until = datetime.now() + timedelta(minutes=minutes)
                if self._state: self._state.profit_locked = True
                order_log.warning("[리스크] %d회 연속 손절 발생 -> %d분간 매수 차단 (냉각기)", self._consecutive_losses, minutes)
        else:
            # 익절 시 연속 손실 카운트 초기화
            if realized > 0:
                self._consecutive_losses = 0
