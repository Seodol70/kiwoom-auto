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
from scanner.smart_scanner import format_trade_amount_korean
from ui.components.common import _NoWheelDoubleSpinBox, _NoWheelSpinBox




class PortfolioPanel(QWidget):
    """우측 — 보유 종목 현황 (보유중 + 감시중 통합)"""


    tp_changed  = pyqtSignal(float)        # 익절 기준(%) 변경 시
    sl_changed  = pyqtSignal(float)        # 손절 기준(%) 변경 시
    row_clicked = pyqtSignal(str)          # 종목코드 클릭
    manual_sell = pyqtSignal(str, str, int)  # 수동 매도: (code, name, qty)


    # 현재가 다음은 % → 원 순(HTS·스캐너 '당일등락률'과 동일하게 %가 앞)
    _HEADERS = ["종목코드", "종목명", "수량", "평균단가", "현재가", "매수가대비(%)", "손익", "상태", "수동매도"]
    _COL_STATUS = 7   # "상태" 컬럼 인덱스
    _COL_SELL  = 8    # "수동매도" 컬럼 인덱스


    def __init__(self, tp_init: float = 3.0, sl_init: float = -1.0, parent=None) -> None:
        super().__init__(parent)
        self._prev_row_count: int = -1   # 첫 행 잘림 방지용
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)


        title = QLabel("  💼 보유 현황")
        title.setObjectName("panel_title")
        lay.addWidget(title)


        # ── 예수금 + 익절/손절 설정 한 줄 ──────────────────────────────
        info_row = QWidget()
        info_lay = QHBoxLayout(info_row)
        info_lay.setContentsMargins(8, 2, 8, 2)
        info_lay.setSpacing(6)


        self._lbl_cash = QLabel("예수금: —")
        self._lbl_cash.setObjectName("cash_label")
        info_lay.addWidget(self._lbl_cash)
        
        self._lbl_total_pnl = QLabel("합산: — (—%)")
        self._lbl_total_pnl.setStyleSheet("color: #94e2d5; font-weight: bold; margin-left: 10px;")
        info_lay.addWidget(self._lbl_total_pnl)
        
        info_lay.addStretch()


        lbl_tp = QLabel("익절")
        lbl_tp.setObjectName("lbl_tp")
        info_lay.addWidget(lbl_tp)
        self._spin_tp = _NoWheelDoubleSpinBox()
        self._spin_tp.setObjectName("spin_tp")
        self._spin_tp.setRange(0.1, 30.0)
        self._spin_tp.setSingleStep(0.5)
        self._spin_tp.setDecimals(1)
        self._spin_tp.setSuffix(" %")
        self._spin_tp.setValue(tp_init)
        self._spin_tp.setFixedWidth(74)
        self._spin_tp.setToolTip(
            "익절 기준(%) — 매수 평단 대비 순수 등락률, 이 값 이상이면 자동 매도 (수수료·세금 제외)"
        )
        self._spin_tp.valueChanged.connect(self.tp_changed.emit)
        info_lay.addWidget(self._spin_tp)


        lbl_sl = QLabel("손절")
        lbl_sl.setObjectName("lbl_sl")
        info_lay.addWidget(lbl_sl)
        self._spin_sl = _NoWheelDoubleSpinBox()
        self._spin_sl.setObjectName("spin_sl")
        self._spin_sl.setRange(-30.0, -0.1)
        self._spin_sl.setSingleStep(0.5)
        self._spin_sl.setDecimals(1)
        self._spin_sl.setSuffix(" %")
        self._spin_sl.setValue(sl_init)
        self._spin_sl.setFixedWidth(74)
        self._spin_sl.setToolTip(
            "손절 기준(%) — 매수 평단 대비 순수 등락률, 이 값 이하이면 자동 매도 (수수료·세금 제외)"
        )
        self._spin_sl.valueChanged.connect(self.sl_changed.emit)
        info_lay.addWidget(self._spin_sl)


        lay.addWidget(info_row)


        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        # 한글 폰트 설정 (깨짐 방지)
        table_font = QFont("Malgun Gothic", 9)
        self._table.setFont(table_font)
        hdr = self._table.horizontalHeader()
        hdr.setFont(QFont("Malgun Gothic", 9))
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        # 컬럼별 최적 너비 (코드/명/수량/평균단가/현재가/수익률/손익/상태/수동매도)
        col_widths = [68, 110, 50, 78, 78, 62, 82, 70, 105]
        for i, w in enumerate(col_widths):
            hdr.resizeSection(i, w)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)                   # 종목명
        hdr.setSectionResizeMode(self._COL_STATUS, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(self._COL_SELL,   QHeaderView.Fixed)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.cellClicked.connect(self._on_click)
        lay.addWidget(self._table)


    def _make_item(self, text: str, align_right: bool = False,
                   fg: Optional[QColor] = None) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(
            Qt.AlignVCenter | (Qt.AlignRight if align_right else Qt.AlignLeft)
        )
        if fg:
            item.setForeground(fg)
        return item


    @pyqtSlot(dict)
    def refresh(self, data: dict) -> None:
        cash          = data.get("cash", 0)
        positions     = data.get("positions", {})       # dict[code, Position]
        max_positions = data.get("max_positions", len(positions))

        # 보유 여유분만큼만 감시중(미보유) 표시 — 최근 신호 우선, 보유 종목은 항상 포함
        all_watch   = data.get("watch_today", {})
        slack       = max(0, max_positions - len(positions))
        non_pos     = {c: v for c, v in all_watch.items() if c not in positions}
        watch_today = {c: v for c, v in all_watch.items() if c in positions}
        watch_today.update(dict(list(non_pos.items())[-slack:]) if slack else {})


        self._lbl_cash.setText(f"  예수금: {cash:,} 원")

        # ── 합산 손익 계산 ──
        total_buy = sum(p.avg_price * p.qty for p in positions.values())
        total_pnl = sum(p.pnl for p in positions.values())
        total_pct = (total_pnl / total_buy * 100) if total_buy > 0 else 0.0
        
        color_hex = "#f38ba8" if total_pnl < 0 else "#a6e3a1" if total_pnl > 0 else "#cdd6f4"
        self._lbl_total_pnl.setText(f"합산: {total_pnl:+,}원 ({total_pct:+.2f}%)")
        self._lbl_total_pnl.setStyleSheet(f"color: {color_hex}; font-weight: bold; margin-left: 10px;")


        # ── refresh 전 수동매도 스핀박스 값 보존 (사용자 입력 유지) ──────
        _saved_qty: dict[str, int] = {}
        for row in range(self._table.rowCount()):
            code_item = self._table.item(row, 0)
            w = self._table.cellWidget(row, self._COL_SELL)
            if code_item and w:
                spin = w.findChild(QSpinBox)
                if spin:
                    _saved_qty[code_item.text()] = spin.value()


        # 보유중 + 감시중 전용(미보유) 행 구성
        watch_only = {c: v for c, v in watch_today.items() if c not in positions}
        total_rows = len(positions) + len(watch_only)
        row_count_changed = (total_rows != self._prev_row_count)
        self._prev_row_count = total_rows
        self._table.setRowCount(total_rows)


        r = 0
        # ── 보유 포지션 ───────────────────────────────────────────────
        for pos in positions.values():
            ret_pct = float(pos.price_change_pct_vs_avg)     # 매수 평단 대비 순수 등락률(%)
            pnl = int(pos.pnl)                               # 평가손익(원) = (현재가 - 평균단가) × 수량
            color = QColor("#f38ba8") if pnl < 0 else QColor("#a6e3a1")  # 손익 기준 색상
            status = "감시중" if pos.code in watch_today else "보유중"
            s_color = QColor("#fab387") if status == "감시중" else QColor("#89b4fa")
            values = [
                (pos.code,                  False, None),
                (pos.name,                  False, None),
                (str(pos.qty),              True,  None),
                (f"{pos.avg_price:,}",      True,  None),
                (f"{pos.current_price:,}",  True,  None),
                (f"{ret_pct:+.2f}%",        True,  color),
                (f"{pnl:+,}",               True,  color),
                (status,                    False, s_color),
            ]
            for c, (text, right, fg) in enumerate(values):
                self._table.setItem(r, c, self._make_item(text, right, fg))


            # ── 수동매도 위젯: [스핀박스] [매도] ────────────────────
            cell_w = QWidget()
            cell_lay = QHBoxLayout(cell_w)
            cell_lay.setContentsMargins(2, 1, 2, 1)
            cell_lay.setSpacing(2)


            spin = _NoWheelSpinBox()
            spin.setRange(1, max(1, pos.qty))
            # 저장된 수량 복원, 없으면 전량
            saved = _saved_qty.get(pos.code, pos.qty)
            spin.setValue(min(saved, pos.qty))
            spin.setFixedWidth(54)
            spin.setToolTip("매도 수량")


            btn = QPushButton("매도")
            btn.setObjectName("manual_sell_btn")
            btn.setFixedWidth(40)
            btn.setToolTip(f"{pos.name} 수동 매도")
            # 클릭 시 시그널 발생 (클로저로 code·name·spin 캡처)
            btn.clicked.connect(
                lambda _checked, c=pos.code, n=pos.name, s=spin:
                    self.manual_sell.emit(c, n, s.value())
            )


            cell_lay.addWidget(spin)
            cell_lay.addWidget(btn)
            self._table.setCellWidget(r, self._COL_SELL, cell_w)
            r += 1


        # ── 감시중 전용 (미보유) ──────────────────────────────────────
        w_color = QColor("#fab387")   # 주황색
        for code, info in watch_only.items():
            price_str = f"{info.get('price', 0):,}" if info.get('price') else "-"
            values = [
                (code,                   False, None),
                (info.get("name", code), False, None),
                ("-",                    True,  None),
                ("-",                    True,  None),
                (price_str,              True,  None),
                ("-",                    True,  None),
                ("-",                    True,  None),
                ("감시중",               False, w_color),
            ]
            for c, (text, right, fg) in enumerate(values):
                self._table.setItem(r, c, self._make_item(text, right, fg))
            # 감시중(미보유) 행에는 수동매도 위젯 없음
            self._table.setCellWidget(r, self._COL_SELL, None)
            r += 1


        # 행 수가 바뀐 경우(종목 추가/제거)에만 맨 위로 이동 — 첫 행 잘림 방지
        if row_count_changed and total_rows > 0:
            self._table.scrollToTop()


    def _on_click(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item:
            self.row_clicked.emit(item.text())




