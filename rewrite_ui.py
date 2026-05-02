import re

with open('ui/main_window.py', 'r', encoding='utf-8') as f:
    code = f.read()

new_setup = '''    def _setup_modules(self) -> None:
        from app.core import ApplicationContext
        self.app_context = ApplicationContext(self._kiwoom, parent=self)
        self.login_mgr = self.app_context.login_mgr
        self.order_mgr = self.app_context.order_mgr
        self._audit = self.app_context.audit
        self._snap_store = self.app_context.snap_store
        self._scan_cfg = self.app_context.scan_cfg
        self._smart_scanner = self.app_context.smart_scanner
        self.market_scheduler = self.app_context.market_scheduler
        self.risk_manager = self.app_context.risk_manager
        self.trading_controller = self.app_context.trading_controller
        self._health_monitor = self.app_context.health_monitor
        self._tg = getattr(self.app_context, "tg_bot", None)

        # Signals
        self.login_mgr.login_success.connect(self._on_login_success)
        self.login_mgr.login_failed.connect(lambda m: self.log_panel.append(f"⚠ 로그인 실패: {m}"))
        self._kiwoom.set_auto_login_callback(lambda: self.log_panel.append("✅ 자동 재로그인 성공"))
        self.order_mgr.order_sent.connect(lambda d: self.log_panel.append(f"{d['side']} 주문 전송 — {d['name']}({d['code']}) {d['qty']}주 {d['price']}원"))
        self.order_mgr.order_filled.connect(self._on_order_filled)
        self.order_mgr.order_failed.connect(lambda m: self.log_panel.append(f"⚠ 주문 실패: {m}"))

        self.portfolio_panel.manual_sell.connect(self._on_manual_sell)
        self._smart_scanner.on_signal = self._on_scan_signal_direct
        self.portfolio_panel.tp_changed.connect(self._on_tp_changed)
        self.portfolio_panel.sl_changed.connect(self._on_sl_changed)
        self.log_panel.append("[스캐너] SmartScanner 초기화 완료")

        from scanner.smart_scanner import scan_log
        from ui.components.log_panel import ScannerLogHandler, SysLogQtHandler
        self._scan_log_handler = ScannerLogHandler(self)
        self._scan_log_handler.log_entry.connect(self.log_panel.append_scanner)
        scan_log.addHandler(self._scan_log_handler)

        self._setup_controller_signals()
        self.log_panel.append("[앱] Application Layer 초기화 완료")

        self._sys_log_handler = SysLogQtHandler(self)
        self._sys_log_handler.log_entry.connect(self.log_panel.append_syslog)
        import logging
        logging.getLogger().addHandler(self._sys_log_handler)
        logging.getLogger("kiwoom_api").info("[SysLog] 시스템 로그 패널 연결 완료")

        import queue as _queue
        from scanner.news_analyzer import NewsAnalyzer
        self._news_queue = _queue.Queue()
        self._news_analyzer = NewsAnalyzer(on_result=lambda r: self._news_queue.put(r))
        self._news_analyzer.start()
        self.log_panel.append("[뉴스] NewsAnalyzer 백그라운드 스레드 시작")

        from ui.workers import ScannerWorker, PortfolioWorker
        from PyQt5.QtCore import QThread
        self._scan_thread = QThread(self)
        self._scan_worker = ScannerWorker(self._snap_store, self._scan_cfg, self.order_mgr)
        self._scan_worker._audit = self._audit
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.signal_detected.connect(self._on_scan_signal)
        self._scan_worker.watch_list_updated.connect(self.scanner_panel.refresh)
        self._scan_worker.watch_list_updated.connect(self.investor_panel.refresh)
        self._scan_worker.log_message.connect(self.log_panel.append)

        self._port_worker = PortfolioWorker(self.order_mgr, parent=self)
        self._port_worker.refresh_done.connect(self._on_portfolio_refresh)
        self._port_worker.log_message.connect(self.log_panel.append)

        self._auto_trading = False
        self.header.auto_trade_toggled.connect(self._on_auto_trade_toggle)
        self.header.exit_requested.connect(self.close)
        self.header.unlock_requested.connect(self._on_manual_unlock_requested)
        self.header.overnight_mode_toggled.connect(self._on_overnight_mode_toggle)
        self.header.switch_real_requested.connect(self._on_switch_real_requested)

        self.scanner_panel.row_clicked.connect(self._on_code_selected)
        self.scanner_panel.manual_buy_requested.connect(self._on_manual_buy)
        self.portfolio_panel.row_clicked.connect(self._on_code_selected)

        if self._tg:
            self._tg.cmd_start.connect(lambda: self._on_auto_trade_toggle(True))
            self._tg.cmd_stop.connect(lambda: self._on_auto_trade_toggle(False))
            self._tg.cmd_status.connect(self._on_tg_status_requested)
            self.log_panel.append("[연결] 텔레그램 봇 연결됨")
'''

code = re.sub(
    r'    def _setup_modules\(self\) -> None:.*?    def _setup_controller_signals\(self\) -> None:',
    new_setup + '\n    def _setup_controller_signals(self) -> None:',
    code,
    flags=re.DOTALL
)

with open('ui/main_window.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Replacement done.")
