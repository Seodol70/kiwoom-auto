from __future__ import annotations
import logging
import threading


logger = logging.getLogger(__name__)


class TopVolumeManager:
    def __init__(self, top_n: int = 200) -> None:
        self.top_n     = top_n
        self._amounts: dict[str, int] = {}
        self._lock     = threading.Lock()


    def clear(self) -> None:
        """이전 스캔에서 쌓인 거래대금 맵을 비운다."""
        with self._lock:
            self._amounts.clear()


    def update(self, code: str, trade_amount: int) -> bool:
        with self._lock:
            self._amounts[code] = trade_amount
            return self._rank(code) <= self.top_n


    def get_top_codes(self, n: Optional[int] = None) -> list[str]:
        n = n or self.top_n
        with self._lock:
            return [c for c, _ in sorted(
                self._amounts.items(), key=lambda x: -x[1]
            )[:n]]


    def _rank(self, code: str) -> int:
        sorted_codes = [c for c, _ in sorted(
            self._amounts.items(), key=lambda x: -x[1]
        )]
        try:
            return sorted_codes.index(code) + 1
        except ValueError:
            return 999999
