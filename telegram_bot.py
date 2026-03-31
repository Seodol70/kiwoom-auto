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
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """daemon thread 시작 (폴링 루프)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("[TelegramBot] 시작됨 (chat_id=%s)", self._chat_id)

    def stop(self) -> None:
        """폴링 루프 종료."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
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
        """Long Polling 루프 — 10초 타임아웃."""
        offset = 0
        while self._running:
            try:
                r = requests.get(
                    f"{BASE_URL}/bot{self._token}/getUpdates",
                    params={"timeout": 10, "offset": offset},
                    timeout=15,
                )
                if r.status_code != 200:
                    logger.warning("[TelegramBot] getUpdates 실패: %d", r.status_code)
                    time.sleep(5)
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
                time.sleep(5)
