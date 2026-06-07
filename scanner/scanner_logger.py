"""
ScannerLogger — 스캐너 신호 로깅

smart_scanner.py 에서 분리.
로거 이름 'scanner.audit' 유지 (LogPanel.append_scanner 슬롯 호환).
배치 기록 방식으로 I/O 최적화 (TradeAuditLogger 패턴 동일 적용).
"""
import logging
import logging.handlers
import os
import csv
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime


from logging_config import WinSafeRotatingFileHandler as _WinSafeRotatingFileHandler


def _build_scan_logger(log_dir: str = "logs") -> logging.Logger:
    """scanner.log 전용 로거 ('scanner.audit') 빌드."""
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("scanner.audit")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    handler = _WinSafeRotatingFileHandler(
        filename=os.path.join(log_dir, "scanner.log"),
        maxBytes=20 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s\t%(levelname)s\t%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    return logger


scan_log = _build_scan_logger()
scan_log.info("--- ScannerLogger Initialized (Session Start: %s) ---", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

# CSV 컬럼 정의
_CSV_BASE_FIELDS = ["timestamp", "code", "name", "reason"]

# 신호 CSV에 반드시 포함할 선행지표 컬럼 —
# 첫 신호가 JDM 타입이 아닐 때도 헤더 일관성을 보장하기 위해 명시적으로 선언.
_SIGNAL_LI_COLS = [
    "li_bs", "li_vb", "li_cr", "li_ca", "li_hp", "li_hv", "li_aw", "li_tv", "li_leading"
]


class ScannerLogger:
    """신호 선정/탈락 기록 담당 — 배치 기록 방식으로 I/O 최적화"""

    # 클래스 레벨 배치 버퍼 (파일별)
    _write_buffers: dict[str, list] = {
        "scanner_passed.csv": [],
        "scanner_rejected.csv": [],
        "scanner_signal.csv": [],
    }
    _batch_lock = threading.Lock()
    _flush_interval_sec = 5.0
    _batch_size_limit = 50        # 이 건수 이상이면 즉시 플러시
    _last_flush_time = datetime.now()
    _stop_event = threading.Event()
    _bg_thread: threading.Thread | None = None

    # [FIX 2026-06-04] passed() UI 중복 로그 방지 — 동일 (code, step) 60초 쿨다운
    _passed_cooldown: dict[tuple, float] = {}
    _passed_cooldown_sec: float = 60.0

    @classmethod
    def _ensure_bg_thread(cls) -> None:
        """백그라운드 flush 스레드가 없으면 시작."""
        if cls._bg_thread is None or not cls._bg_thread.is_alive():
            cls._stop_event.clear()
            cls._bg_thread = threading.Thread(
                target=cls._bg_flush_loop, daemon=True, name="ScannerLogger-Flush"
            )
            cls._bg_thread.start()

    @classmethod
    def _bg_flush_loop(cls) -> None:
        """5초마다 버퍼를 파일에 기록."""
        while not cls._stop_event.is_set():
            wait = max(0.1, cls._flush_interval_sec - (
                datetime.now() - cls._last_flush_time
            ).total_seconds())
            if cls._stop_event.wait(wait):
                break
            with cls._batch_lock:
                cls._do_batch_write()

    @classmethod
    def _do_batch_write(cls) -> None:
        """버퍼에 쌓인 항목을 각 CSV 파일에 일괄 기록. Lock 내부에서 호출."""
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)

        for filename, rows in cls._write_buffers.items():
            if not rows:
                continue
            csv_path = log_dir / filename
            file_exists = csv_path.exists()
            try:
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    # 배치 내 모든 행의 키 합집합으로 헤더 결정 (삽입 순서 보존)
                    fieldnames = list(dict.fromkeys(k for row in rows for k in row.keys()))
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    if not file_exists:
                        writer.writeheader()
                    for row in rows:
                        writer.writerow(row)
                rows.clear()
            except Exception as e:
                scan_log.error("CSV 배치 기록 실패: %s — %s", filename, e)

        cls._last_flush_time = datetime.now()

    @classmethod
    def _buffer_csv(cls, filename: str, code: str, name: str, reason: str, values: dict) -> None:
        """CSV 행을 버퍼에 추가하고, 임계값 초과 시 즉시 플러시."""
        cls._ensure_bg_thread()

        row = {
            "timestamp": datetime.now().isoformat(),
            "code": code,
            "name": name,
            "reason": reason,
        }
        row.update(values)

        with cls._batch_lock:
            if filename not in cls._write_buffers:
                cls._write_buffers[filename] = []
            cls._write_buffers[filename].append(row)
            total = sum(len(v) for v in cls._write_buffers.values())
            if total >= cls._batch_size_limit:
                cls._do_batch_write()

    @classmethod
    def flush(cls) -> None:
        """장 마감·프로그램 종료 시 잔여 버퍼를 즉시 기록."""
        with cls._batch_lock:
            cls._do_batch_write()

    @classmethod
    def stop(cls) -> None:
        """백그라운드 스레드 중지 및 잔여 데이터 저장."""
        cls._stop_event.set()
        cls.flush()

    @staticmethod
    def passed(code: str, name: str, filter_name: str, detail: str = "", values: dict = None) -> None:
        """선정된 신호 기록."""
        reason = detail if detail else filter_name
        # [FIX 2026-06-04] 동일 (code, step) 60초 내 반복 호출 시 UI 로그만 스킵
        # 평가 루프가 1초마다 돌며 passed()를 계속 호출해 UI가 도배되는 문제 방지.
        # 파일(CSV) 기록은 그대로 유지.
        _key = (code, filter_name)
        _now = time.monotonic()
        _last = ScannerLogger._passed_cooldown.get(_key, 0.0)
        if _now - _last < ScannerLogger._passed_cooldown_sec:
            # UI 로그 스킵, 파일만 기록
            ScannerLogger._buffer_csv("scanner_passed.csv", code, name, f"[{filter_name}] {reason}", values or {})
            return
        ScannerLogger._passed_cooldown[_key] = _now
        # Handler 형식: "PASS/FAIL\tcode\tname\tstep\treason"
        scan_log.info("PASS\t%s\t%s\t%s\t%s", code, name, filter_name, reason)
        ScannerLogger._buffer_csv("scanner_passed.csv", code, name, f"[{filter_name}] {reason}", values or {})
    
    @staticmethod
    def rejected(code: str, name: str, filter_name: str, detail: str = "") -> None:
        """탈락 신호 기록."""
        reason = detail if detail else filter_name
        # Handler 형식: "PASS/FAIL\tcode\tname\tstep\treason"
        scan_log.debug("FAIL\t%s\t%s\t%s\t%s", code, name, filter_name, reason)
        ScannerLogger._buffer_csv("scanner_rejected.csv", code, name, f"[{filter_name}] {reason}", {})

    @staticmethod
    def signal(sig) -> None:
        """최종 신호 기록 — 날짜별 파일로 저장하여 컬럼 변경 시 헤더 충돌 방지."""
        reason = f"[{sig.signal_type}] {sig.reason}"
        scan_log.warning("PASS\t%s\t%s\tSIGNAL\t%s", sig.code, sig.name, reason)
        today = datetime.now().strftime("%Y%m%d")
        values = dict(sig.values or {})
        # GAP_PULLBACK 등 JDM 외 신호가 먼저 기록되어도 li_ 헤더가 누락되지 않도록
        # 모든 선행지표 컬럼을 기본값("")으로 보장
        for col in _SIGNAL_LI_COLS:
            values.setdefault(col, "")
        ScannerLogger._buffer_csv(f"scanner_signal_{today}.csv", sig.code, sig.name, reason, values)

    @staticmethod
    def near_miss(
        code: str, name: str, filter_name: str,
        actual=None, threshold=None, reason: str = "",
    ) -> None:
        """거의 통과할 뻔한 탈락 (near-miss) 기록 — DEBUG 레벨."""
        detail = reason or f"actual={actual} threshold={threshold}"
        # Handler 형식: "PASS/FAIL/NEAR\tcode\tname\tstep\treason"
        scan_log.debug("NEAR\t%s\t%s\t%s\t%s", code, name, filter_name, detail)
