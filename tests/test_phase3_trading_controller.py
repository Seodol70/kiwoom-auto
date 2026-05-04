"""Phase 3-3: TradingController 청산 로직 통합 테스트

Phase 5에서 _check_hard_stop / _check_stop_loss / _check_trail_stop 이
TradingController에서 제거되고 ExitStrategy.should_exit()로 통합됨.
해당 테스트들은 ExitStrategy를 직접 호출하도록 업데이트됨.
"""

import sys
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt5.QtWidgets import QApplication
from app.trading_controller import TradingController, ExitContext
from app.strategy import ExitStrategy


class MockPosition:
    """테스트용 Position Mock"""

    def __init__(
        self,
        code: str,
        name: str,
        qty: int,
        avg_price: int,
        current_price: int,
    ):
        self.code = code
        self.name = name
        self.qty = qty
        self.avg_price = avg_price
        self.current_price = current_price
        self.peak_price = current_price
        self.entry_time = None
        self.candle_stop_price = 0
        self.trend_level = 0

    @property
    def price_change_pct_vs_avg(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price * 100


class MockOrderManager:
    """테스트용 OrderManager Mock"""

    def __init__(self):
        self.positions = {}
        self._pending = {}
        self.available_cash = 10000000
        self.max_positions = 5

    def is_pending(self, code: str) -> bool:
        return code in self._pending

    def mark_stop_loss(self, code: str):
        pass

    def force_exit(self, code: str, name: str, qty: int, reason: str = ""):
        pass

    def sell(self, code: str, name: str, qty: int, price: int = 0):
        pass

    def handle_signal(self, sig):
        pass


class MockConfig:
    """테스트용 Config Mock"""

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


class MockRiskManager:
    """테스트용 RiskManager Mock"""

    @property
    def is_new_entry_locked(self):
        return False

    @property
    def is_daily_loss_cut_done(self):
        return False


@dataclass
class MockSignal:
    """테스트용 Signal Mock"""

    code: str
    price: int
    qty: int
    name: str = "테스트종목"
    signal_type: str = "JDM_ENTRY"
    reason: str = "테스트"
    sector: str = ""


def test_hard_stop():
    """Hard Stop 청산 테스트 — ExitStrategy.should_exit() 경유"""
    app = QApplication.instance() or QApplication([])

    scan_cfg = MockConfig()
    es = ExitStrategy(scan_cfg=scan_cfg)
    ctx = ExitContext(
        sl_pct=-1.2, trail_activation=1.0,
        trail_tier1=1.5, trail_tier2=2.5, trail_tier3=3.5,
        time_cut_min=25, partial_profit_pct=3.0, atr_trail_enabled=False,
    )

    # -2.5% 손실 → hard_stop_pct(-2.0%) 이하
    pos = MockPosition(code="005930", name="삼성전자", qty=10, avg_price=100000, current_price=97500)
    should_exit, reason = es.should_exit(pos, ctx)
    assert should_exit, "Hard Stop이 발동되지 않음"
    assert "Hard Stop" in reason
    print("[OK] Hard Stop 테스트 통과")


def test_stop_loss():
    """손절 청산 테스트 — ExitStrategy.should_exit() 경유"""
    app = QApplication.instance() or QApplication([])

    scan_cfg = MockConfig()
    es = ExitStrategy(scan_cfg=scan_cfg)
    ctx = ExitContext(
        sl_pct=-1.2, trail_activation=1.0,
        trail_tier1=1.5, trail_tier2=2.5, trail_tier3=3.5,
        time_cut_min=25, partial_profit_pct=3.0, atr_trail_enabled=False,
    )

    # -1.5% 손실 → sl_pct(-1.2%) 이하
    pos = MockPosition(code="005930", name="삼성전자", qty=10, avg_price=100000, current_price=98500)
    should_exit, reason = es.should_exit(pos, ctx)
    assert should_exit, "손절이 발동되지 않음"
    assert "Stop Loss" in reason
    print("[OK] 손절 테스트 통과")


def test_trail_stop():
    """트레일 스탑 청산 테스트 — ExitStrategy.should_exit() 경유"""
    app = QApplication.instance() or QApplication([])

    scan_cfg = MockConfig()
    es = ExitStrategy(scan_cfg=scan_cfg)
    ctx = ExitContext(
        sl_pct=-1.2, trail_activation=1.0,
        trail_tier1=1.5, trail_tier2=2.5, trail_tier3=3.5,
        time_cut_min=25, partial_profit_pct=3.0, atr_trail_enabled=False,
    )

    # 진입가 100,000 / peak 103,000 (+3.0%) / 현재가 99,200
    # 고점 대비 하락 = (103000-99200)/103000 = 3.69% > trail_pct_tier3(3.5%)
    pos = MockPosition(code="005930", name="삼성전자", qty=10, avg_price=100000, current_price=99200)
    pos.peak_price = 103000
    should_exit, reason = es.should_exit(pos, ctx)
    assert should_exit, "트레일 스탑이 발동되지 않음"
    assert "Trail Stop" in reason
    print("[OK] 트레일 스탑 테스트 통과")


def test_signal_filter_no_positions():
    """신호 필터: 포지션 5개 풀 테스트"""
    app = QApplication.instance() or QApplication([])

    order_mgr = MockOrderManager()
    scan_cfg = MockConfig()
    risk_mgr = MockRiskManager()

    controller = TradingController(
        order_mgr=order_mgr,
        scan_cfg=scan_cfg,
        risk_mgr=risk_mgr,
        parent=None,
    )

    # 포지션 5개 채우기
    for i in range(5):
        pos = MockPosition(
            code=f"00{1000+i}",
            name=f"종목{i}",
            qty=10,
            avg_price=100000,
            current_price=105000,
        )
        order_mgr.positions[f"00{1000+i}"] = pos

    # 자동매매 ON
    controller.set_auto_trading(True)

    # 6번째 신호 필터
    sig = MockSignal(code="006000", price=100000, qty=1)
    result = controller.handle_signal(sig)

    assert not result, "포지션 5개 풀 상태에서 신호가 통과됨"
    print("[OK] 포지션 풀 필터 테스트 통과")


if __name__ == "__main__":
    print("\n=== Phase 3-3: TradingController Test Start ===\n")

    try:
        test_hard_stop()
        test_stop_loss()
        test_trail_stop()
        test_signal_filter_no_positions()

        print("\n[OK] All TradingController tests passed!\n")
    except AssertionError as e:
        print(f"\n[FAIL] Assertion failed: {e}\n")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[FAIL] Test error: {e}\n")
        import traceback

        traceback.print_exc()
        sys.exit(1)
