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


class TestEquivalenceWithOrderManager:
    """
    [리팩토링 2단계, 2026-06-30] PnLTracker가 order_manager.py의 기존 계산식을
    완전히 대체하기 위한 동치성 검증. order_manager.py가 PnLTracker로 교체된 뒤에도
    이 테스트가 두 식(원본 인라인 식 vs PnLTracker) 사이의 회귀를 잡아낸다.

    원본 식 출처:
    - position_pnl()      <- Position.pnl 프로퍼티 (order_manager.py:133-142)
    - sell_fill_realized() <- OrderManager._handle_sell_fill() (order_manager.py:1909-1914)
    """

    FEE = 0.00015
    TAX = 0.0023

    @staticmethod
    def _position_pnl(avg_price, current_price, qty, fee=FEE, tax=TAX):
        if not current_price or not avg_price:
            return 0
        buy_total = avg_price * qty
        sell_total = current_price * qty
        fees = buy_total * fee + sell_total * fee
        tax_amt = sell_total * tax
        return int(sell_total - buy_total - fees - tax_amt)

    @staticmethod
    def _sell_fill_realized(avg_price, filled_price, qty, fee=FEE, tax=TAX):
        sell_amount = filled_price * qty
        buy_amount = avg_price * qty
        cost = round(sell_amount * (fee + tax) + buy_amount * fee)
        return (filled_price - avg_price) * qty - cost

    def test_calculate_pnl_matches_position_pnl_random(self):
        """무작위 2000건에서 calculate_pnl()이 Position.pnl 원본 식과 1원도 틀리지 않는다"""
        import random
        random.seed(42)
        for _ in range(2000):
            avg = random.randint(100, 500_000)
            cur = random.randint(100, 500_000)
            qty = random.randint(1, 1000)
            expected = self._position_pnl(avg, cur, qty)
            actual = PnLTracker.calculate_pnl(avg, cur, qty, fee_rate=self.FEE, tax_rate=self.TAX)
            assert actual == expected, f"avg={avg} cur={cur} qty={qty}: expected={expected} actual={actual}"

    def test_calculate_realized_pnl_matches_sell_fill_random(self):
        """무작위 2000건에서 calculate_realized_pnl()이 _handle_sell_fill() 원본 식과 1원도 틀리지 않는다"""
        import random
        random.seed(43)
        for _ in range(2000):
            avg = random.randint(100, 500_000)
            cur = random.randint(100, 500_000)
            qty = random.randint(1, 1000)
            expected = self._sell_fill_realized(avg, cur, qty)
            actual = PnLTracker.calculate_realized_pnl(avg, cur, qty, fee_rate=self.FEE, tax_rate=self.TAX)
            assert actual == expected, f"avg={avg} cur={cur} qty={qty}: expected={expected} actual={actual}"

    def test_calculate_pnl_zero_price_returns_zero(self):
        """Position.pnl과 동일하게 current_price/avg_price가 0이면 0 반환"""
        assert PnLTracker.calculate_pnl(0, 100_000, 10) == 0
        assert PnLTracker.calculate_pnl(100_000, 0, 10) == 0
