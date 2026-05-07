"""
base.py — 매수 전략 추상 베이스 클래스
"""
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from scanner.models import StockSnapshot, ScanSignal
    from scanner.config import SmartScannerConfig

class BaseStrategy(ABC):
    """
    모든 매수 전략의 기본 인터페이스.
    """
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        """전략명 (예: 'JDM', 'BREAKOUT')"""
        return self._name

    @abstractmethod
    def evaluate(
        self, 
        snap: "StockSnapshot", 
        cfg: "SmartScannerConfig",
        index_history: Optional[dict[str, list[float]]] = None
    ) -> Optional["ScanSignal"]:
        """
        신호 발생 여부를 평가하고 ScanSignal 객체를 생성한다.
        
        Returns:
            Optional[ScanSignal]: 신호 발생 시 ScanSignal 객체, 아니면 None.
        """
        pass
