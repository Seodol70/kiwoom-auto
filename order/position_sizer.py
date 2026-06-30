"""
position_sizer.py — 진입 수량 계산(Position Sizing) 전략

OrderManager.handle_signal()의 수량 계산 3모드(FIXED/RISK/EQUAL, 원래
order_manager.py:846-882)를 Strategy 패턴으로 추출했다. scanner/smart_scanner.py의
strategy_map과 동일한 관용구(이름 -> 객체 매핑, mode 문자열로 조회)를 사용한다.

각 Sizer는 동일한 계산식을 그대로 옮긴 것이며, 로직을 단 한 줄도 바꾸지 않았다
(tests/test_position_sizer.py와 tests/test_handle_signal_characterization.py의
사이징 테스트가 이를 보증한다).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from logging_config import order_log


class PositionSizer(ABC):
    """진입 수량 계산 전략의 공통 인터페이스."""

    @abstractmethod
    def calculate(self, price: int, scan_cfg: Any, order_mgr: Any) -> int:
        """주어진 진입가에 대해 1차 매수 수량을 계산한다(주문한도/예수금 추가조정 전).

        Args:
            price: 진입 가격(시장가 정렬 후)
            scan_cfg: SmartScannerConfig — 모드별 파라미터 조회용
            order_mgr: OrderManager — cash/positions/total_equity 등 상태 조회용

        Returns:
            계산된 수량(주). price<=0이면 항상 0.
        """
        raise NotImplementedError


class FixedSizer(PositionSizer):
    """FIXED: 설정된 고정 금액으로 분할 매수 (원래 order_manager.py:849-853)."""

    def calculate(self, price: int, scan_cfg: Any, order_mgr: Any) -> int:
        budget = int(getattr(scan_cfg, "fixed_order_amount", 1_500_000))
        qty = budget // price if price > 0 else 0
        order_log.info("[사이징:FIXED] 목표금액=%s원 -> %d주", f"{budget:,}", qty)
        return qty


class RiskSizer(PositionSizer):
    """RISK: 회당 리스크 한도(예: 총자산 1%) 기반 수량 산출 (원래 order_manager.py:855-871).

    공식: qty = (총자산 * 리스크%) / (진입가 - 손절가)
    """

    def calculate(self, price: int, scan_cfg: Any, order_mgr: Any) -> int:
        if price <= 0:
            return 0
        risk_pct = float(getattr(scan_cfg, "risk_per_trade_pct", 1.0))
        total_equity = order_mgr.total_equity
        risk_amount = int(total_equity * (risk_pct / 100.0))

        sl_pct = abs(float(getattr(scan_cfg, "jdm_stop_loss_pct", -1.2)))
        stop_price = int(price * (1 - sl_pct / 100.0))
        risk_per_share = max(1, price - stop_price)

        qty = risk_amount // risk_per_share
        order_log.info(
            "[사이징:RISK] 총자산=%s원 리스크=%s원(%s%%) 손절가=%s원 -> %d주",
            f"{total_equity:,}", f"{risk_amount:,}", f"{risk_pct}", f"{stop_price:,}", qty
        )
        return qty


class EqualSizer(PositionSizer):
    """EQUAL(기본값): 가용 예수금을 남은 슬롯 수로 균등 분배 (원래 order_manager.py:873-882).

    [BUG FIX 2026-05-26] available_cash(미체결 매수 차감 반영) 사용.
    remaining_slots에서 _pending(매수+매도 주문 대기)도 빼므로 슬롯 측에서도 중복 방지.
    """

    def calculate(self, price: int, scan_cfg: Any, order_mgr: Any) -> int:
        cash_avail = order_mgr.available_cash
        remaining_slots = order_mgr.max_positions - len(order_mgr.positions) - len(order_mgr._pending)
        remaining_slots = max(remaining_slots, 1)
        budget = cash_avail // remaining_slots
        qty = budget // price if price > 0 else 0
        order_log.info("[사이징:EQUAL] 가용예수금=%s원 슬롯=%d -> %d주", f"{cash_avail:,}", remaining_slots, qty)
        return qty


# scanner/smart_scanner.py의 strategy_map과 동일한 관용구 — mode 문자열로 조회
POSITION_SIZERS: dict[str, PositionSizer] = {
    "FIXED": FixedSizer(),
    "RISK": RiskSizer(),
    "EQUAL": EqualSizer(),
}


def get_position_sizer(mode: str) -> PositionSizer:
    """mode 문자열(대소문자 무관)에 해당하는 Sizer를 반환한다.
    알 수 없는 mode는 EQUAL로 폴백한다(원래 handle_signal()의 if/elif/else 구조와 동일하게
    FIXED/RISK가 아니면 항상 else=EQUAL이었음)."""
    return POSITION_SIZERS.get(mode.upper(), POSITION_SIZERS["EQUAL"])
