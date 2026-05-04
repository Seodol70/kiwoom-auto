import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt5.QtWidgets import QApplication
from app.connection_watchdog import ConnectionWatchdog

def test_watchdog_logic():
    app = QApplication.instance() or QApplication([])
    
    mock_kiwoom = MagicMock()
    mock_login_mgr = MagicMock()
    mock_scanner = MagicMock()
    
    watchdog = ConnectionWatchdog(mock_kiwoom, mock_login_mgr, mock_scanner)
    
    # 1. Connection Lost Detection
    mock_kiwoom.is_connected.return_value = False
    
    lost_fired = False
    def on_lost():
        nonlocal lost_fired
        lost_fired = True
        
    watchdog.connection_lost.connect(on_lost)
    watchdog._on_check()
    
    assert lost_fired, "Connection lost signal should be fired"
    assert watchdog._is_lost is True
    assert not watchdog._check_timer.isActive(), "Check timer should stop on loss"
    
    # 2. Retry - Success
    mock_login_mgr.reconnect_silent.return_value = True
    
    recovered_fired = False
    def on_recovered():
        nonlocal recovered_fired
        recovered_fired = True
        
    watchdog.connection_recovered.connect(on_recovered)
    watchdog._on_retry()
    
    assert recovered_fired, "Connection recovered signal should be fired"
    assert watchdog._is_lost is False
    assert watchdog._check_timer.isActive(), "Check timer should restart on recovery"
    mock_scanner.watch_q.refresh.assert_called_once()
    
    # 3. Retry - Failure and Max Retry
    mock_kiwoom.is_connected.return_value = False
    watchdog._is_lost = False  # Reset for new simulation
    watchdog._on_check() # This will set _is_lost=True and stop the timer
    
    watchdog._retry_count = watchdog.MAX_RETRY - 1
    mock_login_mgr.reconnect_silent.return_value = False
    
    failed_fired = False
    def on_failed(msg):
        nonlocal failed_fired
        failed_fired = True
        
    watchdog.reconnect_failed.connect(on_failed)
    watchdog._on_retry()
    
    assert failed_fired, "Reconnect failed signal should be fired on max retry"
    assert not watchdog._check_timer.isActive(), "Check timer should stay stopped"

    print("[OK] ConnectionWatchdog logic test passed")

if __name__ == "__main__":
    try:
        test_watchdog_logic()
    except Exception as e:
        print(f"[FAIL] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
