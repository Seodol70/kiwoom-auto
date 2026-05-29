# -*- coding: utf-8 -*-
"""
SignalManager - 프로그램 전체의 시그널 연결을 중앙 관리하는 클래스
"""

from PyQt5.QtCore import QObject, pyqtSlot
import logging

logger = logging.getLogger(__name__)

class SignalManager:
    """
    엔진(TradingController, OrderManager 등)과 UI(MainWindow, Panels) 간의
    모든 시그널-슬롯 연결을 중앙에서 제어합니다.
    """
    
    def __init__(self, win):
        self.win = win
        self.state = win.state
        self.tc = win.trading_controller
        self.om = win.order_mgr
        self.lm = win.login_mgr

    def bind_all(self):
        """모든 카테고리의 시그널을 연결"""
        self._bind_auth()
        self._bind_trading_core()
        self._bind_ui_interactions()
        self._bind_background_workers()
        self._bind_scanner_core()
        self._bind_context_updates()
        self._bind_external()
        self._bind_watchdog()
        logger.info("[SignalManager] 모든 시그널 연결 완료")

    def _bind_auth(self):
        """로그인 및 인증 관련 시그널"""
        self.lm.login_success.connect(self.win._on_login_success)
        self.lm.login_failed.connect(lambda m: self.win.append_log(f"⚠ 로그인 실패: {m}"))
        self.win._kiwoom.set_auto_login_callback(lambda: self.win.append_log("✅ 자동 재로그인 성공"))

    def _bind_trading_core(self):
        """주문, 체결, 필터링 등 핵심 거래 로직"""
        # 주문 로그 — lambda 제거, 전용 슬롯 연결
        self.om.order_sent.connect(self.win._on_order_sent)
        self.om.order_filled.connect(self.win._on_order_filled)
        self.om.order_failed.connect(lambda m: self.win.append_log(f"⚠ 주문 실패: {m}"))
        
        # 컨트롤러 피드백
        self.tc.signal_rejected.connect(lambda msg: self.win.append_log(f"❌ [진입거절] {msg}"))
        # [NEW] 감시 및 매매 핵심 로그는 왼쪽 패널(append)로 출력
        self.tc.log_message.connect(self.win.log_panel.append)
        
        # [NEW] 로그 핸들러 시그널 연결 (SysLogQtHandler -> LogPanel)
        if hasattr(self.win, "_sys_handler"):
            self.win._sys_handler.log_entry.connect(self.win.log_panel.append_syslog)
        if hasattr(self.win, "_scanner_handler"):
            self.win._scanner_handler.log_entry.connect(self.win.log_panel.append_scanner)
        
        # 포트폴리오 업데이트 — 이제 AppState.portfolio_updated 시그널로 일원화됨
        self.tc.scan_status_updated.connect(self.win._on_scan_status_updated)

        # 첫 신호 자동매매 시작 — TC에서 판단, UI만 반응
        self.tc.auto_trade_started.connect(self.win._on_auto_trade_started)

        # [NEW] 당일 실현손익 업데이트 연동
        self.state.pnl_updated.connect(self.win.header.set_pnl)

        # 지수 급락 감지 신호 연결 (상태 변경)
        self.tc.market_crash_detected.connect(self.tc._on_market_crash_detected)

    def _bind_ui_interactions(self):
        """사용자 조작(버튼 클릭, 값 변경 등) 관련 시그널"""
        # 헤더 바 컨트롤
        self.win.header.auto_trade_toggled.connect(self.win._on_auto_trade_toggle)
        self.win.header.auto_trade_toggled.connect(self.tc.set_auto_trading)
        self.win.header.overnight_mode_toggled.connect(self.win._on_overnight_mode_toggle)
        self.win.header.switch_real_requested.connect(self.win._on_switch_real_requested)
        self.win.header.reload_requested.connect(self.win._on_reload_config)
        self.win.header.unlock_requested.connect(self.win._on_manual_unlock_requested)
        self.win.header.exit_requested.connect(self.win.close)

        # 패널 조작
        self.win.portfolio_panel.manual_sell.connect(self.win._on_manual_sell)
        self.win.portfolio_panel.tp_changed.connect(self.state.set_risk_params)
        self.win.portfolio_panel.sl_changed.connect(lambda v: self.state.set_risk_params(sl=v))
        self.win.portfolio_panel.row_clicked.connect(self.win._on_code_selected)

        self.win.scanner_panel.row_clicked.connect(self.win._on_code_selected)
        self.win.scanner_panel.manual_buy_requested.connect(self.win._on_manual_buy)

    def _bind_background_workers(self):
        """워커 스레드 및 스케줄러 관련 시그널"""
        self.win._port_worker.refresh_done.connect(self.win._on_portfolio_refresh)
        self.win._port_worker.log_message.connect(self.win.append_log)

        # ScannerWorker 관련 시그널 (제거 예정)
        # self.win._scan_worker.signal_detected.connect(self.tc.handle_signal)
        # self.win._scan_worker.signal_detected.connect(self.win._on_scan_signal)
        # self.win._scan_worker.watch_list_updated.connect(self.win.scanner_panel.refresh)
        # self.win._scan_worker.log_message.connect(self.win.append_log)

        # 스케줄러
        ms = self.win.market_scheduler
        ms.market_opened.connect(self.win._on_market_opened)
        ms.market_closing.connect(self.win._on_market_closing)
        ms.feedback_triggered.connect(self.win._on_feedback_triggered)
        ms.day_reset.connect(self.win._on_day_reset)
        # [Step 3 Phase 1] 모든 청산 신호를 tick_exit_check()로 통합
        ms.overnight_gap_check.connect(self.tc.tick_exit_check)
        ms.eod_daytime_check.connect(self.tc.tick_exit_check)
        ms.eod_trend_check.connect(self.tc.tick_exit_check)
        ms.overnight_timecut.connect(self.tc.tick_exit_check)
        ms.phase1_cutoff.connect(self.tc.tick_exit_check)
        ms.phase1_trail.connect(self.tc.tick_exit_check)
        ms.overnight_auto_enabled.connect(lambda: self.win.header.set_overnight_checked(True))
        ms.overnight_auto_enabled.connect(lambda: self.win._on_overnight_mode_toggle(True))

        # 리스크 매니저
        rm = self.win.risk_manager
        if rm:
            rm.daily_loss_cut.connect(self.win._on_loss_cut)
            rm.daily_loss_cut.connect(lambda: self.win.append_log("🔴 [리스크] 당일 손익 락 발동 (매수 차단)"))
            rm.daily_loss_cut.connect(lambda: self.win.header.set_risk_status("DANGER", "LOSS CUT"))
            # [Step 3] AppState에 리스크 락 상태 반영
            rm.daily_loss_cut.connect(lambda: setattr(self.state, "loss_cut_locked", True))
            
            rm.daily_profit_locked.connect(self.win._on_profit_locked)
            rm.daily_profit_locked.connect(lambda: self.win.header.set_risk_status("WARNING", "PROFIT LOCK"))
            # 익절 락은 보통 매수 차단까지는 아니지만, 필요시 설정 가능
            # rm.daily_profit_locked.connect(lambda: setattr(self.state, "risk_locked", True))
            
        # [NEW] 주문 관리자 -> 헤더 사이징 표시 (초기화 시 한 번)
        mode = getattr(self.win._scan_cfg, "position_sizing_mode", "EQUAL")
        self.win.header.set_sizing_mode(mode)

    def _bind_scanner_core(self):
        """SmartScanner 신호 경로 중앙화 (Critical 4)"""
        ss = getattr(self.win, "_smart_scanner", None)
        if ss:
            # [Step 2] 콜백 대신 pyqtSignal 연결 방식으로 전환
            ss.signal_detected.connect(self.tc.handle_signal)
            ss.signal_detected.connect(self.win._on_scan_signal)
            # [Phase 3] UI 하이라이트 효과 연결
            ss.signal_detected.connect(self.win.scanner_panel.add_signal)
            # [NEW] 실시간 감시 목록 UI 갱신 연결 (최적화된 증분 업데이트)
            ss.watch_list_updated.connect(self.win.scanner_panel.refresh)
            # [Phase D-3] 포지션 현재가 갱신 신호 연결 (OrderManager에게 전달)
            ss.price_updated.connect(self.om._on_price_updated)
            logger.info("[SignalManager] SmartScanner 시그널 연결 완료 (signal_detected & watch_list_updated & price_updated)")

    def _bind_context_updates(self):
        """중앙 상태 관리자와 UI 동기화"""
        self.state.auto_trading_changed.connect(self.win.header.set_auto_checked)
        self.state.overnight_mode_changed.connect(self.win.header.set_overnight_checked)
        self.state.market_data_updated.connect(self.win._on_market_data_updated)
        self.state.account_changed.connect(self.win.header.set_connected)
        self.state.log_requested.connect(self.win.append_log)
        
        # AppState 포트폴리오 변경 시 UI 패널 갱신
        self.state.portfolio_updated.connect(self.win._on_portfolio_refresh)
        self.state.pnl_updated.connect(self.win.header.set_pnl)
        
        # 컨트롤러-상태 직접 연결
        self.tc.market_data_updated.connect(self.state.update_market_data)

    def _bind_external(self):
        """텔레그램 등 외부 연동"""
        if self.win._tg:
            self.win._tg.cmd_start.connect(lambda: self.win._on_auto_trade_toggle(True))
            self.win._tg.cmd_stop.connect(lambda: self.win._on_auto_trade_toggle(False))
            self.win._tg.cmd_status.connect(self.win._on_tg_status_requested)
            self.win.append_log("[연결] 텔레그램 봇 시그널 바인딩 완료")
    def _bind_watchdog(self):
        """연결 감시(Self-Healing) 관련 시그널"""
        wd = getattr(self.win, "_watchdog", None)
        if not wd:
            return
            
        wd.connection_lost.connect(self.win._on_connection_lost)
        wd.connection_recovered.connect(self.win._on_connection_recovered)
        wd.reconnect_failed.connect(self.win._on_reconnect_failed)
        
        # [Phase 3] HealthMonitor 상태 LED 연결
        hm = getattr(self.win, "_health_monitor", None)
        if hm:
            hm.status_changed.connect(self.win.header.set_health_status)
            
        logger.info("[SignalManager] Watchdog & HealthMonitor 시그널 연결 완료")
