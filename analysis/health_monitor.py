# -*- coding: utf-8 -*-
"""
analysis/health_monitor.py
──────────────────────────
자동매매 시스템 자가 진단 + 자기 진화 모듈.

구성 요소
  HealthEventLog        — JSONL 영속 로그 (thread-safe)
  WatchdogTimer         — UI 프리징 감지 (daemon thread)
  SignalDroughtDetector — 신호 가뭄 시 파라미터 인트라데이 완화
  ErrorRateTracker      — TR 연속 실패 추적 → 재연결 트리거
  HealthMonitor         — 위 컴포넌트 통합, 공개 API

스레드 설계
  - HealthEventLog  : 모든 스레드에서 호출 가능 (내부 Lock 보호)
  - WatchdogTimer   : daemon thread, on_freeze 콜백만 호출
  - SignalDrought   : HealthMonitor._run() 전용 (단일 스레드)
  - ErrorRateTracker: 모든 스레드에서 호출 가능 (내부 Lock 보호)
  - HealthMonitor   : start/stop → 메인 스레드, record_*() → 어디서든
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

HEALTH_LOG_PATH = Path("logs/health_events.jsonl")

# WatchdogTimer
WATCHDOG_PING_SEC    = 5      # 메인 스레드가 ACK를 보내는 주기 (초)
WATCHDOG_TIMEOUT_SEC = 20     # 이 시간 동안 ACK 없으면 프리징 판정 (일봉체인 10종목×1s+여유 = 20s)

# SignalDroughtDetector
DROUGHT_WINDOW_MIN   = 45     # 신호 없는 시간이 이 분 이상이면 완화 시작
DROUGHT_MAX_STEPS    = 3      # 최대 완화 단계

# 단계별 완화 테이블 (단계 0 = 기본, 1~3 = 점진 완화)
RELAX_TABLE: Dict[str, List[float]] = {
    "jdm_rsi_entry_min_trend":      [45.0, 42.0, 38.0, 35.0],
    "ema_disp_max_pct_trend":       [7.0,  8.0,  9.0,  10.0],
    "jdm_rsi_high_trend":           [80.0, 82.0, 84.0, 86.0],
    "price_ema_disp_max_pct_trend": [6.0,  7.0,  8.0,  9.0],
}

# ErrorRateTracker
TR_FAIL_THRESHOLD    = 5      # 연속 실패 이 횟수 이상 → 재연결 요청

# HealthMonitor 주기
MONITOR_TICK_SEC     = 10     # _run() 루프 주기


# ──────────────────────────────────────────────────────────────────────────────
# HealthEventLog
# ──────────────────────────────────────────────────────────────────────────────

class HealthEventLog:
    """
    thread-safe JSONL 로거.
    모든 스레드에서 write() 가능.  read_today() 는 메인 스레드에서만 호출.
    """

    def __init__(self, path: Path = HEALTH_LOG_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event_type: str, payload: dict) -> None:
        record = {
            "ts":    datetime.now().isoformat(timespec="seconds"),
            "type":  event_type,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                logger.warning("[HealthLog] write 실패: %s", exc)

    def read_today(self) -> List[dict]:
        today = datetime.now().strftime("%Y-%m-%d")
        result: List[dict] = []
        if not self._path.exists():
            return result
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        if rec.get("ts", "").startswith(today):
                            result.append(rec)
                    except json.JSONDecodeError:
                        pass
        except OSError as exc:
            logger.warning("[HealthLog] read 실패: %s", exc)
        return result


# ──────────────────────────────────────────────────────────────────────────────
# WatchdogTimer
# ──────────────────────────────────────────────────────────────────────────────

class WatchdogTimer:
    """
    daemon thread에서 메인 스레드 ACK를 감시.
    ack() 가 WATCHDOG_TIMEOUT_SEC 이상 안 오면 on_freeze() 콜백 호출.

    설계 원칙:
      - WatchdogTimer 자체는 공유 뮤터블 상태를 최소화
      - _last_ack_at 은 float (atomic write on CPython) → Lock 불필요
      - on_freeze 콜백은 새 스레드에서 호출 (Watchdog 루프 차단 방지)
    """

    def __init__(
        self,
        on_freeze: Callable[[], None],
        ping_sec:    float = WATCHDOG_PING_SEC,
        timeout_sec: float = WATCHDOG_TIMEOUT_SEC,
    ) -> None:
        self._on_freeze   = on_freeze
        self._ping_sec    = ping_sec
        self._timeout_sec = timeout_sec
        self._last_ack_at = time.monotonic()
        self._running     = False
        self._thread:  Optional[threading.Thread] = None
        self._fired    = threading.Event()   # 중복 콜백 방지 — set/clear가 원자적

    def start(self) -> None:
        self._running     = True
        self._last_ack_at = time.monotonic()
        self._fired.clear()
        self._thread = threading.Thread(
            target=self._run, name="WatchdogTimer", daemon=True
        )
        self._thread.start()
        logger.debug("[Watchdog] 시작 (timeout=%.0fs)", self._timeout_sec)

    def stop(self) -> None:
        self._running = False

    def ack(self) -> None:
        """메인 스레드에서 주기적으로 호출해 '살아있음'을 알림."""
        self._last_ack_at = time.monotonic()
        self._fired.clear()   # 재연결 후 재사용 가능

    def _run(self) -> None:
        while self._running:
            time.sleep(self._ping_sec)
            elapsed = time.monotonic() - self._last_ack_at
            if elapsed >= self._timeout_sec and not self._fired.is_set():
                self._fired.set()
                logger.critical(
                    "[Watchdog] UI 프리징 감지! ACK 없은 지 %.1f초", elapsed
                )
                # 콜백을 별도 스레드로 → Watchdog 루프 블로킹 방지
                cb_thread = threading.Thread(
                    target=self._safe_callback, daemon=True
                )
                cb_thread.start()

    def _safe_callback(self) -> None:
        try:
            self._on_freeze()
        except Exception:
            logger.exception("[Watchdog] on_freeze 콜백 예외")


# ──────────────────────────────────────────────────────────────────────────────
# SignalDroughtDetector
# ──────────────────────────────────────────────────────────────────────────────

class SignalDroughtDetector:
    """
    마지막 매수 신호 이후 DROUGHT_WINDOW_MIN 분 경과 시 파라미터 완화 제안.
    단일 스레드(HealthMonitor._run)에서만 접근 → Lock 불필요.

    완화는 RELAX_TABLE 기반 DROUGHT_MAX_STEPS 단계로 제한.
    하루가 끝나면 reset_day() 로 초기화.
    """

    def __init__(self) -> None:
        self._last_signal_at: float  = time.monotonic()
        self._relax_step:     int    = 0
        self._applied_at:     float  = 0.0   # 마지막 완화 시각

    def record_signal(self) -> None:
        self._last_signal_at = time.monotonic()

    def reset_day(self) -> None:
        self._last_signal_at = time.monotonic()
        self._relax_step     = 0
        self._applied_at     = 0.0

    @property
    def current_step(self) -> int:
        return self._relax_step

    def check(self) -> Optional[Dict[str, float]]:
        """
        완화가 필요하면 {param: new_value, ...} 딕트를 반환.
        아직 불필요하거나 최대 단계면 None.
        """
        if self._relax_step >= DROUGHT_MAX_STEPS:
            return None

        elapsed_min = (time.monotonic() - self._last_signal_at) / 60.0
        if elapsed_min < DROUGHT_WINDOW_MIN:
            return None

        # 완화 쿨다운: 이미 같은 단계를 적용한 뒤 완화 윈도우가 다시 지나야 재적용
        if self._applied_at > 0:
            since_applied = (time.monotonic() - self._applied_at) / 60.0
            if since_applied < DROUGHT_WINDOW_MIN:
                return None

        next_step = self._relax_step + 1
        params: Dict[str, float] = {
            key: values[next_step]
            for key, values in RELAX_TABLE.items()
        }
        self._relax_step = next_step
        self._applied_at = time.monotonic()
        return params


# ──────────────────────────────────────────────────────────────────────────────
# ErrorRateTracker
# ──────────────────────────────────────────────────────────────────────────────

class ErrorRateTracker:
    """
    TR 호출 성공/실패를 추적해 연속 실패 임계치 초과 시 on_reconnect 콜백 호출.
    모든 스레드에서 record_*() 호출 가능 (내부 Lock 보호).
    """

    def __init__(
        self,
        on_reconnect:    Callable[[], None],
        fail_threshold:  int = TR_FAIL_THRESHOLD,
    ) -> None:
        self._on_reconnect   = on_reconnect
        self._fail_threshold = fail_threshold
        self._lock           = threading.Lock()
        self._consecutive    = 0
        self._total_ok       = 0
        self._total_fail     = 0

    def record_ok(self) -> None:
        with self._lock:
            self._consecutive = 0
            self._total_ok   += 1

    def record_fail(self, tr_code: str = "") -> None:
        trigger = False
        with self._lock:
            self._consecutive += 1
            self._total_fail  += 1
            if self._consecutive >= self._fail_threshold:
                trigger = True
                self._consecutive = 0   # 재요청 후 카운트 리셋

        if trigger:
            logger.error(
                "[ErrorTracker] TR 연속 실패 %d회 → 재연결 요청 (tr=%s)",
                self._fail_threshold, tr_code,
            )
            threading.Thread(
                target=self._safe_reconnect, daemon=True
            ).start()

    def _safe_reconnect(self) -> None:
        try:
            self._on_reconnect()
        except Exception:
            logger.exception("[ErrorTracker] on_reconnect 콜백 예외")

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "consecutive_fail": self._consecutive,
                "total_ok":         self._total_ok,
                "total_fail":       self._total_fail,
            }


# ──────────────────────────────────────────────────────────────────────────────
# HealthMonitor (메인 진입점)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    code:       str
    pnl:        float
    entry_time: str
    exit_time:  str
    reason:     str = ""


class HealthMonitor:
    """
    자동매매 자가 진단 통합 클래스.

    사용 예:
        monitor = HealthMonitor(
            scan_cfg       = cfg,
            on_param_relax = lambda params: cfg.update(**params),
            on_freeze      = kiwoom_mgr.auto_reconnect,
            on_reconnect   = kiwoom_mgr.auto_reconnect,
        )
        monitor.start()

        # 메인 스레드 QTimer (5초마다)
        monitor.ack()

        # 신호 발생 시
        monitor.record_signal(code, name, signal_type)

        # 체결 완료 시
        monitor.record_trade(trade_record)

        # TR 호출 후
        monitor.record_tr_result(tr_code, ok=True)

        # 장 시작 09:00
        monitor.reset_day()
    """

    def __init__(
        self,
        scan_cfg,                                      # SmartScannerConfig 인스턴스
        on_param_relax: Callable[[Dict[str, float]], None],
        on_freeze:      Optional[Callable[[], None]] = None,
        on_reconnect:   Optional[Callable[[], None]] = None,
        log_path:       Path = HEALTH_LOG_PATH,
    ) -> None:
        self._cfg            = scan_cfg
        self._on_param_relax = on_param_relax
        self._log_path       = log_path

        self._event_log      = HealthEventLog(log_path)
        self._drought        = SignalDroughtDetector()

        _freeze_cb           = on_freeze    or self._default_freeze_handler
        _reconnect_cb        = on_reconnect or self._default_reconnect_handler

        self._watchdog       = WatchdogTimer(on_freeze=_freeze_cb)
        self._err_tracker    = ErrorRateTracker(on_reconnect=_reconnect_cb)

        self._running        = False
        self._thread:        Optional[threading.Thread] = None
        self._lock           = threading.Lock()   # _today_trades 보호

        self._today_trades:  List[TradeRecord] = []
        self._today_signals: int               = 0

    # ── 수명 주기 ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._watchdog.start()
        self._thread = threading.Thread(
            target=self._run, name="HealthMonitor", daemon=True
        )
        self._thread.start()
        logger.info("[HealthMonitor] 시작")

    def stop(self) -> None:
        self._running = False
        self._watchdog.stop()
        logger.info("[HealthMonitor] 중지")

    # ── 메인 스레드에서 주기적으로 호출 (QTimer 5s) ──────────────────────────

    def ack(self) -> None:
        """UI가 살아있음을 Watchdog에 알린다. Qt QTimer에서 5초마다 호출."""
        self._watchdog.ack()

    # ── 이벤트 기록 (모든 스레드에서 호출 가능) ──────────────────────────────

    def record_signal(self, code: str, name: str, signal_type: str) -> None:
        self._drought.record_signal()
        with self._lock:
            self._today_signals += 1
        self._event_log.write("SIGNAL", {
            "code":   code,
            "name":   name,
            "signal": signal_type,
        })

    def record_trade(self, trade: TradeRecord) -> None:
        with self._lock:
            self._today_trades.append(trade)
        self._event_log.write("TRADE", {
            "code":       trade.code,
            "pnl":        trade.pnl,
            "entry_time": trade.entry_time,
            "exit_time":  trade.exit_time,
            "reason":     trade.reason,
        })

    def record_tr_result(self, tr_code: str, ok: bool) -> None:
        if ok:
            self._err_tracker.record_ok()
        else:
            self._err_tracker.record_fail(tr_code)
            self._event_log.write("TR_FAIL", {"tr_code": tr_code})

    def record_freeze(self) -> None:
        self._event_log.write("FREEZE", {
            "watchdog_timeout": WATCHDOG_TIMEOUT_SEC,
            "err_stats":        self._err_tracker.stats,
        })

    def reset_day(self) -> None:
        """장 시작(09:00)에 호출해 당일 통계 초기화."""
        self._drought.reset_day()
        with self._lock:
            self._today_trades  = []
            self._today_signals = 0
        self._event_log.write("DAY_RESET", {})
        logger.info("[HealthMonitor] 당일 통계 초기화")

    # ── 배경 스레드 ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self._running:
            time.sleep(MONITOR_TICK_SEC)
            if not self._running:
                break
            try:
                self._check_drought()
                self._check_trade_health()
            except Exception:
                logger.exception("[HealthMonitor] _run 예외")

    def _check_drought(self) -> None:
        params = self._drought.check()
        if params is None:
            return

        step = self._drought.current_step
        logger.warning(
            "[HealthMonitor] 신호 가뭄 완화 step=%d: %s",
            step, {k: v for k, v in params.items()},
        )
        self._event_log.write("DROUGHT_RELAX", {
            "step":   step,
            "params": params,
        })

        # SmartScannerConfig 파라미터 갱신 (메인 스레드가 아님 → setattr 직접 사용)
        # SmartScannerConfig는 dataclass이고 읽기/쓰기가 짧아 GIL 범위 내에서 안전
        try:
            for k, v in params.items():
                if hasattr(self._cfg, k):
                    setattr(self._cfg, k, v)
            self._on_param_relax(params)
        except Exception:
            logger.exception("[HealthMonitor] 파라미터 완화 적용 실패")

    def _check_trade_health(self) -> None:
        """당일 연속 손절이 3회 이상이면 경고 이벤트 기록."""
        with self._lock:
            trades = list(self._today_trades)

        if len(trades) < 3:
            return
        recent = trades[-3:]
        if all(t.pnl < 0 for t in recent):
            losses = [t.pnl for t in recent]
            logger.warning(
                "[HealthMonitor] 연속 손절 3회 감지: %s",
                [f"{p:+,.0f}" for p in losses],
            )
            self._event_log.write("CONSECUTIVE_LOSS", {
                "count":  3,
                "losses": losses,
            })

    # ── 기본 콜백 (on_freeze / on_reconnect 미지정 시) ────────────────────────

    def _default_freeze_handler(self) -> None:
        self.record_freeze()
        logger.critical("[HealthMonitor] UI 프리징 — 재연결 콜백 미등록")

    def _default_reconnect_handler(self) -> None:
        logger.error("[HealthMonitor] TR 재연결 — 재연결 콜백 미등록")

    # ── 일별 요약 (FeedbackEngine.run_daily() 에서 호출) ──────────────────────

    def build_daily_summary(self) -> dict:
        """
        당일 HealthEventLog를 읽어 FeedbackEngine이 활용할 수 있는 요약 딕트 반환.
        run_daily() 호출 전 main thread에서 사용.
        """
        events     = self._event_log.read_today()
        signals    = [e for e in events if e["type"] == "SIGNAL"]
        trades     = [e for e in events if e["type"] == "TRADE"]
        freezes    = [e for e in events if e["type"] == "FREEZE"]
        tr_fails   = [e for e in events if e["type"] == "TR_FAIL"]
        relaxations= [e for e in events if e["type"] == "DROUGHT_RELAX"]
        cons_loss  = [e for e in events if e["type"] == "CONSECUTIVE_LOSS"]

        realized   = [t["pnl"] for t in trades if "pnl" in t]
        win_count  = sum(1 for p in realized if p > 0)
        loss_count = sum(1 for p in realized if p <= 0)

        return {
            "date":            datetime.now().strftime("%Y-%m-%d"),
            "signal_count":    len(signals),
            "trade_count":     len(trades),
            "win_count":       win_count,
            "loss_count":      loss_count,
            "total_pnl":       sum(realized),
            "win_rate":        win_count / len(realized) if realized else 0.0,
            "freeze_count":    len(freezes),
            "tr_fail_count":   len(tr_fails),
            "relaxation_steps":len(relaxations),
            "consec_loss_events": len(cons_loss),
            "err_stats":       self._err_tracker.stats,
        }
