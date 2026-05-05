from __future__ import annotations

import logging
from typing import Optional

from PyQt5.QtCore import QObject

from auth.login_manager import LoginManager
from order.order_manager import OrderManager
from scanner.smart_scanner import SmartScanner, SmartScannerConfig, SnapshotStore
from scanner.news_analyzer import NewsAnalyzer
from app.market_scheduler import MarketScheduler
from app.risk_manager import RiskManager
from app.state import AppState
from app.trading_controller import TradingController
from analysis.health_monitor import HealthMonitor
from trade_audit_logger import TradeAuditLogger
from app.config_manager import config_manager as cfg

logger = logging.getLogger(__name__)

class ApplicationContext(QObject):
    """
    백엔드 모듈 조립(Bootstrap) 및 관리를 담당하는 애플리케이션 컨텍스트.
    UI 클래스(MainWindow)는 화면 표시에만 집중하고,
    비즈니스 로직과 객체 수명 주기는 여기서 관리합니다.
    """
    def __init__(self, kiwoom, parent=None):
        super().__init__(parent)
        self.kiwoom = kiwoom
        
        # ── 전역 상태 관리자 (Single Source of Truth) ──
        self.state = AppState()
        
        # 기본 스토어 및 로거
        self.audit = TradeAuditLogger(log_dir="logs")
        self.snap_store = SnapshotStore()
        
        # ── LoginManager ──
        self.login_mgr = LoginManager(self.kiwoom, parent=self)
        
        # ── OrderManager ──
        self.order_mgr = OrderManager(
            self.kiwoom,
            max_positions=cfg.get("max_positions", 5),
            parent=self
        )
        self.order_mgr.set_state(self.state) # AppState 주입
        self.order_mgr.set_snapshot_store(self.snap_store)
        self.order_mgr._audit = self.audit
        self.kiwoom._on_order_msg_cb = self.order_mgr.on_order_msg
        
        # ── SmartScanner Config ──
        self.scan_cfg = SmartScannerConfig.from_adaptive("params/adaptive_params.json")
        _yosep_preset = str(cfg.get("yosep_preset", "") or "").strip().lower()
        if _yosep_preset:
            self.scan_cfg.apply_yosep_preset(_yosep_preset)
        self.scan_cfg.max_change_pct = float(cfg.get("max_change_pct", 15.0))
        self.scan_cfg.signal_cooldown_sec = float(cfg.get("signal_cooldown_sec", 45.0))
        self.scan_cfg.index_block_pct = float(cfg.get("market_index_block_pct", -1.5))
        
        _wpm = cfg.get("watch_pool_max")
        if _wpm is not None:
            wpm = max(1, int(_wpm))
            self.scan_cfg.watch_pool_max = wpm
            self.scan_cfg.realtime_sub_max = wpm
            self.scan_cfg.display_top_n = wpm
            
        # ── SmartScanner ──
        self.smart_scanner = SmartScanner(self.kiwoom, self.scan_cfg)
        self.smart_scanner.store = self.snap_store
        self.smart_scanner.app_context = self # [NEW] 지수 가속도 등 피처 계산을 위해 컨텍스트 주입
        
        # ── HealthMonitor ──
        def _on_freeze_handler():
            force_fn = getattr(self.kiwoom, "force_unfreeze", None)
            if force_fn:
                force_fn()

        self.health_monitor = HealthMonitor(
            scan_cfg=self.scan_cfg,
            on_param_relax=lambda *args: None,  # UI 델리게이트 필요
            on_freeze=_on_freeze_handler,
            on_reconnect=self.login_mgr.reconnect_silent
        )

        # ── Application Layer ──
        self.market_scheduler = MarketScheduler(self)
        self.risk_manager = RiskManager(self.order_mgr, self.scan_cfg, self, app_state=self.state)
        self.trading_controller = TradingController(
            self.kiwoom, self.order_mgr, self.scan_cfg, self.risk_manager,
            smart_scanner=self.smart_scanner,
            snap_store=self.snap_store,
            health_monitor=self.health_monitor,
            parent=self
        )

        # ── SmartScanner ↔ OrderManager 연결 (의존성 주입) ──
        self.smart_scanner._order_mgr = self.order_mgr
        self.order_mgr.on_position_opened = self.smart_scanner.register_code_realtime
        self.order_mgr.on_position_closed = self.smart_scanner.unregister_code_realtime

        # ── TradingController 상태 주입 (AppState) ──
        self.trading_controller._ctx = self.state

        # ── 텔레그램 ──
        self.tg_bot = None
        if cfg.get("TELEGRAM", {}).get("enabled") and cfg.get("TELEGRAM", {}).get("token"):
            try:
                # TelegramBot 임포트는 지연
                from telegram_bot import TelegramBot
                self.tg_bot = TelegramBot(cfg.TELEGRAM["token"], cfg.TELEGRAM["chat_id"], parent=self)
            except Exception as e:
                logger.warning("[텔레그램] 봇 초기화 실패: %s", e)
        
        # ── 시그널 연결 (모듈 간 연동) ──
        # 로그인 성공 시 계좌번호를 OrderManager에 전달
        self.login_mgr.login_success.connect(lambda acc, mode: self.order_mgr.set_account(acc))
        
        # 실시간 가격 업데이트를 OrderManager에 전달 (손절/익절 실시간 감시용)
        self.smart_scanner.price_updated.connect(self.order_mgr._on_price_updated)
        
        # [초기화] 설정 파일에 계좌번호가 명시되어 있으면 미리 세팅 (다이얼로그 스킵용)
        _conf_acc = cfg.ACCOUNT.get("number", "")
        if _conf_acc:
            self.order_mgr.set_account(_conf_acc)
                
    def start_services(self):
        """백그라운드 서비스 시작"""
        if self.tg_bot:
            self.tg_bot.start()
        self.market_scheduler.start()
