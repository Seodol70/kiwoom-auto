"""
SmartScanner — 영웅문 조건검색 없이 파이썬이 직접 전 종목을 감시한다.

개선 포인트
  ① 메모리 최적화  : SnapshotStore — pandas DataFrame 을 1차 캐시로 사용.
                      API 재호출 없이 메모리 내 연산으로 신호를 판단한다.
  ② 구조화 로그    : ScannerLogger — scanner.log 에 선정/탈락 이유를 기록한다.
  ③ 터미널 뷰      : ScannerDisplay — rich 라이브러리로 VS Code 터미널에
                      실시간 감시 테이블과 신호 알림을 출력한다.

3단계 핵심 로직
  [1단계] Pre-Filter  (09:00 1회)
    GetCodeListByMarket → opt10030 → 거래대금 상위 200위 → SnapshotStore 적재
  [2단계] Real-time Scan  (1초 주기)
    PriorityWatchQueue(SetRealReg) → SnapshotStore 갱신 → 신호 판단
  [3단계] Final Signal
    ScanSignal → on_signal 콜백 → 주문 모듈
"""

from __future__ import annotations

import heapq
import logging
import logging.handlers
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Callable, Optional

import pandas as pd

from scanner.universe import _is_ordinary_stock
from PyQt5.QtCore import QTimer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# 로거 설정
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)     # 일반 로거 (콘솔)

def _build_scan_logger(log_dir: str = "logs") -> logging.Logger:
    """scanner.log 전용 로거를 생성한다."""
    os.makedirs(log_dir, exist_ok=True)
    scan_log = logging.getLogger("scanner.audit")
    scan_log.setLevel(logging.DEBUG)
    scan_log.propagate = False   # 루트 로거로 전파 금지

    handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(log_dir, "scanner.log"),
        maxBytes=20 * 1024 * 1024,   # 20 MB
        backupCount=10,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s\t%(levelname)s\t%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    scan_log.addHandler(handler)
    return scan_log

scan_log: logging.Logger = _build_scan_logger()


# ---------------------------------------------------------------------------
# 거래대금 표기 (원 → 조·억 한글, 진단용 증가율)
# ---------------------------------------------------------------------------

_JO_WON = 1_000_000_000_000
_EOK_WON = 100_000_000


def format_trade_amount_korean(amount_won: int) -> str:
    """
    누적 거래대금(원)을 '3조 7,238억' 형으로 표기한다.
    1억 미만은 만원·원 단위로 축약한다.
    """
    try:
        n = int(amount_won)
    except (TypeError, ValueError):
        return "0억"
    if n <= 0:
        return "0억"
    jo = n // _JO_WON
    rem = n % _JO_WON
    eok_int = rem // _EOK_WON
    parts: list[str] = []
    if jo > 0:
        parts.append(f"{jo}조")
    if eok_int > 0:
        parts.append(f"{eok_int:,}억")
    if parts:
        return " ".join(parts)
    man = n // 10_000
    if man > 0:
        return f"{man:,}만원"
    return f"{n:,}원"


def format_trade_amount_growth(current: int, baseline: Optional[int]) -> str:
    """거래대금 증가율(%) — baseline 이 없거나 0이면 '—'."""
    if baseline is None or baseline <= 0:
        return "증가율(9시대비) —"
    pct = (current - baseline) / baseline * 100.0
    return (
        f"증가율(9시대비) {pct:+.1f}% "
        f"(기준 {format_trade_amount_korean(baseline)})"
    )


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass
class SmartScannerConfig:
    # opt10030 최초 수집 목표(연속조회로 최대 ~2회 TR). 이후 ETF·우선주 제거 → watch_pool_max 로 캡.
    collect_raw_top_n:    int   = 200
    watch_pool_max:       int   = 110         # 필터 후 거래대금 상위 유지 (100~120 권장 중앙값)
    pre_filter_top_n:     int   = 200         # 하위 호환: collect_raw_top_n 과 동일 사용 권장
    pre_filter_time:      dtime = dtime(9, 0, 0)
    realtime_sub_max:     int   = 110         # SetRealReg 감시 상한( watch_pool_max 와 맞춤)
    scan_interval:        float = 1.0
    tr_delay:             float = 0.25        # TRRequestQueue 최소 간격
    breakout_ratio:       float = 0.02
    breakout_volume_mult: float = 1.5
    jdm_ma_short:         int   = 7          # 최적화됨: 5→7
    jdm_ma_long:          int   = 15         # 최적화됨: 20→15
    jdm_rsi_low:          float = 35.0       # 레거시(다른 로직 참고용). 진입은 jdm_rsi_entry_min 사용
    jdm_rsi_high:         float = 70.0       # RSI 상한(과열 차단)
    jdm_rsi_entry_min:    float = 60.0       # JDM 진입 RSI 하한 — 횡보·무기력 구간 배제
    jdm_min_ma_spread_abs: int = 30          # 골든크로스 시 MA단기−MA장기 최소 이격(원) — 미세 교차 방지
    jdm_take_profit_pct:  float = 3.0        # 익절 목표 (최적화됨: 4.0%→3.0%)
    jdm_stop_loss_pct:    float = -1.0       # 손절 기준 (최적화됨: -1.5%→-1.0%)
    markets:              tuple = ("0", "10")
    screen_realtime:      str   = "9200"
    display_top_n:        int   = 50    # 스캐너 UI 감시 테이블·Worker 상위 표시
    # [진단] 로그 거래대금 상위 샘플 — 매수 후보 전체와 무관(후보는 watch_pool_max·display_top_n 참고)
    diagnostic_sample_n:  int   = 5
    log_dir:              str   = "logs"
    # 등락률이 이 값 **이상**이면 감시·신호·매수 대상에서 제외 (config RISK.max_change_pct 와 동기화)
    max_change_pct:       float = 15.0
    # ScannerWorker: 동일 종목 재 emit 최소 간격(초). 에지 트리거와 병행 (config RISK.signal_cooldown_sec)
    signal_cooldown_sec:  float = 45.0
    # [NEW] 4중 필터 — JDM 신호 품질 강화
    entry_start_time:     dtime = dtime(9, 0, 0)    # 진입 허용 시작
    entry_end_time:       dtime = dtime(9, 30, 0)   # 진입 허용 종료
    min_chejan_strength:  float = 120.0             # 체결강도 하한 (%)
    volume_surge_mult:    float = 3.0               # 분봉 거래량 배수 (직전 5분 평균 대비)
    max_disparity_pct:    float = 5.0               # MA20 이격도 상한 (%)


# ---------------------------------------------------------------------------
# TRRequestQueue — 키움 API 요청 간격 보장 (최대 4회/초)
# ---------------------------------------------------------------------------

def is_pure_equity_name(name: str) -> bool:
    """
    ETF·ETN·인버스·레버리지·스팩 및 국내 ETF 브랜드명이 들어가면 False.

    스캐너 감시/스냅샷 적재 시 순수 주식만 남기기 위해 사용한다.
    """
    if not name or not str(name).strip():
        return False
    n = str(name).strip()
    upper = n.upper()

    # 강화된 필터링 — ETF/ETN/파생상품 전부 제외
    exclude_kw = (
        # 기본
        "ETF", "ETN", "인버스", "레버리지", "곱버스", "역추적",
        "2X", "3X", "5X", "10X", "스팩", "SPAC", "헷지", "HEDGE",
        # 선물추적, 옵션, 수익증권
        "선물", "옵션", "수익증권", "구조", "파생",
        # ETF 브랜드
        "KODEX", "TIGER", "KBSTAR", "HANAR", "KOSEF", "ARIRANG",
        "TIMEFOLIO", "KINDEX", "ACE", "RISE", "SOL", "FOCUS",
    )
    for kw in exclude_kw:
        if kw in n or kw in upper:
            return False

    return True


def filter_equity_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """opt10030 등에서 받은 행 리스트에서 우선주·비주식(ETF 등)을 제거한다."""
    out: list[dict] = []
    dropped = 0
    for r in rows:
        code = str(r.get("code", "")).lstrip("A").strip()
        if not _is_ordinary_stock(code):
            dropped += 1
            logger.debug("[유니버스필터] 우선주 제외 — %s(%s)", r.get("name", ""), code)
            continue
        nm = r.get("name", "")
        if is_pure_equity_name(str(nm)):
            out.append(r)
        else:
            dropped += 1
            logger.debug(
                "[유니버스필터] 제외 — %s(%s)",
                nm, code,
            )
    if dropped:
        logger.info("[유니버스필터] 우선주·ETF·파생 등 제외 %d건 → 잔여 %d건", dropped, len(out))
    return out, dropped


def apply_watch_pool_cap(rows: list[dict], watch_pool_max: int) -> list[dict]:
    """거래대금 내림차순으로 상위 watch_pool_max 종목만 유지."""
    if not rows:
        return []
    rows = sorted(
        rows,
        key=lambda r: int(r.get("trade_amount", 0) or 0),
        reverse=True,
    )
    return rows[:watch_pool_max]


class TRRequestQueue:
    """
    키움 TR 호출 간격을 중앙에서 관리한다.

    키움 API 제한: 연속 TR 호출 간 최소 0.2초 권장.
    여기서 0.25초로 설정해 여유를 두고, 모든 TR 호출을
    call() 메서드를 통해 실행하면 자동으로 간격이 보장된다.

    기존 time.sleep(tr_delay) 분산 호출을 이 클래스로 대체한다.
    """
    _MIN_INTERVAL = 0.25  # 초

    def __init__(self) -> None:
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def call(self, fn: Callable, *args):
        """fn(*args)를 최소 간격 보장 후 실행하고 결과를 반환한다."""
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            wait = self._MIN_INTERVAL - elapsed
            if wait > 0:
                time.sleep(wait)
            result = fn(*args)
            self._last_call = time.monotonic()
            return result


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class StockSnapshot:
    code:          str
    name:          str
    current_price: int   = 0
    open_price:    int   = 0
    high_price:    int   = 0
    low_price:     int   = 0
    volume:        int   = 0
    trade_amount:  int   = 0
    prev_close:    int   = 0
    change_pct:    float = 0.0
    closes_1min:   list  = field(default_factory=list)
    chejan_strength: float = 100.0          # [NEW] 체결강도 (FID 20)
    volumes_1min:   list  = field(default_factory=list)   # [NEW] 1분봉 거래량
    updated_at:    datetime = field(default_factory=datetime.now)


@dataclass
class ScanSignal:
    code:         str
    name:         str
    signal_type:  str        # "BREAKOUT" | "JDM_ENTRY"
    price:        int
    reason:       str
    generated_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# ① SnapshotStore — pandas DataFrame 캐시
# ---------------------------------------------------------------------------

_DF_COLS = [
    "code", "name",
    "current_price", "open_price", "high_price", "low_price",
    "volume", "trade_amount", "prev_close", "change_pct",
    "rank", "updated_at",
]


def _df_cell_scalar(val, default=None):
    """
    DataFrame.loc[code] 행에서 컬럼 값이 스칼라가 아니라 Series인 경우(중복 컬럼명 등) 대비.
    truthiness 검사로 Series를 건드리지 않도록 첫 스칼라만 꺼낸다.
    """
    if val is None:
        return default
    if isinstance(val, pd.Series):
        if val.empty:
            return default
        val = val.iloc[0]
    try:
        if pd.isna(val):
            return default
    except TypeError:
        pass
    return val


class SnapshotStore:
    """
    전 종목 스냅샷을 pandas DataFrame 에 보관한다.

    ┌──────────────────────────────────────────────────┐
    │ 왜 DataFrame 인가?                               │
    │  · bulk_update() 1회 호출로 200종목 일괄 적재   │
    │  · df.nlargest() 로 API 재호출 없이 순위 산출   │
    │  · 컬럼 연산 (vectorized) 으로 신호 판단 가능   │
    │  · 백테스트 CSV 로 바로 export 가능             │
    └──────────────────────────────────────────────────┘

    실시간 틱은 update_price() 로 행 단위 갱신한다.
    closes_1min 은 DataFrame 외부에서 dict 로 별도 관리
    (리스트 컬럼은 벡터 연산 불가 → 분리가 더 효율적).
    """

    def __init__(self) -> None:
        self._df   = pd.DataFrame(columns=_DF_COLS).set_index("code")
        self._mins: dict[str, list[float]] = {}   # code → 1분봉 종가
        self._last_min: dict[str, int] = {}        # code → 마지막 기록된 분(minute)
        # [NEW] 분봉 거래량 추적
        self._min_vols:  dict[str, list[int]]   = {}   # code → 1분봉 별 거래량 델타
        self._last_vol:  dict[str, int]         = {}   # code → 직전 분 경계 누적거래량
        # [NEW] 체결강도 추적
        self._chejan_str: dict[str, float]      = {}   # code → 체결강도 (FID 20)
        self._lock = threading.Lock()

    # ── 일괄 적재 ─────────────────────────────────────────────────────────

    # 숫자형으로 강제 변환할 컬럼
    _NUM_COLS = [
        "current_price", "open_price", "high_price", "low_price",
        "volume", "trade_amount", "prev_close", "change_pct", "rank",
    ]

    def bulk_update(self, rows: list[dict]) -> None:
        """
        Pre-Filter 결과(list[dict])를 DataFrame 에 일괄 적재한다.
        기존 행이 있으면 갱신, 없으면 추가한다.
        """
        if not rows:
            logger.warning("[SnapshotStore.bulk_update] rows 빈 리스트 — 적재 스킵")
            return

        rows, _dropped = filter_equity_rows(rows)
        if not rows:
            logger.warning("[SnapshotStore.bulk_update] ETF·파생 제외 후 빈 리스트 — 적재 스킵")
            return

        # 첫 행 진단 로그
        first = rows[0]
        logger.debug("[SnapshotStore.bulk_update] 첫 행 keys=%s", list(first.keys()))
        logger.debug("[SnapshotStore.bulk_update] 첫 행 샘플: code=%s name=%s price=%s volume=%s trade_amount=%s",
                     first.get("code"), first.get("name"),
                     first.get("current_price"), first.get("volume"),
                     first.get("trade_amount"))

        new_df = pd.DataFrame(rows).set_index("code")
        new_df["updated_at"] = datetime.now()
        # 숫자 컬럼 타입 보장
        for col in self._NUM_COLS:
            if col in new_df.columns:
                new_df[col] = pd.to_numeric(new_df[col], errors="coerce").fillna(0)
        with self._lock:
            self._df = new_df.combine_first(self._df)
            for col in self._NUM_COLS:
                if col in self._df.columns:
                    self._df[col] = pd.to_numeric(self._df[col], errors="coerce").fillna(0)
            # 이전 세션에서 남은 ETF 행 제거 (combine_first 로 잔존 가능)
            if not self._df.empty and "name" in self._df.columns:
                keep = self._df["name"].astype(str).map(is_pure_equity_name)
                for c in self._df.index[~keep].tolist():
                    self._mins.pop(c, None)
                self._df = self._df[keep]
            for code in new_df.index:
                if code not in self._mins:
                    self._mins[code] = []

        logger.debug("[SnapshotStore.bulk_update] 적재 완료 — df 행수=%d", len(self._df))

    # ── 실시간 틱 갱신 ────────────────────────────────────────────────────

    # 틱 갱신 시 업데이트할 컬럼 — updated_at 제외 (핫패스 슬림화)
    _TICK_COLS = ["current_price", "high_price", "low_price",
                  "open_price", "volume", "trade_amount", "change_pct"]

    def update_price(
        self,
        code:         str,
        current_price: int,
        high_price:   int,
        low_price:    int,
        open_price:   int,
        volume:       int,
        trade_amount: int,
        change_pct:   float,
    ) -> None:
        """
        실시간 체결 한 틱을 해당 종목 행에 반영한다.

        슬림화: updated_at 컬럼을 핫패스에서 제거
        → datetime.now() 호출 1회 / DataFrame 쓰기 컬럼 수 감소
        1분봉 누적은 로컬 변수로 분(minute) 비교해 최소 조건에서만 append.
        """
        with self._lock:
            if code not in self._df.index:
                return   # Pre-Filter 에 없는 종목은 무시
            self._df.loc[code, self._TICK_COLS] = [
                current_price, high_price, low_price, open_price,
                volume, trade_amount, change_pct,
            ]
            # 1분봉 누적 — 분(minute)이 바뀔 때만 append (초당 수십 번 실행 최소화)
            cur_min = (datetime.now().hour * 60 +  # noqa: DTZ005
                       datetime.now().minute)
            if self._last_min.get(code, -1) != cur_min:
                self._last_min[code] = cur_min
                # [기존] 1분봉 종가 기록
                mins = self._mins.setdefault(code, [])
                mins.append(float(current_price))
                if len(mins) > 120:
                    mins.pop(0)
                # [NEW] 1분봉 거래량 델타 기록 (직전 분의 누적거래량 증가량)
                prev_cumvol = self._last_vol.get(code, volume)
                delta       = max(0, volume - prev_cumvol)
                vols        = self._min_vols.setdefault(code, [])
                vols.append(delta)
                if len(vols) > 120:
                    vols.pop(0)
                self._last_vol[code] = volume  # 새 분 시작 기준선 갱신

    # ── 조회 ──────────────────────────────────────────────────────────────

    def get_snapshot(self, code: str) -> Optional[StockSnapshot]:
        """단일 종목 스냅샷을 반환한다 (API 호출 없음)."""
        with self._lock:
            if code not in self._df.index:
                return None
            row = self._df.loc[code]

            def safe_int_cell(key: str, default: int = 0) -> int:
                v = _df_cell_scalar(row.get(key, default), None)
                if v is None:
                    return default
                try:
                    iv = int(float(v))
                except (TypeError, ValueError):
                    return default
                return iv if iv != 0 else default

            def safe_float_cell(key: str, default: float = 0.0) -> float:
                v = _df_cell_scalar(row.get(key, default), None)
                if v is None:
                    return default
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return default
                return fv if fv != 0 else default

            nm = _df_cell_scalar(row.get("name", ""), "")
            name_s = str(nm) if nm is not None else ""

            ua_raw = _df_cell_scalar(row.get("updated_at"), None)
            if isinstance(ua_raw, datetime):
                updated_at = ua_raw
            elif ua_raw is not None:
                try:
                    updated_at = pd.Timestamp(ua_raw).to_pydatetime()
                except Exception:
                    updated_at = datetime.now()
            else:
                updated_at = datetime.now()

            return StockSnapshot(
                code          = code,
                name          = name_s,
                current_price = safe_int_cell("current_price", 0),
                open_price    = safe_int_cell("open_price",    0),
                high_price    = safe_int_cell("high_price",    0),
                low_price     = safe_int_cell("low_price",     0),
                volume        = safe_int_cell("volume",        0),
                trade_amount  = safe_int_cell("trade_amount",  0),
                prev_close    = safe_int_cell("prev_close",    0),
                change_pct    = safe_float_cell("change_pct",  0.0),
                closes_1min   = list(self._mins.get(code, [])),
                chejan_strength = self._chejan_str.get(code, 100.0),  # [NEW]
                volumes_1min    = list(self._min_vols.get(code, [])),  # [NEW]
                updated_at    = updated_at,
            )

    def update_chejan_strength(self, code: str, strength: float) -> None:
        """[NEW] 체결강도(FID 20) 갱신."""
        if strength > 0:
            with self._lock:
                self._chejan_str[code] = strength

    def prefilter_candidates(self, max_change_pct: Optional[float] = None) -> list[str]:
        """
        벡터화 사전 필터 — DataFrame 연산으로 Python 루프 전 후보 종목을 추린다.

        조건 (모두 DataFrame 컬럼 연산, O(n) 한 번):
          ① current_price > 0          (가격 유효)
          ② current_price > open_price  (시가 돌파 기본 조건)
          ③ change_pct > 0             (양봉 기조)
          ④ volume > 0                 (거래량 있음)
          ⑤ max_change_pct 지정 시: change_pct < max_change_pct (과열 급등 제외)

        반환값: 조건을 통과한 종목코드 리스트 (MA 검사는 이후 Python 루프에서)

        효과: 50종목 중 보통 5~15개만 남아 Python 루프 비용이 70~90% 감소
        """
        with self._lock:
            if self._df.empty:
                return []
            df = self._df
            ch = df.get("change_pct", pd.Series(0, index=df.index))
            mask = (
                (df["current_price"] > 0) &
                (df["current_price"] > df["open_price"]) &
                (ch > 0) &
                (df["volume"] > 0)
            )
            if max_change_pct is not None:
                mask = mask & (ch < max_change_pct)
            return list(df.index[mask])

    def set_min_candles(self, code: str, closes: list) -> None:
        """opt10080 등으로 가져온 분봉 종가 리스트를 초기값으로 설정한다."""
        with self._lock:
            self._mins[code] = [float(c) for c in closes if c]

    def top_by_trade_amount(self, n: int = 20) -> pd.DataFrame:
        """
        거래대금 상위 n 종목 DataFrame 반환 (복사본).
        trade_amount 가 모두 0 이면 volume → rank 순으로 fallback.
        """
        with self._lock:
            if self._df.empty:
                return pd.DataFrame()
            non_zero_amt = self._df[self._df["trade_amount"] > 0]
            if not non_zero_amt.empty:
                return non_zero_amt.nlargest(n, "trade_amount").copy()
            # trade_amount 모두 0인 경우 거래량으로 fallback
            non_zero_vol = self._df[self._df["volume"] > 0]
            if not non_zero_vol.empty:
                return non_zero_vol.nlargest(n, "volume").copy()
            # 거래량도 없으면 rank 기준
            if "rank" in self._df.columns:
                ranked = self._df.dropna(subset=["rank"])
                if not ranked.empty:
                    return ranked.nsmallest(n, "rank").copy()
            return self._df.head(n).copy()

    def export_csv(self, path: str = "logs/snapshot.csv") -> None:
        """현재 스냅샷을 CSV 로 내보낸다."""
        with self._lock:
            self._df.reset_index().to_csv(path, index=False, encoding="utf-8-sig")

    def __len__(self) -> int:
        return len(self._df)


# ---------------------------------------------------------------------------
# ② ScannerLogger — scanner.log 구조화 기록
# ---------------------------------------------------------------------------

class ScannerLogger:
    """
    스캐너 판단 근거를 scanner.log 에 기록한다.

    선정 로그: PASS  | code | name | signal_type | reason
    탈락 로그: FAIL  | code | name | filter_step | reason
    신호 로그: SIGNAL| code | name | signal_type | price | reason
    """

    @staticmethod
    def passed(code: str, name: str, step: str, reason: str) -> None:
        scan_log.info("PASS\t%s\t%s\t%s\t%s", code, name, step, reason)

    @staticmethod
    def rejected(code: str, name: str, step: str, reason: str) -> None:
        scan_log.debug("FAIL\t%s\t%s\t%s\t%s", code, name, step, reason)

    @staticmethod
    def signal(sig: ScanSignal) -> None:
        scan_log.warning(
            "SIGNAL\t%s\t%s\t%s\t%d\t%s",
            sig.code, sig.name, sig.signal_type, sig.price, sig.reason,
        )

    @staticmethod
    def pre_filter_summary(total: int, passed: int, top_n: int) -> None:
        scan_log.info(
            "PRE_FILTER\t전체=%d\t통과=%d\tTop%d 선정",
            total, passed, top_n,
        )


# ---------------------------------------------------------------------------
# ③ ScannerDisplay — rich 터미널 뷰
# ---------------------------------------------------------------------------

_CONSOLE = Console()

class ScannerDisplay:
    """
    rich.Live 를 사용해 VS Code 터미널에 실시간 감시 테이블을 출력한다.

    사용 예)
        display = ScannerDisplay(store, cfg)
        display.start()          # 백그라운드 갱신 시작
        display.alert(signal)    # 신호 발생 시 즉시 알림
        display.stop()
    """

    def __init__(self, store: SnapshotStore, cfg: SmartScannerConfig) -> None:
        self._store   = store
        self._cfg     = cfg
        self._live    = Live(console=_CONSOLE, refresh_per_second=1, screen=False)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._live.start()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ScannerDisplay"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._live.stop()

    def alert(self, sig: ScanSignal) -> None:
        """신호 발생 시 터미널에 즉시 강조 출력한다."""
        color = "bright_red" if sig.signal_type == "BREAKOUT" else "bright_green"
        _CONSOLE.print(
            f"\n🚨 [{color}][ {sig.signal_type} ] {sig.name}({sig.code})[/] "
            f"  가격 [bold]{sig.price:,}원[/]  |  {sig.reason}\n",
        )

    # ── 루프 ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            self._live.update(self._build_table())
            time.sleep(1.0)

    def _build_table(self) -> Table:
        top_df = self._store.top_by_trade_amount(self._cfg.display_top_n)

        table = Table(
            title=f"[bold cyan]SmartScanner 감시 현황[/]  "
                  f"{datetime.now().strftime('%H:%M:%S')}  "
                  f"[dim](감시 {len(top_df)}종목)[/]",
            show_lines=False,
            header_style="bold white on dark_blue",
            border_style="dim",
        )
        table.add_column("순위",   justify="right",  width=5)
        table.add_column("종목코드", width=8)
        table.add_column("종목명",  width=12)
        table.add_column("현재가",  justify="right",  width=9)
        table.add_column("등락률",  justify="right",  width=8)
        table.add_column("거래량",  justify="right",  width=10)
        table.add_column("거래대금", justify="right", width=16)
        table.add_column("갱신시각", width=9)

        if top_df.empty:
            table.add_row(*["─"] * 8)
            return table

        for rank, (code, row) in enumerate(top_df.iterrows(), 1):
            # pandas Series에서 값 안전하게 추출 (or 연산자 사용 금지)
            cp = row.get("change_pct", 0)
            change = float(cp) if cp else 0.0
            if change > 0:
                pct_text = Text(f"+{change:.2f}%", style="bright_red")
            elif change < 0:
                pct_text = Text(f"{change:.2f}%",  style="bright_blue")
            else:
                pct_text = Text(f"{change:.2f}%",  style="white")

            p = row.get("current_price", 0)
            v = row.get("volume", 0)
            a = row.get("trade_amount", 0)
            price = int(p) if p else 0
            vol   = int(v) if v else 0
            amt   = int(a) if a else 0
            upd   = row.get("updated_at", datetime.now())
            upd_s = upd.strftime("%H:%M:%S") if isinstance(upd, datetime) else "--:--:--"

            table.add_row(
                str(rank),
                str(code),
                str(row.get("name", "")),
                f"{price:,}",
                pct_text,
                f"{vol:,}",
                format_trade_amount_korean(amt),
                upd_s,
            )

        return table


# ---------------------------------------------------------------------------
# TopVolumeManager — 거래대금 상위 N 종목 관리
# ---------------------------------------------------------------------------

class TopVolumeManager:
    def __init__(self, top_n: int = 200) -> None:
        self.top_n     = top_n
        self._amounts: dict[str, int] = {}
        self._lock     = threading.Lock()

    def clear(self) -> None:
        """이전 스캔에서 쌓인 거래대금 맵을 비운다."""
        with self._lock:
            self._amounts.clear()

    def update(self, code: str, trade_amount: int) -> bool:
        with self._lock:
            self._amounts[code] = trade_amount
            return self._rank(code) <= self.top_n

    def get_top_codes(self, n: Optional[int] = None) -> list[str]:
        n = n or self.top_n
        with self._lock:
            return [c for c, _ in sorted(
                self._amounts.items(), key=lambda x: -x[1]
            )[:n]]

    def _rank(self, code: str) -> int:
        sorted_codes = [c for c, _ in sorted(
            self._amounts.items(), key=lambda x: -x[1]
        )]
        try:
            return sorted_codes.index(code) + 1
        except ValueError:
            return 999999


# ---------------------------------------------------------------------------
# PriorityWatchQueue — SetRealReg 구독 관리
# ---------------------------------------------------------------------------

class PriorityWatchQueue:
    def __init__(self, kiwoom, screen_no: str = "9200", max_subs: int = 100) -> None:
        self._kiwoom   = kiwoom
        self._screen   = screen_no
        self._max_subs = max_subs
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()

    def refresh(self, top_codes: list[str]) -> None:
        with self._lock:
            target    = set(top_codes[: self._max_subs])
            to_add    = target - self._subscribed
            to_remove = self._subscribed - target
            for code in to_remove:
                self._unsub(code)
            if to_add:
                # 여러 종목을 SetRealReg 1회 배치 호출 (50회 → 1회)
                # strCodeList 에 ';' 구분 다종목 지원 (키움 API 공식 지원)
                code_list = ";".join(to_add)
                self._kiwoom._ocx.dynamicCall(
                    "SetRealReg(QString, QString, QString, QString)",
                    [self._screen, code_list, "10;11;12;13;14;16;17;18;20", "1"],  # [NEW] FID 20: 체결강도
                )
                self._subscribed.update(to_add)
                logger.debug("[PriorityWatchQueue] SetRealReg 배치 등록 %d종목", len(to_add))

    def _sub(self, code: str) -> None:
        self._kiwoom._ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            [self._screen, code, "10;11;12;13;14;16;17;18;20", "1"],  # [NEW] FID 20: 체결강도
        )
        self._subscribed.add(code)

    def _unsub(self, code: str) -> None:
        self._kiwoom._ocx.dynamicCall(
            "SetRealRemove(QString, QString)", [self._screen, code]
        )
        self._subscribed.discard(code)

    @property
    def subscribed(self) -> set[str]:
        return self._subscribed.copy()


# ---------------------------------------------------------------------------
# 신호 판단 함수 (순수 함수)
# ---------------------------------------------------------------------------

def check_breakout(
    snap:           StockSnapshot,
    breakout_ratio: float = 0.02,
    volume_mult:    float = 1.5,
) -> Optional[str]:
    if snap.prev_close <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT", "prev_close=0")
        return None

    threshold = snap.prev_close * (1 + breakout_ratio)

    if snap.current_price < threshold:
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"현재가 {snap.current_price:,} < 돌파기준 {threshold:,.0f}",
        )
        return None

    if snap.current_price < snap.high_price:
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"신고가 미갱신 (현재 {snap.current_price:,} < 당일고가 {snap.high_price:,})",
        )
        return None

    avg_vol = snap.trade_amount / snap.current_price if snap.current_price else 0
    if avg_vol > 0 and snap.volume < avg_vol * volume_mult:
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"거래량 부족 ({snap.volume:,} < 기준 {avg_vol * volume_mult:,.0f})",
        )
        return None

    reason = (
        f"전일종가 {snap.prev_close:,} 대비 {breakout_ratio*100:.0f}% 돌파 "
        f"| 현재가 {snap.current_price:,}"
    )
    ScannerLogger.passed(snap.code, snap.name, "BREAKOUT", reason)
    return reason


def check_testa_alignment(
    snap: StockSnapshot,
    max_ma_spread: float = 0.05,   # MA10-MA50 이격도 상한 (5%) — 과열 설거지 방지
) -> Optional[str]:
    """
    테스타 정배열 확인: MA10 > MA20 > MA50 + 이격도 과열 필터.

    조건:
      ① MA10 > MA20 > MA50   (정배열)
      ② (MA10 - MA50) / MA50 ≤ max_ma_spread   (이격 과열 차단)
         → MA10 이 MA50 보다 5% 이상 높으면 이미 급등 종료 구간 (설거지 위험)

    1분봉 종가 50개 이상 필요.
    """
    closes = snap.closes_1min
    if len(closes) < 50:
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"1분봉 데이터 부족 ({len(closes)}/50)",
        )
        return None

    from strategy.jang_dong_min import calc_ma
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma50 = calc_ma(closes, 50)

    if any(v is None for v in [ma10, ma20, ma50]):
        ScannerLogger.rejected(snap.code, snap.name, "TESTA", "MA 계산 실패")
        return None

    if not (ma10 > ma20 > ma50):
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"정배열 미충족 MA10={ma10:.0f} MA20={ma20:.0f} MA50={ma50:.0f}",
        )
        return None

    # 이격도 과열 체크 — (MA10 - MA50) / MA50 > max_ma_spread 이면 탈락
    spread = (ma10 - ma50) / ma50 if ma50 > 0 else 0.0
    if spread > max_ma_spread:
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"MA 이격 과열 {spread:.1%} > {max_ma_spread:.0%} "
            f"(MA10={ma10:.0f} MA50={ma50:.0f}) — 설거지 위험",
        )
        return None

    reason = (
        f"정배열 MA10={ma10:.0f} > MA20={ma20:.0f} > MA50={ma50:.0f} "
        f"이격={spread:.1%}"
    )
    ScannerLogger.passed(snap.code, snap.name, "TESTA", reason)
    return reason


def check_jdm_open_breakout(
    snap: StockSnapshot,
    min_body_ratio: float = 0.7,   # 양봉 몸통 비율 하한 — 윗꼬리 가짜 돌파 차단
) -> Optional[str]:
    """
    장동민 시가 돌파: 현재가 > 시가 + 양봉 몸통 비율 필터.

    조건:
      ① current_price > open_price   (시가 돌파)
      ② (current_price - open_price) / (high_price - low_price) ≥ min_body_ratio
         → 몸통이 전체 캔들 범위의 70% 이상 — 윗꼬리 달린 가짜 돌파 차단
         (예: 시가 1만, 고가 1.1만, 현재가 1.01만 → 몸통 10%에 불과 → 탈락)
    """
    if snap.open_price <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_OPEN", "시가/현재가 0")
        return None

    if snap.current_price <= snap.open_price:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_OPEN",
            f"시가 미돌파 현재가={snap.current_price:,} 시가={snap.open_price:,}",
        )
        return None

    # 양봉 몸통 비율 체크
    candle_range = snap.high_price - snap.low_price
    if candle_range > 0:
        body_ratio = (snap.current_price - snap.open_price) / candle_range
        if body_ratio < min_body_ratio:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_OPEN",
                f"몸통 비율 부족 {body_ratio:.0%} < {min_body_ratio:.0%} "
                f"(윗꼬리 달린 가짜 돌파 — 고가={snap.high_price:,})",
            )
            return None

    breakout_pct = (snap.current_price - snap.open_price) / snap.open_price * 100
    body_ratio_str = (
        f" 몸통={((snap.current_price - snap.open_price) / candle_range):.0%}"
        if candle_range > 0 else ""
    )
    reason = (
        f"시가돌파 현재가={snap.current_price:,} > 시가={snap.open_price:,} "
        f"(+{breakout_pct:.2f}%){body_ratio_str}"
    )
    ScannerLogger.passed(snap.code, snap.name, "JDM_OPEN", reason)
    return reason


# [NEW] 신규 필터 함수 3개 — JDM 신호 품질 강화 (4중 필터)

def check_volume_surge(
    snap: StockSnapshot,
    surge_mult: float = 3.0,
) -> Optional[str]:
    """[NEW] 직전 5분 평균 거래량 대비 surge_mult 배 이상인지 확인."""
    vols = snap.volumes_1min
    if len(vols) < 6:
        return None   # 데이터 부족
    avg5 = sum(vols[-6:-1]) / 5
    if avg5 <= 0:
        return None
    cur = vols[-1]
    if cur < avg5 * surge_mult:
        ScannerLogger.rejected(snap.code, snap.name, "VOL_SURGE",
                               f"거래량 {cur:,} / 5분평균 {avg5:,.0f} ({cur/avg5:.1f}배 < {surge_mult}배)")
        return None
    return f"거래량급증{cur:,}주({cur/avg5:.1f}배)"


def check_chejan_strength(
    snap: StockSnapshot,
    min_strength: float = 120.0,
) -> Optional[str]:
    """[NEW] 체결강도 min_strength% 이상 확인 (매수 수급 우위)."""
    if snap.chejan_strength < min_strength:
        ScannerLogger.rejected(snap.code, snap.name, "CHEJAN",
                               f"체결강도 {snap.chejan_strength:.0f}% < {min_strength:.0f}%")
        return None
    return f"체결강도{snap.chejan_strength:.0f}%"


def check_disparity_from_ma(
    snap: StockSnapshot,
    ma_period: int   = 20,
    max_pct: float   = 5.0,
) -> Optional[str]:
    """[NEW] 1분봉 MA(ma_period) 대비 이격도 max_pct% 이내 확인 (과열 차단)."""
    closes = snap.closes_1min
    if len(closes) < ma_period:
        return None   # 데이터 부족 시 bypass (초반 20분간 허용)
    from strategy.jang_dong_min import calc_ma
    ma = calc_ma(closes, ma_period)
    if ma is None or ma <= 0:
        return None
    disp = (snap.current_price - ma) / ma * 100
    if disp > max_pct:
        ScannerLogger.rejected(snap.code, snap.name, "DISPARITY",
                               f"MA{ma_period} 이격도 {disp:.1f}% > {max_pct:.1f}%")
        return None
    return f"MA{ma_period}이격{disp:.1f}%"


def check_jdm_entry(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    JDM_ENTRY 통합 게이트 (ScannerWorker / SmartScanner._evaluate 공통).

    ① 진입 허용 시각(entry_start~entry_end) — 오후 저유동 구간 등 배제
    ② 직전 5분 평균 대비 분봉 거래량 volume_surge_mult 배 이상
    ③ 체결강도 min_chejan_strength% 이상
    ④ MA 골든크로스 + 단·장기 이격 jdm_min_ma_spread_abs 원 이상
    ⑤ RSI ∈ [jdm_rsi_entry_min, jdm_rsi_high)
    """
    now = datetime.now().time()
    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_TIME",
            f"진입 허용 시간 아님 ({cfg.entry_start_time}~{cfg.entry_end_time})",
        )
        return None

    closes = snap.closes_1min
    need   = cfg.jdm_ma_long + 1

    if len(closes) < need:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM",
            f"1분봉 데이터 부족 ({len(closes)}/{need})",
        )
        return None

    r_vol = check_volume_surge(snap, cfg.volume_surge_mult)
    if r_vol is None:
        return None

    r_chej = check_chejan_strength(snap, cfg.min_chejan_strength)
    if r_chej is None:
        return None

    from strategy.jang_dong_min import calc_ma, calc_rsi
    ma_s  = calc_ma(closes,      cfg.jdm_ma_short)
    ma_l  = calc_ma(closes,      cfg.jdm_ma_long)
    rsi   = calc_rsi(closes,     14)
    pma_s = calc_ma(closes[:-1], cfg.jdm_ma_short)
    pma_l = calc_ma(closes[:-1], cfg.jdm_ma_long)

    if any(v is None for v in [ma_s, ma_l, rsi, pma_s, pma_l]):
        return None

    golden = pma_s <= pma_l and ma_s > ma_l
    if not golden:
        ScannerLogger.rejected(snap.code, snap.name, "JDM", "골든크로스 미충족")
        return None

    spread = float(ma_s) - float(ma_l)
    if spread < float(cfg.jdm_min_ma_spread_abs):
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM",
            f"MA 이격 부족 (단기−장기={spread:.0f}원 < {cfg.jdm_min_ma_spread_abs}원)",
        )
        return None

    rsi_ok = cfg.jdm_rsi_entry_min <= rsi < cfg.jdm_rsi_high
    if not rsi_ok:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM",
            f"RSI {rsi:.1f} (진입허용 {cfg.jdm_rsi_entry_min:.0f}~{cfg.jdm_rsi_high:.0f})",
        )
        return None

    core = (
        f"MA골든크로스 MA{cfg.jdm_ma_short}={ma_s:.0f} / "
        f"MA{cfg.jdm_ma_long}={ma_l:.0f} | RSI={rsi:.1f}"
    )
    reason = f"{r_vol} | {r_chej} | {core}"
    ScannerLogger.passed(snap.code, snap.name, "JDM", reason)
    return reason


# ---------------------------------------------------------------------------
# SmartScanner — 통합 오케스트레이터
# ---------------------------------------------------------------------------

class SmartScanner:
    """
    3단계 스마트 스캐너 (메모리 최적화 + 로그 + 터미널 뷰 통합).

    사용 예)
        scanner = SmartScanner(kiwoom)
        scanner.on_signal = lambda sig: order_module.execute(sig)
        scanner.start()
    """

    def __init__(self, kiwoom, cfg: Optional[SmartScannerConfig] = None) -> None:
        self._kiwoom = kiwoom
        self.cfg     = cfg or SmartScannerConfig()

        # ① DataFrame 캐시
        self.store   = SnapshotStore()

        # TR 요청 큐 — 키움 API 간격 보장
        self._tr_q   = TRRequestQueue()

        # 컴포넌트
        self.top_mgr = TopVolumeManager(
            top_n=max(self.cfg.collect_raw_top_n, self.cfg.watch_pool_max),
        )
        self.watch_q = PriorityWatchQueue(
            kiwoom,
            screen_no=self.cfg.screen_realtime,
            max_subs=self.cfg.realtime_sub_max,
        )

        # ③ 터미널 뷰
        self.display = ScannerDisplay(self.store, self.cfg)

        self._running     = False
        self._prefiltered = False
        self._scan_thread: Optional[threading.Thread] = None
        self._lock        = threading.Lock()

        self.on_signal: Optional[Callable[[ScanSignal], None]] = None

        # 포지션 현재가 실시간 업데이트용 (MainWindow에서 주입)
        self._order_mgr = None

        # watch_q.refresh 쓰로틀 — SetRealReg를 매 틱 호출 방지 (30초 간격)
        self._last_watchq_refresh: float = 0.0
        self._WATCHQ_INTERVAL: float = 30.0

        # 동적 감시 중단: 포지션 풀(max_positions)시 유니버스 감시를 보유종목만으로 축소
        self._universe_paused: bool = False
        # WATCH 모드 예비 종목 갱신 주기 (스코어링 기반)
        self._last_reserve_refresh: float = 0.0
        self._RESERVE_INTERVAL: float = 10.0   # 10초마다 예비 top-2 재선정

        # 거래대금 '9시(장시작) 대비' 증가율 — 종목별 당일 최초 관측값(설정: pre_filter_time 이후·양수)을 기준
        self._amt_baseline_date: Optional[date] = None
        self._amt_baseline: dict[str, int] = {}

        self._connect_realtime_signal()

    def _roll_amt_baseline_date(self) -> None:
        t = date.today()
        if self._amt_baseline_date != t:
            self._amt_baseline_date = t
            self._amt_baseline.clear()

    def _touch_trade_amt_baseline(self, code: str, amt: int) -> None:
        """기준 시각(pre_filter_time) 이후 해당 종목의 최초 양수 거래대금을 당일 기준으로 고정."""
        self._roll_amt_baseline_date()
        if code in self._amt_baseline or amt <= 0:
            return
        if datetime.now().time() < self.cfg.pre_filter_time:
            return
        self._amt_baseline[code] = amt

    def _trade_amount_diag(self, code: str, amt: int) -> str:
        """Pre-Filter 등 로그용: 조·억 표기 + 9시대비 증가율."""
        a = int(amt or 0)
        self._touch_trade_amt_baseline(code, a)
        ta = format_trade_amount_korean(a)
        gr = format_trade_amount_growth(a, self._amt_baseline.get(code))
        return f"거래대금 {ta} · {gr}"

    # -----------------------------------------------------------------------
    # 시작 / 정지
    # -----------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        all_codes = self._fetch_all_codes()
        logger.info("전 종목 %d개 수집", len(all_codes))

        # ③ 터미널 뷰 시작
        self.display.start()

        # 1단계 예약
        # 현재 시각이 09:00~15:20 사이면 즉시 실행, 아니면 내일 09:00 예약
        now = datetime.now().time()
        market_start = self.cfg.pre_filter_time  # 이미 dtime 타입
        market_end = dtime(15, 30, 0)

        if market_start <= now <= market_end:
            logger.info("현재 시각이 장시간(%s~%s) — Pre-Filter 즉시 실행",
                       self.cfg.pre_filter_time, "15:30")
            self._run_pre_filter()
        else:
            secs = self._seconds_until(self.cfg.pre_filter_time)
            t = threading.Timer(secs, self._run_pre_filter)
            t.daemon = True
            t.start()
            logger.info("Pre-Filter %.0f초 후 실행 예약", secs)

        # 2단계 루프
        self._scan_thread = threading.Thread(
            target=self._realtime_loop, daemon=True, name="ScanLoop"
        )
        self._scan_thread.start()

    def stop(self) -> None:
        self._running = False
        self.display.stop()
        self.store.export_csv(os.path.join(self.cfg.log_dir, "snapshot_final.csv"))
        logger.info("SmartScanner 정지 — 스냅샷 저장 완료")

    # -----------------------------------------------------------------------
    # 1단계: Pre-Filter
    # -----------------------------------------------------------------------

    def _run_pre_filter(self) -> None:
        logger.info(
            "▶ [1단계] Pre-Filter 시작 — opt10030 상위 %d종목 수집 → 필터 후 감시 %d종목",
            self.cfg.collect_raw_top_n, self.cfg.watch_pool_max,
        )
        scan_log.info("PRE_FILTER_START\t%s", datetime.now().strftime("%H:%M:%S"))

        rows = self._fetch_top_volume_rows(target=self.cfg.collect_raw_top_n)
        rows, _ = filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        _n0 = len(rows)
        rows = [r for r in rows if float(r.get("change_pct", 0) or 0) < mc]
        if _n0 != len(rows):
            logger.info(
                "  등락률 상한 %.1f%% 미만만 유지 — %d → %d종목",
                mc, _n0, len(rows),
            )
        rows = apply_watch_pool_cap(rows, self.cfg.watch_pool_max)
        if not rows:
            logger.warning("  ⚠ Pre-Filter — 필터 후 종목 없음, Pre-Filter 생략")
            return

        logger.info("  📊 감시 후보 %d종목 (순수 주식·거래대금 상위·등락률 < %.1f%%)", len(rows), mc)

        # ① DataFrame 에 일괄 적재
        self.top_mgr.clear()
        self.store.bulk_update(rows)

        for idx, row in enumerate(rows, 1):
            self.top_mgr.update(row["code"], row["trade_amount"])
            change_pct = row.get("change_pct", 0)

            log_msg = (
                f"{self._trade_amount_diag(row['code'], int(row.get('trade_amount') or 0))} / "
                f"등락률 {change_pct:+.2f}% / "
                f"현재가 {row.get('current_price', 0):,}원"
            )

            ScannerLogger.passed(
                row["code"], row.get("name", ""), "PRE_FILTER", log_msg
            )

            if idx % 10 == 0 or idx <= 5:
                logger.info("  ✓ [%3d] %s(%s) %s",
                           idx, row.get("name", "")[:10], row["code"], log_msg)

        top_codes = self.top_mgr.get_top_codes()
        self.watch_q.refresh(top_codes)
        self._prefiltered = True

        ScannerLogger.pre_filter_summary(
            total=len(rows), passed=len(top_codes),
            top_n=self.cfg.watch_pool_max,
        )
        logger.info("▶ [1단계] Pre-Filter 완료 — %d→%d종목 선정", len(rows), len(top_codes))
        for i, code in enumerate(top_codes[:10], 1):
            snap = self.store.get_snapshot(code)
            if snap:
                logger.info("  🎯 [%2d순] %s(%s) %s원", i, snap.name[:10], snap.code, f"{snap.current_price:,}")

    # -----------------------------------------------------------------------
    # 2단계: Real-time Scan 루프
    # -----------------------------------------------------------------------

    def _realtime_loop(self) -> None:
        logger.info("▶ [2단계] Real-time Scan 시작")
        while self._running:
            t0 = time.monotonic()
            if self._prefiltered:
                if self._universe_paused:
                    # ====== WATCH 모드 ======
                    # Tier 1(보유 5개): 현재가 갱신은 _on_receive_real_data + order_manager 처리
                    #   → 여기서는 아무것도 하지 않음 (0.1초 sleep만)
                    # Tier 2(예비 2개): 10초마다 스코어링으로 최신화
                    if t0 - self._last_reserve_refresh >= self._RESERVE_INTERVAL:
                        self._refresh_reserve_codes()
                        self._last_reserve_refresh = t0
                else:
                    # ====== SEARCH 모드 ======
                    # Tier 3 전체(~110개): 매 사이클 _evaluate() 실행
                    for code in list(self.watch_q.subscribed):
                        snap = self.store.get_snapshot(code)
                        if snap:
                            self._evaluate(snap)
            elapsed = time.monotonic() - t0
            # WATCH 모드: 0.1초(초정밀 대기) / SEARCH 모드: 기본 주기(1초)
            interval = 0.1 if self._universe_paused else self.cfg.scan_interval
            time.sleep(max(0.0, interval - elapsed))

    def _evaluate(self, snap: StockSnapshot) -> None:
        # ① 유니버스 감시 중단 — 포지션 풀 시 신규 신호 판단 차단
        if self._universe_paused:
            return

        # ② 등락률 상한 (기존)
        if snap.change_pct >= self.cfg.max_change_pct:
            return

        # ② 시간 필터 — check_jdm_entry에도 동일 적용(이중 차단으로 조기 return)
        now = datetime.now().time()
        if not (self.cfg.entry_start_time <= now <= self.cfg.entry_end_time):
            return

        # ③ 시가 돌파 + 신고가 경신
        r_open = check_jdm_open_breakout(snap)
        if r_open is None:
            return

        # ④ MA20 이격도 — 데이터 부족 시 bypass(None은 조인에서 제외)
        r_disp = check_disparity_from_ma(snap, max_pct=self.cfg.max_disparity_pct)

        # ⑤ JDM: 시간·거래량급증·체결강도·골든크로스·MA이격·RSI≥하한 (통합)
        r_jdm = check_jdm_entry(snap, self.cfg)
        if r_jdm is None:
            return

        reason = " | ".join(r for r in [r_open, r_disp, r_jdm] if r)
        self._emit(ScanSignal(snap.code, snap.name, "JDM_ENTRY", snap.current_price, reason))

    # -----------------------------------------------------------------------
    # 3단계: Final Signal
    # -----------------------------------------------------------------------

    def _emit(self, sig: ScanSignal) -> None:
        # ② 파일 로그
        ScannerLogger.signal(sig)
        logger.warning("🚨 [3단계] %s(%s) [%s] %s", sig.name, sig.code,
                       sig.signal_type, sig.reason)
        # ③ 터미널 알림
        self.display.alert(sig)

        if self.on_signal:
            self.on_signal(sig)

    # -----------------------------------------------------------------------
    # 실시간 데이터 콜백
    # -----------------------------------------------------------------------

    def _connect_realtime_signal(self) -> None:
        self._kiwoom._ocx.OnReceiveRealData.connect(self._on_receive_real_data)

    def _on_receive_real_data(
        self, code: str, real_type: str, real_data: str
    ) -> None:
        if real_type not in ("주식체결",):
            return

        def fid(n: int) -> str:
            return self._kiwoom._ocx.dynamicCall(
                "GetCommRealData(QString, int)", [code, n]
            )

        try:
            from kiwoom_api import safe_int, safe_float
            price = safe_int(fid(10))
            vol   = safe_int(fid(13))
            amt   = safe_int(fid(14)) * 1000   # 거래대금: 천원 단위 → 원
            high  = safe_int(fid(17))
            low   = safe_int(fid(18))
            open_ = safe_int(fid(16))
            pct   = safe_float(fid(12))
            strength = safe_float(fid(20))    # [NEW] FID 20: 체결강도

            if price <= 0:
                return   # 유효하지 않은 체결 데이터

            # ① DataFrame 갱신 (API 재호출 없음)
            self.store.update_price(
                code=code, current_price=price, high_price=high,
                low_price=low, open_price=open_, volume=vol,
                trade_amount=amt, change_pct=pct,
            )
            self._touch_trade_amt_baseline(code, amt)
            self.top_mgr.update(code, amt)

            # [NEW] 체결강도 저장 (FID 20)
            if strength > 0:
                self.store.update_chejan_strength(code, strength)

            # [NEW] 포지션 종목 현재가 실시간 반영 (손절/익절 정확도 개선)
            if self._order_mgr and code in self._order_mgr.positions and price > 0:
                self._order_mgr.positions[code].current_price = price

            # watch_q.refresh — SetRealReg/Remove를 매 틱 호출하면 API 과부하
            # 30초 간격으로만 구독 목록을 갱신한다 (유니버스 감시 중단 중은 스킵)
            now_t = time.monotonic()
            if not self._universe_paused and now_t - self._last_watchq_refresh >= self._WATCHQ_INTERVAL:
                self.watch_q.refresh(self.top_mgr.get_top_codes())
                self._last_watchq_refresh = now_t

        except Exception as e:
            logger.debug("실시간 파싱 오류 — %s: %s", code, e)

    # -----------------------------------------------------------------------
    # 헬퍼
    # -----------------------------------------------------------------------

    def _fetch_all_codes(self) -> list[str]:
        codes = []
        for m in self.cfg.markets:
            raw = self._kiwoom._ocx.dynamicCall(
                "GetCodeListByMarket(QString)", [m]
            )
            codes.extend(c for c in raw.strip().split(";") if c)
        return codes

    def _fetch_top_trade_amount(self, count: int) -> list[dict]:
        """하위 호환용 — _fetch_top_volume_rows 위임"""
        return self._fetch_top_volume_rows(target=min(count, self.cfg.collect_raw_top_n))

    def _fetch_top_volume_rows(
        self,
        target: int = 200,
        on_progress: Optional[Callable] = None,
        retry: int = 2,
    ) -> list[dict]:
        """
        거래대금 상위 조회 — opt10030 (KiwoomManager.fetch_opt10030_top_volume).

        200종목 근처는 보통 TR 2회(연속조회) + 레이트리미터 ~0.5s 수준.
        """
        logger.info("[opt10030] 거래대금 상위 조회 시작 (목표 %d종목)", target)
        if on_progress:
            on_progress("거래대금 상위 조회", 0, target, "opt10030 조회 중...")

        for attempt in range(retry):
            try:
                if hasattr(self._kiwoom, "fetch_opt10030_top_volume"):
                    rows = self._tr_q.call(self._kiwoom.fetch_opt10030_top_volume, target)
                else:
                    rows = self._tr_q.call(self._do_fetch_opt10030)
                    rows = rows[:target]
                logger.info("[opt10030] 응답 %d행 (목표 %d)", len(rows), target)

                if rows:
                    result = rows[:target]
                    logger.info("[opt10030] 최종 %d종목 확보", len(result))
                    if on_progress:
                        on_progress("거래대금 상위 조회", len(result), target,
                                    f"{len(result)}종목 확보")
                    return result

            except Exception as e:
                logger.warning("[opt10030] 조회 실패 (attempt %d): %s", attempt + 1, e)

        # opt10030 결과 없을 때 — 코스피 시총 상위 종목으로 대체
        logger.warning("[opt10030] 실제 조회 실패 — 시총 상위 종목으로 대체")
        fallback = [
            {"code": "005930", "name": "삼성전자",        "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "000660", "name": "SK하이닉스",       "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "207940", "name": "삼성바이오로직스",  "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "005380", "name": "현대차",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "373220", "name": "LG에너지솔루션",   "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "000270", "name": "기아",             "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "035420", "name": "NAVER",            "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "051910", "name": "LG화학",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "006400", "name": "삼성SDI",          "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
            {"code": "035720", "name": "카카오",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0},
        ]
        logger.info("[opt10030] 대체 종목 %d개 사용", len(fallback))
        return fallback[:target]

    def _do_fetch_opt10030(self) -> list[dict]:
        """opt10030 CommRqData 호출 → rows 반환"""
        logger.debug("[opt10030] CommRqData 호출")
        self._kiwoom._set_input("시장구분",     "0")  # 0=전체
        self._kiwoom._set_input("정렬구분",     "1")  # 1=거래대금 내림차순
        self._kiwoom._set_input("관리종목포함", "0")  # 0=제외
        self._kiwoom._set_input("신용구분",     "0")  # 0=전체
        self._kiwoom._comm_rq("opt10030", "거래대금상위", "9000")
        rows = self._kiwoom._tr_data.get("rows", [])
        logger.debug("[opt10030] 응답 %d행", len(rows))
        return rows

    def run_periodic_scan(self, on_progress=None) -> list:
        """
        1분마다 호출하는 전체 스캔 사이클.

        1. opt10030 으로 거래대금 상위 collect_raw_top_n 종목 조회(필요 시 연속조회)
        2. 우선주·ETF 제거 후 거래대금 상위 watch_pool_max 만 스냅샷·감시에 유지
        3. 테스타 정배열 + 장동민 시가돌파 필터링
        4. 통과 종목을 final_targets(ScanSignal 리스트)로 반환

        Args:
            on_progress: 진행 콜백 — on_progress(phase, current, total, detail)
        """
        def _prog(phase, current, total, detail=""):
            if on_progress:
                on_progress(phase, current, total, detail)

        # ① WATCH 모드(포지션 풀)이면 opt10030 호출 자체를 스킵
        if self._universe_paused:
            logger.info("[주기 스캔] WATCH 모드 — opt10030 스캔 스킵 (SetRealReg 감시 중)")
            return []

        logger.info("=" * 60)
        logger.info("[주기 스캔] 시작 — %s", datetime.now().strftime("%H:%M:%S"))
        _prog("거래대금 상위 조회", 0, self.cfg.collect_raw_top_n, "opt10030 조회 중...")

        # 연결 확인
        if hasattr(self._kiwoom, 'is_connected') and not self._kiwoom.is_connected():
            logger.warning("[주기 스캔] 연결 끊김 — 스킵")
            return []

        # 1. opt10030 조회(연속조회) → 필터 → 우선주·ETF 제외 → 거래대금 상위 watch_pool_max 유지
        rows = self._fetch_top_volume_rows(
            target=self.cfg.collect_raw_top_n, on_progress=on_progress,
        )
        rows, _ = filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        _n0 = len(rows)
        rows = [r for r in rows if float(r.get("change_pct", 0) or 0) < mc]
        if _n0 != len(rows):
            logger.info(
                "[주기 스캔] 등락률 상한 %.1f%% 미만만 유지 — %d → %d종목",
                mc, _n0, len(rows),
            )
        rows = apply_watch_pool_cap(rows, self.cfg.watch_pool_max)
        if not rows:
            logger.warning("[주기 스캔] 필터 후 종목 없음 — 중단")
            return []

        _prog("거래대금 상위 조회", len(rows), self.cfg.watch_pool_max,
              f"{len(rows)}종목 감시 후보")

        logger.info(
            "[주기 스캔] 감시 후보 %d종목 (수집 %d → 등락 <%.1f%%·상위 %d)",
            len(rows), self.cfg.collect_raw_top_n, mc, self.cfg.watch_pool_max,
        )

        # 2. SnapshotStore / TopVolumeManager 갱신
        self.top_mgr.clear()
        logger.info("[주기 스캔] STEP-A: bulk_update 시작 (%d행)", len(rows))
        self.store.bulk_update(rows)
        logger.info("[주기 스캔] STEP-B: bulk_update 완료")

        for row in rows:
            _c = row["code"]
            _a = int(row.get("trade_amount") or 0)
            self._touch_trade_amt_baseline(_c, _a)
            self.top_mgr.update(_c, _a)
        logger.info("[주기 스캔] STEP-C: top_mgr 갱신 완료")

        # 감시·선정용 코드 목록은 SnapshotStore(이번 스캔·유니버스필터 반영)만 사용한다.
        # TopVolumeManager 는 실시간 틱으로 과거 종목이 누적되어 스냅샷과 불일치할 수 있음(예: 99 vs 36).
        _watch_df = self.store.top_by_trade_amount(self.cfg.watch_pool_max)
        top_codes = _watch_df.index.tolist() if not _watch_df.empty else []
        logger.info(
            "[주기 스캔] STEP-D: top_codes %d개 (스냅샷 기준, 순수 주식만)",
            len(top_codes),
        )

        # STEP-E: SetRealReg 를 이벤트루프 다음 사이클로 위임
        # — dynamicCall 내부에서 Windows 메시지 처리 → OCX 재진입 데드락 방지
        # — 유니버스 감시 중단 중은 스킵
        _reg_codes = top_codes[:self.cfg.realtime_sub_max]
        logger.info("[주기 스캔] STEP-E: watch_q.refresh 예약 (구독대상=%d)", len(_reg_codes))
        if not self._universe_paused:
            QTimer.singleShot(0, lambda c=_reg_codes: self.watch_q.refresh(c))
        logger.info("[주기 스캔] STEP-F: watch_q.refresh %s", "스킵(감시중단)" if self._universe_paused else "예약 완료")

        self._prefiltered = True
        logger.info("[주기 스캔] STEP-G: prefiltered=True")

        # STEP-H: 분봉 초기 로딩 — 데이터 부족 종목을 비동기(QTimer 체인)로 처리
        # ⚠️  메인 스레드에서 TR 을 동기 루프로 호출하면 UI 가 수십 초 얼어붙음.
        #     QTimer.singleShot 체인으로 한 종목씩 분산 처리한다.
        _CANDLE_MIN_BARS = 55   # MA50 에 필요한 최소 분봉 수
        _CANDLE_LOAD_MAX = 12   # 한 스캔 사이클당 최대 예약 종목 수
        codes_need = [
            code for code in top_codes
            if len(self.store._mins.get(code, [])) < _CANDLE_MIN_BARS
        ][:_CANDLE_LOAD_MAX]

        if codes_need:
            logger.info(
                "[주기 스캔] STEP-H: 1분봉 비동기 로딩 예약 (%d종목) — "
                "350ms 간격으로 순차 처리, UI 블로킹 없음",
                len(codes_need),
            )
            QTimer.singleShot(500, lambda c=list(codes_need): self._load_candles_async(c, 0))
        else:
            logger.info("[주기 스캔] STEP-H: 분봉 데이터 충분 — 초기 로딩 스킵")

        # 진단 로그: bulk_update 이후 거래대금 상위 N종 샘플 (N=diagnostic_sample_n)
        _dn = max(1, int(self.cfg.diagnostic_sample_n))
        sample = self.store.top_by_trade_amount(_dn)
        if not sample.empty:
            for code_s, row_s in sample.iterrows():
                _amt = int(row_s.get("trade_amount", 0))
                _ta = format_trade_amount_korean(_amt)
                _gr = format_trade_amount_growth(_amt, self._amt_baseline.get(str(code_s)))
                logger.info(
                    "[진단] %s(%s) 현재가=%s 거래대금=%s · %s 거래량=%s",
                    row_s.get("name", "?"), code_s,
                    f"{int(row_s.get('current_price', 0)):,}",
                    _ta, _gr,
                    f"{float(row_s.get('volume', 0)):,.0f}",
                )
            logger.info(
                "[진단] 안내 — 위 %d종은 거래대금 상위 샘플이다. 실제 감시·스냅샷 후보는 최대 %d종, "
                "ScannerWorker 신호 판단은 상위 %d종에서 수행된다.",
                _dn,
                self.cfg.watch_pool_max,
                self.cfg.display_top_n,
            )
        else:
            logger.warning("[진단] top_by_trade_amount 결과 없음 — 파싱 필드명 불일치 가능성")
            # rank 기반 샘플 확인
            with self.store._lock:
                df_sample = self.store._df.head(_dn)
            if not df_sample.empty:
                logger.warning("[진단] DataFrame 직접 샘플: %s", df_sample[["trade_amount","volume","rank"]].to_dict())

        logger.info("[주기 스캔] SnapshotStore 갱신 완료 (%d종목)", len(rows))

        # 3. 신호 판단은 _realtime_loop()의 _evaluate()에서 백그라운드 스레드가 담당.
        #    주기 스캔은 데이터 갱신(opt10030 + SnapshotStore)만 수행하고 종료.
        #    (과거 TESTA+JDM 필터 루프 제거 — 110종목 동기 루프가 메인 스레드를 차단하던 원인)
        logger.info("[주기 스캔] 완료 — 신호 판단은 실시간 워커(_evaluate)에 위임")
        logger.info("=" * 60)
        _prog("감시종목 갱신", len(top_codes), len(top_codes), "데이터 갱신 완료")
        return []

    # -----------------------------------------------------------------------
    # 포지션 실시간 현재가 갱신 (손절/익절 정확도 개선)
    # -----------------------------------------------------------------------

    _SCREEN_POSITION = "9210"   # 포지션 종목 전용 스크린 (watch_q의 9200과 분리)

    def add_position_realtime(self, code: str) -> None:
        """포지션 종목 실시간 현재가 구독 (별도 스크린 9210)"""
        try:
            self._kiwoom._ocx.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                [self._SCREEN_POSITION, code, "10;12", "1"],
            )
            logger.info("[포지션 실시간] 등록 — %s", code)
        except Exception as e:
            logger.warning("[포지션 실시간] 등록 실패 — %s: %s", code, e)

    def remove_position_realtime(self, code: str) -> None:
        """포지션 종목 실시간 구독 해제"""
        try:
            self._kiwoom._ocx.dynamicCall(
                "SetRealRemove(QString, QString)", [self._SCREEN_POSITION, code]
            )
            logger.info("[포지션 실시간] 해제 — %s", code)
        except Exception as e:
            logger.warning("[포지션 실시간] 해제 실패 — %s: %s", code, e)

    def pause_universe_watch(self, position_codes: list[str]) -> None:
        """포지션 풀 — 유니버스 감시를 보유 종목 + 임시 예비 2개로 축소.

        이후 _realtime_loop이 10초마다 _refresh_reserve_codes()로 예비를 스코어 기반 최신화.
        """
        self._universe_paused = True
        self._last_reserve_refresh = 0.0   # 첫 루프에서 즉시 스코어링 갱신 유도
        # 초기 예비: 스코어링 전 임시로 거래대금 상위 2개
        reserve = [c for c in self.top_mgr.get_top_codes() if c not in position_codes][:2]
        self.watch_q.refresh(position_codes + reserve)
        logger.info(
            "[Watch] WATCH 모드 진입 — 보유 %d개 + 임시예비 %d개 구독 (10초 후 스코어링 갱신)",
            len(position_codes), len(reserve),
        )

    def resume_universe_watch(self) -> None:
        """슬롯 생김 — 유니버스 감시 전체 복원."""
        self._universe_paused = False
        top = self.top_mgr.get_top_codes()
        self.watch_q.refresh(top)
        logger.info("[Watch] 유니버스 감시 재개 — 상위 %d종목 구독", len(top))

    # -----------------------------------------------------------------------
    # WATCH 모드 — 예비 종목 스코어링
    # -----------------------------------------------------------------------

    def _score_candidate(self, snap: "StockSnapshot") -> float:
        """예비 종목 점수 계산. 0이면 불합격 (진입 조건 미충족).

        기준:
        - 등락률 > 0, < max_change_pct (상승 중이되 과열 아님)
        - 현재가 > 시가 (시가 돌파 유지)
        - 등락률 점수(0~100) + 체결강도 보너스(0~20) 합산
        """
        if snap.current_price <= 0 or snap.open_price <= 0:
            return 0.0
        if snap.change_pct <= 0:
            return 0.0
        if snap.change_pct >= self.cfg.max_change_pct:
            return 0.0
        if snap.current_price <= snap.open_price:
            return 0.0

        score = min(snap.change_pct, 10.0) * 10.0                          # 등락률 (최대 100점)
        score += min(max(snap.chejan_strength - 100.0, 0.0), 100.0) * 0.2  # 체결강도 보너스 (최대 20점)
        return score

    def _refresh_reserve_codes(self) -> None:
        """WATCH 모드 전용 — _RESERVE_INTERVAL마다 예비 top-2를 실시간 점수로 최신화.

        TR 호출 없이 메모리(top_mgr + SnapshotStore)만 사용.
        상위 30개 후보에서 스코어링 후 가장 좋은 2개를 watch_q에 유지.
        """
        if not self._universe_paused:
            return

        pos_codes: set[str] = set()
        if self._order_mgr:
            pos_codes = set(self._order_mgr.positions.keys())

        if not pos_codes:
            return

        # top_mgr 상위 30개 중 보유 제외 → 스코어링
        candidates = [c for c in self.top_mgr.get_top_codes() if c not in pos_codes][:30]
        scored: list[tuple[float, str]] = []
        for code in candidates:
            snap = self.store.get_snapshot(code)
            if snap is not None:
                s = self._score_candidate(snap)
                if s > 0.0:
                    scored.append((s, code))

        scored.sort(reverse=True)
        new_reserve = [c for _, c in scored[:2]]

        # 현재 구독 중인 예비 목록과 비교 (보유 제외)
        old_reserve = [c for c in self.watch_q.subscribed if c not in pos_codes]

        if set(new_reserve) != set(old_reserve):
            self.watch_q.refresh(list(pos_codes) + new_reserve)
            logger.info(
                "[Watch] 예비 갱신 — %s → %s (점수: %s)",
                old_reserve or "없음",
                new_reserve or "없음",
                [f"{c}:{s:.0f}점" for s, c in scored[:2]],
            )
        else:
            logger.debug("[Watch] 예비 유지 — %s", new_reserve)

    def _load_candles_async(self, codes: list, idx: int) -> None:
        """
        분봉 초기 로딩을 QTimer.singleShot 체인으로 1종목씩 비동기 처리한다.

        메인 스레드에서 동기 루프로 여러 TR 을 연속 호출하면 UI 가 얼어붙는다.
        각 종목을 350ms 간격 체인으로 분산시켜 이벤트 루프가 살아있게 유지한다.
        """
        if idx >= len(codes):
            logger.info("[STEP-H async] 완료 — 총 %d종목 처리", len(codes))
            return

        code = codes[idx]
        try:
            candles = self._tr_q.call(self._kiwoom.get_min_candles, code, 1, 70)
            closes = [c["close"] for c in reversed(candles) if c.get("close")]
            if closes:
                self.store.set_min_candles(code, closes)
                logger.debug("[STEP-H async] %s 1분봉 %d개 로딩 완료", code, len(closes))
            else:
                logger.debug("[STEP-H async] %s 응답 없음 — 스킵", code)
        except Exception as e:
            logger.warning("[STEP-H async] %s 1분봉 로딩 실패: %s", code, e)

        # 다음 종목을 350ms 후 처리 (TR 간격 0.25s + 여유 100ms)
        QTimer.singleShot(350, lambda: self._load_candles_async(codes, idx + 1))

    # _init_min_candles_for_top 제거됨 (2025-03 최적화)
    # SetRealReg 실시간 틱이 SnapshotStore.update_price()에서
    # 분봉을 자동 누적하므로 opt10080 TR 호출 불필요.

    @staticmethod
    def _seconds_until(t: dtime) -> float:
        now    = datetime.now()
        target = now.replace(hour=t.hour, minute=t.minute,
                             second=t.second, microsecond=0)
        return max(0.0, (target - now).total_seconds())
