import sqlite3
import os
import logging
from datetime import datetime
from threading import Lock

logger = logging.getLogger(__name__)

class DatabaseManager:
    """
    SQLite 데이터베이스 관리자 (싱글톤)
    매매 내역, 신호 로그 및 통계 데이터를 저장합니다.
    """
    _instance = None
    _lock = Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DatabaseManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_path="data/trading.db"):
        if self._initialized:
            return
        
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._initialized = True
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_db(self):
        """테이블 초기화"""
        from contextlib import closing
        try:
            with closing(self._get_connection()) as conn:
                cursor = conn.cursor()
                
                # 1. trades 테이블 (TradeAuditLogger 기반)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_key TEXT UNIQUE,
                        trade_date TEXT,
                        code TEXT,
                        name TEXT,
                        signal_type TEXT,
                        signal_time TEXT,
                        signal_price INTEGER,
                        signal_reason TEXT,
                        rsi_at_signal REAL,
                        ma_short_at_signal REAL,
                        ma_long_at_signal REAL,
                        ema_short_at_signal REAL,
                        ema_long_at_signal REAL,
                        chejan_strength_at_signal REAL,
                        volume_ratio_at_signal REAL,
                        change_pct_at_signal REAL,
                        trade_amount_at_signal INTEGER,
                        kospi_chg_at_signal REAL,
                        kosdaq_chg_at_signal REAL,
                        investor_score_at_signal INTEGER,
                        rs_score REAL,
                        buy_order_time TEXT,
                        buy_order_price INTEGER,
                        buy_order_qty INTEGER,
                        buy_fill_time TEXT,
                        buy_fill_price INTEGER,
                        buy_fill_qty INTEGER,
                        sell_decision_time TEXT,
                        sell_decision_price INTEGER,
                        sell_reason TEXT,
                        sell_order_time TEXT,
                        sell_fill_time TEXT,
                        sell_fill_price INTEGER,
                        sell_fill_qty INTEGER,
                        avg_buy_price INTEGER,
                        return_pct REAL,
                        realized_pnl INTEGER,
                        holding_minutes REAL,
                        final_status TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 2. 인덱스 생성
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date)")
                
                conn.commit()
            logger.info("[DatabaseManager] DB 초기화 완료: %s", self.db_path)
        except Exception as e:
            logger.error("[DatabaseManager] DB 초기화 실패: %s", e)

    def upsert_trade(self, trade_key: str, data: dict):
        """매매 내역 Insert 또는 Update"""
        from contextlib import closing
        try:
            columns = list(data.keys())
            placeholders = ", ".join(["?" for _ in columns])
            update_stmt = ", ".join([f"{col}=excluded.{col}" for col in columns if col != 'trade_key'])
            
            # trade_key가 이미 있으면 update, 없으면 insert (SQLite 3.24+)
            query = f"""
                INSERT INTO trades (trade_key, {", ".join(columns)})
                VALUES (?, {placeholders})
                ON CONFLICT(trade_key) DO UPDATE SET
                    {update_stmt},
                    updated_at=CURRENT_TIMESTAMP
            """
            
            params = [trade_key] + [data[col] for col in columns]
            
            with closing(self._get_connection()) as conn:
                conn.execute(query, params)
                conn.commit()
        except Exception as e:
            logger.error("[DatabaseManager] upsert_trade 실패 (key=%s): %s", trade_key, e)

    def get_summary_stats(self):
        """기본 통계 산출"""
        from contextlib import closing
        query = """
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as win_count,
                AVG(return_pct) as avg_return,
                SUM(realized_pnl) as total_pnl
            FROM trades
            WHERE final_status = 'COMPLETED'
        """
        try:
            with closing(self._get_connection()) as conn:
                conn.row_factory = sqlite3.Row
                return conn.execute(query).fetchone()
        except Exception as e:
            logger.error("[DatabaseManager] get_summary_stats 실패: %s", e)
            return None
