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


from scanner.universe import (
    UniverseManager, is_ordinary_stock, is_pure_equity_name, filter_equity_rows,
    format_trade_amount_korean, apply_watch_pool_cap, apply_universe_score_cap
)
from scanner.trade_amount import TradeAmountHelper
from scanner.snapshot_store import SnapshotStore
from scanner.indicator_service import IndicatorService
from infra.db_manager import DatabaseManager
from kiwoom_api import safe_int, safe_float
from PyQt5.QtCore import QObject, pyqtSignal, QTimer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# 로거 설정
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)     # 일반 로거 (콘솔)


from scanner.scanner_logger import scan_log, ScannerLogger




# ---------------------------------------------------------------------------
# 거래대금 표기 (원 → 조·억 한글, 진단용 증가율)
# ---------------------------------------------------------------------------


_JO_WON = 1_000_000_000_000
_EOK_WON = 100_000_000






from scanner.universe import (
    UniverseManager, is_ordinary_stock, is_pure_equity_name, filter_equity_rows,
    apply_watch_pool_cap, apply_universe_score_cap
)
from scanner.trade_amount import (
    format_trade_amount_korean, format_trade_amount_growth, TradeAmountHelper
)




# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


# [Extracted] SmartScannerConfig moved to scanner.config
from scanner.config import SmartScannerConfig






# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------


# [Extracted] StockSnapshot moved to scanner.models
from scanner.models import StockSnapshot




# [Extracted] ScanSignal moved to scanner.models
from scanner.models import ScanSignal






# ---------------------------------------------------------------------------
# 시세/로그/디스플레이 컴포넌트 (외부 모듈)
# ---------------------------------------------------------------------------
from scanner.snapshot_store import SnapshotStore
from scanner.scanner_logger import ScannerLogger
from scanner.display import ScannerDisplay
from scanner.top_volume import TopVolumeManager
from scanner.queue import TRRequestQueue, PriorityWatchQueue


# (Logger import moved to top)




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
    check_pullback_entry, check_vwap_filter
)




# ---------------------------------------------------------------------------
# SmartScanner — 통합 오케스트레이터
# ---------------------------------------------------------------------------


class SmartScanner(QObject):
    """
    3단계 스마트 스캐너 (메모리 최적화 + 로그 + 터미널 뷰 통합).
    QObject를 상속받아 표준 pyqtSignal로 신호를 전달합니다.


    사용 예)
        scanner = SmartScanner(kiwoom)
        scanner.on_signal = lambda sig: order_module.execute(sig)
        scanner.start()
    """


    # ── Qt 시그널 ──────────────────────────────────────────────────────────
    signal_detected = pyqtSignal(object)  # ScanSignal 객체 전달
    price_updated = pyqtSignal(str, int, float, int)  # (code, price, pct, trend_level) — 포지션 현재가 갱신
    index_updated = pyqtSignal(str, float, float) # [NEW] (idx_name, price, pct) — 실시간 지수 갱신
    watch_list_updated = pyqtSignal(list) # [NEW] UI 갱신용 종목 리스트 (list[dict])

    def __init__(self, kiwoom, cfg: Optional[SmartScannerConfig] = None,
                 notification_mgr: Optional["NotificationManager"] = None,
                 on_signal_callback: Optional[callable] = None) -> None:
        super().__init__()
        self._kiwoom = kiwoom
        self.cfg     = cfg or SmartScannerConfig()
        self.notif_mgr = notification_mgr
        self._on_signal_callback = on_signal_callback  # 크로스 스레드 signal 대체용


        # ① DataFrame 캐시
        self.store   = SnapshotStore()

        # [NEW] 유니버스 관리자 (필터링/스코어링 전담)
        self.universe_mgr = UniverseManager(self.cfg)

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

        # [NEW] 전략 모듈 로드 (v3.0)
        from scanner.strategies.breakout import BreakoutStrategy
        from scanner.strategies.jdm_entry import JdmStrategy
        from scanner.strategies.pullback import PullbackStrategy
        from scanner.strategies.eod import EODStrategy
        from scanner.strategies.overheat_pullback import OverheatPullbackStrategy
        from scanner.strategies.gap_pullback import GapPullbackStrategy  # [2026-06-02] C전략
        self.strategy_map = {
            "BREAKOUT": BreakoutStrategy(),
            "JDM_ENTRY": JdmStrategy(),
            "PULLBACK": PullbackStrategy(),
            "GAP_PULLBACK": GapPullbackStrategy(),   # [2026-06-02] C전략: 갭 눌림목
            "EOD": EODStrategy(),
            "OVERHEAT_PULLBACK": OverheatPullbackStrategy(),
        }


        self._running     = False
        self._prefiltered = False
        self._scan_thread: Optional[threading.Thread] = None
        self._lock        = threading.Lock()

        # [FIX] UI 업데이트 큐 (스레드 분리)
        self._ui_queue: _Deque = _Deque(maxlen=2)  # 최신 데이터 + 1개 버퍼 (손실 방지)
        self._ui_update_timer = None

        # 첫 스캔 시 전체 종목 1분봉 일괄 로딩 플래그
        # True가 되면 이후 사이클은 12종목/사이클 제한으로 복귀
        self._initial_candle_load_done: bool = False
        # 큐 및 상태
        self._tr_q_last_ts = 0.0
        self._last_signal_ts:  dict[str, float] = {}
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


        # 평가 쿨다운: 종목당 30초 간격 제한
        self._last_eval_ts: dict[str, float] = {}
        self._last_real_tick_time: float = time.monotonic()  # [NEW] 실시간 데이터 하트비트

        # WATCH 모드 예비 종목 갱신 주기 (스코어링 기반)
        self._last_reserve_refresh: float = 0.0
        self._RESERVE_INTERVAL: float = 10.0   # 10초마다 예비 top-2 재선정

        # [NEW] 자원 정리 주기 (메모리 누수 방지)
        self._last_cleanup_ts: float = 0.0
        self._CLEANUP_INTERVAL: float = 600.0  # 10분마다 정리


        # 거래대금 '9시(장시작) 대비' 증가율 — 종목별 당일 최초 관측값(설정: pre_filter_time 이후·양수)을 기준
        self._amt_baseline_date: Optional[date] = None
        self._amt_baseline: dict[str, int] = {}
        # opt10030 직전 성공 결과 캐시 — 실패 시 하드코딩 대체 대신 이전 결과 재사용
        self._last_volume_rows: list[dict] = []
        self._last_volume_updated: float = 0.0  # 마지막 캐시 갱신 시각 (time.monotonic)
        self._opt10030_fetching: bool = False   # opt10030 중복 호출 방지 플래그
        # [FIX 2026-06-05] 재시작 시 당일 opt10030 캐시 즉시 복원
        self._load_opt10030_cache()
        # 전일 거래량 캐시 (UniverseManager에서 관리)
        self._prev_volumes = self.universe_mgr._prev_volumes
        # 동일 종목/신호 중복 emit 방지 (signal_cooldown_sec)
        self._last_signal_ts: dict[tuple[str, str], float] = {}


        self._connect_realtime_signal()


    # ── 전일 거래량 캐시 save/load ────────────────────────────────────────────


    def save_prev_volumes(self) -> None:
        """현재 SnapshotStore의 거래량을 전일 거래량으로 저장 (15:20 강제청산 시 호출)."""
        try:
            with self.store._lock:
                snap_df = self.store._df.copy()
            if snap_df.empty or "volume" not in snap_df.columns:
                return
            volumes = {
                str(code): int(row["volume"])
                for code, row in snap_df.iterrows()
                if int(row.get("volume", 0) or 0) > 0
            }
            self.universe_mgr.save_prev_volumes(volumes)
            self._prev_volumes = volumes # 메모리 캐시 동기화
        except Exception as e:
            logger.warning("[SmartScanner] 전일 거래량 저장 실패: %s", e)


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
        """Pre-Filter 등 로그용: 조·억 표기 + 9시대비 증가율 (TradeAmountHelper)."""
        a = int(amt or 0)
        self._touch_trade_amt_baseline(code, a)
        diag_str = TradeAmountHelper.diagnostic_string(a, self._amt_baseline.get(code))
        return f"거래대금 {diag_str}"


    # -----------------------------------------------------------------------
    # 시작 / 정지
    # -----------------------------------------------------------------------


    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # ✅ 2026-05-11: CircuitBreaker 상태 진단
        ban_status = self._kiwoom.get_tr_ban_status()
        if ban_status:
            logger.critical("[진단] CircuitBreaker 활성 — %d개 TR 차단 중:", len(ban_status))
            for tr_code, remaining_sec in ban_status.items():
                logger.critical("[진단]   - %s: %.0f초 남음", tr_code, remaining_sec)
        else:
            logger.info("[진단] CircuitBreaker 비활성 (모든 TR 사용 가능)")

        # 캐시된 분봉 데이터 메모리 로드 (전체 한번에 로드 - I/O 최소화)
        self.store.load_1min_cache()
        logger.info("분봉 캐시 메모리 로드 완료")

        all_codes = self._fetch_all_codes()
        logger.info("전 종목 %d개 수집", len(all_codes))


        # ③ 터미널 뷰 시작 (UI 대시보드 집중을 위해 비활성화)
        # self.display.start()


        # 1단계 예약
        # 현재 시각이 09:00~15:20 사이면 즉시 실행, 아니면 내일 09:00 예약
        now = datetime.now().time()
        market_start = self.cfg.pre_filter_time  # 이미 dtime 타입
        market_end = dtime(15, 30, 0)

        logger.warning("🔍 [SmartScanner] Pre-Filter 스케줄 확인 — 현재 %s, 예약 %s~%s", now, market_start, market_end)

        if market_start <= now <= market_end:
            logger.info("현재 시각이 장시간(%s~%s) — Pre-Filter 즉시 실행",
                       self.cfg.pre_filter_time, "15:30")
            self._run_pre_filter()
        else:
            secs = self._seconds_until(self.cfg.pre_filter_time)
            t = threading.Timer(secs, self._run_pre_filter)
            t.daemon = True
            t.start()
            logger.warning("⏳ [SmartScanner] Pre-Filter %.0f초(= %s) 후 실행 예약", secs, (datetime.now() + timedelta(seconds=secs)).time())
            # [FIX 2026-06-01] 장 시작 전이어도 분봉 캐시가 있으면 즉시 UI 활성화
            # → 이전 거래일 캐시 종목으로 감시 목록 표시 (09:00 Pre-Filter 실행 시 갱신)
            # → 5분 제한 제거: 08:11 기동처럼 수십 분 전에 켜도 감시 목록 표시
            cached_code_count = len(getattr(self.store, '_min_candle_cache', {}))
            if not self._prefiltered:
                self._prefiltered = True
                logger.info("[2단계] Pre-Filter 대기 중(%.0f초) — 캐시 %d종목으로 UI 즉시 활성화", secs, cached_code_count)


        # 2단계 루프
        self._scan_thread = threading.Thread(
            target=self._realtime_loop, daemon=True, name="ScanLoop"
        )
        self._scan_thread.start()

        # [NEW] UI 업데이트 타이머 (Qt 메인 스레드에서 실행)
        if hasattr(self, "watch_list_updated"):
            from PyQt5.QtCore import QTimer
            self._ui_update_timer = QTimer()
            self._ui_update_timer.timeout.connect(self._process_ui_queue)
            self._ui_update_timer.start(1000)  # [2026-05-21] 500→1000ms (UI 부하 감소, 변경 감지 로직과 시너지)

        # [2026-05-28] opt10030 갱신 전용 타이머 — run_periodic_scan과 분리
        # run_periodic_scan은 캐시만 읽고, opt10030 조회는 이 타이머가 단독으로 담당
        self._opt10030_refresh_timer = QTimer()
        self._opt10030_refresh_timer.timeout.connect(self._bg_fetch_opt10030)
        self._opt10030_refresh_timer.start(5 * 60 * 1000)  # 5분마다 갱신


    def stop(self) -> None:
        self._running = False

        # 실시간 콜백 등록 해제
        try:
            self._kiwoom._ocx.OnReceiveRealData.disconnect(self._on_receive_real_data)
            logger.info("SmartScanner 실시간 콜백 등록 해제 완료")
        except Exception as e:
            logger.warning("SmartScanner 콜백 등록 해제 실패: %s", e)

        # SetRealReg 해제 (모든 감시 종목 unsubscribe)
        try:
            self.watch_q.refresh([])
            logger.info("SmartScanner 실시간 감시 해제 완료")
        except Exception as e:
            logger.warning("SmartScanner 감시 해제 실패: %s", e)

        # opt10030 갱신 타이머 정지
        if hasattr(self, "_opt10030_refresh_timer"):
            self._opt10030_refresh_timer.stop()

        # 디스플레이 정지
        self.display.stop()

        # 스냅샷 저장
        self.store.export_csv(os.path.join(self.cfg.log_dir, "snapshot_final.csv"))
        logger.info("SmartScanner 정지 완료")

    def _process_ui_queue(self) -> None:
        """UI 큐에서 최신 데이터를 가져와서 emit (Qt 메인 스레드에서 호출).
        [FIX 2026-05-21] 데이터 변경 감지 후에만 emit — 메인 스레드 부하 감소
        """
        try:
            if not self._ui_queue:
                # 큐가 비어있는 경우는 60초마다만 로깅
                if time.monotonic() - getattr(self, "_last_queue_empty_log", 0) > 60.0:
                    self._last_queue_empty_log = time.monotonic()
                    logger.info("[UI큐] 큐가 비어있음 — 데이터 도착 대기 중")
                return

            ui_rows = self._ui_queue.pop()

            # [FIX 2026-05-21] 변경 감지: 핵심 필드만 비교 (code, price, signal, change_pct)
            # 변경이 없으면 emit 생략 (UI 부하 큰 폭 감소)
            curr_sig = tuple(
                (r.get("code"), r.get("price"), r.get("signal"), r.get("change_pct"))
                for r in ui_rows
            )
            if curr_sig == getattr(self, "_last_ui_sig", None):
                return  # 변경 없음 — emit 생략

            self._last_ui_sig = curr_sig

            # 10초마다 한 번만 로깅 (과도한 로그 방지)
            if time.monotonic() - getattr(self, "_last_ui_emit_log", 0) > 10.0:
                self._last_ui_emit_log = time.monotonic()
                logger.info("[UI큐-EMIT] %d개 행 전송 (변경 감지됨)", len(ui_rows))

            self.watch_list_updated.emit(ui_rows)
        except Exception as e:
            logger.warning("[UI큐-PROCESS-ERROR] %s", e)

    def _seconds_until(self, target_time: dtime) -> float:
        now = datetime.now().time()
        target_dt = datetime.combine(datetime.today(), target_time)
        now_dt = datetime.combine(datetime.today(), now)

        delta = target_dt - now_dt
        if delta.total_seconds() < 0:
            target_dt = datetime.combine(datetime.today() + timedelta(days=1), target_time)
            delta = target_dt - now_dt

        return delta.total_seconds()

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

        # [2026-05-11 긴급수정] opt10030에서 종목명이 반환되지 않음 -> 모든 종목을 opt10001로 강제 조회
        if rows:
            # 첫 번째 종목에서 name이 code와 같으면, opt10030이 종목명을 전혀 반환하지 않은 것
            if rows[0].get("name") == rows[0].get("code"):
                all_codes = [r.get("code") for r in rows if r.get("code")]
                logger.warning("  ⚠ 종목명 미반환 (%d종목) -> opt10001 전체 조회 시작", len(all_codes))
                info_list = self._tr_q.call(self._kiwoom.get_multiple_stock_info, all_codes[:200])
                if info_list:
                    code_to_row = {r.get("code"): r for r in rows if r.get("code")}
                    updated_count = 0
                    for info in info_list:
                        c = info.get("code")
                        if c in code_to_row:
                            row = code_to_row[c]
                            # 이름 반드시 업데이트
                            row["name"] = info.get("name", "")
                            row["current_price"] = info.get("current_price", row.get("current_price"))
                            row["change_pct"] = info.get("change_pct", row.get("change_pct"))
                            row["trade_amount"] = info.get("trade_amount", row.get("trade_amount"))
                            row["prev_close"] = info.get("prev_close", row.get("prev_close"))
                            row["volume"] = info.get("volume", row.get("volume"))
                            updated_count += 1
                    logger.info("  ✓ 종목명 업데이트 완료: %d/%d", updated_count, len(all_codes))

        # [기존] 가격 보강 로직 (opt10030 실패 시 또는 가격이 0일 때 opt10001 연동)
        # [FIX 2026-06-01] CircuitBreaker 활성 중이면 opt10004 배치 호출 자체를 건너뜀
        # 09:00 Pre-Filter 시 CircuitBreaker+캐시만료(is_fallback=True) 조합으로
        # opt10004 30종목 동기 TR → 메인 스레드 블로킹 → UI 멈춤 반복 사건의 원인
        cb_active = (self._kiwoom.is_tr_banned("opt10004") or
                     self._kiwoom.is_tr_banned("opt10030"))
        is_fallback = (not cb_active and
                       getattr(self, "_last_volume_updated", 0) < time.monotonic() - 300.0)

        target_indices = []
        for i, r in enumerate(rows):
            if is_fallback and i < 30: # 폴백 시 상위 30종목 강제 보정
                target_indices.append(i)
            elif safe_int(r.get("current_price")) <= 0: # 가격이 0이면 무조건 보정
                target_indices.append(i)

        if cb_active and target_indices:
            logger.info("  ⚠ CircuitBreaker 활성 — opt10004 데이터 보강 스킵 (%d종목)", len(target_indices))
            target_indices = [i for i in target_indices
                              if safe_int(rows[i].get("current_price")) <= 0]  # 가격 0만 유지

        if target_indices:
            logger.info("  ⚠ 데이터 보강 필요 (%d종목) -> opt10004 배치 연동 시작", len(target_indices))
            target_codes = [rows[idx].get("code") for idx in target_indices if rows[idx].get("code")][:100]
            if target_codes:
                info_list = self._tr_q.call(self._kiwoom.get_multiple_stock_info, target_codes)
                if info_list:
                    # 결과를 rows에 매핑
                    code_to_row = {r.get("code"): r for r in rows if r.get("code")}
                    for info in info_list:
                        c = info.get("code")
                        if c in code_to_row:
                            row = code_to_row[c]
                            row["current_price"] = info.get("current_price", row.get("current_price"))
                            row["change_pct"] = info.get("change_pct", row.get("change_pct"))
                            row["trade_amount"] = info.get("trade_amount", row.get("trade_amount"))
                            row["prev_close"] = info.get("prev_close", row.get("prev_close"))
                            row["name"] = info.get("name", row.get("name"))

        rows, _ = self.universe_mgr.filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        mn = float(getattr(self.cfg, "min_change_pct", -1.5))
        _n0 = len(rows)
        rows = [r for r in rows if mn <= float(r.get("change_pct", 0) or 0) < mc]
        if _n0 != len(rows):
            logger.info(
                "  등락률 범위 %.1f%% ~ %.1f%% 유지 — %d → %d종목",
                mn, mc, _n0, len(rows),
            )
        rows = self.universe_mgr.apply_scoring_cap(rows, self.cfg.watch_pool_max)
        if not rows:
            logger.warning("  ⚠ Pre-Filter — 필터 후 종목 없음, Pre-Filter 생략")
            return


        logger.info("  📊 감시 후보 %d종목 (순수 주식·hybrid스코어 상위·등락률 < %.1f%%)", len(rows), mc)

        # [CLEANUP 2026-05-29] 주기 스캔마다 5건 WARNING 폭주 → 제거

        # ① DataFrame 에 일괄 적재
        self.top_mgr.clear()
        self.store.bulk_update(rows, kiwoom_mgr=self._kiwoom)
        
        # [NEW] TR 소모 없는 최종 기준가 복구 (0원 방어막)
        # opt10030 차단(-200) 상태에서도 로컬 OCX 메모리를 통해 기준가 복구 시도
        with self.store._lock:
            for code in self.store._df.index:
                st = self.store._get_state(code)
                if st.prev_close <= 0:
                    recovered = self._kiwoom.get_current_price(code)
                    if recovered > 0:
                        st.prev_close = recovered
                        if st.current_price > 0:
                            st.change_pct = round((st.current_price - recovered) / recovered * 100, 2)
                            
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
        # [NEW] 모든 감시 종목에 대해 시장 구분(KOSPI/KOSDAQ) 태깅
        self._tag_market_types()

        self.watch_q.refresh(top_codes)
        self._prefiltered = True

        logger.info("▶ [1단계] Pre-Filter 완료 — %d→%d종목 선정 (감시 상한: %d)", len(rows), len(top_codes), self.cfg.watch_pool_max)
        for i, code in enumerate(top_codes[:10], 1):
            snap = self.store.get_snapshot(code)
            if snap:
                logger.info("  🎯 [%2d순] %s(%s) %s원", i, snap.name[:10], snap.code, f"{snap.current_price:,}")


    # -----------------------------------------------------------------------
    # 2단계: Real-time Scan 루프
    # -----------------------------------------------------------------------


    def _realtime_loop(self) -> None:
        logger.info("▶ [2단계] Real-time Scan 시작")
        _prefilter_logged = False
        while self._running:
            t0 = time.monotonic()
            if self._prefiltered:
                if not getattr(self, "_loop_mode_logged", False):
                    mode = "WATCH" if self._universe_paused else "SEARCH"
                    _has_om = hasattr(self, 'order_mgr') and self.order_mgr
                    _pos_str = str(len(self.order_mgr.positions)) if _has_om else "N/A(order_mgr 미연결)"
                    logger.info("[2단계 모드] %s 모드로 진입 (포지션: %s)", mode, _pos_str)
                    self._loop_mode_logged = True
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
                    # [Optimization] 루프 시작 전 1회 일괄 동기화 (O(1) DataFrame update)
                    self.store.sync()

                    # Tier 3 전체(~110개): 매 사이클 _evaluate() 실행
                    # [NEW] UI 갱신을 위해 상위 120종목 획득 (ScannerWorker 기존 동작 유지)
                    _top_n = max(120, int(getattr(self.cfg, "display_top_n", 50)))
                    top_df = self.store.top_by_trade_amount(_top_n)
                    if top_df.empty:
                        if not getattr(self, "_search_no_data_warned", False):
                            logger.info("[2단계] SEARCH 모드 시작 — store에 아직 데이터 없음 (부팅 초기 정상)")
                            self._search_no_data_warned = True
                    elif len(top_df) < 10:
                        _prev_cnt = getattr(self, "_search_few_data_count", -1)
                        if _prev_cnt != len(top_df):
                            logger.info("[2단계] SEARCH 모드 — 종목 수 부족: %d개 (부팅 직후)", len(top_df))
                            self._search_few_data_count = len(top_df)
                    else:
                        # 데이터 정상 → 플래그 리셋 (재부팅 시 재로깅 가능)
                        self._search_no_data_warned = False
                        self._search_few_data_count = -1
                    
                    ui_rows = []
                    subscribed = set(self.watch_q.subscribed)

                    # vol_burst 점수로 평가 순서 재조정 — "막 터지는 종목" 우선 평가
                    # top_df는 거래대금 순이지만, 실시간 거래량 폭발 종목을 앞으로 당김
                    eval_order = list(top_df.index)
                    try:
                        burst_scores = {}
                        for code in eval_order:
                            if code in subscribed:
                                sn = self.store.get_snapshot(code)
                                if sn:
                                    vols = list(getattr(sn, "volumes_1min", None) or [])
                                    burst_scores[code] = IndicatorService.calc_vol_burst_score(vols)
                        if burst_scores:
                            eval_order.sort(
                                key=lambda c: burst_scores.get(c, 0.0),
                                reverse=True
                            )
                    except Exception:
                        pass  # 정렬 실패 시 기존 거래대금 순 유지

                    # 1. 상위 종목들에 대해 데이터 수집 및 감시 대상 평가
                    for code in eval_order:
                        snap = self.store.get_snapshot(code)
                        if not snap: continue

                        # 감시 대상인 경우에만 전략 평가 실행
                        sig_type = None
                        if code in subscribed:
                            sig_type = self._evaluate(snap)

                        # UI 행 데이터 생성
                        ui_row = self._build_ui_row(snap, sig_type)
                        # [CLEANUP 2026-05-29] 진단용 WARNING 제거 (UI 멈춤 위험)
                        ui_rows.append(ui_row)
                    
                    if ui_rows:
                        # 이미 거래대금 순으로 정렬되어 있으나 보장 차원에서 재정렬
                        ui_rows.sort(key=lambda x: x["trade_amount"], reverse=True)
                        # [FIX] UI 업데이트 큐에 저장 (스캔 스레드는 즉시 반환)
                        try:
                            self._ui_queue.append(ui_rows)
                            # [2026-05-21] 큐 append 진단 로그 제거 (에러만 유지)
                        except Exception as e:
                            logger.warning("[✗UI큐-APPEND-ERROR] %s", e)
                        # [FIX] UI 송신 주기를 5초로 제한 (메인 스레드 부하 최소화)
                        _last_ui_send = getattr(self, "_last_ui_send", 0)
                        if t0 - _last_ui_send > 5.0:
                            self._last_ui_send = t0
                            if t0 - getattr(self, "_last_ui_log", 0) > 10.0:
                                self._last_ui_log = t0
                                logger.info("[UI통합] UI 데이터 송신 완료 (%d종목)", len(ui_rows))
            # [NEW] 주기적 자원 정리 (10분 간격)
            if t0 - self._last_cleanup_ts >= self._CLEANUP_INTERVAL:
                self._last_cleanup_ts = t0
                active = set(self.watch_q.subscribed)
                c_store = self.store.cleanup_stale_data(active)
                c_order = 0
                if self._order_mgr:
                    c_order = self._order_mgr.cleanup_stale_data(active)
                logger.info("[Cleanup] 주기적 자원 정리 완료 — SnapshotStore: %d건, OrderManager: %d건", c_store, c_order)

            elapsed = time.monotonic() - t0
            # UI 및 실시간 평가 루프는 1초 주기로 고정 (cfg.scan_interval은 TR 주기이므로 여기선 배제)
            interval = 0.1 if self._universe_paused else 1.0
            time.sleep(max(0.0, interval - elapsed))


    def _build_ui_row(self, snap: StockSnapshot, signal: Optional[str] = None) -> dict:
        """StockSnapshot 정보를 UI용 dict 행으로 변환 (ScannerWorker 통합)"""
        st = self.store.get_internal_state(snap.code)

        # [DEBUG] snap.name 확인
        if not getattr(self, "_snap_name_log", None):
            self._snap_name_log = set()
        if snap.code not in self._snap_name_log and len(self._snap_name_log) < 5:
            self._snap_name_log.add(snap.code)
            logger.debug("[_build_ui_row] %s: snap.name=%r | st.name=%r",
                        snap.code, snap.name, st.name if st else "N/A")

        # 추세 텍스트 변환
        _tlv = snap.trend_level
        _trend_text = "횡보"
        if _tlv >= 3: _trend_text = "강세"
        elif _tlv == 2: _trend_text = "상승"
        elif _tlv == 1: _trend_text = "약세"
        elif _tlv < 0: _trend_text = "하락"

        return {
            "code":           snap.code,
            "name":           snap.name,
            "price":          snap.current_price,
            "change_pct":     snap.change_pct,
            "volume":         snap.volume,
            "trade_amount":   snap.trade_amount,
            "signal":         signal or "",
            "investor_score": st.inv_score if st else snap.investor_score,
            "foreign_net":    st.inv_foreign if st else snap.foreign_net,
            "inst_net":       st.inv_inst if st else snap.inst_net,
            "trend_level":    _tlv,
            "trend_text":     _trend_text,
            "chejan":         st.chejan_str if st else snap.chejan_strength,
        }

    def _evaluate(self, snap: StockSnapshot) -> Optional[str]:
        # ① 유니버스 감시 중단 — 포지션 풀 시 신규 신호 판단 차단
        if self._universe_paused:
            return None


        # ① 평가 쿨다운 게이팅 — 종목당 최소 30초 간격 (exploration_mode: 0.5초)
        now_ts = time.monotonic()
        _cooldown = 0.5 if getattr(self.cfg, "exploration_mode", False) else 30.0
        if not hasattr(self, "_last_eval_ts"):
            self._last_eval_ts = {}
        if now_ts - self._last_eval_ts.get(snap.code, 0) < _cooldown:
            return None
        self._last_eval_ts[snap.code] = now_ts


        # ② 등락률 범위 (하한~상한)
        _mn = float(getattr(self.cfg, "min_change_pct", -1.5))
        if snap.change_pct < _mn or snap.change_pct >= self.cfg.max_change_pct:
            return None


        # ② 시간 필터
        now = datetime.now().time()
        if not (self.cfg.entry_start_time <= now <= self.cfg.entry_end_time):
            return None


        # ②-bis 요셉 시그널 추세 단계 갱신 (분 단위 1회)
        if getattr(self.cfg, "yosep_trend_enabled", True):
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
            
            # [NEW] 스캐너 로그 패널에 현재 상태 출력 (사용자 요청)
            _trend_text = "횡보"
            if trend_level >= 3: _trend_text = "강세"
            elif trend_level == 2: _trend_text = "상승"
            elif trend_level == 1: _trend_text = "약세"
            elif trend_level < 0: _trend_text = "하락"
            
            # 추세 단계 변경 시에만 로그 기록 (사용자 가독성 중심)
            if trend_level != snap.trend_prev_level:
                ScannerLogger.passed(snap.code, snap.name, "TREND_CHECK", f"추세변화:{_trend_text}(Lv{snap.trend_prev_level}→{trend_level}) 등락:{snap.change_pct}%")

        # [2026-06-02] 60분봉 추세 판정
        if getattr(self.cfg, "h1_trend_enabled", True) and snap.h1_closes:
            h1 = IndicatorService.get_h1_trend(
                h1_closes=snap.h1_closes,
                h1_highs=snap.h1_highs or None,
                h1_lows=snap.h1_lows   or None,
            )
            snap.h1_trend = h1["trend"]
            snap.h1_slope = h1["slope"]
            snap.h1_rsi   = h1["rsi"]

        # [2026-06-02] MTF 추세 판정 (1분봉·5분봉 방향 일치 여부)
        if getattr(self.cfg, "mtf_enabled", True):
            mtf = IndicatorService.get_mtf_trend(
                closes_1min=list(snap.closes_1min or []),
                volumes_1min=list(snap.volumes_1min or []),
                highs_1min=list(snap.highs_1min or []),
                lows_1min=list(snap.lows_1min or []),
            )
            snap.mtf_aligned   = mtf["aligned"]
            snap.mtf_tf1_slope = mtf["tf1_slope"]
            snap.mtf_tf5_slope = mtf["tf5_slope"]
            snap.mtf_tf1_trend = mtf["tf1_trend"]
            snap.mtf_tf5_trend = mtf["tf5_trend"]
            snap.mtf_tf5_bars  = mtf["tf5_bars"]

        # [v3.0] 모듈화된 전략 루프 적용
        enabled = set(getattr(self.cfg, "enabled_strategies", ("JDM_ENTRY", "PULLBACK", "GAP_PULLBACK")) or ())
        order = tuple(getattr(self.cfg, "strategy_order", ("JDM_ENTRY", "PULLBACK", "GAP_PULLBACK")) or ())

        for strategy_name in order:
            if strategy_name not in enabled:
                continue

            strat_obj = self.strategy_map.get(strategy_name)
            if not strat_obj:
                logger.warning("[Strategy] 알 수 없는 전략명 스킵 — %s", strategy_name)
                continue

            # [NEW] AI 피처용 지수 히스토리 추출
            idx_hist = getattr(self.app_context.state, "index_history", None) if hasattr(self, "app_context") else None
            try:
                sig = strat_obj.evaluate(snap, self.cfg, index_history=idx_hist)
                # [2026-05-22] _evaluate WARNING 로그 제거 (종목당 전략당 1건 = 초당 200건 발생)
                if sig is not None:
                    sig.trend_level = int(getattr(snap, "trend_level", 0))
                    sig.trend_prev_level = int(getattr(snap, "trend_prev_level", 0))
                    self._emit(sig)
                    return strategy_name
            except Exception as e:
                logger.error("[_evaluate 오류] %s(%s) [%s] %s", snap.code, snap.name, strategy_name, e)
        
        return None
        if not getattr(self.cfg, "exploration_mode", False):
            # 탐색 모드가 아닐 때만 너무 잦은 로그 방지용으로 필터링
            from scanner.scanner_logger import ScannerLogger as _SL
            _SL.rejected(snap.code, snap.name, "STRATEGY", "모든 전략 조건 미달 (탈락)")



    # -----------------------------------------------------------------------
    # 3단계: Final Signal
    # -----------------------------------------------------------------------


    def _emit(self, sig: ScanSignal) -> None:
        # 동일 종목/신호 재발행 쿨다운 — 로그 전에 체크 (로그 노이즈 방지)
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

        logger.info("[신호발생] %s(%s) [%s] 가격=%d 사유=%s",
                    sig.name, sig.code, sig.signal_type, int(sig.price), sig.reason)

        # [NEW] SQLite DB에 신호 및 AI 피처 저장 (비동기 처리로 UI 프리징 방지)
        try:
            db_data = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": sig.code,
                "name": sig.name,
                "signal_type": sig.signal_type,
                "price": int(sig.price),
                "reason": sig.reason
            }
            # AI 피처가 있으면 업데이트
            if sig.values:
                db_data.update(sig.values)

            # 백그라운드 스레드에서 비동기 실행 (메인 스레드 블로킹 방지)
            threading.Thread(
                target=DatabaseManager().insert_signal,
                args=(db_data,),
                daemon=True
            ).start()
        except Exception as e:
            logger.error("[SmartScanner] DB 신호 저장 실패: %s", e)

        # ② 파일 로그 (ScannerLogger만 유지, WARNING 로그 제거)
        ScannerLogger.signal(sig)
        # [2026-05-22] WARNING 🚨 [3단계] 로그 제거 (신호당 1건, 메인 스레드 부하)
        # [2026-05-26] 신호 발생 텔레그램 알림 제거 — 체결 알림만 받기 위함
        # 알림이 너무 많이 와서 정작 중요한 매수/매도 체결을 놓치는 문제 해결
        # 시스템 로그는 ScannerLogger로 유지됨 (파일 기록)

        # ④ 시그널 발행 (주문 엔진/UI 전송)
        self.signal_detected.emit(sig)

        # ④-1 크로스 스레드 workaround: TradingController에 직접 콜백 호출 (2026-05-12)
        # [2026-05-22] 진단 WARNING 로그 4건 제거 (신호당 4건, 메인 스레드 부하 심각)
        if hasattr(self, '_on_signal_callback') and self._on_signal_callback:
            try:
                self._on_signal_callback(sig)
            except Exception as e:
                logger.error("[신호콜백 오류] %s", e)


    # -----------------------------------------------------------------------
    # 실시간 데이터 콜백
    # -----------------------------------------------------------------------


    def _connect_realtime_signal(self) -> None:
        self._kiwoom._ocx.OnReceiveRealData.connect(self._on_receive_real_data)


    def _on_receive_real_data(
        self, code: str, real_type: str, real_data: str
    ) -> None:
        # 1. 하트비트 갱신
        self._last_real_tick_time = time.monotonic()
        
        # [FIX] real_type 한글 깨짐 강제 보정 (더 공격적으로)
        try:
            if real_type:
                # 이미 한글(유니코드 0xAC00 이상)이 포함되어 있다면 스킵
                if not any(ord(c) > 0x8800 for c in real_type):
                    # latin-1으로 인코딩 후 cp949로 재해석 시도
                    real_type = real_type.encode('latin-1').decode('cp949')
        except Exception:
            pass

        # [2026-05-21] 실시간 틱 수신 진단 로그 제거 (5초당 1건, 시간당 133건 부하)

        # 2. 지수 데이터 처리 (TR 차단 대비 상시 감시)
        if real_type == "업종지수":
            self._handle_index_realtime(code)
            return

        if real_type not in ("주식체결", "주식호가잔량"):
            return

        def fid(n: int) -> str:
            return self._kiwoom._ocx.dynamicCall(
                "GetCommRealData(QString, int)", [code, n]
            )

        # [NEW] 호가 잔량 처리
        if real_type == "주식호가잔량":
            total_ask = safe_int(fid(121))  # 매도총잔량
            total_bid = safe_int(fid(125))  # 매수총잔량
            # [2026-06-02] 호가 상세 파싱 (1~5호가 가격·수량)
            # 매도: 가격 FID 41,43,45,47,49 / 수량 FID 61,63,65,67,69
            # 매수: 가격 FID 51,53,55,57,59 / 수량 FID 71,73,75,77,79
            ask_price_fids = [41, 43, 45, 47, 49]
            ask_qty_fids   = [61, 63, 65, 67, 69]
            bid_price_fids = [51, 53, 55, 57, 59]
            bid_qty_fids   = [71, 73, 75, 77, 79]
            ask_prices = [abs(safe_int(fid(f))) for f in ask_price_fids]
            ask_qtys   = [abs(safe_int(fid(f))) for f in ask_qty_fids]
            bid_prices = [abs(safe_int(fid(f))) for f in bid_price_fids]
            bid_qtys   = [abs(safe_int(fid(f))) for f in bid_qty_fids]
            self.store.update_hoga(
                code, total_ask, total_bid,
                ask_prices=ask_prices, ask_qtys=ask_qtys,
                bid_prices=bid_prices, bid_qtys=bid_qtys,
            )
            # 종목별 최초 수신 시 1회 로그 (호가 데이터 수신 확인용)
            _hoga_logged = getattr(self, "_hoga_first_logged", None)
            if _hoga_logged is None:
                self._hoga_first_logged = set()
                _hoga_logged = self._hoga_first_logged
            if code not in _hoga_logged and any(bid_qtys):
                _hoga_logged.add(code)
                logger.info(
                    "[호가수신] %s 매도1~3호가: %s주 / 매수1~3호가: %s주 / 압력비=%.2f",
                    code,
                    "/".join(str(q) for q in ask_qtys[:3]),
                    "/".join(str(q) for q in bid_qtys[:3]),
                    (sum(bid_qtys[1:3]) / max(sum(ask_qtys[1:3]), 1)),
                )
            return

        # 주식체결 처리
        try:
            price      = safe_int(fid(10))
            change_amt = safe_float(fid(11))  # 전일대비 금액
            pct        = safe_float(fid(12))  # 등락률 (%)
            
            # [진단] 실시간 데이터 값 확인 (개발용 - DEBUG)
            if self._last_real_tick_time - getattr(self, "_last_field_diag", 0) > 60.0: # 주기를 60초로 상향
                self._last_field_diag = self._last_real_tick_time
                logger.debug("[데이터확인] %s | 현재가=%d | 전일대비=%.1f | 등락률(FID12)=%.2f%%", 
                            code, price, change_amt, pct)

            # FID 13 = 누적거래대금 (천원 단위)
            # 한온시스템 raw=123,022,733 × 1,000 = 1,230억 ✅ (실제와 일치)
            # 삼성전자 raw=25,857,422 × 1,000 = 258억 (모의투자 서버 제한 데이터)
            cum_amt    = safe_int(fid(13))  # FID 13: 누적거래대금 (천원 단위)
            # 거래량: FID 13 거래대금(원) ÷ 현재가 = 근사 거래량(주)
            cum_vol    = int(cum_amt * 1_000 / price) if price > 0 and cum_amt > 0 else 0
            high       = safe_int(fid(17))
            low        = safe_int(fid(18))
            open_      = safe_int(fid(16))
            strength_raw = safe_float(fid(20))    # FID 20: 체결강도

            # [진단] 첫 틱 기록용 집합 초기화
            if not getattr(self, "_fid_diag_logged", None):
                self._fid_diag_logged = set()
            if code not in self._fid_diag_logged:
                self._fid_diag_logged.add(code)

            # FID 20 정규화 (10000 이상이면 100으로 나눔)
            strength = strength_raw / 100.0 if strength_raw >= 10000.0 else strength_raw

            if price <= 0:
                return   # 유효하지 않은 체결 데이터

            # [NEW] AI 학습용 틱 로그 저장 (비동기 처리 권장하나 여기서는 간단히 필터링 후 저장)
            if real_type == "주식체결":
                # 감시 중인 종목만 저장하여 I/O 부하 최소화
                if code in self.watch_q.subscribed:
                    self._log_tick_to_csv(code, price, int(fid(14)), cum_vol, pct)

            # 등락률·기준가 계산
            # FID 11(전일대비 금액)은 실시간으로 항상 제공되며 부호가 정확함
            # [CLEANUP 2026-05-29] 147830 진단용 WARNING 8개 제거 (UI 멈춤 위험)
            if change_amt != 0.0 and price > 0:
                prev_close = price - int(change_amt)
            else:
                # FID 11도 없는 경우 — 기존 snapshot의 prev_close 재사용
                st_temp = self.store.get_snapshot(code)
                prev_close = st_temp.prev_close if st_temp else 0

            # [FINAL FALLBACK] 여전히 기준가가 0이라면 OCX 강제 동원 (2단계)
            if prev_close <= 0:
                prev_close_ocx = self._kiwoom.get_current_price(code)
                if prev_close_ocx > 0:
                    prev_close = prev_close_ocx
            if prev_close <= 0:
                prev_close_master = self._kiwoom.get_master_price(code)
                if prev_close_master > 0:
                    prev_close = prev_close_master

            # [NEW] 기준가가 여전히 0이면 시가로 역산 (장 초기 대비)
            if prev_close <= 0 and open_ > 0:
                prev_close = open_

            # [ULTIMATE FALLBACK] 정말로 0이라면... 현재가를 기준가로 임시 세팅
            if prev_close <= 0 and price > 0:
                prev_close = price

            if prev_close > 0 and (pct == 0.0 or abs(pct) < 0.001):
                # 기준가는 있는데 등락률이 0이면 직접 계산 (FID 12 지연 대응)
                pct = round((price - prev_close) / prev_close * 100, 2)

            # 거래대금 = FID 13 × 1,000 (천원 → 원)
            raw_cum_amt = cum_amt
            real_trade_amt = cum_amt * 1_000 if cum_amt > 0 else price * cum_vol

            # [2026-05-21] FID진단 로그 제거 — FID 13 부정확 문제는 이미 해결됨,
            # 실시간 거래에 사용되지 않으며 시간당 3,000건 메인 스레드 부하 유발

            # [FINAL RECOVERY] 기준가가 여전히 0이면 마스터 정보에서 가져옴
            if prev_close <= 0:
                prev_close = abs(self._kiwoom.get_master_last_price(code))
                if prev_close > 0:
                    logger.warning("[SmartScanner] %s 기준가 마스터 복구 성공: %d", code, prev_close)
            
            # ① DataFrame에서 종목명 미리 읽기 (name 파라미터로 전달)
            name_from_df = self.store.get_name(code)
            if not getattr(self, "_name_pass_log", None):
                self._name_pass_log = set()
            if code not in self._name_pass_log and len(self._name_pass_log) < 5:
                self._name_pass_log.add(code)
                logger.debug("[on_opt10001] %s: name_from_df=%r", code, name_from_df)

            # ② DataFrame 및 상태 갱신
            # 누적 거래량(cum_vol)과 누적 거래대금(cum_amt)을 직접 전달하여 VWAP 정밀도 확보
            self.store.update_price(
                code=code, current_price=price, high_price=high,
                low_price=low, open_price=open_, volume=cum_vol,
                trade_amount=real_trade_amt,
                change_pct=pct,
                cum_vol=cum_vol,
                cum_amt=raw_cum_amt, # 원본 저장
                prev_close=prev_close,
                name=name_from_df,
            )

            snap_now = self.store.get_snapshot(code)
            amt = int(snap_now.trade_amount) if snap_now else 0
            self._touch_trade_amt_baseline(code, amt)
            self.top_mgr.update(code, amt)


            # [NEW] 체결강도 저장 (FID 20)
            if strength > 0:
                self.store.update_chejan_strength(code, strength)


            # [NEW] 포지션 종목 현재가 실시간 반영 (손절/익절 정확도 개선) — Qt Signal로 OrderManager에 전달
            if price > 0 and snap_now is not None:
                trend_level = int(getattr(snap_now, "trend_level", 0))
                self.price_updated.emit(code, price, float(snap_now.change_pct), trend_level)
                # [FIX 2026-06-05] _evaluate 호출 제거 — _realtime_loop(1초 주기)에서 평가
                # 틱마다 호출하면 분 게이팅이 있어도 race condition으로 중복 신호 발생


            # watch_q.refresh — SetRealReg/Remove를 매 틱 호출하면 API 과부하
            # 30초 간격으로만 구독 목록을 갱신한다 (유니버스 감시 중단 중은 스킵)
            now_t = time.monotonic()
            if not self._universe_paused and now_t - self._last_watchq_refresh >= self._WATCHQ_INTERVAL:
                self.watch_q.refresh(self.top_mgr.get_top_codes())
                self._last_watchq_refresh = now_t


        except Exception as e:
            logger.debug("실시간 파싱 오류 — %s: %s", code, e)


    def _handle_index_realtime(self, idx_code: str) -> None:
        """실시간 지수 데이터 처리 및 TradingController 전파"""
        try:
            def fid(n: int) -> str:
                return self._kiwoom._ocx.dynamicCall(
                    "GetCommRealData(QString, int)", [idx_code, n]
                )
            
            cur_price = safe_float(fid(10)) # 현재가
            chg_pct = safe_float(fid(12))   # 등락률
            
            if cur_price == 0: return
            
            # 지수 종류 결정
            idx_name = "KOSPI" if idx_code == "001" else "KOSDAQ"
            
            # SmartScanner 상태 업데이트
            if idx_name == "KOSPI":
                self._kospi_cur = cur_price
                self._kospi_chg_pct = chg_pct
            else:
                self._kosdaq_cur = cur_price
                self._kosdaq_chg_pct = chg_pct
            
            # [NEW] TradingController 연동을 위한 시그널 발행
            self.index_updated.emit(idx_name, cur_price, chg_pct)
                
            # 지수 정보 동기화 (RS 필터용)
            if self.cfg:
                if idx_name == "KOSPI": self.cfg.kospi_chg_pct = chg_pct
                else: self.cfg.kosdaq_chg_pct = chg_pct

        except Exception as e:
            logger.debug("[_handle_index_realtime] 오류: %s", e)

    def _subscribe_market_indices(self) -> None:
        """코스피/코스닥 지수 실시간 구독 등록"""
        try:
            # 업종지수: 001(코스피), 101(코스닥)
            for code in ["001", "101"]:
                self._kiwoom._ocx.dynamicCall(
                    "SetRealReg(QString, QString, QString, QString)",
                    ["9001", code, "10;11;12", "1"]
                )
            logger.info("[SmartScanner] 실시간 지수 구독 완료 (KOSPI, KOSDAQ)")
        except Exception as e:
            logger.warning("[SmartScanner] 지수 구독 실패: %s", e)

    def _log_tick_to_csv(self, code: str, price: float, vol: int, cum_vol: int, pct: float) -> None:
        """AI 학습용 틱 데이터를 CSV에 기록 (비동기 파일 I/O 권장)"""
        try:
            import os
            import csv
            log_dir = os.path.join(self.cfg.log_dir, "ticks")
            os.makedirs(log_dir, exist_ok=True)
            
            day = datetime.now().strftime("%Y%m%d")
            filename = os.path.join(log_dir, f"ticks_{day}.csv")
            
            file_exists = os.path.isfile(filename)
            with open(filename, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["timestamp", "code", "price", "volume", "cum_volume", "change_pct"])
                
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                writer.writerow([ts, code, price, vol, cum_vol, pct])
        except Exception as e:
            # 틱 로깅 실패는 시스템 중단 사유가 아니므로 디버그 로그만
            pass


    # -----------------------------------------------------------------------
    # 헬퍼
    # -----------------------------------------------------------------------


    def _fetch_all_codes(self) -> list[str]:
        """전 종목 코드를 수집한다 (코스피 + 코스닥)."""
        all_codes = []
        for mkt in ["0", "10"]:
            raw = self._kiwoom._ocx.dynamicCall("GetCodeListByMarket(QString)", [mkt])
            all_codes.extend([c for c in raw.split(";") if c])
        return all_codes


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


    def _opt10030_cache_path(self) -> str:
        """당일 opt10030 캐시 파일 경로"""
        today = datetime.now().strftime("%Y%m%d")
        return os.path.join("params", f"opt10030_{today}.json")

    def _save_opt10030_cache(self, rows: list[dict]) -> None:
        """opt10030 결과를 당일 파일로 저장 (재시작 시 재조회 방지)"""
        import json
        try:
            path = self._opt10030_cache_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
            logger.debug("[opt10030] 당일 캐시 저장 완료 — %d종목 → %s", len(rows), path)
        except Exception as e:
            logger.warning("[opt10030] 캐시 저장 실패: %s", e)

    def _load_opt10030_cache(self) -> None:
        """재시작 시 당일 opt10030 캐시 복원 — 5분 이내면 즉시 재사용 가능 상태로 로드"""
        import json
        try:
            path = self._opt10030_cache_path()
            if not os.path.exists(path):
                return
            mtime = os.path.getmtime(path)
            age = time.time() - mtime
            # 당일 파일이면 나이에 관계없이 로드 (같은 날이면 종목 구성 유사)
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if rows:
                self._last_volume_rows = rows
                # 파일 나이가 300초 미만이면 캐시 유효로 처리 (재조회 건너뜀)
                if age < 300:
                    self._last_volume_updated = time.monotonic() - age
                    logger.info("[opt10030] 당일 캐시 복원 완료 — %d종목 (나이 %.0f초, 유효)", len(rows), age)
                else:
                    # 나이가 오래됐어도 데이터는 있으므로 실패 시 fallback으로 사용
                    logger.info("[opt10030] 당일 캐시 복원 완료 — %d종목 (나이 %.0f초, 재조회 필요)", len(rows), age)
        except Exception as e:
            logger.warning("[opt10030] 캐시 로드 실패: %s", e)

    def _fetch_top_volume_rows(
        self,
        target: int = 200,
        on_progress: Optional[Callable] = None,
        retry: int = 2,
    ) -> list[dict]:
        """
        거래대금 상위 조회 — opt10004 (한 번에 200개, Circuit Breaker 회피용)
        2026-05-07: opt10030 Circuit Breaker 문제 해결을 위해 opt10004로 변경

        Fallback: opt10004 실패 → opt10030 → opt10032


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

        # ⚠️ 2026-05-11: CircuitBreaker 활성 중이면 캐시 반환 (메인 스레드 보호)
        if self._kiwoom.is_tr_banned("opt10004") or self._kiwoom.is_tr_banned("opt10030"):
            if self._last_volume_rows:
                logger.warning("[opt10030] CircuitBreaker 활성 — 캐시 데이터 반환 (나이 %.1fs, %d종목)",
                             cache_age, len(self._last_volume_rows))
                return self._last_volume_rows[:target]
            else:
                logger.warning("[opt10030] CircuitBreaker 활성이고 캐시 없음 — 빈 목록 반환")
                return []

        self._opt10030_fetching = True
        try:
            # CircuitBreaker 활성 중이면 조회 중단
            if self._kiwoom.is_tr_banned("opt10004") or self._kiwoom.is_tr_banned("opt10030"):
                logger.warning("[CircuitBreaker] 활성 중 — opt10030 조회 중단")
                return []

            # opt10030 단일 조회 (폴백 없음, 성공하거나 실패하거나 하나만)
            logger.info("[opt10030] 조회 시작 (관리종목포함, 신용구분 전체)")
            rows = self._tr_q.call(self._kiwoom.fetch_opt10030_top_volume, target)

            if rows:
                logger.info("[opt10030] 성공 — %d개 종목 수신", len(rows))
                self._last_volume_rows = rows
                self._last_volume_updated = time.monotonic()
                # [FIX 2026-06-05] 당일 캐시 파일로 저장 — 재시작 시 즉시 재사용
                self._save_opt10030_cache(rows)
                return rows[:target]
            else:
                logger.warning("[opt10030] 0개 반환 — TR 재진입 차단 또는 서버 오류. 기존 캐시 %d종목 유지", len(self._last_volume_rows))
                # 기존 캐시가 있으면 그대로 반환 (대시보드 초기화 방지)
                if self._last_volume_rows:
                    return self._last_volume_rows[:target]
                return []

        except Exception as e:
            logger.error("[opt10030 조회 오류] %s", e)
            return []
        finally:
            self._opt10030_fetching = False

    def run_periodic_scan(self, on_progress=None) -> list:
        """
        1분마다 호출하는 전체 스캔 사이클 (오케스트레이터).
        """
        def _prog(phase, current, total, detail=""):
            if on_progress: on_progress(phase, current, total, detail)

        # 1. 전제 조건 확인
        if not self._check_scan_prerequisites():
            return []

        logger.info("=" * 60)
        # ① 하트비트 체크 (60초 이상 무소식 시 경고)
        silence = time.monotonic() - self._last_real_tick_time
        if silence > 60.0:
            logger.warning("  ⚠ 실시간 데이터 침묵 중 (%.1f초간 수신 없음) — 구독 상태 확인 필요", silence)

        logger.info("▶ [2단계] 주기 스캔 시작 (Interval %.0fs)", self.cfg.scan_interval)
        self._log_store_health()
        # [NEW] 실시간 지수 구독 시작
        self._subscribe_market_indices()

        # 2. 거래대금 상위 데이터 확보
        _prog("거래대금 상위 조회", 0, self.cfg.collect_raw_top_n, "데이터 수집 중...")
        rows = self._get_top_volume_data(_prog)
        # [CLEANUP 2026-05-26] 평소 정상 작동 시 INFO 로그 불필요 — 실패 시에만 WARNING
        logger.debug("[주기 스캔] _get_top_volume_data 반환: %d행", len(rows) if rows else 0)

        if not rows:
            logger.warning("[주기 스캔] 종목 데이터 없음 - 신호 발생 불가능")
            return []

        # 3. 유니버스 필터링 및 스코어링
        rows = self._filter_and_score_universe(rows)
        if not rows: return []
        _prog("거래대금 상위 조회", len(rows), self.cfg.watch_pool_max, f"{len(rows)}종목 감시 후보")

        # 4. 상태 컨테이너(Store, TopMgr) 갱신
        self._update_state_containers(rows)

        # 5. 실시간 구독 갱신 (상위 N종목)
        top_codes = [r["code"] for r in rows]
        logger.debug("[주기 스캔] SetRealReg 구독: %d종목", len(top_codes))
        self._refresh_realtime_watch(top_codes)
        self._prefiltered = True  # [NEW] 실시간 루프 가동 플래그 활성화

        # 6. 부족한 분봉 데이터 비동기 로딩
        self._ensure_candle_data(top_codes)

        # 7. 진단 로그 출력
        self._print_diagnostic_logs()

        # 8. 일봉 갱신 스케줄링
        self._schedule_daily_refresh(top_codes)

        # 9. 캐시 저장 (5분 주기)
        self._handle_periodic_cache_save()

        logger.info("[주기 스캔] 완료 — 신호 판단은 실시간 워커(_evaluate)에 위임")
        logger.info("=" * 60)
        _prog("감시종목 갱신", len(top_codes), len(top_codes), "데이터 갱신 완료")
        return top_codes

    def _check_scan_prerequisites(self) -> bool:
        """스캔을 시작할 수 있는 상태인지 확인한다."""
        if self._universe_paused:
            logger.info("[주기 스캔] WATCH 모드 — opt10030 스캔 스킵 (SetRealReg 감시 중)")
            self._log_store_health()
            return False

        # 장 시작 전(08:30 이전)에는 opt10030이 데이터를 제공하지 않음 — 스킵
        now_t = datetime.now().time()
        from datetime import time as _time
        if now_t < _time(8, 30):
            logger.debug("[주기 스캔] 장 전 (%s) — opt10030 스킵", now_t.strftime("%H:%M"))
            return False

        if hasattr(self._kiwoom, 'is_connected') and not self._kiwoom.is_connected():
            logger.warning("[주기 스캔] 연결 끊김 — 스킵")
            return False
        return True

    def _get_top_volume_data(self, prog_cb: Callable) -> list[dict]:
        """거래대금 상위 데이터를 확보한다 (캐시 전용 — 동기 TR 조회 금지).

        opt10030 갱신은 _opt10030_refresh_timer (5분 주기) 또는 _bg_fetch_opt10030이 담당.
        이 메서드는 캐시를 반환하기만 한다 — 절대로 블로킹 TR 호출 금지.
        """
        cache_age = time.monotonic() - self._last_volume_updated
        if self._last_volume_rows:
            if cache_age >= 300.0:
                # 캐시 만료 — 백그라운드 갱신 예약 후 이전 캐시로 스캔 계속
                logger.info("[주기 스캔] 캐시 만료 (%.1fs) → 백그라운드 갱신 예약, 이전 캐시 사용 (%d종목)",
                            cache_age, len(self._last_volume_rows))
                QTimer.singleShot(100, self._bg_fetch_opt10030)
            else:
                logger.debug("[주기 스캔] 캐시 재사용 (나이 %.1fs, %d종목)", cache_age, len(self._last_volume_rows))
            return self._last_volume_rows[:]

        # 캐시 자체가 없는 경우 (최초 기동 직후 또는 완전 초기화 후)
        logger.info("[주기 스캔] 캐시 없음 → 백그라운드 갱신 예약 (이번 스캔은 빈 데이터)")
        QTimer.singleShot(100, self._bg_fetch_opt10030)
        rows = []

        if not rows:
            # ✅ 2026-05-11: 모의투자 환경 대응
            # opt10004/opt10030이 0개 반환 시 실시간 데이터에서 수집된 종목 사용
            # 또는 전일 거래량 캐시 기반 전 종목 폴백
            logger.warning("[주기 스캔] TR 조회 실패 (opt10004/opt10030=0개)")

            # 1단계: 실시간 데이터에서 수집된 종목 확인
            with self.store._lock:
                realtime_codes = list(self.store._states.keys())

            logger.debug("[폴백] 실시간 데이터에서 %d개 종목 감지", len(realtime_codes))

            # 실시간 데이터도 없으면 GetCodeListByMarket으로 전체 종목 조회
            if not realtime_codes:
                logger.warning("[폴백] opt10030 미응답 + 실시간 데이터 없음 → GetCodeListByMarket 전체 종목 조회")
                try:
                    all_codes = self._kiwoom.get_code_list_by_market("0")  # 0=코스피
                    all_codes.extend(self._kiwoom.get_code_list_by_market("10"))  # 10=코스닥
                    realtime_codes = all_codes[:200]  # 상위 200개만 (SetRealReg 부하 제한)
                    logger.info("[GetCodeListByMarket] %d개 종목 확보 → SetRealReg 등록", len(realtime_codes))
                except Exception as e:
                    logger.error("[GetCodeListByMarket 실패] %s", e)
                    realtime_codes = []
            if realtime_codes and len(realtime_codes) <= 10:
                for code in realtime_codes:
                    logger.warning("[진단]   - %s", code)

            if realtime_codes:
                logger.info("[주기 스캔] 실시간 데이터에서 %d종목 감지 — 이를 유니버스로 사용", len(realtime_codes))

                # ⚠️ [2026-05-11] 성능 최적화: 한 번에 모든 상태 스냅샷 추출 (락 최소화)
                fallback_rows = []
                with self.store._lock:
                    all_states = {code: self.store._states.get(code) for code in realtime_codes}

                for code in realtime_codes:
                    st = all_states.get(code)
                    if st:
                        # GetMasterCodeName(): 키움 내부 메모리 즉시 조회 (블로킹 없음)
                        name = st.name if st.name and st.name != code else ""
                        if not name and self._kiwoom:
                            try:
                                name = self._kiwoom.get_stock_name(code) or code
                            except Exception:
                                name = code

                        row_data = {
                            "code": code,
                            "name": name,
                            "current_price": st.current_price,
                            "trade_amount": st.trade_amount,
                            "volume": st.volume,
                            "change_pct": st.change_pct,
                            "prev_close": st.prev_close,
                        }
                        fallback_rows.append(row_data)

                if fallback_rows:
                    # ✅ 2026-05-11: 등락률이 비정상인 항목만 필터링 (종목명은 코드로 대체 가능)
                    valid_rows = [r for r in fallback_rows
                                 if abs(r.get("change_pct", 0)) <= 50.0]

                    logger.info("[주기 스캔] 실시간 기반 %d종목 → 필터 후 %d종목 (등락률 ±50%%)", len(fallback_rows), len(valid_rows))

                    if valid_rows:
                        valid_rows.sort(key=lambda x: x["change_pct"], reverse=True)
                        logger.warning("[진단] 필터된 종목 상위 5개:")
                        for i, r in enumerate(valid_rows[:5]):
                            logger.warning("[진단]   [%d] %s(%s): %.2f%%", i, r.get("code"), r.get("name"), r.get("change_pct", 0))
                        return valid_rows[:self.cfg.collect_raw_top_n]
                    else:
                        logger.warning("[진단] 필터 후 유효한 종목 없음 (모두 비정상 데이터)")

            # 2단계: 실시간 데이터 없으면 전일 거래량 캐시 기반 폴백
            logger.warning("[주기 스캔] 실시간 데이터도 없음 -> 전일 거래량 캐시 기반 유니버스 생성 시도")
            all_codes = self._fetch_all_codes()
            if all_codes:
                fallback_rows = []
                from PyQt5.QtCore import QCoreApplication

                # [OPTIMIZED] 루프 중간에 UI 이벤트 처리 (화면 멈춤 방지)
                _loop_cnt = 0
                for code in all_codes:
                    _loop_cnt += 1
                    if _loop_cnt % 200 == 0:
                        QCoreApplication.processEvents()

                    pv = self.universe_mgr._prev_volumes.get(code, 0)
                    if pv > 0:
                        # [FIX] 스토어 초기화 시 종목명이 없으면 OCX에서 직접 가져옴 (필터링 탈락 방지)
                        name = self.store.get_name(code) or self._kiwoom.get_stock_name(code)
                        # [FIX] 기준가(전일종가) 복구
                        master_p = self._kiwoom.get_master_last_price(code)

                        snap = self.store.get_snapshot(code)
                        cur_p = snap.current_price if snap and snap.current_price > 0 else 0
                        chg_pct = 0.0
                        
                        if cur_p == 0:
                            cur_p = master_p
                            
                        # 등락률 계산
                        if master_p > 0:
                            chg_pct = round((cur_p - master_p) / master_p * 100, 2)
                        
                        fallback_rows.append({
                            "code": code,
                            "name": name.strip(),
                            "current_price": cur_p,
                            "trade_amount": pv * 1000,
                            "volume": pv,
                            "change_pct": chg_pct,
                            "prev_close": master_p,
                        })
                
                if fallback_rows:
                    # 등락률 순 정렬 (사용자 요청: 등락률 높은 거부터)
                    fallback_rows.sort(key=lambda x: x["change_pct"], reverse=True)
                    logger.info("[주기 스캔] 전일 캐시 기반 %d종목 유니버스 생성 완료 (등락률 우선)", len(fallback_rows))
                    
                    # 너무 많이 하면 다시 멈추므로 100개 정도로 제한 (opt10004 배치 처리)
                    warmup_targets = fallback_rows[:100]
                    warmup_codes = [r["code"] for r in warmup_targets]
                    logger.info("[WARMUP] 상위 %d종목 시세 동기화 시작 (opt10004 배치)...", len(warmup_targets))
                    
                    info_list = self._tr_q.call(self._kiwoom.get_multiple_stock_info, warmup_codes)
                    if info_list:
                        # 결과를 warmup_targets에 매핑
                        code_to_target = {r["code"]: r for r in warmup_targets}
                        for info in info_list:
                            c = info.get("code")
                            if c in code_to_target:
                                target = code_to_target[c]
                                target.update({
                                    "current_price": info["current_price"],
                                    "change_pct": info["change_pct"],
                                    "trade_amount": info["trade_amount"],
                                    "volume": info["volume"],
                                })
                    
                    # 갱신된 데이터로 다시 정렬
                    fallback_rows.sort(key=lambda x: x["change_pct"], reverse=True)
                    return fallback_rows[:self.cfg.collect_raw_top_n]

            logger.warning("[주기 스캔] opt10030/10032/전일캐시 모두 실패 — 빈 유니버스 반환")
            return []
        return rows

    def _filter_and_score_universe(self, rows: list[dict]) -> list[dict]:
        """유니버스 필터링(우선주 제외 등) 및 하이브리드 스코어링을 적용한다."""
        _n_total = len(rows)
        rows, _dropped_equity = filter_equity_rows(rows)
        _n_equity = len(rows)
        
        mc = self.cfg.max_change_pct
        rows = [r for r in rows if 0.0 <= float(r.get("change_pct", 0) or 0) < mc]
        _n_chg = len(rows)
        
        if _n_total != _n_chg:
            logger.info("[주기 스캔] 필터 결과: 전체 %d -> 종목명필터 %d -> 등락률필터(0%%~%.1f%%) %d종목", 
                        _n_total, _n_equity, mc, _n_chg)
            
        if not rows:
            logger.warning("[주기 스캔] 필터 후 남은 종목이 없습니다 (전체 %d개 중 전원 탈락)", _n_total)
            return []

        rows = apply_universe_score_cap(rows, self.cfg.watch_pool_max, self.cfg, self._prev_volumes)
        return rows

    def _update_state_containers(self, rows: list[dict]) -> None:
        """SnapshotStore와 TopVolumeManager에 최신 데이터를 반영한다."""
        self.top_mgr.clear()
        self.store.bulk_update(rows)
        for row in rows:
            code = row["code"]
            amt = int(row.get("trade_amount") or 0)
            self._touch_trade_amt_baseline(code, amt)
            self.top_mgr.update(code, amt)

    def _refresh_realtime_watch(self, top_codes: list[str]) -> None:
        """실시간 틱 구독 목록을 갱신한다. (보유 종목 포함)"""
        # 1. 감시 리스트 구성: 상위 종목 + 현재 보유 종목 (관리용)
        held_codes = []
        if self._order_mgr and hasattr(self._order_mgr, "positions"):
            held_codes = [c for c in self._order_mgr.positions.keys() if c]
            
        # [NEW] 만약 상위 종목이 없다면(TR 차단 등), 보유 종목이라도 무조건 감시
        if not top_codes and not held_codes:
            # 최후의 보루: 삼성전자 강제 추가 (데이터 흐름 확인용)
            top_codes = ["005930"]
            
        # 보유 종목은 항상 최상단 우선순위로 등록 (실시간 가격 갱신 보장)
        combined = list(dict.fromkeys(held_codes + top_codes))
        
        # Kiwoom SetRealReg 최대 갯수(약 100개) 제한 고려
        reg_codes = combined[:getattr(self.cfg, "realtime_sub_max", 100)]
        
        if self.watch_q:
            logger.info("[실시간] 구독 갱신 시도: %d종목 (보유=%d, 상위=%d)", 
                        len(reg_codes), len(held_codes), len(top_codes))
            self.watch_q.refresh(reg_codes)

    def register_code_realtime(self, code: str) -> None:
        """단일 종목 실시간 시세 등록 (보유 종목 진입 시 즉시 호출)"""
        if self.watch_q:
            self.watch_q._sub(code)
            logger.debug("[SmartScanner] 보유종목 실시간 등록: %s", code)

    def unregister_code_realtime(self, code: str) -> None:
        """단일 종목 실시간 시세 해제 (포지션 청산 시 호출)"""
        if self.watch_q:
            # 단, 해당 종목이 Top N 감시 리스트에 있다면 해제하지 않음
            top_codes = self.top_mgr.get_top_codes()
            if code not in top_codes:
                self.watch_q._unsub(code)
                logger.debug("[SmartScanner] 보유종목 실시간 해제: %s", code)
            else:
                logger.debug("[SmartScanner] 보유종목 청산되었으나 감시 상위권이므로 구독 유지: %s", code)

    def _ensure_candle_data(self, top_codes: list[str]) -> None:
        """부족한 분봉 데이터를 비동기적으로 로딩하도록 예약한다. (장초반 Turbo 모드 포함)"""
        # opt10080이 서킷브레이커에 의해 차단 중이면 로딩 시도 자체를 건너뜀
        if getattr(self._kiwoom, "is_tr_banned", lambda _: False)("opt10080"):
            return

        from datetime import time as _time
        now_t = datetime.now().time()
        # 장 초반(09:00~09:10) 여부 — 이 시간대만 진짜 TurboWarmup
        is_opening = _time(9, 0) <= now_t <= _time(9, 10)

        min_bars = 55
        load_max = 6
        codes_need_all = [c for c in top_codes if self.store.get_candle_count(c) < min_bars]

        # [FIX 2026-06-05] TurboWarmup은 09:00~09:10에만 적용
        # 이전: not _initial_candle_load_done이면 시간대 무관하게 20종목 일괄 → -200 반복
        # 수정: 09:10 이후 재시작 시에는 일반 load_max(6종목/주기)로 제한
        TURBO_MAX = 20
        if is_opening:
            codes_need = codes_need_all[:TURBO_MAX]
            if codes_need:
                logger.info("[TurboWarmup] 09:00~09:10 장초반 로딩 (%d종목, 상한 %d)",
                            len(codes_need), TURBO_MAX)
        elif not self._initial_candle_load_done:
            # 장중 재시작: 6종목씩 나눠서 로딩 (TR 부하 방지)
            codes_need = codes_need_all[:load_max]
            if codes_need:
                logger.info("[캔들로딩] 장중 재시작 분봉 보충 (%d종목, 총 부족 %d종목)",
                            len(codes_need), len(codes_need_all))
        else:
            codes_need = codes_need_all[:load_max]

        if codes_need:
            delay = 200 if is_opening else 500
            QTimer.singleShot(delay, lambda c=list(codes_need): self._load_candles_async(c, 0))

    def _print_diagnostic_logs(self) -> None:
        """진단용 로그를 출력한다 (현재 감시 중인 상위 종목)."""
        dn = max(1, int(self.cfg.diagnostic_sample_n))
        top_codes = self.top_mgr.get_top_codes()[:dn]
        if not top_codes: return
        
        with self.store._lock:
            df_snap = self.store._df.copy()

        for code in top_codes:
            if code not in df_snap.index: continue
            row = df_snap.loc[code]
            amt = int(row.get("trade_amount", 0))
            logger.info(
                "[진단] %s(%s) 현재가=%s 등락=%s 거래대금=%s",
                row.get("name", "?"), code, f"{int(row.get('current_price', 0)):,}",
                f"{float(row.get('change_pct', 0)):+.2f}%",
                format_trade_amount_korean(amt)
            )

    def _schedule_daily_refresh(self, top_codes: list[str]) -> None:
        """일봉 데이터 갱신 스케줄을 관리한다."""
        now = time.time()
        if now - self._last_daily_update < self._daily_update_interval_sec:
            return
            
        self._last_daily_update = now
        _chg_min = float(getattr(self.cfg, "eod_change_pct_min", 2.0))
        _chg_max = float(getattr(self.cfg, "eod_change_pct_max", 10.0))
        
        with self.store._lock:
            df_snap = self.store._df.copy()
            
        candidates = [
            c for c in top_codes
            if _chg_min <= float(df_snap.at[c, "change_pct"] if c in df_snap.index else 0.0) <= _chg_max
        ]
        rest = [c for c in top_codes if c not in set(candidates)]
        self._daily_refresh_pending = (candidates + rest)[:10]
        logger.info("[일봉갱신] %d종목 예약 완료", len(self._daily_refresh_pending))

    def _handle_periodic_cache_save(self) -> None:
        """5분 주기로 분봉 캐시를 저장한다."""
        now = time.time()
        if now - getattr(self, "_last_1min_cache_save", 0) >= 300:
            self._last_1min_cache_save = now
            threading.Thread(target=self.store.save_1min_cache, daemon=True).start()

    # -----------------------------------------------------------------------



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
        # 장 외 시간 + 초기 로딩이 이미 완료된 경우 → TR 호출 생략
        # 재연결 후 opt10080/10081이 야간에 불필요하게 호출되어 일일 TR 한도를 소진하는 것을 방지
        from datetime import time as _dtime
        _t = datetime.now().time()
        _MARKET_OPEN  = _dtime(8, 50)
        _MARKET_CLOSE = _dtime(15, 40)
        _outside_market = not (_MARKET_OPEN <= _t <= _MARKET_CLOSE)
        if _outside_market and self._initial_candle_load_done:
            if idx == 0:
                logger.debug("[STEP-H] 장 외 시간 — 분봉 TR 호출 생략 (%s)", _t.strftime("%H:%M"))
            return  # 전체 체인 중단

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
            # 1분봉 체인 완료 후 60분봉 체인 시작 (500ms 딜레이로 TR 분리)
            if getattr(self.cfg, "h1_trend_enabled", True):
                logger.info("[STEP-H async] 60분봉 별도 체인 시작 (500ms 후)")
                QTimer.singleShot(500, lambda: self._load_h1_candles_async(codes, 0))
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

        # 다음 종목을 비동기 처리
        from datetime import time as _time
        is_opening = _time(9, 0) <= datetime.now().time() <= _time(9, 10)
        interval = 250 if is_opening else 350  # 장초반 Turbo: 0.25초 (최소 간격)
        QTimer.singleShot(interval, lambda: self._load_candles_async(codes, idx + 1))


    # ── 60분봉 초기 로딩 (1분봉 체인 완료 후 별도 체인으로 분리) ─────────────────

    def _load_h1_candles_async(self, codes: list, idx: int) -> None:
        """
        60분봉 로딩을 1분봉 체인과 분리된 별도 QTimer 체인으로 처리한다.
        1분봉 체인 완료 후 500ms 딜레이를 두고 시작하며, 종목당 600ms 간격으로 분산한다.
        동일 TR(opt10080) 연속 호출로 인한 -200 에러를 방지한다.
        """
        if idx >= len(codes):
            logger.info("[H1 async] 60분봉 로딩 완료 — 총 %d종목", len(codes))
            return

        if not getattr(self.cfg, "h1_trend_enabled", True):
            return

        if getattr(self._kiwoom, "_tr_busy", False):
            QTimer.singleShot(400, lambda: self._load_h1_candles_async(codes, idx))
            return

        code = codes[idx]
        try:
            h1_candles = self._kiwoom.get_min_candles(code, 60, 20)
            h1_ohlc = [c for c in reversed(h1_candles) if c.get("close")]
            if h1_ohlc:
                self.store.set_h1_candles(code, h1_ohlc)
                logger.debug("[H1 async] %s 60분봉 %d개 로딩 완료", code, len(h1_ohlc))
        except Exception as e:
            logger.debug("[H1 async] %s 60분봉 로딩 실패: %s", code, e)

        QTimer.singleShot(600, lambda: self._load_h1_candles_async(codes, idx + 1))

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


    def _tag_market_types(self) -> None:
        """현재 SnapshotStore에 적재된 모든 종목의 시장 구분(KOSPI/KOSDAQ)을 태깅한다."""
        try:
            with self.store._lock:
                for m in self.cfg.markets: # ("0", "10")
                    m_codes = self._kiwoom.get_code_list_by_market(m)
                    for c in m_codes:
                        if c in self.store._states:
                            self.store._states[c].market_type = m
            logger.debug("[SmartScanner] 시장 구분 태깅 완료 (KOSPI/KOSDAQ)")
        except Exception as e:
            logger.warning("[SmartScanner] 시장 구분 태깅 실패: %s", e)


    def _log_store_health(self) -> None:
        """SnapshotStore의 현재 상태를 요약하여 로그에 기록한다."""
        try:
            count = len(self.store)
            logger.info("[SnapshotStore Health] 감시 중인 종목 수: %d", count)
        except Exception as e:
            logger.debug("[_log_store_health] 오류: %s", e)
