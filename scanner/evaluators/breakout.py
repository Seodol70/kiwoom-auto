"""
breakout.py — 돌파(Breakout) 전략 신호 평가
"""
from typing import Optional, TYPE_CHECKING
from datetime import datetime, time as dtime
from scanner.scanner_logger import ScannerLogger
from .common import _resolve_time_slot, _get_slot_value, check_vwap_filter

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

def check_breakout(
    snap:                    "StockSnapshot",
    breakout_ratio:          float = 0.03,
    volume_mult:             float = 1.0,
    pullback_from_high_pct:  float = 1.5,
    min_rising_bars:         int   = 2,
) -> Optional[str]:
    """
    단기 박스권/전고점 돌파 여부를 확인한다.
    """
    if snap.prev_close <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT", "prev_close=0")
        return None

    threshold = snap.prev_close * (1 + breakout_ratio)

    if snap.current_price < threshold:
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"현재가 {snap.current_price:,} < 돌파기준 {threshold:,.0f}",
        )
        return None

    avg_vol = snap.trade_amount / snap.current_price if snap.current_price else 0
    if snap.trade_amount > 0 and (avg_vol <= 0 or snap.volume < avg_vol * volume_mult):
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"거래량 부족 ({snap.volume:,} < 기준 {avg_vol * volume_mult:,.0f})",
        )
        return None

    if pullback_from_high_pct > 0 and snap.high_price > 0:
        pullback = (snap.current_price - snap.high_price) / snap.high_price * 100
        if pullback <= -pullback_from_high_pct:
            ScannerLogger.rejected(
                snap.code, snap.name, "BREAKOUT",
                f"고점({snap.high_price:,}) 대비 {pullback:.2f}% 하락 중 "
                f"(차단기준 -{pullback_from_high_pct:.1f}%) — 하락추세",
            )
            return None

    closes = snap.closes_1min
    if min_rising_bars > 0 and len(closes) >= min_rising_bars + 1:
        rising = all(
            closes[-(i + 1)] > closes[-(i + 2)]
            for i in range(min_rising_bars)
        )
        if not rising:
            recent = [int(closes[-(i + 1)]) for i in range(min(min_rising_bars + 1, len(closes)))]
            recent_str = " → ".join(f"{p:,}" for p in reversed(recent))
            ScannerLogger.rejected(
                snap.code, snap.name, "BREAKOUT",
                f"1분봉 연속상승 {min_rising_bars}개 미충족 ({recent_str}) — 하락/횡보",
            )
            return None

    reason = (
        f"전일종가 {snap.prev_close:,} 대비 {breakout_ratio*100:.1f}% 돌파 "
        f"| 현재가 {snap.current_price:,}"
    )
    ScannerLogger.passed(snap.code, snap.name, "BREAKOUT", reason)
    return reason

def check_breakout_gate(snap: "StockSnapshot", cfg: "SmartScannerConfig") -> Optional[str]:
    """
    BREAKOUT 확인 후 진입 가능 여부를 검증하는 공통 게이트.
    """
    now = datetime.now().time()
    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_TIME",
            f"진입 허용 시간 아님 ({cfg.entry_start_time}~{cfg.entry_end_time})")
        return None

    _slot       = _resolve_time_slot(now, cfg)
    _eff_ch_max = _get_slot_value(_slot, cfg, "max_change_pct", cfg.max_change_pct)
    _snap_chg   = float(getattr(snap, "change_pct", 0) or 0)
    if _snap_chg >= _eff_ch_max:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_CHGPCT",
            f"[{_slot}] 등락률 {_snap_chg:.2f}% ≥ 구간 상한 {_eff_ch_max:.0f}%")
        return None

    _eff_chejan = _get_slot_value(_slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
    if snap.chejan_strength < _eff_chejan:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN",
            actual=snap.chejan_strength, threshold=_eff_chejan,
            reason=f"[{_slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {_eff_chejan:.0f}%",
        )
        return None

    if _slot == "MORNING":
        _chejan_max = getattr(cfg, "breakout_chejan_max_morning", 950.0)
    else:
        _chejan_max = getattr(cfg, "breakout_chejan_max", 800.0)
    if snap.chejan_strength >= _chejan_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN_MAX",
            actual=snap.chejan_strength, threshold=_chejan_max,
            reason=f"[{_slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {_chejan_max:.0f}%",
        )
        return None

    _rsi_max = getattr(cfg, "breakout_rsi_max", 80.0)
    if snap.rsi > 0 and snap.rsi >= _rsi_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_RSI_MAX",
            actual=snap.rsi, threshold=_rsi_max,
            reason=f"[{_slot}] RSI 과매수 차단 — {snap.rsi:.1f} ≥ {_rsi_max:.1f}",
        )
        return None

    r_vwap = check_vwap_filter(snap)
    if r_vwap is None:
        return None

    return f"[{_slot}] 체결강도 {snap.chejan_strength:.0f}% | 등락률 {_snap_chg:.1f}% | {r_vwap}"
