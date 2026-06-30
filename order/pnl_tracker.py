"""
PnLTracker — 손익 계산 통합

order/order_manager.py에 흩어져 있던 수수료·세금 포함 손익 계산 3곳을 통합한다.
(kiwoom_api.py/trade_audit_logger.py/feedback_engine.py는 자체 계산 없이 이미 계산된
값을 그대로 읽거나 키움 서버 응답을 그대로 전달할 뿐이라 통합 대상이 아니었음 —
리팩토링 2단계 2026-06-30 조사로 확인.)

수수료 및 세금은 ConfigManager 를 통해 동적으로 가져옴.

[리팩토링 2단계 2026-06-30] calculate_pnl()/calculate_realized_pnl()/calculate_buy_fee()의
int() 캐스팅 위치를 order_manager.py의 기존 계산식과 정확히 일치시켰다(무작위 20만건 대조로
검증, 변경 전 75~88%에서 1~2원 차이가 나던 것을 0건으로 맞춤). 자세한 매핑:
- calculate_pnl()           <- Position.pnl 프로퍼티 (미실현, 원래 order_manager.py:133-142)
- calculate_realized_pnl()  <- OrderManager._handle_sell_fill() (실현, 원래 order_manager.py:1909-1914)
- calculate_buy_fee()       <- OrderManager._handle_buy_fill() (매수 수수료, 원래 order_manager.py:1882)
order_manager.py는 현재 위 세 곳 모두 이 클래스를 호출하도록 교체 완료됨.
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
        평가손익 계산 (수수료·세금 차감 후, Position.pnl과 동일 식 — 마지막에 한 번만 int() 적용).
        """
        if not current_price or not avg_price:
            return 0
        fr, tr = PnLTracker._get_rates(fee_rate, tax_rate)
        buy_total = avg_price * qty
        sell_total = current_price * qty
        fees = buy_total * fr + sell_total * fr
        tax = sell_total * tr
        return int(sell_total - buy_total - fees - tax)

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
    def calculate_buy_fee(
        price: int,
        qty: int,
        fee_rate: float | None = None,
    ) -> int:
        """매수 체결 시 수수료만 계산 (세금 없음, cash 차감용 — OrderManager._handle_buy_fill)."""
        fr, _ = PnLTracker._get_rates(fee_rate, None)
        return int(price * qty * fr)

    @staticmethod
    def calculate_realized_pnl(
        entry_price: int,
        exit_price: int,
        qty: int,
        fee_rate: float | None = None,
        tax_rate: float | None = None,
    ) -> int:
        """실현손익 계산 (체결된 주문의 손익, _handle_sell_fill과 동일 식 —
        cost를 round()로 한 번에 계산 후 차감)."""
        fr, tr = PnLTracker._get_rates(fee_rate, tax_rate)
        sell_amount = exit_price * qty
        buy_amount = entry_price * qty
        cost = round(sell_amount * (fr + tr) + buy_amount * fr)
        return (exit_price - entry_price) * qty - cost
