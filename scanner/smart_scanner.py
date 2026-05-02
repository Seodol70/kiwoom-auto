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


import csv
import heapq
import json
import logging
import logging.handlers
import os
import threading
import time
from collections import deque as _Deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, ClassVar, Optional


import pandas as pd


from scanner.universe import is_ordinary_stock, is_pure_equity_name, filter_equity_rows
from PyQt5.QtCore import QTimer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# 로거 설정
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)     # 일반 로거 (콘솔)


class _WinSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Windows 호환 RotatingFileHandler.


    표준 RotatingFileHandler는 파일 회전 시 os.rename을 사용하는데,
    Windows에서는 다른 프로세스(log_monitor, VS Code 등)가 scanner.log를
    열고 있으면 PermissionError(WinError 32)가 발생한다.


    이 핸들러는 rename 대신 shutil.copy2 + truncate 방식으로 회전해
    파일이 읽기 모드로 열려 있는 상태에서도 안전하게 동작한다.
    """


    def doRollover(self) -> None:
        import shutil


        # 현재 스트림 닫기
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]


        # 백업 파일 순환: .N → .N+1 (backupCount-1 → backupCount, …, 1 → 2)
        # 백업→백업 이동은 해당 파일을 아무도 열지 않으므로 rename OK
        for i in range(self.backupCount - 1, 0, -1):
            sfn = self.rotation_filename(f"{self.baseFilename}.{i}")
            dfn = self.rotation_filename(f"{self.baseFilename}.{i + 1}")
            if os.path.exists(sfn):
                if os.path.exists(dfn):
                    os.remove(dfn)
                os.rename(sfn, dfn)


        # 현재 로그 → .1 : rename 대신 copy + truncate
        dfn = self.rotation_filename(f"{self.baseFilename}.1")
        if os.path.exists(dfn):
            os.remove(dfn)
        if os.path.exists(self.baseFilename):
            shutil.copy2(self.baseFilename, dfn)          # 복사
            with open(self.baseFilename, "w",             # 원본 비우기
                      encoding=self.encoding or "utf-8"):
                pass


        if not self.delay:
            self.stream = self._open()




def _build_scan_logger(log_dir: str = "logs") -> logging.Logger:
    """scanner.log 전용 로거를 생성한다."""
    os.makedirs(log_dir, exist_ok=True)
    scan_log = logging.getLogger("scanner.audit")
    scan_log.setLevel(logging.DEBUG)
    scan_log.propagate = False   # 루트 로거로 전파 금지


    handler = _WinSafeRotatingFileHandler(
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
    거래대금(원)을 읽기 편한 한글 형식으로 표기.


    예시:
    - 487,000,000,000원 → "4,870억" (조 미포함) 또는 "0.487조"
    - 1,234,000,000,000원 → "1.2조 340억" (조 포함 시 소수점)
    - 12,340,000원 → "1,234만원"
    """
    try:
        n = int(amount_won)
    except (TypeError, ValueError):
        return "0원"
    if n <= 0:
        return "0원"


    jo = n // _JO_WON  # 1조 = 1,000,000,000,000
    rem = n % _JO_WON
    eok_int = rem // _EOK_WON  # 1억 = 100,000,000


    parts: list[str] = []


    # 조 단위 표기 (1조 이상)
    if jo > 0:
        if eok_int > 0:
            # 조와 억을 함께 표시 (예: "1.2조 340억")
            jo_decimal = jo + eok_int / 1_0000  # 1조 + n억을 소수점으로
            parts.append(f"{jo_decimal:.1f}조")
        else:
            # 억이 없으면 조만 (예: "1조")
            parts.append(f"{jo}조")
    elif eok_int > 0:
        # 1조 미만이면 억으로 표시 (예: "1,234억")
        parts.append(f"{eok_int:,}억")
    else:
        # 1억 미만이면 만원, 원으로 표시
        man = n // 10_000
        if man > 0:
            return f"{man:,}만원"
        return f"{n:,}원"


    return " ".join(parts)




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


# [Extracted] SmartScannerConfig moved to scanner.config
from scanner.config import SmartScannerConfig






# ---------------------------------------------------------------------------
# TRRequestQueue — 키움 API 요청 간격 보장 (최대 4회/초)
# ---------------------------------------------------------------------------


# (Filtering logic moved to scanner.universe)






def apply_watch_pool_cap(rows: list[dict], watch_pool_max: int) -> list[dict]:
    """거래대금 내림차순으로 상위 watch_pool_max 종목만 유지. (레거시 — apply_universe_score_cap 사용 권장)"""
    if not rows:
        return []
    rows = sorted(
        rows,
        key=lambda r: int(r.get("trade_amount", 0) or 0),
        reverse=True,
    )
    return rows[:watch_pool_max]




def _vol_pace_ratio(today_vol: int, prev_volume: int) -> float:
    """
    거래량 페이스 비율 (시간대 편향 보정).


    단순 today_vol/prev_volume 은 장 초반(09:00~09:30)에 항상 극소값이 되어
    vol_ratio 가중치가 무의미해지는 문제가 있다.


    이를 해결하기 위해 '경과시간 대비 기대 거래량'으로 정규화한다:
        pace_ratio = today_vol / (prev_volume × elapsed_ratio)


    여기서 elapsed_ratio = 장 시작 후 경과 분 / 390(총 거래 시간 분).
    최소 5분 보정으로 장 정확히 열리는 순간의 분모 0 방지.


    해석: pace_ratio = 1.0 → 전일과 동일한 속도 / 2.0 → 전일의 2배 속도
    이 값은 시간대에 무관하게 동일한 의미를 가진다.


    Returns
    -------
    pace_ratio : float  (0.0 이면 계산 불가 — 중립 처리)
    """
    if prev_volume <= 0 or today_vol <= 0:
        return 0.0


    _MARKET_OPEN_MIN   = 9 * 60   # 09:00 분 기준
    _TOTAL_TRADING_MIN = 390      # 09:00 ~ 15:30
    _MIN_ELAPSED_MIN   = 5        # 극초반 분모 0 방어


    now = datetime.now().time()   # 모듈 최상위 from datetime import datetime
    now_min = now.hour * 60 + now.minute
    elapsed = max(now_min - _MARKET_OPEN_MIN, _MIN_ELAPSED_MIN)


    # 장외 시간(사전/사후)은 전체 거래 시간 기준으로 클램프
    elapsed = min(elapsed, _TOTAL_TRADING_MIN)
    elapsed_ratio = elapsed / _TOTAL_TRADING_MIN


    return today_vol / (prev_volume * elapsed_ratio)




def apply_universe_score_cap(
    rows: list[dict],
    watch_pool_max: int,
    cfg: "SmartScannerConfig",
    prev_volumes: dict[str, int],
) -> list[dict]:
    """
    거래대금 순위 × 거래량 페이스 × 등락률을 복합 스코어링해 상위 watch_pool_max 종목 반환.


    Hybrid score = trade_amt_score×w1 + vol_pace_score×w2 + chg_pct_score×w3


    vol_pace_score 는 거래량 페이스 비율(_vol_pace_ratio) 기반:
      - pace_ratio  = today_vol / (prev_volume × elapsed_ratio)
      - 장 초반이어도 "같은 시간대 기준 전일 대비 몇 배 속도인지"로 비교
      - min(pace_ratio / 3.0, 1.0)  — 전일 대비 3배 속도=만점
      - 전일 데이터 없으면 0.5 (중립)
    """
    if not rows:
        return []


    n = len(rows)
    w_amt = getattr(cfg, "universe_trade_amt_weight", 0.4)
    w_vol = getattr(cfg, "universe_vol_ratio_weight", 0.4)
    w_chg = getattr(cfg, "universe_chg_pct_weight",   0.2)


    # 거래대금 내림차순 순위 → 정규화 점수
    sorted_by_amt = sorted(rows, key=lambda r: int(r.get("trade_amount", 0) or 0), reverse=True)
    amt_rank: dict[str, float] = {}
    for i, r in enumerate(sorted_by_amt):
        # i=0 (1위) → 1.0, i=n-1 (꼴찌) → 0.0
        amt_rank[r["code"]] = 1.0 - (i / max(n - 1, 1))


    scored: list[tuple[float, dict]] = []
    for r in rows:
        code = r["code"]


        # ① 거래대금 스코어
        s_amt = amt_rank.get(code, 0.5)


        # ② 거래량 페이스 스코어 (시간대 편향 보정)
        pv = prev_volumes.get(code, 0)
        today_vol = int(r.get("volume", 0) or 0)
        pace = _vol_pace_ratio(today_vol, pv)
        if pace > 0:
            r["vol_ratio"]   = round(pace, 4)  # SnapshotStore에 시간 보정 배율 저장
            r["prev_volume"] = pv
            s_vol = min(pace / 3.0, 1.0)       # pace 3배 = 만점
        else:
            r["vol_ratio"]   = 0.0
            r["prev_volume"] = pv
            s_vol = 0.5                         # 전일 데이터 없으면 중립


        # ③ 등락률 스코어
        chg = float(r.get("change_pct", 0) or 0)
        s_chg = min(max(chg / 10.0, 0.0), 1.0)


        score = s_amt * w_amt + s_vol * w_vol + s_chg * w_chg
        scored.append((score, r))


    scored.sort(key=lambda x: x[0], reverse=True)
    result = [r for _, r in scored[:watch_pool_max]]
    logger.debug(
        "[유니버스스코어] pool %d→%d, top5: %s",
        n, len(result),
        [(r["code"], round(sc, 3)) for sc, r in scored[:5]],
    )
    return result






# [Extracted] TRRequestQueue moved to scanner.queue
from scanner.queue import TRRequestQueue




# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------


# [Extracted] StockSnapshot moved to scanner.models
from scanner.models import StockSnapshot




# [Extracted] ScanSignal moved to scanner.models
from scanner.models import ScanSignal






# ---------------------------------------------------------------------------
# ① SnapshotStore — pandas DataFrame 캐시
# ---------------------------------------------------------------------------


from scanner.snapshot_store import SnapshotStore  # re-export (호환성)




# ┌────────────────────────────────────────────────────────────┐
# │ [Phase 2] SnapshotStore class moved to scanner/snapshot_store.py  │
# └────────────────────────────────────────────────────────────┘


# [Phase 2] SnapshotStore class extracted to scanner/snapshot_store.py


from scanner.scanner_logger import ScannerLogger  # re-export (호환성)




# ---------------------------------------------------------------------------
# ③ ScannerDisplay — rich 터미널 뷰
# ---------------------------------------------------------------------------


_CONSOLE = Console()




# [Extracted] ScannerDisplay moved to scanner.display
from scanner.display import ScannerDisplay




# ---------------------------------------------------------------------------
# TopVolumeManager — 거래대금 상위 N 종목 관리
# ---------------------------------------------------------------------------




# [Extracted] TopVolumeManager moved to scanner.top_volume
from scanner.top_volume import TopVolumeManager




# ---------------------------------------------------------------------------
# PriorityWatchQueue — SetRealReg 구독 관리
# ---------------------------------------------------------------------------




# [Extracted] PriorityWatchQueue moved to scanner.queue
from scanner.queue import PriorityWatchQueue




# ---------------------------------------------------------------------------
# 신호 판단 함수 (순수 함수)
# ---------------------------------------------------------------------------




# [Extracted] Signal evaluation functions moved to scanner.signal_evaluator
from scanner.signal_evaluator import (
    check_breakout, check_testa_alignment, check_jdm_open_breakout,
    check_volume_surge, check_chejan_strength, check_disparity_from_ma,
    check_ema20_filter, check_bullish_engulfing, check_bullish_pin_bar,
    check_breakout_gate, check_pre_surge, check_opening_surge,
    check_opening_scalp, check_eod_entry, check_jdm_entry,
    check_pullback_entry
)




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


        # 첫 스캔 시 전체 종목 1분봉 일괄 로딩 플래그
        # True가 되면 이후 사이클은 12종목/사이클 제한으로 복귀
        self._initial_candle_load_done: bool = False


        self.on_signal:        Optional[Callable[[ScanSignal], None]] = None
        self.on_index_update:  Optional[Callable[[str, float, float], None]] = None  # (idx_code, current, chg_pct)


        # 포지션 현재가 실시간 업데이트용 (MainWindow에서 주입)
        self._order_mgr = None


        # watch_q.refresh 쓰로틀 — SetRealReg를 매 틱 호출 방지 (30초 간격)
        self._last_watchq_refresh: float = 0.0
        self._WATCHQ_INTERVAL: float = 30.0


        # [NEW] 일봉 데이터 갱신 쓰로틀 (2026-04-03)
        self._last_daily_update: float = 0.0
        self._daily_update_interval_sec: float = self.cfg.daily_candle_refresh_min * 60.0  # 분 → 초
        self._daily_refresh_pending: list = []   # MainWindow QTimer 체인이 소비할 갱신 대기 목록


        # 동적 감시 중단: 포지션 풀(max_positions)시 유니버스 감시를 보유종목만으로 축소
        self._universe_paused: bool = False


        # 캔들 마감 게이팅: 분이 바뀔 때만 _evaluate() 실행 (틱 기반 고점 진입 방지)
        self._eval_min: dict[str, int] = {}
        # WATCH 모드 예비 종목 갱신 주기 (스코어링 기반)
        self._last_reserve_refresh: float = 0.0
        self._RESERVE_INTERVAL: float = 10.0   # 10초마다 예비 top-2 재선정


        # 거래대금 '9시(장시작) 대비' 증가율 — 종목별 당일 최초 관측값(설정: pre_filter_time 이후·양수)을 기준
        self._amt_baseline_date: Optional[date] = None
        self._amt_baseline: dict[str, int] = {}
        # opt10030 직전 성공 결과 캐시 — 실패 시 하드코딩 대체 대신 이전 결과 재사용
        self._last_volume_rows: list[dict] = []
        self._last_volume_updated: float = 0.0  # 마지막 캐시 갱신 시각 (time.monotonic)
        self._opt10030_fetching: bool = False   # opt10030 중복 호출 방지 플래그
        # 전일 거래량 캐시 — hybrid universe score vol_ratio 계산용
        # {code: prev_volume}; 매일 장 마감(15:20) save_prev_volumes() 로 갱신
        self._prev_volumes: dict[str, int] = {}
        self._load_prev_volumes()
        # 동일 종목/신호 중복 emit 방지 (signal_cooldown_sec)
        self._last_signal_ts: dict[tuple[str, str], float] = {}


        self._connect_realtime_signal()


    # ── 전일 거래량 캐시 save/load ────────────────────────────────────────────


    def _prev_volumes_path(self) -> Path:
        return Path("logs") / "prev_volumes.json"


    def _load_prev_volumes(self) -> None:
        """
        logs/prev_volumes.json 에서 전일 거래량 캐시를 로드.
        파일에 저장된 날짜가 오늘이거나 4일 이상 지난 경우 사용하지 않는다.
        """
        # json 은 모듈 최상위에서 이미 임포트됨
        path = self._prev_volumes_path()
        if not path.exists():
            logger.info("[prev_volumes] 캐시 파일 없음 — vol_ratio 중립(0.5)으로 동작")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            saved_date_str = data.get("date", "")
            saved_date = date.fromisoformat(saved_date_str) if saved_date_str else None
            today = date.today()
            if saved_date is None:
                logger.warning("[prev_volumes] 날짜 정보 없음 — 캐시 무효")
                return
            if saved_date >= today:
                logger.info("[prev_volumes] 저장 날짜(%s)가 오늘 이후 — 무효 (장중 저장본)", saved_date)
                return
            age_days = (today - saved_date).days
            if age_days > 4:
                logger.warning("[prev_volumes] 캐시가 %d일 경과 — 무효 (주말/연휴 등)", age_days)
                return
            volumes: dict = data.get("volumes", {})
            self._prev_volumes = {k: int(v) for k, v in volumes.items() if int(v or 0) > 0}
            logger.info("[prev_volumes] 로드 완료 — %d종목 (%s 기준)", len(self._prev_volumes), saved_date)
        except Exception as e:
            logger.warning("[prev_volumes] 로드 실패: %s", e)


    def save_prev_volumes(self) -> None:
        """
        현재 SnapshotStore의 거래량을 전일 거래량으로 저장 (15:20 강제청산 시 호출).
        저장 형식: {"date": "YYYY-MM-DD", "volumes": {"code": volume, ...}}
        """
        # json 은 모듈 최상위에서 이미 임포트됨
        try:
            with self.store._lock:
                snap_df = self.store._df.copy()
            if snap_df.empty or "volume" not in snap_df.columns:
                logger.warning("[prev_volumes] 스냅샷 데이터 없음 — 저장 스킵")
                return
            volumes = {
                str(code): int(row["volume"])
                for code, row in snap_df.iterrows()
                if int(row.get("volume", 0) or 0) > 0
            }
            path = self._prev_volumes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": date.today().isoformat(), "volumes": volumes}, f, ensure_ascii=False)
            logger.info("[prev_volumes] 저장 완료 — %d종목 (%s)", len(volumes), date.today())
            self._prev_volumes = volumes  # 메모리 캐시도 갱신
        except Exception as e:
            logger.warning("[prev_volumes] 저장 실패: %s", e)


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
        rows = apply_universe_score_cap(rows, self.cfg.watch_pool_max, self.cfg, self._prev_volumes)
        if not rows:
            logger.warning("  ⚠ Pre-Filter — 필터 후 종목 없음, Pre-Filter 생략")
            return


        logger.info("  📊 감시 후보 %d종목 (순수 주식·hybrid스코어 상위·등락률 < %.1f%%)", len(rows), mc)


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


            # [개선] 10개마다 1개씩만 로그 기록 — 로그 양 90% 감소, 속도 개선
            if idx % 10 == 0 or idx <= 5:
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


        # ① 캔들 마감 게이팅 — 분이 바뀔 때만 평가 (틱 기반 고점 진입 방지)
        cur_min = datetime.now().minute
        if self._eval_min.get(snap.code, -1) == cur_min:
            return
        self._eval_min[snap.code] = cur_min


        # ② 등락률 상한
        if snap.change_pct >= self.cfg.max_change_pct:
            return


        # ② 시간 필터
        now = datetime.now().time()
        if not (self.cfg.entry_start_time <= now <= self.cfg.entry_end_time):
            return


        # ②-bis 요셉 시그널 추세 단계 갱신 (분 단위 1회)
        if getattr(self.cfg, "yosep_trend_enabled", True):
            from scanner.indicator_service import IndicatorService
            trend_level = IndicatorService.get_trend_status(
                closes=list(snap.closes_1min or []),
                highs=list(snap.highs_1min or []),
                lows=list(snap.lows_1min or []),
                volumes=list(snap.volumes_1min or []),
                ema_period=int(getattr(self.cfg, "yosep_ema_period", 20)),
                atr_period=int(getattr(self.cfg, "yosep_atr_period", 14)),
                volume_lookback=int(getattr(self.cfg, "yosep_volume_lookback", 20)),
            )
            snap.trend_prev_level = int(getattr(snap, "trend_level", 0))
            snap.trend_level = int(trend_level)
            self.store.update_trend_level(snap.code, trend_level)


        enabled = set(getattr(self.cfg, "enabled_strategies", ("BREAKOUT", "JDM_ENTRY")) or ())
        order = tuple(getattr(self.cfg, "strategy_order", ("BREAKOUT", "JDM_ENTRY")) or ())


        # strategy_order를 따르되 enabled에 없는 항목은 스킵.
        # 모든 전략이 비활성/미설정이면 안전하게 종료.
        for strategy in order:
            if strategy not in enabled:
                continue


            sig: Optional[ScanSignal] = None
            if strategy == "BREAKOUT":
                sig = self._build_breakout_signal(snap)
            elif strategy == "JDM_ENTRY":
                sig = self._build_jdm_signal(snap)
            elif strategy == "PULLBACK":
                sig = self._build_pullback_signal(snap)
            else:
                logger.debug("[Strategy] 알 수 없는 전략명 스킵 — %s", strategy)
                continue


            if sig is not None:
                sig.trend_level = int(getattr(snap, "trend_level", 0))
                sig.trend_prev_level = int(getattr(snap, "trend_prev_level", 0))
                self._emit(sig)
                # 같은 분 다중 전략 동시 진입 방지: 우선순위 첫 통과 전략만 발행
                return


    def _build_breakout_signal(self, snap: StockSnapshot) -> Optional[ScanSignal]:
        """BREAKOUT 전략 평가 후 통과 시 ScanSignal을 반환한다."""
        # trend_level에 따라 고점 필터 동적 조정 (2026-04-23)
        # 상승 추세 종목은 고점에서 조금 하락해도 진입 허용, 약한 추세는 엄격하게
        trend_level = int(getattr(snap, "trend_level", 0))
        if trend_level >= 2:  # 상승 추세
            pullback_threshold = 5.0   # 고점 대비 5% 하락까지만 차단
        elif trend_level == 1:  # 중간 추세
            pullback_threshold = 3.0   # 고점 대비 3% 하락까지만 차단
        else:  # trend_level == 0 (약한 추세)
            pullback_threshold = self.cfg.breakout_pullback_from_high_pct  # 기본값 2.5%


        r_breakout = check_breakout(
            snap,
            breakout_ratio=self.cfg.breakout_ratio,
            volume_mult=self.cfg.breakout_volume_mult,
            pullback_from_high_pct=pullback_threshold,
            min_rising_bars=self.cfg.breakout_min_rising_bars,
        )
        if not r_breakout:
            return None


        r_gate = check_breakout_gate(snap, self.cfg)
        if not r_gate:
            return None


        reason = " | ".join(r for r in [r_breakout, r_gate] if r)
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        return ScanSignal(
            snap.code, snap.name, "BREAKOUT", snap.current_price, reason,
            entry_candle_low=candle_low,
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
        )


    def _build_jdm_signal(self, snap: StockSnapshot) -> Optional[ScanSignal]:
        """JDM_ENTRY 전략 평가 후 통과 시 ScanSignal을 반환한다."""
        # EMA20 필터 — 현재가가 20분 EMA 위에 있어야 진입 (추세 상승 확인)
        r_ema20 = check_ema20_filter(snap)
        if r_ema20 is None:
            return None


        # MA20 이격도 — 데이터 부족 시 bypass(None은 조인에서 제외)
        r_disp = check_disparity_from_ma(snap, max_pct=self.cfg.max_disparity_pct)


        # JDM 통합 게이트
        r_jdm = check_jdm_entry(snap, self.cfg)
        if r_jdm is None:
            return None


        reason = " | ".join(r for r in [r_ema20, r_disp, r_jdm] if r)
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        # 일봉 맥락 — TP 상향 여부 판단
        from strategy.jang_dong_min import get_daily_context as _gdc
        _dctx = _gdc(snap.daily_closes, snap.current_price,
                     float(getattr(self.cfg, "daily_near_high_threshold_pct", 3.0)))
        return ScanSignal(
            snap.code, snap.name, "JDM_ENTRY", snap.current_price, reason,
            entry_candle_low=candle_low,
            near_daily_high=_dctx["near_high"],
            daily_ma20=_dctx["daily_ma20"],
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
        )

    def _build_pullback_signal(self, snap: StockSnapshot) -> Optional[ScanSignal]:
        """PULLBACK_ENTRY 전략 평가 후 통과 시 ScanSignal을 반환한다."""
        r_pullback = check_pullback_entry(snap, self.cfg)
        if r_pullback is None:
            return None
            
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        return ScanSignal(
            snap.code, snap.name, "PULLBACK", snap.current_price, r_pullback,
            entry_candle_low=candle_low,
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
        )


    # -----------------------------------------------------------------------
    # 3단계: Final Signal
    # -----------------------------------------------------------------------


    def _emit(self, sig: ScanSignal) -> None:
        # 동일 종목/신호 재발행 쿨다운
        now_ts = time.monotonic()
        cooldown = float(getattr(self.cfg, "signal_cooldown_sec", 0.0) or 0.0)
        key = (sig.code, sig.signal_type)
        last_ts = self._last_signal_ts.get(key, 0.0)
        if cooldown > 0 and (now_ts - last_ts) < cooldown:
            logger.debug(
                "[SignalCooldown] %s(%s) [%s] 스킵 — %.1fs < %.1fs",
                sig.name, sig.code, sig.signal_type, (now_ts - last_ts), cooldown,
            )
            return
        self._last_signal_ts[key] = now_ts


        # ② 파일 로그
        ScannerLogger.signal(sig)
        logger.warning("🚨 [3단계] %s(%s) [%s] %s", sig.name, sig.code,
                       sig.signal_type, sig.reason)
        # ③ 터미널 알림
        self.display.alert(sig)


        if self.on_signal:
            if threading.current_thread() is threading.main_thread():
                self.on_signal(sig)
            else:
                # ScanLoop 스레드 → 메인 스레드로 안전 위임
                QTimer.singleShot(0, lambda s=sig: self.on_signal(s))


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
            # [FIX] FID 14는 "누적거래금액"이 아니라 "현재 틱의 거래금액"
            # → opt10030의 누적 거래대금을 보존하기 위해 실시간 업데이트 제외
            high  = safe_int(fid(17))
            low   = safe_int(fid(18))
            open_ = safe_int(fid(16))
            pct   = safe_float(fid(12))
            strength_raw = safe_float(fid(20))    # [NEW] FID 20: 체결강도
            # FID 20은 일부 상황에서 실제값의 100배로 반환됨 (e.g., 91818 → 918.18%)
            # 10000 이상이면 100으로 나눠서 정규화
            strength = strength_raw / 100.0 if strength_raw >= 10000.0 else strength_raw


            if price <= 0:
                return   # 유효하지 않은 체결 데이터


            # ① DataFrame 갱신 (API 재호출 없음)
            # trade_amount는 opt10030의 누적값을 유지 (FID 14는 현재 틱만 포함)
            self.store.update_price(
                code=code, current_price=price, high_price=high,
                low_price=low, open_price=open_, volume=vol,
                trade_amount=None,  # ← 거래대금은 opt10030 값만 사용
                change_pct=pct,
            )
            snap_now = self.store.get_snapshot(code)
            amt = int(snap_now.trade_amount) if snap_now else 0
            self._touch_trade_amt_baseline(code, amt)
            self.top_mgr.update(code, amt)


            # [NEW] 체결강도 저장 (FID 20)
            if strength > 0:
                self.store.update_chejan_strength(code, strength)


            # [NEW] 포지션 종목 현재가 실시간 반영 (손절/익절 정확도 개선) [Phase 1] position_repo 경유로 변경
            if self._order_mgr and hasattr(self._order_mgr, "position_repo") and price > 0:
                self._order_mgr.position_repo.update_price(code, price)
            elif self._order_mgr and code in self._order_mgr.positions and price > 0:
                # 폴백 (position_repo 없을 경우 — 호환성)
                self._order_mgr.positions[code].current_price = price
            if self._order_mgr and code in self._order_mgr.positions and snap_now is not None and hasattr(self._order_mgr, "update_position_trend"):
                self._order_mgr.update_position_trend(code, int(getattr(snap_now, "trend_level", 0)))


            # watch_q.refresh — SetRealReg/Remove를 매 틱 호출하면 API 과부하
            # 30초 간격으로만 구독 목록을 갱신한다 (유니버스 감시 중단 중은 스킵)
            now_t = time.monotonic()
            if not self._universe_paused and now_t - self._last_watchq_refresh >= self._WATCHQ_INTERVAL:
                self.watch_q.refresh(self.top_mgr.get_top_codes())
                self._last_watchq_refresh = now_t


        except Exception as e:
            logger.debug("실시간 파싱 오류 — %s: %s", code, e)


    def _handle_index_realtime(self, idx_code: str) -> None:
        """지수 로직 비활성화 — 장초반 TR 부하 제거"""
        return


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


    def _bg_fetch_opt10030(self) -> None:
        """
        백그라운드에서 opt10030을 갱신 (메인 스레드 블로킹 회피).


        QTimer.singleShot으로 메인 스레드 이벤트 루프 이후에 실행되므로,
        이전 주기의 스캔 결과 처리가 완료된 후 opt10030 갱신 시작.
        """
        if self._opt10030_fetching:
            logger.debug("[opt10030 BG] 이미 갱신 중 — 스킵")
            return


        logger.debug("[opt10030 BG] 백그라운드 갱신 시작")
        try:
            was_empty = not self._last_volume_rows  # 최초 캐시 여부
            rows = self._fetch_top_volume_rows(target=self.cfg.collect_raw_top_n, retry=1)
            logger.info("[opt10030 BG] 갱신 완료 — %d종목 캐시", len(rows))
            # 시작 시 캐시가 비어있다가 처음 채워졌으면 즉시 재스캔 — 5종목 → 400종목 즉시 반영
            if was_empty and rows:
                logger.info("[opt10030 BG] 최초 캐시 완료 → 즉시 재스캔 예약")
                QTimer.singleShot(200, self.run_periodic_scan)
        except Exception as e:
            logger.warning("[opt10030 BG] 갱신 실패: %s", e)


    def _fetch_top_volume_rows(
        self,
        target: int = 200,
        on_progress: Optional[Callable] = None,
        retry: int = 2,
    ) -> list[dict]:
        """
        거래대금 상위 조회 — opt10030 (KiwoomManager.fetch_opt10030_top_volume).


        target=400 기준 TR 약 4회(연속조회) + 레이트리미터 각 0.25s → 합계 ~1~2s 수준.


        [2026-04-23] 최적화:
        - 중복 호출 방지: 이미 fetching 중이면 캐시 우선 반환
        - 캐시 우선: 5분 이내 갱신된 캐시는 즉시 반환 (메인 스레드 블로킹 회피)
        """
        now = time.monotonic()
        cache_age = now - self._last_volume_updated


        # ① 이미 fetching 중이면 캐시 우선 (중복 호출 차단)
        if self._opt10030_fetching:
            if self._last_volume_rows:
                logger.info("[opt10030] 진행 중 — 캐시 %d종목 (나이 %.1fs)",
                           len(self._last_volume_rows), cache_age)
                return self._last_volume_rows[:target]
            else:
                logger.warning("[opt10030] 진행 중인데 캐시 없음 — 대기")
                # 캐시가 없으면 fallback까지 기다림 (밑으로 진행)


        # ② 최근 5분 이내 갱신된 캐시 있으면 즉시 반환 (메인 스레드 블로킹 회피)
        if self._last_volume_rows and cache_age < 300.0:  # 5분
            logger.info("[opt10030] 캐시 재사용 (나이 %.1fs, %d종목)", cache_age, len(self._last_volume_rows))
            return self._last_volume_rows[:target]


        # ③ 실제 조회 필요 — 플래그 설정 후 진행
        logger.info("[opt10030] 거래대금 상위 조회 시작 (목표 %d종목, 캐시나이 %.1fs)", target, cache_age)
        if on_progress:
            on_progress("거래대금 상위 조회", 0, target, "opt10030 조회 중...")


        self._opt10030_fetching = True
        try:
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
                        self._last_volume_rows = result
                        self._last_volume_updated = time.monotonic()
                        if on_progress:
                            on_progress("거래대금 상위 조회", len(result), target,
                                        f"{len(result)}종목 확보")
                        return result


                except Exception as e:
                    logger.warning("[opt10030] 조회 실패 (attempt %d): %s", attempt + 1, e)
        finally:
            self._opt10030_fetching = False


        # opt10030 결과 없을 때 — 직전 성공 결과 재사용 (캐시 없을 때만 하드코딩 대체)
        if self._last_volume_rows:
            logger.warning("[opt10030] 실패 — 직전 스캔 결과 %d종목 재사용 (나이 %.1fs)",
                           len(self._last_volume_rows), cache_age)
            return self._last_volume_rows[:target]


        logger.warning("[opt10030] 실제 조회 실패 — 시총 상위 종목으로 대체 (캐시 없음, 최초 실패)")
        fallback = [
            {"code": "005930", "name": "삼성전자",        "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "000660", "name": "SK하이닉스",       "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "207940", "name": "삼성바이오로직스",  "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "005380", "name": "현대차",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "373220", "name": "LG에너지솔루션",   "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "000270", "name": "기아",             "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "035420", "name": "NAVER",            "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "051910", "name": "LG화학",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "006400", "name": "삼성SDI",          "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "035720", "name": "카카오",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
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


    def _log_store_health(self) -> None:
        """SnapshotStore 상태를 5분마다 한 번 로깅 (Zone 6)."""
        _now = time.monotonic()
        if _now - getattr(self, "_store_health_last", 0.0) < 300.0:
            return
        self._store_health_last = _now


        try:
            with self.store._lock:
                _codes_idx   = list(self.store._df.index)   # DataFrame index = 등록 종목
                _n_codes     = len(_codes_idx)
                _n_mins      = len(self.store._mins)        # 1분봉 데이터 보유 종목 수
                _n_tick_vols = len(getattr(self.store, "_tick_ts_vol", {}))
                _n_sectors   = len(getattr(self.store, "_sector_cache", {}))
                _codes_no_1m = [
                    c for c in _codes_idx
                    if len(self.store._mins.get(c, [])) < 5
                ]
            logger.info(
                "[스토어헬스] 종목=%d 1분봉보유=%d 틱Vel=%d 섹터캐시=%d "
                "1분봉5개미만=%d개%s",
                _n_codes, _n_mins, _n_tick_vols, _n_sectors,
                len(_codes_no_1m),
                f" {_codes_no_1m[:5]}" if _codes_no_1m else "",
            )
        except Exception as _e:
            logger.debug("[스토어헬스] 수집 실패: %s", _e)


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
            self._log_store_health()
            return []


        logger.info("=" * 60)
        logger.info("[주기 스캔] 시작 — %s", datetime.now().strftime("%H:%M:%S"))
        self._log_store_health()


        # 연결 확인
        if hasattr(self._kiwoom, 'is_connected') and not self._kiwoom.is_connected():
            logger.warning("[주기 스캔] 연결 끊김 — 스킵")
            return []


        # 1. opt10030 조회 — 메인 스레드 블로킹 방지를 위해 캐시 우선 사용 + 백그라운드 갱신
        # [2026-04-23] 최적화: 캐시가 있으면 즉시 반환 후 백그라운드에서 갱신
        #              캐시 없으면 fallback 대체 + 백그라운드 갱신 시작
        _prog("거래대금 상위 조회", 0, self.cfg.collect_raw_top_n, "opt10030 조회 중...")


        # 캐시된 결과 또는 즉시 조회 (run_periodic_scan은 백그라운드 스레드이므로 동기 OK)
        cache_age = time.monotonic() - self._last_volume_updated
        if self._last_volume_rows and cache_age < 300.0:  # 5분 이내 캐시
            rows = self._last_volume_rows[:]
            logger.info("[주기 스캔] 캐시된 opt10030 결과 사용 (나이 %.1fs, %d종목)", cache_age, len(rows))
        else:
            # 캐시 없음 또는 만료: 즉시 opt10030 조회 (백그라운드 스레드이므로 블로킹 무해)
            logger.info("[주기 스캔] opt10030 즉시 조회 (캐시나이 %.1fs)", cache_age)
            rows = self._fetch_top_volume_rows(
                target=self.cfg.collect_raw_top_n,
                on_progress=_prog
            )
            if not rows:
                # 조회 실패 시에만 fallback (최후의 수단)
                logger.warning("[주기 스캔] opt10030 조회 실패 — fallback 5대장 대체")
                rows = [
                    {"code": "005930", "name": "삼성전자",        "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
                    {"code": "000660", "name": "SK하이닉스",       "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
                    {"code": "207940", "name": "삼성바이오로직스",  "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
                    {"code": "005380", "name": "현대차",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
                    {"code": "373220", "name": "LG에너지솔루션",   "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
                ]
        rows, _ = filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        _n0 = len(rows)
        rows = [r for r in rows if float(r.get("change_pct", 0) or 0) < mc]
        if _n0 != len(rows):
            logger.info(
                "[주기 스캔] 등락률 상한 %.1f%% 미만만 유지 — %d → %d종목",
                mc, _n0, len(rows),
            )
        rows = apply_universe_score_cap(rows, self.cfg.watch_pool_max, self.cfg, self._prev_volumes)
        if not rows:
            logger.warning("[주기 스캔] 필터 후 종목 없음 — 중단")
            return []


        _prog("거래대금 상위 조회", len(rows), self.cfg.watch_pool_max,
              f"{len(rows)}종목 감시 후보")


        logger.info(
            "[주기 스캔] 감시 후보 %d종목 (수집 %d → 등락 <%.1f%%·hybrid스코어 상위 %d)",
            len(rows), self.cfg.collect_raw_top_n, mc, self.cfg.watch_pool_max,
        )


        # 2. SnapshotStore / TopVolumeManager 갱신
        self.top_mgr.clear()
        logger.debug("[주기 스캔] STEP-A: bulk_update 시작 (%d행)", len(rows))
        self.store.bulk_update(rows)
        logger.debug("[주기 스캔] STEP-B: bulk_update 완료")


        for row in rows:
            _c = row["code"]
            _a = int(row.get("trade_amount") or 0)
            self._touch_trade_amt_baseline(_c, _a)
            self.top_mgr.update(_c, _a)
        logger.debug("[주기 스캔] STEP-C: top_mgr 갱신 완료")


        # 감시·선정용 코드 목록은 SnapshotStore(이번 스캔·유니버스필터 반영)만 사용한다.
        # TopVolumeManager 는 실시간 틱으로 과거 종목이 누적되어 스냅샷과 불일치할 수 있음(예: 99 vs 36).
        _watch_df = self.store.top_by_trade_amount(self.cfg.watch_pool_max)
        top_codes = _watch_df.index.tolist() if not _watch_df.empty else []
        logger.debug(
            "[주기 스캔] STEP-D: top_codes %d개 (스냅샷 기준, 순수 주식만)",
            len(top_codes),
        )


        # STEP-E: SetRealReg 를 이벤트루프 다음 사이클로 위임
        # — dynamicCall 내부에서 Windows 메시지 처리 → OCX 재진입 데드락 방지
        # — 유니버스 감시 중단 중은 스킵
        _reg_codes = top_codes[:self.cfg.realtime_sub_max]
        logger.debug("[주기 스캔] STEP-E: watch_q.refresh 예약 (구독대상=%d)", len(_reg_codes))
        if not self._universe_paused:
            QTimer.singleShot(0, lambda c=_reg_codes: self.watch_q.refresh(c))
        logger.debug("[주기 스캔] STEP-F: watch_q.refresh %s", "스킵(감시중단)" if self._universe_paused else "예약 완료")


        self._prefiltered = True
        logger.debug("[주기 스캔] STEP-G: prefiltered=True")


        # STEP-H: 분봉 초기 로딩 — 데이터 부족 종목을 비동기(QTimer 체인)로 처리
        # ⚠️  메인 스레드에서 TR 을 동기 루프로 호출하면 UI 가 수십 초 얼어붙음.
        #     QTimer.singleShot 체인으로 한 종목씩 분산 처리한다.
        _CANDLE_MIN_BARS = 55   # MA50 에 필요한 최소 분봉 수
        _CANDLE_LOAD_MAX = 6    # 이후 사이클당 최대 예약 종목 수 (12→6, TR 경합 감소)
        codes_need_all = [
            code for code in top_codes
            if len(self.store._mins.get(code, [])) < _CANDLE_MIN_BARS
        ]


        if not self._initial_candle_load_done:
            # 첫 스캔: 제한 없이 전체 종목 일괄 로딩 (장 시작부터 누적된 데이터 확보)
            codes_need = codes_need_all
            if codes_need:
                logger.info(
                    "[주기 스캔] STEP-H: 첫 스캔 — 전체 %d종목 1분봉 일괄 로딩 시작 "
                    "(350ms 간격 체인, 이후 사이클은 %d종목/회로 복귀)",
                    len(codes_need), _CANDLE_LOAD_MAX,
                )
        else:
            # 이후 사이클: 12종목/사이클 제한 유지 (신규 편입 종목만 처리)
            codes_need = codes_need_all[:_CANDLE_LOAD_MAX]


        if codes_need:
            if self._initial_candle_load_done:
                logger.debug(
                    "[주기 스캔] STEP-H: 1분봉 비동기 로딩 예약 (%d종목) — "
                    "350ms 간격으로 순차 처리, UI 블로킹 없음",
                    len(codes_need),
                )
            QTimer.singleShot(500, lambda c=list(codes_need): self._load_candles_async(c, 0))
        else:
            logger.debug("[주기 스캔] STEP-H: 분봉 데이터 충분 — 초기 로딩 스킵")


        # 진단 로그: bulk_update 이후 거래대금 상위 N종 샘플 (N=diagnostic_sample_n)
        _dn = max(1, int(self.cfg.diagnostic_sample_n))
        sample = self.store.top_by_trade_amount(_dn)
        if not sample.empty:
            for code_s, row_s in sample.iterrows():
                _amt = int(row_s.get("trade_amount", 0))
                _ta = format_trade_amount_korean(_amt)
                _gr = format_trade_amount_growth(_amt, self._amt_baseline.get(str(code_s)))
                logger.debug(
                    "[진단] %s(%s) 현재가=%s 거래대금=%s · %s 거래량=%s",
                    row_s.get("name", "?"), code_s,
                    f"{int(row_s.get('current_price', 0)):,}",
                    _ta, _gr,
                    f"{float(row_s.get('volume', 0)):,.0f}",
                )
            logger.debug(
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


        # [일봉 갱신] 5분 주기 — 후보 코드만 계산해 _daily_refresh_pending에 저장.
        # 실제 TR 호출(opt10081)은 MainWindow가 QTimer 체인으로 처리해 메인 스레드 블로킹 방지.
        now = time.time()
        if now - self._last_daily_update >= self._daily_update_interval_sec:
            self._last_daily_update = now
            _eod_chg_min = float(getattr(self.cfg, "eod_change_pct_min", 2.0))
            _eod_chg_max = float(getattr(self.cfg, "eod_change_pct_max", 10.0))
            _daily_refresh_max = 10
            with self.store._lock:
                _df_snap = self.store._df.copy()
            _eod_candidates = [
                c for c in top_codes
                if _eod_chg_min <= float(_df_snap.at[c, "change_pct"]
                                         if c in _df_snap.index else 0.0) <= _eod_chg_max
            ]
            _rest = [c for c in top_codes if c not in set(_eod_candidates)]
            self._daily_refresh_pending = (_eod_candidates + _rest)[:_daily_refresh_max]
            logger.info("[일봉갱신] %d종목 예약 (EOD후보%d+보완%d) — QTimer 체인으로 처리",
                        len(self._daily_refresh_pending),
                        min(len(_eod_candidates), _daily_refresh_max),
                        max(0, len(self._daily_refresh_pending) - len(_eod_candidates)))


        # 3. 신호 판단은 _realtime_loop()의 _evaluate()에서 백그라운드 스레드가 담당.
        #    주기 스캔은 데이터 갱신(opt10030 + SnapshotStore)만 수행하고 종료.
        #    (과거 TESTA+JDM 필터 루프 제거 — 110종목 동기 루프가 메인 스레드를 차단하던 원인)
        logger.info("[주기 스캔] 완료 — 신호 판단은 실시간 워커(_evaluate)에 위임")
        logger.info("=" * 60)


        # 1분봉 캐시 저장 — 5분 주기, 백그라운드 스레드에서 실행 (메인 스레드 I/O 블로킹 방지)
        if now - getattr(self, "_last_1min_cache_save", 0) >= 300:
            self._last_1min_cache_save = now
            import threading as _threading
            _threading.Thread(target=self.store.save_1min_cache, daemon=True).start()


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
            if not self._initial_candle_load_done:
                self._initial_candle_load_done = True
                logger.info("[STEP-H async] 첫 일괄 로딩 완료 — 이후 사이클 12종목/회 제한 복귀")
                # 캐시 저장 — 백그라운드 스레드 (메인 스레드 I/O 블로킹 방지)
                import threading as _threading
                def _save():
                    try:
                        self.store.save_1min_cache()
                        logger.info("[STEP-H async] 1분봉 캐시 파일 저장 완료")
                    except Exception as _e:
                        logger.warning("[STEP-H async] 1분봉 캐시 저장 실패: %s", _e)
                _threading.Thread(target=_save, daemon=True).start()
            return


        # _tr_busy 중이면 동일 종목을 최대 3회 재시도 후 다음으로 (cascade 방지)
        if getattr(self._kiwoom, "_tr_busy", False):
            retries = getattr(self, "_candle_retry_count", 0)
            if retries < 3:
                self._candle_retry_count = retries + 1
                logger.debug("[STEP-H async] TR 처리 중 — %s 재시도 %d/3", codes[idx], retries + 1)
                QTimer.singleShot(400, lambda: self._load_candles_async(codes, idx))
            else:
                self._candle_retry_count = 0
                logger.debug("[STEP-H async] TR 처리 중 — %s 재시도 초과, 다음으로", codes[idx])
                QTimer.singleShot(350, lambda: self._load_candles_async(codes, idx + 1))
            return
        self._candle_retry_count = 0


        code = codes[idx]


        # ① 파일 캐시 우선 확인 — 있으면 TR 호출 생략 (재시작/신규 편입 즉시 복구)
        cached_n = self.store.load_1min_for_code(code)
        if cached_n >= 55:
            logger.debug("[STEP-H async] %s 캐시에서 %d개 로딩 완료 — TR 스킵", code, cached_n)
            QTimer.singleShot(0, lambda: self._load_candles_async(codes, idx + 1))
            return


        # ② 캐시 없거나 부족 → opt10080 TR 호출 (direct, _tr_q 미사용 — cascade 방지)
        try:
            candles = self._kiwoom.get_min_candles(code, 1, 70)
            ohlc = [c for c in reversed(candles) if c.get("close")]
            if ohlc:
                self.store.set_min_candles_ohlc(code, ohlc)
                logger.debug("[STEP-H async] %s TR 1분봉 OHLC %d개 로딩 완료", code, len(ohlc))
            else:
                logger.debug("[STEP-H async] %s TR 응답 없음 — 스킵", code)
        except Exception as e:
            logger.warning("[STEP-H async] %s 1분봉 로딩 실패: %s", code, e)


        # 다음 종목을 350ms 후 처리 (TR 간격 0.25s + 여유 100ms)
        QTimer.singleShot(350, lambda: self._load_candles_async(codes, idx + 1))


    # _init_min_candles_for_top 제거됨 (2025-03 최적화)
    # SetRealReg 실시간 틱이 SnapshotStore.update_price()에서
    # 분봉을 자동 누적하므로 opt10080 TR 호출 불필요.


    # ── 수급 필터: opt10059 10분 주기 갱신 ────────────────────────────────────


    def trigger_investor_refresh(self) -> None:
        """
        메인 스레드 QTimer에서 호출 — 수급 데이터 갱신 시작점.
        watch pool 상위 investor_top_n 종목을 350ms 체인으로 순차 조회한다.
        (동기 루프 대신 QTimer.singleShot 체인 → UI 블로킹 없음)
        """
        if not self.cfg.investor_filter_enabled:
            return
        top_codes = (
            self.store.top_by_trade_amount(self.cfg.investor_top_n)
            .index.tolist()
        )
        if not top_codes:
            return
        logger.info("[수급갱신] %d종목 opt10059 갱신 시작", len(top_codes))
        QTimer.singleShot(0, lambda: self._refresh_investor_data_async(top_codes, 0))


    def _refresh_investor_data_async(self, codes: list, idx: int, retries: int = 0) -> None:
        """
        opt10059를 QTimer.singleShot 체인으로 1종목씩 비동기 처리한다.
        350ms 간격 → 최대 15종목 × 0.35s ≈ 5.25초 (TR 레이트 리미터 내).


        _tr_busy 시 최대 3회 재시도(800ms 간격), 초과 시 다음 종목으로 이동.
        _tr_q.call() 래퍼 사용 금지 — processEvents 중 스캔 타이머 발화로 인한
        cascading nested event loop 프리징 방지.
        """
        if idx >= len(codes):
            logger.info("[수급갱신] 완료 — %d종목 처리", len(codes))
            return


        # _tr_busy 시 같은 종목 재시도 (3회 한도) → 초과 시 skip
        if getattr(self._kiwoom, "_tr_busy", False):
            if retries < 3:
                logger.debug("[수급갱신] TR 처리 중 — %s 재시도 %d/3", codes[idx], retries + 1)
                QTimer.singleShot(
                    800, lambda: self._refresh_investor_data_async(codes, idx, retries + 1)
                )
            else:
                logger.debug("[수급갱신] TR 처리 중 — %s 재시도 초과, 다음 종목 이동", codes[idx])
                QTimer.singleShot(
                    500, lambda: self._refresh_investor_data_async(codes, idx + 1, 0)
                )
            return


        code = codes[idx]
        try:
            # _tr_q.call() 래퍼 제거 — processEvents cascade 방지
            # _comm_rq 내부의 TRRateLimiter가 rate limiting을 직접 처리함
            data = self._kiwoom.get_investor_trend(code)
            self.store.update_investor(code, data["foreign_net"], data["inst_net"])
            snap = self.store.get_snapshot(code)
            if snap:
                ScannerLogger.passed(
                    code, snap.name, "INVESTOR_REFRESH",
                    f"외국인={data['foreign_net']:+d} 기관={data['inst_net']:+d} "
                    f"score={snap.investor_score:+d}",
                )
        except Exception as e:
            logger.debug("[수급갱신] %s 실패: %s", code, e)


        QTimer.singleShot(350, lambda: self._refresh_investor_data_async(codes, idx + 1, 0))


    @staticmethod
    def _seconds_until(t: dtime) -> float:
        now    = datetime.now()
        target = now.replace(hour=t.hour, minute=t.minute,
                             second=t.second, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(0.0, (target - now).total_seconds())
