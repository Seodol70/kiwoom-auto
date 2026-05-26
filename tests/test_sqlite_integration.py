import unittest
import os
import shutil
from infra.db_manager import DatabaseManager
from trade_audit_logger import TradeAuditLogger
from datetime import datetime

class TestSQLiteIntegration(unittest.TestCase):
    def setUp(self):
        # 테스트용 별도 DB 경로
        self.test_db = "data/test_trading.db"
        self.test_log_dir = "logs_test"
        
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        if os.path.exists(self.test_log_dir):
            shutil.rmtree(self.test_log_dir)
            
        self.db = DatabaseManager(db_path=self.test_db)
        self.audit = TradeAuditLogger(log_dir=self.test_log_dir, db_manager=self.db)

    def test_upsert_flow(self):
        # 1. Signal Log
        class MockSignal:
            code = "005930"
            name = "삼성전자"
            signal_type = "TEST_SIG"
            price = 70000
            reason = "Test Reason"
        
        class MockSnap:
            chejan_strength = 110.0
            change_pct = 1.5
            trade_amount = 1000000000
            investor_score = 1
            closes_1min = [69000, 69500, 70000]
            volumes_1min = [100, 150, 200]

        self.audit.log_signal(MockSignal(), MockSnap())

        # [FIX 2026-05-26] 중간 flush_all 제거.
        # log_signal은 즉시 _flush_row()를 호출하므로 _write_buffer에 SIGNAL_ONLY 행이 들어가지만,
        # _pending_rows에서는 _flush_row()가 row를 유지한다 (sell_fill까지 누적 가능).
        # 만약 여기서 flush_all() 호출 시 _pending_rows가 비워져 후속 buy_fill/sell_fill이 무력화됨.
        # → 신호→매수→매도 전 과정 끝낸 뒤 한 번만 flush_all() 호출하는 게 올바른 시퀀스.

        # 2. Buy Fill
        self.audit.log_buy_fill("005930", 10, 70000)

        # 3. Sell Fill
        self.audit.log_sell_fill("005930", 10, 71000, 70000, 10000)

        # Flush all pending rows to DB
        self.audit.flush_all()

        # DB 확인 — name 검증
        from contextlib import closing
        with closing(self.db._get_connection()) as conn:
            row = conn.execute("SELECT name FROM trades WHERE code='005930'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "삼성전자")

        # 최종 DB 확인 — 컬럼명으로 직접 SELECT (위치 기반 인덱싱 회피)
        from contextlib import closing
        with closing(self.db._get_connection()) as conn:
            row = conn.execute(
                "SELECT realized_pnl, final_status FROM trades WHERE code='005930'"
            ).fetchone()
            self.assertEqual(row[0], 10000)        # realized_pnl
            self.assertEqual(row[1], "COMPLETED")  # final_status

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        if os.path.exists(self.test_log_dir):
            shutil.rmtree(self.test_log_dir)

if __name__ == "__main__":
    unittest.main()
