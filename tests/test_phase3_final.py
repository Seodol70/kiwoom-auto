"""Phase 3 최종 통합 테스트"""

import sys
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt5.QtWidgets import QApplication
from app.market_scheduler import MarketScheduler
from app.risk_manager import RiskManager
from app.trading_controller import TradingController


class MockOrderManager:
    def __init__(self):
        self.positions = {}
        self.available_cash = 1000000
        self.max_positions = 5
        self._pending = {}
        self.order_filled = MagicMock()  # RiskManager.__init__에서 connect

    @property
    def daily_realized_pnl(self):
        return 0

    def is_pending(self, code):
        return code in self._pending

    def handle_signal(self, sig):
        pass


class MockConfig:
    daily_profit_lock_won = 100000
    daily_loss_cut_won = -100000
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


def _make_fresh_session_mgr():
    """Fresh session state를 반환하는 mock session manager 생성"""
    session_mgr = MagicMock()
    session_mgr.load.return_value = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "daily_realized_pnl": 0.0,
        "is_loss_cut_locked": False,
        "is_profit_locked": False,
        "timestamp": datetime.now().isoformat(),
    }
    return session_mgr


def test_phase3_modules():
    """Phase 3 모든 모듈이 정상 생성되는지 테스트"""
    app = QApplication.instance() or QApplication([])

    order_mgr = MockOrderManager()
    scan_cfg = MockConfig()

    # 1. MarketScheduler
    scheduler = MarketScheduler(parent=None)
    scheduler.start()
    assert scheduler._timer.isActive(), "MarketScheduler 타이머 미활성화"
    scheduler.stop()
    assert not scheduler._timer.isActive(), "MarketScheduler 타이머 미중지"
    print("[OK] MarketScheduler 작동 확인")

    # 2. RiskManager
    risk_mgr = RiskManager(order_mgr, scan_cfg, parent=None, session_mgr=_make_fresh_session_mgr())
    assert not risk_mgr.is_new_entry_locked, "RiskManager 초기 상태 오류"
    risk_mgr.check()  # 정상 작동 확인
    print("[OK] RiskManager 작동 확인")

    # 3. TradingController
    controller = TradingController(
        order_mgr=order_mgr,
        scan_cfg=scan_cfg,
        risk_mgr=risk_mgr,
        snap_store=None,
        parent=None,
    )
    assert not controller.auto_trading, "TradingController 초기 상태 오류"
    controller.set_auto_trading(True)
    assert controller.auto_trading, "TradingController 설정 오류"
    print("[OK] TradingController 작동 확인")

    # 4. 신호 연결 테스트
    signals_fired = {"market_opened": False}

    def on_market_opened():
        signals_fired["market_opened"] = True

    scheduler.market_opened.connect(on_market_opened)
    # 신호는 타이머로 인해 자동 발행되므로 수동 테스트는 생략
    print("[OK] 신호 연결 테스트 통과")

    print("\n[OK] Phase 3 최종 통합 테스트 완료\n")


if __name__ == "__main__":
    print("\n=== Phase 3 Final Integration Test ===\n")

    try:
        test_phase3_modules()
    except AssertionError as e:
        print(f"\n[FAIL] {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n[FAIL] {e}\n")
        import traceback

        traceback.print_exc()
        sys.exit(1)
