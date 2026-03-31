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
from datetime import datetime, time as dtime
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
    jdm_rsi_low:          float = 35.0       # 최적화됨: 30→35
    jdm_rsi_high:         float = 70.0
    jdm_take_profit_pct:  float = 3.0        # 익절 목표 (최적화됨: 4.0%→3.0%)
    jdm_stop_loss_pct:    float = -1.0       # 손절 기준 (최적화됨: -1.5%→-1.0%)
    markets:              tuple = ("0", "10")
    screen_realtime:      str   = "9200"
    display_top_n:        int   = 50    # 스캐너 UI 감시 테이블·Worker 상위 표시
    log_dir:              str   = "logs"
    # 등락률이 이 값 **이상**이면 감시·신호·매수 대상에서 제외 (config RISK.max_change_pct 와 동기화)
    max_change_pct:       float = 15.0


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
                mins = self._mins.setdefault(code, [])
                mins.append(float(current_price))
                if len(mins) > 120:
                    mins.pop(0)

    # ── 조회 ──────────────────────────────────────────────────────────────

    def get_snapshot(self, code: str) -> Optional[StockSnapshot]:
        """단일 종목 스냅샷을 반환한다 (API 호출 없음)."""
        with self._lock:
            if code not in self._df.index:
                return None
            row = self._df.loc[code]
            return StockSnapshot(
                code          = code,
                name          = str(row.get("name", "")),
                current_price = int(row.get("current_price", 0) or 0),
                open_price    = int(row.get("open_price",    0) or 0),
                high_price    = int(row.get("high_price",    0) or 0),
                low_price     = int(row.get("low_price",     0) or 0),
                volume        = int(row.get("volume",        0) or 0),
                trade_amount  = int(row.get("trade_amount",  0) or 0),
                prev_close    = int(row.get("prev_close",    0) or 0),
                change_pct    = float(row.get("change_pct",  0) or 0),
                closes_1min   = list(self._mins.get(code, [])),
                updated_at    = row.get("updated_at", datetime.now()),
            )

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
        table.add_column("거래대금(억)", justify="right", width=10)
        table.add_column("갱신시각", width=9)

        if top_df.empty:
            table.add_row(*["─"] * 8)
            return table

        for rank, (code, row) in enumerate(top_df.iterrows(), 1):
            change = float(row.get("change_pct", 0) or 0)
            if change > 0:
                pct_text = Text(f"+{change:.2f}%", style="bright_red")
            elif change < 0:
                pct_text = Text(f"{change:.2f}%",  style="bright_blue")
            else:
                pct_text = Text(f"{change:.2f}%",  style="white")

            price = int(row.get("current_price", 0) or 0)
            vol   = int(row.get("volume",        0) or 0)
            amt   = int(row.get("trade_amount",  0) or 0)
            upd   = row.get("updated_at", datetime.now())
            upd_s = upd.strftime("%H:%M:%S") if isinstance(upd, datetime) else "--:--:--"

            table.add_row(
                str(rank),
                str(code),
                str(row.get("name", "")),
                f"{price:,}",
                pct_text,
                f"{vol:,}",
                f"{amt / 1e8:.1f}",
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
                    [self._screen, code_list, "10;11;12;13;14;16;17;18", "1"],
                )
                self._subscribed.update(to_add)
                logger.debug("[PriorityWatchQueue] SetRealReg 배치 등록 %d종목", len(to_add))

    def _sub(self, code: str) -> None:
        self._kiwoom._ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            [self._screen, code, "10;11;12;13;14;16;17;18", "1"],
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


def check_jdm_entry(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    closes = snap.closes_1min
    need   = cfg.jdm_ma_long + 1

    if len(closes) < need:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM",
            f"1분봉 데이터 부족 ({len(closes)}/{need})",
        )
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
    rsi_ok = cfg.jdm_rsi_low < rsi < cfg.jdm_rsi_high

    if not golden:
        ScannerLogger.rejected(snap.code, snap.name, "JDM", "골든크로스 미충족")
        return None
    if not rsi_ok:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM",
            f"RSI 범위 이탈 ({rsi:.1f})",
        )
        return None

    reason = (
        f"MA골든크로스 MA{cfg.jdm_ma_short}={ma_s:.0f} / "
        f"MA{cfg.jdm_ma_long}={ma_l:.0f} | RSI={rsi:.1f}"
    )
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

        # watch_q.refresh 쓰로틀 — SetRealReg를 매 틱 호출 방지 (30초 간격)
        self._last_watchq_refresh: float = 0.0
        self._WATCHQ_INTERVAL: float = 30.0

        self._connect_realtime_signal()

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
            trade_billion = row["trade_amount"] / 1e8
            change_pct = row.get("change_pct", 0)

            log_msg = (f"거래대금 {trade_billion:.1f}억 / "
                      f"등락률 {change_pct:+.2f}% / "
                      f"현재가 {row.get('current_price', 0):,}원")

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
                for code in list(self.watch_q.subscribed):
                    # ① API 없이 store 에서 바로 읽음
                    snap = self.store.get_snapshot(code)
                    if snap:
                        self._evaluate(snap)
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, self.cfg.scan_interval - elapsed))

    def _evaluate(self, snap: StockSnapshot) -> None:
        if snap.change_pct >= self.cfg.max_change_pct:
            return
        reason = check_breakout(snap, self.cfg.breakout_ratio,
                                self.cfg.breakout_volume_mult)
        if reason:
            self._emit(ScanSignal(snap.code, snap.name,
                                  "BREAKOUT", snap.current_price, reason))
            return

        reason = check_jdm_entry(snap, self.cfg)
        if reason:
            self._emit(ScanSignal(snap.code, snap.name,
                                  "JDM_ENTRY", snap.current_price, reason))

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

            if price <= 0:
                return   # 유효하지 않은 체결 데이터

            # ① DataFrame 갱신 (API 재호출 없음)
            self.store.update_price(
                code=code, current_price=price, high_price=high,
                low_price=low, open_price=open_, volume=vol,
                trade_amount=amt, change_pct=pct,
            )
            self.top_mgr.update(code, amt)

            # watch_q.refresh — SetRealReg/Remove를 매 틱 호출하면 API 과부하
            # 30초 간격으로만 구독 목록을 갱신한다
            now_t = time.monotonic()
            if now_t - self._last_watchq_refresh >= self._WATCHQ_INTERVAL:
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
            self.top_mgr.update(row["code"], row.get("trade_amount", 0))
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
        _reg_codes = top_codes[:self.cfg.realtime_sub_max]
        logger.info("[주기 스캔] STEP-E: watch_q.refresh 예약 (구독대상=%d)", len(_reg_codes))
        QTimer.singleShot(0, lambda c=_reg_codes: self.watch_q.refresh(c))
        logger.info("[주기 스캔] STEP-F: watch_q.refresh 예약 완료")

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

        # 진단 로그: bulk_update 이후 샘플 확인
        sample = self.store.top_by_trade_amount(3)
        if not sample.empty:
            for code_s, row_s in sample.iterrows():
                logger.info("[진단] %s(%s) 현재가=%s 거래대금=%s 거래량=%s",
                            row_s.get("name", "?"), code_s,
                            f"{int(row_s.get('current_price', 0)):,}",
                            f"{int(row_s.get('trade_amount', 0)):,}",
                            f"{float(row_s.get('volume', 0)):,.0f}")
        else:
            logger.warning("[진단] top_by_trade_amount 결과 없음 — 파싱 필드명 불일치 가능성")
            # rank 기반 샘플 확인
            with self.store._lock:
                df_sample = self.store._df.head(3)
            if not df_sample.empty:
                logger.warning("[진단] DataFrame 직접 샘플: %s", df_sample[["trade_amount","volume","rank"]].to_dict())

        logger.info("[주기 스캔] SnapshotStore 갱신 완료 (%d종목)", len(rows))

        # 3. 분봉 데이터: SetRealReg 실시간 틱 누적 방식으로 자동 채워짐
        #    (opt10080 TR 호출 제거 — SnapshotStore.update_price()가 매 틱마다 누적)
        _prog("감시종목 선정", 0, len(top_codes), "신호 판단 중...")

        # 4. 필터링 (스냅샷은 이미 순수 주식만 — 종목명 재확인)
        final_targets = []
        testa_pass = 0
        open_pass = 0
        no_data_cnt = 0

        for code in top_codes:
            snap = self.store.get_snapshot(code)
            if snap is None:
                logger.debug("[주기 스캔] %s 스냅샷 없음 — 스킵", code)
                continue

            if not is_pure_equity_name(snap.name):
                logger.debug("[주기 스캔] %s — ETF·파생 등 제외", snap.name)
                continue

            if snap.change_pct >= self.cfg.max_change_pct:
                logger.debug(
                    "[주기 스캔] %s — 등락률 %.1f%% >= 상한 %.1f%% 제외",
                    snap.name, snap.change_pct, self.cfg.max_change_pct,
                )
                continue

            n_bars = len(snap.closes_1min)
            logger.debug("[주기 스캔] 종목 검사: %s(%s) 현재가=%d 시가=%d 1분봉=%d개",
                         snap.name, code, snap.current_price,
                         snap.open_price, n_bars)

            # 1분봉 데이터 부족하면 시가돌파만 체크 (정배열 생략)
            if n_bars < 50:
                no_data_cnt += 1
                if n_bars < 2:
                    continue  # 아예 없으면 스킵
                # 데이터 부족 시 시가돌파 조건만으로 후보 표시 (추천은 아님)
                breakout = check_jdm_open_breakout(snap)
                if breakout:
                    logger.debug("[시가돌파 후보] %s(%s) 1분봉부족(%d개) %s",
                                 snap.name, code, n_bars, breakout)
                continue

            # 정배열 체크 (데이터 충분할 때만)
            aligned = check_testa_alignment(snap)
            if not aligned:
                continue
            testa_pass += 1

            # 시가 돌파 체크
            breakout = check_jdm_open_breakout(snap)
            if not breakout:
                continue
            open_pass += 1

            reason = f"{aligned} | {breakout}"
            sig = ScanSignal(code, snap.name, "TESTA+JDM", snap.current_price, reason)
            ScannerLogger.signal(sig)
            final_targets.append(sig)
            logger.info("[최종선정] %s(%s) %s", snap.name, code, reason)

        logger.info("[주기 스캔] 완료 — 조회=%d / 데이터부족=%d / 정배열=%d / 시가돌파=%d / 최종선정=%d",
                    len(top_codes), no_data_cnt, testa_pass, open_pass, len(final_targets))
        logger.info("=" * 60)
        _prog("감시종목 선정", len(final_targets), len(top_codes),
              f"{len(final_targets)}종목 선정 완료")
        return final_targets

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
