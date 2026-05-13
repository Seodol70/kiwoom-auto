from __future__ import annotations
import logging
import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from telegram_bot import TelegramBot

logger = logging.getLogger(__name__)

class NotificationManager:
    """
    시스템 알림 통합 관리자.
    로그 출력, 텔레그램 전송, 사운드 알림 등을 한곳에서 제어한다.
    """

    def __init__(self, telegram_bot: Optional[TelegramBot] = None) -> None:
        self.telegram_bot = telegram_bot
        self._sound_enabled = True

    def set_telegram_bot(self, bot: TelegramBot) -> None:
        self.telegram_bot = bot

    def info(self, title: str, message: str, telegram: bool = False, sound: bool = False) -> None:
        self.notify("INFO", title, message, telegram, sound)

    def warning(self, title: str, message: str, telegram: bool = True, sound: bool = False) -> None:
        self.notify("WARNING", title, message, telegram, sound)

    def critical(self, title: str, message: str, telegram: bool = True, sound: bool = False) -> None:
        self.notify("CRITICAL", title, message, telegram, sound)

    def notify(self, level: str, title: str, message: str, 
               telegram: bool = False, sound: bool = False) -> None:
        """
        통합 알림 실행.
        """
        # [NEW] 한글 깨짐 방어막 (latin-1 -> cp949)
        def fix_text(t: str) -> str:
            if not t: return t
            try:
                # 깨진 한글 패턴 감지 (ASCII 범위를 벗어나는 문자열 중 cp949로 해석 가능한 경우)
                if any(ord(c) > 255 for c in t):
                    # 이미 유니코드일 수 있으나, 깨진 상태일 수 있으므로 latin-1로 밀어내고 다시 decode
                    return t.encode('latin-1').decode('cp949')
            except Exception:
                pass
            return t

        title   = fix_text(title)
        message = fix_text(message)
        full_msg = f"[{level}] {title} — {message}"
        
        # 1. 로깅
        logger.info(full_msg)

        # 2. 텔레그램 전송
        if telegram and self.telegram_bot:
            self.telegram_bot.send(full_msg)

