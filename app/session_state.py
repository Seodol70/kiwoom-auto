"""Session State Persistence — 프로그램 재시작 후에도 당일 손익/리스크 상태 복원"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class SessionStateManager:
    """
    AppState와 RiskManager의 중요 상태를 JSON 파일에 저장/복원.

    저장 항목:
    - daily_realized_pnl: 당일 실현손익
    - is_loss_cut_locked: 손절 락 상태
    - is_profit_locked: 익절 락 상태
    - date: 상태 저장 날짜 (새 날이면 리셋)
    """

    STATE_FILE = Path.home() / ".kiwoom-auto" / "session_state.json"

    def __init__(self):
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        logger.info("[SessionState] 상태 파일: %s", self.STATE_FILE)

    def load(self) -> Dict[str, Any]:
        """저장된 상태 복원. 새 날이면 리셋."""
        if not self.STATE_FILE.exists():
            logger.info("[SessionState] 상태 파일 없음 — 초기화")
            return self._new_state()

        try:
            with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)

            # 날짜 확인: 다른 날이면 리셋
            saved_date = state.get("date", "")
            today = datetime.now().strftime("%Y-%m-%d")

            if saved_date != today:
                logger.info("[SessionState] 새 날 시작 — 당일 손익 리셋 (%s → %s)", saved_date, today)
                return self._new_state()

            logger.info("[SessionState] 상태 복원 (PnL=%.0f, LossCut=%s, ProfitLock=%s)",
                       state.get("daily_realized_pnl", 0),
                       state.get("is_loss_cut_locked", False),
                       state.get("is_profit_locked", False))
            return state

        except Exception as e:
            logger.warning("[SessionState] 복원 실패: %s — 초기화", e)
            return self._new_state()

    def save(self, daily_pnl: float, loss_cut_locked: bool, profit_locked: bool) -> None:
        """상태 저장 (즉시)"""
        state = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "daily_realized_pnl": daily_pnl,
            "is_loss_cut_locked": loss_cut_locked,
            "is_profit_locked": profit_locked,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            with open(self.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("[SessionState] 저장 실패: %s", e)

    @staticmethod
    def _new_state() -> Dict[str, Any]:
        """새로운 상태 초기화"""
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "daily_realized_pnl": 0.0,
            "is_loss_cut_locked": False,
            "is_profit_locked": False,
            "timestamp": datetime.now().isoformat(),
        }
