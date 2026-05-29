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
from engine.workers import PortfolioWorker

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
        self._scan_in_progress: bool = False
        self._liquidate_in_progress: bool = False
        self._already_started: bool = False
        self._log_queue: list[str] = []
        self._today_watch: dict[str, Any] = {}

        # 중앙 상태 관리자 (초기화 전)
        self.state = None

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
        self.state = self.app_context.state # 컨텍스트에서 상태 인스턴스 획득
        
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

        # 상태 동기화 (AppContext에서 이미 _ctx 주입됨)
        self.order_mgr.set_state(self.state)
        self.order_mgr.set_health_monitor(self._health_monitor)
        self.order_mgr.set_config(self._scan_cfg)  # [FIX 2026-05-12] config 주입
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

        # [FIX 2026-05-29] 일봉 갱신 시그널 연결 — daily_refresh_requested → refresh_daily_candles
        # 미연결 상태로 일봉 데이터가 전혀 로딩되지 않아 BREAKOUT_NO_DAILY로 모든 신호 차단됐던 문제
        self.trading_controller.daily_refresh_requested.connect(
            lambda codes: self.trading_controller.refresh_daily_candles(codes, 0)
        )

        # 3. 장 종료 후 분석은 MarketScheduler의 feedback_triggered 신호로 처리됨

        # 4. [Option A 2026-05-27] 청산 평가 독립 타이머 (5초 주기)
        # — 잔고 동기화 워커와 분리. 잔고 워커가 멈춰도 손절/익절 정상 작동
        # — 빛과전자 -52,211원 사례(잔고 워커 11분 침묵) 재발 방지
        self._exit_check_timer = QTimer(self)
        self._exit_check_timer.timeout.connect(self.trading_controller.tick_exit_check)

    def _setup_background_workers(self) -> None:
        """백그라운드 워커 설정 및 시작"""
        logger.info("[MainWindow] 워커 설정 시작...")
        # 1. 포트폴리오 워커 (UI 스레드 - Kiwoom OCX 싱글 스레드 제약 때문)
        self._port_worker = PortfolioWorker(self.order_mgr, self.trading_controller)
        
        
        logger.info("[MainWindow] 백그라운드 워커 설정 완료")

    def _setup_news_analyzer(self) -> None:
        """뉴스 분석기 — ApplicationContext에서 생성된 인스턴스 참조
        [2026-05-26] trading_controller와 동일 인스턴스 공유 (매매 결정에 활용)
        """
        self._news_analyzer = self.app_context.news_analyzer

    def start_after_login(self) -> None:
        """로그인 후 실질적 시스템 가동"""
        self._port_worker.sync()
        # 10초(10,000) -> 60초(60,000)로 상향 조정 (서버 부하 방지)
        self._port_refresh_timer.start(60_000)
        # 60초(60,000) -> 120초(120,000)로 상향 조정 (config 연동)
        scan_interval = int(getattr(self._scan_cfg, "scan_interval", 120.0)) * 1000
        self._scan_refresh_timer.start(scan_interval)
        # [Option A 2026-05-27] 청산 평가 5초 타이머 시작 (잔고 워커와 독립)
        self._exit_check_timer.start(5_000)

        # [NEW 2026-05-29] 시스템 자체 진단기 — 5분 후 자동 1회 실행
        if not hasattr(self, "_diagnostics"):
            from infra.diagnostics import SystemDiagnostics
            self._diagnostics = SystemDiagnostics(parent=self)
            self._diagnostics.critical_finding.connect(self.append_log)
            self._diagnostics.schedule_initial_run(delay_sec=300)
            self.append_log("🩺 [진단] 시스템 진단기 가동 — 5분 후 자동 점검")

        # 2. 스마트 스캐너 시작 (백그라운드 루프 시동)
        if hasattr(self, "_smart_scanner"):
            self._smart_scanner.start()
            self.append_log("🔍 [스캐너] 스마트 스캐너 루프 시작")

        # 초기 스캔 즉시 실행
        QTimer.singleShot(1000, self.trading_controller.run_periodic_scan)

        # [NEW 2026-05-12] 자동매매 자동 시작
        if not self.state.auto_trading:
            self.state.auto_trading = True
            self.header.set_auto_checked(True)
            self.append_log("🟢 [자동시작] 자동매매 시스템이 자동으로 시작되었습니다.")

        self.append_log("🚀 [시스템] 로그인 후 자동 동기화 및 스캔 시작")

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
        """프로그램 종료 시 자원 정리 및 스레드 조기 중단"""
        self.append_log("👋 프로그램 종료 중... (자원 정리 중)")
        logger.info("[MainWindow] 종료 절차 시작")

        try:
            # 1. 타이머 중단
            if hasattr(self, "_log_timer"): self._log_timer.stop()
            if hasattr(self, "_port_refresh_timer"): self._port_refresh_timer.stop()
            if hasattr(self, "_scan_refresh_timer"): self._scan_refresh_timer.stop()
            if hasattr(self, "_exit_check_timer"): self._exit_check_timer.stop()

            # 2. 백그라운드 워커/스레드 안전 종료
            # [NEW 2026-05-26] PriorityWatchQueue 워커 중지
            # OCX 파괴 전에 워커를 멈춰야 SetRealRemove 'QAxWidget deleted' 에러 방지
            if hasattr(self, "_smart_scanner") and hasattr(self._smart_scanner, "watch_q"):
                try:
                    self._smart_scanner.watch_q.stop()
                    logger.info("[MainWindow] PriorityWatchQueue 워커 중지")
                except Exception as _e:
                    logger.warning("[MainWindow] watch_q.stop() 실패: %s", _e)

            # [NEW 2026-05-26] NewsAnalyzer 백그라운드 스레드 중지
            if hasattr(self, "_news_analyzer") and self._news_analyzer:
                try:
                    self._news_analyzer.stop()
                    logger.info("[MainWindow] NewsAnalyzer 중지")
                except Exception as _e:
                    logger.warning("[MainWindow] news_analyzer.stop() 실패: %s", _e)

            # 4. 데이터 강제 Flush (Clean Exit)
            if hasattr(self, "_audit") and self._audit:
                logger.info("[MainWindow] 매매 로그 강제 저장(Flush) 시작")
                self._audit.flush_all()
                logger.info("[MainWindow] 매매 로그 저장 완료")

            # 5. 키움 API 세션 정리 (필요 시)
            # self._kiwoom.logout() 등

        except Exception as e:
            logger.error("[MainWindow] 종료 처리 중 오류: %s", e)
        
        logger.info("[MainWindow] 종료 절차 완료")
        super().closeEvent(event)


def launch(kiwoom):
    """Qt 대시보드 실행 엔트리포인트"""
    win = MainWindow(kiwoom)
    win.show()
    return win
