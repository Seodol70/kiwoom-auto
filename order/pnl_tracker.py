"""
PnLTracker — 손익 계산 통합

현재 4개 파일(kiwoom_api.py, order_manager.py, trade_audit_logger.py, feedback_engine.py)에
흩어진 손익 계산을 단일 모듈로 통합.

수수료 및 세금은 ConfigManager 를 통해 동적으로 가져옴.

TODO(리팩토링 2단계): order_manager.py가 여전히 _FEE/_TAX를 직접 계산하고 있어
(order_manager.py:34-35, 140-141, 1887, 1913) 이 클래스는 아직 미채택 상태다.
계산식이 수치적으로 100% 동일함을 단위테스트로 먼저 대조한 뒤 호출부를 교체할 것.
"""
from __future__ import annotations
from app.config_manager import config_manager as cfg

class PnLTracker:
    """손익 계산 통합 클래스"""

    @staticmethod
    def _get_rates(fee_rate: float | None, tax_rate: float | None) -> tuple[float, float]:
        """최신 수수료/세율을 가져온다."""
        if fee_rate is None:
            fee_rate = cfg.COST.get("fee_rate", 0.00015)
        if tax_rate is None:
            tax_rate = cfg.COST.get("tax_rate", 0.0023)
        return fee_rate, tax_rate

    @staticmethod
    def calculate_pnl(
        avg_price: int,
        current_price: int,
        qty: int,
        fee_rate: float | None = None,
        tax_rate: float | None = None,
    ) -> int:
        """
        평가손익 계산 (수수료·세금 차감 후).
        """
        fr, tr = PnLTracker._get_rates(fee_rate, tax_rate)
        gross = (current_price - avg_price) * qty
        buy_fee = int(avg_price * qty * fr)
        sell_fee = int(current_price * qty * (fr + tr))
        return gross - buy_fee - sell_fee

    @staticmethod
    def calculate_return_pct(
        avg_price: int,
        current_price: int,
        qty: int,
        fee_rate: float | None = None,
        tax_rate: float | None = None,
    ) -> float:
        """
        수익률 계산 (수수료·세금 차감 후, %).
        """
        cost = avg_price * qty
        if not cost:
            return 0.0
        pnl = PnLTracker.calculate_pnl(avg_price, current_price, qty, fee_rate, tax_rate)
        return pnl / cost * 100.0

    @staticmethod
    def calculate_pure_change_pct(avg_price: int, current_price: int) -> float:
        """순수 등락률 계산 (수수료·세금 미반영, %)."""
        if avg_price <= 0:
            return 0.0
        return (current_price - avg_price) / avg_price * 100.0

    @staticmethod
    def calculate_unrealized_cost_minus_value(
        avg_price: int,
        current_price: int,
        qty: int,
    ) -> int:
        """미실현 원가-평가 차이 (양수=손실, 음수=이익)."""
        return (avg_price - current_price) * qty

    @staticmethod
    def calculate_realized_pnl(
        entry_price: int,
        exit_price: int,
        qty: int,
        fee_rate: float | None = None,
        tax_rate: float | None = None,
    ) -> int:
        """실현손익 계산 (체결된 주문의 손익)."""
        fr, tr = PnLTracker._get_rates(fee_rate, tax_rate)
        gross = (exit_price - entry_price) * qty
        buy_fee = int(entry_price * qty * fr)
        sell_fee = int(exit_price * qty * (fr + tr))
        return gross - buy_fee - sell_fee
