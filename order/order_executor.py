"""Order executor — Kiwoom SendOrder API 전담"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from order.order_types import OrderType, PriceType

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class OrderExecutor:
    """
    Kiwoom SendOrder 전담 실행자.

    순수 API 호출만 담당하며, 상태 관리는 OrderManager에 위임.
    """

    def __init__(self, kiwoom, account: str, max_order_amount: int):
        """
        Args:
            kiwoom: KiwoomManager 인스턴스
            account: 계좌번호
            max_order_amount: 최대 주문금액 (Kiwoom API 제한값)
        """
        self._kiwoom = kiwoom
        self._account = account
        self._max_order_amount = max_order_amount

    def set_account(self, account: str):
        """계좌번호 동적 업데이트 (로그인 후 호출)"""
        self._account = account
        logger.info("[OrderExecutor] 계좌번호 업데이트: %s", account)

    def send(
        self,
        order_type: int,
        code: str,
        name: str,
        qty: int,
        price: int = 0,
        price_type: str = "",
    ) -> tuple[int, str]:
        """
        Kiwoom SendOrder 호출.

        Args:
            order_type: OrderType.BUY (1) / OrderType.SELL (2)
            code: 종목코드
            name: 종목명
            qty: 수량
            price: 지정가 (0이면 시장가)
            price_type: "00"(지정가), "03"(시장가) 등. 비어있으면 price에 따라 자동 결정.

        Returns:
            (ret: int, rq_name: str)
        """
        # price_type 이 명시되지 않은 경우 자동 결정 (하위 호환)
        if not price_type:
            price_type = PriceType.MARKET if price == 0 else PriceType.LIMIT
            
        rq_name = f"{'매수' if order_type == OrderType.BUY else '매도'}_{code}"

        ret = self._kiwoom._ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rq_name, "1001", self._account,
             order_type, code, qty, price, price_type, ""],
        )

        return ret, rq_name
