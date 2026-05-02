"""
KiwoomProtocol — Kiwoom Gateway 공통 인터페이스

KiwoomManager(실제 OCX) 와 MockKiwoomGateway(테스트)가 모두 구현하는 ABC.
"""
from abc import ABC, abstractmethod
from typing import Optional, Callable


class KiwoomProtocol(ABC):
    """Kiwoom API의 공통 인터페이스 — OCX 추상화"""

    # ──────────────────────────────────────────────────────────────────────
    # 로그인 / 연결
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    def login(self) -> bool:
        """로그인. 성공 시 True."""
        pass

    @abstractmethod
    def get_login_state(self) -> int:
        """로그인 상태 (0=미연결, 1=연결)."""
        pass

    @abstractmethod
    def auto_reconnect(self) -> bool:
        """강제 재로그인 시도."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """연결 여부 boolean."""
        pass

    @abstractmethod
    def set_auto_login_callback(self, callback: Callable) -> None:
        """재로그인 시 호출될 콜백 등록."""
        pass

    # ──────────────────────────────────────────────────────────────────────
    # 주식 주문
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    def send_order(
        self,
        code: str,
        order_type: int,
        qty: int,
        price: int,
        price_type: str = "03",
    ) -> Optional[str]:
        """
        주문 발송.

        Args:
            code: 종목 코드 (6자리)
            order_type: 1=매수, 2=매도
            qty: 수량
            price: 가격 (0=시장가)
            price_type: "00"=지정가, "03"=시장가

        Returns:
            주문번호 (문자열), 실패 시 None
        """
        pass

    # ──────────────────────────────────────────────────────────────────────
    # 데이터 조회 (TR)
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    def get_stock_info(self, code: str) -> dict:
        """
        종목 기본정보 (opt10001).

        Returns:
            {
                'name': str,
                'sector': str,
                'current_price': int,
                'prev_close': int,
                'high_price': int,
                'low_price': int,
                ...
            }
        """
        pass

    @abstractmethod
    def get_current_price(self, code: str) -> int:
        """현재가 (메모리 캐시에서 빠른 조회)."""
        pass

    @abstractmethod
    def fetch_opt10030_top_volume(self, top_n: int = 200) -> list[dict]:
        """
        거래대금 상위 종목 (opt10030 연속조회).

        Returns:
            [
                {
                    'code': str,
                    'name': str,
                    'current_price': int,
                    'trade_amount': int,
                    'volume': int,
                    ...
                },
                ...
            ]
        """
        pass

    @abstractmethod
    def get_balance(self) -> dict:
        """
        예수금/평가금액 (opw00001).

        Returns:
            {
                'cash': int,
                'stock_value': int,
                'total': int,
                'pnl': int,
                'pnl_pct': float,
            }
        """
        pass

    @abstractmethod
    def get_holdings(self) -> list[dict]:
        """
        보유 종목 목록 (opw00018).

        Returns:
            [
                {
                    'code': str,
                    'name': str,
                    'qty': int,
                    'avg_price': int,
                    'current_price': int,
                    'pnl': int,
                    'pnl_pct': float,
                },
                ...
            ]
        """
        pass

    @abstractmethod
    def get_daily_candles(self, code: str, count: int = 60) -> list[dict]:
        """
        일봉 데이터 (opt10081).

        Returns:
            [
                {
                    'date': str (YYYYMMDD),
                    'open': int,
                    'high': int,
                    'low': int,
                    'close': int,
                    'volume': int,
                },
                ...
            ]
        """
        pass

    @abstractmethod
    def get_min_candles(self, code: str, count: int = 60) -> list[dict]:
        """
        분봉 데이터 (opt10080).

        Returns:
            [
                {
                    'time': str (HHmmss),
                    'open': int,
                    'high': int,
                    'low': int,
                    'close': int,
                    'volume': int,
                },
                ...
            ]
        """
        pass

    @abstractmethod
    def get_today_realized_pnl(self) -> int:
        """당일 실현손익 (opt10074)."""
        pass

    @abstractmethod
    def get_investor_trend(self, code: str) -> dict:
        """
        외국인/기관 순매수 (opt10059).

        Returns:
            {
                'foreign_net_buy': int,
                'inst_net_buy': int,
            }
        """
        pass

    @abstractmethod
    def get_index_info(self, code: str) -> dict:
        """
        지수 정보 (opt20001).

        Returns:
            {
                'name': str,
                'current': float,
                'change': float,
                'change_pct': float,
            }
        """
        pass

    @abstractmethod
    def get_kospi_codes(self) -> list[str]:
        """코스피 전종목 코드."""
        pass

    @abstractmethod
    def get_kosdaq_codes(self) -> list[str]:
        """코스닥 전종목 코드."""
        pass

    @abstractmethod
    def get_stock_name(self, code: str) -> str:
        """종목명 조회."""
        pass

    @abstractmethod
    def force_unfreeze(self) -> None:
        """Watchdog이 프리징 감지 시 강제 이벤트 루프 해제."""
        pass
