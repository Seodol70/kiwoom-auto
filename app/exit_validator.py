"""청산 검증자 체인 (Composite 패턴)"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from order.order_manager import OrderManager
    from order.order_manager import Position
    from scanner.smart_scanner import SmartScannerConfig
    from strategy.base import ExitContext
    from strategy.jang_dong_min import JangDongMinStrategy


logger = logging.getLogger(__name__)


@dataclass
class ExitValidationContext:
    """청산 검증 컨텍스트 (의존성 주입)"""
    trading_cfg: SmartScannerConfig
    strategy: Optional[JangDongMinStrategy] = None
    now: Optional[datetime] = None
    exit_context: Optional[ExitContext] = None


class ExitValidator(ABC):
    """청산 검증자 인터페이스"""

    @abstractmethod
    def validate(
        self,
        pos: Position,
        ctx: ExitValidationContext,
    ) -> tuple[bool, str, float]:
        """
        청산 판정 실행.

        Args:
            pos: 포지션 객체
            ctx: 검증 컨텍스트

        Returns:
            (should_exit: bool, reason: str, exit_qty_pct: float)
            - exit_qty_pct: 0.0 (미청산) ~ 1.0 (전량) ~ 0.5 (분할)
        """
        pass


class StrategyExitValidator(ExitValidator):
    """전략 기반 청산 (should_exit/should_partial_exit 위임)"""

    def validate(self, pos: Position, ctx: ExitValidationContext) -> tuple[bool, str, float]:
        if not ctx.strategy or not ctx.exit_context:
            return False, "", 0.0

        # Partial exit 우선 체크 (분할익절)
        try:
            partial_passed, partial_ratio = ctx.strategy.should_partial_exit(pos, ctx.exit_context)
            if partial_passed and partial_ratio > 0:
                logger.debug("[분할익절] %s — 비율 %.1f%%", pos.code, partial_ratio * 100)
                return True, f"분할익절 ({partial_ratio*100:.0f}%)", partial_ratio
        except Exception as e:
            logger.warning("[분할익절 에러] %s: %s", pos.code, e)

        # Full exit 체크
        try:
            full_passed, reason = ctx.strategy.should_exit(pos, ctx.exit_context)
            if full_passed:
                logger.debug("[전체청산] %s — 사유: %s", pos.code, reason)
                return True, reason, 1.0
        except Exception as e:
            logger.warning("[청산 판정 에러] %s: %s", pos.code, e)

        return False, "", 0.0


class EODDayTargetValidator(ExitValidator):
    """EOD 포지션 일중 수익률 목표"""

    def validate(self, pos: Position, ctx: ExitValidationContext) -> tuple[bool, str, float]:
        # EOD 일중 포지션만 처리
        if not getattr(pos, "eod_trade", False):
            return False, "", 0.0
        if getattr(pos, "overnight_held", False):
            return False, "", 0.0

        if pos.qty <= 0 or pos.avg_price <= 0:
            return False, "", 0.0

        # 수익률 계산
        pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price * 100

        # 손절 체크
        sl_pct = float(getattr(ctx.trading_cfg, "stop_loss_pct", -1.2))
        if pnl_pct <= sl_pct:
            reason = f"손절 ({pnl_pct:.2f}% < {sl_pct}%)"
            logger.info("[손절] %s(%s) %s", pos.name, pos.code, reason)
            return True, reason, 1.0

        # 익절 체크
        tp_pct = float(getattr(ctx.trading_cfg, "jdm_take_profit_pct", getattr(ctx.trading_cfg, "take_profit_pct", 3.0)))
        if pnl_pct >= tp_pct:
            reason = f"익절 ({pnl_pct:.2f}% >= {tp_pct}%)"
            logger.info("[익절] %s(%s) %s", pos.name, pos.code, reason)
            return True, reason, 1.0

        # 분할익절 체크
        partial_tp = float(getattr(ctx.trading_cfg, "partial_profit_pct", 1.5))
        if pnl_pct >= partial_tp:
            reason = f"분할익절 ({pnl_pct:.2f}%)"
            logger.debug("[분할] %s(%s) %s", pos.name, pos.code, reason)
            return True, reason, 0.5

        return False, "", 0.0


class EODGapValidator(ExitValidator):
    """EOD 포지션 갭 분류 및 처리"""

    def validate(self, pos: Position, ctx: ExitValidationContext) -> tuple[bool, str, float]:
        # EOD 일중 포지션만 처리
        if not getattr(pos, "eod_trade", False):
            return False, "", 0.0
        if getattr(pos, "overnight_held", False):
            return False, "", 0.0

        if pos.qty <= 0 or pos.avg_price <= 0:
            return False, "", 0.0

        # 갭 분류
        gap_pct = (pos.current_price - pos.avg_price) / pos.avg_price * 100
        gap_threshold = float(getattr(ctx.trading_cfg, "gap_threshold", 1.0))

        if gap_pct < -gap_threshold:
            # 갭다운 → 강제 청산
            reason = f"갭다운 ({gap_pct:.2f}%)"
            logger.info("[갭다운] %s(%s) %s", pos.name, pos.code, reason)
            return True, reason, 1.0

        if gap_pct > gap_threshold:
            # 갭업 → 강제 청산
            reason = f"갭업 ({gap_pct:.2f}%)"
            logger.info("[갭업] %s(%s) %s", pos.name, pos.code, reason)
            return True, reason, 1.0

        # 보합 → overnight_held=True 플래그 설정 (상태 전이)
        logger.debug("[갭보합] %s(%s) overnight 전환 준비", pos.name, pos.code)
        pos.overnight_held = True
        return False, "", 0.0


class EODTrendBreakValidator(ExitValidator):
    """overnight 포지션 일봉 정배열 파괴 시 강제 청산"""

    def validate(self, pos: Position, ctx: ExitValidationContext) -> tuple[bool, str, float]:
        if not getattr(pos, "overnight_held", False):
            return False, "", 0.0

        if pos.qty <= 0:
            return False, "", 0.0

        # 일봉 정배열 확인 (close > ma20 > open)
        daily_ma20 = getattr(pos, "daily_ma20", 0)
        daily_open = getattr(pos, "daily_open", 0)
        daily_close = pos.current_price

        # 정배열 파괴: close < ma20 또는 ma20 < open
        if (daily_close < daily_ma20) or (daily_ma20 < daily_open and daily_open > 0):
            reason = f"추세파괴 (close={daily_close}, ma20={daily_ma20}, open={daily_open})"
            logger.info("[추세파괴] %s(%s) %s", pos.name, pos.code, reason)
            return True, reason, 1.0

        return False, "", 0.0


class EODTimecutValidator(ExitValidator):
    """overnight 포지션 09:30 데드라인 + 최소수익 확인"""

    def validate(self, pos: Position, ctx: ExitValidationContext) -> tuple[bool, str, float]:
        if not getattr(pos, "overnight_held", False):
            return False, "", 0.0

        now = ctx.now or datetime.now()
        timecut_hour = 9
        timecut_min = 30

        # 09:30 도달 확인
        if not (now.hour == timecut_hour and now.minute >= timecut_min):
            return False, "", 0.0

        if pos.qty <= 0 or pos.avg_price <= 0:
            return False, "", 0.0

        # 최소수익률 확인
        min_profit_pct = float(getattr(ctx.trading_cfg, "overnight_min_profit", 0.5))
        pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price * 100

        if pnl_pct < min_profit_pct:
            reason = f"09:30 타임컷 (수익률 {pnl_pct:.2f}% < {min_profit_pct}%)"
            logger.info("[타임컷] %s(%s) %s", pos.name, pos.code, reason)
            return True, reason, 1.0

        return False, "", 0.0


class Phase1LiquidationValidator(ExitValidator):
    """Phase1 포지션 정리 (10:30 강제/트레일)"""

    def validate(self, pos: Position, ctx: ExitValidationContext) -> tuple[bool, str, float]:
        if getattr(pos, "entry_phase", 0) != 1:
            return False, "", 0.0

        now = ctx.now or datetime.now()
        phase1_close_hour = 10
        phase1_close_min = 30

        # 10:30 도달 확인
        if not (now.hour >= phase1_close_hour and now.minute >= phase1_close_min):
            return False, "", 0.0

        if pos.qty <= 0 or pos.avg_price <= 0:
            return False, "", 0.0

        # 10:30 강제 청산
        reason = f"Phase1 10:30 강제정리"
        logger.info("[Phase1정리] %s(%s) %s", pos.name, pos.code, reason)
        return True, reason, 1.0


class MarketCloseValidator(ExitValidator):
    """장 마감 전 전체 청산 (EOD 포지션 제외)"""

    def validate(self, pos: Position, ctx: ExitValidationContext) -> tuple[bool, str, float]:
        # EOD 포지션은 제외 (별도 처리)
        if getattr(pos, "eod_trade", False):
            return False, "", 0.0

        now = ctx.now or datetime.now()
        close_hour = 15
        close_min = 10

        # 15:10 이후 확인
        if not (now.hour >= close_hour and now.minute >= close_min):
            return False, "", 0.0

        if pos.qty <= 0:
            return False, "", 0.0

        # 장 마감 청산
        reason = "장 마감 청산"
        logger.info("[마감청산] %s(%s) %s", pos.name, pos.code, reason)
        return True, reason, 1.0


class ExitValidatorChain:
    """청산 검증자 체인 (Composite 패턴)"""

    def __init__(self):
        self.validators = [
            StrategyExitValidator(),
            EODDayTargetValidator(),
            EODGapValidator(),
            EODTrendBreakValidator(),
            EODTimecutValidator(),
            Phase1LiquidationValidator(),
            MarketCloseValidator(),
        ]

    def validate(
        self,
        pos: Position,
        ctx: ExitValidationContext,
    ) -> tuple[bool, str, float]:
        """
        체인 실행: 첫 통과 지점에서 반환 (우선순위).

        Returns:
            (should_exit: bool, reason: str, exit_qty_pct: float)
        """
        for validator in self.validators:
            try:
                should_exit, reason, qty_pct = validator.validate(pos, ctx)
                if should_exit:
                    return True, reason, qty_pct
            except Exception as e:
                logger.warning("[검증자 에러] %s (%s): %s", validator.__class__.__name__, pos.code, e)

        return False, "", 0.0


class ExitDecisionAggregator:
    """여러 청산 판정을 수집 + 로그 + 실행"""

    def process_positions(
        self,
        positions: dict[str, "Position"],
        ctx: ExitValidationContext,
        order_mgr: OrderManager,
        log_signal,
    ) -> None:
        """
        모든 포지션에 대해 청산 판정 실행 및 실행.

        Args:
            positions: code → Position 매핑
            ctx: 검증 컨텍스트
            order_mgr: 주문 관리자 (force_exit/sell 호출)
            log_signal: UI 로그 신호 (pyqtSignal)
        """
        chain = ExitValidatorChain()

        for code, pos in list(positions.items()):
            should_exit, reason, qty_pct = chain.validate(pos, ctx)

            if should_exit:
                self._execute_exit(code, pos, reason, qty_pct, order_mgr, log_signal)

    def _execute_exit(
        self,
        code: str,
        pos: Position,
        reason: str,
        qty_pct: float,
        order_mgr: OrderManager,
        log_signal,
    ) -> None:
        """청산 실행 + 로그 (중앙화)"""
        # 로그
        msg = f"[청산] {pos.name}({code}) {reason}"
        logger.info(msg)
        if log_signal:
            log_signal.emit(f"🔴 {msg}")

        # 청산 실행
        exit_qty = int(pos.qty * qty_pct)
        if exit_qty <= 0:
            return

        if qty_pct >= 1.0:
            # 전량 청산 → force_exit
            order_mgr.force_exit(code, pos.name, exit_qty, reason)
        else:
            # 분할 청산 → sell
            order_mgr.sell(code, pos.name, exit_qty)
