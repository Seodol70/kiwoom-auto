"""
TelegramBot — 텔레그램 봇 모듈

역할:
  1. Long Polling으로 명령 수신 (/status, /start, /stop)
  2. 메인 스레드로 pyqtSignal emit
  3. 메시지 발송 (체결 알림, 상태 보고)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests
from PyQt5.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org"


class TelegramBot(QObject):
    """텔레그램 봇 — daemon thread 기반 polling"""

    cmd_start = pyqtSignal()    # /start 명령
    cmd_stop = pyqtSignal()     # /stop 명령
    cmd_status = pyqtSignal()   # /status 명령

    def __init__(self, token: str, chat_id: str, parent=None) -> None:
        super().__init__(parent)
        self._token = token
        self._chat_id = chat_id
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """daemon thread 시작 (폴링 루프)."""
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("[TelegramBot] 시작됨 (chat_id=%s)", self._chat_id)

    def stop(self) -> None:
        """폴링 루프 종료 — stop_event로 빠른 중단 신호 전달."""
        self._running = False
        self._stop_event.set()      # 폴링 루프가 sleep 중이면 즉시 깨움
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)  # getUpdates timeout(5s) + 여유 3s
        logger.info("[TelegramBot] 중지됨")

    def send(self, text: str) -> None:
        """메시지 발송 (thread-safe)."""
        try:
            requests.post(
                f"{BASE_URL}/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text},
                timeout=5,
            )
        except Exception as e:
            logger.warning("[TelegramBot] 메시지 발송 실패: %s", e)

    def _poll_loop(self) -> None:
        """Long Polling 루프 — 5초 타임아웃 (stop 시 빠른 정리)."""
        offset = 0
        conflict_count = 0          # 409 연속 횟수

        while self._running and not self._stop_event.is_set():
            try:
                r = requests.get(
                    f"{BASE_URL}/bot{self._token}/getUpdates",
                    params={"timeout": 5, "offset": offset},
                    timeout=7,      # getUpdates(5s) + 네트워크 여유(2s)
                )

                if r.status_code == 409:
                    conflict_count += 1
                    wait = min(5 * (2 ** (conflict_count - 1)), 60)  # 5→10→20→40→60s 상한
                    logger.warning(
                        "[TelegramBot] getUpdates 실패: 409 (중복 인스턴스, %d회째) — %ds 대기 후 재시도",
                        conflict_count, wait,
                    )
                    if conflict_count >= 4:
                        logger.error("[TelegramBot] 409 4회 연속 — 봇 중지 (다른 인스턴스 확인 필요)")
                        self._running = False
                        break
                    self._stop_event.wait(wait)  # sleep 대신 event wait → stop() 호출 시 즉시 탈출
                    continue

                # 정상 응답 → 연속 충돌 카운터 리셋
                conflict_count = 0

                if r.status_code != 200:
                    logger.warning("[TelegramBot] getUpdates 실패: %d", r.status_code)
                    self._stop_event.wait(5)
                    continue

                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1

                    msg = upd.get("message", {})
                    text = msg.get("text", "").strip()

                    if text == "/start":
                        logger.info("[TelegramBot] 명령 수신: /start")
                        self.cmd_start.emit()
                    elif text == "/stop":
                        logger.info("[TelegramBot] 명령 수신: /stop")
                        self.cmd_stop.emit()
                    elif text == "/status":
                        logger.info("[TelegramBot] 명령 수신: /status")
                        self.cmd_status.emit()

            except requests.exceptions.ReadTimeout:
                # timeout 정상 → 계속 폴링
                pass
            except Exception as e:
                logger.warning("[TelegramBot] 폴링 오류: %s", e)
                self._stop_event.wait(5)
