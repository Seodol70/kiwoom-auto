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
MIN_TRADE_AMT: int = 5_000_000_000   # 50억 원
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


def _is_ordinary_stock(code: str) -> bool:
    """
    보통주만 True. 우선주·ETF·ETN 은 False.

    키움 코드 규칙:
      - 6자리 숫자
      - 끝자리 0: 보통주 (삼성전자 005930)
      - 끝자리 5: 보통주 일부 (카카오 035720)
      - 끝자리 0 외: 우선주 (삼성전자우 005935 → 끝 5는 예외적으로 허용)
      - "1"로 시작: ETF (KODEX200 069500 → 실제론 069로 시작하므로 코드 앞 검토)
    실용적 규칙: 끝자리가 0 또는 5 인 6자리 숫자만 허용
    """
    if len(code) != 6 or not code.isdigit():
        return False
    return code[-1] in ("0", "5")
