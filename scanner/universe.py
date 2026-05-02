"""
유니버스 필터 — 전 종목에서 매매 불가·비적격 종목을 제거한다.

get_filtered_universe() 가 반환하는 set[str] 이
condition_search.py 와 scanner_main.py 의 공통 기반 풀이 된다.

제거 기준:
  ① 관리종목  (GetMasterStockState 에 "관리" 포함)
  ② 거래정지  (GetMasterStockState 에 "정지" 포함)
  ③ 우선주     (종목코드 끝자리 0·5 외)
  ④ ETF·ETN   (코드 앞자리 "1"로 시작하는 6자리)
  ⑤ 평균 거래대금 < MIN_TRADE_AMT (기본 50억)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 유니버스 기본 파라미터
MIN_TRADE_AMT: int = 10_000_000_000   # 100억 원
TR_DELAY:      float = 0.22          # 키움 TR 딜레이 정책


def get_filtered_universe(
    kiwoom,
    markets:       tuple[str, ...] = ("0", "10"),   # "0": 코스피, "10": 코스닥
    min_trade_amt: int = MIN_TRADE_AMT,
) -> set[str]:
    """
    키움 API로 전 종목을 조회하고 비적격 종목을 제거한 유니버스를 반환한다.

    Args:
        kiwoom:        KiwoomManager 인스턴스
        markets:       조회할 시장 목록
        min_trade_amt: 최소 평균 거래대금 (원)

    Returns:
        유효 종목코드 set  예) {"005930", "000660", ...}
    """
    all_codes: list[str] = []
    for market_id in markets:
        codes = _fetch_codes(kiwoom, market_id)
        logger.info("시장 %s → %d 종목", market_id, len(codes))
        all_codes.extend(codes)

    universe: set[str] = set()
    excluded: dict[str, int] = {
        "우선주/ETF":   0,
        "관리종목":     0,
        "거래정지":     0,
        "투자경고":     0,
        "스팩":         0,
        "거래대금미달": 0,
    }

    # 종목명 기반 차단 키워드
    _NAME_BLOCK = ("스팩", "SPAC", "ETF", "ETN", "레버리지", "인버스", "곱버스")

    for code in all_codes:
        # ① 우선주·ETF 제외 (코드 끝자리 0·5 외)
        if not _is_ordinary_stock(code):
            excluded["우선주/ETF"] += 1
            continue

        # ② 관리종목·거래정지·투자경고 제외
        state = _fetch_stock_state(kiwoom, code)
        if "관리" in state:
            excluded["관리종목"] += 1
            continue
        if "정지" in state:
            excluded["거래정지"] += 1
            continue
        if any(w in state for w in ("투자경고", "투자위험", "투자주의")):
            excluded["투자경고"] += 1
            continue

        # ③ 거래대금 기준 + 종목명 키워드 차단
        try:
            info = kiwoom.get_stock_info(code)
            name = info.get("name", "")

            # 종목명 기반 차단 (스팩/ETF/ETN/레버리지 등)
            name_upper = name.upper()
            if any(kw.upper() in name_upper for kw in _NAME_BLOCK):
                key = "스팩" if "스팩" in name or "SPAC" in name_upper else "우선주/ETF"
                excluded[key] += 1
                continue

            trade_amt = info["current_price"] * info["volume"]
            if trade_amt < min_trade_amt:
                excluded["거래대금미달"] += 1
                continue
        except Exception as e:
            logger.warning("종목 정보 조회 실패 — %s: %s", code, e)
            continue

        universe.add(code)

    logger.info(
        "유니버스 구성 완료 — 전체 %d → 통과 %d | 제외: %s",
        len(all_codes), len(universe),
        ", ".join(f"{k} {v}건" for k, v in excluded.items() if v),
    )
    return universe


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _fetch_codes(kiwoom, market_id: str) -> list[str]:
    """GetCodeListByMarket 으로 시장별 전 종목코드를 가져온다."""
    raw: str = kiwoom._ocx.dynamicCall(
        "GetCodeListByMarket(QString)", [market_id]
    )
    return [c for c in raw.strip().split(";") if c]


def _fetch_stock_state(kiwoom, code: str) -> str:
    """
    GetMasterStockState 로 종목 상태 문자열을 반환한다.
    예) "" | "관리종목" | "거래정지" | "투자경고"
    """
    return kiwoom._ocx.dynamicCall(
        "GetMasterStockState(QString)", [code]
    ).strip()


def is_pure_equity_name(name: str) -> bool:
    """
    ETF·ETN·인버스·레버리지·스팩 및 국내 ETF 브랜드명이 들어가면 False.
    순수 주식 종목만 필터링하기 위해 사용한다.
    """
    if not name or not str(name).strip():
        return False
    n = str(name).strip()
    upper = n.upper()

    exclude_kw = (
        "ETF", "ETN", "인버스", "레버리지", "곱버스", "역추적",
        "2X", "3X", "5X", "10X", "스팩", "SPAC", "헷지", "HEDGE",
        "선물", "옵션", "수익증권", "구조", "파생",
        "KODEX", "TIGER", "KBSTAR", "HANAR", "KOSEF", "ARIRANG",
        "TIMEFOLIO", "KINDEX", "ACE", "RISE", "SOL", "FOCUS",
    )
    for kw in exclude_kw:
        if kw in n or kw in upper:
            return False

    return True


def filter_equity_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """리스트 형태의 종목 데이터에서 비적격 종목(우선주, ETF 등)을 일괄 제거한다."""
    out: list[dict] = []
    dropped = 0
    for r in rows:
        code = str(r.get("code", "")).lstrip("A").strip()
        if not is_ordinary_stock(code):
            dropped += 1
            continue
        nm = r.get("name", "")
        if is_pure_equity_name(str(nm)):
            out.append(r)
        else:
            dropped += 1
    return out, dropped


def is_ordinary_stock(code: str) -> bool:
    """
    보통주만 True. 우선주·ETF·ETN 은 False.
    키움 규칙: 6자리 숫자이면서 끝자리가 0 또는 5인 경우만 보통주로 간주 (단순화).
    """
    if not code or len(code) != 6 or not code.isdigit():
        return False
    return code[-1] in ("0", "5")


def _is_ordinary_stock(code: str) -> bool:
    """(레거시 호환용)"""
    return is_ordinary_stock(code)
