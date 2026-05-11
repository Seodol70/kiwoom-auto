#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
VWAP 필터 테스트
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from scanner.models import StockSnapshot
from scanner.evaluators.common import check_vwap_filter

# 스냅샷 생성 (vwap 필드 제공 안 함)
snap = StockSnapshot(
    code="000100", name="test",
    current_price=100_000, open_price=99_000,
    high_price=101_000, low_price=98_000,
    prev_close=99_000, volume=100_000,
    trade_amount=10_000_000, change_pct=1.0,
    chejan_strength=500.0, rsi=55.0,
    closes_1min=[99_000, 100_000],
)

print(f"snap.vwap 존재? {hasattr(snap, 'vwap')}")
print(f"snap.vwap 값: {getattr(snap, 'vwap', 'NOT_FOUND')}")

result = check_vwap_filter(snap)
print(f"\ncheck_vwap_filter 결과: {result}")
print(f"결과 타입: {type(result)}")
