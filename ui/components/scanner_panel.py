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
    _HEADERS = ["종목코드", "종목명", "현재가", "당일등락률", "거래대금", "신호", "추세", "수급", "매수"]


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)


        title = QLabel("  🔍 스캐너 감시 종목")
        title.setObjectName("panel_title")
        lay.addWidget(title)


        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)  # 모든 컬럼 마우스 드래그 가능
        hdr.setStretchLastSection(False)
        # 컬럼별 초기 너비 — 마우스로 자유롭게 조절 가능
        col_widths = [65, 100, 78, 60, 90, 65, 72, 72, 42]  # 코드/명/가/등락/거래대금/신호/추세/수급/매수
        for i, w in enumerate(col_widths):
            hdr.resizeSection(i, w)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)  # 종목명만 자동 늘어남 (나머지는 Interactive 유지)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.cellClicked.connect(self._on_click)
        lay.addWidget(self._table)


    @pyqtSlot(list)
    def refresh(self, rows: list[dict]) -> None:
        # 행 수가 다를 때만 setRowCount (비싼 작업)
        if self._table.rowCount() != len(rows):
            self._table.setRowCount(len(rows))


        for r, row in enumerate(rows):
            change = row["change_pct"]
            color  = QColor("#f38ba8") if change < 0 else QColor("#a6e3a1")
            has_sig = bool(row.get("signal"))
            bg_color = QColor("#2a1a2e") if has_sig else None


            # [진단] 거래대금 단위 확인
            trade_amt = int(row.get("trade_amount") or 0)
            if r < 3:  # 상위 3개만 진단 로그
                import logging as _log
                _log.getLogger(__name__).debug(
                    "[ScannerPanel] %s 거래대금: raw=%d 포맷=%s",
                    row["code"], trade_amt, format_trade_amount_korean(trade_amt)
                )


            iscore   = row.get("investor_score", 0)
            f_net    = row.get("foreign_net", 0)
            i_net    = row.get("inst_net", 0)
            # 외국인/기관 각각 방향 표시
            if f_net or i_net:
                f_arrow = "▲" if f_net >= 0 else "▼"
                i_arrow = "▲" if i_net >= 0 else "▼"
                inv_text = f"외{f_arrow}기{i_arrow}"
                # 색상: 둘 다 순매수=초록, 둘 다 순매도=빨강, 혼합=노랑
                if f_net >= 0 and i_net >= 0:
                    inv_color = QColor("#a6e3a1")   # 초록
                elif f_net < 0 and i_net < 0:
                    inv_color = QColor("#f38ba8")   # 빨강
                else:
                    inv_color = QColor("#f9e2af")   # 노랑 (혼합)
            else:
                inv_text  = "-"
                inv_color = QColor("#6c7086")   # 회색
            inv_tip = (
                f"외국인: {f_net:+,}주\n기관: {i_net:+,}주\n수급점수: {iscore:+d}"
                if (f_net or i_net) else "수급 미조회"
            )


            # 추세 표시
            trend_text  = row.get("trend_text", "데이터부족")
            tlevel      = int(row.get("trend_level", 0))
            chejan      = float(row.get("chejan", 0.0))
            if trend_text == "강세":
                trend_color = QColor("#a6e3a1")   # 초록
            elif trend_text == "상승":
                trend_color = QColor("#89dceb")   # 시안
            elif trend_text == "약세":
                trend_color = QColor("#cdd6f4")   # 연청
            elif trend_text == "하락":
                trend_color = QColor("#f38ba8")   # 빨강
            elif trend_text == "횡보":
                trend_color = QColor("#a6adc8")   # 연회색
            else:
                trend_color = QColor("#585b70")   # 어두운 회색 (데이터부족)
            trend_tip = f"추세Lv {tlevel} | 체결강도 {chejan:.0f}%"


            texts = [
                row["code"],
                row["name"],
                f"{row['price']:,}",
                f"{change:+.2f}%",
                format_trade_amount_korean(trade_amt),
                row.get("signal", ""),
                trend_text,
                inv_text,
            ]
            for c, text in enumerate(texts):
                existing = self._table.item(r, c)
                # 텍스트가 바뀐 경우만 새 아이템 생성 (변경 없으면 스킵)
                if existing and existing.text() == text:
                    continue
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignVCenter |
                    (Qt.AlignRight if c >= 2 else Qt.AlignLeft)
                )
                if c in (2, 3):
                    item.setForeground(color)
                if c == 6:   # 추세
                    item.setForeground(trend_color)
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignCenter)
                    item.setToolTip(trend_tip)
                if c == 7:   # 수급
                    item.setForeground(inv_color)
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignCenter)
                    item.setToolTip(inv_tip)
                if bg_color:
                    item.setBackground(bg_color)
                self._table.setItem(r, c, item)


            # ── 매수 버튼 (마지막 컬럼) — 종목마다 1개 ──────────────
            _code  = row["code"]
            _name  = row["name"]
            _price = row["price"]
            existing_btn = self._table.cellWidget(r, 8)
            # 같은 종목이면 버튼 재사용, 다른 종목이면 새로 생성
            if existing_btn is None or existing_btn.property("code") != _code:
                btn = QPushButton("매수")
                btn.setProperty("code", _code)
                btn.setFixedHeight(22)
                btn.setStyleSheet(
                    "QPushButton{background:#45475a;color:#cdd6f4;border-radius:3px;font-size:11px;}"
                    "QPushButton:hover{background:#89dceb;color:#1e1e2e;}"
                    "QPushButton:pressed{background:#74c7ec;}"
                )
                btn.clicked.connect(
                    lambda _chk, c=_code, n=_name, p=_price:
                        self.manual_buy_requested.emit(c, n, p)
                )
                self._table.setCellWidget(r, 8, btn)


    def _on_click(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item:
            self.row_clicked.emit(item.text())




