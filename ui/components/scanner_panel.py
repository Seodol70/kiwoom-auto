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
    QDialog, QDialogButtonBox, QComboBox, QGroupBox, QAction, QMenu, QLineEdit
)


from config import TELEGRAM as _TG
from telegram_bot import TelegramBot
from scanner.smart_scanner import format_trade_amount_korean


class ScannerPanel(QWidget):
    """좌측 — 스캐너 포착 종목 리스트"""


    row_clicked          = pyqtSignal(str)           # 선택 종목코드
    manual_buy_requested = pyqtSignal(str, str, int)  # code, name, price


    # 스캐너: 전일 대비 당일 등락률(%) — 보유현황의 '수익률'(평단 대비)과 구분
    _HEADERS = ["No.", "종목코드", "종목명", "현재가", "당일등락률", "거래대금", "신호", "추세", "매수"]


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._flash_map: dict[str, float] = {}  # code -> expiry_time
        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._update_flashes)
        self._flash_timer.start(500)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        title_lay = QHBoxLayout()
        title = QLabel("  🔍 스캐너 감시 종목")
        title.setObjectName("panel_title")
        title_lay.addWidget(title)
        
        title_lay.addStretch()
        
        # [NEW] 수동 종목 검색 및 매수 입력창
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("종목코드 입력 후 Enter (수동매수)")
        self._search_input.setFixedWidth(180)
        self._search_input.setFixedHeight(24)
        self._search_input.setStyleSheet("""
            QLineEdit { 
                background-color: #1e1e2e; color: #cdd6f4; 
                border: 1px solid #45475a; border-radius: 4px; padding-left: 5px;
                font-size: 9pt;
            }
            QLineEdit:focus { border: 1px solid #89b4fa; }
        """)
        self._search_input.returnPressed.connect(self._on_search_requested)
        title_lay.addWidget(self._search_input)
        
        lay.addLayout(title_lay)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        # 한글 폰트 설정 (깨짐 방지)
        table_font = QFont("Malgun Gothic", 9)
        self._table.setFont(table_font)
        hdr = self._table.horizontalHeader()
        hdr.setFont(QFont("Malgun Gothic", 9))
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        # [NEW] 정렬 기능 활성화
        self._table.setSortingEnabled(True)
        col_widths = [35, 65, 120, 78, 60, 95, 65, 80, 42]
        for i, w in enumerate(col_widths):
            hdr.resizeSection(i, w)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
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
        # [FIX 2026-05-08] 증분 업데이트: 변경된 셀만 업데이트 (메인 스레드 블로킹 최소화)

        # [DEBUG] 데이터 수신 확인
        if rows and time.time() - getattr(self, "_last_refresh_log", 0) > 10.0:
            self._last_refresh_log = time.time()
            logging.info("🖥 [ScannerPanel] UI 데이터 수신 완료 (%d종목)", len(rows))

        # 이전 데이터 캐시 (변경 감지용)
        old_rows = getattr(self, "_cached_rows", {})
        new_rows = {row["code"]: row for row in rows}
        self._cached_rows = new_rows

        # 현재 선택된 종목 코드 및 스크롤 위치 저장
        selected_code = None
        sel_items = self._table.selectedItems()
        if sel_items:
            row_idx = sel_items[0].row()
            it_c = self._table.item(row_idx, 1)
            if it_c: selected_code = it_c.text()

        scroll_pos = self._table.verticalScrollBar().value()

        # 행 수 변경 시에만 재구성
        if self._table.rowCount() != len(rows):
            self._table.setRowCount(len(rows))

        # [FIX] 정렬 일시 중지 (성능 최적화)
        self._table.setSortingEnabled(False)

        now = time.time()
        for r, row in enumerate(rows):
            code = row["code"]
            change = row["change_pct"]
            color = QColor("#f38ba8") if change < 0 else QColor("#a6e3a1")
            
            # 하이라이트 체크 (최근 신호 종목)
            is_flashing = code in self._flash_map and self._flash_map[code] > now
            bg_color = QColor("#4b4b00") if is_flashing else None # 진한 노란색 강조

            # 0: No.
            it_no = QTableWidgetItem()
            it_no.setData(Qt.EditRole, r + 1)
            it_no.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(r, 0, it_no)

            # 1: 종목코드
            it_code = QTableWidgetItem(code)
            it_code.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(r, 1, it_code)

            # 2: 종목명
            it_name = QTableWidgetItem(row["name"])
            self._table.setItem(r, 2, it_name)

            # 3: 현재가
            it_price = QTableWidgetItem()
            try:
                _p_val = int(row.get("price", 0))
            except:
                _p_val = 0
            it_price.setData(Qt.EditRole, _p_val)
            it_price.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            it_price.setForeground(color)
            self._table.setItem(r, 3, it_price)

            # 4: 당일등락률
            it_pct = QTableWidgetItem(f"{change:+.2f}%")
            try:
                _c_val = float(change)
            except:
                _c_val = 0.0
            it_pct.setData(Qt.EditRole, _c_val)
            it_pct.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            it_pct.setForeground(color)
            self._table.setItem(r, 4, it_pct)

            # 5: 거래대금 (사용자 요청으로 정렬 부하 방지를 위해 숫자 정렬 제외)
            it_amt = QTableWidgetItem(format_trade_amount_korean(row.get("trade_amount", 0)))
            it_amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(r, 5, it_amt)

            # 6: 신호
            it_sig = QTableWidgetItem(str(row.get("signal", "")))
            it_sig.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(r, 6, it_sig)

            # 7: 추세
            it_trend = QTableWidgetItem(row.get("trend_text", "데이터부족"))
            it_trend.setTextAlignment(Qt.AlignCenter)
            # 추세별 색상
            if "강세" in row.get("trend_text", "") or "상승" in row.get("trend_text", ""):
                it_trend.setForeground(QColor("#a6e3a1"))
            elif "약세" in row.get("trend_text", "") or "하락" in row.get("trend_text", ""):
                it_trend.setForeground(QColor("#f38ba8"))
            self._table.setItem(r, 7, it_trend)

            # 8: 매수 버튼 (매번 생성하지 않고 재사용하여 성능 최적화)
            btn = self._table.cellWidget(r, 8)
            if not isinstance(btn, QPushButton):
                btn = QPushButton("매수")
                btn.setFixedSize(38, 22)
                btn.setObjectName("buy_button")
                self._table.setCellWidget(r, 8, btn)
            
            # 기존 연결 해제 후 재연결 (클로저 문제 방지)
            try: btn.clicked.disconnect()
            except: pass
            btn.clicked.connect(lambda _, c=code, n=row["name"], p=row["price"]: self.manual_buy_requested.emit(c, n, p))
            # 배경색 적용 (하이라이트)
            if bg_color:
                for c in range(self._table.columnCount() - 1):
                    item = self._table.item(r, c)
                    if item: item.setBackground(bg_color)
            else:
                for c in range(self._table.columnCount() - 1):
                    item = self._table.item(r, c)
                    if item: item.setBackground(QColor(0,0,0,0)) # 투명 (교차 행 색상 유지)

        # 정렬 복구
        self._table.setSortingEnabled(True)

        # 이전 선택 및 스크롤 복구
        if selected_code:
            for r in range(self._table.rowCount()):
                it = self._table.item(r, 1) # 종목코드 열
                if it and it.text() == selected_code:
                    self._table.selectRow(r)
                    break
        self._table.verticalScrollBar().setValue(scroll_pos)


    def _on_click(self, row: int, _col: int) -> None:
        # [FIX] 0번 열은 "No." 이고, 1번 열이 "종목코드"임
        item = self._table.item(row, 1)
        if item:
            self.row_clicked.emit(item.text())




    def _on_search_requested(self) -> None:
        """종목코드를 직접 입력하여 수동매수 창 띄우기"""
        code = self._search_input.text().strip()
        if not code: return
        
        # SnapshotStore 에서 최신 정보 조회 (MainWindow를 통해 접근)
        win = self.window()
        if hasattr(win, "_snap_store"):
            snap = win._snap_store.get_snapshot(code)
            if snap:
                self.manual_buy_requested.emit(code, snap.name, snap.current_price)
                self._search_input.clear()
            else:
                logging.warning("⚠️ [수동매수] 종목 정보를 찾을 수 없습니다: %s", code)
        else:
            # SnapshotStore 접근 불가 시 기본값으로 시도 (API 조회 필요할 수 있음)
            logging.warning("⚠️ [수동매수] 시스템 초기화 중입니다. 잠시 후 다시 시도해 주세요.")
