# -*- coding: utf-8 -*-
"""
Premium Dark QSS Stylesheet
"""

_DARK_QSS = """
/* ─── 전역 스타일 (Premium Dark) ─────────────────────────── */
* {
    font-family: 'Malgun Gothic', 'Outfit', 'Inter', sans-serif;
    font-size: 9pt;
    color: #cdd6f4;
}
QMainWindow {
    background: #0b0b12; /* 더 깊은 블랙 */
}
QSplitter::handle {
    background: #181825;
}
QSplitter::handle:hover {
    background: #313244;
}

/* ─── 헤더 바 ─────────────────────────────────────────── */
QWidget#header_bar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #181825, stop:1 #11111b);
    border-bottom: 2px solid #1e1e2e;
}
QLabel#lbl_title {
    color: #cba6f7; /* Mauve */
    font-size: 15pt;
    font-weight: 800;
    padding-left: 15px;
}

/* Risk Status Label */
QLabel#lbl_risk_status {
    background: #1e1e2e;
    color: #a6e3a1; /* Green */
    border-radius: 12px;
    padding: 2px 10px;
    font-weight: bold;
    font-size: 8pt;
    border: 1px solid #313244;
}
QLabel#lbl_risk_status[status="danger"] {
    background: #f38ba8;
    color: #11111b;
}
QLabel#lbl_risk_status[status="warning"] {
    background: #fab387;
    color: #11111b;
}

/* ─── 버튼 (Modern Glassmorphism Style) ─────────────────── */
QPushButton {
    background: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 5px 12px;
    font-weight: 600;
}
QPushButton:hover {
    background: #45475a;
    border-color: #585b70;
}
QPushButton:pressed {
    background: #1e1e2e;
}

/* ─── 자동매매 버튼 ────────────────────────────────────── */
QPushButton#btn_auto_off {
    background: transparent;
    color: #f38ba8;
    border: 2px solid #f38ba8;
}
QPushButton#btn_auto_off:hover {
    background: rgba(243, 139, 168, 0.1);
}
QPushButton#btn_auto_on {
    background: #f38ba8;
    color: #11111b;
    border: 2px solid #f38ba8;
}

/* ─── 오전 골든타임 버튼 ──────────────────────────────── */
QPushButton#btn_goldentime_off {
    background: transparent;
    color: #f9e2af;
    border: 1px solid #f9e2af;
}
QPushButton#btn_goldentime_off:hover {
    background: rgba(249, 226, 175, 0.1);
}
QPushButton#btn_goldentime_on {
    background: #f9e2af;
    color: #11111b;
    border: 2px solid #f9e2af;
    font-weight: bold;
}

/* ─── 실전/모의 전환 버튼 ────────────────────────────── */
QPushButton#btn_switch_real {
    color: #fab387;
    border: 1px solid #fab387;
}
QPushButton#btn_switch_real:hover {
    background: rgba(250, 179, 135, 0.1);
}

/* ─── 재시작/종료 버튼 ────────────────────────────────── */
QPushButton#btn_restart {
    background: #94e2d5; /* Teal */
    color: #11111b;
    border: none;
}
QPushButton#btn_exit {
    background: #f38ba8; /* Red */
    color: #ffffff;      /* 밝은 색으로 변경 */
    font-weight: bold;
    border: none;
}

/* ─── 입력 필드 및 드롭다운 ────────────────────────────── */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #1e1e2e;
    border: 1px solid #313244;
    border-radius: 4px;
    padding: 4px;
    color: #cdd6f4;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
    border-color: #cba6f7;
}

/* 익절/손절 레이블 강조 */
QLabel#lbl_tp { color: #a6e3a1; font-weight: bold; } /* 초록색 익절 */
QLabel#lbl_sl { color: #f38ba8; font-weight: bold; } /* 빨간색 손절 */

/* ─── 테이블 (QTableWidget) ────────────────────────────── */
QTableWidget {
    background: #11111b;
    alternate-background-color: #181825; /* 홀수 행 배경색 */
    border: none;
    gridline-color: #313244;
    border-radius: 8px;
}
QHeaderView::section {
    background: #181825;
    color: #9399b2;
    padding: 8px;
    border: none;
    font-weight: bold;
    border-bottom: 1px solid #313244;
}
QTableWidget::item {
    padding: 5px;
    border-bottom: 1px solid #1e1e2e;
}
QTableWidget::item:selected {
    background: #313244;
    color: #f5e0dc;
}

/* 테이블 내부 매수/매도 버튼 */
QTableWidget QPushButton {
    background: #313244;
    border: 1px solid #45475a;
    border-radius: 3px;
    padding: 2px;
    font-size: 8pt;
    min-width: 40px;
}
QTableWidget QPushButton:hover {
    background: #45475a;
}

/* 특정 버튼 강조 */
QPushButton#btn_manual_buy {
    background: #fab387; /* Peach */
    color: #11111b;
}
QPushButton#btn_manual_sell {
    background: #f38ba8; /* Red */
    color: #11111b;
}

/* ─── 스크롤바 ────────────────────────────────────────── */
QScrollBar:vertical {
    border: none;
    background: #11111b;
    width: 10px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: #313244;
    min-height: 20px;
    border-radius: 5px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

/* ─── 로그 패널 (QTextEdit) ────────────────────────────── */
QTextEdit#log_output {
    background: #0b0b12;
    border: 1px solid #1e1e2e;
    border-radius: 8px;
    padding: 10px;
    line-height: 1.5;
}

/* ─── 메시지 박스 (QMessageBox) ────────────────────────── */
QMessageBox {
    background-color: #1e1e2e;
}
QMessageBox QLabel {
    color: #cdd6f4;
    font-size: 10pt;
}
QMessageBox QPushButton {
    background: #313244;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 5px 15px;
    color: #cdd6f4;
}
QMessageBox QPushButton:hover {
    background: #45475a;
}
"""
