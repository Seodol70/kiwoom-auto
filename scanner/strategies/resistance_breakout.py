from __future__ import annotations
import time
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.indicator_service import IndicatorService
from scanner.evaluators.resistance_breakout import check_resistance_breakout

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig


class ResistanceBreakoutStrategy(BaseStrategy):
    """
    저항선(직전 N분 구간 고점) 돌파 전략 (RESISTANCE_BREAKOUT).

    [2026-06-30] "오를 종목 포착 3가지 신호" ③번. 판정 로직은
    scanner.evaluators.resistance_breakout.check_resistance_breakout()에 위임됨.
    """

    # 종목별 마지막 신호 시각 — 동일 종목 신호 스팸 방지
    _last_signal_ts: dict[str, float] = {}

    def __init__(self):
        super().__init__("RESISTANCE_BREAKOUT")

    def evaluate(
        self,
        snap: "StockSnapshot",
        cfg: "SmartScannerConfig",
        index_history: Optional[dict[str, list[float]]] = None,
    ) -> Optional[ScanSignal]:
        # 종목별 쿨다운 — _emit() 쿨다운과 독립적으로 전략 레벨에서 차단
        _cooldown = float(getattr(cfg, "rb_signal_cooldown_sec", 60.0))
        _now_ts = time.monotonic()
        if _now_ts - ResistanceBreakoutStrategy._last_signal_ts.get(snap.code, 0.0) < _cooldown:
            return None

        reason = check_resistance_breakout(snap, cfg)
        if reason is None:
            return None

        ResistanceBreakoutStrategy._last_signal_ts[snap.code] = _now_ts

        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        return ScanSignal(
            snap.code, snap.name, self.name, reason, snap.current_price,
            is_warmup=False,
            values=ai_features,
        )
