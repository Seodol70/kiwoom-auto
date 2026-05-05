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
from scanner.snapshot_store import SnapshotStore
from scanner.indicator_service import IndicatorService
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
    format_trade_amount_korean, apply_watch_pool_cap, apply_universe_score_cap,
    format_trade_amount_growth
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
    check_pullback_entry
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

    def __init__(self, kiwoom, cfg: Optional[SmartScannerConfig] = None) -> None:
        super().__init__()
        self._kiwoom = kiwoom
        self.cfg     = cfg or SmartScannerConfig()


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


        self._running     = False
        self._prefiltered = False
        self._scan_thread: Optional[threading.Thread] = None
        self._lock        = threading.Lock()


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
        """Pre-Filter 등 로그용: 조·억 표기 + 9시대비 증가율."""
        a = int(amt or 0)
        self._touch_trade_amt_baseline(code, a)
        ta = self.universe_mgr.format_trade_amount(a)
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
        rows, _ = self.universe_mgr.filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        _n0 = len(rows)
        rows = [r for r in rows if float(r.get("change_pct", 0) or 0) < mc]
        if _n0 != len(rows):
            logger.info(
                "  등락률 상한 %.1f%% 미만만 유지 — %d → %d종목",
                mc, _n0, len(rows),
            )
        rows = self.universe_mgr.apply_scoring_cap(rows, self.cfg.watch_pool_max)
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
        from scanner.indicator_service import IndicatorService
        _gdc = IndicatorService.get_daily_context
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


        # ④ 시그널 발행 (주문 엔진/UI 전송)
        self.signal_detected.emit(sig)


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
                        
                        # [NEW] opt10030 응답이 없으면(공휴일/장전) opt10032(전일거래대금상위) 시도
                        if not rows and hasattr(self._kiwoom, "fetch_opt10032_top_volume"):
                            logger.info("[opt10030] 오늘 데이터 없음 -> opt10032(전일거래대금상위) 폴백 시도")
                            rows = self._tr_q.call(self._kiwoom.fetch_opt10032_top_volume, target)
                    else:
                        rows = self._tr_q.call(self._do_fetch_opt10030)
                        rows = rows[:target]
                    logger.info("[opt10030] 응답 %d행 (목표 %d)", len(rows), target)


                    if rows:
                        result = rows[:target]
                        # [DEBUG] opt10030 응답 상세 로깅
                        for i, r in enumerate(result[:5]):
                            name = r.get("name", "?")
                            logger.warning("[opt10030] 응답 #%d: code=%s, name=%s (type=%s, repr=%r)",
                                         i, r.get("code"), name, type(name).__name__, name)

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

        # 모든 재시도 실패 — 이전 캐시 재사용 (있으면) 또는 5대장 fallback
        if self._last_volume_rows:
            logger.info("[opt10030] 조회 실패 — 이전 캐시 %d종목 재사용", len(self._last_volume_rows))
            return self._last_volume_rows[:target]

        logger.warning("[opt10030] 모든 시도 실패 (캐시 없음) -> 상위 폴백 로직으로 위임")
        return []

    def _log_store_health(self) -> None:
        """SnapshotStore 상태를 주기적으로 로깅한다."""
        _now = time.monotonic()
        if _now - getattr(self, "_store_health_last", 0.0) < 300.0:
            return
        self._store_health_last = _now
        try:
            with self.store._lock:
                _n_codes = len(self.store._df)
                _n_mins = len([s for s in self.store._states.values() if s.mins])
            logger.info("[스토어헬스] 종목=%d 1분봉보유=%d", _n_codes, _n_mins)
        except Exception as e:
            logger.debug("[스토어헬스] 수집 실패: %s", e)

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
        logger.info("[주기 스캔] 시작 — %s", datetime.now().strftime("%H:%M:%S"))
        self._log_store_health()

        # 2. 거래대금 상위 데이터 확보
        _prog("거래대금 상위 조회", 0, self.cfg.collect_raw_top_n, "데이터 수집 중...")
        rows = self._get_top_volume_data(_prog)
        if not rows: return []

        # 3. 유니버스 필터링 및 스코어링
        rows = self._filter_and_score_universe(rows)
        if not rows: return []
        _prog("거래대금 상위 조회", len(rows), self.cfg.watch_pool_max, f"{len(rows)}종목 감시 후보")

        # 4. 상태 컨테이너(Store, TopMgr) 갱신
        self._update_state_containers(rows)

        # 5. 실시간 구독 갱신 (상위 N종목)
        _watch_df = self.store.top_by_trade_amount(self.cfg.watch_pool_max)
        top_codes = _watch_df.index.tolist() if not _watch_df.empty else []
        self._refresh_realtime_watch(top_codes)

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
        """거래대금 상위 데이터를 확보한다 (캐시 우선, 실패 시 fallback)."""
        cache_age = time.monotonic() - self._last_volume_updated
        if self._last_volume_rows and cache_age < 300.0:
            logger.info("[주기 스캔] 캐시된 opt10030 결과 사용 (나이 %.1fs, %d종목)", cache_age, len(self._last_volume_rows))
            return self._last_volume_rows[:]
        
        logger.info("[주기 스캔] opt10030 즉시 조회 (캐시나이 %.1fs)", cache_age)
        rows = self._fetch_top_volume_rows(target=self.cfg.collect_raw_top_n, on_progress=prog_cb)

        # [DEBUG] _fetch_top_volume_rows에서 받은 rows 로깅
        if rows:
            for i, r in enumerate(rows[:5]):
                name = r.get("name", "?")
                logger.warning("[스캔] _fetch_top_volume_rows 결과 #%d: code=%s, name=%s (type=%s, repr=%r)",
                             i, r.get("code"), name, type(name).__name__, name)

        if not rows:
            # [NEW] TR 실패 시(공휴일/장전) 전일 거래량 캐시 기반 전 종목 폴백
            logger.warning("[주기 스캔] TR 조회 실패 -> 전일 거래량 캐시 기반 유니버스 생성 시도")
            all_codes = self._fetch_all_codes()
            if all_codes:
                fallback_rows = []
                for code in all_codes:
                    pv = self.universe_mgr._prev_volumes.get(code, 0)
                    if pv > 0:
                        # GetMasterCodeName의 CP949 인코딩 보정
                        raw_name = self._kiwoom._ocx.dynamicCall("GetMasterCodeName(QString)", [code])
                        try:
                            # CP949 → UTF-8 변환 (키움API는 CP949 인코딩)
                            if raw_name:
                                name = raw_name.encode('latin-1').decode('cp949')
                            else:
                                name = ""
                        except (UnicodeDecodeError, UnicodeEncodeError, AttributeError):
                            # 변환 실패 시 원본 사용
                            name = raw_name or ""

                        fallback_rows.append({
                            "code": code,
                            "name": name.strip(),
                            "current_price": 0,
                            "trade_amount": pv * 1000,
                            "volume": pv,
                            "change_pct": 0.0,
                            "prev_close": 0
                        })
                
                if fallback_rows:
                    # 거래대금(추정) 순 정렬
                    fallback_rows.sort(key=lambda x: x["trade_amount"], reverse=True)
                    logger.info("[주기 스캔] 전일 캐시 기반 %d종목 유니버스 생성 완료", len(fallback_rows))
                    return fallback_rows[:self.cfg.collect_raw_top_n]

            logger.warning("[주기 스캔] opt10030/10032/전일캐시 모두 실패 — fallback 10대장 대체")
            return [
                {"code": "005930", "name": "삼성전자", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "000660", "name": "SK하이닉스", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "207940", "name": "삼성바이오로직스", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "005380", "name": "현대차", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "373220", "name": "LG에너지솔루션", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "000270", "name": "기아", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "068270", "name": "셀트리온", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "005490", "name": "POSCO홀딩스", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "035420", "name": "NAVER", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
                {"code": "006400", "name": "삼성SDI", "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "volume": 0, "prev_close": 0},
            ]
        return rows

    def _filter_and_score_universe(self, rows: list[dict]) -> list[dict]:
        """유니버스 필터링(우선주 제외 등) 및 하이브리드 스코어링을 적용한다."""
        rows, _ = filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        _n0 = len(rows)
        rows = [r for r in rows if float(r.get("change_pct", 0) or 0) < mc]
        
        if _n0 != len(rows):
            logger.info("[주기 스캔] 등락률 상한 %.1f%% 미만만 유지 — %d → %d종목", mc, _n0, len(rows))
            
        rows = apply_universe_score_cap(rows, self.cfg.watch_pool_max, self.cfg, self._prev_volumes)
        if not rows:
            logger.warning("[주기 스캔] 필터 후 종목 없음 — 중단")
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
        """실시간 틱 구독 목록을 갱신한다."""
        reg_codes = top_codes[:self.cfg.realtime_sub_max]
        self.watch_q.refresh(reg_codes)

    def _ensure_candle_data(self, top_codes: list[str]) -> None:
        """부족한 분봉 데이터를 비동기적으로 로딩하도록 예약한다."""
        min_bars = 55
        load_max = 6
        codes_need_all = [c for c in top_codes if self.store.get_candle_count(c) < min_bars]
        
        if not self._initial_candle_load_done:
            codes_need = codes_need_all
            logger.info("[주기 스캔] 첫 일괄 로딩 시작 (%d종목)", len(codes_need))
        else:
            codes_need = codes_need_all[:load_max]

        if codes_need:
            QTimer.singleShot(500, lambda c=list(codes_need): self._load_candles_async(c, 0))

    def _print_diagnostic_logs(self) -> None:
        """진단용 로그를 출력한다."""
        dn = max(1, int(self.cfg.diagnostic_sample_n))
        sample = self.store.top_by_trade_amount(dn)
        if sample.empty: return

        for code, row in sample.iterrows():
            amt = int(row.get("trade_amount", 0))
            logger.debug(
                "[진단] %s(%s) 현재가=%s 거래대금=%s · %s",
                row.get("name", "?"), code, f"{int(row.get('current_price', 0)):,}",
                format_trade_amount_korean(amt),
                format_trade_amount_growth(amt, self._amt_baseline.get(str(code)))
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



