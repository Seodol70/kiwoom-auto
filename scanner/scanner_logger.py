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


class _WinSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Windows 호환 RotatingFileHandler (copy+truncate 방식).
    파일이 VS Code나 다른 툴에 의해 열려 있어도 안전하게 회전(Rollover) 가능하다.
    """
    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        for i in range(self.backupCount - 1, 0, -1):
            sfn = self.rotation_filename(f"{self.baseFilename}.{i}")
            dfn = self.rotation_filename(f"{self.baseFilename}.{i + 1}")
            if os.path.exists(sfn):
                if os.path.exists(dfn):
                    os.remove(dfn)
                os.rename(sfn, dfn)

        dfn = self.rotation_filename(f"{self.baseFilename}.1")
        if os.path.exists(dfn):
            os.remove(dfn)
        if os.path.exists(self.baseFilename):
            shutil.copy2(self.baseFilename, dfn)
            with open(self.baseFilename, "w", encoding=self.encoding or "utf-8"):
                pass

        if not self.delay:
            self.stream = self._open()


def _build_scan_logger(log_dir: str = "logs") -> logging.Logger:
    """scanner.log 전용 로거 ('scanner.audit') 빌드."""
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("scanner.audit")
    logger.setLevel(logging.DEBUG)
    logger.propagate = True

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
                    # 첫 행을 헤더 기준으로 삼음
                    fieldnames = list(rows[0].keys())
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
        """최종 신호 기록."""
        reason = f"[{sig.signal_type}] {sig.reason}"
        # Handler 형식: "PASS/FAIL\tcode\tname\tstep\treason" (신호는 PASS 취급)
        scan_log.warning("PASS\t%s\t%s\tSIGNAL\t%s", sig.code, sig.name, reason)
        ScannerLogger._buffer_csv("scanner_signal.csv", sig.code, sig.name, reason, sig.values or {})

    @staticmethod
    def near_miss(
        code: str, name: str, filter_name: str,
        actual=None, threshold=None, reason: str = "",
    ) -> None:
        """거의 통과할 뻔한 탈락 (near-miss) 기록 — DEBUG 레벨."""
        detail = reason or f"actual={actual} threshold={threshold}"
        scan_log.debug("⚡ [근접탈락] %s(%s) [%s] %s", code, name, filter_name, detail)
