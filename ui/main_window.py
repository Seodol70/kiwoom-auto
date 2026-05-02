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






# UI Components
from ui.components.common import _hline, _NoWheelSpinBox, _NoWheelDoubleSpinBox
from ui.components.header_bar import HeaderBar, ManualBuyDialog
from ui.components.scanner_panel import ScannerPanel
from ui.components.chart_panel import ChartPanel
from ui.components.portfolio_panel import PortfolioPanel
from ui.components.investor_panel import InvestorPanel, ScanStatusBar
from ui.components.log_panel import ScannerLogHandler, SysLogQtHandler, LogPanel






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


        from engine.workers import ScannerWorker, PortfolioWorker
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


        # TradingController → 청산 판정 로그
        self.trading_controller.log_message.connect(self.log_panel.append)


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


        from app.background_tasks import SystemMonitor
        self.sys_monitor = SystemMonitor(self)
        self.sys_monitor.connection_check_requested.connect(self._check_connection)
        self.sys_monitor.news_drain_requested.connect(self._drain_news_queue)
        self.sys_monitor.cleanup_requested.connect(self._cleanup_memory)
        self.sys_monitor.start()


        # 지수 급락 감지 (60초마다) — 헤더 지수 표시 + 급락 감지
        self._crash_check_timer = QTimer(self)
        self._crash_check_timer.timeout.connect(self._check_market_crash)
        QTimer.singleShot(35_000, lambda: self._crash_check_timer.start(60_000))  # 35s 뒤 시작


        # opt10030 주기 스캔 (1분마다) — 메인 스레드에서 호출 (Kiwoom TR은 메인 스레드만 지원)
        # 타임아웃 2초로 설정하여 응답 없으면 빨리 폴백
        self._scan_refresh_timer = QTimer(self)
        self._scan_refresh_timer.timeout.connect(self._run_scanner_scan)
        # 장 시작 전까지는 타이머만 등록, start_after_login 후 가동




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
        self.market_scheduler.stop()
        if hasattr(self, "sys_monitor"):
            self.sys_monitor.stop()
        self._balance_timer.stop()
        self._crash_check_timer.stop()
        self._chart_timer.stop()
        self._scan_refresh_timer.stop()
        self._tg_report_timer.stop()
        if hasattr(self, "_news_analyzer"):
            self._news_analyzer.stop()
        if hasattr(self, "_health_monitor"):
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
        if hasattr(self, 'trading_controller'):
            self.trading_controller.check_market_crash()


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
        if hasattr(self, 'trading_controller'):
            self.trading_controller.check_overnight_gap()


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
        if hasattr(self, 'trading_controller'):
            self.trading_controller.liquidate_phase1_positions(forced)




    def _liquidate_all_positions(self) -> None:
        if hasattr(self, 'trading_controller'):
            self.trading_controller.liquidate_all_positions()




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
            self, '실전 전환', '실전 모드로 전환하려면 재시작이 필요합니다.\n로그인 캐시를 삭제하고 종료하시겠습니까?',
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
