from __future__ import annotations
import logging
import time
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.indicator_service import IndicatorService
from scanner.evaluators.pullback import check_pullback_entry

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class PullbackStrategy(BaseStrategy):
    """
    눌림목 진입 전략 (PULLBACK).
    조건 판단은 evaluators/pullback.py 의 check_pullback_entry() 에 위임 — 단일 진실 공급원.
    """

    _last_signal_ts: dict[str, float] = {}

    def __init__(self):
        super().__init__("PULLBACK")

    def evaluate(self, snap: "StockSnapshot", cfg: "SmartScannerConfig",
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        # 전략 레벨 쿨다운 (_emit() 와 독립적)
        _cooldown = float(getattr(cfg, "signal_cooldown_sec", 60.0))
        _now_ts = time.monotonic()
        if _now_ts - PullbackStrategy._last_signal_ts.get(snap.code, 0.0) < _cooldown:
            return None

        # 모든 진입 조건 판단은 evaluator 에 위임
        reason = check_pullback_entry(snap, cfg)
        if reason is None:
            return None

        # 쿨다운 타임스탬프 갱신
        PullbackStrategy._last_signal_ts[snap.code] = _now_ts

        # ScanSignal 생성 (신호 메타데이터 빌딩은 strategy 책임)
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)
        ai_features["li_rs"]      = round(IndicatorService.calc_rs_leading_score(
            float(getattr(snap, "rs_score", 0.0) or 0.0)), 3)
        ai_features["li_leading"] = round(IndicatorService.get_leading_score(snap) or 0.0, 3)
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        change_pct = float(getattr(snap, "change_pct", 0) or 0)
        if candle_low > 0:
            ai_features["entry_candle_low"] = candle_low
        if change_pct != 0:
            ai_features["change_pct"] = change_pct

        return ScanSignal(
            snap.code, snap.name, self.name, reason, snap.current_price,
            is_warmup=False,
            values=ai_features
        )
