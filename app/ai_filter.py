import os
import logging
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import joblib
    HAS_ML = True
except ImportError:
    HAS_ML = False
    # 32bit Python 환경(키움 OCX 제약)에서는 sklearn/joblib 설치 불가 — 정상 상황
    # is_ready=False → should_enter()는 True(무조건 통과) 반환 — 매매에 영향 없음
    logger.debug("[AIFilter] joblib 미설치 — AI 필터 관찰 모드 (32bit 환경 정상)")

class AIFilter:
    """
    학습된 ML 모델을 사용하여 신호의 진입 적합성을 판정합니다.
    """
    def __init__(self, model_path="models/signal_filter_v1.pkl"):
        self.model_path = model_path
        self.model = None
        self.features = []
        self.is_ready = False
        self.load_model()

    def load_model(self):
        """모델 파일 로드"""
        if not HAS_ML:
            return

        if not os.path.exists(self.model_path):
            logger.info("[AIFilter] 모델 파일이 없습니다. 필터링을 건너뜁니다.")
            return
            
        try:
            data = joblib.load(self.model_path)
            self.model = data['model']
            self.features = data['features']
            self.is_ready = True
            logger.info("[AIFilter] 모델 로드 완료 (학습시점: %s, 데이터수: %s)", 
                        data.get('trained_at'), data.get('data_count'))
        except Exception as e:
            logger.error("[AIFilter] 모델 로드 실패: %s", e)

    def predict_win_rate(self, signal_data: dict) -> float:
        """신호의 예상 승률(0.0 ~ 1.0) 반환"""
        if not HAS_ML or not self.is_ready or self.model is None:
            return 1.0 # 모델이 없으면 무조건 통과 (또는 기본값)
            
        try:
            # 특성 벡터 구성
            X = pd.DataFrame([signal_data])[self.features]
            # 숫자형 변환 및 결측치 처리
            for col in self.features:
                X[col] = pd.to_numeric(X[col], errors='coerce')
            X = X.fillna(0)
            
            # 승리(1) 확률 계산
            probs = self.model.predict_proba(X)[0]
            # classes_ 순서에 따라 1의 인덱스 확인
            idx_1 = list(self.model.classes_).index(1) if 1 in self.model.classes_ else -1
            
            if idx_1 != -1:
                return float(probs[idx_1])
            return 0.0
        except Exception as e:
            logger.error("[AIFilter] 승률 예측 실패: %s", e)
            return 1.0

    def should_enter(self, signal_data: dict, threshold: float = 0.5) -> tuple[bool, float]:
        """진입 여부 판정"""
        if not self.is_ready:
            return True, 1.0 # 모델 준비 전에는 필터링 안 함 (Observation Mode)
            
        win_rate = self.predict_win_rate(signal_data)
        passed = win_rate >= threshold
        
        return passed, win_rate
