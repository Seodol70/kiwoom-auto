"""
eod.py — 종가매매 중장기 전략 (EOD/Overnight)
"""
from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.indicator_service import IndicatorService
from scanner.evaluators.eod import check_eod_entry

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class EODStrategy(BaseStrategy):
    """
    종가매매 중장기 전략 (EOD = End-Of-Day).
    일봉 정배열 + 신고가 근처 + 분봉 추세 기반으로 익일 보유 목적 진입.
    """

    def __init__(self):
        super().__init__("EOD")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig,
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        # 1. EOD 진입 조건 체크
        eod_reason = check_eod_entry(snap, cfg)
        if not eod_reason:
            return None

        # 2. AI 피처 및 신호 생성
        change_pct = float(getattr(snap, "change_pct", 0) or 0)
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        if change_pct != 0:
            ai_features["change_pct"] = change_pct

        # EOD 포지션 마킹
        ai_features["eod_trade"] = True

        return ScanSignal(
            snap.code, snap.name, self.name, snap.current_price, eod_reason,
            is_warmup="[WARMUP]" in eod_reason,
            values=ai_features
        )
