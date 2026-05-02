"""
SignalEvaluator — 신호 판정 순수함수 모음

현재 smart_scanner.py 하단의 check_breakout, check_jdm_entry 등을 이전.
외부에서 import 가능한 순수함수로 제공 (테스트 용이).

[Phase 2] 아직 구현 중 — smart_scanner.py의 함수들을 점진적으로 이동할 예정.
"""
from __future__ import annotations

from typing import Optional, Tuple

from scanner.models import StockSnapshot
from scanner.indicator_service import IndicatorService


def check_breakout(snap: StockSnapshot, threshold_pct: float = 0.5) -> bool:
    """
    돌파 신호 (시가 대비 0.5% 이상 상승).

    Args:
        snap: 종목 스냅샷
        threshold_pct: 상승률 기준 (%)

    Returns:
        돌파 여부
    """
    if not snap.open_price or snap.open_price <= 0:
        return False
    rise_pct = (snap.current_price - snap.open_price) / snap.open_price * 100
    return rise_pct >= threshold_pct


def check_jdm_entry(snap: StockSnapshot, cfg) -> Tuple[bool, str]:
    """
    JDM(장동민) 종합 매수 필터.

    Args:
        snap: 종목 스냅샷
        cfg: SmartScannerConfig

    Returns:
        (판단, 사유)

    [TODO] Phase 2에서 smart_scanner.py의 check_jdm_entry 로직 이전
    """
    # TODO: 구현 (smart_scanner.py에서 400줄짜리 로직 이전)
    return True, "[TODO] check_jdm_entry logic not yet migrated"


def check_breakout_gate(
    snap: StockSnapshot, cfg
) -> Tuple[bool, str]:
    """
    돌파 게이트 — 추가 필터링.

    [TODO] Phase 2에서 smart_scanner.py의 check_breakout_gate 로직 이전
    """
    return True, "[TODO] check_breakout_gate logic not yet migrated"


def check_opening_surge(snap: StockSnapshot, cfg) -> Tuple[bool, str]:
    """
    아침 급등 신호 (OPENING_SURGE).

    [TODO] Phase 2에서 smart_scanner.py의 check_opening_surge 로직 이전
    """
    return True, "[TODO] check_opening_surge logic not yet migrated"


def check_opening_scalp(snap: StockSnapshot, cfg) -> Tuple[bool, str]:
    """
    아침 스캘핑 신호 (OPENING_SCALP).

    [TODO] Phase 2에서 smart_scanner.py의 check_opening_scalp 로직 이전
    """
    return True, "[TODO] check_opening_scalp logic not yet migrated"


def check_eod_entry(snap: StockSnapshot, cfg) -> Tuple[bool, str]:
    """
    종가 매매 신호 (EOD_ENTRY).

    [TODO] Phase 2에서 smart_scanner.py의 check_eod_entry 로직 이전
    """
    return True, "[TODO] check_eod_entry logic not yet migrated"


def check_pre_surge(snap: StockSnapshot, cfg) -> Tuple[bool, str]:
    """
    사전 급등 신호.

    [TODO] Phase 2에서 smart_scanner.py의 check_pre_surge 로직 이전
    """
    return True, "[TODO] check_pre_surge logic not yet migrated"


# ────────────────────────────────────────────────────────────────────────────
# [Phase 2] 추가 순수함수들
# ────────────────────────────────────────────────────────────────────────────
# 아래 함수들은 smart_scanner.py에서 점진적으로 이전할 예정:
# - check_testa_alignment
# - check_jdm_open_breakout
# - check_volume_surge
# - check_chejan_strength
# - check_disparity_from_ma
# - check_ema20_filter
# - check_bullish_engulfing
# - check_bullish_pin_bar
# ... 등
