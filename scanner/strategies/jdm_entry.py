from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.indicator_service import IndicatorService
from scanner.signal_evaluator import check_jdm_entry

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class JdmStrategy(BaseStrategy):
    """
    JDM(Joseph) 진입 전략.
    복합적인 수급, 추세, 지표 필터를 통과해야 함.
    모든 판정 로직은 signal_evaluator.check_jdm_entry()에 위임됨.
    """

    def __init__(self):
        super().__init__("JDM_ENTRY")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig,
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        """
        신호 판정을 수행하고 ScanSignal 객체를 생성하여 반환한다.
        """
        # 핵심 판정 로직 위임
        reason = check_jdm_entry(snap, cfg)
        if reason is None:
            return None

        # AI 피처 추출 (학습용 데이터 수집)
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        # 신호 생성
        is_warmup = "WARMUP" in reason
        entry_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0

        return ScanSignal(
            snap.code, snap.name, self.name, snap.current_price, reason,
            entry_candle_low=entry_low,
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
            is_warmup=is_warmup,
            values=ai_features
        )
