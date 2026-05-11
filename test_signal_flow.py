#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
신호 판정 로직 검증 스크립트
실제 설정이 신호 판정에 올바르게 적용되는지 확인
"""

from datetime import datetime, time as dtime
from scanner.models import StockSnapshot
from scanner.config import SmartScannerConfig
from scanner.evaluators.breakout import check_breakout, check_breakout_gate
from scanner.evaluators.jdm import _jdm_build_ctx

def test_breakout_gate():
    """BREAKOUT 게이트 필터 검증"""
    print("=" * 60)
    print("BREAKOUT 게이트 필터 검증")
    print("=" * 60)

    cfg = SmartScannerConfig()
    print(f"\n[Config] 현재 설정:")
    print(f"  - CHEJAN_MAX (오후): {cfg.breakout_chejan_max}%")
    print(f"  - CHEJAN_MAX (오전): {cfg.breakout_chejan_max_morning}%")
    print(f"  - Entry 시간: {cfg.entry_start_time} ~ {cfg.entry_end_time}")

    # 테스트 1: 체결강도 1518% → 1500% 기준으로 NEAR_MISS 예상 (차단됨)
    snap1 = StockSnapshot(
        code="000000",
        name="테스트1_1518%체결강도",
        current_price=100_000,
        prev_close=99_000,
        open_price=99_000,
        high_price=100_000,
        low_price=98_000,
        volume=500_000,
        trade_amount=50_000_000,
        change_pct=1.0,
        chejan_strength=1518.0,
        closes_1min=[99_000, 100_000, 100_500],  # 연속상승
        rsi=65.0,
        trend_level=2,
    )

    print(f"\n[Test 1] 체결강도=1518% (1500% 기준 초과 1개)")
    result1 = check_breakout_gate(snap1, cfg)
    print(f"  -> 결과: {result1 if result1 else 'BLOCKED (NEAR_MISS)'}")

    # 테스트 2: 체결강도 1200% → 1500% 기준 미달 (통과 예상, 다른 필터 체크)
    snap2 = StockSnapshot(
        code="000001",
        name="테스트2_1200%체결강도",
        current_price=100_000,
        prev_close=99_000,
        open_price=99_000,
        high_price=100_000,
        low_price=98_000,
        volume=500_000,
        trade_amount=50_000_000,
        change_pct=1.0,
        chejan_strength=1200.0,
        closes_1min=[99_000, 100_000, 100_500],
        rsi=65.0,
        trend_level=2,
    )

    print(f"\n[Test 2] 체결강도=1200% (1500% 기준 미달, 다른 필터 체크)")
    result2 = check_breakout_gate(snap2, cfg)
    print(f"  -> 결과: {result2 if result2 else 'BLOCKED'}")

    # 테스트 3: 거래량 50,000주 → JDM 필터에서 차단 예상
    snap3 = StockSnapshot(
        code="000002",
        name="테스트3_저거래량",
        current_price=100_000,
        prev_close=99_000,
        open_price=99_000,
        high_price=100_000,
        low_price=98_000,
        volume=50_000,  # 100,000 미만
        trade_amount=5_000_000,
        change_pct=1.0,
        chejan_strength=500.0,
        closes_1min=[99_000, 100_000, 100_500],
        rsi=65.0,
        trend_level=2,
    )

    print(f"\n[Test 3] 거래량=50,000주 (JDM 필터 테스트)")
    jdm_ctx = _jdm_build_ctx(snap3, cfg)
    print(f"  -> JDM 컨텍스트: {jdm_ctx if jdm_ctx else 'BLOCKED (거래량 미달)'}")

if __name__ == "__main__":
    test_breakout_gate()
