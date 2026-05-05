import sqlite3
import pandas as pd
import numpy as np
import os
import joblib
import logging
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from infra.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

class MLTrainer:
    """
    매매 내역을 기반으로 승률 예측 모델을 학습합니다.
    """
    def __init__(self, db_path="data/trading.db", model_dir="models"):
        self.db_path = db_path
        self.model_dir = model_dir
        os.makedirs(model_dir, exist_ok=True)
        
        # 학습에 사용할 특성(Features) 정의
        self.feature_cols = [
            "f_rsi", "f_ema20_gap", "f_pct_b", "f_vol_surge", 
            "f_change_pct", "f_strength", "f_trend",
            "f_price_mom", "f_intra_pos", "f_volatility", "f_ma_align", "f_rs_score",
            "f_vwap_dist", "f_mtf_15m_gap", "f_mtf_60m_gap",
            "f_hoga_ratio", "f_candle_body", "f_candle_upper_tail", "f_candle_lower_tail"
        ]

    def load_data(self):
        """DB에서 학습 데이터 로드"""
        if not os.path.exists(self.db_path):
            return pd.DataFrame()
            
        # signals 테이블에서 데이터 로드 (결과값은 추후 라벨링 로직 필요)
        query = "SELECT * FROM signals"
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query(query, conn)
                return df
        except Exception as e:
            logger.error("[MLTrainer] 데이터 로드 실패: %s", e)
            return pd.DataFrame()

    def preprocess(self, df):
        """데이터 전처리"""
        if df.empty:
            return None, None
            
        # 필요한 컬럼만 추출 및 결측치 처리
        X = df[self.feature_cols].copy()
        for col in self.feature_cols:
            X[col] = pd.to_numeric(X[col], errors='coerce')
        X = X.fillna(X.mean())
        
        # 타겟 설정 (수익률 > 0 이면 1, 아니면 0)
        y = (df['realized_pnl'] > 0).astype(int)
        
        return X, y

    def train(self):
        """모델 학습 및 저장"""
        df = self.load_data()
        
        if len(df) < 20:
            logger.warning("[MLTrainer] 데이터 부족 (현재 %d건). 최소 20건 이상 필요.", len(df))
            return False
            
        X, y = self.preprocess(df)
        if X is None or len(np.unique(y)) < 2:
            logger.warning("[MLTrainer] 학습 가능한 데이터 상태가 아님 (클래스 부족 등)")
            return False
            
        # 모델 생성 및 학습
        model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
        model.fit(X, y)
        
        # 모델 및 특성 목록 저장
        model_path = os.path.join(self.model_dir, "signal_filter_v1.pkl")
        joblib.dump({
            'model': model,
            'features': self.feature_cols,
            'trained_at': datetime.now().isoformat(),
            'data_count': len(df)
        }, model_path)
        
        logger.info("[MLTrainer] 모델 학습 완료 및 저장: %s (%d건)", model_path, len(df))
        return True

    def generate_dummy_model(self):
        """초기 테스트를 위한 더미 모델 생성 (무조건 승인하는 모델)"""
        import numpy as np
        from sklearn.dummy import DummyClassifier
        
        X = np.zeros((10, len(self.feature_cols)))
        y = np.ones(10)
        
        model = DummyClassifier(strategy="constant", constant=1)
        model.fit(X, y)
        
        model_path = os.path.join(self.model_dir, "signal_filter_v1.pkl")
        joblib.dump({
            'model': model,
            'features': self.feature_cols,
            'trained_at': "DUMMY",
            'data_count': 0
        }, model_path)
        logger.info("[MLTrainer] 더미 모델 생성 완료 (테스트용)")

from datetime import datetime
if __name__ == "__main__":
    trainer = MLTrainer()
    if not trainer.train():
        trainer.generate_dummy_model()
