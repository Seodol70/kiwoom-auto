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

    def warning(self, title: str, message: str, telegram: bool = True, sound: bool = True) -> None:
        self.notify("WARNING", title, message, telegram, sound)

    def critical(self, title: str, message: str, telegram: bool = True, sound: bool = True) -> None:
        self.notify("CRITICAL", title, message, telegram, sound)

    def notify(self, level: str, title: str, message: str, 
               telegram: bool = False, sound: bool = False) -> None:
        """
        통합 알림 실행.
        """
        full_msg = f"[{level}] {title} — {message}"
        
        # 1. 로깅 (이미 각 컴포넌트에서 하고 있을 수 있으므로 알림 전용 로거 사용)
        logger.info(full_msg)

        # 2. 텔레그램 전송
        if telegram and self.telegram_bot:
            self.telegram_bot.send(full_msg)

        # 3. 사운드 알림 (Windows 전용)
        if sound and self._sound_enabled:
            self._play_system_sound(level)

    def _play_system_sound(self, level: str) -> None:
        try:
            import winsound
            if level == "CRITICAL":
                winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
            elif level == "WARNING":
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
            else:
                winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except ImportError:
            pass # Windows 가 아닐 경우 무시
        except Exception as e:
            logger.debug("[Notification] 사운드 재생 실패: %s", e)
