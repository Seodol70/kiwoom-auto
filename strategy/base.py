# -*- coding: utf-8 -*-
"""
BaseStrategy - 모든 매매 전략의 추상 베이스 클래스
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Any

if TYPE_CHECKING:
    from scanner.models import ScanSignal
    from app.risk_manager import RiskManager
    from order.order_manager import OrderManager

@dataclass
class ExitContext:
    """청산 판정용 파라미터 (시간대별 오버라이드 가능)"""
    sl_pct: float
    trail_activation: float
    trail_tier1: float
    trail_tier2: float
    trail_tier3: float
    time_cut_min: int
    partial_profit_pct: float = 0.0
    atr_trail_enabled: bool = False

class BaseStrategy(ABC):
    """
    모든 매매 전략의 기본 클래스.
    진입 필터링, 청산 판정, 상태 업데이트 로직을 캡슐화합니다.
    """

    def __init__(self, order_mgr: OrderManager, risk_mgr: RiskManager, scan_cfg: Any):
        self._order_mgr = order_mgr
        self._risk_mgr = risk_mgr
        self._scan_cfg = scan_cfg

    @abstractmethod
    def should_entry(self, sig: ScanSignal, auto_trading: bool) -> tuple[bool, str]:
        """
        신규 진입 가능 여부를 판단합니다.
        
        Returns:
            (bool, str): (진입여부, 사유)
        """
        pass

    @abstractmethod
    def should_exit(self, pos: Any, ctx: ExitContext) -> tuple[bool, str]:
        """
        기 보유 포지션의 전량 청산 여부를 판단합니다.
        
        Returns:
            (bool, str): (청산여부, 사유)
        """
        pass

    @abstractmethod
    def should_partial_exit(self, pos: Any, ctx: ExitContext) -> tuple[bool, float]:
        """
        분할 익절 여부와 비율을 판단합니다.
        
        Returns:
            (bool, float): (익절여부, 매도비율 0.0~1.0)
        """
        pass

    @abstractmethod
    def update_state(self, pos: Any, ctx: Optional["ExitContext"] = None) -> None:
        """
        실시간 가격 데이터에 따라 포지션의 상태(최고가 등)를 업데이트합니다.
        ctx가 있으면 시간대별 trail_activation 파라미터를 우선 적용합니다.
        """
        pass

    def get_name(self) -> str:
        """전략의 이름을 반환합니다."""
        return self.__class__.__name__
