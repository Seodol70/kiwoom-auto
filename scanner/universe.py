"""
유니버스 관리자 — 전 종목에서 매매 적격 종목을 필터링하고 스코어링하여 감시 대상을 선정한다.
"""

from __future__ import annotations
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class UniverseManager:
    """
    종목 필터링, 스코어링, 유니버스(Watch Pool) 구성을 전담하는 클래스.
    """
    def __init__(self, cfg: Optional[SmartScannerConfig] = None, skip_log: bool = False):
        self.cfg = cfg
        self._prev_volumes: dict[str, int] = {}
        self._load_prev_volumes(skip_log=skip_log)
        
    # ─── 전일 거래량 캐시 관리 ───────────────────────────────────────────

    def _prev_volumes_path(self) -> Path:
        return Path("logs") / "prev_volumes.json"

    def _load_prev_volumes(self, skip_log: bool = False) -> None:
        """logs/prev_volumes.json 에서 전일 거래량 캐시를 로드."""
        path = self._prev_volumes_path()
        if not path.exists():
            if not skip_log:
                logger.info("[Universe] 전일 거래량 캐시 없음 — vol_ratio 중립으로 동작")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            saved_date_str = data.get("date", "")
            saved_date = date.fromisoformat(saved_date_str) if saved_date_str else None
            today = date.today()
            
            if saved_date is None or saved_date >= today:
                return
            
            # 10일 이상 지난 데이터는 무시 (긴 연휴/공휴일 대응)
            if (today - saved_date).days > 10:
                if not skip_log:
                    logger.warning("[Universe] 전일 거래량 캐시가 너무 오래됨 (%d일 전)", (today - saved_date).days)
                return
                
            volumes = data.get("volumes", {})
            self._prev_volumes = {k: int(v) for k, v in volumes.items() if int(v or 0) > 0}
            if not skip_log:
                logger.info("[Universe] 전일 거래량 캐시 로드 완료: %d종목", len(self._prev_volumes))
        except Exception as e:
            if not skip_log:
                logger.warning("[Universe] 전일 거래량 로드 실패: %s", e)

    def save_prev_volumes(self, current_volumes: dict[str, int]) -> None:
        """현재 거래량을 전일 거래량으로 저장."""
        try:
            path = self._prev_volumes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "date": date.today().isoformat(),
                    "volumes": current_volumes
                }, f, ensure_ascii=False)
            self._prev_volumes = current_volumes
            logger.info("[Universe] 전일 거래량 캐시 저장 완료: %d종목", len(current_volumes))
        except Exception as e:
            logger.error("[Universe] 전일 거래량 저장 실패: %s", e)

    # ─── 필터링 및 스코어링 ──────────────────────────────────────────────

    def filter_equity_rows(self, rows: list[dict]) -> tuple[list[dict], int]:
        """비적격 종목(우선주, ETF, 스팩 등) 제거."""
        out: list[dict] = []
        dropped = 0
        for r in rows:
            code = str(r.get("code", "")).lstrip("A").strip()
            if not is_ordinary_stock(code):
                dropped += 1
                continue
            nm = r.get("name", "")
            if is_pure_equity_name(nm):
                out.append(r)
            else:
                dropped += 1
        return out, dropped

    @staticmethod
    def is_ordinary_stock(code: str) -> bool:
        """보통주 여부 확인 (6자리 숫자, 끝자리 0 또는 5)."""
        if not code or len(code) != 6 or not code.isdigit():
            return False
        return code[-1] in ("0", "5")

    @staticmethod
    def is_pure_equity_name(name: str) -> bool:
        """순수 주식 종목명 여부 확인 (ETF, 스팩 등 제외)."""
        if not name: return False
        n = str(name).strip().upper()
        exclude_kw = (
            "ETF", "ETN", "인버스", "레버리지", "곱버스", "역추적",
            "2X", "3X", "5X", "10X", "스팩", "SPAC", "HEDGE", "선물", "옵션",
            "KODEX", "TIGER", "KBSTAR", "HANAR", "KOSEF", "ARIRANG", "ACE", "RISE", "SOL"
        )
        return not any(kw in n for kw in exclude_kw)

    def apply_scoring_cap(self, rows: list[dict], limit: int) -> list[dict]:
        """거래대금, 페이스, 등락률을 조합한 스코어링으로 상위 종목 선정."""
        if not rows: return []
        
        n = len(rows)
        w_amt = getattr(self.cfg, "universe_trade_amt_weight", 0.4) if self.cfg else 0.4
        w_vol = getattr(self.cfg, "universe_vol_ratio_weight", 0.4) if self.cfg else 0.4
        w_chg = getattr(self.cfg, "universe_chg_pct_weight", 0.2) if self.cfg else 0.2
        
        # 거래대금 순위 스코어 (0.0 ~ 1.0)
        sorted_by_amt = sorted(rows, key=lambda r: int(r.get("trade_amount", 0) or 0), reverse=True)
        amt_rank = {r["code"]: 1.0 - (i / max(n - 1, 1)) for i, r in enumerate(sorted_by_amt)}
        
        scored = []
        for r in rows:
            code = r["code"]
            s_amt = amt_rank.get(code, 0.5)
            
            # 거래량 페이스 스코어
            today_vol = int(r.get("volume", 0) or 0)
            pv = self._prev_volumes.get(code, 0)
            pace = self._calculate_vol_pace(today_vol, pv)
            r["vol_ratio"] = round(pace, 4)
            s_vol = min(pace / 3.0, 1.0) if pace > 0 else 0.5
            
            # 등락률 스코어
            chg = float(r.get("change_pct", 0) or 0)
            s_chg = min(max(chg / 10.0, 0.0), 1.0)
            
            score = s_amt * w_amt + s_vol * w_vol + s_chg * w_chg
            scored.append((score, r))
            
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    def _calculate_vol_pace(self, today_vol: int, prev_volume: int) -> float:
        """시간대 보정 거래량 페이스 비율 계산."""
        if prev_volume <= 0 or today_vol <= 0: return 0.0
        
        now = datetime.now().time()
        now_min = now.hour * 60 + now.minute
        elapsed = max(now_min - (9 * 60), 5) # 장 시작(09:00) 후 경과 분 (최소 5분)
        elapsed = min(elapsed, 390)         # 장 마감(15:30)까지 총 390분
        
        elapsed_ratio = elapsed / 390
        return today_vol / (prev_volume * elapsed_ratio)

    @staticmethod
    def format_trade_amount(amount_won: int) -> str:
        """거래대금을 읽기 편한 한글 형식으로 변환."""
        n = int(amount_won or 0)
        if n <= 0: return "0원"
        
        if n >= 1_000_000_000_000:
            return f"{n / 1_000_000_000_000:.1f}조"
        if n >= 100_000_000:
            return f"{n // 100_000_000:,}억"
        if n >= 10_000:
            return f"{n // 10_000:,}만원"
        return f"{n:,}원"

# ─── 하위 호환용 글로벌 함수들 ──────────────────────────────────────────

def is_ordinary_stock(code: str) -> bool:
    """[레거시 호환] 보통주 여부 확인."""
    return UniverseManager.is_ordinary_stock(code)

def is_pure_equity_name(name: str) -> bool:
    """[레거시 호환] 순수 주식 명칭 여부 확인."""
    return UniverseManager.is_pure_equity_name(name)

def filter_equity_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """[레거시 호환] 종목 리스트 필터링."""
    # 필터링은 내부적으로 @staticmethod를 쓰도록 수정함
    out: list[dict] = []
    dropped = 0
    for r in rows:
        code = str(r.get("code", "")).lstrip("A").strip()
        if not is_ordinary_stock(code):
            dropped += 1
            continue
        nm = r.get("name", "")
        if is_pure_equity_name(nm):
            out.append(r)
        else:
            dropped += 1
    return out, dropped

def get_filtered_universe(rows: list[dict]) -> list[str]:
    """[레거시 호환] 필터링된 종목코드 리스트 반환."""
    filtered, _ = filter_equity_rows(rows)
    return [r["code"] for r in filtered]

def format_trade_amount_korean(amount_won: int) -> str:
    """[레거시 호환] 한글 거래대금 포맷팅."""
    return UniverseManager.format_trade_amount(amount_won)

def apply_watch_pool_cap(rows: list[dict], limit: int) -> list[dict]:
    """[레거시 호환] 거래대금 단일 정렬로 상위 N개 선정."""
    sorted_rows = sorted(rows, key=lambda r: int(r.get("trade_amount", 0) or 0), reverse=True)
    return sorted_rows[:limit]

def apply_universe_score_cap(rows: list[dict], limit: int, cfg: Optional[SmartScannerConfig] = None, prev_volumes: Optional[dict] = None) -> list[dict]:
    """[레거시 호환] 복합 스코어링으로 상위 N개 선정."""
    # 여기서는 스코어링을 위해 인스턴스가 필요하므로 skip_log=True 옵션 사용
    mgr = UniverseManager(cfg, skip_log=True)
    if prev_volumes:
        mgr._prev_volumes = prev_volumes
    return mgr.apply_scoring_cap(rows, limit)


_JO_WON = 1_000_000_000_000
_EOK_WON = 100_000_000


def format_trade_amount_growth(current: int, baseline: Optional[int]) -> str:
    """거래대금 증가율(%) — baseline 이 없거나 0이면 '—'."""
    if baseline is None or baseline <= 0:
        return "증가율(9시대비) —"
    pct = (current - baseline) / baseline * 100.0
    return (
        f"증가율(9시대비) {pct:+.1f}% "
        f"(기준 {format_trade_amount_korean(baseline)})"
    )


def seconds_until(t: dtime) -> float:
    """특정 시간(hour, minute, second)까지 남은 초를 계산한다."""
    now = datetime.now()
    target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


# ─── 호가 단위(Tick Size) 관련 유틸리티 (2023년 KRX 규정 기준) ───────────

def get_hoga_unit(price: int, market_type: str = "10") -> int:
    """
    현재 가격과 시장 구분(KOSPI/KOSDAQ)에 따른 호가 단위를 반환한다.
    "0": KOSPI, "10": KOSDAQ
    """
    p = abs(int(price))
    if p < 2000:
        return 1
    elif p < 5000:
        return 5
    elif p < 20000:
        return 10
    elif p < 50000:
        return 50
    
    # 5만원 이상부터는 코스피/코스닥 차이 없음 (단일화)
    if p < 200000:
        return 100
    elif p < 500000:
        return 500
    else:
        return 1000


def align_price_to_hoga(price: float, market_type: str = "10", direction: str = "round") -> int:
    """
    가격을 해당 종목의 호가 단위에 맞게 보정한다.
    direction: "round"(반올림), "up"(올림), "down"(내림)
    """
    if price <= 0: return 0
    unit = get_hoga_unit(int(price), market_type)
    
    if direction == "up":
        return int(((price + unit - 0.1) // unit) * unit)
    elif direction == "down":
        return int((price // unit) * unit)
    else: # round
        return int(((price + (unit / 2)) // unit) * unit)
