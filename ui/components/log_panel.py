from __future__ import annotations
import os, sys, time, threading, logging, logging.handlers
from datetime import datetime
from typing import Optional


import pyqtgraph as pg
from PyQt5.QtCore import Qt, QObject, QThread, QTimer, QEvent, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QSplitter,
    QFrame, QHeaderView, QSizePolicy, QProgressBar, QDoubleSpinBox, QSpinBox,
    QDialog, QDialogButtonBox, QComboBox, QGroupBox, QAction, QMenu
)


from config import TELEGRAM as _TG
from telegram_bot import TelegramBot
from scanner.smart_scanner import format_trade_amount_korean


class ScannerLogHandler(QObject, logging.Handler):
    """
    scanner.audit 로거의 INFO(PASS) 메시지를 Qt 시그널로 메인 스레드에 중계한다.


    ScannerWorker 스레드에서 emit() 호출 → log_entry 시그널 → 메인 스레드 슬롯.
    DEBUG(FAIL)는 scanner.log 파일에만 기록하고 UI에는 표시하지 않는다.
    """
    log_entry = pyqtSignal(str)   # "PASS|FAIL", 포맷된 메시지


    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        logging.Handler.__init__(self)
        self.setLevel(logging.DEBUG)  # PASS(INFO) + FAIL(DEBUG) 모두 수신, 표시는 필터링


    # UI에 표시할 PASS 단계 — INVESTOR_REFRESH 는 노이즈이므로 제외
    _PASS_SKIP_STEPS = {"INVESTOR_REFRESH"}


    # UI에 표시할 FAIL 단계 — 초기 필터(VOL_SURGE, TIME 등) 제외, 후기 필터만 표시
    # 이 목록에 없는 FAIL은 파일에만 기록 (수백 개 초기 거절이 패널을 덮는 것 방지)
    _FAIL_SHOW_STEPS = {
        "JDM_RSI", "JDM_EMA", "JDM_PRICE_EMA", "JDM_SLIP",
        "JDM_CANDLE", "JDM_PIVOT",
        "JDM_DAILY_MA20", "JDM_MA20_SLOPE", "JDM_ALIGN",
        "JDM_SURGE", "JDM_LIQUIDITY",
        "BREAKOUT", "PRE_SURGE", "OPENING_SCALP",
    }


    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()          # "PASS/FAIL\tcode\tname\tstep\treason"
            parts = msg.split("\t")
            if len(parts) < 5:
                if record.levelno >= logging.INFO:
                    self.log_entry.emit(f"INFO\t{msg}\t\t")
                return


            level = parts[0]   # "PASS" | "FAIL"
            step  = parts[3]


            # PASS 중 노이즈 단계 제외
            if level == "PASS" and step in self._PASS_SKIP_STEPS:
                return
            # FAIL 중 초기 필터 단계 제외 (후기 단계만 표시)
            if level == "FAIL" and step not in self._FAIL_SHOW_STEPS:
                return


            code   = parts[1]
            name   = parts[2]
            reason = "\t".join(parts[4:])
            formatted = f"{level}\t{name}({code})\t[{step}]\t{reason}"
            self.log_entry.emit(formatted)
        except Exception:
            self.handleError(record)




class SysLogQtHandler(QObject, logging.Handler):
    """
    Python root logger → Qt 시그널 브릿지.


    이 앱의 주요 모듈(INFO+)만 표시하고, 서드파티/Qt 내부 로그는 제외.


    스레드 안전성:
      - logging.Handler 내부 lock 이 emit() 호출을 직렬화.
      - pyqtSignal.emit() 을 비-메인 스레드에서 호출하면 Qt AutoConnection 이
        자동으로 QueuedConnection 으로 처리 → 슬롯은 메인 스레드에서만 실행.


    Rate limit:
      - 동일 메시지 _DEDUP_WINDOW_SEC 이내 반복 스킵.
      - 초당 최대 _MAX_PER_SEC 건 제한 (Qt 이벤트 큐 과부하 방지).
    """
    log_entry = pyqtSignal(str)   # "LEVEL\tlogger_name\tmessage"


    # 이 앱의 모듈 접두사 — INFO 이상 표시
    _APP_PREFIXES = (
        "kiwoom_api", "kiwoom",
        "order", "scanner", "strategy",
        "analysis", "telegram_bot",
        "ui.main_window", "__main__",
    )
    # scanner.audit 는 propagate=False 라 root 에 안 옴 (방어용)
    _EXCLUDE_PREFIXES = ("scanner.audit",)


    _DEDUP_WINDOW_SEC = 2.0   # 동일 메시지 중복 억제 (초)
    _MAX_PER_SEC      = 30    # 초당 최대 emit 건수


    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        logging.Handler.__init__(self)
        self.setLevel(logging.DEBUG)   # 핸들러 자체는 전부 받고, 아래에서 직접 필터링
        self._last_key:   str   = ""
        self._last_time:  float = 0.0
        self._sec_bucket: int   = 0
        self._sec_count:  int   = 0


    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name or ""


            # scanner.audit 제외 (이미 ScannerLogHandler 가 처리)
            for pfx in self._EXCLUDE_PREFIXES:
                if name.startswith(pfx):
                    return


            # 레벨 기준:
            #   앱 모듈(INFO+) vs 그 외(WARNING+)
            is_app = any(name == p or name.startswith(p + ".") for p in self._APP_PREFIXES)
            min_level = logging.INFO if is_app else logging.WARNING
            if record.levelno < min_level:
                return


            now = time.monotonic()


            # 초당 최대 건수 제한
            sec_bucket = int(now)
            if sec_bucket != self._sec_bucket:
                self._sec_bucket = sec_bucket
                self._sec_count  = 0
            self._sec_count += 1
            if self._sec_count > self._MAX_PER_SEC:
                return


            # 동일 메시지 중복 억제
            msg = record.getMessage()
            key = f"{record.levelno}|{name}|{msg[:80]}"
            if key == self._last_key and (now - self._last_time) < self._DEDUP_WINDOW_SEC:
                return
            self._last_key  = key
            self._last_time = now


            if len(msg) > 200:
                msg = msg[:197] + "…"


            self.log_entry.emit(f"{record.levelname}\t{name}\t{msg}")
        except Exception:
            self.handleError(record)




class LogPanel(QWidget):
    """
    하단 로그 패널 — 좌우 분할 구조:
      ┌──────────────────────┬──────────────────────┐
      │  📡 스캐너 진단       │  🖥 시스템 로그        │
      │  거래/체결/신호 이벤트 │  Python logging 출력  │
      └──────────────────────┴──────────────────────┘
    """


    _MAX_BLOCKS = 500   # Qt 문서 블록(줄) 상한 — setMaximumBlockCount 로 O(1) 자동 정리


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(180)


        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)


        # 타이틀 바 — 두 패널 레이블을 수평으로 배치
        title_bar = QWidget()
        title_lay = QHBoxLayout(title_bar)
        title_lay.setContentsMargins(0, 0, 0, 0)
        title_lay.setSpacing(0)


        lbl_left = QLabel("  📡 스캐너 진단")
        lbl_left.setObjectName("panel_title")
        lbl_right = QLabel("  🖥 시스템 로그")
        lbl_right.setObjectName("panel_title")
        title_lay.addWidget(lbl_left)
        title_lay.addWidget(lbl_right)
        outer.addWidget(title_bar)


        # 좌우 분할 QSplitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)


        # 왼쪽: 스캐너 진단 / 거래 이벤트
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Consolas", 9))
        self._log.setObjectName("log_area")
        # Qt 내장 블록 수 제한 — 초과 시 가장 오래된 블록을 O(1)로 자동 제거
        # (수동 cursor 루프 불필요 — 메인 스레드 블로킹 없음)
        self._log.document().setMaximumBlockCount(self._MAX_BLOCKS)
        splitter.addWidget(self._log)


        # 오른쪽: 시스템(Python logging) 로그
        self._sys_log = QTextEdit()
        self._sys_log.setReadOnly(True)
        self._sys_log.setFont(QFont("Consolas", 9))
        self._sys_log.setObjectName("log_area")
        self._sys_log.document().setMaximumBlockCount(self._MAX_BLOCKS)
        splitter.addWidget(self._sys_log)


        splitter.setSizes([1, 1])   # 50:50 초기 비율
        outer.addWidget(splitter)


    def verticalScrollBar(self):
        """메인 로그 영역의 수직 스크롤바 반환 (MainWindow 버퍼링 호환성용)"""
        return self._log.verticalScrollBar()


    # ── 왼쪽 패널: 스캐너 진단 ───────────────────────────────────────────────


    @pyqtSlot(str)
    def append_scanner(self, formatted: str) -> None:
        """ScannerLogHandler 에서 오는 PASS/FAIL 진단 메시지를 표시한다."""
        parts = formatted.split("\t")
        if len(parts) < 4:
            return
        level, who, step, reason = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        ts = datetime.now().strftime("%H:%M:%S")


        if level == "PASS":
            color  = "#a6e3a1"
            prefix = "✅"
        elif level == "FAIL":
            color  = "#585b70"
            prefix = "✗"
        else:
            color  = "#cdd6f4"
            prefix = "ℹ"


        self._log.append(f'<span style="color:{color};">[{ts}] {prefix} {who} {step} {reason}</span>')
        self._log.moveCursor(QTextCursor.MoveOperation.End)


    @pyqtSlot(str)
    def append(self, text: str) -> None:
        """주문·체결·시스템 이벤트 (기존 경로)."""
        import re
        has_ts = re.match(r"^\[\d{2}:\d{2}:\d{2}\]", text)
        ts_str = "" if has_ts else f"[{datetime.now().strftime('%H:%M:%S')}] "

        if "체결" in text or "완료" in text:
            color = "#89dceb"
        elif "오류" in text or "실패" in text or "경고" in text:
            color = "#f38ba8"
        elif "🚨" in text or "신호" in text:
            color = "#fab387"
        else:
            color = "#6c7086"

        self._log.append(f'<span style="color:{color};">{ts_str}{text}</span>')



    # ── 오른쪽 패널: 시스템 로그 ─────────────────────────────────────────────


    @pyqtSlot(str)
    def append_syslog(self, formatted: str) -> None:
        """SysLogQtHandler 에서 오는 Python logging 메시지를 표시한다."""
        parts = formatted.split("\t", 2)
        if len(parts) < 3:
            return
        level_name, logger_name, msg = parts


        if level_name == "CRITICAL":
            color = "#ff4444"
        elif level_name == "ERROR":
            color = "#f38ba8"
        elif level_name == "WARNING":
            color = "#f9e2af"
        elif level_name == "INFO":
            color = "#89b4fa"
        else:
            color = "#6c7086"


        ts         = datetime.now().strftime("%H:%M:%S")
        short_name = logger_name.split(".")[-1] if logger_name else "root"


        self._sys_log.append(f'<span style="color:{color};">[{ts}] [{short_name}] {msg}</span>')
        self._sys_log.moveCursor(QTextCursor.MoveOperation.End)




