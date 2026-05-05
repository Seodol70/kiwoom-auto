from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from scanner.models import StockSnapshot, ScanSignal
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class BaseStrategy(ABC):
    """
    모든 매수 전략의 기본 클래스.
    이 클래스를 상속받아 구체적인 진입 로직을 구현한다.
    """

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig, 
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        """
        종목 스냅샷을 분석하여 진입 신호(ScanSignal) 발생 여부를 결정한다.
        신호 조건 미충족 시 None을 반환한다.
        """
        pass

    def __repr__(self) -> str:
        return f"<Strategy:{self.name}>"
