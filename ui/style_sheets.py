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
    background: #f38ba8;
    color: #11111b;
    border: none;
}

/* ─── 테이블 (Sleek List) ───────────────────────────────── */
QTableWidget {
    background: #0d0d14;
    border: none;
    gridline-color: #181825;
    selection-background-color: #313244;
}
QHeaderView::section {
    background: #11111b;
    color: #9399b2;
    border: none;
    border-bottom: 2px solid #1e1e2e;
    padding: 6px;
    font-weight: 700;
    text-transform: uppercase;
    font-size: 8pt;
}

/* ─── 로그 (Console style) ──────────────────────────────── */
QTextEdit#log_area {
    background: #09090f;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 9pt;
    line-height: 140%;
}

/* ─── 스크롤바 ────────────────────────────────────────── */
QScrollBar:vertical {
    background: transparent;
    width: 6px;
}
QScrollBar::handle:vertical {
    background: #313244;
    border-radius: 3px;
}
QScrollBar::handle:vertical:hover {
    background: #45475a;
}
"""
