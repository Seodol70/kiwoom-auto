#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_breakout_gate() 반환값 직접 테스트
"""

import sys
from datetime import datetime, time as dtime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from scanner.models import StockSnapshot
from scanner.config import SmartScannerConfig
from scanner.evaluators.breakout import check_breakout_gate

def create_mock_snapshot(
    code: str, name: str, current_price: int, prev_close: int,
    chejan_strength: float = 500.0, volume: int = 200_000,
    change_pct: float = 1.5, rsi: float = 55.0,
    closes_1min: list = None, trend_level: int = 2,
) -> StockSnapshot:
    """모의 스냅샷 생성"""
    if closes_1min is None:
        closes_1min = [prev_close] * 3 + [current_price - 1000, current_price]

    return StockSnapshot(
        code=code, name=name,
        current_price=current_price, open_price=prev_close,
        high_price=current_price + 1000, low_price=prev_close - 500,
        prev_close=prev_close, volume=volume,
        trade_amount=volume * current_price // 1000,
        change_pct=change_pct, chejan_strength=chejan_strength,
        rsi=rsi, closes_1min=closes_1min,
        opens_1min=[prev_close] * len(closes_1min),
        highs_1min=[max(closes_1min) + 500] * len(closes_1min),
        lows_1min=[min(closes_1min) - 500] * len(closes_1min),
        volumes_1min=[100 + i*10 for i in range(len(closes_1min))],
        trend_level=trend_level,
    )

if __name__ == "__main__":
    cfg = SmartScannerConfig()

    snap1 = create_mock_snapshot(
        code="000100", name="test_normal",
        current_price=420_000, prev_close=405_000,
        chejan_strength=800.0, volume=300_000,
        change_pct=3.7, rsi=58.0,
        closes_1min=[405_000, 410_000, 415_000, 418_000, 420_000],
        trend_level=2,
    )

    print("\nTest: check_breakout_gate() 직접 호출")
    print("="*70)
    print(f"코드: {snap1.code}, 종목: {snap1.name}")
    print(f"현재가: {snap1.current_price:,}, 체결강도: {snap1.chejan_strength:.0f}%")

    result = check_breakout_gate(snap1, cfg)

    print(f"\n반환값 타입: {type(result)}")
    print(f"반환값: {result}")
    print(f"bool(result): {bool(result)}")
    print(f"result is None: {result is None}")
    print(f"result if result else 'NONE': {result if result else 'NONE'}")
