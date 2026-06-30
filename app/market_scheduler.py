"""Market time scheduler — 장개시, 마감, 자정 이벤트 발행"""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

if TYPE_CHECKING:
    pass


class MarketScheduler(QObject):
    """
    시장 시간 기반 이벤트 스케줄러.

    - QTimer로 1분마다 현재 시각 체크
    - 시간대별로 pyqtSignal 발행
    - 상태 플래그로 중복 이벤트 방지
    """

    # ─── pyqtSignal (발행) ──────────────────────────────────────────────────

    market_opened = pyqtSignal()
    """08:00 장개시 신호"""

    phase1_cutoff = pyqtSignal()
    """10:30 Phase 1 강제청산 신호"""

    phase1_trail = pyqtSignal()
    """10:30~15:15 Phase 1 트레일 체크 신호 (매분)"""

    overnight_gap_check = pyqtSignal()
    """09:00 오버나잇 갭 체크 신호"""

    eod_daytime_check = pyqtSignal()
    """09:00~14:55 EOD 포지션 당일 수익률 체크 신호 (매분)"""

    eod_trend_check = pyqtSignal()
    """09:00~15:15 EOD 포지션 일봉 추세 체크 신호 (매분, Stage 3)"""

    overnight_timecut = pyqtSignal()
    """09:30 오버나잇 타임컷 신호"""

    overnight_auto_enabled = pyqtSignal()
    """14:40 야간보유 자동 ON 신호"""

    market_closing = pyqtSignal()
    """15:20 장마감 + 강제청산 신호"""

    feedback_triggered = pyqtSignal()
    """15:35 피드백 루프 신호"""

    day_reset = pyqtSignal()
    """자정 플래그 리셋 신호"""

    # ─── 상태 플래그 ──────────────────────────────────────────────────────

    def __init__(self, parent=None):
        super().__init__(parent)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer_timeout)
        self._timer.setInterval(60000)  # 1분마다

        # 중복 이벤트 방지 플래그
        self._opened_today = False
        self._closed_today = False
        self._feedback_done_today = False
        self._eod_gap_checked_today = False
        self._eod_auto_enabled_today = False

    # ─── 제어 메서드 ──────────────────────────────────────────────────────

    def start(self) -> None:
        """스케줄러 시작"""
        self._timer.start()

    def stop(self) -> None:
        """스케줄러 중지"""
        self._timer.stop()

    def reset_flags(self) -> None:
        """플래그 초기화 (테스트/수동 리셋 용도)"""
        self._opened_today = False
        self._closed_today = False
        self._feedback_done_today = False
        self._eod_gap_checked_today = False
        self._eod_auto_enabled_today = False

    # ─── 타이머 콜백 ──────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_timer_timeout(self) -> None:
        """1분마다 호출 — 현재 시각과 매칭해 신호 발행.

        [BUG FIX 2026-05-26] 기존 elif 사슬이 동일 분에 여러 신호가 함께 발행되어야 하는
        경우를 차단했음 (eod_trend_check는 14:55~15:14에만 발행, phase1_trail은 영영 발행 안됨).
        → 각 조건을 독립적인 if로 분리해 같은 분에 필요한 모든 신호가 발행되도록 수정.
        """
        if not getattr(self, "_timer", None) or not self._timer.isActive():
            # 비정상 호출 가드 — 스케줄러 중지 상태에서 콜백이 남아있는 경우
            pass
        now = datetime.now()
        now_time = now.time()
        is_weekday = now.weekday() < 5  # 월~금 (0~4)
        if not is_weekday:
            return  # 평일 외에는 모든 신호 발행 스킵

        # ─── 자정 플래그 리셋 (가장 먼저 처리) ─────────────────────────
        if now_time.hour == 0 and now_time.minute == 0:
            self._opened_today = False
            self._closed_today = False
            self._feedback_done_today = False
            self._eod_gap_checked_today = False
            self._eod_auto_enabled_today = False
            self.day_reset.emit()
            return  # 자정 처리 후 다른 신호 발행 없음

        # ─── 08:00 자동 시작 (1회만) ──────────────────────────────────
        if (time(8, 0) <= now_time < time(8, 1)
                and not self._opened_today):
            self._opened_today = True
            self.market_opened.emit()

        # ─── 09:00 EOD 갭 체크 신호 (1회만) ───────────────────────────
        if (time(9, 0) <= now_time < time(9, 30)
                and not self._eod_gap_checked_today):
            self._eod_gap_checked_today = True
            self.overnight_gap_check.emit()

        # ─── 09:30 EOD 타임컷 신호 (1분 윈도우, 매일 1회) ─────────────
        if time(9, 30) <= now_time < time(9, 31):
            self.overnight_timecut.emit()

        # ─── 10:30 Phase 1 강제청산 (1분 윈도우, 매일 1회) ────────────
        if time(10, 30) <= now_time < time(10, 31):
            self.phase1_cutoff.emit()

        # ─── 09:00~15:19 EOD 당일 수익률 체크 (매분) ──────────────────
        # [FIX 2026-06-29] EOD 진입 시간대가 14:50~15:20으로 변경됨에 따라 14:55에
        # 닫히던 상한을 15:20 강제청산 직전(15:19)까지 확장 — 14:50~14:55 진입분도
        # 15:20 전까지 일중 손절/익절 감시를 계속 받도록 함.
        if time(9, 0) <= now_time < time(15, 19):
            self.eod_daytime_check.emit()

        # ─── 09:00~15:19 EOD 추세 체크 (매분, Stage 3) ────────────────
        if time(9, 0) <= now_time < time(15, 19):
            self.eod_trend_check.emit()

        # ─── 10:31~15:15 Phase 1 트레일 체크 (매분) ───────────────────
        if time(10, 31) <= now_time < time(15, 15):
            self.phase1_trail.emit()

        # ─── 14:40 야간보유 자동 ON (1회만) ───────────────────────────
        if (time(14, 40) <= now_time < time(14, 41)
                and not self._eod_auto_enabled_today):
            self._eod_auto_enabled_today = True
            self.overnight_auto_enabled.emit()

        # ─── 15:15~15:20 자동 청산 (매분, MainWindow에서 처리) ────────
        # 별도 신호 없음 — MainWindow가 자체 타이머로 처리

        # ─── 15:20 강제청산 + 자동매매 OFF (1회만) ────────────────────
        if (time(15, 20) <= now_time < time(15, 21)
                and not self._closed_today):
            self._closed_today = True
            self.market_closing.emit()

        # ─── 15:35 피드백 루프 (1회만) ────────────────────────────────
        if (time(15, 35) <= now_time < time(15, 36)
                and not self._feedback_done_today):
            self._feedback_done_today = True
            self.feedback_triggered.emit()
