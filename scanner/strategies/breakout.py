"""
breakout.py — 돌파 매매 전략
"""
from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.indicator_service import IndicatorService
from scanner.signal_evaluator import check_breakout, check_breakout_gate

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class BreakoutStrategy(BaseStrategy):
    """
    돌파 매매 전략 (BREAKOUT).
    모든 판정 로직은 signal_evaluator 로직을 재사용함.
    """

    def __init__(self):
        super().__init__("BREAKOUT")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig, 
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        # 1. 기본 돌파 체크 (기존 check_breakout 로직)
        # cfg에 정의된 파라미터 사용
        breakout_reason = check_breakout(
            snap,
            breakout_ratio=cfg.breakout_ratio,
            pullback_from_high_pct=cfg.breakout_pullback_from_high_pct,
            min_rising_bars=cfg.breakout_min_rising_bars
        )
        if not breakout_reason:
            return None

        # 2. 진입 게이트 체크 (공통 필터) — OPENING 슬롯에서는 스킵 (2026-05-12: 학습 데이터 수집)
        # [CLEANUP 2026-05-26] breakout_debug.log 디스크 IO + WARNING 로그 전부 제거
        # — 매 신호 평가마다 4회 파일 flush + 2회 WARNING → UI 큐 폭주 / 디스크 부하 원인
        # — 2026-05-26 14:15:00 UI 프리징 사건이 이 로그 폭주로 발생
        from datetime import datetime
        from scanner.evaluators.common import _resolve_time_slot
        now = datetime.now().time()
        slot = _resolve_time_slot(now, cfg)

        if slot == "OPENING":
            gate_reason = "[OPENING_GATE_SKIP]"
        else:
            gate_reason = check_breakout_gate(snap, cfg)
            if not gate_reason:
                # 차단 사유는 evaluators 내부에서 이미 ScannerLogger.rejected로 기록됨
                return None

        # 3. AI 피처 및 신호 생성
        reason = f"{breakout_reason} | {gate_reason}"
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        change_pct = float(getattr(snap, "change_pct", 0) or 0)
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        if candle_low > 0:
            ai_features["entry_candle_low"] = candle_low
        if change_pct != 0:
            ai_features["change_pct"] = change_pct

        return ScanSignal(
            snap.code, snap.name, self.name, reason, snap.current_price,
            is_warmup="[WARMUP]" in reason,
            values=ai_features
        )
