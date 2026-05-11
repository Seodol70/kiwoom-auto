#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BREAKOUT_GATE 디버깅 — 어느 필터에서 차단되는지 추적
"""

import sys
from datetime import datetime, time as dtime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from scanner.models import StockSnapshot
from scanner.config import SmartScannerConfig
from scanner.evaluators.common import _resolve_time_slot, _get_slot_value, check_vwap_filter

def debug_breakout_gate(snap: "StockSnapshot", cfg: "SmartScannerConfig"):
    """BREAKOUT_GATE 각 필터 단계별 디버깅"""

    now = dtime(12, 30, 0)  # MIDDAY

    print("\n" + "="*70)
    print(f"BREAKOUT_GATE 디버깅: {snap.code} {snap.name}")
    print("="*70)

    # Step 1: 진입 시간 체크
    print(f"\n[Step 1] 진입 시간 체크")
    print(f"  현재시간: {now}")
    print(f"  진입가능: {cfg.entry_start_time} ~ {cfg.entry_end_time}")

    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        print(f"  FAIL: 진입 허용 시간이 아님")
        return
    print(f"  PASS")

    # Step 2: 시간 슬롯 결정
    print(f"\n[Step 2] 시간 슬롯 결정")
    _slot = _resolve_time_slot(now, cfg)
    print(f"  슬롯: {_slot}")

    # Step 3: 등락률 체크
    print(f"\n[Step 3] 등락률 (change_pct) 체크")
    _eff_ch_max = _get_slot_value(_slot, cfg, "max_change_pct", cfg.max_change_pct)
    _snap_chg = float(getattr(snap, "change_pct", 0) or 0)
    print(f"  현재 등락률: {_snap_chg:.2f}%")
    print(f"  [{_slot}] 상한: {_eff_ch_max:.0f}%")

    if _snap_chg >= _eff_ch_max:
        print(f"  FAIL: {_snap_chg:.2f}% >= {_eff_ch_max:.0f}%")
        return
    print(f"  PASS: {_snap_chg:.2f}% < {_eff_ch_max:.0f}%")

    # Step 4: 최소 체결강도 체크
    print(f"\n[Step 4] 최소 체결강도 (min_chejan_strength) 체크")
    _eff_chejan = _get_slot_value(_slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
    print(f"  현재 체결강도: {snap.chejan_strength:.0f}%")
    print(f"  [{_slot}] 최소값: {_eff_chejan:.0f}%")

    if snap.chejan_strength < _eff_chejan:
        print(f"  FAIL: {snap.chejan_strength:.0f}% < {_eff_chejan:.0f}%")
        return
    print(f"  PASS: {snap.chejan_strength:.0f}% >= {_eff_chejan:.0f}%")

    # Step 5: 최대 체결강도 체크
    print(f"\n[Step 5] 최대 체결강도 (breakout_chejan_max) 체크")
    if _slot == "MORNING":
        _chejan_max = getattr(cfg, "breakout_chejan_max_morning", 950.0)
    else:
        _chejan_max = getattr(cfg, "breakout_chejan_max", 800.0)
    print(f"  현재 체결강도: {snap.chejan_strength:.0f}%")
    print(f"  [{_slot}] 최대값: {_chejan_max:.0f}%")

    if snap.chejan_strength >= _chejan_max:
        print(f"  FAIL: {snap.chejan_strength:.0f}% >= {_chejan_max:.0f}%")
        return
    print(f"  PASS: {snap.chejan_strength:.0f}% < {_chejan_max:.0f}%")

    # Step 6: RSI 체크
    print(f"\n[Step 6] RSI 최대값 (breakout_rsi_max) 체크")
    _rsi_max = getattr(cfg, "breakout_rsi_max", 80.0)
    print(f"  현재 RSI: {snap.rsi:.1f}")
    print(f"  [{_slot}] 최대값: {_rsi_max:.1f}")

    if snap.rsi > 0 and snap.rsi >= _rsi_max:
        print(f"  FAIL: {snap.rsi:.1f} >= {_rsi_max:.1f}")
        return
    print(f"  PASS: {snap.rsi:.1f} < {_rsi_max:.1f}")

    # Step 7: VWAP 필터
    print(f"\n[Step 7] VWAP 필터")
    r_vwap = check_vwap_filter(snap)
    if r_vwap is None:
        print(f"  FAIL: VWAP 필터 차단")
        return
    print(f"  PASS: {r_vwap}")

    print(f"\n" + "="*70)
    print(f"최종 결과: PASS")
    print(f"["f"{_slot}] 체결강도 {snap.chejan_strength:.0f}% | 등락률 {_snap_chg:.1f}% | {r_vwap}")
    print("="*70)

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

    # 시나리오 1: 정상 BREAKOUT 신호
    snap1 = create_mock_snapshot(
        code="000100", name="test_normal",
        current_price=420_000, prev_close=405_000,
        chejan_strength=800.0, volume=300_000,
        change_pct=3.7, rsi=58.0,
        closes_1min=[405_000, 410_000, 415_000, 418_000, 420_000],
        trend_level=2,
    )

    print("\n" + "="*70)
    print("시나리오 1: 정상 BREAKOUT (800% 체결강도)")
    print("="*70)
    debug_breakout_gate(snap1, cfg)
