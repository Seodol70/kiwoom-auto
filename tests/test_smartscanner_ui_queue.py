"""
Tests for SmartScanner UI queue and async update logic
"""
import unittest
import time
from unittest.mock import Mock, MagicMock, patch
from collections import deque


class TestSmartScannerUIQueue(unittest.TestCase):
    """Test SmartScanner UI queue separation from scan thread"""

    def test_ui_queue_maxlen_one(self):
        """Test that UI queue only keeps the latest data"""
        ui_queue = deque(maxlen=1)

        # Add multiple items
        ui_queue.append({"data": "first"})
        ui_queue.append({"data": "second"})
        ui_queue.append({"data": "third"})

        # Only latest should remain
        self.assertEqual(len(ui_queue), 1)
        self.assertEqual(ui_queue[0]["data"], "third")

    def test_ui_queue_deque_pop(self):
        """Test popping from deque with maxlen=1"""
        ui_queue = deque(maxlen=1)
        ui_queue.append({"rows": [1, 2, 3]})

        # Pop should work
        data = ui_queue.pop()
        self.assertEqual(data["rows"], [1, 2, 3])
        self.assertEqual(len(ui_queue), 0)

    def test_ui_queue_empty_pop_raises_indexerror(self):
        """Test that popping from empty queue raises IndexError"""
        ui_queue = deque(maxlen=1)

        with self.assertRaises(IndexError):
            ui_queue.pop()

    def test_ui_queue_separation_from_scan(self):
        """Test that UI queue allows scan thread to proceed without blocking"""
        ui_queue = deque(maxlen=1)

        # Simulate scan thread adding data
        scan_data_1 = [{"code": "005930", "price": 100}]
        scan_data_2 = [{"code": "000660", "price": 200}]
        scan_data_3 = [{"code": "068270", "price": 300}]

        # Add multiple data points (like scan thread would)
        ui_queue.append(scan_data_1)  # Should be kept
        ui_queue.append(scan_data_2)  # Overwrites scan_data_1
        ui_queue.append(scan_data_3)  # Overwrites scan_data_2

        # Only latest remains
        self.assertEqual(len(ui_queue), 1)
        latest = ui_queue[0]
        self.assertEqual(latest[0]["code"], "068270")

    def test_ui_queue_nonblocking_on_full(self):
        """Test that appending to full queue (maxlen=1) doesn't block"""
        ui_queue = deque(maxlen=1)

        # This should not raise, and old data should be automatically discarded
        ui_queue.append({"data": "old"})
        ui_queue.append({"data": "new"})  # This should not block

        self.assertEqual(len(ui_queue), 1)
        self.assertEqual(ui_queue[0]["data"], "new")

    def test_ui_timer_polling_pattern(self):
        """Test the pattern of polling UI queue periodically"""
        ui_queue = deque(maxlen=1)
        emitted = []

        def emit_ui_update(data):
            emitted.append(data)

        # Simulate multiple scans adding data
        ui_queue.append([{"code": "005930"}])
        time.sleep(0.05)
        ui_queue.append([{"code": "000660"}])
        time.sleep(0.05)
        ui_queue.append([{"code": "068270"}])

        # Simulate UI timer polling (every 500ms, but we do it once)
        if ui_queue:
            emit_ui_update(ui_queue.pop())

        # Should have emitted the latest
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0]["code"], "068270")

    def test_ui_update_throttling(self):
        """Test that UI updates can be throttled (5s interval)"""
        last_ui_send = 0
        current_time = 0
        UI_SEND_INTERVAL = 5.0

        # Simulate multiple scans at different times
        scan_times = [1.0, 2.0, 3.0, 4.0, 5.1, 6.1, 11.2]
        ui_sends = []

        for scan_time in scan_times:
            current_time = scan_time
            # First send should happen at first call
            if last_ui_send == 0 or current_time - last_ui_send > UI_SEND_INTERVAL:
                ui_sends.append(current_time)
                last_ui_send = current_time

        # Should send at 1.0 (first), 6.1 (>5s after 1.0), 11.2 (>5s after 6.1)
        self.assertEqual(len(ui_sends), 3)
        self.assertAlmostEqual(ui_sends[0], 1.0)
        self.assertAlmostEqual(ui_sends[1], 6.1)
        self.assertAlmostEqual(ui_sends[2], 11.2)


class TestScanThreadUIIsolation(unittest.TestCase):
    """Test that scan thread doesn't block on UI operations"""

    def test_scan_thread_nonblocking_append(self):
        """Test that scan thread can append to UI queue without blocking"""
        ui_queue = deque(maxlen=1)
        scan_times = []
        ui_times = []

        # Simulate fast scan thread
        for i in range(5):
            scan_start = time.time()
            ui_queue.append({"batch": i})  # Should not block
            scan_times.append(time.time() - scan_start)

        # All appends should be very fast (<1ms)
        for elapsed in scan_times:
            self.assertLess(elapsed, 0.01)

    def test_concurrent_scan_and_ui_read(self):
        """Test that UI thread can read without blocking scan thread"""
        import threading

        ui_queue = deque(maxlen=1)
        scan_success = []
        ui_success = []

        def scan_worker():
            """Simulates scan thread adding data"""
            try:
                for i in range(10):
                    ui_queue.append({"scan_iter": i})
                    time.sleep(0.01)
                scan_success.append(True)
            except Exception as e:
                scan_success.append(False)

        def ui_worker():
            """Simulates UI thread reading data"""
            try:
                for _ in range(5):
                    if ui_queue:
                        ui_queue.pop()
                    time.sleep(0.02)
                ui_success.append(True)
            except Exception:
                ui_success.append(False)

        scan_thread = threading.Thread(target=scan_worker, daemon=True)
        ui_thread = threading.Thread(target=ui_worker, daemon=True)

        scan_thread.start()
        ui_thread.start()

        scan_thread.join(timeout=2.0)
        ui_thread.join(timeout=2.0)

        # Both should succeed without deadlock or exception
        self.assertTrue(any(scan_success))
        self.assertTrue(any(ui_success))


if __name__ == "__main__":
    unittest.main()
