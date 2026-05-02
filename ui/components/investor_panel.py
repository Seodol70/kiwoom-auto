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


class InvestorPanel(QWidget):
    """수급 현황 패널 — 로그 패널 위. 외국인/기관 순매수 상위 종목을 요약 표시.


    watch_list_updated 시그널(ScannerWorker)의 rows 를 받아
    investor_score != 0 인 종목을 순위별로 표시한다.
    """


    _HEADERS = ["종목명", "외국인(주)", "기관(주)", "수급"]
    _MAX_ROWS = 8   # 최대 표시 행


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(112)


        root = QVBoxLayout(self)
        root.setContentsMargins(4, 0, 4, 2)
        root.setSpacing(2)


        # ── 제목 + 갱신 시각 ─────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("  📡 수급 현황 (외국인/기관 순매수▲ · 순매도▼)")
        title.setObjectName("panel_title")
        hdr.addWidget(title)
        hdr.addStretch()
        self._lbl_updated = QLabel("갱신: --:--")
        self._lbl_updated.setStyleSheet("color:#6c7086; font-size:9px;")
        hdr.addWidget(self._lbl_updated)
        root.addLayout(hdr)


        # ── 테이블 ───────────────────────────────────────────────────────
        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setFont(QFont("Consolas", 8))


        hdr_h = self._table.horizontalHeader()
        hdr_h.setSectionResizeMode(0, QHeaderView.Stretch)          # 종목명 늘어남
        hdr_h.setSectionResizeMode(1, QHeaderView.ResizeToContents) # 외국인
        hdr_h.setSectionResizeMode(2, QHeaderView.ResizeToContents) # 기관
        hdr_h.resizeSection(3, 36)                                   # 수급점수
        hdr_h.setSectionResizeMode(3, QHeaderView.Fixed)


        self._table.verticalHeader().setDefaultSectionSize(18)
        root.addWidget(self._table)


    @pyqtSlot(list)
    def refresh(self, rows: list) -> None:
        """
        watch_list_updated 로 받은 rows(list[dict]) 에서
        investor_score 기준으로 상위 _MAX_ROWS 종목을 렌더링.
        수급 미조회(score==0 AND foreign==0 AND inst==0) 종목은 제외.
        """
        # 수급 데이터가 있는 종목만 추출 → score 절댓값 내림차순, 같으면 외국인 기준
        inv_rows = [
            r for r in rows
            if r.get("investor_score", 0) != 0
            or r.get("foreign_net", 0) != 0
            or r.get("inst_net", 0) != 0
        ]
        inv_rows.sort(
            key=lambda r: (abs(r.get("investor_score", 0)),
                           abs(r.get("foreign_net", 0))),
            reverse=True,
        )
        inv_rows = inv_rows[:self._MAX_ROWS]


        if self._table.rowCount() != len(inv_rows):
            self._table.setRowCount(len(inv_rows))


        for row_idx, r in enumerate(inv_rows):
            iscore = r.get("investor_score", 0)
            f_net  = r.get("foreign_net",    0)
            i_net  = r.get("inst_net",       0)


            if iscore > 0:
                score_txt   = "▲"
                score_color = QColor("#a6e3a1")
            elif iscore < 0:
                score_txt   = "▼"
                score_color = QColor("#f38ba8")
            else:
                score_txt   = "-"
                score_color = QColor("#6c7086")


            f_color = QColor("#a6e3a1") if f_net > 0 else (
                      QColor("#f38ba8") if f_net < 0 else QColor("#6c7086"))
            i_color = QColor("#a6e3a1") if i_net > 0 else (
                      QColor("#f38ba8") if i_net < 0 else QColor("#6c7086"))


            cells = [
                (r.get("name", r.get("code", "")), QColor("#cdd6f4"), Qt.AlignLeft),
                (f"{f_net:+,}",                    f_color,           Qt.AlignRight),
                (f"{i_net:+,}",                    i_color,           Qt.AlignRight),
                (score_txt,                         score_color,       Qt.AlignCenter),
            ]
            for col_idx, (text, fg, align) in enumerate(cells):
                existing = self._table.item(row_idx, col_idx)
                if existing and existing.text() == text:
                    continue
                item = QTableWidgetItem(text)
                item.setForeground(fg)
                item.setTextAlignment(Qt.AlignVCenter | align)
                self._table.setItem(row_idx, col_idx, item)


        # 갱신 시각 표시
        from datetime import datetime
        self._lbl_updated.setText(f"갱신: {datetime.now().strftime('%H:%M')}")


        # 수급 데이터 없는 경우 안내
        if not inv_rows:
            self._table.setRowCount(1)
            item = QTableWidgetItem("수급 데이터 조회 대기 중 (10분 주기 갱신)")
            item.setForeground(QColor("#6c7086"))
            item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            self._table.setItem(0, 0, item)
            # 나머지 셀 비움
            for c in range(1, len(self._HEADERS)):
                self._table.setItem(0, c, QTableWidgetItem(""))




class ScanStatusBar(QWidget):
    """스캔 진행 상태바 — opt10030 조회 / 분봉 초기화 / 감시종목 확정"""


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(24)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(8)


        self._lbl_phase = QLabel("대기 중")
        self._lbl_phase.setObjectName("scan_phase")
        self._lbl_phase.setFixedWidth(130)


        self._bar = QProgressBar()
        self._bar.setFixedHeight(10)
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)


        self._lbl_detail = QLabel("")
        self._lbl_detail.setObjectName("scan_detail")


        lay.addWidget(QLabel("  스캔:"))
        lay.addWidget(self._lbl_phase)
        lay.addWidget(self._bar, stretch=1)
        lay.addWidget(self._lbl_detail)


    def update(self, phase: str, current: int, total: int, detail: str = "") -> None:
        """TR 조회 중 메인 스레드에서 호출 — 페인트/타이머만 허용, 사용자 입력은 차단."""
        self._lbl_phase.setText(phase)
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(current)
        self._lbl_detail.setText(detail)
        # ExcludeUserInputEvents: 버튼 클릭·타이머 콜백은 차단, 화면 갱신만 허용
        # → scan 도중 _auto_sell_by_pnl() 등이 TR을 중첩 호출하는 것을 방지
        from PyQt5.QtCore import QEventLoop as _QEL
        QApplication.processEvents(_QEL.ExcludeUserInputEvents)


    def done(self, msg: str) -> None:
        self._lbl_phase.setText("완료")
        self._bar.setValue(self._bar.maximum())
        self._lbl_detail.setText(msg)
        from PyQt5.QtCore import QEventLoop as _QEL
        QApplication.processEvents(_QEL.ExcludeUserInputEvents)


    def reset(self) -> None:
        self._lbl_phase.setText("대기 중")
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._lbl_detail.setText("")




