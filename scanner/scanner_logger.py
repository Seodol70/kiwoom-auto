"""
ScannerLogger — 스캐너 신호 로깅

smart_scanner.py 에서 분리.
로거 이름 'scanner.audit' 유지 (LogPanel.append_scanner 슬롯 호환).
"""
import logging
import csv
from pathlib import Path
from datetime import datetime


_scan_log = logging.getLogger("scanner.audit")


class ScannerLogger:
    """신호 선정/탈락 기록 담당"""

    @staticmethod
    def passed(code: str, name: str, reason: str, values: dict = None) -> None:
        """선정된 신호 기록.

        Args:
            code: 종목 코드
            name: 종목명
            reason: 신호 발생 사유
            values: 추가 메타 데이터 (RSI, EMA 등)
        """
        msg = f"✅ [통과] {code}({name}) {reason}"
        _scan_log.info(msg)
        if values:
            ScannerLogger._write_csv("scanner_passed.csv", code, name, reason, values)

    @staticmethod
    def rejected(code: str, name: str, reason: str) -> None:
        """탈락 신호 기록.

        Args:
            code: 종목 코드
            name: 종목명
            reason: 탈락 사유
        """
        msg = f"❌ [탈락] {code}({name}) {reason}"
        _scan_log.debug(msg)
        ScannerLogger._write_csv("scanner_rejected.csv", code, name, reason, {})

    @staticmethod
    def signal(sig) -> None:
        """최종 신호 기록.

        Args:
            sig: ScanSignal 객체
        """
        msg = f"🚨 [신호] {sig.code}({sig.name}) [{sig.signal_type}] {sig.reason}"
        _scan_log.warning(msg)
        ScannerLogger._write_csv("scanner_signal.csv", sig.code, sig.name, sig.reason, sig.values or {})

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
            _scan_log.error(f"CSV 기록 실패: {filename}, {e}")
