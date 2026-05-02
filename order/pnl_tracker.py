"""
PnLTracker — 손익 계산 통합

현재 4개 파일(kiwoom_api.py, order_manager.py, trade_audit_logger.py, feedback_engine.py)에
흩어진 손익 계산을 단일 모듈로 통합.

수수료 및 세금은 config.py의 COST 에서 읽음.
"""
from __future__ import annotations

from config import COST as _COST

_FEE = _COST.get("fee_rate", 0.00015)
_TAX = _COST.get("tax_rate", 0.0023)


class PnLTracker:
    """손익 계산 통합 클래스"""

    @staticmethod
    def calculate_pnl(
        avg_price: int,
        current_price: int,
        qty: int,
        fee_rate: float = _FEE,
        tax_rate: float = _TAX,
    ) -> int:
        """
        평가손익 계산 (수수료·세금 차감 후).

        Args:
            avg_price: 평균 매입가
            current_price: 현재가
            qty: 수량
            fee_rate: 수수료율 (기본값 config에서 읽음)
            tax_rate: 세율 (기본값 config에서 읽음)

        Returns:
            평가손익 (원)
        """
        gross = (current_price - avg_price) * qty
        buy_fee = int(avg_price * qty * fee_rate)
        sell_fee = int(current_price * qty * (fee_rate + tax_rate))
        return gross - buy_fee - sell_fee

    @staticmethod
    def calculate_return_pct(
        avg_price: int,
        current_price: int,
        qty: int,
        fee_rate: float = _FEE,
        tax_rate: float = _TAX,
    ) -> float:
        """
        수익률 계산 (수수료·세금 차감 후, %).

        Args:
            avg_price: 평균 매입가
            current_price: 현재가
            qty: 수량
            fee_rate: 수수료율
            tax_rate: 세율

        Returns:
            수익률 (%)
        """
        cost = avg_price * qty
        if not cost:
            return 0.0
        pnl = PnLTracker.calculate_pnl(avg_price, current_price, qty, fee_rate, tax_rate)
        return pnl / cost * 100.0

    @staticmethod
    def calculate_pure_change_pct(avg_price: int, current_price: int) -> float:
        """
        순수 등락률 계산 (수수료·세금 미반영, %).

        Args:
            avg_price: 평균 매입가
            current_price: 현재가

        Returns:
            등락률 (%)
        """
        if avg_price <= 0:
            return 0.0
        return (current_price - avg_price) / avg_price * 100.0

    @staticmethod
    def calculate_unrealized_cost_minus_value(
        avg_price: int, current_price: int, qty: int
    ) -> int:
        """
        매입금액 - 평가금액 (UI 손익 열).

        Args:
            avg_price: 평균 매입가
            current_price: 현재가
            qty: 수량

        Returns:
            매입금액 - 평가금액 (원)
        """
        return avg_price * qty - current_price * qty

    @staticmethod
    def calculate_realized_pnl(
        entry_price: int,
        exit_price: int,
        qty: int,
        fee_rate: float = _FEE,
        tax_rate: float = _TAX,
    ) -> int:
        """
        실현손익 계산 (체결된 주문의 손익).

        Args:
            entry_price: 진입가 (평균 매입가)
            exit_price: 출현가 (평균 매도가)
            qty: 수량
            fee_rate: 수수료율
            tax_rate: 세율

        Returns:
            실현손익 (원)
        """
        gross = (exit_price - entry_price) * qty
        buy_fee = int(entry_price * qty * fee_rate)
        sell_fee = int(exit_price * qty * (fee_rate + tax_rate))
        return gross - buy_fee - sell_fee
