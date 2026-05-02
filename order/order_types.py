"""Order types — Kiwoom API 주문 타입 상수"""


class OrderType:
    """주문 구분"""
    BUY = 1
    SELL = 2
    BUY_CANCEL = 3
    SELL_CANCEL = 4


class PriceType:
    """호가 구분"""
    LIMIT = "00"  # 지정가
    MARKET = "03"  # 시장가
