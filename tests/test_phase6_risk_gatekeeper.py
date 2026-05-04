import pytest
from unittest.mock import MagicMock
from datetime import datetime
from PyQt5.QtWidgets import QApplication
from app.state import AppState
from app.risk_manager import RiskManager
from order.order_manager import OrderManager
from ui.signal_manager import SignalManager

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


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

def test_risk_lock_integration(qapp):
    # 1. Setup Mock Objects
    state = AppState()
    win = MagicMock()
    win.state = state
    
    # Mock OrderManager
    order_mgr = MagicMock(spec=OrderManager)
    order_mgr.daily_realized_pnl = 0
    order_mgr.positions = {}
    win.order_mgr = order_mgr
    
    # Mock Config
    scan_cfg = MagicMock()
    scan_cfg.daily_profit_lock_won = 100_000
    scan_cfg.daily_loss_cut_won = 50_000
    win._scan_cfg = scan_cfg
    
    # 2. Setup RiskManager
    rm = RiskManager(order_mgr=order_mgr, scan_cfg=scan_cfg, session_mgr=_make_fresh_session_mgr())
    win.risk_manager = rm
    
    # Mock TradingController
    tc = MagicMock()
    win.trading_controller = tc
    
    # 3. Setup SignalManager to bind RiskManager -> AppState
    sig_mgr = SignalManager(win)
    sig_mgr.bind_all()
    
    # Verify risk_locked is False initially
    assert state.risk_locked is False
    
    # 4. Trigger Loss Cut
    order_mgr.daily_realized_pnl = -60_000 # Exceeds loss_cut_won(50k)
    rm.check()
    
    # 5. Check if AppState.risk_locked is True via SignalManager binding
    # RiskManager.daily_loss_cut -> SignalManager -> AppState.risk_locked = True
    assert state.risk_locked is True

def test_order_manager_gatekeeper_integration(qapp):
    # 1. Setup
    state = AppState()
    kiwoom = MagicMock()
    order_mgr = OrderManager(kiwoom)
    order_mgr.set_state(state)
    
    # Initially allowed
    state.risk_locked = False
    state.update_market_data(2500, 0, 800, 0, False) # No crash
    
    # Mock stock info for universe filter
    kiwoom._ocx.dynamicCall.return_value = "정상"
    
    # 2. Test Allowed Case
    # We need to bypass universe filter by providing a 'pure equity' name
    from scanner.universe import is_pure_equity_name
    assert order_mgr._is_buy_allowed("005930", "삼성전자") is True
    
    # 3. Test Risk Locked Case
    state.risk_locked = True
    assert order_mgr._is_buy_allowed("005930", "삼성전자") is False
    
    # 4. Test Market Crash Case
    state.risk_locked = False
    state.update_market_data(2500, 0, 800, 0, True) # Crash!
    assert order_mgr._is_buy_allowed("005930", "삼성전자") is False
