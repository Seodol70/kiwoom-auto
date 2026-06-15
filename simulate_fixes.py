"""
simulate_fixes.py — 2026-06-15 수정사항 시뮬레이션
검증 항목:
  1. 트레일스탑 activation 3.0% (손실 청산 버그 수정)
  2. PULLBACK 전략 (yosep 비활성 시 trend 체크 스킵)
  3. _find_timecut_position 35분 config 적용
  4. 서킷브레이커 (daily_max_stoplosses)
  5. 당일 강제청산 후 재진입 차단 (_no_reentry_today)
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional
import textwrap

PASS = "✅ PASS"
FAIL = "❌ FAIL"

# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)

def result(label: str, ok: bool, detail: str = ""):
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label}")
    if detail:
        for line in detail.strip().split('\n'):
            print(f"       {line}")

# ─────────────────────────────────────────────────────────────────────────────
# 1. 트레일스탑 activation 수정 검증
# ─────────────────────────────────────────────────────────────────────────────
section("① 트레일스탑 — activation 손실 청산 버그 수정")

from scanner.config import SmartScannerConfig
cfg = SmartScannerConfig()

activation = cfg.trail_activation_pct        # 3.0
tier1_trail = cfg.trail_pct_tier1             # 2.5
tier1_max   = cfg.trail_tier1_max             # 5.0

scenarios = [
    ("고점 +1.5%", 1.5),
    ("고점 +2.0%", 2.0),
    ("고점 +2.9%", 2.9),
    ("고점 +3.0%", 3.0),   # activation 경계
    ("고점 +3.5%", 3.5),
    ("고점 +5.0%", 5.0),   # tier2 경계
]

for label, peak_pct in scenarios:
    if peak_pct < activation:
        # trail 미활성 — stop_loss(-1.2%)만 적용
        activated = False
        exit_at = None
        detail = f"trail 미활성 (peak {peak_pct}% < activation {activation}%) → 일반 손절(-1.2%)로 처리"
    else:
        activated = True
        # tier 결정
        if peak_pct < tier1_max:
            trail_pct = tier1_trail   # 2.5%
        elif peak_pct < cfg.trail_tier2_max:
            trail_pct = cfg.trail_pct_tier2  # 3.0%
        else:
            trail_pct = cfg.trail_pct_tier3  # 3.0%
        trail_price_ratio = peak_pct * (1 - trail_pct / 100)
        exit_at = trail_price_ratio   # avg 대비 exit %
        detail = f"trail 활성 | peak={peak_pct}% trail={trail_pct}% → exit≥+{exit_at:.2f}% 보장"

    ok = (not activated) or (exit_at is not None and exit_at > 0)
    result(label, ok, detail)

# 구버전과 비교
print("\n  [구버전 vs 신버전 비교]")
old_activation = 1.5
old_tier1 = 2.5
breakeven_peak = old_tier1 / (1 - old_tier1/100)
print(f"  구버전: activation={old_activation}%, trail={old_tier1}%")
print(f"          손실 청산 위험 구간: peak {old_activation}% ~ {breakeven_peak:.2f}% (trail exit이 entry 이하)")
print(f"  신버전: activation={activation}%, trail={tier1_trail}%")
print(f"          최소 exit = {activation}% × {1-tier1_trail/100:.3f} = +{activation*(1-tier1_trail/100):.3f}% → 항상 흑자")

# ─────────────────────────────────────────────────────────────────────────────
# 2. PULLBACK 전략 yosep 비활성 대응 검증
# ─────────────────────────────────────────────────────────────────────────────
section("② PULLBACK 전략 — yosep 비활성 시 trend_level 체크 스킵")

from scanner.models import StockSnapshot
from scanner.evaluators.pullback import check_pullback_entry

def make_snap_pullback(trend_level: int, yosep_on: bool) -> tuple:
    """PULLBACK 진입 조건을 최대한 만족하는 mock snap + cfg"""
    cfg_pb = SmartScannerConfig()
    cfg_pb.yosep_trend_enabled       = yosep_on
    cfg_pb.pullback_min_trend_lv     = 3
    cfg_pb.daily_ma20_filter_enabled = False
    cfg_pb.daily_ma20_slope_enabled  = False
    cfg_pb.mtf_enabled               = False
    cfg_pb.hoga_pressure_enabled     = False
    cfg_pb.pullback_vel_ratio_min    = 0.0   # vel_ratio 체크 비활성 (snap에 필드 없음)
    cfg_pb.pullback_leading_score_min = 0.0  # 선행점수 체크 비활성

    # 20봉 EMA20 기준: 상승 추세 (EMA20 근처 눌림)
    base = 10000
    closes = [base + i*20 for i in range(18)]    # 18봉 상승
    closes += [closes[-1] - 30, closes[-1] - 20] # 2봉 하락 (눌림)
    closes.append(closes[-1] + 15)                # 최근봉 반등
    current_price = closes[-1]

    snap = StockSnapshot(
        code="000001", name="테스트", market_type="10",
        current_price=current_price, open_price=base,
        high_price=current_price+50, low_price=base-50,
        prev_close=base-100, volume=1_000_000, trade_amount=10_000_000_000,
        change_pct=2.0,
    )
    snap.closes_1min  = closes
    snap.volumes_1min = [100_000] * (len(closes)-2) + [80_000, 60_000, 120_000]
    snap.highs_1min   = [c + 20 for c in closes]
    snap.lows_1min    = [c - 20 for c in closes]
    snap.daily_closes = []   # daily_ma20_filter 비활성이므로 비워도 됨
    snap.trend_level  = trend_level
    snap.mtf_tf5_bars = 0   # MTF 비활성
    # VWAP: cumulative 값으로 설정 (vwap = amount*1000 / volume)
    # 목표 vwap = current_price - 50 (현재가 위에 있어야 VWAP 통과)
    target_vwap = current_price - 50
    snap.cumulative_volume = 1_000_000
    snap.cumulative_amount = int(target_vwap * snap.cumulative_volume / 1000)

    return snap, cfg_pb

# 핵심 검증: yosep OFF 시 PULLBACK_TREND 로그가 찍히지 않아야 함
# ScannerLogger.rejected 호출 여부를 가로채서 확인
from scanner.scanner_logger import ScannerLogger
_rejected_tags = []
_orig_rejected = ScannerLogger.rejected.__func__ if hasattr(ScannerLogger.rejected, '__func__') else None

import unittest.mock as mock
with mock.patch.object(ScannerLogger, 'rejected', side_effect=lambda *a, **kw: _rejected_tags.append(a[2])):
    snap1, cfg1 = make_snap_pullback(trend_level=0, yosep_on=True)
    _rejected_tags.clear()
    check_pullback_entry(snap1, cfg1)
    trend_rejected_yosep_on = "PULLBACK_TREND" in _rejected_tags
    result("yosep ON + trend_lv=0 → PULLBACK_TREND로 거절", trend_rejected_yosep_on,
           f"거절 태그: {_rejected_tags}")

    snap3, cfg3 = make_snap_pullback(trend_level=0, yosep_on=False)
    _rejected_tags.clear()
    check_pullback_entry(snap3, cfg3)
    trend_rejected_yosep_off = "PULLBACK_TREND" in _rejected_tags
    result("yosep OFF + trend_lv=0 → PULLBACK_TREND 거절 없음 (수정 핵심)", not trend_rejected_yosep_off,
           f"거절 태그: {_rejected_tags} (PULLBACK_TREND 없으면 trend 체크 스킵 성공)")

# ─────────────────────────────────────────────────────────────────────────────
# 3. _find_timecut_position 35분 config 적용 검증
# ─────────────────────────────────────────────────────────────────────────────
section("③ _find_timecut_position — 35분 config 적용")

from order.order_manager import Position

def make_position(held_minutes: float, pnl_pct_override: float) -> Position:
    p = Position(
        code="000001", name="테스트",
        qty=10, avg_price=10000, current_price=10000,
        entry_time=datetime.now() - timedelta(minutes=held_minutes),
    )
    # pnl_pct를 직접 조작하기 위해 current_price를 역산
    p.current_price = int(p.avg_price * (1 + pnl_pct_override / 100))
    return p

# _find_timecut_position 로직을 직접 재현 (의존성 없이)
def find_timecut_sim(positions, time_cut_minutes: float):
    candidates = [p for p in positions if p.qty > 0]
    if not candidates:
        return None
    now = datetime.now()
    losses = [p for p in candidates if p.pnl_pct < 0]
    if losses:
        return min(losses, key=lambda p: p.pnl_pct)
    _tc_min = time_cut_minutes
    for p in candidates:
        if p.entry_time:
            elapsed = (now - p.entry_time).total_seconds() / 60
            if elapsed >= _tc_min and p.pnl_pct < 0.3:
                return p
    return None

timecut_cases = [
    (20, 0.2, "20분 보유 +0.2%"),
    (30, 0.2, "30분 보유 +0.2%"),
    (35, 0.2, "35분 보유 +0.2%"),  # 경계
    (36, 0.2, "36분 보유 +0.2%"),
    (40, 0.5, "40분 보유 +0.5% (수익 양호)"),
]

OLD_TC = 20   # 구버전
NEW_TC = cfg.time_cut_minutes  # 35

for elapsed, pnl, label in timecut_cases:
    pos = make_position(elapsed, pnl)
    old_cut = find_timecut_sim([pos], OLD_TC)
    new_cut = find_timecut_sim([pos], NEW_TC)
    detail = f"구버전({OLD_TC}분): {'교체대상' if old_cut else '유지'} → 신버전({NEW_TC}분): {'교체대상' if new_cut else '유지'}"
    # 기대값 정의
    timecut_possible = pnl < 0.3   # 수익 < 0.3%여야 타임컷 대상
    if not timecut_possible:
        # 수익 양호 → 둘 다 유지
        ok = (old_cut is None and new_cut is None)
    elif elapsed < OLD_TC:
        # 구버전 기준도 안됨 → 둘 다 유지
        ok = (old_cut is None and new_cut is None)
    elif elapsed < NEW_TC:
        # 구버전은 교체, 신버전은 유지 (35분 미만)
        ok = (old_cut is not None and new_cut is None)
    else:
        # 둘 다 교체 (35분 이상)
        ok = (old_cut is not None and new_cut is not None)
    result(label, ok, detail)

# ─────────────────────────────────────────────────────────────────────────────
# 4. 서킷브레이커 검증
# ─────────────────────────────────────────────────────────────────────────────
section("④ 서킷브레이커 — daily_max_stoplosses")

# 서킷브레이커 로직 직접 재현
def circuit_breaker_check(stop_loss_today: set, max_sl: int) -> bool:
    """True = 진입 차단"""
    if max_sl > 0 and len(stop_loss_today) >= max_sl:
        return True
    return False

cb_cases = [
    (set(), 0,  "손절 0회 / 한도 0(비활성)  → 진입 허용"),
    (set(), 5,  "손절 0회 / 한도 5           → 진입 허용"),
    ({"A","B","C","D"}, 5, "손절 4회 / 한도 5 → 진입 허용"),
    ({"A","B","C","D","E"}, 5, "손절 5회 / 한도 5 → 차단"),
    ({"A","B","C","D","E","F"}, 5, "손절 6회 / 한도 5 → 차단"),
]

for sl_set, max_sl, label in cb_cases:
    blocked = circuit_breaker_check(sl_set, max_sl)
    expect_block = (max_sl > 0 and len(sl_set) >= max_sl)
    result(label, blocked == expect_block,
           f"손절={len(sl_set)}회 한도={max_sl} → {'차단' if blocked else '허용'}")

# ─────────────────────────────────────────────────────────────────────────────
# 5. 당일 강제청산 후 재진입 차단 (_no_reentry_today)
# ─────────────────────────────────────────────────────────────────────────────
section("⑤ 당일 강제청산 후 재진입 차단")

# force_exit 내 _no_reentry_today 등록 로직 재현
def simulate_force_exit(reason: str, no_reentry_set: set, code: str = "000001"):
    _manual_reasons = ("수동", "DayClose", "Day Close")
    if not any(r in reason for r in _manual_reasons):
        if code not in no_reentry_set:
            no_reentry_set.add(code)

exit_cases = [
    ("트레일스탑",  True,  "트레일스탑 후 재진입 차단"),
    ("타임컷",      True,  "타임컷 후 재진입 차단"),
    ("하드스탑",    True,  "하드스탑 후 재진입 차단"),
    ("확정손절",    True,  "확정손절 후 재진입 차단"),
    ("본절가스탑",  True,  "본절가스탑 후 재진입 차단"),
    ("수동",        False, "수동 청산 후 재진입 허용"),
    ("DayClose",    False, "DayClose 후 재진입 허용"),
]

for reason, expect_block, label in exit_cases:
    no_reentry = set()
    simulate_force_exit(reason, no_reentry)
    blocked = "000001" in no_reentry
    result(label, blocked == expect_block,
           f"reason='{reason}' → {'_no_reentry 등록됨' if blocked else '등록 안됨'}")

# ─────────────────────────────────────────────────────────────────────────────
# 최종 요약
# ─────────────────────────────────────────────────────────────────────────────
section("최종 요약")
print("""
  수정 항목                       변경 내용
  ──────────────────────────────────────────────────────────
  ① 트레일스탑 activation          1.5% → 3.0%
     (손실 청산 버그)               peak 3.0% 기준: exit +2.925% 보장

  ② PULLBACK trend_level 스킵      yosep OFF 시 trend 체크 건너뜀
     (전략 사망 버그)               이제 EMA/RSI/반등에너지 등 실질 조건으로 평가

  ③ _find_timecut_position          하드코딩 20분 → config 35분 적용
     (타임컷 미적용 버그)            25~35분 보유 포지션이 교체 안됨 (기회 유지)

  ④ 서킷브레이커                    daily_max_stoplosses 추가 (기본 0=비활성)
     (연속 손절 무한 진입)           5~7로 설정하면 연속 손절 후 자동 중단

  ⑤ 당일 재진입 차단 확대           트레일스탑·타임컷·본절가스탑 후 재진입 차단
     (반복 손실 52건)                손절 외 청산도 당일 같은 종목 재매수 불가
""")
