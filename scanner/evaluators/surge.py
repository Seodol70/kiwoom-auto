"""
surge.py — 장 초반 급등(Surge) 및 스캘핑 신호 평가
"""
from typing import Optional, TYPE_CHECKING
from datetime import datetime, time as dtime
from scanner.scanner_logger import ScannerLogger

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

def check_pre_surge(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
) -> Optional[str]:
    """
    PRE_SURGE — 08:00~09:00 시간외 단일가 구간.
    """
    chg     = float(snap.change_pct or 0)
    chg_min = getattr(cfg, "pre_surge_chg_min",  2.0)
    chg_max = getattr(cfg, "pre_surge_chg_max", 20.0)
    if not (chg_min <= chg < chg_max):
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE",
            f"등락률 범위 미충족 — {chg:+.2f}% (기준 {chg_min:.1f}%~{chg_max:.1f}%)")
        return None

    chejan_min = getattr(cfg, "pre_surge_chejan_min", 110.0)
    if snap.chejan_strength < chejan_min:
        ScannerLogger.near_miss(snap.code, snap.name, "PRE_SURGE",
            actual=snap.chejan_strength, threshold=chejan_min,
            reason=f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%")
        return None

    chejan_max = getattr(cfg, "pre_surge_chejan_max", 700.0)
    if snap.chejan_strength >= chejan_max:
        ScannerLogger.near_miss(snap.code, snap.name, "PRE_SURGE",
            actual=snap.chejan_strength, threshold=chejan_max,
            reason=f"체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {chejan_max:.0f}%")
        return None

    rsi_max = getattr(cfg, "pre_surge_rsi_max", 88.0)
    if snap.rsi > 0 and snap.rsi >= rsi_max:
        ScannerLogger.near_miss(snap.code, snap.name, "PRE_SURGE",
            actual=snap.rsi, threshold=rsi_max,
            reason=f"RSI 과매수 차단 — {snap.rsi:.1f} ≥ {rsi_max:.1f}")
        return None

    if snap.volume <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE", "거래량 없음")
        return None

    return f"PRE_SURGE 시간외 등락 {chg:+.2f}% / 체결강도 {snap.chejan_strength:.0f}% / 거래량 {snap.volume:,}"

def check_opening_surge(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
) -> Optional[str]:
    """
    OPENING_SURGE — 09:00~09:16 정규장 초반 (1분봉 < 8개).
    """
    surge_max = getattr(cfg, "entry_open_surge_max_opening", getattr(cfg, "entry_open_surge_max", 7.0))
    if snap.open_price > 0:
        surge_from_open = (snap.current_price - snap.open_price) / snap.open_price * 100
        if surge_from_open >= surge_max:
            ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
                f"시가 대비 이미 {surge_from_open:.2f}% 상승 ≥ 상한 {surge_max:.1f}%")
            return None

    chg     = float(snap.change_pct or 0)
    chg_min = getattr(cfg, "opening_surge_chg_min", 1.0)
    chg_max = getattr(cfg, "max_change_pct_opening", getattr(cfg, "max_change_pct", 20.0))
    if not (chg_min <= chg < chg_max):
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"등락률 범위 미충족 — {chg:+.2f}% (기준 {chg_min:.1f}%~{chg_max:.1f}%)")
        return None

    chejan_min = getattr(cfg, "opening_surge_chejan_min", 120.0)
    if snap.chejan_strength < chejan_min:
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%")
        return None

    vol_mult = getattr(cfg, "opening_surge_vol_mult", 1.2)
    vols = list(snap.volumes_1min) if snap.volumes_1min else []
    if len(vols) >= 2:
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
        if avg_vol > 0 and vols[-1] < avg_vol * vol_mult:
            ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
                f"거래량 미달 — {vols[-1]:,} < 평균 {avg_vol:,.0f} × {vol_mult:.1f}")
            return None

    return f"OPENING_SURGE 등락 {chg:+.2f}% / 체결강도 {snap.chejan_strength:.0f}% / 거래량 {snap.volume:,}"

def check_opening_scalp(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
) -> Optional[str]:
    """
    Phase 1 모닝 스캘핑 진입 신호 (09:00~09:30).
    """
    min_candles = int(getattr(cfg, "phase1_min_candles", 3))
    if len(snap.closes_1min) < min_candles:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CANDLES", f"1분봉 {len(snap.closes_1min)}개 < 최소 {min_candles}개")
        return None

    if snap.open_price > 0 and snap.current_price < snap.open_price:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_DIRECTION", f"시가 하방 — 현재가 {snap.current_price:,} < 시가 {snap.open_price:,}")
        return None

    open_rise_max = float(getattr(cfg, "phase1_open_rise_max", 8.0))
    if snap.open_price > 0:
        open_rise = (snap.current_price - snap.open_price) / snap.open_price * 100
        if open_rise > open_rise_max:
            ScannerLogger.rejected(snap.code, snap.name, "SCALP_OPEN_RISE", f"시가 대비 {open_rise:.1f}% 상승 > 상한 {open_rise_max:.1f}%")
            return None
    else:
        open_rise = 0.0

    chejan_min = float(getattr(cfg, "phase1_chejan_min", 120.0))
    chejan_max = float(getattr(cfg, "phase1_chejan_max", 700.0))
    if snap.chejan_strength < chejan_min:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHEJAN", f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%")
        return None
    if snap.chejan_strength >= chejan_max:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHEJAN", f"체결강도 과열 — {snap.chejan_strength:.0f}% ≥ {chejan_max:.0f}%")
        return None

    chg_max = float(getattr(cfg, "phase1_change_pct_max", 15.0))
    if snap.change_pct > chg_max:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHANGE", f"등락률 {snap.change_pct:.1f}% > 상한 {chg_max:.1f}%")
        return None

    reason = f"[SCALP] PRE_SURGE 추적 진입 — 시가 대비 +{open_rise:.1f}% | 체결강도 {snap.chejan_strength:.0f}% | 등락률 {snap.change_pct:+.1f}%"
    ScannerLogger.passed(snap.code, snap.name, "OPENING_SCALP", reason)
    return reason
