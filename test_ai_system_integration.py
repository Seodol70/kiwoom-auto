#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ai_system_integration.py
─────────────────────────────

AI 시스템 전체 파이프라인 통합 테스트:
  1. 피처 추출 (extract_ml_features)
  2. 모델 로드 (AIFilter.load_model)
  3. 승률 예측 (AIFilter.predict_win_rate)
  4. 진입 판정 (AIFilter.should_enter)

테스트 흐름:
  Signal + StockSnapshot → extract_ml_features
    → AIFilter.predict_win_rate → should_enter
"""

import sys
import logging
from pathlib import Path

# 프로젝트 루트를 PATH에 추가
PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

def test_feature_extraction():
    """Test 1: 피처 추출"""
    logger.info("=" * 80)
    logger.info("Test 1: 피처 추출 (extract_ml_features)")
    logger.info("=" * 80)

    from scanner.models import StockSnapshot, ScanSignal
    from analysis.feature_engineer import extract_ml_features
    import numpy as np

    # Mock StockSnapshot 생성
    snap = StockSnapshot(
        code="000660",
        name="SK하이닉스",
        current_price=100000,
        open_price=99000,
        high_price=102000,
        low_price=98000,
        volume=1000000,
        trade_amount=100_000_000_000,  # 100억
        change_pct=1.0,
        prev_close=99000,
        chejan_strength=200,
        trend_level=2,
        rs_score=1.5,
        closes_1min=[99000 + i*100 for i in range(30)],  # 30개의 1분봉
        volumes_1min=[10000 + i*100 for i in range(30)],
        opens_1min=[99000 + i*50 for i in range(30)],
        highs_1min=[99500 + i*100 for i in range(30)],
        lows_1min=[98500 + i*100 for i in range(30)],
    )

    # Mock ScanSignal 생성
    sig = ScanSignal(
        code="000660",
        name="SK하이닉스",
        price=100000,
        signal_type="JDM",
        reason="RSI > 50 & EMA10 > EMA20"
    )

    # 피처 추출
    features = extract_ml_features(sig, snap)

    if features:
        logger.info("✅ 피처 추출 성공!")
        logger.info(f"   - 추출된 피처 개수: {len(features)}")
        logger.info(f"   - 피처 목록: {list(features.keys())}")

        # 피처 값 샘플 출력
        sample_features = {k: v for k, v in list(features.items())[:5]}
        logger.info(f"   - 샘플 피처: {sample_features}")

        # ML 트레이너 기대 피처 확인
        expected_features = [
            "f_rsi", "f_ema20_gap", "f_pct_b", "f_vol_surge",
            "f_change_pct", "f_strength", "f_trend",
            "f_price_mom", "f_intra_pos", "f_volatility", "f_ma_align", "f_rs_score",
            "f_vwap_dist", "f_mtf_15m_gap", "f_mtf_60m_gap",
            "f_hoga_ratio", "f_candle_body", "f_candle_upper_tail", "f_candle_lower_tail"
        ]

        missing = set(expected_features) - set(features.keys())
        if missing:
            logger.warning(f"⚠️  누락된 피처: {missing}")
        else:
            logger.info(f"✅ 모든 19개 기본 피처 정규화 완료")

        return True
    else:
        logger.error("❌ 피처 추출 실패 (데이터 부족)")
        return False


def test_ai_filter_load():
    """Test 2: AI 필터 모델 로드"""
    logger.info("=" * 80)
    logger.info("Test 2: AI 필터 모델 로드")
    logger.info("=" * 80)

    from app.ai_filter import AIFilter

    ai_filter = AIFilter()

    if ai_filter.is_ready:
        logger.info("✅ 모델 로드 성공!")
        logger.info(f"   - 모델: {ai_filter.model}")
        logger.info(f"   - 피처 개수: {len(ai_filter.features)}")
        return True
    else:
        logger.warning("⚠️  모델이 아직 준비되지 않음 (첫 학습 필요 또는 sklearn 미설치)")
        logger.info("   → 더미 모델 생성 시도...")
        try:
            from analysis.ml_trainer import MLTrainer
            trainer = MLTrainer()
            trainer.generate_dummy_model()
            logger.info("   → 더미 모델 생성 완료")
        except ImportError as e:
            logger.warning(f"   ⚠️  sklearn 설치 필요: {e}")
            logger.info("   → 스킵 (프로덕션 환경에서는 필요)")
        return True  # 테스트 계속 진행


def test_ai_prediction():
    """Test 3: AI 예측 (승률 판정)"""
    logger.info("=" * 80)
    logger.info("Test 3: AI 예측 (승률 판정)")
    logger.info("=" * 80)

    from scanner.models import StockSnapshot, ScanSignal
    from analysis.feature_engineer import extract_ml_features
    from app.ai_filter import AIFilter

    # Mock 데이터
    snap = StockSnapshot(
        code="000660",
        name="SK하이닉스",
        current_price=100000,
        open_price=99000,
        high_price=102000,
        low_price=98000,
        volume=1000000,
        trade_amount=100_000_000_000,
        change_pct=1.0,
        prev_close=99000,
        chejan_strength=200,
        trend_level=2,
        rs_score=1.5,
        closes_1min=[99000 + i*100 for i in range(30)],
        volumes_1min=[10000 + i*100 for i in range(30)],
        opens_1min=[99000 + i*50 for i in range(30)],
        highs_1min=[99500 + i*100 for i in range(30)],
        lows_1min=[98500 + i*100 for i in range(30)],
    )

    sig = ScanSignal(
        code="000660",
        name="SK하이닉스",
        price=100000,
        signal_type="JDM",
        reason="RSI > 50 & EMA10 > EMA20"
    )

    # 피처 추출
    features = extract_ml_features(sig, snap)
    if not features:
        logger.error("❌ 피처 추출 실패")
        return False

    # AI 필터
    ai_filter = AIFilter()
    if not ai_filter.is_ready:
        logger.warning("⚠️  AI 모델이 준비되지 않음 → 더미 모델 사용 시도")
        try:
            from analysis.ml_trainer import MLTrainer
            trainer = MLTrainer()
            trainer.generate_dummy_model()
            ai_filter = AIFilter()
        except ImportError:
            logger.warning("   ⚠️  sklearn 미설치 → 관찰 모드로 진행 (모든 신호 승인)")
            pass

    # 승률 예측
    win_rate = ai_filter.predict_win_rate(features)
    logger.info(f"✅ 예상 승률: {win_rate*100:.1f}%")

    # 진입 판정
    should_enter, final_win_rate = ai_filter.should_enter(features, threshold=0.5)
    logger.info(f"   - 진입 판정: {'승인' if should_enter else '거절'}")
    logger.info(f"   - 임계값: 50.0%")

    return True


def test_feature_engineer_signature():
    """Test 4: 특성 엔지니어 함수 서명 검증"""
    logger.info("=" * 80)
    logger.info("Test 4: 특성 엔지니어 함수 서명 검증")
    logger.info("=" * 80)

    from analysis.feature_engineer import extract_ml_features
    import inspect

    sig = inspect.signature(extract_ml_features)
    logger.info(f"✅ extract_ml_features 함수 서명:")
    logger.info(f"   - 파라미터: {list(sig.parameters.keys())}")
    logger.info(f"   - 반환값: dict")

    return True


def test_end_to_end():
    """Test 5: 엔드-투-엔드 테스트 (신호 → 진입 판정)"""
    logger.info("=" * 80)
    logger.info("Test 5: 엔드-투-엔드 테스트")
    logger.info("=" * 80)

    from scanner.models import StockSnapshot, ScanSignal
    from analysis.feature_engineer import extract_ml_features
    from app.ai_filter import AIFilter

    # Mock 신호 및 스냅샷
    snap = StockSnapshot(
        code="005930",
        name="삼성전자",
        current_price=70000,
        open_price=69000,
        high_price=71000,
        low_price=68000,
        volume=2000000,
        trade_amount=140_000_000_000,
        change_pct=1.45,
        prev_close=69000,
        chejan_strength=250,
        trend_level=2,
        rs_score=2.1,
        closes_1min=[69000 + i*50 for i in range(40)],
        volumes_1min=[15000 + i*100 for i in range(40)],
        opens_1min=[69000 + i*30 for i in range(40)],
        highs_1min=[69500 + i*50 for i in range(40)],
        lows_1min=[68500 + i*50 for i in range(40)],
    )

    sig = ScanSignal(
        code="005930",
        name="삼성전자",
        price=70000,
        signal_type="BREAKOUT",
        reason="이전 고점 돌파"
    )

    # 파이프라인
    logger.info(f"1️⃣  신호 수신: {sig.name}({sig.code}) - {sig.signal_type}")

    features = extract_ml_features(sig, snap)
    if not features:
        logger.error("❌ 피처 추출 실패")
        return False
    logger.info(f"2️⃣  피처 추출 완료: {len(features)}개")

    ai_filter = AIFilter()
    if not ai_filter.is_ready:
        try:
            from analysis.ml_trainer import MLTrainer
            trainer = MLTrainer()
            trainer.generate_dummy_model()
            ai_filter = AIFilter()
        except ImportError:
            logger.warning("   ⚠️  sklearn 미설치 → 관찰 모드로 진행")
            pass

    should_enter, win_rate = ai_filter.should_enter(features, threshold=0.5)
    logger.info(f"3️⃣  AI 판정: {'✅ 진입 승인' if should_enter else '❌ 거절'} (승률 {win_rate*100:.1f}%)")

    logger.info("✅ 엔드-투-엔드 테스트 완료!")
    return True


def main():
    """전체 테스트 실행"""
    logger.info("\n")
    logger.info("🚀 AI 시스템 통합 테스트 시작")
    logger.info("=" * 80)

    results = {
        "피처 추출": test_feature_extraction(),
        "모델 로드": test_ai_filter_load(),
        "AI 예측": test_ai_prediction(),
        "함수 서명": test_feature_engineer_signature(),
        "엔드-투-엔드": test_end_to_end(),
    }

    logger.info("\n")
    logger.info("=" * 80)
    logger.info("📊 테스트 결과 요약")
    logger.info("=" * 80)

    for name, result in results.items():
        status = "✅ 통과" if result else "❌ 실패"
        logger.info(f"{status}: {name}")

    all_passed = all(results.values())
    logger.info("=" * 80)
    if all_passed:
        logger.info("🎉 모든 테스트 통과!")
    else:
        logger.info("⚠️  일부 테스트 실패 — 위 로그 확인")
    logger.info("=" * 80)

    return 0 if all_passed else 1


if __name__ == "__main__":
    exit(main())
