"""청산 검증자 체인 테스트 (Step 3)"""

import pytest
from datetime import datetime
from unittest.mock import Mock, MagicMock

from app.exit_validator import (
    ExitValidatorChain,
    ExitValidationContext,
    StrategyExitValidator,
    EODDayTargetValidator,
    EODGapValidator,
    EODTrendBreakValidator,
    EODTimecutValidator,
    Phase1LiquidationValidator,
    MarketCloseValidator,
    ExitDecisionAggregator,
)


# ============================================================================
# Mock 객체 정의
# ============================================================================


class MockPosition:
    """Position 모의 객체"""
    def __init__(
        self,
        code="005930",
        name="삼성전자",
        qty=10,
        avg_price=50000,
        current_price=50000,
        entry_phase=0,
        eod_trade=False,
        overnight_held=False,
    ):
        self.code = code
        self.name = name
        self.qty = qty
        self.avg_price = avg_price
        self.current_price = current_price
        self.entry_phase = entry_phase
        self.eod_trade = eod_trade
        self.overnight_held = overnight_held
        self.daily_ma20 = avg_price
        self.daily_open = avg_price


class MockConfig:
    """SmartScannerConfig 모의 객체"""
    def __init__(self):
        self.stop_loss_pct = -1.2
        self.take_profit_pct = 2.5
        self.partial_profit_pct = 1.5
        self.gap_threshold = 1.0
        self.overnight_min_profit = 0.5


class MockStrategy:
    """Strategy 모의 객체"""
    def __init__(self):
        self.should_exit_result = (False, "")
        self.should_partial_exit_result = (False, 0.0)

    def should_exit(self, pos, ctx):
        return self.should_exit_result

    def should_partial_exit(self, pos, ctx):
        return self.should_partial_exit_result


# ============================================================================
# 단위 테스트
# ============================================================================


class TestStrategyExitValidator:
    def test_strategy_full_exit(self):
        validator = StrategyExitValidator()
        pos = MockPosition(current_price=49000)  # -2% 손절
        strategy = MockStrategy()
        strategy.should_exit_result = (True, "손절 지점")
        exit_ctx = Mock()

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            strategy=strategy,
            exit_context=exit_ctx,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert qty_pct == 1.0

    def test_strategy_partial_exit(self):
        validator = StrategyExitValidator()
        pos = MockPosition()
        strategy = MockStrategy()
        strategy.should_partial_exit_result = (True, 0.5)
        exit_ctx = Mock()

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            strategy=strategy,
            exit_context=exit_ctx,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert qty_pct == 0.5

    def test_no_exit(self):
        validator = StrategyExitValidator()
        pos = MockPosition()
        strategy = MockStrategy()
        exit_ctx = Mock()

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            strategy=strategy,
            exit_context=exit_ctx,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit


class TestEODDayTargetValidator:
    def test_stop_loss(self):
        validator = EODDayTargetValidator()
        pos = MockPosition(eod_trade=True, current_price=49400)  # -1.2% 손절
        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert qty_pct == 1.0
        assert "손절" in reason

    def test_take_profit(self):
        validator = EODDayTargetValidator()
        pos = MockPosition(eod_trade=True, current_price=51250)  # +2.5% 익절
        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert qty_pct == 1.0
        assert "익절" in reason

    def test_partial_profit(self):
        validator = EODDayTargetValidator()
        pos = MockPosition(eod_trade=True, current_price=50750)  # +1.5% 분할
        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert qty_pct == 0.5

    def test_skip_overnight(self):
        validator = EODDayTargetValidator()
        pos = MockPosition(eod_trade=True, overnight_held=True)
        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit


class TestEODGapValidator:
    def test_gap_down(self):
        validator = EODGapValidator()
        pos = MockPosition(eod_trade=True, avg_price=50000, current_price=49400)  # -1.2% 갭다운
        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert "갭다운" in reason

    def test_gap_up(self):
        validator = EODGapValidator()
        pos = MockPosition(eod_trade=True, avg_price=50000, current_price=50600)  # +1.2% 갭업
        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert "갭업" in reason

    def test_gap_neutral_sets_overnight_flag(self):
        validator = EODGapValidator()
        pos = MockPosition(eod_trade=True, avg_price=50000, current_price=50000)  # 0% 보합
        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit
        assert pos.overnight_held  # Flag should be set


class TestEODTrendBreakValidator:
    def test_trend_break(self):
        validator = EODTrendBreakValidator()
        pos = MockPosition(overnight_held=True, current_price=49000)  # close < ma20
        pos.daily_ma20 = 50000
        pos.daily_open = 50000

        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert "추세파괴" in reason

    def test_no_trend_break(self):
        validator = EODTrendBreakValidator()
        pos = MockPosition(overnight_held=True, current_price=51000)
        pos.daily_ma20 = 50000
        pos.daily_open = 49000

        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit


class TestEODTimecutValidator:
    def test_timecut_with_min_profit(self):
        """09:30 도달 + 최소 수익률 충족 → 청산하지 않음"""
        validator = EODTimecutValidator()
        pos = MockPosition(overnight_held=True, avg_price=50000, current_price=50250)  # +0.5%
        now = datetime.strptime("09:30", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit  # 최소 수익률 달성 → 유지

    def test_timecut_below_min_profit(self):
        """09:30 도달 + 최소 수익률 미달 → 청산 (손절)"""
        validator = EODTimecutValidator()
        pos = MockPosition(overnight_held=True, avg_price=50000, current_price=50100)  # +0.2% (미달)
        now = datetime.strptime("09:30", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit  # 최소 수익률 미달 → 강제 청산
        assert qty_pct == 1.0

    def test_before_timecut(self):
        """09:30 이전 → 청산하지 않음"""
        validator = EODTimecutValidator()
        pos = MockPosition(overnight_held=True, avg_price=50000, current_price=50100)
        now = datetime.strptime("09:29", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit


class TestPhase1LiquidationValidator:
    def test_phase1_liquidation(self):
        validator = Phase1LiquidationValidator()
        pos = MockPosition(entry_phase=1)
        now = datetime.strptime("10:30", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert qty_pct == 1.0

    def test_before_10_30(self):
        validator = Phase1LiquidationValidator()
        pos = MockPosition(entry_phase=1)
        now = datetime.strptime("10:29", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit


class TestMarketCloseValidator:
    def test_market_close_liquidation(self):
        validator = MarketCloseValidator()
        pos = MockPosition(eod_trade=False)
        now = datetime.strptime("15:10", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert should_exit
        assert qty_pct == 1.0

    def test_skip_eod_positions(self):
        validator = MarketCloseValidator()
        pos = MockPosition(eod_trade=True)
        now = datetime.strptime("15:10", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        should_exit, reason, qty_pct = validator.validate(pos, ctx)
        assert not should_exit


# ============================================================================
# 체인 통합 테스트
# ============================================================================


class TestExitValidatorChain:
    def test_chain_stops_at_first_validator(self):
        """체인이 첫 번째 통과 지점에서 중단해야 함"""
        chain = ExitValidatorChain()
        pos = MockPosition(eod_trade=True, current_price=49400)  # EOD 손절

        ctx = ExitValidationContext(trading_cfg=MockConfig())

        should_exit, reason, qty_pct = chain.validate(pos, ctx)
        assert should_exit
        assert "손절" in reason

    def test_chain_all_validators_checked(self):
        """어떤 validator도 조건을 만족하지 않으면 False 반환"""
        chain = ExitValidatorChain()
        pos = MockPosition(
            eod_trade=False,
            overnight_held=False,
            entry_phase=0,
            current_price=50000,  # 변화 없음
        )

        now = datetime.strptime("14:00", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        strategy = MockStrategy()
        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            strategy=strategy,
            now=now,
        )

        should_exit, reason, qty_pct = chain.validate(pos, ctx)
        assert not should_exit


class TestExitDecisionAggregator:
    def test_process_positions(self):
        """여러 포지션에 대해 청산 판정 및 실행"""
        aggregator = ExitDecisionAggregator()
        order_mgr = Mock()
        order_mgr.force_exit = Mock()
        order_mgr.sell = Mock()
        log_signal = Mock()

        # 포지션 생성: 하나는 손절, 하나는 유지
        positions = {
            "005930": MockPosition(code="005930", eod_trade=True, avg_price=50000, current_price=49400),  # 손절
            "000660": MockPosition(code="000660", eod_trade=False, avg_price=50000, current_price=50000),  # 유지
        }

        now = datetime.strptime("14:00", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day,
        )

        ctx = ExitValidationContext(
            trading_cfg=MockConfig(),
            now=now,
        )

        aggregator.process_positions(positions, ctx, order_mgr, log_signal)

        # force_exit이 한 번 호출되어야 함 (005930)
        assert order_mgr.force_exit.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
