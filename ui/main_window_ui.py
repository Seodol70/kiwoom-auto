# -*- coding: utf-8 -*-
"""
MainWindowUI Mixin - UI 레이아웃 및 구성 전담
"""

from __future__ import annotations
from datetime import datetime
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QFrame
)
from ui.style_sheets import _DARK_QSS
from app.config_manager import config_manager as cfg

# UI Components
from ui.components.header_bar import HeaderBar
from ui.components.scanner_panel import ScannerPanel
from ui.components.chart_panel import ChartPanel
from ui.components.portfolio_panel import PortfolioPanel
from ui.components.investor_panel import ScanStatusBar
from ui.components.log_panel import LogPanel, ScannerLogHandler, SysLogQtHandler

class MainWindowUI:
    """MainWindow의 시각적 레이아웃 구성을 담당하는 Mixin"""
    
    def _init_window_settings(self):
        self.setWindowTitle("키움 자동매매 대시보드")
        self.resize(1600, 900)
        self.setMinimumSize(1200, 800)
        self.setStyleSheet(_DARK_QSS)

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
        h_split.setHandleWidth(6)

        # 좌: 스캐너 감시 종목
        self.scanner_panel = ScannerPanel()
        h_split.addWidget(self.scanner_panel)

        # 우: 보유현황(위) + 차트(아래) 세로 분할
        right_v = QSplitter(Qt.Vertical)
        right_v.setHandleWidth(6)
        _RISK = cfg.RISK
        self.portfolio_panel = PortfolioPanel(
            tp_init=_RISK.get("take_profit_pct", 3.0),
            sl_init=_RISK.get("stop_loss_pct",  -1.2),
        )
        self.chart_panel = ChartPanel()
        right_v.addWidget(self.portfolio_panel)
        right_v.addWidget(self.chart_panel)
        right_v.setSizes([560, 280])   # 보유현황:차트 ≈ 66:33 (차트 크기 축소)
        h_split.addWidget(right_v)

        # 6 : 4 비율 (스캐너 60% : 보유현황 40%)
        h_split.setSizes([960, 640])
        h_split.setStretchFactor(0, 6)  # 스캐너 60%
        h_split.setStretchFactor(1, 4)  # 보유현황+차트 40%
        root.addWidget(h_split, stretch=1)

        # 구분선
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setObjectName("h_sep")
        root.addWidget(sep2)

        # 스캔 상태바
        self.scan_status = ScanStatusBar()
        root.addWidget(self.scan_status)

        # 하단 로그
        self.log_panel = LogPanel()
        root.addWidget(self.log_panel)

    def append_log(self, text: str) -> None:
        """로그를 큐에 쌓는다. (실제 UI 반영은 0.5초마다 일괄 수행)"""
        if not text:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.append(f"[{ts}] {text}")
        
        # 큐 폭주 방지 (최대 100개까지만 유지 후 일괄 배출)
        if len(self._log_queue) > 100:
            self._log_queue.pop(0)

    def _flush_logs(self) -> None:
        """큐에 쌓인 로그를 log_panel에 일괄 추가한다."""
        if not hasattr(self, "log_panel") or not self.log_panel:
            return
        if not self._log_queue:
            return
            
        scrollbar = self.log_panel.verticalScrollBar()
        is_at_bottom = scrollbar.value() >= scrollbar.maximum() - 20
        
        self.log_panel.append("\n".join(self._log_queue))
        self._log_queue.clear()
        
        if is_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _setup_logging_handlers(self) -> None:
        """스캐너 및 시스템 로그를 UI Panel에 연결"""
        import logging
        # Scanner Log (전략 판정 전용)
        self._scanner_handler = ScannerLogHandler(self.scanner_panel)
        logging.getLogger("scanner.audit").addHandler(self._scanner_handler)
        
        # System Log
        self._sys_handler = SysLogQtHandler(self.log_panel)
        logging.getLogger().addHandler(self._sys_handler)

        # [NEW] 스캐너 워커 로그 전파 차단 (중복 출력 방지)
        logging.getLogger("scanner.worker").propagate = False

        # [NEW] 초기 기동 시 이미 로그인된 상태라면 헤더 정보 동기화
        if hasattr(self, "login_mgr") and self.login_mgr.account:
            self.header.set_connected(self.login_mgr.account, self.login_mgr.server_mode)
        
        # [NEW] 초기 손익 데이터 동기화
        if hasattr(self, "state") and self.state:
            self.header.set_pnl(self.state.daily_realized_pnl)
