"""
test_pnl_tracker.py — PnLTracker 손익 계산 단위 테스트

수수료·세금을 포함한 실제 손익 계산 검증.
"""

import pytest
from order.pnl_tracker import PnLTracker


class TestCalculatePnL:
    """평가손익 계산 테스트"""

    def test_pnl_profit(self):
        """수익 - 간단한 경우"""
        pnl = PnLTracker.calculate_pnl(
            avg_price=100_000,
            current_price=105_000,
            qty=10,
        )
        # 순이익 = (105_000 - 100_000) × 10 - 수수료 - 세금
        # = 50_000 - 매수수수료 - 매도수수료
        assert pnl > 0
        assert pnl < 50_000  # 수수료/세금으로 인한 감소

    def test_pnl_loss(self):
        """손실 - 가격 하락"""
        pnl = PnLTracker.calculate_pnl(
            avg_price=100_000,
            current_price=98_000,
            qty=10,
        )
        # 손실 = (98_000 - 100_000) × 10 - 수수료 - 세금
        assert pnl < 0

    def test_pnl_zero(self):
        """손익분기점 근처"""
        pnl = PnLTracker.calculate_pnl(
            avg_price=100_000,
            current_price=100_000,
            qty=10,
        )
        # 현재가 = 평단이면 순손실만 (수수료·세금)
        assert pnl < 0

    def test_pnl_fee_included(self):
        """수수료 감소 확인"""
        pnl_no_fee = PnLTracker.calculate_pnl(
            avg_price=100_000,
            current_price=101_000,
            qty=10,
            fee_rate=0.0,
            tax_rate=0.0,
        )

        pnl_with_fee = PnLTracker.calculate_pnl(
            avg_price=100_000,
            current_price=101_000,
            qty=10,
            fee_rate=0.00015,
            tax_rate=0.0023,
        )

        # 수수료 있을 때가 더 작아야 함
        assert pnl_with_fee < pnl_no_fee

    def test_pnl_large_quantity(self):
        """대량 거래"""
        pnl = PnLTracker.calculate_pnl(
            avg_price=10_000,
            current_price=10_100,
            qty=1000,
        )
        # 100 × 1000 = 100_000 이익 (수수료 차감 후)
        assert pnl > 50_000

    def test_pnl_small_quantity(self):
        """소량 거래"""
        pnl = PnLTracker.calculate_pnl(
            avg_price=100_000,
            current_price=105_000,
            qty=1,
        )
        # 5_000 이익 (수수료 차감)
        assert 0 < pnl < 5_000


class TestCalculateReturnPct:
    """수익률 계산 테스트"""

    def test_return_pct_positive(self):
        """양의 수익률"""
        ret_pct = PnLTracker.calculate_return_pct(
            avg_price=100_000,
            current_price=110_000,
            qty=10,
        )
        # 100_000 이익 / 1_000_000 비용 = 10% 이상 (수수료 차감 후)
        assert ret_pct > 5.0  # 수수료로 인한 감소

    def test_return_pct_negative(self):
        """음의 수익률"""
        ret_pct = PnLTracker.calculate_return_pct(
            avg_price=100_000,
            current_price=90_000,
            qty=10,
        )
        # -100_000 손실 / 1_000_000 비용 = -10%
        assert ret_pct < -5.0

    def test_return_pct_zero_cost(self):
        """비용이 0일 때 (에지 케이스)"""
        ret_pct = PnLTracker.calculate_return_pct(
            avg_price=0,
            current_price=100_000,
            qty=10,
        )
        assert ret_pct == 0.0

    def test_return_pct_small_profit(self):
        """1% 수익"""
        ret_pct = PnLTracker.calculate_return_pct(
            avg_price=100_000,
            current_price=101_000,
            qty=10,
        )
        # 약 0.7~0.8% (수수료로 인한 감소)
        assert 0 < ret_pct < 1.0


class TestCalculatePureChangePct:
    """순수 등락률 계산 테스트 (수수료·세금 미반영)"""

    def test_pure_change_pct_up(self):
        """상승"""
        change_pct = PnLTracker.calculate_pure_change_pct(
            avg_price=100_000,
            current_price=105_000,
        )
        assert change_pct == 5.0

    def test_pure_change_pct_down(self):
        """하락"""
        change_pct = PnLTracker.calculate_pure_change_pct(
            avg_price=100_000,
            current_price=95_000,
        )
        assert change_pct == -5.0

    def test_pure_change_pct_zero(self):
        """변화 없음"""
        change_pct = PnLTracker.calculate_pure_change_pct(
            avg_price=100_000,
            current_price=100_000,
        )
        assert change_pct == 0.0

    def test_pure_change_pct_edge_case_zero_avg(self):
        """평단 0일 때"""
        change_pct = PnLTracker.calculate_pure_change_pct(
            avg_price=0,
            current_price=100_000,
        )
        assert change_pct == 0.0

    def test_pure_change_pct_exact(self):
        """정확한 계산"""
        change_pct = PnLTracker.calculate_pure_change_pct(
            avg_price=50_000,
            current_price=55_000,
        )
        assert change_pct == 10.0


class TestCalculateUnrealizedCostMinusValue:
    """매입금액 - 평가금액 계산 테스트 (UI 손익 열)"""

    def test_cost_minus_value_positive(self):
        """손실 (평가금액 < 매입금액)"""
        cmv = PnLTracker.calculate_unrealized_cost_minus_value(
            avg_price=100_000,
            current_price=95_000,
            qty=10,
        )
        # 1_000_000 - 950_000 = 50_000
        assert cmv == 50_000

    def test_cost_minus_value_negative(self):
        """이익 (평가금액 > 매입금액)"""
        cmv = PnLTracker.calculate_unrealized_cost_minus_value(
            avg_price=100_000,
            current_price=105_000,
            qty=10,
        )
        # 1_000_000 - 1_050_000 = -50_000
        assert cmv == -50_000

    def test_cost_minus_value_zero(self):
        """변화 없음"""
        cmv = PnLTracker.calculate_unrealized_cost_minus_value(
            avg_price=100_000,
            current_price=100_000,
            qty=10,
        )
        assert cmv == 0
