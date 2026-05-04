"""
ScannerLogger — 스캐너 신호 로깅

smart_scanner.py 에서 분리.
로거 이름 'scanner.audit' 유지 (LogPanel.append_scanner 슬롯 호환).
"""
import logging
import logging.handlers
import os
import csv
import shutil
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


class ScannerLogger:
    """신호 선정/탈락 기록 담당"""

    @staticmethod
    def passed(code: str, name: str, filter_name: str, detail: str = "", values: dict = None) -> None:
        """선정된 신호 기록."""
        reason = f"[{filter_name}] {detail}" if detail else filter_name
        msg = f"✅ [통과] {code}({name}) {reason}"
        scan_log.info(msg)
        if values:
            ScannerLogger._write_csv("scanner_passed.csv", code, name, reason, values)

    @staticmethod
    def rejected(code: str, name: str, filter_name: str, detail: str = "") -> None:
        """탈락 신호 기록."""
        reason = f"[{filter_name}] {detail}" if detail else filter_name
        msg = f"❌ [탈락] {code}({name}) {reason}"
        scan_log.debug(msg)
        ScannerLogger._write_csv("scanner_rejected.csv", code, name, reason, {})

    @staticmethod
    def signal(sig) -> None:
        """최종 신호 기록.

        Args:
            sig: ScanSignal 객체
        """
        msg = f"🚨 [신호] {sig.code}({sig.name}) [{sig.signal_type}] {sig.reason}"
        scan_log.warning(msg)
        ScannerLogger._write_csv("scanner_signal.csv", sig.code, sig.name, sig.reason, sig.values or {})

    @staticmethod
    def near_miss(
        code: str, name: str, filter_name: str,
        actual=None, threshold=None, reason: str = "",
    ) -> None:
        """거의 통과할 뻔한 탈락 (near-miss) 기록 — DEBUG 레벨."""
        detail = reason or f"actual={actual} threshold={threshold}"
        msg = f"⚡ [근접탈락] {code}({name}) [{filter_name}] {detail}"
        scan_log.debug(msg)

    @staticmethod
    def _write_csv(filename: str, code: str, name: str, reason: str, values: dict) -> None:
        """CSV 파일 기록 (선택 사항).

        Args:
            filename: CSV 파일명
            code: 종목 코드
            name: 종목명
            reason: 사유
            values: 추가 값
        """
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        csv_path = log_dir / filename

        try:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                row = [datetime.now().isoformat(), code, name, reason]
                row.extend(values.values())
                writer.writerow(row)
        except Exception as e:
            scan_log.error(f"CSV 기록 실패: {filename}, {e}")
