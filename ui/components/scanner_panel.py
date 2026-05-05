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


class ScannerPanel(QWidget):
    """좌측 — 스캐너 포착 종목 리스트"""


    row_clicked          = pyqtSignal(str)           # 선택 종목코드
    manual_buy_requested = pyqtSignal(str, str, int)  # code, name, price


    # 스캐너: 전일 대비 당일 등락률(%) — 보유현황의 '수익률'(평단 대비)과 구분
    _HEADERS = ["종목코드", "종목명", "현재가", "당일등락률", "거래대금", "신호", "추세", "매수"]


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._flash_map: dict[str, float] = {}  # code -> expiry_time
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._update_flashes)
        self._flash_timer.start(500)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        title = QLabel("  🔍 스캐너 감시 종목")
        title.setObjectName("panel_title")
        lay.addWidget(title)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        col_widths = [65, 120, 78, 60, 95, 65, 80, 42]
        for i, w in enumerate(col_widths):
            hdr.resizeSection(i, w)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.cellClicked.connect(self._on_click)
        lay.addWidget(self._table)

    def add_signal(self, sig: Any) -> None:
        """새로운 신호 발생 시 하이라이트 등록"""
        code = getattr(sig, "code", str(sig))
        self._flash_map[code] = time.time() + 3.0 # 3초간 유지
        # 테이블 즉시 갱신은 refresh() 호출 시 자연스럽게 이루어짐

    def _update_flashes(self) -> None:
        """만료된 하이라이트 제거 및 필요시 갱신"""
        now = time.time()
        expired = [c for c, t in self._flash_map.items() if t < now]
        if expired:
            for c in expired: self._flash_map.pop(c, None)
            # 하이라이트 해제를 위해 테이블 다시 그리기 트리거는 refresh()에 의존하거나 
            # 상태가 중요하면 여기서 viewport update 호출 가능

    @pyqtSlot(list)
    def refresh(self, rows: list[dict]) -> None:
        if self._table.rowCount() != len(rows):
            self._table.setRowCount(len(rows))

        now = time.time()
        for r, row in enumerate(rows):
            code = row["code"]
            change = row["change_pct"]
            color = QColor("#f38ba8") if change < 0 else QColor("#a6e3a1")
            
            # 하이라이트 체크 (최근 신호 종목)
            is_flashing = code in self._flash_map and self._flash_map[code] > now
            
            if is_flashing:
                bg_color = QColor("#4b4b00") # 진한 노란색 강조 (Dark Theme에 적합)
            elif bool(row.get("signal")):
                bg_color = QColor("#2a1a2e") # 일반 신호 배경
            else:
                bg_color = None

            trade_amt = int(row.get("trade_amount") or 0)
            trend_text = row.get("trend_text", "데이터부족")
            tlevel = int(row.get("trend_level", 0))
            chejan = float(row.get("chejan", 0.0))
            
            if trend_text == "강세": trend_color = QColor("#a6e3a1")
            elif trend_text == "상승": trend_color = QColor("#89dceb")
            elif trend_text == "약세": trend_color = QColor("#cdd6f4")
            elif trend_text == "하락": trend_color = QColor("#f38ba8")
            elif trend_text == "횡보": trend_color = QColor("#a6adc8")
            else: trend_color = QColor("#585b70")
            
            trend_tip = f"추세Lv {tlevel} | 체결강도 {chejan:.0f}%"

            texts = [
                code,
                row["name"],
                f"{row['price']:,}",
                f"{change:+.2f}%",
                format_trade_amount_korean(trade_amt),
                row.get("signal", ""),
                trend_text,
            ]
            for c, text in enumerate(texts):
                item = self._table.item(r, c)
                if not item:
                    item = QTableWidgetItem(text)
                    self._table.setItem(r, c, item)
                else:
                    item.setText(text)
                
                item.setTextAlignment(Qt.AlignVCenter | (Qt.AlignRight if c >= 2 else Qt.AlignLeft))
                if c in (2, 3): item.setForeground(color)
                if c == 6:
                    item.setForeground(trend_color)
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignCenter)
                    item.setToolTip(trend_tip)
                
                # 배경색 적용
                if bg_color:
                    item.setBackground(bg_color)
                else:
                    item.setBackground(QColor(0,0,0,0)) # 투명 (교차 행 색상 유지)

            # 매수 버튼 관리
            _name = row["name"]
            _price = row["price"]
            existing_btn = self._table.cellWidget(r, 7)
            if existing_btn is None or existing_btn.property("code") != code:
                btn = QPushButton("매수")
                btn.setProperty("code", code)
                btn.setFixedHeight(22)
                btn.setStyleSheet(
                    "QPushButton{background:#fab387;color:#11111b;border-radius:3px;font-size:11px;font-weight:bold;}"
                    "QPushButton:hover{background:#f9e2af;color:#1e1e2e;}"
                    "QPushButton:pressed{background:#eba0ac;}"
                )
                btn.clicked.connect(lambda _chk, c=code, n=_name, p=_price: self.manual_buy_requested.emit(c, n, p))
                self._table.setCellWidget(r, 7, btn)


    def _on_click(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item:
            self.row_clicked.emit(item.text())




