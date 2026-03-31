"""
데이터 소스 추상화 레이어

USE_LIVE = False  →  Mock 데이터 (키움 API 없이 개발/테스트)
USE_LIVE = True   →  실제 KiwoomManager 호출

app.py 는 DataProvider 만 바라보므로
실제 API 연동 시 이 파일만 수정하면 됩니다.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

# 실제 연동 시 True 로 변경
USE_LIVE = False

# ---------------------------------------------------------------------------
# Mock 데이터
# ---------------------------------------------------------------------------

_MOCK_STOCKS = {
    "005930": {"name": "삼성전자",   "base": 74_000},
    "000660": {"name": "SK하이닉스", "base": 183_000},
    "035720": {"name": "카카오",     "base": 46_500},
    "035420": {"name": "NAVER",      "base": 172_000},
    "051910": {"name": "LG화학",     "base": 320_000},
}


def _rand(seed: int | None = None):
    r = random.Random(seed)
    return r


def mock_balance() -> dict:
    r = _rand()
    pnl = r.randint(-500_000, 800_000)
    total = 10_000_000 + pnl
    return {
        "cash":        r.randint(2_000_000, 5_000_000),
        "stock_value": total - r.randint(2_000_000, 5_000_000),
        "total":       total,
        "pnl":         pnl,
        "pnl_pct":     round(pnl / 10_000_000 * 100, 2),
    }


def mock_holdings() -> list[dict]:
    rows = []
    for code, info in list(_MOCK_STOCKS.items())[:3]:
        r = _rand(int(code))
        avg   = info["base"]
        curr  = int(avg * r.uniform(0.97, 1.04))
        qty   = r.randint(5, 20)
        pnl   = (curr - avg) * qty
        rows.append({
            "종목코드": code,
            "종목명":   info["name"],
            "보유수량": qty,
            "매입가":   avg,
            "현재가":   curr,
            "평가손익": pnl,
            "수익률(%)": round(pnl / (avg * qty) * 100, 2),
        })
    return rows


def mock_price_history(code: str, count: int = 80) -> pd.DataFrame:
    info  = _MOCK_STOCKS.get(code, {"base": 50_000})
    r     = _rand(int(code) % 1000)
    price = info["base"]
    base_t = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    rows  = []
    for i in range(count):
        o  = price + r.randint(-300, 300)
        c  = o + r.randint(-400, 400)
        h  = max(o, c) + r.randint(0, 150)
        lo = min(o, c) - r.randint(0, 150)
        rows.append({
            "time":   (base_t + timedelta(minutes=i * 3)).strftime("%H:%M"),
            "open":  max(1, o),
            "high":  max(1, h),
            "low":   max(1, lo),
            "close": max(1, c),
            "volume": r.randint(50_000, 300_000),
        })
        price = c
    return pd.DataFrame(rows)


def mock_new_signal(code: str) -> dict:
    """현재 시각 기준 새 신호 1건 생성"""
    info  = _MOCK_STOCKS.get(code, {"name": code, "base": 50_000})
    r     = random.Random()
    price = int(info["base"] * r.uniform(0.97, 1.03))
    ma5   = int(price * r.uniform(0.998, 1.002))
    ma20  = int(price * r.uniform(0.995, 1.005))
    rsi   = round(r.uniform(25, 75), 1)

    # 간단한 신호 로직
    if ma5 > ma20 and rsi < 65:
        signal = "BUY"
    elif ma5 < ma20 or rsi > 70:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {
        "시각":   datetime.now().strftime("%H:%M:%S"),
        "종목코드": code,
        "종목명":  info["name"],
        "신호":    signal,
        "가격":    price,
        "MA5":    ma5,
        "MA20":   ma20,
        "RSI":    rsi,
    }


# ---------------------------------------------------------------------------
# DataProvider — app.py 가 직접 호출하는 인터페이스
# ---------------------------------------------------------------------------

class DataProvider:
    """
    Mock 모드: USE_LIVE = False
    Live 모드: USE_LIVE = True → KiwoomManager 인스턴스 필요
    """

    def __init__(self, kiwoom=None) -> None:
        self._kiwoom = kiwoom
        self._live   = USE_LIVE and kiwoom is not None

    # ── 계좌 ────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        if self._live:
            return self._kiwoom.get_balance()
        return mock_balance()

    # ── 보유 종목 ────────────────────────────────────────────────────────

    def get_holdings(self) -> pd.DataFrame:
        if self._live:
            rows = self._kiwoom.get_holdings()
            return pd.DataFrame(rows).rename(columns={
                "code": "종목코드", "name": "종목명",
                "qty": "보유수량", "avg_price": "매입가",
                "current_price": "현재가", "pnl": "평가손익",
                "pnl_pct": "수익률(%)",
            })
        return pd.DataFrame(mock_holdings())

    # ── 가격 (분봉) ──────────────────────────────────────────────────────

    def get_price_history(self, code: str, count: int = 80) -> pd.DataFrame:
        if self._live:
            candles = self._kiwoom.get_min_candles(code, tick_unit=3, count=count)
            return pd.DataFrame(candles).rename(columns={
                "time": "time", "open": "open", "high": "high",
                "low": "low", "close": "close", "volume": "volume",
            })
        return mock_price_history(code, count)

    # ── 신호 (단건) ──────────────────────────────────────────────────────

    def get_new_signal(self, code: str) -> dict:
        """갱신 주기마다 호출 → 신호 1건 반환"""
        if self._live:
            # TODO: strategy 실행 결과를 반환하도록 연동
            raise NotImplementedError
        return mock_new_signal(code)

    # ── 종목 목록 ────────────────────────────────────────────────────────

    def get_stock_list(self) -> dict[str, str]:
        """code → name 매핑 반환"""
        if self._live:
            codes = self._kiwoom.get_kospi_codes()[:20]
            return {c: self._kiwoom.get_stock_name(c) for c in codes}
        return {c: v["name"] for c, v in _MOCK_STOCKS.items()}
