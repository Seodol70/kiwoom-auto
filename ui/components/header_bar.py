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
from ui.components.common import _hline




class HeaderBar(QWidget):
    """상단 상태 바 — Safety Switch 포함"""


    # 자동매매 ON/OFF 상태 변경 시 MainWindow 로 전달
    auto_trade_toggled = pyqtSignal(bool)      # True = 시작, False = 정지
    exit_requested = pyqtSignal()              # 프로그램 종료 요청
    unlock_requested = pyqtSignal()            # 일일 손익 락 수동 해제 요청
    overnight_mode_toggled = pyqtSignal(bool)  # True = 야간보유 ON, False = OFF
    switch_real_requested = pyqtSignal()       # 실전투자 전환 버튼


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(52)
        self.setObjectName("header_bar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(16)


        self._lbl_title = QLabel("📈 키움 자동매매")
        self._lbl_title.setFont(QFont("Malgun Gothic", 12, QFont.Bold))
        self._lbl_title.setObjectName("lbl_title")


        self._lbl_account = self._make("계좌: —")
        self._lbl_mode    = self._make("—")
        self._lbl_conn    = self._make("● 미연결")
        self._lbl_conn.setObjectName("conn_off")
        self._lbl_pnl     = self._make("당일 실현손익: —")
        self._lbl_kospi   = self._make("코스피: —")
        self._lbl_kosdaq  = self._make("코스닥: —")


        # ── Safety Switch ───────────────────────────────────────────────
        self._btn_auto = QPushButton("▶ 자동매매 시작")
        self._btn_auto.setObjectName("btn_auto_off")
        self._btn_auto.setCheckable(True)
        self._btn_auto.setChecked(False)
        self._btn_auto.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_auto.setFixedSize(140, 30)
        self._btn_auto.clicked.connect(self._on_auto_clicked)


        # ── 재시작 버튼 ────────────────────────────────────────────────
        self._btn_restart = QPushButton("🔄 재시작")
        self._btn_restart.setObjectName("btn_restart")
        self._btn_restart.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_restart.setFixedSize(75, 30)
        self._btn_restart.clicked.connect(self._on_restart_clicked)


        # ── 종료 버튼 ─────────────────────────────────────────────────
        self._btn_exit = QPushButton("⏻ 종료")
        self._btn_exit.setObjectName("btn_exit")
        self._btn_exit.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_exit.setFixedSize(75, 30)
        self._btn_exit.clicked.connect(self._on_exit_clicked)


        # ── 일일 손익 락 해제 버튼 ─────────────────────────────────────
        self._btn_unlock = QPushButton("🔓 락 해제")
        self._btn_unlock.setObjectName("btn_unlock")
        self._btn_unlock.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_unlock.setFixedSize(85, 30)
        self._btn_unlock.clicked.connect(self._on_unlock_clicked)


        # ── 야간보유 모드 토글 버튼 ────────────────────────────────────
        self._btn_overnight = QPushButton("🌙 야간보유 OFF")
        self._btn_overnight.setObjectName("btn_overnight_off")
        self._btn_overnight.setCheckable(True)
        self._btn_overnight.setChecked(False)
        self._btn_overnight.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_overnight.setFixedSize(120, 30)
        self._btn_overnight.setToolTip(
            "야간보유 모드: ON 시 14:40~14:55에 EOD 신호 발생, 당일 15:19 강제청산 제외\n"
            "익일 09:00 갭 체크 후 자동 관리 (갭상승 +2% 익절 / 갭하락 -1.5% 손절 / 09:30 타임컷)"
        )
        self._btn_overnight.clicked.connect(self._on_overnight_clicked)


        # ── 실전투자 전환 버튼 ────────────────────────────────────────────────
        self._btn_switch_real = QPushButton("💎 실전투자 전환")
        self._btn_switch_real.setObjectName("btn_switch_real")
        self._btn_switch_real.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_switch_real.setFixedSize(120, 30)
        self._btn_switch_real.setVisible(False)
        self._btn_switch_real.clicked.connect(self._on_switch_real_clicked)


        lay.addWidget(self._lbl_title)
        lay.addStretch()
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_kospi)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_kosdaq)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_account)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_mode)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_conn)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_pnl)
        lay.addWidget(self._divider())
        lay.addWidget(self._btn_switch_real)
        lay.addWidget(self._btn_overnight)
        lay.addWidget(self._btn_auto)
        lay.addWidget(self._btn_unlock)
        lay.addWidget(self._btn_restart)
        lay.addWidget(self._btn_exit)


    def _make(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Malgun Gothic", 9))
        return lbl


    def _divider(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setObjectName("v_divider")
        return f


    def _on_auto_clicked(self, checked: bool) -> None:
        if checked:
            self._btn_auto.setText("⏹ 자동매매 정지")
            self._btn_auto.setObjectName("btn_auto_on")
        else:
            self._btn_auto.setText("▶ 자동매매 시작")
            self._btn_auto.setObjectName("btn_auto_off")
        # QSS objectName 변경 즉시 반영
        self._btn_auto.style().unpolish(self._btn_auto)
        self._btn_auto.style().polish(self._btn_auto)
        self.auto_trade_toggled.emit(checked)


    def _on_switch_real_clicked(self) -> None:
        self.switch_real_requested.emit()


    def _on_overnight_clicked(self, checked: bool) -> None:
        if checked:
            self._btn_overnight.setText("🌙 야간보유 ON")
            self._btn_overnight.setObjectName("btn_overnight_on")
        else:
            self._btn_overnight.setText("🌙 야간보유 OFF")
            self._btn_overnight.setObjectName("btn_overnight_off")
        self._btn_overnight.style().unpolish(self._btn_overnight)
        self._btn_overnight.style().polish(self._btn_overnight)
        self.overnight_mode_toggled.emit(checked)


    def _on_restart_clicked(self) -> None:
        """프로그램 재시작 버튼 클릭 — 소스 변경사항 반영"""
        import subprocess
        import sys
        import os
        # 현재 스크립트의 절대 경로 (소스 변경 시 자동 반영)
        script_path = os.path.abspath(__file__)
        # 새 프로세스 시작
        subprocess.Popen([sys.executable, script_path])
        # 현재 프로세스 종료
        self.exit_requested.emit()


    def _on_exit_clicked(self) -> None:
        """프로그램 종료 버튼 클릭"""
        self.exit_requested.emit()


    def _on_unlock_clicked(self) -> None:
        """일일 손익 락 수동 해제 요청."""
        self.unlock_requested.emit()


    def set_connected(self, account: str, mode: str) -> None:
        self._lbl_account.setText(f"계좌: {account}")
        self._lbl_mode.setText(f"{'🟠 실전' if mode == '실전투자' else '🟢 모의'}")
        self._lbl_conn.setText("● 연결됨")
        self._lbl_conn.setObjectName("conn_on")
        self._lbl_conn.style().unpolish(self._lbl_conn)
        self._lbl_conn.style().polish(self._lbl_conn)
        self._btn_switch_real.setVisible(mode != "실전투자")


    def set_pnl(self, pnl: int) -> None:
        sign = "+" if pnl >= 0 else ""
        self._lbl_pnl.setText(f"당일 실현손익: {sign}{pnl:,}원")
        color = "#f38ba8" if pnl < 0 else "#a6e3a1"
        self._lbl_pnl.setStyleSheet(f"color: {color};")
        self._lbl_pnl.setToolTip(
            "잔고 동기화 시 opt10074 계좌 당일 실현손익에, 그 이후 앱에서 받은 매도 체결 손익을 더한 값입니다."
        )


    def set_index(self, kospi_current: float, kospi_chg: float,
                  kosdaq_current: float, kosdaq_chg: float) -> None:
        """코스피·코스닥 현재가 및 등락률 표시."""
        def _fmt(name: str, cur: float, chg: float) -> str:
            arrow = "▲" if chg >= 0 else "▼"
            return f"{name} {cur:,.2f} {arrow}{abs(chg):.2f}%"


        self._lbl_kospi.setText(_fmt("코스피", kospi_current, kospi_chg))
        self._lbl_kosdaq.setText(_fmt("코스닥", kosdaq_current, kosdaq_chg))


        kospi_color  = "#f38ba8" if kospi_chg  < 0 else "#a6e3a1"
        kosdaq_color = "#f38ba8" if kosdaq_chg < 0 else "#a6e3a1"
        self._lbl_kospi.setStyleSheet(f"color: {kospi_color};")
        self._lbl_kosdaq.setStyleSheet(f"color: {kosdaq_color};")




class ManualBuyDialog(QDialog):
    """수동 매수 확인 다이얼로그 — 시장가/지정가, 수량 입력."""


    def __init__(self, code: str, name: str, price: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("수동 매수")
        self.setFixedWidth(280)
        self.setModal(True)


        self._code  = code
        self._price = price


        lay = QVBoxLayout(self)
        lay.setSpacing(10)


        # ── 종목 정보 ──────────────────────────────────────────────────
        info = QLabel(f"<b>{name}</b>  ({code})")
        info.setAlignment(Qt.AlignCenter)
        lay.addWidget(info)


        self._lbl_price = QLabel(f"현재가: {price:,} 원")
        self._lbl_price.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._lbl_price)


        lay.addWidget(_hline())


        # ── 주문유형 ──────────────────────────────────────────────────
        g = QGridLayout()
        g.setColumnStretch(1, 1)


        g.addWidget(QLabel("주문유형"), 0, 0)
        self._combo_type = QComboBox()
        self._combo_type.addItems(["시장가", "지정가"])
        self._combo_type.currentTextChanged.connect(self._on_type_changed)
        g.addWidget(self._combo_type, 0, 1)


        g.addWidget(QLabel("수량"), 1, 0)
        self._spin_qty = QSpinBox()
        self._spin_qty.setRange(1, 9999)
        self._spin_qty.setValue(10)
        self._spin_qty.setSuffix(" 주")
        self._spin_qty.valueChanged.connect(self._update_estimate)
        g.addWidget(self._spin_qty, 1, 1)


        g.addWidget(QLabel("지정가"), 2, 0)
        self._spin_lmt = QSpinBox()
        self._spin_lmt.setRange(1, 99_999_999)
        self._spin_lmt.setValue(price)
        self._spin_lmt.setSingleStep(10)
        self._spin_lmt.setSuffix(" 원")
        self._spin_lmt.setEnabled(False)   # 시장가가 기본
        self._spin_lmt.valueChanged.connect(self._update_estimate)
        g.addWidget(self._spin_lmt, 2, 1)


        lay.addLayout(g)


        # ── 예상금액 ──────────────────────────────────────────────────
        self._lbl_est = QLabel()
        self._lbl_est.setAlignment(Qt.AlignCenter)
        self._lbl_est.setStyleSheet("color:#fab387; font-weight:bold;")
        lay.addWidget(self._lbl_est)
        self._update_estimate()


        lay.addWidget(_hline())


        # ── 버튼 ──────────────────────────────────────────────────────
        btns = QDialogButtonBox()
        self._btn_ok     = btns.addButton("매수 확인", QDialogButtonBox.AcceptRole)
        self._btn_cancel = btns.addButton("취소",      QDialogButtonBox.RejectRole)
        self._btn_ok.setStyleSheet("background:#a6e3a1; color:#1e1e2e; font-weight:bold;")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)


    # ── 내부 슬롯 ────────────────────────────────────────────────────
    def _on_type_changed(self, t: str) -> None:
        is_limit = (t == "지정가")
        self._spin_lmt.setEnabled(is_limit)
        self._update_estimate()


    def _update_estimate(self) -> None:
        qty = self._spin_qty.value()
        if self._combo_type.currentText() == "지정가":
            p = self._spin_lmt.value()
        else:
            p = self._price
        self._lbl_est.setText(f"예상금액: {qty * p:,} 원")


    # ── 결과 접근 ────────────────────────────────────────────────────
    def result_values(self) -> tuple[int, str, int]:
        """(수량, 주문유형코드, 가격) — 시장가=03, 지정가=00"""
        qty  = self._spin_qty.value()
        if self._combo_type.currentText() == "지정가":
            otype = "00"
            oprice = self._spin_lmt.value()
        else:
            otype  = "03"
            oprice = 0
        return qty, otype, oprice




