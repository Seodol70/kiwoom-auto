import sys
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from order.order_manager import OrderManager, Position
from app.risk_manager import RiskManager
from scanner.config import SmartScannerConfig

def test_dynamic_sizing_equal():
    print("\n[Test] Dynamic Sizing: EQUAL")
    kiwoom = MagicMock()
    cfg = SmartScannerConfig()
    cfg.position_sizing_mode = "EQUAL"
    om = OrderManager(kiwoom)
    om.max_order_amount = 10_000_000
    om.max_order_amount = 10_000_000
    om._scan_cfg = cfg
    om.cash = 10_000_000
    om.max_positions = 5
    
    # Existing positions: 2
    om.positions = {
        "000001": Position(code="000001", name="A", qty=10, avg_price=10000, current_price=10000),
        "000002": Position(code="000002", name="B", qty=10, avg_price=10000, current_price=10000),
    }
    
    # Signal for code "000003" at price 50,000
    signal = MagicMock(code="000003", name="C", price=50000)
    signal.entry_candle_low = 0
    signal.trend_level = 0
    signal.trend_prev_level = 0
    signal.near_daily_high = False
    signal.custom_tp_pct = 0.0
    signal.eod_trade = False
    signal.entry_phase = 0
    
    # We need to mock _is_buy_allowed and other filters
    om._is_buy_allowed = MagicMock(return_value=True)
    om._kiwoom.get_stock_info = MagicMock(return_value={"change_pct": 5.0, "sector": "IT"})
    om.buy = MagicMock()
    
    om.handle_signal(signal)
    
    # Remaining slots: 5 - 2 = 3
    # Budget: 10,000,000 / 3 = 3,333,333
    # Qty: 3,333,333 // 50,000 = 66
    
    args, kwargs = om.buy.call_args
    qty = args[2]
    print(f"Calculated Qty: {qty}")
    assert qty == 66
    print("[OK] EQUAL sizing passed")

def test_dynamic_sizing_fixed():
    print("\n[Test] Dynamic Sizing: FIXED")
    kiwoom = MagicMock()
    cfg = SmartScannerConfig()
    cfg.position_sizing_mode = "FIXED"
    cfg.fixed_order_amount = 2_000_000
    om = OrderManager(kiwoom)
    om.max_order_amount = 10_000_000
    om._scan_cfg = cfg
    om.cash = 10_000_000
    
    signal = MagicMock(code="000003", name="C", price=50000)
    signal.entry_candle_low = 0
    signal.trend_level = 0
    signal.trend_prev_level = 0
    signal.near_daily_high = False
    signal.custom_tp_pct = 0.0
    signal.eod_trade = False
    signal.entry_phase = 0
    om._is_buy_allowed = MagicMock(return_value=True)
    om._kiwoom.get_stock_info = MagicMock(return_value={"change_pct": 5.0, "sector": "IT"})
    om.buy = MagicMock()
    
    om.handle_signal(signal)
    
    # Qty: 2,000,000 // 50,000 = 40
    args, kwargs = om.buy.call_args
    qty = args[2]
    print(f"Calculated Qty: {qty}")
    assert qty == 40
    print("[OK] FIXED sizing passed")

def test_dynamic_sizing_risk():
    print("\n[Test] Dynamic Sizing: RISK")
    kiwoom = MagicMock()
    cfg = SmartScannerConfig()
    cfg.position_sizing_mode = "RISK"
    cfg.risk_per_trade_pct = 1.0 # 1% of total equity
    cfg.jdm_stop_loss_pct = -2.0 # 2% SL (negative value)
    
    om = OrderManager(kiwoom)
    om.max_order_amount = 10_000_000
    om._scan_cfg = cfg
    om.cash = 10_000_000
    # No positions, total_equity = 10,000,000
    # Risk amount = 10,000,000 * 0.01 = 100,000
    
    # Price 50,000, SL at 49,000 (2% is 1,000)
    # Risk per share = 1,000
    # Qty = 100,000 // 1,000 = 100
    
    signal = MagicMock(code="000003", name="C", price=50000)
    signal.entry_candle_low = 0
    signal.trend_level = 0
    signal.trend_prev_level = 0
    signal.near_daily_high = False
    signal.custom_tp_pct = 0.0
    signal.eod_trade = False
    signal.entry_phase = 0
    om._is_buy_allowed = MagicMock(return_value=True)
    om._kiwoom.get_stock_info = MagicMock(return_value={"change_pct": 5.0, "sector": "IT"})
    om.buy = MagicMock()
    
    om.handle_signal(signal)
    
    args, kwargs = om.buy.call_args
    qty = args[2]
    print(f"Calculated Qty: {qty}")
    assert qty == 100
    print("[OK] RISK sizing passed")

def test_portfolio_loss_cut():
    print("\n[Test] RiskManager: Portfolio Loss Cut")
    om = MagicMock()
    om.daily_realized_pnl = 0
    om.positions = {}
    cfg = SmartScannerConfig()
    cfg.max_portfolio_unrealized_loss_pct = 5.0

    # Positions with 6% total loss
    p1 = MagicMock(avg_price=10000, qty=100, pnl=-60000) # -6%
    om.positions = {"000001": p1}

    # Mock AppState (new signature)
    app_state = MagicMock()
    app_state.profit_locked = False
    app_state.loss_cut_locked = False
    app_state.daily_realized_pnl = 0.0

    rm = RiskManager(om, cfg, app_state=app_state)
    rm.daily_loss_cut = MagicMock()

    rm.check()

    rm.daily_loss_cut.emit.assert_called_once()
    print("[OK] Portfolio loss cut trigger passed")

def test_cooling_off():
    print("\n[Test] RiskManager: Cooling-off")
    om = MagicMock()
    om.daily_realized_pnl = 0
    cfg = SmartScannerConfig()
    cfg.consecutive_loss_limit = 2
    cfg.cooling_off_minutes = 10

    # Mock AppState (new signature)
    app_state = MagicMock()
    app_state.profit_locked = False
    app_state.loss_cut_locked = False
    app_state.daily_realized_pnl = 0.0

    rm = RiskManager(om, cfg, app_state=app_state)

    # Simulate 1st loss
    rm._on_order_filled({"side": "매도체결", "realized_pnl": -1000})
    assert rm.is_new_entry_locked == False

    # Simulate 2nd loss
    rm._on_order_filled({"side": "매도체결", "realized_pnl": -500})
    assert rm.is_new_entry_locked == True
    assert rm._cooling_off_until is not None
    print("[OK] Cooling-off period activated")

if __name__ == "__main__":
    test_dynamic_sizing_equal()
    test_dynamic_sizing_fixed()
    test_dynamic_sizing_risk()
    test_portfolio_loss_cut()
    test_cooling_off()
