"""
MainWindow — 통합 대시보드 스타일 시트 (PyQt5 · Deep Dark)
"""

_DARK_QSS = """
/* ─── 전역 스타일 ─────────────────────────────────────── */
* {
    font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
    font-size: 9pt;
    color: #cdd6f4;  /* 기본 텍스트 색상을 밝은 색으로 설정 */
}
QMainWindow {
    background: #1e1e2e;
}
QSplitter::handle {
    background: #313244;
}

/* ─── 헤더 바 ─────────────────────────────────────────── */
QWidget#header_bar {
    background: #11111b;
    border-bottom: 1px solid #313244;
}
QLabel#lbl_title {  /* logo_label -> lbl_title로 수정 */
    color: #f5c2e7;
    font-size: 14pt;
    font-weight: bold;
    padding: 10px;
}
/* 특정 헤더 정보 라벨 스타일 */
QWidget#header_bar QLabel {
    color: #cdd6f4;
}
QLabel#conn_on  { color: #a6e3a1; font-weight: bold; }  /* 연결됨: 초록색 */
QLabel#conn_off { color: #f38ba8; font-weight: bold; }  /* 미연결: 빨간색 */


/* ─── 버튼 ────────────────────────────────────────────── */
QPushButton {
    background: #313244;
    color: #cdd6f4;
    border: none;
    border-radius: 4px;
    padding: 6px 12px;
}
QPushButton:hover { background: #45475a; }
QPushButton:pressed { background: #585b70; }

/* ─── 자동매매 버튼 ────────────────────────────────────── */
QPushButton#btn_auto_off {
    background: #313244;
    color: #f38ba8;
    border: 1px solid #f38ba8;
    font-weight: bold;
}
QPushButton#btn_auto_on {
    background: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
}

/* ─── 실전/모의 전환 버튼 ────────────────────────────── */
QPushButton#btn_switch_real {
    background: #313244;
    color: #fab387;
    border: 1px solid #fab387;
}

/* ─── 야간보유 모드 버튼 ──────────────────────────────── */
QPushButton#btn_overnight_off {
    background: #313244;
    color: #cba6f7;
    border: 1px solid #cba6f7;
}
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
QDoubleSpinBox::up-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
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
