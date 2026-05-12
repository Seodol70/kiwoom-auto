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
        from datetime import datetime
        from scanner.evaluators.common import _resolve_time_slot
        now = datetime.now().time()
        slot = _resolve_time_slot(now, cfg)

        # 진단 로그를 파일에 직접 저장
        with open("d:\\prj\\kiwoom-auto\\logs\\breakout_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{now}] PASS 신호 도달: {snap.code}({snap.name}) slot={slot}\n")
            f.flush()

        if slot == "OPENING":
            gate_reason = "[OPENING_GATE_SKIP]"
        else:
            gate_reason = check_breakout_gate(snap, cfg)
            # 진단 로그 (파일 + 시스템 로그)
            with open("d:\\prj\\kiwoom-auto\\logs\\breakout_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{now}] gate_reason={gate_reason} for {snap.code}({snap.name})\n")
                f.flush()
            logger.warning("[BREAKOUT 진단] %s(%s) gate_reason=%s", snap.code, snap.name, gate_reason or "FAIL")
            if not gate_reason:
                with open("d:\\prj\\kiwoom-auto\\logs\\breakout_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{now}] BLOCKED by gate: {snap.code}({snap.name})\n")
                    f.flush()
                logger.warning("[BREAKOUT 차단] %s(%s) gate 필터 실패", snap.code, snap.name)
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

        sig = ScanSignal(
            snap.code, snap.name, self.name, reason, snap.current_price,
            is_warmup="[WARMUP]" in reason,
            values=ai_features
        )
        with open("d:\\prj\\kiwoom-auto\\logs\\breakout_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{now}] ScanSignal 생성: {snap.code}({snap.name}) sig={sig}\n")
            f.flush()
        return sig
