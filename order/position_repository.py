"""
PositionRepository — positions dict 캡슐화

SmartScanner 등 외부 모듈이 Position을 직접 수정하지 않도록 공개 API 제공.
모든 positions 수정은 이 저장소를 통해 진행되어 스레드 안전성 보장.
"""
from __future__ import annotations

from typing import Optional
from PyQt5.QtCore import QObject, pyqtSlot


class PositionRepository(QObject):
    """포지션 저장소 — positions dict 캡슐화"""

    def __init__(self, positions: dict) -> None:
        """
        Args:
            positions: OrderManager.positions (dict[code] = Position)
        """
        super().__init__()
        self._positions = positions  # OrderManager 의존성

    @pyqtSlot(str, int)
    def update_price(self, code: str, price: int) -> None:
        """
        현재가 갱신 (메인 스레드에서만 호출).

        SmartScanner._on_receive_real_data 에서
        QMetaObject.invokeMethod(..., Qt.QueuedConnection) 으로 호출됨.

        Args:
            code: 종목 코드
            price: 새로운 현재가
        """
        if code in self._positions:
            self._positions[code].current_price = price

    def get(self, code: str) -> Optional:
        """포지션 조회 (없으면 None)."""
        return self._positions.get(code)

    def list_all(self) -> list:
        """모든 포지션 조회."""
        return list(self._positions.values())

    def exists(self, code: str) -> bool:
        """포지션 존재 여부."""
        return code in self._positions

    def count(self) -> int:
        """보유 종목 수."""
        return len(self._positions)

    def cleanup(self) -> None:
        """(향후 사용) 저장소 정리."""
        pass
