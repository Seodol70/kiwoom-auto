# -*- coding: utf-8 -*-
"""
MainWindow — 통합 대시보드 (오케스트레이터)
Mixin 패턴을 사용하여 UI 구성과 이벤트 처리를 분리함.
"""

from __future__ import annotations
import os
import time
import logging
from datetime import datetime
from typing import Any

from PyQt5.QtCore import Qt, QTimer, pyqtSlot, QThread
from PyQt5.QtWidgets import QMainWindow, QApplication

from ui.main_window_ui import MainWindowUI
from ui.main_window_slots import MainWindowSlots
from app.state import AppState
from app.config_manager import config_manager as cfg
from engine.workers import ScannerWorker, PortfolioWorker

logger = logging.getLogger(__name__)

class MainWindow(QMainWindow, MainWindowUI, MainWindowSlots):
    """
    통합 대시보드 메인 윈도우.
    UI 구성을 MainWindowUI에, 슬롯 처리를 MainWindowSlots에 위임함.
    """

    def __init__(self, kiwoom, parent=None) -> None:
        super().__init__(parent)
        self._kiwoom = kiwoom

        # 상태 및 플래그 초기화
        self._sl_tp_warmup_end: float = 0.0
        self._scan_in_progress: bool = False
        self._liquidate_in_progress: bool = False
        self._market_crash_off: bool = False
        self._already_started: bool = False
        self._opened_today: bool = False
        self._closed_today: bool = False
        self._feedback_done_today: bool = False
        self._log_queue: list[str] = []
        self._today_watch: dict[str, Any] = {}

        # 중앙 상태 관리자
        self.state = AppState()

        # [Step 1] UI 초기화 (MainWindowUI Mixin)
        self._init_window_settings()
        self._build_ui()

        # [Step 2] 모듈 및 워커 설정
        self._setup_modules()
        self._setup_timers()

        # 로그 타이머 시작
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._flush_logs)
        self._log_timer.start(500)

        logger.info("[MainWindow] 초기화 완료")

    def _setup_modules(self) -> None:
        """핵심 모듈 초기화 및 서비스 시작"""
        from app.core import ApplicationContext
        self.app_context = ApplicationContext(self._kiwoom, parent=self)
        
        # 의존성 바인딩
        self.login_mgr = self.app_context.login_mgr
        self.order_mgr = self.app_context.order_mgr
        self._health_monitor = self.app_context.health_monitor
        self._audit = self.app_context.audit
        self._snap_store = self.app_context.snap_store
        self._scan_cfg = self.app_context.scan_cfg
        self._smart_scanner = self.app_context.smart_scanner
        self.market_scheduler = self.app_context.market_scheduler
        self.risk_manager = self.app_context.risk_manager
        self.trading_controller = self.app_context.trading_controller
        
        # 상태 주입
        self.trading_controller._ctx = self.state
        self.order_mgr.set_state(self.state)
        self.order_mgr.set_health_monitor(self._health_monitor)
        self._tg = getattr(self.app_context, "tg_bot", None)

        # 서브 시스템 설정 (Mixin 및 로컬 메서드)
        self._setup_logging_handlers()
        self._setup_news_analyzer()
        self._setup_background_workers()
        
        # 시그널 관리자 시작
        from ui.signal_manager import SignalManager
        self.signal_manager = SignalManager(self)
        self.signal_manager.bind_all()

        # 연결 감시 (Watchdog)
        from app.connection_watchdog import ConnectionWatchdog
        self._watchdog = ConnectionWatchdog(
            kiwoom=self._kiwoom,
            login_mgr=self.login_mgr,
            smart_scanner=self._smart_scanner,
            parent=self,
        )
        self._watchdog.start()

    def _setup_timers(self) -> None:
        """주기적 작업 타이머 설정"""
        # 1. 포트폴리오 동기화 (10초)
        self._port_refresh_timer = QTimer(self)
        self._port_refresh_timer.timeout.connect(self._port_worker.sync)
        
        # 2. 스캐너 주기적 스캔 (60초)
        self._scan_refresh_timer = QTimer(self)
        self._scan_refresh_timer.timeout.connect(self.trading_controller.run_periodic_scan)
        
        # 3. 장 종료 후 분석 (15:35)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.timeout.connect(self._check_market_close_feedback)
        self._feedback_timer.start(60_000)

    def _setup_background_workers(self) -> None:
        """배경 스레드 워커 설정"""
        # 1. 잔고 동기화 워커
        self._port_thread = QThread(self)
        self._port_worker = PortfolioWorker(self._kiwoom, self.order_mgr)
        self._port_worker.moveToThread(self._port_thread)
        self._port_thread.started.connect(self._port_worker.run)
        self._port_thread.start()

        # 2. 스캐너 워커 (레거시 지원용으로 유지 가능, SmartScanner와 병행)
        self._scan_thread = QThread(self)
        self._scan_worker = ScannerWorker(self._snap_store, self._scan_cfg, self.order_mgr)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_thread.start()

    def _setup_news_analyzer(self) -> None:
        """뉴스 분석기 초기화"""
        from scanner.news_analyzer import NewsAnalyzer
        self._news_analyzer = NewsAnalyzer()

    def start_after_login(self) -> None:
        """로그인 후 실질적 시스템 가동"""
        self._port_worker.sync()
        self._port_refresh_timer.start(10_000)
        self._scan_refresh_timer.start(60_000)
        
        # 초기 스캔 즉시 실행
        QTimer.singleShot(1000, self.trading_controller.run_periodic_scan)
        self.append_log("🚀 [시스템] 로그인 후 자동 동기화 및 스캔 시작")

    def _check_market_close_feedback(self) -> None:
        """15:35분 장 마감 피드백 실행 여부 확인"""
        if self._feedback_done_today: return
        now = datetime.now()
        if now.hour == 15 and now.minute >= 35:
            self._feedback_done_today = True
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, self._run_feedback_loop)

    def _run_feedback_loop(self) -> None:
        """피드백 엔진 실행 (QThread)"""
        from app.feedback_worker import FeedbackWorker
        self.log_panel.append("📊 [피드백] 장 마감 분석 시작...")
        self._fb_thread = QThread(self)
        worker = FeedbackWorker()
        worker.moveToThread(self._fb_thread)
        self._fb_thread.started.connect(worker.run)
        worker.finished.connect(self._on_feedback_done)
        worker.finished.connect(self._fb_thread.quit)
        self._fb_thread.start()

    def closeEvent(self, event) -> None:
        """프로그램 종료 시 자원 정리"""
        self.append_log("👋 프로그램 종료 중...")
        if self._tg: self._tg.stop()
        super().closeEvent(event)


def launch(kiwoom):
    """Qt 대시보드 실행 엔트리포인트"""
    win = MainWindow(kiwoom)
    win.show()
    return win
