"""
test_eod_entry_evaluator.py — scanner/evaluators/eod.py:check_eod_entry() 단위 테스트

배경(2026-06-29): EOD(종가매매) 진입 시간대를 14:40~14:55 → 14:50~15:20으로 옮기고,
eod_min_trend_level을 2 → 3(극강추세만)으로 강화했다. 또한 yosep_trend_enabled(전역)가
꺼져 있어도(기본값 False) overnight_mode_enabled가 켜져 있으면 trend_level 체크가
무력화되지 않도록 scanner/smart_scanner.py와 scanner/evaluators/eod.py를 함께 수정했다.
이 테스트는 시간 윈도우 경계와 trend_level 게이트가 의도대로 동작하는지 검증한다.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from scanner.config import SmartScannerConfig
from scanner.evaluators.eod import check_eod_entry
from scanner.models import StockSnapshot


def _make_snap(trend_level: int = 3, chejan_strength: float = 130.0, change_pct: float = 5.0) -> StockSnapshot:
    # 20일간 우상향하는 daily_closes — 20MA 상방 + 신고가 근처 + 정배열(5MA>10MA>20MA)을 모두 만족
    daily_closes = [100.0 + i * 0.5 for i in range(25)]
    current_price = int(daily_closes[-1] + 1)  # 113 — high_25d(112) 대비 근접
    snap = StockSnapshot(
        code="005930", name="삼성전자",
        current_price=current_price, open_price=current_price - 200,
        high_price=current_price + 50, low_price=current_price - 300,
        prev_close=current_price - 300, volume=100_000, trade_amount=10_000_000_000,
        change_pct=change_pct,
    )
    snap.daily_closes = daily_closes
    snap.trend_level = trend_level
    snap.chejan_strength = chejan_strength
    snap.volumes_1min = [1000] * 9 + [2000]  # 평균 1000, 마지막 1분 2000 (2.0배)
    return snap


def _freeze(hhmm: str):
    hh, mm = map(int, hhmm.split(":"))
    fake_now = datetime(2026, 6, 29, hh, mm, 0)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    return patch("scanner.evaluators.eod.datetime", _Frozen)


def _cfg(**kwargs) -> SmartScannerConfig:
    cfg = SmartScannerConfig()
    cfg.overnight_mode_enabled = True
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def test_rejects_before_window_starts():
    """14:49는 시작(14:50) 이전이므로 거절"""
    snap = _make_snap()
    with _freeze("14:49"):
        assert check_eod_entry(snap, _cfg()) is None


def test_accepts_at_window_start():
    """14:50 정각은 시작 시각 포함 — 통과"""
    snap = _make_snap()
    with _freeze("14:50"):
        assert check_eod_entry(snap, _cfg()) is not None


def test_accepts_just_before_window_end():
    """15:19는 종료(15:20) 직전이므로 통과"""
    snap = _make_snap()
    with _freeze("15:19"):
        assert check_eod_entry(snap, _cfg()) is not None


def test_rejects_at_window_end():
    """15:20 정각은 종료 시각 미포함 — 거절 (15:20 강제청산과 경계 일치)"""
    snap = _make_snap()
    with _freeze("15:20"):
        assert check_eod_entry(snap, _cfg()) is None


def test_rejects_trend_level_below_3():
    """trend_level=2는 강화된 기준(>=3) 미달 — 거절"""
    snap = _make_snap(trend_level=2)
    with _freeze("15:00"):
        assert check_eod_entry(snap, _cfg()) is None


def test_accepts_trend_level_3():
    """trend_level=3은 기준 충족 — 통과"""
    snap = _make_snap(trend_level=3)
    with _freeze("15:00"):
        assert check_eod_entry(snap, _cfg()) is not None


def test_trend_check_active_even_when_yosep_trend_disabled():
    """yosep_trend_enabled=False(전역 기본값)여도 overnight_mode_enabled=True면
    trend_level 미달 신호는 여전히 거절돼야 한다 (전역 플래그에 무력화되지 않음)"""
    snap = _make_snap(trend_level=1)
    cfg = _cfg(yosep_trend_enabled=False)
    with _freeze("15:00"):
        assert check_eod_entry(snap, cfg) is None


def test_overnight_mode_disabled_blocks_entry_entirely():
    """overnight_mode_enabled=False면 시간대/조건과 무관하게 항상 거절"""
    snap = _make_snap()
    cfg = _cfg()
    cfg.overnight_mode_enabled = False
    with _freeze("15:00"):
        assert check_eod_entry(snap, cfg) is None
