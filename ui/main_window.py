"""
MainWindow — 통합 대시보드 (PyQt5 · Deep Dark)

레이아웃
  ┌─────────────────────────────────────────────────────────┐
  │  HEADER : 계좌 / 서버 모드 / 연결 상태 / 당일 손익     │
  ├────────────┬──────────────────────────┬─────────────────┤
  │  SCANNER   │       CHART              │   PORTFOLIO     │
  │  (좌 패널) │  (캔들 + MA + Volume)   │   (우 패널)    │
  │  포착 종목 │                          │   보유 현황    │
  ├────────────┴──────────────────────────┴─────────────────┤
  │  LOG : 주문 전송 / 체결 / 스캐너 이벤트               │
  └─────────────────────────────────────────────────────────┘

스레딩 설계
  메인 스레드 : Qt 이벤트 루프 + Kiwoom OCX (QAxWidget)
  ScannerWorker(QThread) : SnapshotStore 읽기 + 신호 판단 (순수 Python)
  PortfolioWorker(QThread) : 잔고 동기화 (kiwoom TR 호출 — QMetaObject 경유)

  규칙: UI 위젯 갱신은 반드시 메인 스레드 pyqtSlot 에서만 수행
"""

from __future__ import annotations

import os
import time
import threading
import logging
import logging.handlers
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt5")
import pyqtgraph as pg
from PyQt5.QtCore import (
    Qt, QObject, QThread, QTimer, QEvent,
    pyqtSignal, pyqtSlot,
)
from PyQt5.QtGui import QColor, QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QTextEdit, QSplitter, QFrame, QHeaderView,
    QSizePolicy, QProgressBar, QDoubleSpinBox, QSpinBox,
    QDialog, QDialogButtonBox, QComboBox,
)

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.workers import ScannerWorker, PortfolioWorker

from logging_config import position_log
from config import TELEGRAM as _TG
from telegram_bot import TelegramBot
from scanner.smart_scanner import format_trade_amount_korean

# pyqtgraph Dark 설정 (import 직후 바로)
pg.setConfigOption("background", "#0d0d14")
pg.setConfigOption("foreground", "#cdd6f4")


# ---------------------------------------------------------------------------
# ── UI 패널 위젯 ─────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

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


def _hline() -> QFrame:
    """수평 구분선."""
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f


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


class ChartPanel(QWidget):
    """우하단 — 1분봉 차트 + 종목 판단 정보 패널"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 좌: 차트 영역 ──────────────────────────────────────────────
        chart_w = QWidget()
        chart_lay = QVBoxLayout(chart_w)
        chart_lay.setContentsMargins(0, 0, 0, 0)
        chart_lay.setSpacing(0)

        self._lbl_code = QLabel("  종목 차트")
        self._lbl_code.setObjectName("panel_title")
        chart_lay.addWidget(self._lbl_code)

        self._gw = pg.GraphicsLayoutWidget()
        chart_lay.addWidget(self._gw)

        # 가격 플롯 (상단 70%)
        self._price_plot = self._gw.addPlot(row=0, col=0)
        self._price_plot.showGrid(x=True, y=True, alpha=0.15)
        self._price_plot.getAxis("left").setWidth(70)
        self._price_plot.getAxis("bottom").setStyle(showValues=False)

        self._fill_base  = self._price_plot.plot(pen=None)
        self._price_line = self._price_plot.plot(pen=pg.mkPen("#74b9ff", width=2))
        self._price_fill = pg.FillBetweenItem(
            self._fill_base, self._price_line,
            brush=pg.mkBrush(116, 185, 255, 25),
        )
        self._price_plot.addItem(self._price_fill)

        # MA7 / MA15 (JDM 전략 기준)
        self._ma7_line  = self._price_plot.plot(pen=pg.mkPen("#ffeaa7", width=1.5))
        self._ma15_line = self._price_plot.plot(pen=pg.mkPen("#a29bfe", width=1.5))

        # 수평선: 현재가(파랑점선) / 매수가(노랑) / 트레일가(주황점선) / 고점(초록점선) / 손절(빨강점선)
        self._curr_line  = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#74b9ff", width=1, style=Qt.DashLine))
        self._buy_line   = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#ffeaa7", width=2, style=Qt.SolidLine))
        self._trail_line = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#fab387", width=1, style=Qt.DashLine))   # 주황 — 트레일 스탑가
        self._peak_line  = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#a6e3a1", width=1, style=Qt.DotLine))    # 초록 점선 — 고점
        self._sl_line    = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#f38ba8", width=1, style=Qt.DashLine))
        for line in [self._curr_line, self._buy_line, self._trail_line, self._peak_line, self._sl_line]:
            self._price_plot.addItem(line)
        self._buy_line.setVisible(False)
        self._trail_line.setVisible(False)
        self._peak_line.setVisible(False)
        self._sl_line.setVisible(False)

        # 범례
        leg = self._price_plot.addLegend(offset=(10, 10))
        leg.addItem(self._price_line, "현재가")
        leg.addItem(self._ma7_line,  "MA7")
        leg.addItem(self._ma15_line, "MA15")

        # 거래량 플롯 (하단 30%)
        self._volume_plot = self._gw.addPlot(row=1, col=0)
        self._volume_plot.showGrid(x=False, y=True, alpha=0.15)
        self._volume_plot.getAxis("left").setWidth(70)
        self._volume_plot.setLabel("bottom", "분봉 (분)")
        self._volume_plot.setXLink(self._price_plot)
        self._vol_bars = pg.BarGraphItem(x=[], height=[], width=0.7, pen=None)
        self._volume_plot.addItem(self._vol_bars)

        self._gw.ci.layout.setRowStretchFactor(0, 7)
        self._gw.ci.layout.setRowStretchFactor(1, 3)
        root.addWidget(chart_w, stretch=6)

        # ── 우: 정보 패널 ──────────────────────────────────────────────
        info_w = QWidget()
        info_w.setObjectName("chart_info_panel")
        info_lay = QVBoxLayout(info_w)
        info_lay.setContentsMargins(12, 12, 12, 12)
        info_lay.setSpacing(6)

        def _lbl(text="—", bold=False, size=9, color=None):
            l = QLabel(text)
            f = QFont("Malgun Gothic", size)
            f.setBold(bold)
            l.setFont(f)
            l.setWordWrap(True)
            if color:
                l.setStyleSheet(f"color: {color};")
            return l

        def _sep():
            f = QFrame()
            f.setFrameShape(QFrame.HLine)
            f.setStyleSheet("color: #313244; margin: 2px 0;")
            return f

        self._i_name   = _lbl("종목 선택", bold=True, size=10)
        self._i_signal = _lbl("신호: —", size=8, color="#a6e3a1")
        info_lay.addWidget(self._i_name)
        info_lay.addWidget(self._i_signal)
        info_lay.addWidget(_sep())

        self._i_buy    = _lbl("매수가: —", size=9)
        self._i_curr   = _lbl("현재가: —", size=9)
        self._i_pnl    = _lbl("손익: —", bold=True, size=10)
        info_lay.addWidget(self._i_buy)
        info_lay.addWidget(self._i_curr)
        info_lay.addWidget(self._i_pnl)
        info_lay.addWidget(_sep())

        self._i_hold   = _lbl("보유: —", size=9)
        self._i_remain = _lbl("남은 시간: —", size=9)
        info_lay.addWidget(self._i_hold)
        info_lay.addWidget(self._i_remain)
        info_lay.addWidget(_sep())

        self._i_peak  = _lbl("고점: —", size=8, color="#a6e3a1")
        self._i_trail = _lbl("트레일: —", size=8, color="#fab387")
        self._i_sl    = _lbl("손절까지: —", size=8, color="#f38ba8")
        info_lay.addWidget(self._i_peak)
        info_lay.addWidget(self._i_trail)
        info_lay.addWidget(self._i_sl)
        info_lay.addStretch()
        root.addWidget(info_w, stretch=2)

    @staticmethod
    def _rolling_mean(arr, window: int):
        import numpy as np
        a = np.array(arr, dtype=float)
        result = np.empty(len(a))
        kernel = np.ones(window) / window
        full = np.convolve(a, kernel, mode="full")[:len(a)]
        for i in range(min(window - 1, len(a))):
            result[i] = a[: i + 1].mean()
        result[window - 1:] = full[window - 1:]
        return result

    def update_chart(
        self,
        closes: list,
        volumes: list,
        code: str,
        name: str,
        position=None,
        trail_price: int = 0,
        sl_pct: float = -1.5,
        signal_reason: str = None,
    ) -> None:
        """1분봉 데이터 + 포지션 정보로 차트와 정보 패널을 갱신한다."""
        self._lbl_code.setText(f"  {'📈' if position else '👁️'} {name}  ({code})")

        # ── 차트 갱신 ────────────────────────────────────────────────
        if len(closes) >= 2:
            import numpy as np
            x = list(range(len(closes)))
            self._price_line.setData(x=x, y=closes)
            self._fill_base.setData(x=x, y=[min(closes)] * len(closes))
            self._curr_line.setValue(closes[-1])
            if len(closes) >= 7:
                self._ma7_line.setData(x=x, y=self._rolling_mean(closes, 7))
            if len(closes) >= 15:
                self._ma15_line.setData(x=x, y=self._rolling_mean(closes, 15))
            if volumes:
                vols = volumes[:len(closes)]
                avg_vol = float(np.mean(vols)) if vols else 1.0
                self._vol_bars.setOpts(
                    x=x[:len(vols)], height=vols, width=0.7,
                    brushes=[
                        pg.mkBrush("#a6e3a1") if v >= avg_vol else pg.mkBrush("#585b70")
                        for v in vols
                    ],
                )

        # ── 정보 패널 갱신 ───────────────────────────────────────────
        curr = closes[-1] if closes else 0

        if position:
            avg  = position.avg_price
            curr = position.current_price or curr
            qty  = position.qty
            peak = position.peak_price or 0

            sl_price = int(avg * (1 + sl_pct / 100))

            self._buy_line.setValue(avg);   self._buy_line.setVisible(True)
            self._sl_line.setValue(sl_price); self._sl_line.setVisible(True)
            if peak > 0:
                self._peak_line.setValue(peak);   self._peak_line.setVisible(True)
            else:
                self._peak_line.setVisible(False)
            if trail_price > 0:
                self._trail_line.setValue(trail_price); self._trail_line.setVisible(True)
            else:
                self._trail_line.setVisible(False)

            pnl      = (curr - avg) * qty
            pnl_pct  = (curr - avg) / avg * 100 if avg else 0
            sign     = "+" if pnl >= 0 else ""
            color    = "#a6e3a1" if pnl >= 0 else "#f38ba8"

            dist_sl_pct = (curr - sl_price) / curr * 100 if curr else 0

            from datetime import datetime as _dt
            hold_str = "—"
            remain_str = "—"
            if hasattr(position, "entry_time") and position.entry_time:
                held = int((_dt.now() - position.entry_time).total_seconds() / 60)
                hold_str   = f"{held}분 경과"
                remain_str = f"{max(0, 60 - held)}분 남음"

            # 트레일 정보 텍스트
            if peak > 0 and trail_price > 0:
                peak_chg_pct  = (peak - avg) / avg * 100 if avg else 0
                trail_chg_pct = (trail_price - avg) / avg * 100 if avg else 0
                peak_txt  = f"고점:  {peak:,}원  (+{peak_chg_pct:.2f}%)"
                trail_txt = f"트레일가:  {trail_price:,}원  ({trail_chg_pct:+.2f}%)"
            elif peak > 0:
                peak_chg_pct = (peak - avg) / avg * 100 if avg else 0
                peak_txt  = f"고점:  {peak:,}원  (+{peak_chg_pct:.2f}%)"
                trail_txt = "트레일:  대기 중 (고점 미달성)"
            else:
                peak_txt  = f"고점:  — (활성화 대기)"
                trail_txt = "트레일:  —"

            self._i_name.setText(f"📈 {name}\n({code})")
            self._i_signal.setText(f"신호: {signal_reason or '앱 매수'}")
            self._i_buy.setText(f"매수가:  {avg:,}원")
            self._i_curr.setText(f"현재가:  {curr:,}원")
            self._i_pnl.setText(f"손익:  {sign}{pnl:,}원  ({sign}{pnl_pct:.2f}%)")
            self._i_pnl.setStyleSheet(f"color: {color}; font-weight: bold;")
            self._i_hold.setText(f"보유: {hold_str}")
            self._i_remain.setText(f"홀딩: {remain_str}  (최대 60분)")
            self._i_peak.setText(peak_txt)
            self._i_trail.setText(trail_txt)
            self._i_sl.setText(f"손절까지:  {-dist_sl_pct:.2f}%  ({sl_price:,}원)")
        else:
            self._buy_line.setVisible(False)
            self._trail_line.setVisible(False)
            self._peak_line.setVisible(False)
            self._sl_line.setVisible(False)

            self._i_name.setText(f"👁️ {name}\n({code})")
            self._i_signal.setText(f"신호: {signal_reason or '감시 중'}")
            self._i_buy.setText("매수가:  —  (미보유)")
            self._i_curr.setText(f"현재가:  {curr:,}원" if curr else "현재가:  —")
            self._i_pnl.setText("손익:  —")
            self._i_pnl.setStyleSheet("color: #6c7086;")
            self._i_hold.setText("보유:  —")
            self._i_remain.setText("홀딩:  —")
            self._i_peak.setText("고점:  —")
            self._i_trail.setText("트레일:  —")
            self._i_sl.setText(f"손절 기준:  {sl_pct:.1f}%")


class _NoWheelSpinBox(QSpinBox):
    """마우스 휠로 값이 바뀌지 않는 SpinBox — 테이블 스크롤 우선"""
    def wheelEvent(self, e):
        e.ignore()


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    """마우스 휠로 값이 바뀌지 않는 DoubleSpinBox — 테이블 스크롤 우선"""
    def wheelEvent(self, e):
        e.ignore()


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
        info_lay.addStretch()

        lbl_tp = QLabel("익절")
        lbl_tp.setObjectName("risk_label")
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
        lbl_sl.setObjectName("risk_label")
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
        hdr = self._table.horizontalHeader()
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
        cash        = data.get("cash", 0)
        positions   = data.get("positions", {})       # dict[code, Position]
        watch_today = data.get("watch_today", {})     # dict[code, {name, price, signal_type}]

        self._lbl_cash.setText(f"  예수금: {cash:,} 원")

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
        ts = datetime.now().strftime("%H:%M:%S")

        if "체결" in text or "완료" in text:
            color = "#89dceb"
        elif "오류" in text or "실패" in text or "경고" in text:
            color = "#f38ba8"
        elif "🚨" in text or "신호" in text:
            color = "#fab387"
        else:
            color = "#6c7086"

        self._log.append(f'<span style="color:{color};">[{ts}] {text}</span>')
        self._log.moveCursor(QTextCursor.MoveOperation.End)

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


# ---------------------------------------------------------------------------
# ── MainWindow ───────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    통합 대시보드 메인 윈도우.

    사용 예)
        app = QApplication(sys.argv)
        kiwoom = KiwoomManager()
        win = MainWindow(kiwoom)
        win.show()
        sys.exit(app.exec())
    """

    def __init__(self, kiwoom, parent=None) -> None:
        super().__init__(parent)
        self._kiwoom = kiwoom

        # 당일 감시 종목 누적 {code: {name, price, signal_type}}
        self._today_watch: dict = {}
        # time.monotonic() 기준 — 이 시각 이전에는 _auto_sell_by_pnl 미실행
        self._sl_tp_warmup_end: float = 0.0
        # 블로킹 방지 플래그
        self._scan_in_progress: bool = False
        self._liquidate_in_progress: bool = False
        # 급락 감지로 자동매매가 OFF된 상태 — 수동으로 켜야만 해제됨
        self._market_crash_off: bool = False

        self.setWindowTitle("키움 자동매매 대시보드")
        self.resize(1600, 900)
        self.setStyleSheet(_DARK_QSS)

        self._build_ui()
        self._setup_modules()
        self._setup_timers()

    # -----------------------------------------------------------------------
    # UI 구성
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 상단 헤더
        self.header = HeaderBar()
        root.addWidget(self.header)

        # 구분선
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("h_sep")
        root.addWidget(sep)

        # 메인 영역 — 좌(스캐너 40%) | 우(보유현황+차트 60%)
        h_split = QSplitter(Qt.Horizontal)
        h_split.setHandleWidth(2)

        # 좌: 스캐너 감시 종목
        self.scanner_panel = ScannerPanel()
        h_split.addWidget(self.scanner_panel)

        # 우: 보유현황(위) + 차트(아래) 세로 분할
        right_v = QSplitter(Qt.Vertical)
        right_v.setHandleWidth(2)
        from config import RISK as _RISK
        self.portfolio_panel = PortfolioPanel(
            tp_init=_RISK.get("take_profit_pct", 3.0),
            sl_init=_RISK.get("stop_loss_pct",  -1.2),
        )
        self.chart_panel     = ChartPanel()
        right_v.addWidget(self.portfolio_panel)
        right_v.addWidget(self.chart_panel)
        right_v.setSizes([320, 520])   # 보유현황:차트 ≈ 38:62
        h_split.addWidget(right_v)

        # 4 : 6 비율
        h_split.setSizes([640, 960])

        root.addWidget(h_split, stretch=1)

        # 구분선
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setObjectName("h_sep")
        root.addWidget(sep2)

        # 스캔 상태바
        self.scan_status = ScanStatusBar()
        root.addWidget(self.scan_status)

        # 수급 현황 패널 (스캔 상태바 아래, 로그 위)
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.HLine)
        sep3.setObjectName("h_sep")
        root.addWidget(sep3)

        self.investor_panel = InvestorPanel()
        root.addWidget(self.investor_panel)

        # 하단 로그
        self.log_panel = LogPanel()
        root.addWidget(self.log_panel)

    # -----------------------------------------------------------------------
    # 모듈 / 워커 / 시그널 연결
    # -----------------------------------------------------------------------

    def _setup_modules(self) -> None:
        from auth.login_manager import LoginManager
        from order.order_manager import OrderManager
        from trade_audit_logger import TradeAuditLogger
        self._audit = TradeAuditLogger(log_dir="logs")

        # ── LoginManager ──────────────────────────────────────────────────
        self.login_mgr = LoginManager(self._kiwoom, parent=self)
        self.login_mgr.login_success.connect(self._on_login_success)
        self.login_mgr.login_failed.connect(
            lambda m: self.log_panel.append(f"⚠ 로그인 실패: {m}")
        )

        # ── 자동 재로그인 콜백 등록 ────────────────────────────────────────
        self._kiwoom.set_auto_login_callback(lambda: self.log_panel.append("✅ 자동 재로그인 성공"))

        # ── OrderManager ──────────────────────────────────────────────────
        from config import STRATEGY
        self.order_mgr = OrderManager(
            self._kiwoom,
            max_positions=STRATEGY.get("max_positions", 5),
            parent=self
        )
        self.order_mgr._audit = self._audit
        # [800033] 매도가능수량 부족 에러 → 포지션 메모리 정리 콜백
        self._kiwoom._on_order_msg_cb = self.order_mgr.on_order_msg
        self.order_mgr.order_sent.connect(
            lambda d: self.log_panel.append(
                f"{d['side']} 주문 전송 — {d['name']}({d['code']}) "
                f"{d['qty']}주 {d['price']}원"
            )
        )
        self.order_mgr.order_filled.connect(self._on_order_filled)
        self.order_mgr.order_failed.connect(
            lambda m: self.log_panel.append(f"⚠ 주문 실패: {m}")
        )

        # ── 보유현황 수동 매도 ────────────────────────────────────────────
        self.portfolio_panel.manual_sell.connect(self._on_manual_sell)

        # ── SmartScanner 설정 ─────────────────────────────────────────────
        from config import RISK as _RISK
        from scanner.smart_scanner import SmartScanner, SmartScannerConfig, SnapshotStore
        self._snap_store = SnapshotStore()
        self._scan_cfg   = SmartScannerConfig.from_adaptive("config/adaptive_params.json")
        _yosep_preset = str(STRATEGY.get("yosep_preset", "") or "").strip().lower()
        if _yosep_preset:
            self._scan_cfg.apply_yosep_preset(_yosep_preset)
        self._scan_cfg.max_change_pct = float(_RISK.get("max_change_pct", 15.0))
        self._scan_cfg.signal_cooldown_sec = float(
            _RISK.get("signal_cooldown_sec", 45.0)
        )
        self._scan_cfg.index_block_pct = float(_RISK.get("market_index_block_pct", -1.5))
        # [진단] 샘플 개수 — None 이면 max_positions 와 동일하게 맞춤(혼동 완화)
        _dsn = STRATEGY.get("diagnostic_sample_n")
        if _dsn is not None:
            self._scan_cfg.diagnostic_sample_n = max(1, int(_dsn))
        else:
            self._scan_cfg.diagnostic_sample_n = max(
                1, int(STRATEGY.get("max_positions", 5))
            )
        # 감시 유니버스 상한 — 숫자로 주면 후보·실시간 구독·Worker 표시 행 수가 함께 줄어듦
        _wpm = STRATEGY.get("watch_pool_max")
        if _wpm is not None:
            wpm = max(1, int(_wpm))
            self._scan_cfg.watch_pool_max = wpm
            self._scan_cfg.realtime_sub_max = wpm
            self._scan_cfg.display_top_n = wpm

        # SmartScanner 생성 (실시간 OnReceiveRealData 연결 포함)
        self._smart_scanner = SmartScanner(self._kiwoom, self._scan_cfg)
        self._smart_scanner.store = self._snap_store   # ScannerWorker와 store 공유
        self._smart_scanner.on_signal = self._on_scan_signal_direct

        # 익절/손절 기준값 — config 우선, 이후 화면 SpinBox 변경 시 실시간 반영
        self._auto_tp_pct:   float = _RISK.get("take_profit_pct", 3.0)
        self._auto_sl_pct:   float = _RISK.get("stop_loss_pct",  -1.2)
        self._hard_stop_pct: float = _RISK.get("hard_stop_pct",  -2.0)
        self.portfolio_panel.tp_changed.connect(self._on_tp_changed)
        self.portfolio_panel.sl_changed.connect(self._on_sl_changed)
        self.log_panel.append("[스캐너] SmartScanner 초기화 완료")

        # TradingEngine MVC Controller
        from engine.trading_engine import TradingEngine
        self._engine = TradingEngine(
            kiwoom=self._kiwoom,
            order_manager=self.order_mgr,
            snap_store=self._snap_store,
            scan_cfg=self._scan_cfg,
            audit=self._audit,
            parent=self
        )
        self._engine.log_message.connect(self.log_panel.append)

        # ── ScannerLogHandler — scanner.audit → 대시보드 패널 중계 ─────────
        from scanner.smart_scanner import scan_log as _scan_audit_log
        self._scan_log_handler = ScannerLogHandler(self)
        self._scan_log_handler.log_entry.connect(self.log_panel.append_scanner)
        _scan_audit_log.addHandler(self._scan_log_handler)

        # ── Phase 3: Application Layer 초기화 ────────────────────────
        from app.market_scheduler import MarketScheduler
        from app.risk_manager import RiskManager
        from app.trading_controller import TradingController

        self.market_scheduler = MarketScheduler(self)
        self.risk_manager = RiskManager(self.order_mgr, self._scan_cfg, self)
        self.trading_controller = TradingController(
            self.order_mgr, self._scan_cfg, self.risk_manager,
            snap_store=self._snap_store, parent=self
        )
        self._setup_controller_signals()
        self.log_panel.append("[앱] Application Layer 초기화 완료")

        # ── SysLogQtHandler — Python root logger → 시스템 로그 패널(우) ──────
        self._sys_log_handler = SysLogQtHandler(self)
        self._sys_log_handler.log_entry.connect(self.log_panel.append_syslog)
        logging.getLogger().addHandler(self._sys_log_handler)
        # 핸들러 연결 확인용 — 시스템 로그 패널에 바로 표시되는지 검증
        logging.getLogger("kiwoom_api").info("[SysLog] 시스템 로그 패널 연결 완료")

        # ── NewsAnalyzer — 백그라운드 뉴스 분석 ──────────────────────────
        import queue as _queue
        from scanner.news_analyzer import NewsAnalyzer
        self._news_queue: _queue.Queue = _queue.Queue()
        self._news_analyzer = NewsAnalyzer(
            on_result=lambda r: self._news_queue.put(r)   # 백그라운드 스레드 → 큐
        )
        self._news_analyzer.start()
        self.log_panel.append("[뉴스] NewsAnalyzer 백그라운드 스레드 시작")

        # ── ScannerWorker → QThread ───────────────────────────────────────
        self._scan_thread  = QThread(self)
        self._scan_worker  = ScannerWorker(self._snap_store, self._scan_cfg, self.order_mgr)
        self._scan_worker._audit = self._audit
        self._scan_worker.moveToThread(self._scan_thread)

        self._scan_thread.started.connect(self._scan_worker.run)
        # signal_detected → _on_scan_signal (로그) + 자동매매 ON 상태일 때만 주문
        self._scan_worker.signal_detected.connect(self._on_scan_signal)
        self._scan_worker.watch_list_updated.connect(self.scanner_panel.refresh)
        self._scan_worker.watch_list_updated.connect(self.investor_panel.refresh)
        self._scan_worker.log_message.connect(self.log_panel.append)

        # ── PortfolioWorker — 메인 스레드 QTimer 방식 ────────────────────
        self._port_worker = PortfolioWorker(self.order_mgr, parent=self)
        self._port_worker.refresh_done.connect(self._on_portfolio_refresh)
        self._port_worker.log_message.connect(self.log_panel.append)

        # ── Safety Switch → 자동매매 ON/OFF ──────────────────────────────
        self._auto_trading: bool = False   # 기본값: 정지 상태
        self.header.auto_trade_toggled.connect(self._on_auto_trade_toggle)
        self.header.exit_requested.connect(self.close)
        self.header.unlock_requested.connect(self._on_manual_unlock_requested)
        self.header.overnight_mode_toggled.connect(self._on_overnight_mode_toggle)
        self.header.switch_real_requested.connect(self._on_switch_real_requested)

        # 버튼 연결 확인 로그
        self.log_panel.append(
            f"[연결] auto_trade_toggled 시그널 연결됨 "
            f"(receiver: _on_auto_trade_toggle)"
        )

        # ── 스캐너 클릭 → 차트 갱신 ─────────────────────────────────────
        self.scanner_panel.row_clicked.connect(self._on_code_selected)

        # ── 스캐너 수동 매수 버튼 ─────────────────────────────────────
        self.scanner_panel.manual_buy_requested.connect(self._on_manual_buy)

        # ── 보유현황 클릭 → 차트 갱신 ───────────────────────────────────
        self.portfolio_panel.row_clicked.connect(self._on_code_selected)

        # ── 텔레그램 봇 초기화 ──────────────────────────────────────────
        if _TG.get("enabled") and _TG.get("token"):
            try:
                self._tg = TelegramBot(_TG["token"], _TG["chat_id"], parent=self)
                self._tg.cmd_start.connect(lambda: self._on_auto_trade_toggle(True))
                self._tg.cmd_stop.connect(lambda: self._on_auto_trade_toggle(False))
                self._tg.cmd_status.connect(self._on_tg_status_requested)
                self._tg.start()
                self.log_panel.append("[연결] 텔레그램 봇 연결됨")
            except Exception as e:
                logger.warning("[텔레그램] 봇 초기화 실패: %s", e)
                self._tg = None
        else:
            self._tg = None

        # ── HealthMonitor — 자가 진단/자기 진화 ──────────────────────────────
        from analysis.health_monitor import HealthMonitor as _HealthMonitor
        # on_freeze: force_unfreeze (TR EventLoop 강제 해제) + 스캔 상태 리셋 + 비동기 재연결
        def _on_freeze_handler():
            import logging as _lg
            _freeze_log = _lg.getLogger(__name__)
            _freeze_log.warning("[on_freeze] 프리징 복구 핸들러 실행")
            # ① TR EventLoop 강제 종료
            force_fn = getattr(self._kiwoom, "force_unfreeze", None)
            if force_fn:
                force_fn()
            # ② 스캔 진행 상태 플래그 리셋 (이후 스캔 사이클 차단 방지)
            self._scan_in_progress = False
            _freeze_log.warning("[on_freeze] _scan_in_progress 리셋 완료")
            # ③ 재연결 제거 — force_unfreeze만으로 TR EventLoop 복구 충분
            # reconnect_silent()는 CommConnect() → 로그인 창 팝업 → 화면 차단 유발
            # 연결 상태는 기존 15분 주기 _connection_timer가 별도로 확인

        self._health_monitor = _HealthMonitor(
            scan_cfg       = self._scan_cfg,
            on_param_relax = self._on_health_param_relax,
            on_freeze      = _on_freeze_handler,
            on_reconnect   = (self.login_mgr.reconnect_silent
                              if hasattr(self, "login_mgr") and self.login_mgr
                              else getattr(self._kiwoom, "auto_reconnect", None)),
        )

    def _setup_controller_signals(self) -> None:
        """Application Layer 신호 연결"""
        # MarketScheduler → MainWindow 슬롯
        self.market_scheduler.market_opened.connect(self._on_market_opened)
        self.market_scheduler.market_closing.connect(self._on_market_closing)
        self.market_scheduler.feedback_triggered.connect(self._on_feedback_triggered)
        self.market_scheduler.day_reset.connect(self._on_day_reset)

        # RiskManager → MainWindow 슬롯
        self.risk_manager.daily_profit_locked.connect(self._on_profit_locked)
        self.risk_manager.daily_loss_cut.connect(self._on_loss_cut)

        # TradingController → 신호 거절 로그
        self.trading_controller.signal_rejected.connect(
            lambda msg: logger.debug(f"[신호 거절] {msg}")
        )

        # HeaderBar 신호 → TradingController
        self.header.auto_trade_toggled.connect(self.trading_controller.set_auto_trading)

    def _setup_timers(self) -> None:
        """QTimer — 모두 메인 스레드에서 실행 (Kiwoom OCX 스레드 규칙 준수)"""
        self._selected_code: str = ""

        # 차트 갱신 + 현재가 기반 포트폴리오 갱신 (2초)
        self._chart_timer = QTimer(self)
        self._chart_timer.timeout.connect(self._refresh_chart)
        self._chart_timer.timeout.connect(self._refresh_portfolio_prices)
        self._chart_timer.start(5000)  # 2026-04-23: 2s→5s (메인 스레드 이벤트 루프 부하 감소, Watchdog ACK 우선순위)

        # 잔고 동기화 (1분) — scan_refresh_timer(60s)와 발화 시간 어긋나도록 5s 지연 시작
        self._balance_timer = QTimer(self)
        self._balance_timer.timeout.connect(self._port_worker.sync)
        # QTimer.singleShot으로 첫 발화를 5s 늦춰 scan timer와 충돌 방지
        QTimer.singleShot(5_000, lambda: self._balance_timer.start(60_000))

        # Phase 3: MarketScheduler가 시장 시간 스케줄링을 전담함

        # 연결 상태 확인 (15분마다) — 자동 재로그인
        self._connection_timer = QTimer(self)
        self._connection_timer.timeout.connect(self._check_connection)
        self._connection_timer.start(900_000)  # 15분

        # 지수 급락 감지 (60초마다) — 헤더 지수 표시 + 급락 감지
        self._crash_check_timer = QTimer(self)
        self._crash_check_timer.timeout.connect(self._check_market_crash)
        QTimer.singleShot(35_000, lambda: self._crash_check_timer.start(60_000))  # 35s 뒤 시작

        # opt10030 주기 스캔 (1분마다) — 메인 스레드에서 호출 (Kiwoom TR은 메인 스레드만 지원)
        # 타임아웃 2초로 설정하여 응답 없으면 빨리 폴백
        self._scan_refresh_timer = QTimer(self)
        self._scan_refresh_timer.timeout.connect(self._run_scanner_scan)
        # 장 시작 전까지는 타이머만 등록, start_after_login 후 가동

        # 뉴스 분석 결과 드레인 (1초마다) — 백그라운드 스레드 결과를 메인 스레드에서 안전하게 처리
        self._news_drain_timer = QTimer(self)
        self._news_drain_timer.timeout.connect(self._drain_news_queue)
        self._news_drain_timer.start(1000)

        # 텔레그램 1시간 보고 (1시간마다) — 자동매매 ON일 때 장중에만 발송
        self._tg_report_timer = QTimer(self)
        self._tg_report_timer.timeout.connect(self._send_tg_status)
        self._tg_report_timer.start(60 * 60 * 1000)

        # Watchdog ACK (5초마다) — HealthMonitor에 UI가 살아있음을 알림
        self._watchdog_ack_timer = QTimer(self)
        self._watchdog_ack_timer.timeout.connect(
            lambda: getattr(self, "_health_monitor", None) and self._health_monitor.ack()
        )
        self._watchdog_ack_timer.start(5_000)

        # [P2] 메모리 정리 (1시간마다) — 오래된 dict 키 삭제
        self._memory_cleanup_timer = QTimer(self)
        self._memory_cleanup_timer.timeout.connect(self._cleanup_memory)
        self._memory_cleanup_timer.start(60 * 60 * 1000)  # 1시간

        # [수급 필터] 외국인/기관 순매수 opt10059 주기 갱신
        _investor_ms = int(self._scan_cfg.investor_refresh_min * 60 * 1000)
        self._investor_timer = QTimer(self)
        self._investor_timer.timeout.connect(self._on_investor_refresh_tick)
        self._investor_timer.start(_investor_ms)  # 기본 10분

        # Phase 3: MarketScheduler 시작 (1분마다 시장 시간 체크)
        self.market_scheduler.start()

        self._opened_today:       bool = False
        self._closed_today:       bool = False
        self._feedback_done_today: bool = False
        self._feedback_thread = None   # GC 방지용 참조

        # Phase 3: 이제 risk_manager에서 관리되지만, 로그 중복 방지를 위해 로컬도 유지
        self._manual_unlock_active: bool = False
        self._new_entry_locked:    bool = False
        self._daily_loss_cut_done: bool = False

    # -----------------------------------------------------------------------
    # 로그인 후 워커 시작
    # -----------------------------------------------------------------------

    def start_after_login(self) -> None:
        """로그인 완료 후 호출"""
        self._kiwoom._account   = self.login_mgr.account
        self.order_mgr._account = self.login_mgr.account

        self._smart_scanner.on_index_update = self._on_realtime_index

        # [NEW] 포지션 실시간 현재가 갱신 + 동적 감시 중단/재개 콜백 연결
        self._smart_scanner._order_mgr = self.order_mgr
        max_pos = self.order_mgr.max_positions

        def _on_pos_opened(code: str):
            self._smart_scanner.add_position_realtime(code)
            # 포지션이 max에 도달했으면 유니버스 감시 중단
            if len(self.order_mgr.positions) >= max_pos:
                pos_codes = list(self.order_mgr.positions.keys())
                self._smart_scanner.pause_universe_watch(pos_codes)

        def _on_pos_closed(code: str):
            self._smart_scanner.remove_position_realtime(code)
            # on_position_closed는 del positions[code] 이전에 호출되므로 현재 포지션 수 - 1
            remaining = len(self.order_mgr.positions) - 1
            if remaining < max_pos:
                self._smart_scanner.resume_universe_watch()

        self.order_mgr.on_position_opened = _on_pos_opened
        self.order_mgr.on_position_closed = _on_pos_closed
        self.log_panel.append("[실시간] 포지션 현재가 갱신 + 동적 감시 중단/재개 콜백 연결")

        # ScannerWorker 스레드 시작 (실시간 신호 판단)
        self._scan_thread.start()
        self.log_panel.append("[워커] ScannerWorker 스레드 시작")

        # 잔고 1회 즉시 동기화
        self._port_worker.sync()

        # [NEW] 기존 보유 포지션 실시간 등록 (앱 외부에서 매수한 포지션 포함)
        for code in self.order_mgr.positions:
            self._smart_scanner.add_position_realtime(code)

        # [NEW] 잔고 동기화 완료(2~3초) 후 초기 감시 상태 결정
        # — 포지션 풀이면 자동으로 유니버스 감시 중단
        def _init_watch_state():
            if len(self.order_mgr.positions) >= max_pos:
                pos_codes = list(self.order_mgr.positions.keys())
                self._smart_scanner.pause_universe_watch(pos_codes)

        QTimer.singleShot(3000, _init_watch_state)

        # HealthMonitor 시작
        self._health_monitor.start()
        self.log_panel.append("[HealthMonitor] 자가 진단 시작")

        # opt10030 첫 스캔을 1초 후 실행 (로그인 직후 여유)
        self.log_panel.append("[스캔] 1초 후 opt10030 초기 스캔 예약...")
        QTimer.singleShot(1000, self._run_scanner_scan)

        # 지수 초기 조회 — 스캔(1s) 완료 후 5s 뒤 (TR 충돌 회피)
        QTimer.singleShot(6_000, self._check_market_crash)

        # 수급 초기 조회 — 스캔 + 지수 완료 후 10s 뒤
        QTimer.singleShot(12_000, self._on_investor_refresh_tick)

        # 이후 1분마다 반복 스캔
        self._scan_refresh_timer.start(60_000)
        self.log_panel.append("[스캔] 1분 주기 스캔 타이머 시작 (타임아웃 2초)")

    def closeEvent(self, event) -> None:
        self._audit.flush_all()
        self.market_scheduler.stop()  # Phase 3: MarketScheduler 중지
        self._connection_timer.stop()
        self._balance_timer.stop()
        self._crash_check_timer.stop()
        self._chart_timer.stop()
        self._scan_refresh_timer.stop()
        self._news_drain_timer.stop()
        self._tg_report_timer.stop()
        self._news_analyzer.stop()
        self._health_monitor.stop()
        self._scan_worker.stop()
        self._scan_thread.quit()
        self._scan_thread.wait(3000)
        # log_monitor 자식 프로세스 종료
        proc = getattr(self, "_log_monitor_proc", None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        # 로그 핸들러 정리 — root logger에서 제거 (프로그램 종료 후 시그널 emit 방지)
        logging.getLogger().removeHandler(self._sys_log_handler)
        if self._tg:
            # 종료 알림 발송 (타임아웃 2초로 빨리 처리)
            import requests
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self._tg._token}/sendMessage",
                    json={"chat_id": self._tg._chat_id, "text": "🛑 프로그램 종료됨"},
                    timeout=2,
                )
            except Exception:
                pass
            self._tg.stop()
        super().closeEvent(event)

    # -----------------------------------------------------------------------
    # 슬롯 — 이벤트 처리
    # -----------------------------------------------------------------------

    @pyqtSlot(str, str)
    def _on_login_success(self, account: str, mode: str) -> None:
        import time as _time
        from config import RISK as _RISK2

        self.header.set_connected(account, mode)
        self.log_panel.append(f"로그인 성공 — {mode} / 계좌: {account}")

        # 재연결(reconnect_silent) 시 start_after_login() 재호출 차단
        # → CommConnect 중복 호출(err=-101) 및 워밍업 리셋으로 인한 오청산 방지
        if getattr(self, "_already_started", False):
            # 계좌 정보만 최신화하고 잔고 재동기화
            if hasattr(self._kiwoom, "_account"):
                self._kiwoom._account = account
            if hasattr(self.order_mgr, "_account"):
                self.order_mgr._account = account
            self._port_worker.sync()
            self.log_panel.append(f"[재연결] 계좌 재설정 완료 — {mode} / {account}")
            return

        self._already_started = True
        self._today_watch.clear()           # 로그인 시 당일 감시 목록 초기화
        self._news_analyzer.reset_daily()   # 뉴스 분석 캐시 초기화
        _wu = float(_RISK2.get("sl_tp_warmup_sec", 45.0))
        self._sl_tp_warmup_end = _time.monotonic() + max(0.0, _wu)
        if _wu > 0:
            self.log_panel.append(
                f"[리스크] 로그인 후 {_wu:.0f}초간 자동 손절·익절 보류 (잔고·시세 안정화)"
            )
        # 텔레그램 시작 알림
        if self._tg:
            self._tg.send(f"🚀 프로그램 시작됨\n계좌: {account}\n모드: {mode}")
        self.start_after_login()

    @pyqtSlot(dict)
    def _on_order_filled(self, d: dict) -> None:
        ab = d.get("avg_buy_price")
        if d.get("side") == "매도체결" and ab is not None:
            line = (
                f"✅ {d['side']} — {d['name']}({d['code']}) {d['filled_qty']}주 "
                f"매수가 {ab:,}원 → 매도가 {d['filled_price']:,}원"
            )
            # 매도 완료 종목은 감시 목록에서 제거
            self._today_watch.pop(d.get("code", ""), None)
        else:
            line = (
                f"✅ {d['side']} — {d['name']}({d['code']}) "
                f"{d['filled_qty']}주 @{d['filled_price']:,}원"
            )
        self.log_panel.append(line)
        # 텔레그램 알림 발송
        if self._tg:
            self._tg.send(line)
        # 포트폴리오 즉시 갱신 트리거
        self._on_portfolio_refresh({
            "cash":      self.order_mgr.cash,
            "positions": dict(self.order_mgr.positions),
        })

        # HealthMonitor — 매도체결 시 손익 기록
        _hm = getattr(self, "_health_monitor", None)
        if _hm is not None and d.get("side") == "매도체결":
            _ab = d.get("avg_buy_price") or 0
            _fp = d.get("filled_price", 0)
            _fq = d.get("filled_qty",   0)
            _pnl = (_fp - _ab) * _fq if _ab and _fp and _fq else 0.0
            from analysis.health_monitor import TradeRecord as _TR
            _hm.record_trade(_TR(
                code       = d.get("code",  ""),
                pnl        = float(_pnl),
                entry_time = str(d.get("entry_time",  "")),
                exit_time  = str(d.get("filled_time", "")),
                reason     = d.get("reason", ""),
            ))

    @pyqtSlot()
    def _check_connection(self) -> None:
        """15분마다 연결 상태 확인 — 끊김 감지 시 자동 재로그인"""
        if not self._kiwoom.is_connected():
            self.log_panel.append("⚠️ 연결 끊김 감지 — 자동 재로그인 시도 중...")
            if hasattr(self, "login_mgr") and self.login_mgr:
                self.login_mgr.reconnect_silent()
            else:
                self._kiwoom.auto_reconnect()

    def _on_realtime_index(self, idx_code: str, current: float, chg_pct: float) -> None:
        """지수 로직 비활성화 — TR 부하 제거"""
        return

    @pyqtSlot()
    def _check_market_crash(self) -> None:
        """지수 조회 비활성화 — TR 부하 제거. 급락 감지 미사용."""
        return

    # Phase 3: _check_market_time과 _check_daily_pnl_limits는 제거됨
    # → MarketScheduler가 시장 시간 스케줄링 담당
    # → RiskManager가 일일 손익 한도 체크 담당

    # ── Feedback Loop ─────────────────────────────────────────────────────────

    def _run_feedback_loop(self) -> None:
        """15:35에 호출 — FeedbackEngine을 QThread에서 실행, 완료 시 _on_feedback_done() 호출."""
        from PyQt5.QtCore import QThread
        self.log_panel.append("📊 [피드백] 장 마감 분석 시작...")
        thread = QThread(self)
        worker = _FeedbackWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_feedback_done)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._feedback_thread = thread   # GC 방지

    @pyqtSlot(object)
    def _on_feedback_done(self, result) -> None:
        """피드백 완료 콜백 — UI 갱신 및 로그 출력"""
        from datetime import date as _date
        pnl_str  = f"{result.total_realized:+,.0f}원"
        color    = "#a6e3a1" if result.profitable else "#f38ba8"

        self.log_panel.append(
            f"📊 [피드백] {result.total_trades}건 분석 완료 | 손익 {pnl_str}"
        )
        if result.profitable:
            self.log_panel.append("  └─ 수익 당일 — 파라미터 유지")
        elif result.adjustments:
            for adj in result.adjustments:
                arrow = "▲" if adj.new_val > adj.old_val else "▼"
                self.log_panel.append(
                    f"  └─ {adj.param}: {adj.old_val} {arrow} {adj.new_val}"
                    f"  ({adj.reason})"
                )
        for reason in result.skipped_reasons:
            self.log_panel.append(f"  └─ [보류] {reason}")

        if result.report_path:
            self.log_panel.append(f"  └─ 리포트: {result.report_path}")

        # Telegram 알림 — LogAnalyzer가 생성한 상세 리포트 우선 사용
        _tg = getattr(self, "_tg", None)
        if _tg:
            if result.telegram_msg:
                msg = result.telegram_msg
            else:
                # LogAnalyzer 실패 시 단순 fallback
                adj_lines = "\n".join(
                    f"  {a.param}: {a.old_val}→{a.new_val}"
                    for a in result.adjustments
                ) or "  (변경 없음)"
                msg = (
                    f"[피드백] {result.date} 장 마감 분석\n"
                    f"손익: {pnl_str} | 체결 {result.total_trades}건\n"
                    f"파라미터 조정 {len(result.adjustments)}개:\n{adj_lines}"
                )
            try:
                _tg.send(msg)
            except Exception:
                pass

    def _check_overnight_gap(self) -> None:
        """
        EOD 포지션 익일 09:00 갭 체크.

        - 갭 상승 ≥ eod_gap_up_exit_pct(+2%): 즉시 시장가 익절
        - 갭 하락 ≤ eod_gap_down_exit_pct(-1.5%): 즉시 시장가 손절
        - 보합: overnight_held = True 로 전환 → 트레일 스탑 / 타임컷으로 관리
        """
        import logging as _log
        _logger = _log.getLogger(__name__)
        _gap_up  = float(getattr(self._scan_cfg, "eod_gap_up_exit_pct",   2.0))
        _gap_dn  = float(getattr(self._scan_cfg, "eod_gap_down_exit_pct", -1.5))

        eod_positions = [
            (code, pos) for code, pos in list(self.order_mgr.positions.items())
            if getattr(pos, "eod_trade", False)
        ]
        if not eod_positions:
            return

        self.log_panel.append(f"🌅 [EOD갭체크] {len(eod_positions)}개 오버나잇 포지션 갭 확인...")

        for code, pos in eod_positions:
            if pos.avg_price <= 0:
                continue
            chg = float(pos.price_change_pct_vs_avg)

            if chg >= _gap_up:
                # 갭 상승 → 즉시 익절
                self.log_panel.append(
                    f"🟢 [EOD갭익절] {pos.name}({code}) 갭 상승 {chg:+.2f}% ≥ {_gap_up:.1f}% — "
                    f"{pos.qty}주 즉시 시장가 매도"
                )
                self._audit.log_sell_decision(
                    code, f"EOD 갭익절 {chg:+.2f}% (기준 +{_gap_up:.1f}%)", pos.current_price
                )
                self.order_mgr.force_exit(code, pos.name, pos.qty, reason=f"EOD 갭익절 {chg:+.2f}%")
                _logger.info("[EOD갭익절] %s(%s) %+.2f%%", pos.name, code, chg)

            elif chg <= _gap_dn:
                # 갭 하락 → 즉시 손절
                self.log_panel.append(
                    f"🔴 [EOD갭손절] {pos.name}({code}) 갭 하락 {chg:+.2f}% ≤ {_gap_dn:.1f}% — "
                    f"{pos.qty}주 즉시 시장가 매도"
                )
                self._audit.log_sell_decision(
                    code, f"EOD 갭손절 {chg:+.2f}% (기준 {_gap_dn:.1f}%)", pos.current_price
                )
                self.order_mgr.mark_stop_loss(code)
                self.order_mgr.force_exit(code, pos.name, pos.qty, reason=f"EOD 갭손절 {chg:+.2f}%")
                _logger.info("[EOD갭손절] %s(%s) %+.2f%%", pos.name, code, chg)

            else:
                # 보합 → overnight_held = True 전환, 이후 _auto_sell_by_pnl 에서 트레일 스탑으로 관리
                pos.overnight_held = True
                self.log_panel.append(
                    f"⏳ [EOD보합] {pos.name}({code}) 갭 {chg:+.2f}% — "
                    f"트레일 스탑 모드로 전환 (타임컷 09:30)"
                )
                _logger.info("[EOD보합] %s(%s) %+.2f%% → 트레일 스탑 관리", pos.name, code, chg)

    def _check_overnight_timecut(self) -> None:
        """
        EOD 포지션 익일 09:30 타임컷.
        overnight_held = True 이고 수익률 eod_timecut_min_pct 미달이면 강제 청산.
        """
        import logging as _log
        _logger = _log.getLogger(__name__)
        _min_pct = float(getattr(self._scan_cfg, "eod_timecut_min_pct", 1.0))

        for code, pos in list(self.order_mgr.positions.items()):
            if not getattr(pos, "overnight_held", False):
                continue
            chg = float(pos.price_change_pct_vs_avg)
            if chg < _min_pct:
                self.log_panel.append(
                    f"⏱️ [EOD타임컷] {pos.name}({code}) 09:30 수익 {chg:+.2f}% < {_min_pct:.1f}% — "
                    f"{pos.qty}주 강제 청산"
                )
                self._audit.log_sell_decision(
                    code, f"EOD 타임컷 09:30 수익 {chg:+.2f}% (기준 {_min_pct:.1f}%)", pos.current_price
                )
                self.order_mgr.force_exit(code, pos.name, pos.qty,
                                          reason=f"EOD 타임컷 09:30 ({chg:+.2f}%)")
                _logger.info("[EOD타임컷] %s(%s) %+.2f%%", pos.name, code, chg)

    def _reload_adaptive_config(self) -> None:
        """
        adaptive_params.json 을 다시 읽어 _scan_cfg 를 갱신한다.
        매일 08:00 자동 시작 시 호출 — 전날 FeedbackEngine 이 조정한 값을
        재시작 없이 당일에 바로 적용하기 위함.

        config.py 의 RISK 오버라이드는 항상 adaptive 값 위에 덮어씀 (우선순위 보장).
        """
        try:
            from config import RISK as _RISK, STRATEGY as _STRAT
            from scanner.smart_scanner import SmartScannerConfig

            new_cfg = SmartScannerConfig.from_adaptive("config/adaptive_params.json")

            # config.py 고정 오버라이드 재적용 (adaptive 값이 이 값을 덮어쓰면 안 됨)
            new_cfg.max_change_pct     = float(_RISK.get("max_change_pct", 15.0))
            new_cfg.signal_cooldown_sec = float(_RISK.get("signal_cooldown_sec", 45.0))
            new_cfg.index_block_pct    = float(_RISK.get("market_index_block_pct", -1.5))

            _yosep = str(_STRAT.get("yosep_preset", "") or "").strip().lower()
            if _yosep:
                new_cfg.apply_yosep_preset(_yosep)

            _wpm = _STRAT.get("watch_pool_max")
            if _wpm is not None:
                wpm = max(1, int(_wpm))
                new_cfg.watch_pool_max   = wpm
                new_cfg.realtime_sub_max = wpm
                new_cfg.display_top_n    = wpm

            # 공유 참조 갱신: ScannerWorker 와 SmartScanner 가 _scan_cfg 를 직접 참조하므로
            # 기존 객체의 필드를 in-place 로 업데이트 (객체 교체가 아닌 속성 복사)
            for field_name, new_val in vars(new_cfg).items():
                try:
                    setattr(self._scan_cfg, field_name, new_val)
                except Exception:
                    pass

            logger.info("[AdaptiveReload] config/adaptive_params.json 리로드 완료")
            self.log_panel.append("⚙️ [적응형파라미터] 어제 피드백 조정값 적용됨")

        except Exception as _e:
            logger.warning("[AdaptiveReload] 리로드 실패: %s", _e)

    def _liquidate_phase1_positions(self, forced: bool = False) -> None:
        """
        Phase 1 (OPENING_SCALP) 포지션 관리:
        - forced=True  (10:30): 트레일 여부 무관 전량 강제 청산
        - forced=False (10:31~): 고점 대비 phase1_trail_drop_pct% 하락 시 청산
        """
        import logging as _log_module
        _logger = _log_module.getLogger(__name__)
        _trail_drop = float(getattr(self._scan_cfg, "phase1_trail_drop_pct", 1.0))

        for code, pos in list(self.order_mgr.positions.items()):
            if getattr(pos, "entry_phase", 0) != 1:
                continue
            if pos.qty <= 0:
                continue

            if forced:
                # 10:30 강제청산 — Phase 1 포지션 전량 매도
                self.order_mgr.force_exit(code, pos.name, pos.qty,
                                          reason="Phase1 10:30 강제청산")
                self.log_panel.append(
                    f"⏱ [Phase1강제청산] {pos.name}({code}) {pos.qty}주 — 10:30 타임컷"
                )
                _logger.info("[Phase1강제청산] %s(%s) %d주 시장가 매도", pos.name, code, pos.qty)
            else:
                # 트레일 — 고점 대비 drop% 이상 하락 시 청산
                if pos.peak_price <= 0 or pos.current_price <= 0:
                    continue
                drop_pct = (pos.peak_price - pos.current_price) / pos.peak_price * 100
                if drop_pct >= _trail_drop:
                    self.order_mgr.force_exit(code, pos.name, pos.qty,
                                              reason=f"Phase1 trail -{_trail_drop:.1f}%")
                    self.log_panel.append(
                        f"📉 [Phase1트레일] {pos.name}({code}) 고점 {pos.peak_price:,} → "
                        f"현재 {pos.current_price:,} (-{drop_pct:.1f}%) 청산"
                    )
                    _logger.info("[Phase1트레일] %s(%s) 고점 %.0f → 현재 %.0f (-%.1f%%) 청산",
                                 pos.name, code, pos.peak_price, pos.current_price, drop_pct)

    def _liquidate_all_positions(self) -> None:
        """오늘 이 앱에서 매수한 수량만 강제 청산 (장 종료 1분 전 15:19). 기존 보유·HTS 매수분은 제외."""
        from datetime import date as _date
        import logging as _log
        _logger = _log.getLogger(__name__)

        # 이전 청산이 아직 진행 중이면 스킵 (블로킹 방지)
        if getattr(self, '_liquidate_in_progress', False):
            _logger.warning("[_liquidate_all_positions] 이전 청산 진행 중 — 스킵")
            return

        self._liquidate_in_progress = True
        try:
            positions = list(self.order_mgr.positions.items())

            if not positions:
                self.log_panel.append("💤 보유 포지션 없음 — 청산 생략")
                return

            targets = []
            for code, pos in positions:
                # 종가매매(EOD) 포지션은 당일 강제청산 제외 — 익일 갭 체크로 관리
                if getattr(pos, "eod_trade", False):
                    self.log_panel.append(
                        f"🌙 [EOD유지] {pos.name}({code}) — 종가매매 포지션, 당일 청산 제외"
                    )
                    continue
                q = getattr(pos, "qty_buy_today_app", 0) or 0
                # qty_buy_today_app가 0이더라도 opened_by_app이면 청산 대상
                if q <= 0 and not getattr(pos, "opened_by_app", False):
                    continue
                sell_qty = min(pos.qty, q) if q > 0 else pos.qty
                if sell_qty > 0:
                    targets.append((code, pos, sell_qty))

            if not targets:
                self.log_panel.append(
                    "💤 앱 매수 포지션 없음 — 자동청산 생략 (기존 보유·HTS 매수분 유지)"
                )
                return

            self.log_panel.append(
                f"🔴 [자동청산 시작] 오늘 앱 매수 {len(targets)}종목만 청산 (기준일 {_date.today().isoformat()})..."
            )

            for code, pos, sell_qty in targets:
                try:
                    self._audit.log_sell_decision(
                        code, "Day Close 15:19 강제청산", pos.current_price,
                    )
                    self.order_mgr.sell(code, pos.name, sell_qty, price=0)
                    self.log_panel.append(
                        f"  └─ {pos.name}({code}) {sell_qty}주 시장가 매도 주문 "
                        f"(보유 {pos.qty}주 중 오늘 앱 매수분)"
                    )
                except Exception as e:
                    self.log_panel.append(
                        f"  ⚠️ {pos.name}({code}) 청산 실패: {e}"
                    )
                    _logger.exception(f"[청산 실패] {code}")

            self.log_panel.append("🔴 [자동청산] 청산 명령 전송 — 미체결 시 다음 분에 재확인")
        except Exception as e:
            self.log_panel.append(f"🔴 [자동청산 오류] {e}")
            _logger.exception("[_liquidate_all_positions] 예외")
        finally:
            self._liquidate_in_progress = False

    def _check_ema_exit_positions(self) -> None:
        """
        [추세추종] 1분봉 EMA(20) 하향 돌파 청산 — 매 분 호출.

        보유 포지션의 현재가가 1분봉 EMA(20) 아래로 내려가면 추세 소멸로 판단해
        시장가 청산한다. Phase1(opening_scalp) 및 EOD 포지션은 각자 별도 로직으로
        관리하므로 여기서는 제외.

        조건:
          - opened_by_app=True (앱 매수 포지션)
          - entry_phase != 1 (Phase1은 별도 트레일로 관리)
          - eod_trade=False (EOD 포지션 제외)
          - closes_1min 길이 ≥ 20
          - current_price < EMA(20) of closes_1min
        """
        import logging as _log
        _logger = _log.getLogger(__name__)
        from strategy.jang_dong_min import calc_ema as _calc_ema

        for code, pos in list(self.order_mgr.positions.items()):
            # Phase1·EOD·비앱 포지션 제외
            if not getattr(pos, "opened_by_app", False):
                continue
            if getattr(pos, "entry_phase", 0) == 1:
                continue
            if getattr(pos, "eod_trade", False):
                continue

            snap = self._snap_store.get_snapshot(code)
            if snap is None:
                continue

            closes = list(getattr(snap, "closes_1min", []) or [])
            if len(closes) < 20:
                continue

            ema20 = _calc_ema(closes, 20)
            if ema20 is None or ema20 <= 0:
                continue

            cur = pos.current_price or snap.current_price
            if cur <= 0:
                continue

            if cur < ema20:
                msg = (
                    f"📉 [EMA청산] {pos.name}({code}) "
                    f"현재가 {cur:,} < EMA20 {ema20:,.0f} — 추세 소멸 청산"
                )
                self.log_panel.append(msg)
                _logger.info("[EMA청산] %s(%s) cur=%s ema20=%.0f", pos.name, code, cur, ema20)
                try:
                    self._audit.log_sell_decision(code, "EMA20 하향 돌파 — 추세 소멸", cur)
                    self.order_mgr.sell(code, pos.name, pos.qty, price=0)
                except Exception as e:
                    self.log_panel.append(f"  ⚠️ {pos.name}({code}) EMA 청산 실패: {e}")
                    _logger.exception("[EMA청산 실패] %s", code)

    @pyqtSlot(bool)
    def _on_auto_trade_toggle(self, enabled: bool) -> None:
        import logging as _log
        _log.getLogger(__name__).info("[자동매매] 토글 수신: enabled=%s", enabled)
        self._auto_trading = enabled
        # 수동으로 자동매매를 다시 켜면 급락 감지 플래그 리셋 (재활성화 허용)
        if enabled:
            self._market_crash_off = False
        state = "시작" if enabled else "정지"
        self.log_panel.append(f"{'🟢' if enabled else '🔴'} 자동매매 {state}")
        self.log_panel.append(
            f"[상태] auto_trading={self._auto_trading} "
            f"SnapshotStore={len(self._snap_store)}종목"
        )

    @pyqtSlot(bool)
    def _on_overnight_mode_toggle(self, enabled: bool) -> None:
        """야간보유 모드 토글 — SmartScannerConfig.overnight_mode_enabled 실시간 반영"""
        self._scan_cfg.overnight_mode_enabled = enabled
        state = "ON" if enabled else "OFF"
        icon  = "🌙" if enabled else "☀️"
        self.log_panel.append(
            f"{icon} 야간보유 모드 {state} "
            f"({'14:40~14:55 EOD 신호 활성화 / 당일 청산 제외' if enabled else '종가매매 신호 비활성화'})"
        )
        if enabled:
            self.log_panel.append(
                "  └─ 갭 상승 +2% 즉시 익절 / 갭 하락 -1.5% 즉시 손절 / 09:30 타임컷 적용"
            )
        logger.info("[overnight_mode] %s", state)

    @pyqtSlot()
    def _on_switch_real_requested(self) -> None:
        import os
        from PyQt5.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, '실전투자 전환', '실전투자로 전환하려면 프로그램을 재시작해야 합니다.\n기존 설정 캐시를 삭제하고 즉시 재시작하시겠습니까?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            cache_file = "config/login_cache.json"
            if os.path.exists(cache_file):
                os.remove(cache_file)
            self.header._on_restart_clicked()

    def _on_manual_unlock_requested(self) -> None:
        """
        사용자 수동 개입으로 일일 손익 락을 해제한다 (RiskManager 연동).
        """
        prev_locked = self.risk_manager.is_new_entry_locked
        prev_cut_done = self.risk_manager.is_daily_loss_cut_done

        # RiskManager 상태 업데이트
        self.risk_manager.unlock_entry_manual()

        # MainWindow 플래그도 동기화 (로그 중복 방지용)
        self._new_entry_locked = False
        self._daily_loss_cut_done = False
        self._manual_unlock_active = True

        self.log_panel.append(
            "🔓 [수동해제] 일일 손익 락 해제 완료 — 금일 자동 재락 일시 중단"
        )
        logger.warning(
            "[PnlLock] 수동 해제: locked %s→False, loss_cut_done %s→False, manual_unlock_active=True",
            prev_locked, prev_cut_done
        )

    @pyqtSlot(float)
    def _on_tp_changed(self, value: float) -> None:
        self._auto_tp_pct = value
        self.log_panel.append(f"[리스크] 익절 기준 변경 → +{value:.1f}%")

    @pyqtSlot(float)
    def _on_sl_changed(self, value: float) -> None:
        self._auto_sl_pct = value
        self.log_panel.append(f"[리스크] 손절 기준 변경 → {value:.1f}%")

    @pyqtSlot(str, str, int)
    def _on_health_param_relax(self, params: dict) -> None:
        """
        HealthMonitor 데몬 스레드에서 호출될 수 있으므로
        Qt 위젯 접근은 QMetaObject.invokeMethod(QueuedConnection)으로 메인 스레드에 위임.
        실제 setattr은 health_monitor._check_drought()에서 이미 완료됨.
        """
        msg = "  ".join(f"{k}={v}" for k, v in params.items())
        logger.info("[HealthMonitor] 파라미터 완화 적용: %s", msg)
        # 메시지를 리스트에 먼저 추가한 뒤 invokeMethod로 메인스레드 슬롯 예약
        if not hasattr(self, "_health_relax_msgs"):
            self._health_relax_msgs: list = []
        self._health_relax_msgs.append(msg)
        from PyQt5.QtCore import QMetaObject, Qt as _Qt
        QMetaObject.invokeMethod(self, "_health_relax_ui", _Qt.QueuedConnection)

    @pyqtSlot()
    def _health_relax_ui(self) -> None:
        """메인 스레드에서만 실행 — UI 위젯 접근 안전."""
        msgs = getattr(self, "_health_relax_msgs", [])
        while msgs:
            m = msgs.pop(0)
            self.log_panel.append(f"🔧 [가뭄완화] 파라미터 자동 완화: {m}")

    def _on_manual_sell(self, code: str, name: str, qty: int) -> None:
        """보유현황 수동 매도 버튼 처리."""
        pos = self.order_mgr.positions.get(code)
        if pos is None:
            self.log_panel.append(f"⚠ 수동매도 오류 — {name}({code}) 포지션 없음")
            return
        if qty <= 0 or qty > pos.qty:
            self.log_panel.append(
                f"⚠ 수동매도 오류 — 수량 {qty}주 (보유 {pos.qty}주)"
            )
            return
        self.log_panel.append(f"[수동매도] {name}({code}) {qty}주 시장가 요청")
        self._audit.log_sell_decision(code, "수동매도", pos.current_price)
        self.order_mgr.sell(code, name, qty, price=0)
        # 주문 보낸 후 포트폴리오 즉시 업데이트
        self._on_portfolio_refresh({
            "cash":      self.order_mgr.cash,
            "positions": dict(self.order_mgr.positions),
        })

    @pyqtSlot(str, str, int)
    def _on_manual_buy(self, code: str, name: str, price: int) -> None:
        """스캐너 수동 매수 버튼 처리 — 다이얼로그 → order_mgr.buy()."""
        dlg = ManualBuyDialog(code, name, price, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return

        qty, otype, oprice = dlg.result_values()

        # 포지션 한도 초과 경고 (차단은 하지 않음 — 수동이므로 사용자 판단 우선)
        n_pos = len(self.order_mgr.positions)
        max_pos = getattr(self.order_mgr, "max_positions", 5)
        if n_pos >= max_pos:
            self.log_panel.append(
                f"⚠ [수동매수] 포지션 {n_pos}/{max_pos}개 한도 초과 상태로 주문합니다"
            )

        # buy(price=0) → 시장가, buy(price>0) → 지정가
        order_label = "시장가" if otype == "03" else f"지정가 {oprice:,}원"
        self.log_panel.append(f"[수동매수] {name}({code}) {qty}주 {order_label} 요청")
        self.order_mgr.buy(code, name, qty, price=oprice)

    @pyqtSlot()
    def _run_scanner_scan(self) -> None:
        """
        메인 스레드에서 1분마다 호출 (QTimer) — 실제 스캔은 백그라운드 스레드에서 실행.

        메인 스레드 블로킹 방지:
        - run_periodic_scan()을 별도 스레드에서 비동기 실행
        - 진행 상황은 QTimer.singleShot으로 메인 스레드에서 UI 업데이트
        - 완료 후 일봉 갱신을 QTimer 체인으로 처리
        """
        import logging as _log
        from config import STRATEGY as _STR
        _logger = _log.getLogger(__name__)

        # 이전 스캔이 아직 진행 중이면 스킵 (블로킹 방지)
        if getattr(self, '_scan_in_progress', False):
            _logger.debug("[스캔] 이전 스캔 진행 중 — 스킵")
            return

        # [NEW] balance TR 처리 중이면 3초 후 재시도 — scan/balance 충돌 방지 (2026-04-27)
        if getattr(self._kiwoom, '_tr_busy', False):
            _logger.info("[스캔] TR 처리 중 (%s) — 3s 후 재시도",
                        getattr(self._kiwoom, '_tr_current_rq', '?'))
            QTimer.singleShot(3_000, self._run_scanner_scan)
            return

        self._scan_in_progress = True
        self.log_panel.append(f"[스캔] 주기 스캔 시작 — {datetime.now():%H:%M:%S}")
        self.scan_status.reset()

        try:
            # 스캔 시작 전 ACK — run_periodic_scan이 수십 초 걸릴 수 있어 미리 리셋
            _hm = getattr(self, "_health_monitor", None)
            if _hm:
                _hm.ack()

            self._smart_scanner.run_periodic_scan(on_progress=None)

            # 스캔 완료 후 ACK — 스캔이 길었더라도 즉시 Watchdog 안심
            if _hm:
                _hm.ack()

            # 스캔 완료 요약 메시지 (진행 상황은 신호 발생 시에만 표시)
            total_watched = len(self._snap_store)
            self.log_panel.append(f"[스캔] 완료 — 전체 {total_watched}종목 모니터링")
            self.scan_status.done(f"완료 / {total_watched}종목")

            # 일봉 갱신 대기 목록 처리 — QTimer 체인 (최대 10개, 각 350ms 간격)
            _pending = list(getattr(self._smart_scanner, "_daily_refresh_pending", []))[:10]
            if _pending:
                self._smart_scanner._daily_refresh_pending = []
                QTimer.singleShot(500, lambda codes=_pending: self._daily_candle_chain(codes, 0))
        except Exception as e:
            _logger.exception("[스캔] run_periodic_scan 오류")
            self.log_panel.append(f"[스캔 오류] {e}")
            self.scan_status.done(f"오류: {e}")
        finally:
            self._scan_in_progress = False

    def _daily_candle_chain(self, codes: list, idx: int) -> None:
        """
        일봉 데이터를 QTimer 체인으로 1종목씩 비동기 갱신.

        350ms 간격으로 한 번에 1 TR 호출 → TRRateLimiter가 sleep 불필요
        (0.35s > 0.25s MIN_INTERVAL), UI 이벤트 루프 완전 자유.
        """
        import logging as _log
        _logger = _log.getLogger(__name__)

        if idx >= len(codes):
            _logger.info("[일봉갱신] 완료 — %d종목 처리", len(codes))
            return

        # _tr_busy 중이면 이 종목 스킵 후 다음으로 (무한 재시도 방지)
        if getattr(self._kiwoom, "_tr_busy", False):
            _logger.debug("[일봉갱신] TR 사용 중 — %s 스킵 후 다음 종목", codes[idx])
            QTimer.singleShot(350, lambda: self._daily_candle_chain(codes, idx + 1))
            return

        # 일봉 갱신 중 watchdog ACK 명시적 전송 (10종목 × 최대 1s = 최대 10s, 12s 임계값 방어)
        _hm = getattr(self, "_health_monitor", None)
        if _hm:
            _hm.ack()

        code = codes[idx]
        try:
            candles = self._kiwoom.get_daily_candles(code, count=120)
            if candles:
                self._snap_store.set_daily_candles(code, candles)
                _logger.debug("[일봉갱신] %s 완료 (%d개)", code, len(candles))
        except Exception as e:
            _logger.warning("[일봉갱신] %s 실패: %s", code, e)

        # 다음 종목: 350ms 후 처리 (TRRateLimiter 0.25s 간격 자동 충족 → sleep=0)
        QTimer.singleShot(350, lambda: self._daily_candle_chain(codes, idx + 1))

    def _on_scan_signal_direct(self, sig) -> None:
        """SmartScanner.on_signal 콜백 — 메인 스레드에서 직접 호출됨"""
        self._on_scan_signal(sig)

    @pyqtSlot(object)
    def _on_scan_signal(self, sig) -> None:
        """신호 수신 처리 (로그 + 필터링 + 진입)"""
        self.log_panel.append(
            f"🚨 [{sig.signal_type}] {sig.name}({sig.code}) "
            f"@{sig.price:,}원  {sig.reason}"
        )

        # 당일 감시 목록에 누적 (포트폴리오 패널 "감시중" 표시용)
        first_signal = len(self._today_watch) == 0
        self._today_watch[sig.code] = {
            "name":        sig.name,
            "price":       sig.price,
            "signal_type": sig.signal_type,
        }

        # 첫 감시 종목 발생 시 자동매매 자동 시작 (급락 감지로 OFF된 상태이면 차단)
        if first_signal and not self._auto_trading and not self._market_crash_off:
            self.header._btn_auto.setChecked(True)
            self.header._on_auto_clicked(True)
            self.log_panel.append("🟢 감시 종목 발생 — 자동매매 자동 시작")

        # 뉴스 분석 요청 (백그라운드, 즉시 반환)
        self._news_analyzer.analyze(sig.code, sig.name)

        # Phase 1 태깅 (OPENING_SCALP 포지션 한도는 여기서만 체크)
        if sig.signal_type == "OPENING_SCALP":
            sig.entry_phase = 1
            _ph1_max = int(getattr(self._scan_cfg, "phase1_max_positions", 3))
            _ph1_count = sum(1 for p in self.order_mgr.positions.values()
                             if getattr(p, "entry_phase", 0) == 1)
            if _ph1_count >= _ph1_max:
                self.log_panel.append(
                    f"🔒 [Phase1한도] {sig.name}({sig.code}) 스킵 — "
                    f"Phase1 최대 {_ph1_max}개 도달 (현재 {_ph1_count}개)"
                )
                # HealthMonitor 기록 (필터 통과하지 못했으므로 기록)
                _hm = getattr(self, "_health_monitor", None)
                if _hm is not None:
                    _hm.record_signal(sig.code, sig.name, sig.signal_type)
                return
        else:
            sig.entry_phase = 2

        # Phase 3: TradingController에서 신호 필터링 + 진입
        # (자동매매, 포지션 한도, 손익 락, 섹터, 예수금, 중복 진입 등)
        self.trading_controller.set_auto_trading(self._auto_trading)
        if self.trading_controller.handle_signal(sig):
            # 필터 통과 → order_mgr에서 처리 (TradingController에서 호출함)
            pass

        # HealthMonitor에 신호 기록 (매매 여부와 무관하게 신호 자체를 기록)
        _hm = getattr(self, "_health_monitor", None)
        if _hm is not None:
            _hm.record_signal(sig.code, sig.name, sig.signal_type)

    # ────────────────────────────────────────────────────────────────────────
    # Phase 3: Application Layer 신호 처리 슬롯
    # ────────────────────────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_market_opened(self) -> None:
        """08:00 장개시 신호 처리"""
        if self._opened_today:
            return
        self._opened_today = True
        self._reload_adaptive_config()
        self.header._btn_auto.setChecked(True)
        self.header._on_auto_clicked(True)
        self.log_panel.append("📈 08:00 자동매매 시작")

    @pyqtSlot()
    def _on_market_closing(self) -> None:
        """15:20 장마감 + 강제청산 신호 처리"""
        if self._closed_today:
            return
        self._closed_today = True

        # 보유 포지션 강제청산 (EOD 포지션 제외)
        if self.order_mgr.positions:
            self.log_panel.append("🔴 [15:20 강제청산] 모든 포지션 전량 매도 (EOD 제외)...")
            for code, pos in list(self.order_mgr.positions.items()):
                if getattr(pos, "eod_trade", False):
                    self.log_panel.append(
                        f"🌙 [EOD유지] {pos.name}({code}) — 익일 관리 포지션, 15:20 청산 제외"
                    )
                    continue
                if pos.qty > 0:
                    self.order_mgr.force_exit(code, pos.name, pos.qty, reason="Day Close 15:20")
                    self.log_panel.append(f"  └─ {pos.name}({code}) {pos.qty}주 매도 주문")

        # 전일 거래량 캐시 저장
        try:
            if hasattr(self, "_smart_scanner") and self._smart_scanner is not None:
                self._smart_scanner.save_prev_volumes()
                logger.info("[15:20] prev_volumes 저장 완료")
        except Exception as _e:
            logger.warning("[15:20] prev_volumes 저장 실패: %s", _e)

        # 자동매매 OFF
        self.header._btn_auto.setChecked(False)
        self.header._on_auto_clicked(False)
        self.log_panel.append("📉 시장 종료 — 자동매매 중지")

    @pyqtSlot()
    def _on_profit_locked(self) -> None:
        """수익 목표 달성 → 신규 매수 차단"""
        self._new_entry_locked = True
        profit_target = self._scan_cfg.daily_profit_lock_won
        self.log_panel.append(
            f"🔒 [수익 락] 일일 수익 {profit_target:,}원 달성 — 신규 매수 차단"
        )

    @pyqtSlot()
    def _on_loss_cut(self) -> None:
        """손절 한도 도달 → 전량 청산"""
        self._daily_loss_cut_done = True
        loss_limit = self._scan_cfg.daily_loss_cut_won
        self.log_panel.append(
            f"🔴 [손절 한도] 일일 손실 -{loss_limit:,}원 도달 — 전량 강제청산 진행..."
        )
        # 모든 보유 포지션 강제청산
        if self.order_mgr.positions:
            for code, pos in list(self.order_mgr.positions.items()):
                if pos.qty > 0:
                    self.order_mgr.force_exit(code, pos.name, pos.qty, reason="Daily Loss Cut")
                    self.log_panel.append(f"  └─ {pos.name}({code}) {pos.qty}주 매도 주문")

    @pyqtSlot()
    def _on_day_reset(self) -> None:
        """자정 플래그 리셋"""
        self._opened_today = False
        self._closed_today = False
        self._feedback_done_today = False
        self._new_entry_locked = False
        self._daily_loss_cut_done = False
        self._manual_unlock_active = False
        self.risk_manager.reset()
        logger.info("[자정] 당일 플래그 리셋")

    @pyqtSlot()
    def _on_feedback_triggered(self) -> None:
        """15:35 피드백 루프 신호"""
        if self._feedback_done_today:
            return
        self._feedback_done_today = True
        self._run_feedback_loop()

    def _drain_news_queue(self) -> None:
        """
        뉴스 분석 결과를 메인 스레드에서 안전하게 처리.

        NewsAnalyzer 백그라운드 스레드가 결과를 Queue에 넣으면
        이 메서드(QTimer 1초 주기)가 꺼내 로그에 표시한다.
        Qt 위젯 접근은 항상 메인 스레드에서만 이뤄진다.
        """
        try:
            while not self._news_queue.empty():
                result = self._news_queue.get_nowait()
                self.log_panel.append(result.summary())
        except Exception:
            pass

    @pyqtSlot(dict)
    def _on_portfolio_refresh(self, data: dict) -> None:
        """watch_today를 주입하고, 헤더(당일 실현손익)/보유현황을 함께 갱신한다."""
        self.header.set_pnl(self.order_mgr.daily_realized_pnl)
        # 보유 여유분(max_positions - 현재 보유수)만큼만 감시중 표시
        positions = data.get("positions", {})
        slack = max(0, self.order_mgr.max_positions - len(positions))
        # 보유 종목은 포함, 미보유 감시는 slack개로 제한 (최근 신호 우선)
        watch: dict = {}
        non_pos = {c: v for c, v in self._today_watch.items() if c not in positions}
        recent_non_pos = dict(list(non_pos.items())[-slack:]) if slack else {}
        watch.update({c: v for c, v in self._today_watch.items() if c in positions})
        watch.update(recent_non_pos)
        data["watch_today"] = watch
        self.portfolio_panel.refresh(data)

    @pyqtSlot(str)
    def _on_code_selected(self, code: str) -> None:
        self._selected_code = code
        self._refresh_chart()

    @pyqtSlot()
    def _on_tg_status_requested(self) -> None:
        """텔레그램 /status 명령 수신 시 현재 상태 전송."""
        if not self._tg:
            return
        lines = [
            ("🟢 자동매매 ON" if self._auto_trading else "🔴 자동매매 OFF"),
            f"예수금: {self.order_mgr.cash:,}원",
            f"당일 실현손익: {self.order_mgr.daily_realized_pnl:+,}원",
            "",
        ]
        for pos in self.order_mgr.positions.values():
            lines.append(
                f"  {pos.name} {pos.qty}주 "
                f"매수가대비 {pos.price_change_pct_vs_avg:+.2f}% (평가손익 {pos.pnl:+,}원)"
            )
        if not self.order_mgr.positions:
            lines.append("  (보유 없음)")
        self._tg.send("\n".join(lines))

    def _send_tg_status(self) -> None:
        """1시간 주기 텔레그램 자동 보고 — 자동매매 ON일 때, 08:00~15:30 에만."""
        from datetime import datetime, time
        now_time = datetime.now().time()
        if time(8, 0) <= now_time <= time(15, 30):
            if self._auto_trading and self._tg:
                self._on_tg_status_requested()

    def _refresh_chart(self) -> None:
        if not self._selected_code:
            return
        snap = self._snap_store.get_snapshot(self._selected_code)
        if snap is None:
            return
        closes  = snap.closes_1min or [snap.current_price]
        volumes = list(snap.volumes_1min) if snap.volumes_1min else []
        pos     = self.order_mgr.positions.get(self._selected_code)

        # [Trail] 트레일 가격 계산 — 포지션 보유 중이고 peak가 활성화된 경우만
        trail_price = 0
        if pos and pos.peak_price > 0 and pos.avg_price > 0:
            peak_chg = (pos.peak_price - pos.avg_price) / pos.avg_price * 100
            cfg = self._scan_cfg
            if peak_chg >= cfg.trail_activation_pct:
                if peak_chg < cfg.trail_tier1_max:
                    _tp = cfg.trail_pct_tier1
                elif peak_chg < cfg.trail_tier2_max:
                    _tp = cfg.trail_pct_tier2
                else:
                    _tp = cfg.trail_pct_tier3
                trail_price = int(pos.peak_price * (1 - _tp / 100))

        self.chart_panel.update_chart(
            closes, volumes, snap.code, snap.name,
            position=pos,
            trail_price=trail_price,
            sl_pct=self._auto_sl_pct,
        )

    def _refresh_portfolio_prices(self) -> None:
        """보유 종목 현재가를 실시간 스냅샷 우선으로 갱신하고 패널을 업데이트한다."""
        positions = self.order_mgr.positions
        if not positions:
            return
        try:
            for pos in positions.values():
                # 실시간 체결로 누적되는 SnapshotStore 가격을 우선 사용한다.
                # (GetMasterLastPrice는 장중 실시간성과 정확도가 떨어질 수 있음)
                price = 0
                snap = self._snap_store.get_snapshot(pos.code)
                if snap and snap.current_price > 0:
                    price = snap.current_price
                    src = "snapshot"
                else:
                    # [NEW] 스냅샷 미포함 종목 → 강제 갱신 (2026-04-04)
                    price = self._kiwoom.get_current_price(pos.code)
                    src = "master_last (강제갱신)"
                if price > 0 and pos.current_price != price:
                    # 현재가가 변경되었을 때만 로그 출력
                    import logging as _lg
                    _lg.getLogger(__name__).debug(
                        "현재가갱신 — %s(%s) price=%d(avg=%d, src=%s)",
                        pos.name, pos.code, price, pos.avg_price, src
                    )
                    pos.current_price = price
                # [Trail] peak_price 갱신 — 현재가가 avg 대비 trail_activation_pct 이상일 때부터 추적
                if price > 0 and pos.avg_price > 0:
                    activation = pos.avg_price * (1 + self._scan_cfg.trail_activation_pct / 100)
                    if price >= activation and price > pos.peak_price:
                        pos.peak_price = price
        except Exception:
            return
        self._on_portfolio_refresh({
            "cash":      self.order_mgr.cash,
            "positions": dict(positions),
        })
        # Phase 3: TradingController에서 청산 판정 (기존 _auto_sell_by_pnl 대체)
        self.trading_controller.check_and_exit_all()
        self.order_mgr._check_failed_sells()   # 매도 미체결 재시도
        self.order_mgr._check_pending_buys()   # [P2] 매수 미체결 10초 취소

    def _on_investor_refresh_tick(self) -> None:
        """
        수급 갱신 타이머 콜백.

        - investor_refresh_min 변수로 갱신 주기 조정 가능 (기본 10분).
          adaptive_params.json 에서 변경하면 다음 틱에 타이머 간격이 자동으로 재설정됨.
        - 정규장 시간(09:00~15:30) + 자동매매 ON 상태에서만 TR 호출.
        """
        # ── 갱신 주기가 변경됐으면 타이머 재시작 ──────────────────────────
        _want_ms = int(self._scan_cfg.investor_refresh_min * 60 * 1000)
        if self._investor_timer.interval() != _want_ms:
            self._investor_timer.setInterval(_want_ms)
            logger.info(
                "[수급필터] 갱신 주기 변경 → %d분", self._scan_cfg.investor_refresh_min
            )

        if not self._scan_cfg.investor_filter_enabled:
            return
        scanner = getattr(self, "_smart_scanner", None)
        if scanner is None:
            return
        # 정규장 시간(09:00~15:30)에만 조회 — 자동매매 ON/OFF 무관 (표시용)
        from datetime import datetime, time
        now = datetime.now().time()
        if not (time(9, 0) <= now <= time(15, 30)):
            return
        # 다른 TR 진행 중이면 5초 후 재시도 — 단순 return 시 다음 10분 틱까지 대기하게 됨
        if getattr(self._kiwoom, "_tr_busy", False):
            QTimer.singleShot(5_000, self._on_investor_refresh_tick)
            return
        scanner.trigger_investor_refresh()

    @pyqtSlot()
    def _cleanup_memory(self) -> None:
        """[P2] 1시간마다 오래된 dict 키를 삭제해 메모리 누수를 방지한다."""
        import time as _time
        from datetime import datetime as _dt

        now_mono = _time.monotonic()
        now_dt   = _dt.now()
        active_codes: set[str] = set(self.order_mgr.positions.keys())

        cleaned = 0

        # ── ScannerWorker 내부 dict 정리 ────────────────────────────────────
        worker = getattr(self, "_scan_worker", None)
        if worker:
            # BREAKOUT 대기 — 60분 이상 경과한 항목 제거
            stale_bp = [
                c for c, v in list(worker._breakout_pending.items())
                if (now_mono - v.get("first_time", now_mono)) > 3600
            ]
            for c in stale_bp:
                worker._breakout_pending.pop(c, None)
                cleaned += 1

            # 신호 쿨다운 — 보유 중이 아닌 & 마지막 emit 2시간 초과 항목 제거
            stale_emit = [
                c for c, t in list(worker._signal_last_emit_mono.items())
                if c not in active_codes and (now_mono - t) > 7200
            ]
            for c in stale_emit:
                worker._signal_last_emit_mono.pop(c, None)
                worker._signal_prev_active.pop(c, None)
                cleaned += 1

        # ── OrderManager 내부 dict 정리 ─────────────────────────────────────
        om = self.order_mgr

        # 당일 신호 쿨다운 — 보유 중이 아닌 코드 제거 (매일 초기화되지 않으므로 수동 정리)
        stale_sig = [
            c for c, t in list(om._signal_last_time.items())
            if c not in active_codes and (now_mono - t) > 7200
        ]
        for c in stale_sig:
            om._signal_last_time.pop(c, None)
            cleaned += 1

        # 과거 주문 레코드 — 1000건 초과 시 오래된 것부터 삭제 (당일 체결만 보존)
        if len(om.orders) > 1000:
            sorted_keys = sorted(
                om.orders.keys(),
                key=lambda k: om.orders[k].ordered_at
            )
            to_del = sorted_keys[: len(om.orders) - 500]
            for k in to_del:
                del om.orders[k]
            cleaned += len(to_del)

        # ── 오늘 감시 종목 dict 정리 — 보유 중이 아닌 코드 중 오래된 것 ────
        if len(self._today_watch) > 300:
            keep = set(list(self._today_watch.keys())[-200:]) | active_codes
            removed = [c for c in list(self._today_watch) if c not in keep]
            for c in removed:
                del self._today_watch[c]
            cleaned += len(removed)

        if cleaned:
            logger.info("[메모리정리] %d개 항목 제거 — positions=%d, orders=%d, today_watch=%d",
                        cleaned, len(active_codes), len(om.orders), len(self._today_watch))


# ---------------------------------------------------------------------------
# Deep Dark QSS
# ---------------------------------------------------------------------------

_DARK_QSS = """
/* ─── 베이스 ──────────────────────────────────────────── */
QMainWindow, QWidget {
    background-color: #0d0d14;
    color: #cdd6f4;
    font-family: 'Malgun Gothic';
    font-size: 9pt;
}
QSplitter::handle { background: #1e1e2e; }

/* ─── 헤더 ────────────────────────────────────────────── */
QWidget#header_bar   { background: #1a1a2a; border-bottom: 1px solid #313244; }
QLabel#lbl_title     { color: #89b4fa; }
QFrame#v_divider     { color: #313244; }
QLabel#conn_off      { color: #6c7086; }
QLabel#conn_on       { color: #a6e3a1; }

/* ─── Safety Switch 버튼 ─────────────────────────────── */
QPushButton#btn_auto_off {
    background: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 4px 12px;
}
QPushButton#btn_auto_off:hover { background: #45475a; }
QPushButton#btn_auto_on {
    background: #a6e3a1;
    color: #1e1e2e;
    border: none;
    border-radius: 6px;
    padding: 4px 12px;
    font-weight: bold;
}
QPushButton#btn_auto_on:hover { background: #c3f5be; }

/* ─── 야간보유 모드 버튼 ─────────────────────────────── */
QPushButton#btn_overnight_off {
    background: #313244;
    color: #a6adc8;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 4px 8px;
}
QPushButton#btn_overnight_off:hover { background: #45475a; }
QPushButton#btn_overnight_on {
    background: #313350;
    color: #cba6f7;
    border: 1px solid #7c6fcd;
    border-radius: 6px;
    padding: 4px 8px;
    font-weight: bold;
}
QPushButton#btn_overnight_on:hover { background: #3d3665; }

/* ─── 재시작 버튼 ──────────────────────────────────────── */
QPushButton#btn_restart {
    background: #94e2d5;
    color: #1e1e2e;
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    font-weight: bold;
}
QPushButton#btn_restart:hover { background: #a8eee5; }

/* ─── 종료 버튼 ───────────────────────────────────────── */
QPushButton#btn_exit {
    background: #f38ba8;
    color: #1e1e2e;
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    font-weight: bold;
}
QPushButton#btn_exit:hover { background: #f5a3b8; }

/* ─── 차트 정보 패널 ──────────────────────────────────── */
QWidget#chart_info_panel {
    background: #12121e;
    border-left: 1px solid #313244;
}
QWidget#chart_info_panel QLabel {
    color: #cdd6f4;
    padding: 2px 0;
}

/* ─── 수동매도 버튼 ────────────────────────────────────── */
QPushButton#manual_sell_btn {
    background: #f38ba8;
    color: #1e1e2e;
    border-radius: 3px;
    font-weight: bold;
    padding: 1px 4px;
}
QPushButton#manual_sell_btn:hover { background: #eb6f92; }
QPushButton#manual_sell_btn:pressed { background: #d05470; }

/* ─── 패널 타이틀 ─────────────────────────────────────── */
QLabel#panel_title {
    background: #13131f;
    color: #89b4fa;
    font-weight: bold;
    padding: 6px 8px;
    border-bottom: 1px solid #313244;
}
QLabel#cash_label  { color: #fab387; }
QLabel#risk_label  { color: #a6adc8; font-size: 8pt; }

/* ─── 익절/손절 SpinBox ───────────────────────────────── */
QDoubleSpinBox#spin_tp, QDoubleSpinBox#spin_sl {
    background: #1e1e2e;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 2px 4px;
    font-size: 8pt;
}
QDoubleSpinBox#spin_tp { color: #a6e3a1; }
QDoubleSpinBox#spin_sl { color: #f38ba8; }
QDoubleSpinBox#spin_tp:focus, QDoubleSpinBox#spin_sl:focus {
    border-color: #89b4fa;
}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    background: #313244; width: 14px; border: none;
}
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background: #45475a;
}

/* ─── 구분선 ──────────────────────────────────────────── */
QFrame#h_sep { color: #1e1e2e; max-height: 1px; }

/* ─── 테이블 ──────────────────────────────────────────── */
QTableWidget {
    background: #0d0d14;
    border: none;
    gridline-color: #1e1e2e;
    selection-background-color: #2a2a3e;
    selection-color: #cdd6f4;
    alternate-background-color: #111120;
}
QHeaderView::section {
    background: #13131f;
    color: #7f849c;
    border: none;
    border-bottom: 1px solid #313244;
    padding: 4px;
    font-weight: bold;
}
QTableWidget::item { padding: 3px 6px; }

/* ─── 로그 ────────────────────────────────────────────── */
QTextEdit#log_area {
    background: #0a0a10;
    border: none;
    border-top: 1px solid #1e1e2e;
    color: #cdd6f4;
    selection-background-color: #2a2a3e;
}

/* ─── 스크롤바 ────────────────────────────────────────── */
QScrollBar:vertical {
    background: #0d0d14; width: 8px; border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #313244; border-radius: 4px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def _launch_log_monitor():
    """별도 콘솔 창에서 log_monitor.py 를 자동 실행한다 (Windows 전용).

    Returns:
        subprocess.Popen | None — 메인 창 종료 시 terminate() 호출용
    """
    import subprocess
    monitor_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "log_monitor.py",
    )
    if not os.path.exists(monitor_path):
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, monitor_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return proc
    except Exception as e:
        print(f"[WARN] log_monitor 자동 실행 실패: {e}")
        return None


class _FeedbackWorker(QObject):
    """Feedback Loop 를 별도 스레드에서 실행하는 워커."""
    finished = pyqtSignal(object)   # FeedbackResult

    @pyqtSlot()
    def run(self) -> None:
        from datetime import date as _date
        import logging as _logging
        _log = _logging.getLogger(__name__)

        today = _date.today()

        try:
            from analysis.feedback_engine import FeedbackEngine
            from analysis.daily_report import DailyReporter

            engine = FeedbackEngine()
            result = engine.run_daily(today)

            audits = engine.parse_audit(today)
            reporter = DailyReporter()
            report_path = reporter.generate(result, audits)
            result.report_path = str(report_path)

        except Exception as e:
            _log.error("[FeedbackWorker] 피드백 오류: %s", e, exc_info=True)
            from analysis.feedback_engine import FeedbackResult
            result = FeedbackResult(
                date=today, total_realized=0, total_trades=0,
                profitable=False, category_hits={}, adjustments=[],
                skipped_reasons=[f"오류 발생: {e}"], applied=False,
            )

        # ── LogAnalyzer: scanner.log + audit 파싱 → 텔레그램 메시지 생성 ──
        try:
            from analysis.log_analyzer import LogAnalyzer
            analyzer = LogAnalyzer()
            log_result = analyzer.run(today)
            result.telegram_msg = analyzer.format_telegram_report(
                scanner=log_result.scanner,
                trades=log_result.trades,
                feedback_adjustments=result.adjustments,
                feedback_skipped=result.skipped_reasons,
            )
        except Exception as e:
            _log.warning("[FeedbackWorker] LogAnalyzer 오류: %s", e, exc_info=True)
            # 실패해도 기존 피드백 결과는 그대로 emit

        self.finished.emit(result)


def launch(kiwoom) -> None:
    """
    대시보드를 실행한다.

    사용 예)
        from ui.main_window import launch
        launch(kiwoom)
    """
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(kiwoom)
    win.show()

    # 실시간 로그 감시 대시보드 — 비활성화 (스레드 부하 감소)
    win._log_monitor_proc = None  # _launch_log_monitor()

    # 로그인 다이얼로그 — 창이 보인 뒤 실행
    QTimer.singleShot(100, win.login_mgr.show_and_login)

    import os as _os
    _os._exit(app.exec())


if __name__ == "__main__":
    import sys, os, logging
    sys.path.insert(0, os.path.dirname(__file__) + "/..")

    # QApplication을 가장 먼저 생성 (pyqtgraph 포함 모든 Qt 코드보다 앞서야 함)
    from PyQt5.QtWidgets import QApplication
    _app = QApplication(sys.argv)

    # 콘솔 + 파일 로그 설정
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    # 파일 핸들러 (kiwoom_auto.log)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "kiwoom_auto.log"),
        maxBytes=20 * 1024 * 1024,  # 20 MB
        backupCount=10,
        encoding="utf-8",  # UTF-8 강제 (한글 대시 EM-dash 지원)
    )
    file_handler.setLevel(logging.INFO)

    # 콘솔 핸들러 (Windows 기본 인코딩 유지, 에러만 무시)
    import sys
    sys.stdout.reconfigure(errors='replace')
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    # 포매터
    formatter = logging.Formatter(
        "%(asctime)s\t%(levelname)s\t%(name)s\t%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 루트 로거 설정
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
    )

    try:
        from kiwoom_api import KiwoomManager
        kiwoom = KiwoomManager()
    except Exception as e:
        print(f"[WARN] KiwoomManager init failed ({e}) -- Mock mode")
        from kiwoom_api import MockKiwoomManager
        kiwoom = MockKiwoomManager()
    launch(kiwoom)
