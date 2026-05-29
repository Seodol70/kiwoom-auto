"""
diagnostics.py — 시스템 자체 진단기 (Self-Diagnostics)

[2026-05-29] 매번 사용자가 로그 붙여서 진단 요청하는 비효율 해결.
프로그램 시작 후 5분 시점에 자동 진단 1회 + UI 메뉴에서 on-demand 호출.

진단 항목 (5개 카테고리):
  1. TR/API 건강도 — TIMEOUT 횟수, CircuitBreaker 상태, 응답 누락
  2. WARNING/ERROR 폭주 — 분당 발생량, 동일 메시지 반복
  3. 데이터 무결성 — 가격 0원 종목 비율, 일봉 누락, 캐시 hit율
  4. 신호/필터 정상 동작 — 신호 발생량, 거절 사유 분포
  5. 프로그램 오류 — Traceback, 예외 패턴, 메서드 누락

출력:
  - logs/diagnostics.log (구조화 JSON, 항상)
  - UI LogPanel (CRITICAL 등급일 때만)
"""

from __future__ import annotations
import os
import re
import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import IntEnum

from PyQt5.QtCore import QObject, pyqtSignal, QTimer


class Severity(IntEnum):
    """진단 결과 심각도."""
    OK       = 0
    INFO     = 1
    WARNING  = 2
    CRITICAL = 3


@dataclass
class DiagnosticFinding:
    """단일 진단 결과."""
    category: str           # "TR_API" | "LOG_FLOOD" | "DATA" | "SIGNAL" | "ERROR"
    severity: int           # Severity 값
    title: str              # 짧은 제목
    detail: str             # 상세 설명
    metric: Dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""    # 권장 조치

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity_name"] = Severity(self.severity).name
        return d


# ──────────────────────────────────────────────────────────────────────────────
# 로그 라인 파서
# ──────────────────────────────────────────────────────────────────────────────

# 시스템 로그 포맷 예시:
# 2026-05-29 09:17:50	WARNING	scanner.smart_scanner	메시지...
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>[\w.]+)\s+"
    r"(?P<msg>.*)$"
)


def _parse_log_line(line: str) -> Optional[dict]:
    m = _LOG_LINE_RE.match(line.rstrip())
    if not m:
        return None
    return m.groupdict()


# ──────────────────────────────────────────────────────────────────────────────
# 진단 룰 (각 카테고리별 1개 함수)
# ──────────────────────────────────────────────────────────────────────────────

def _diagnose_tr_api(lines: List[dict]) -> List[DiagnosticFinding]:
    """TR/API 건강도."""
    out = []

    # TIMEOUT 카운트 (rq별)
    timeouts = Counter()
    cb_active = set()
    for ln in lines:
        msg = ln["msg"]
        if "TIMEOUT 발동" in msg:
            m = re.search(r"rq=(\S+)", msg)
            if m:
                timeouts[m.group(1)] += 1
        if "CircuitBreaker" in msg and "활성" in msg:
            m = re.search(r"(opt\d+)", msg)
            if m:
                cb_active.add(m.group(1))

    total_timeouts = sum(timeouts.values())
    if total_timeouts >= 20:
        out.append(DiagnosticFinding(
            category="TR_API",
            severity=Severity.CRITICAL,
            title=f"TR TIMEOUT 폭주 — 총 {total_timeouts}회",
            detail=f"분석 구간 내 TR 응답 실패 {total_timeouts}회: {dict(timeouts.most_common(5))}",
            metric={"total": total_timeouts, "by_rq": dict(timeouts)},
            suggestion="해당 TR이 메인 스레드 동기 호출인지 확인. opt10080 등은 워커 스레드 분리 필요.",
        ))
    elif total_timeouts >= 5:
        out.append(DiagnosticFinding(
            category="TR_API",
            severity=Severity.WARNING,
            title=f"TR TIMEOUT 누적 — {total_timeouts}회",
            detail=f"누적된 TIMEOUT: {dict(timeouts.most_common(5))}",
            metric={"total": total_timeouts, "by_rq": dict(timeouts)},
        ))

    if cb_active:
        out.append(DiagnosticFinding(
            category="TR_API",
            severity=Severity.WARNING,
            title=f"CircuitBreaker 활성 — {len(cb_active)}개 TR 차단 중",
            detail=f"차단 TR: {sorted(cb_active)}. 15분 후 자동 해제.",
            metric={"blocked_trs": sorted(cb_active)},
            suggestion="opt10030 차단은 모의투자 환경 정상. 운영 환경에서는 호출 빈도 확인.",
        ))

    # balance 응답 손상 감지
    balance_corrupt = sum(1 for ln in lines
                          if "get_balance" in ln["msg"] and "필드 미획득" in ln["msg"])
    if balance_corrupt >= 3:
        out.append(DiagnosticFinding(
            category="TR_API",
            severity=Severity.CRITICAL,
            title=f"잔고 응답 손상 — {balance_corrupt}회",
            detail="opw00001 응답에서 예수금/평가금액 필드가 비어 있음. 다른 TR 응답이 섞였을 가능성.",
            metric={"count": balance_corrupt},
            suggestion="kiwoom_api._on_receive_tr_data의 rq_name 가드 동작 확인.",
        ))

    return out


def _diagnose_log_flood(lines: List[dict]) -> List[DiagnosticFinding]:
    """WARNING/ERROR 폭주."""
    out = []

    # 분당 WARNING 카운트
    per_min: Dict[str, Counter] = defaultdict(Counter)
    for ln in lines:
        if ln["level"] in ("WARNING", "ERROR", "CRITICAL"):
            mn = ln["ts"][:16]  # YYYY-MM-DD HH:MM
            per_min[mn][ln["level"]] += 1

    # 분당 30건 초과 분 검출 (SysLogQtHandler 한계)
    flood_minutes = []
    for mn, counter in per_min.items():
        total = sum(counter.values())
        if total >= 30:
            flood_minutes.append((mn, total, dict(counter)))

    if flood_minutes:
        flood_minutes.sort(key=lambda x: -x[1])
        top = flood_minutes[0]
        out.append(DiagnosticFinding(
            category="LOG_FLOOD",
            severity=Severity.CRITICAL,
            title=f"WARNING 폭주 — {top[0]} {top[1]}건",
            detail=f"분당 30건 초과한 시점: {len(flood_minutes)}개 분. "
                   f"최대: {top[0]} = {top[1]}건 ({top[2]})",
            metric={"flood_minutes": [{"min": m, "count": c} for m, c, _ in flood_minutes[:5]]},
            suggestion="해당 시점의 WARNING 메시지를 DEBUG로 격하 또는 dedup 처리 필요.",
        ))

    # 동일 메시지 반복 (블랙리스트 무한루프 패턴) — 정상 주기 메시지는 제외
    # UI큐-EMIT, UI통합 송신 완료 등은 1초마다 정상 발행되므로 제외
    NORMAL_REPEAT_PATTERNS = (
        "[UI큐-EMIT]", "[UI통합]", "[UI큐]", "[opt10030] 캐시",
        "[주기 스캔]", "[일봉갱신]", "[STEP-H async]",
        "잔고 동기화", "감시 중인 종목 수",
    )
    msg_repeat: Counter = Counter()
    for ln in lines:
        if ln["level"] in ("WARNING", "INFO"):
            msg_80 = ln["msg"][:80]
            if any(p in msg_80 for p in NORMAL_REPEAT_PATTERNS):
                continue
            # 메시지에서 가변 부분 제거 (코드, 수치)
            key = re.sub(r"\b\d{4,}\b", "N", msg_80)
            msg_repeat[key] += 1

    repeats = [(k, v) for k, v in msg_repeat.items() if v >= 100]
    if repeats:
        repeats.sort(key=lambda x: -x[1])
        top_msg, top_cnt = repeats[0]
        out.append(DiagnosticFinding(
            category="LOG_FLOOD",
            severity=Severity.CRITICAL,
            title=f"동일 메시지 반복 — {top_cnt}회 ({top_msg[:50]}...)",
            detail=f"동일 패턴 100회 이상 반복된 메시지: {len(repeats)}개. "
                   f"최다: {top_cnt}회 — '{top_msg}'",
            metric={"repeats": [{"msg": k[:80], "count": v} for k, v in repeats[:5]]},
            suggestion="무한루프 또는 dedup 누락. 5/22 에코프로 435회, 5/28 차백신 811회와 같은 패턴.",
        ))

    return out


def _diagnose_data_integrity(lines: List[dict]) -> List[DiagnosticFinding]:
    """데이터 무결성."""
    out = []

    # opt10030 0개 반환 횟수
    zero_returns = sum(1 for ln in lines
                       if "0개 반환" in ln["msg"] or "opt10030] 0개" in ln["msg"])
    if zero_returns >= 3:
        out.append(DiagnosticFinding(
            category="DATA",
            severity=Severity.WARNING,
            title=f"opt10030 0개 응답 {zero_returns}회",
            detail="거래대금 상위 조회 시 0개 응답이 반복됨. 모의투자 환경이면 정상.",
            metric={"count": zero_returns},
        ))

    # 일봉 데이터 부족 차단
    no_daily = sum(1 for ln in lines if "BREAKOUT_NO_DAILY" in ln["msg"])
    if no_daily >= 5:
        out.append(DiagnosticFinding(
            category="DATA",
            severity=Severity.INFO,
            title=f"일봉 데이터 부족으로 BREAKOUT 차단 — {no_daily}회",
            detail="장 초반 일봉 미로딩 종목의 BREAKOUT 신호가 안전 차단됨. 정상 동작.",
            metric={"count": no_daily},
        ))

    # NameError / AttributeError 감지
    py_errors = []
    for ln in lines:
        if ln["level"] in ("ERROR", "CRITICAL"):
            m = re.search(r"(NameError|AttributeError|TypeError|KeyError|ValueError):\s*(.+)", ln["msg"])
            if m:
                py_errors.append((m.group(1), m.group(2)[:80]))
        # 메서드 누락 경고도 포착 (실제 사례)
        if "has no attribute" in ln["msg"]:
            m = re.search(r"has no attribute '(\w+)'", ln["msg"])
            if m:
                py_errors.append(("AttributeError", f"missing method: {m.group(1)}"))

    if py_errors:
        err_counter = Counter(py_errors)
        out.append(DiagnosticFinding(
            category="DATA",
            severity=Severity.CRITICAL,
            title=f"Python 예외 감지 — {len(py_errors)}건",
            detail=f"예외 종류별: {dict(err_counter.most_common(5))}",
            metric={"errors": [{"type": t, "msg": m, "count": c} for (t, m), c in err_counter.most_common(10)]},
            suggestion="코드 변경 후 import 검증을 수행하지 않은 경우 발생. 즉시 수정 필요.",
        ))

    return out


def _diagnose_signal_filter(lines: List[dict]) -> List[DiagnosticFinding]:
    """신호/필터 정상 동작."""
    out = []

    # 신호 발생 카운트
    signals = Counter()
    for ln in lines:
        if "신호발생" in ln["msg"]:
            m = re.search(r"\[(BREAKOUT|PULLBACK|JDM_ENTRY|EOD|OVERHEAT_PULLBACK)\]", ln["msg"])
            if m:
                signals[m.group(1)] += 1

    # 진입거절 카운트
    rejects = Counter()
    for ln in lines:
        if "진입거절" in ln["msg"]:
            for kw in ("약한신호", "개장1시간", "냉각기", "OP눌림목", "MagicMock", "포지션", "예수금", "섹터", "블랙리스트"):
                if kw in ln["msg"]:
                    rejects[kw] += 1
                    break

    if signals or rejects:
        out.append(DiagnosticFinding(
            category="SIGNAL",
            severity=Severity.INFO,
            title=f"신호 {sum(signals.values())}건 / 거절 {sum(rejects.values())}건",
            detail=f"신호: {dict(signals)} | 거절: {dict(rejects.most_common(5))}",
            metric={"signals": dict(signals), "rejects": dict(rejects)},
        ))

        # 신호는 발생하는데 진입 0건이면 경고
        total_signals = sum(signals.values())
        if total_signals >= 5 and rejects.get("약한신호", 0) == total_signals:
            out.append(DiagnosticFinding(
                category="SIGNAL",
                severity=Severity.WARNING,
                title="모든 신호가 trend_lv 필터에서 차단됨",
                detail=f"신호 {total_signals}건 모두 약한신호로 거절. trend_lv 필터가 과도하게 엄격할 가능성.",
                metric={"signals": total_signals, "blocked": rejects["약한신호"]},
                suggestion="OPENING 시간대(09:00~09:30)는 1분봉 22개 미만이라 trend_lv=0 가능성. 시간대별 분리 검토.",
            ))

    return out


def _diagnose_errors(lines: List[dict]) -> List[DiagnosticFinding]:
    """Traceback 및 명시적 예외."""
    out = []

    traceback_count = sum(1 for ln in lines if "Traceback" in ln["msg"])
    if traceback_count >= 1:
        out.append(DiagnosticFinding(
            category="ERROR",
            severity=Severity.CRITICAL,
            title=f"Traceback {traceback_count}건 감지",
            detail="Python 예외가 발생함. 코드 변경 후 즉시 수정 필요.",
            metric={"count": traceback_count},
            suggestion="logs/system.log에서 'Traceback' 주변 라인을 직접 확인.",
        ))

    return out


# ──────────────────────────────────────────────────────────────────────────────
# 메인 진단기
# ──────────────────────────────────────────────────────────────────────────────

class SystemDiagnostics(QObject):
    """
    시스템 자체 진단기.

    동작:
      1. 60초 주기 워치독 — 최근 2분 WARNING 폭주·하드스탑 반복 실시간 감시
         → CRITICAL 감지 즉시 UI LogPanel 경고
      2. 5분 후 자동 전체 진단 (스냅샷) → logs/diagnostics.log 기록
      3. on-demand 호출 가능 (run_now)

    [2026-05-29] 배경:
      대시보드 멈춤 직전 패턴 분석(5/27~5/29 14건):
        - 분당 WARNING 30건+ → SysLogQtHandler 큐 과부하
        - 하드스탑 동일 종목 수십 회 반복 → TR 큐 폭주 유발
        - min_candle TIMEOUT 연속 → 메인 스레드 동기 블로킹
      워치독이 이 패턴을 2분 이내에 감지하고 UI에 경고.
    """

    # UI에 위험 등급 발견 시 emit (LogPanel 표시용)
    critical_finding = pyqtSignal(str)  # 한 줄 메시지

    # 워치독: 분당 WARNING 임계치 (이 이상이면 즉시 경고)
    WATCHDOG_WARN_PER_MIN = 20   # 20건/분 이상이면 WARNING
    WATCHDOG_CRIT_PER_MIN = 40   # 40건/분 이상이면 CRITICAL (UI 멈춤 임박)

    def __init__(self, log_path: str = "logs/system.log",
                 diag_log_path: str = "logs/diagnostics.log",
                 lookback_min: int = 10,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.log_path = log_path
        self.diag_log_path = diag_log_path
        self.lookback_min = lookback_min
        self._timer: Optional[QTimer] = None
        self._watchdog_timer: Optional[QTimer] = None
        self._logger = logging.getLogger("diagnostics")
        self._last_warn_alert_ts: float = 0.0   # 같은 경고 반복 방지

        # diagnostics.log 디렉토리 보장
        os.makedirs(os.path.dirname(diag_log_path) or ".", exist_ok=True)

    def schedule_initial_run(self, delay_sec: int = 300) -> None:
        """프로그램 시작 후 전체 진단 + 워치독 가동."""
        # 1) 전체 진단 — delay_sec 후 1회
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.run_now)
        self._timer.start(delay_sec * 1000)
        self._logger.info("[Diagnostics] 자동 전체 진단 예약 — %d초 후 실행", delay_sec)

        # 2) 워치독 — 60초마다 WARNING 폭주 감시 (즉시 시작)
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.timeout.connect(self._watchdog_tick)
        self._watchdog_timer.start(60_000)
        self._logger.info("[Diagnostics] 워치독 가동 — 60초 주기 WARNING 폭주 감시")

    def _watchdog_tick(self) -> None:
        """60초마다 호출 — 최근 2분 로그에서 WARNING 폭주·하드스탑 반복 감지."""
        try:
            self._check_warning_flood()
            self._check_hardstop_loop()
        except Exception as e:
            self._logger.debug("[Diagnostics] 워치독 오류: %s", e)

    def _check_warning_flood(self) -> None:
        """최근 2분 이내 WARNING 폭주 감지 — UI 멈춤 선행 지표."""
        if not os.path.exists(self.log_path):
            return

        now = datetime.now()
        cutoff_str = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        per_min: Counter = Counter()

        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            # 파일 끝 5000라인만 (빠른 처리)
            lines = f.readlines()[-5000:]

        for line in lines:
            d = _parse_log_line(line)
            if d and d["ts"] >= cutoff_str and d["level"] in ("WARNING", "ERROR"):
                mn = d["ts"][:16]
                per_min[mn] += 1

        for mn, cnt in per_min.items():
            if cnt >= self.WATCHDOG_CRIT_PER_MIN:
                # 같은 분에 대해 반복 경고 방지 (60초 쿨다운)
                if time.monotonic() - self._last_warn_alert_ts < 60:
                    return
                self._last_warn_alert_ts = time.monotonic()
                msg = f"🚨 [워치독] WARNING 폭주 감지 — {mn} {cnt}건/분 (임계: {self.WATCHDOG_CRIT_PER_MIN}건). UI 멈춤 위험!"
                self._logger.warning(msg)
                self.critical_finding.emit(msg)
            elif cnt >= self.WATCHDOG_WARN_PER_MIN:
                if time.monotonic() - self._last_warn_alert_ts < 60:
                    return
                self._last_warn_alert_ts = time.monotonic()
                msg = f"⚠️ [워치독] WARNING 증가 — {mn} {cnt}건/분 (주의: {self.WATCHDOG_WARN_PER_MIN}건)"
                self._logger.info(msg)
                self.critical_finding.emit(msg)

    def _check_hardstop_loop(self) -> None:
        """최근 2분 이내 동일 종목 하드스탑 10회+ 반복 감지."""
        if not os.path.exists(self.log_path):
            return

        cutoff_str = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        hardstop_counter: Counter = Counter()

        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-3000:]

        for line in lines:
            d = _parse_log_line(line)
            if d and d["ts"] >= cutoff_str and "하드스탑" in d["msg"] and "임계치 돌파" in d["msg"]:
                m = re.search(r"\((\d{6})\)", d["msg"])
                if m:
                    hardstop_counter[m.group(1)] += 1

        for code, cnt in hardstop_counter.items():
            if cnt >= 10:
                if time.monotonic() - self._last_warn_alert_ts < 60:
                    return
                self._last_warn_alert_ts = time.monotonic()
                msg = (f"🚨 [워치독] 하드스탑 반복 — 종목 {code} {cnt}회 연속. "
                       f"매도 미체결 + WARNING 폭주로 이어질 수 있음!")
                self._logger.warning(msg)
                self.critical_finding.emit(msg)

    def run_now(self) -> List[DiagnosticFinding]:
        """즉시 진단 실행. 결과 리스트 반환."""
        started = time.monotonic()
        try:
            lines = self._read_recent_lines()
        except Exception as e:
            self._logger.error("[Diagnostics] 로그 읽기 실패: %s", e)
            return []

        findings: List[DiagnosticFinding] = []
        for rule_fn in (_diagnose_tr_api, _diagnose_log_flood,
                        _diagnose_data_integrity, _diagnose_signal_filter,
                        _diagnose_errors):
            try:
                findings.extend(rule_fn(lines))
            except Exception as e:
                self._logger.warning("[Diagnostics] 룰 %s 실행 실패: %s", rule_fn.__name__, e)

        self._write_diagnostics_log(findings, len(lines), time.monotonic() - started)
        self._notify_ui_if_critical(findings)
        return findings

    def _read_recent_lines(self) -> List[dict]:
        """최근 lookback_min 분 이내의 로그 라인을 파싱하여 반환."""
        if not os.path.exists(self.log_path):
            return []

        cutoff = datetime.now() - timedelta(minutes=self.lookback_min)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

        parsed: List[dict] = []
        # 파일 끝부터 5만 라인까지만 읽음 (메모리 보호)
        with open(self.log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-50000:]

        for line in lines:
            d = _parse_log_line(line)
            if d and d["ts"] >= cutoff_str:
                parsed.append(d)
        return parsed

    def _write_diagnostics_log(self, findings: List[DiagnosticFinding],
                               line_count: int, elapsed: float) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "lookback_min": self.lookback_min,
            "lines_scanned": line_count,
            "elapsed_sec": round(elapsed, 3),
            "summary": self._summary(findings),
            "findings": [f.to_dict() for f in findings],
        }
        try:
            with open(self.diag_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            self._logger.warning("[Diagnostics] 로그 기록 실패: %s", e)

        self._logger.info("[Diagnostics] %d개 라인 스캔 → %d건 발견 (%.2fs)",
                          line_count, len(findings), elapsed)

    @staticmethod
    def _summary(findings: List[DiagnosticFinding]) -> dict:
        s: Counter = Counter()
        for f in findings:
            s[Severity(f.severity).name] += 1
        return dict(s)

    def _notify_ui_if_critical(self, findings: List[DiagnosticFinding]) -> None:
        crits = [f for f in findings if f.severity == Severity.CRITICAL]
        if not crits:
            return
        for f in crits:
            self.critical_finding.emit(f"🚨 [진단] {f.title}")
