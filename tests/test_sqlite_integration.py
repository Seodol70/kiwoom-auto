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
        # Flush to DB (buffer may not auto-flush with just 1 row)
        self.audit.flush_all()

        # DB 확인
        stats = self.db.get_summary_stats()
        # COMPLETED가 아니므로 stats에는 안 잡히지만, 테이블에는 있어야 함
        from contextlib import closing
        with closing(self.db._get_connection()) as conn:
            row = conn.execute("SELECT * FROM trades WHERE code='005930'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[4], "삼성전자")

        # 2. Buy Fill
        self.audit.log_buy_fill("005930", 10, 70000)

        # 3. Sell Fill
        self.audit.log_sell_fill("005930", 10, 71000, 70000, 10000)

        # Flush all pending rows to DB
        self.audit.flush_all()

        # 최종 DB 확인
        from contextlib import closing
        from trade_audit_logger import COLUMNS
        with closing(self.db._get_connection()) as conn:
            row = conn.execute("SELECT * FROM trades WHERE code='005930'").fetchone()
            realized_pnl_idx = COLUMNS.index("realized_pnl")
            final_status_idx = COLUMNS.index("final_status")
            self.assertEqual(row[realized_pnl_idx], 10000) # realized_pnl
            self.assertEqual(row[final_status_idx], "COMPLETED") # final_status

    def tearDown(self):
        if os.path.exists(self.test_db):
            os.remove(self.test_db)
        if os.path.exists(self.test_log_dir):
            shutil.rmtree(self.test_log_dir)

if __name__ == "__main__":
    unittest.main()
