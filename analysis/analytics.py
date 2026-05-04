import sqlite3
import pandas as pd
import os
import logging
from infra.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

class TradeAnalytics:
    """
    SQLite 데이터를 기반으로 매매 성과를 분석합니다.
    """
    def __init__(self, db_manager=None):
        self.db = db_manager or DatabaseManager()

    def get_trades_df(self):
        """전체 매매 내역을 DataFrame으로 반환"""
        query = "SELECT * FROM trades WHERE final_status = 'COMPLETED'"
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                return pd.read_sql_query(query, conn)
        except Exception as e:
            logger.error("[Analytics] DataFrame 로드 실패: %s", e)
            return pd.DataFrame()

    def report_by_signal_type(self):
        """신호 유형별 성과 분석"""
        df = self.get_trades_df()
        if df.empty:
            return "매매 데이터가 없습니다."
        
        # 승리 여부 계산
        df['is_win'] = df['realized_pnl'] > 0
        
        stats = df.groupby('signal_type').agg({
            'id': 'count',
            'is_win': 'sum',
            'return_pct': ['mean', 'max', 'min'],
            'realized_pnl': 'sum'
        })
        
        stats.columns = ['Trade Count', 'Win Count', 'Avg Return(%)', 'Max Return(%)', 'Min Return(%)', 'Total PnL']
        stats['Win Rate(%)'] = (stats['Win Count'] / stats['Trade Count'] * 100).round(2)
        
        return stats

    def report_by_hour(self):
        """시간대별 성과 분석"""
        df = self.get_trades_df()
        if df.empty:
            return "매매 데이터가 없습니다."
        
        # signal_time에서 시간(HH) 추출
        df['hour'] = df['signal_time'].str.split(':').str[0]
        
        stats = df.groupby('hour').agg({
            'id': 'count',
            'return_pct': 'mean',
            'realized_pnl': 'sum'
        })
        stats.columns = ['Trade Count', 'Avg Return(%)', 'Total PnL']
        return stats

if __name__ == "__main__":
    # 간단한 실행 테스트
    analytics = TradeAnalytics()
    print("=== 신호별 성과 ===")
    print(analytics.report_by_signal_type())
    print("\n=== 시간대별 성과 ===")
    print(analytics.report_by_hour())
