"""MockKiwoomGateway — 단위 테스트용 Kiwoom API Mock 구현"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from infra.kiwoom_protocol import KiwoomProtocol

logger = logging.getLogger(__name__)


class MockKiwoomGateway(KiwoomProtocol):
    """KiwoomProtocol 구현체 — 테스트용 Mock."""

    def __init__(self):
        self._connected = True
        self._login_state = 1
        self._auto_login_callback: Optional[Callable] = None
        self._orders: list[dict] = []
        self._prices: dict[str, int] = {}
        self._stock_info: dict[str, dict] = {}
        self._balance: dict = {
            "cash": 10_000_000,
            "stock_value": 0,
            "total": 10_000_000,
            "pnl": 0,
            "pnl_pct": 0.0,
        }
        self._holdings: list[dict] = []

    # ────────────────────────────────────────────────────────────────────────
    # 로그인 / 연결
    # ────────────────────────────────────────────────────────────────────────

    def login(self) -> bool:
        """로그인 (테스트용: 항상 성공)."""
        self._connected = True
        self._login_state = 1
        return True

    def get_login_state(self) -> int:
        """로그인 상태."""
        return self._login_state

    def auto_reconnect(self) -> bool:
        """강제 재로그인 시도."""
        self._connected = True
        self._login_state = 1
        if self._auto_login_callback:
            self._auto_login_callback()
        return True

    def is_connected(self) -> bool:
        """연결 여부."""
        return self._connected

    def set_auto_login_callback(self, callback: Callable) -> None:
        """재로그인 시 콜백 등록."""
        self._auto_login_callback = callback

    # ────────────────────────────────────────────────────────────────────────
    # 주식 주문
    # ────────────────────────────────────────────────────────────────────────

    def send_order(
        self,
        code: str,
        order_type: int,
        qty: int,
        price: int,
        price_type: str = "03",
    ) -> Optional[str]:
        """
        주문 발송 (테스트용: 항상 성공, order_no 생성).

        Returns:
            주문번호 (문자열)
        """
        order_no = f"TEST_{len(self._orders):04d}"
        self._orders.append({
            "order_no": order_no,
            "code": code,
            "order_type": order_type,
            "qty": qty,
            "price": price,
            "price_type": price_type,
        })
        return order_no

    # ────────────────────────────────────────────────────────────────────────
    # 데이터 조회
    # ────────────────────────────────────────────────────────────────────────

    def get_stock_info(self, code: str) -> dict:
        """종목 기본정보 (테스트용)."""
        if code in self._stock_info:
            return self._stock_info[code]
        # 기본값
        return {
            "name": f"종목_{code}",
            "sector": "테크",
            "current_price": self._prices.get(code, 100_000),
            "prev_close": 100_000,
            "high_price": 105_000,
            "low_price": 95_000,
        }

    def get_current_price(self, code: str) -> int:
        """현재가 (테스트용)."""
        return self._prices.get(code, 100_000)

    def fetch_opt10030_top_volume(self, top_n: int = 200) -> list[dict]:
        """거래대금 상위 종목 (테스트용)."""
        return []

    def get_balance(self) -> dict:
        """예수금/평가금액 (테스트용)."""
        return self._balance.copy()

    def get_holdings(self) -> list[dict]:
        """보유 종목 목록 (테스트용)."""
        return self._holdings.copy()

    def get_daily_candles(self, code: str, count: int = 60) -> list[dict]:
        """일봉 데이터 (테스트용)."""
        return []

    def get_min_candles(self, code: str, count: int = 60) -> list[dict]:
        """분봉 데이터 (테스트용)."""
        return []

    def get_today_realized_pnl(self) -> int:
        """당일 실현손익 (테스트용)."""
        return 0

    def get_investor_trend(self, code: str) -> dict:
        """외국인/기관 순매수 (테스트용)."""
        return {"foreign_net_buy": 0, "inst_net_buy": 0}

    def get_index_info(self, code: str) -> dict:
        """지수 정보 (테스트용)."""
        return {
            "name": f"지수_{code}",
            "current": 3000.0,
            "change": 10.0,
            "change_pct": 0.33,
        }

    def get_kospi_codes(self) -> list[str]:
        """코스피 전종목 코드 (테스트용)."""
        return []

    def get_kosdaq_codes(self) -> list[str]:
        """코스닥 전종목 코드 (테스트용)."""
        return []

    def get_stock_name(self, code: str) -> str:
        """종목명 조회 (테스트용)."""
        info = self.get_stock_info(code)
        return info.get("name", "")

    def force_unfreeze(self) -> None:
        """Watchdog이 프리징 감지 시 강제 해제 (테스트용)."""
        pass

    # ────────────────────────────────────────────────────────────────────────
    # 테스트 헬퍼
    # ────────────────────────────────────────────────────────────────────────

    def set_price(self, code: str, price: int) -> None:
        """테스트용 가격 주입."""
        self._prices[code] = price

    def set_stock_info(self, code: str, info: dict) -> None:
        """테스트용 종목정보 주입."""
        self._stock_info[code] = info

    def set_balance(self, balance: dict) -> None:
        """테스트용 잔고 주입."""
        self._balance.update(balance)

    def set_holdings(self, holdings: list[dict]) -> None:
        """테스트용 보유종목 주입."""
        self._holdings = holdings

    def get_orders(self) -> list[dict]:
        """발송한 주문 목록 반환 (테스트용)."""
        return self._orders.copy()

    def reset(self) -> None:
        """테스트 상태 초기화."""
        self._orders.clear()
        self._prices.clear()
        self._stock_info.clear()
        self._holdings.clear()
        self._balance = {
            "cash": 10_000_000,
            "stock_value": 0,
            "total": 10_000_000,
            "pnl": 0,
            "pnl_pct": 0.0,
        }
