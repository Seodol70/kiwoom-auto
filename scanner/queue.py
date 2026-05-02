from __future__ import annotations
import time
import heapq
import logging
import threading
from collections import deque as _Deque

logger = logging.getLogger(__name__)

class TRRequestQueue:
    """
    키움 TR 호출 간격을 중앙에서 관리한다.

    키움 API 제한: 연속 TR 호출 간 최소 0.2초 권장.
    여기서 0.25초로 설정해 여유를 두고, 모든 TR 호출을
    call() 메서드를 통해 실행하면 자동으로 간격이 보장된다.

    기존 time.sleep(tr_delay) 분산 호출을 이 클래스로 대체한다.
    """
    _MIN_INTERVAL = 0.25  # 초

    def __init__(self) -> None:
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def call(self, fn: Callable, *args):
        """fn(*args)를 최소 간격 보장 후 실행하고 결과를 반환한다.

        _tr_busy + _scan_in_progress 보호가 추가된 이후 cascade 위험 없음.
        processEvents로 Watchdog ACK 발화 허용. 최대 대기: 0.25초.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            wait = max(0.0, self._MIN_INTERVAL - elapsed)
            self._last_call = now + wait
        if wait > 0:
            from PyQt5.QtWidgets import QApplication
            from PyQt5.QtCore import QEventLoop
            QApplication.processEvents(QEventLoop.AllEvents, max(1, int(wait * 1000)))
        return fn(*args)

class PriorityWatchQueue:
    def __init__(self, kiwoom, screen_no: str = "9200", max_subs: int = 100) -> None:
        self._kiwoom   = kiwoom
        self._screen   = screen_no
        self._max_subs = max_subs
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()

    def refresh(self, top_codes: list[str]) -> None:
        with self._lock:
            target    = set(top_codes[: self._max_subs])
            to_add    = target - self._subscribed
            to_remove = self._subscribed - target
            for code in to_remove:
                self._unsub(code)
            if to_add:
                # 여러 종목을 SetRealReg 1회 배치 호출 (50회 → 1회)
                # strCodeList 에 ';' 구분 다종목 지원 (키움 API 공식 지원)
                code_list = ";".join(to_add)
                self._kiwoom._ocx.dynamicCall(
                    "SetRealReg(QString, QString, QString, QString)",
                    [self._screen, code_list, "10;11;12;13;14;16;17;18;20", "1"],  # [NEW] FID 20: 체결강도
                )
                self._subscribed.update(to_add)
                logger.debug("[PriorityWatchQueue] SetRealReg 배치 등록 %d종목", len(to_add))

    def _sub(self, code: str) -> None:
        self._kiwoom._ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            [self._screen, code, "10;11;12;13;14;16;17;18;20", "1"],  # [NEW] FID 20: 체결강도
        )
        self._subscribed.add(code)

    def _unsub(self, code: str) -> None:
        self._kiwoom._ocx.dynamicCall(
            "SetRealRemove(QString, QString)", [self._screen, code]
        )
        self._subscribed.discard(code)

    @property
    def subscribed(self) -> set[str]:
        return self._subscribed.copy()