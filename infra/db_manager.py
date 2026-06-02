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
                        is_warmup INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 2. signals 테이블 (AI 학습용 모든 신호 저장)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        code TEXT,
                        name TEXT,
                        signal_type TEXT,
                        price INTEGER,
                        reason TEXT,
                        f_rsi REAL,
                        f_ema20_gap REAL,
                        f_pct_b REAL,
                        f_vol_surge REAL,
                        f_change_pct REAL,
                        f_strength REAL,
                        f_trend REAL,
                        f_price_mom REAL,
                        f_intra_pos REAL,
                        f_volatility REAL,
                        f_ma_align REAL,
                        f_rs_score REAL,
                        f_vwap_dist REAL,
                        f_mtf_15m_gap REAL,
                        f_mtf_60m_gap REAL,
                        f_hoga_ratio REAL,
                        f_candle_body REAL,
                        f_candle_upper_tail REAL,
                        f_candle_lower_tail REAL,
                        is_traded INTEGER DEFAULT 0,
                        is_warmup INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 기존 DB에 컬럼이 없을 경우 추가 (Migration)
                # trades 테이블 마이그레이션
                cursor.execute("PRAGMA table_info(trades)")
                trades_cols = [col[1] for col in cursor.fetchall()]
                trades_new_cols = [
                    ("trend_level_at_signal", "INTEGER"),  # 신호 당시 추세 레벨 (0~3)
                    ("vwap_at_signal", "REAL"),             # 신호 당시 VWAP
                    ("rs_score_at_signal", "REAL"),         # 신호 당시 RS 점수
                ]
                for col_name, col_type in trades_new_cols:
                    if col_name not in trades_cols:
                        cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
                        logger.info("[DatabaseManager] trades 신규 컬럼 추가: %s", col_name)

                # signals 테이블 마이그레이션 — 누락 컬럼 자동 추가
                cursor.execute("PRAGMA table_info(signals)")
                existing_cols = [col[1] for col in cursor.fetchall()]
                new_cols = [
                    ("f_price_mom",        "REAL"),
                    ("f_intra_pos",        "REAL"),
                    ("f_volatility",       "REAL"),
                    ("f_ma_align",         "REAL"),
                    ("f_rs_score",         "REAL"),
                    ("f_vwap_dist",        "REAL"),
                    ("f_mtf_15m_gap",      "REAL"),
                    ("f_mtf_60m_gap",      "REAL"),
                    ("f_hoga_ratio",       "REAL"),
                    ("f_candle_body",      "REAL"),
                    ("f_candle_upper_tail","REAL"),
                    ("f_candle_lower_tail","REAL"),
                    ("is_warmup",          "INTEGER"),
                    ("entry_candle_low",   "INTEGER"),
                    ("change_pct",         "REAL"),
                    ("is_warmup_signal",   "INTEGER"),
                    ("signal_price",       "INTEGER"),
                    ("eod_trade",          "INTEGER DEFAULT 0"),
                    ("candle_stop",        "INTEGER"),
                    ("near_daily_high",    "INTEGER DEFAULT 0"),
                    ("entry_gap_pct",      "REAL"),
                    ("entry_phase",        "INTEGER"),
                    ("gap_pct",            "REAL"),
                    ("bearish_high",       "REAL"),
                    ("op_current_level",   "INTEGER"),
                    ("op_max_level",       "INTEGER"),
                    ("op_volume_surge",    "REAL"),
                    ("op_mtf_strength",    "INTEGER"),
                    ("op_atr14",           "REAL"),
                    ("op_ema20",           "REAL"),
                ]
                for col_name, col_type in new_cols:
                    if col_name not in existing_cols:
                        cursor.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}")
                        logger.info("[DatabaseManager] 신규 컬럼 추가: %s", col_name)
                
                # 3. 인덱스 생성
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_code ON signals(code)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type)")
                
                conn.commit()
            logger.info("[DatabaseManager] DB 초기화 완료: %s", self.db_path)
        except Exception as e:
            logger.error("[DatabaseManager] DB 초기화 실패: %s", e)

    def upsert_trade(self, trade_key: str, data: dict):
        """매매 내역 Insert 또는 Update"""
        self.upsert_trades_batch([(trade_key, data)])

    def upsert_trades_batch(self, trade_list: list[tuple[str, dict]]):
        """매매 내역 대량 Insert 또는 Update (트랜잭션 활용)"""
        if not trade_list:
            return
            
        from contextlib import closing
        try:
            with closing(self._get_connection()) as conn:
                for trade_key, data in trade_list:
                    columns = list(data.keys())
                    placeholders = ", ".join(["?" for _ in columns])
                    update_stmt = ", ".join([f"{col}=excluded.{col}" for col in columns if col != 'trade_key'])
                    
                    query = f"""
                        INSERT INTO trades (trade_key, {", ".join(columns)})
                        VALUES (?, {placeholders})
                        ON CONFLICT(trade_key) DO UPDATE SET
                            {update_stmt},
                            updated_at=CURRENT_TIMESTAMP
                    """
                    params = [trade_key] + [data[col] for col in columns]
                    conn.execute(query, params)
                
                conn.commit()
                logger.debug("[DatabaseManager] %d건 배치 upsert 완료", len(trade_list))
        except Exception as e:
            logger.error("[DatabaseManager] upsert_trades_batch 실패: %s", e)

    def insert_signal(self, data: dict):
        """AI 학습용 신호 데이터 저장 — 테이블에 없는 컬럼은 자동 무시"""
        from contextlib import closing
        try:
            with closing(self._get_connection()) as conn:
                # 실제 테이블 컬럼 목록 조회 (스키마 불일치 방지)
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(signals)")
                valid_cols = {row[1] for row in cur.fetchall()}

                # 테이블에 있는 컬럼만 필터링
                filtered = {k: v for k, v in data.items() if k in valid_cols}
                if not filtered:
                    return

                columns = list(filtered.keys())
                placeholders = ", ".join(["?" for _ in columns])
                query = f"INSERT INTO signals ({', '.join(columns)}) VALUES ({placeholders})"
                params = [filtered[col] for col in columns]

                conn.execute(query, params)
                conn.commit()
        except Exception as e:
            logger.error("[DatabaseManager] insert_signal 실패: %s", e)

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
