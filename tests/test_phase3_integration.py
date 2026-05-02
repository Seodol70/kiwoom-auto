"""Phase 3 Application Layer 통합 테스트"""

import sys
from datetime import datetime, time
from pathlib import Path

# 테스트 코드는 프로젝트 루트에서 실행되므로 경로 설정
sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

from app.market_scheduler import MarketScheduler
from app.risk_manager import RiskManager
from app.trading_controller import TradingController, ExitContext


def test_market_scheduler_signals():
    """MarketScheduler 신호 발행 테스트"""
    app = QApplication.instance() or QApplication([])

    scheduler = MarketScheduler(parent=None)

    # 신호 수신 확인용 플래그
    signals_received = {}

    def on_signal(sig_name):
        signals_received[sig_name] = True

    # 모든 신호 연결
    scheduler.market_opened.connect(lambda: on_signal("market_opened"))
    scheduler.phase1_cutoff.connect(lambda: on_signal("phase1_cutoff"))
    scheduler.phase1_trail.connect(lambda: on_signal("phase1_trail"))
    scheduler.overnight_auto_enabled.connect(lambda: on_signal("overnight_auto_enabled"))
    scheduler.market_closing.connect(lambda: on_signal("market_closing"))
    scheduler.day_reset.connect(lambda: on_signal("day_reset"))

    # 스케줄러 시작
    scheduler.start()
    assert scheduler._timer.isActive(), "스케줄러 타이머가 활성화되지 않음"

    # 스케줄러 중지
    scheduler.stop()
    assert not scheduler._timer.isActive(), "스케줄러 타이머가 정지되지 않음"

    print("[OK] MarketScheduler 신호 테스트 통과")


def test_exit_context_dataclass():
    """ExitContext 데이터클래스 테스트"""
    ctx = ExitContext(
        sl_pct=-1.2,
        trail_activation=1.0,
        trail_tier1=1.5,
        trail_tier2=2.5,
        trail_tier3=3.5,
        time_cut_min=25,
        partial_profit_pct=3.0,
        atr_trail_enabled=False,
    )

    assert ctx.sl_pct == -1.2
    assert ctx.trail_activation == 1.0
    assert ctx.trail_tier1 == 1.5
    assert ctx.trail_tier2 == 2.5
    assert ctx.trail_tier3 == 3.5
    assert ctx.time_cut_min == 25
    assert ctx.partial_profit_pct == 3.0
    assert ctx.atr_trail_enabled is False

    print("[OK] ExitContext 테스트 통과")


def test_trading_controller_init():
    """TradingController 초기화 테스트"""
    app = QApplication.instance() or QApplication([])

    # Mock OrderManager (최소한의 인터페이스)
    class MockOrderManager:
        def __init__(self):
            self.positions = {}
            self.available_cash = 1000000

    # Mock SmartScannerConfig
    class MockConfig:
        hard_stop_pct = -2.0
        stop_loss_pct = -1.2
        trail_activation_pct = 1.0
        trail_pct_tier1 = 1.5
        trail_pct_tier2 = 2.5
        trail_tier1_max = 1.5
        trail_tier2_max = 2.5
        trail_pct_tier3 = 3.5
        time_cut_minutes = 25
        strong_trend_hold_level = 3
        strong_trend_timecut_exempt = True
        trend_protect_enabled = True

    # Mock RiskManager
    class MockRiskManager:
        @property
        def is_new_entry_locked(self):
            return False

        @property
        def is_daily_loss_cut_done(self):
            return False

    order_mgr = MockOrderManager()
    scan_cfg = MockConfig()
    risk_mgr = MockRiskManager()

    controller = TradingController(
        order_mgr=order_mgr,
        scan_cfg=scan_cfg,
        risk_mgr=risk_mgr,
        parent=None,
    )

    assert controller._order_mgr is order_mgr
    assert controller._scan_cfg is scan_cfg
    assert controller._risk_mgr is risk_mgr
    assert not controller._auto_trading

    # 자동매매 토글
    controller.set_auto_trading(True)
    assert controller.auto_trading is True

    controller.set_auto_trading(False)
    assert controller.auto_trading is False

    print("[OK] TradingController 초기화 테스트 통과")


if __name__ == "__main__":
    print("\n=== Phase 3 Integration Test Start ===\n")

    try:
        test_market_scheduler_signals()
        test_exit_context_dataclass()
        test_trading_controller_init()

        print("\n[OK] All tests passed!\n")
    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}\n")
        import traceback

        traceback.print_exc()
        sys.exit(1)
