# -*- coding: utf-8 -*-
"""
ConnectionWatchdog — 키움 API 연결 감시 및 자가 치유(Self-Healing) 모듈

동작 흐름:
  1. QTimer(check_timer)가 30초마다 is_connected() 확인
  2. 끊김 감지 → connection_lost 시그널 발행 → 재시도 모드 진입
  3. QTimer(retry_timer)가 retry_interval_sec마다 reconnect_silent() 재시도
     - 성공 → connection_recovered 발행 + SetRealReg 구독 복원
     - 최대 재시도 초과 → reconnect_failed 발행

설계 원칙:
  - 반드시 Qt 메인 스레드(QTimer)에서 실행: CommConnect()/QEventLoop 안전성 보장
  - 장 시간(08:55~15:35) vs 야간 재시도 간격 자동 조정
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)

# 장 운영 시간 (재시도 간격 조정에 사용)
_MARKET_OPEN  = dtime(8, 55)
_MARKET_CLOSE = dtime(15, 35)


def _is_market_hours() -> bool:
    """현재 시각이 장 운영 시간대인지 반환."""
    now = datetime.now().time()
    return _MARKET_OPEN <= now <= _MARKET_CLOSE


class ConnectionWatchdog(QObject):
    """
    키움 API 연결을 주기적으로 감시하고, 끊김 발생 시 자동 재연결을 시도합니다.

    사용 예)
        watchdog = ConnectionWatchdog(kiwoom, login_mgr, smart_scanner)
        watchdog.connection_lost.connect(win._on_connection_lost)
        watchdog.connection_recovered.connect(win._on_connection_recovered)
        watchdog.reconnect_failed.connect(win._on_reconnect_failed)
        watchdog.start()
    """

    connection_lost      = pyqtSignal()       # 연결 끊김 감지
    connection_recovered = pyqtSignal()       # 재연결 성공
    reconnect_failed     = pyqtSignal(str)    # 최대 재시도 초과 (사유 메시지)

    # ── 설정값 ─────────────────────────────────────────────────────────────
    CHECK_INTERVAL_MS: int      = 30_000     # 정상 상태 확인 주기 (30초)
    RETRY_INTERVAL_MS: int      = 30_000     # 장 시간 재시도 간격 (30초)
    RETRY_INTERVAL_NIGHT_MS: int = 600_000   # 야간 재시도 간격 (10분)
    MAX_RETRY: int               = 5          # 최대 재시도 횟수

    def __init__(
        self,
        kiwoom,
        login_mgr,
        smart_scanner=None,
        parent: QObject = None,
    ) -> None:
        super().__init__(parent)
        self._kiwoom        = kiwoom
        self._login_mgr     = login_mgr
        self._smart_scanner = smart_scanner

        self._retry_count = 0
        self._is_lost     = False   # 현재 끊김 상태 여부

        # 정상 감시 타이머
        self._check_timer = QTimer(self)
        self._check_timer.timeout.connect(self._on_check)

        # 재시도 타이머 (끊김 감지 시에만 활성)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._on_retry)

    # ── 공개 API ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """감시 루프 시작."""
        self._check_timer.start(self.CHECK_INTERVAL_MS)
        logger.info("[Watchdog] 연결 감시 시작 (주기=%ds, 최대재시도=%d회)",
                    self.CHECK_INTERVAL_MS // 1000, self.MAX_RETRY)

    def stop(self) -> None:
        """감시 루프 중단."""
        self._check_timer.stop()
        self._retry_timer.stop()
        logger.info("[Watchdog] 연결 감시 중단")

    # ── 내부 로직 ─────────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_check(self) -> None:
        """정상 감시 타이머 콜백 — 연결 상태 확인."""
        if self._is_lost:
            return  # 재시도 중에는 check 타이머 무시

        connected = self._kiwoom.is_connected()
        if not connected:
            logger.warning("[Watchdog] 연결 끊김 감지 — 자동 재연결 시작")
            self._is_lost = True
            self._retry_count = 0
            self._check_timer.stop()
            self.connection_lost.emit()
            self._schedule_retry()

    @pyqtSlot()
    def _on_retry(self) -> None:
        """재시도 타이머 콜백 — reconnect_silent() 호출."""
        self._retry_count += 1
        logger.info("[Watchdog] 재연결 시도 %d/%d ...", self._retry_count, self.MAX_RETRY)

        try:
            ok = self._login_mgr.reconnect_silent()
        except Exception as e:
            logger.error("[Watchdog] reconnect_silent() 예외: %s", e)
            ok = False

        if ok:
            logger.info("[Watchdog] 재연결 성공 (시도 %d회)", self._retry_count)
            self._is_lost = False
            self._retry_count = 0
            self.connection_recovered.emit()
            self._restore_subscriptions()
            # 정상 감시 타이머 재개
            self._check_timer.start(self.CHECK_INTERVAL_MS)
        elif self._retry_count >= self.MAX_RETRY:
            msg = f"최대 재시도 {self.MAX_RETRY}회 초과 — 수동 재시작 필요"
            logger.error("[Watchdog] %s", msg)
            self.reconnect_failed.emit(msg)
            # 감시 타이머는 멈춘 채로 유지 (더 이상 시도 안 함)
        else:
            # 아직 시도 횟수 남음 → 간격 조정 후 재예약
            interval = self.RETRY_INTERVAL_MS if _is_market_hours() else self.RETRY_INTERVAL_NIGHT_MS
            logger.info("[Watchdog] 재연결 실패 — %d초 후 재시도 (%d/%d)",
                        interval // 1000, self._retry_count, self.MAX_RETRY)
            self._schedule_retry(interval)

    def _schedule_retry(self, interval_ms: int = None) -> None:
        """재시도 타이머 예약."""
        if interval_ms is None:
            interval_ms = self.RETRY_INTERVAL_MS if _is_market_hours() else self.RETRY_INTERVAL_NIGHT_MS
        self._retry_timer.start(interval_ms)

    def _restore_subscriptions(self) -> None:
        """재연결 후 실시간 구독(SetRealReg)을 복원한다."""
        if self._smart_scanner is None:
            return
        try:
            watch_q = getattr(self._smart_scanner, "watch_q", None)
            if watch_q is None:
                return

            subscribed = list(getattr(watch_q, "subscribed", []))
            if not subscribed:
                # 구독 목록이 없으면 TopVolumeManager 상위 종목으로 대체
                top_mgr = getattr(self._smart_scanner, "top_mgr", None)
                if top_mgr:
                    subscribed = top_mgr.get_top_codes()

            if subscribed:
                watch_q.refresh(subscribed)
                logger.info("[Watchdog] 실시간 구독 복원 완료 — %d종목", len(subscribed))
            else:
                logger.info("[Watchdog] 복원할 구독 목록 없음 (다음 주기 스캔 시 자동 등록)")
        except Exception as e:
            logger.warning("[Watchdog] 구독 복원 실패: %s", e)
