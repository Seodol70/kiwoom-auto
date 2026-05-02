"""
test_order_executor.py — OrderExecutor SendOrder 책임 분리 테스트

MockKiwoomGateway를 사용하여 Kiwoom OCX 없이 테스트.
"""

import pytest
from unittest.mock import MagicMock

from order.order_executor import OrderExecutor
from order.order_types import OrderType, PriceType


class TestOrderExecutor:
    """OrderExecutor 테스트"""

    def test_send_buy_market_order_success(self):
        """시장가 매수 주문 성공"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=0)  # ret=0 (성공)

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        ret, rq_name = executor.send(
            order_type=OrderType.BUY,
            code="005930",
            name="삼성전자",
            qty=10,
            price=0,  # 시장가
        )

        assert ret == 0
        assert rq_name == "매수_005930"
        mock_kiwoom._ocx.dynamicCall.assert_called_once()

    def test_send_sell_limit_order_success(self):
        """지정가 매도 주문 성공"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=0)

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        ret, rq_name = executor.send(
            order_type=OrderType.SELL,
            code="005930",
            name="삼성전자",
            qty=10,
            price=80_000,  # 지정가
        )

        assert ret == 0
        assert rq_name == "매도_005930"

    def test_send_order_failure(self):
        """주문 실패 (ret != 0)"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=-100)  # 실패

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        ret, rq_name = executor.send(
            order_type=OrderType.BUY,
            code="005930",
            name="삼성전자",
            qty=10,
            price=0,
        )

        assert ret == -100
        assert rq_name == "매수_005930"

    def test_market_price_type_zero(self):
        """price=0 시 시장가 호가 구분"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=0)

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        executor.send(OrderType.BUY, "005930", "삼성전자", 10, price=0)

        # dynamicCall의 인자 확인
        call_args = mock_kiwoom._ocx.dynamicCall.call_args
        args_list = call_args[0][1]  # 두 번째 인자 리스트
        price_type = args_list[7]  # price_type은 8번째 인자
        assert price_type == PriceType.MARKET

    def test_limit_price_type_nonzero(self):
        """price!=0 시 지정가 호가 구분"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=0)

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        executor.send(OrderType.SELL, "005930", "삼성전자", 10, price=80_000)

        # dynamicCall의 인자 확인
        call_args = mock_kiwoom._ocx.dynamicCall.call_args
        args_list = call_args[0][1]
        price_type = args_list[7]
        assert price_type == PriceType.LIMIT

    def test_rq_name_format_buy(self):
        """매수 rq_name 형식: '매수_코드'"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=0)

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        ret, rq_name = executor.send(OrderType.BUY, "000660", "SK하이닉스", 5, 0)

        assert rq_name == "매수_000660"

    def test_rq_name_format_sell(self):
        """매도 rq_name 형식: '매도_코드'"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=0)

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        ret, rq_name = executor.send(OrderType.SELL, "068270", "셀트리온", 3, 200_000)

        assert rq_name == "매도_068270"

    def test_send_order_api_call_args(self):
        """SendOrder API 호출 인자 검증"""
        mock_kiwoom = MagicMock()
        mock_kiwoom._ocx = MagicMock()
        mock_kiwoom._ocx.dynamicCall = MagicMock(return_value=0)

        executor = OrderExecutor(mock_kiwoom, "1234567890", 1_500_000)
        executor.send(
            order_type=OrderType.BUY,
            code="005930",
            name="삼성전자",
            qty=10,
            price=85_000,
        )

        # API 호출 확인
        call_args = mock_kiwoom._ocx.dynamicCall.call_args
        api_sig = call_args[0][0]  # API 시그니처
        args_list = call_args[0][1]  # 인자 리스트

        assert "SendOrder" in api_sig
        assert args_list[1] == "1001"  # 스크린 번호
        assert args_list[2] == "1234567890"  # 계좌번호
        assert args_list[3] == OrderType.BUY  # 주문 구분
        assert args_list[4] == "005930"  # 종목 코드
        assert args_list[5] == 10  # 수량
        assert args_list[6] == 85_000  # 가격
