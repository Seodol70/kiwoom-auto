import sqlite3
import pandas as pd
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

class SignalValidator:
    """
    매수 신호의 신뢰성을 분석하는 도구.
    SQLite DB의 trades 테이블을 분석하여 승률, 손익비를 계산합니다.
    """

    def __init__(self, db_path: str = "data/trading.db"):
        self.db_path = db_path

    def analyze_performance(self):
        """신호 유형별 성과 분석 리포트 생성"""
        if not Path(self.db_path).exists():
            print(f"[ERROR] DB file not found: {self.db_path}")
            return

        conn = sqlite3.connect(self.db_path)
        try:
            # 완료된 거래 데이터 로드
            query = """
                SELECT 
                    signal_type, 
                    signal_price,
                    buy_fill_price,
                    sell_fill_price,
                    realized_pnl,
                    rs_score,
                    rsi_at_signal,
                    volume_ratio_at_signal,
                    final_status
                FROM trades
                WHERE final_status = 'COMPLETED'
            """
            df = pd.DataFrame(conn.execute(query).fetchall(), columns=[
                'signal_type', 'sig_price', 'buy_price', 'sell_price', 
                'pnl', 'rs_score', 'rsi', 'vol_ratio'
            ])

            if df.empty:
                print("[INFO] Insufficient data for analysis. (No COMPLETED trades found)")
                return

            # 수익률 계산
            df['return_pct'] = (df['sell_price'] - df['buy_price']) / df['buy_price'] * 100
            df['is_win'] = df['return_pct'] > 0

            print("\n" + "="*60)
            print("Signal Reliability Analysis Report")
            print("="*60)

            # 1. 신호 유형별 승률 및 수익률
            stats = df.groupby('signal_type').agg({
                'is_win': ['count', 'mean'],
                'return_pct': ['mean', 'std', 'max', 'min'],
                'pnl': 'sum'
            })
            stats.columns = ['Count', 'WinRate', 'AvgReturn', 'StdDev', 'Max', 'Min', 'TotalPnL']
            stats['WinRate'] = stats['WinRate'] * 100
            
            print("\n[1. Performance by Signal Type]")
            print(stats.to_string(formatters={'WinRate': '{:,.1f}%'.format, 'AvgReturn': '{:,.2f}%'.format}))

            # 2. RS 필터 영향 분석
            df['rs_bucket'] = pd.cut(df['rs_score'], bins=[-10, 0, 1, 3, 5, 20])
            rs_stats = df.groupby('rs_bucket', observed=True)['is_win'].mean() * 100
            print("\n[2. Win Rate by RS Score (Relative Strength)]")
            for bucket, wr in rs_stats.items():
                print(f"  - RS {bucket}: {wr:.1f}%")

            # 3. 거래량 급증 배수별 승률
            df['vol_bucket'] = pd.cut(df['vol_ratio'], bins=[0, 1, 2, 3, 5, 20])
            vol_stats = df.groupby('vol_bucket', observed=True)['is_win'].mean() * 100
            print("\n[3. Win Rate by Volume Ratio]")
            for bucket, wr in vol_stats.items():
                print(f"  - Vol Ratio {bucket}: {wr:.1f}%")

            # 4. 결론 및 제언
            print("\n[4. Analysis Conclusion]")
            best_sig = stats['WinRate'].idxmax()
            best_wr = stats.loc[best_sig, 'WinRate']
            print(f"  Best Signal: {best_sig} (Win Rate {best_wr:.1f}%)")
            
            # 위험 신호 경고
            low_wr_sigs = stats[stats['WinRate'] < 40].index.tolist()
            if low_wr_sigs:
                print(f"  WARNING: Low performance signals (Win Rate < 40%): {', '.join(low_wr_sigs)}")
            
            print("\n" + "="*60)

        finally:
            conn.close()

if __name__ == "__main__":
    validator = SignalValidator()
    validator.analyze_performance()
