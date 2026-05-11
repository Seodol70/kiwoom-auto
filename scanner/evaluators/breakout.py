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

    # [FIX 2026-05-11] FID 13 거래대금 부정확 → 분봉 거래량으로 변경
    # [FIX 2026-05-11] 분봉 데이터 부족 시 우선순위 변경: 절대값(rank) > 분봉거래량
    vols = list(snap.volumes_1min) if snap.volumes_1min else []

    # 거래량 필터: 분봉 데이터 2개 이상 있을 때만 검사
    if len(vols) >= 2 and volume_mult > 0:
        recent_vols = vols[:-1]  # 직전 데이터들
        avg_vol_1min = sum(recent_vols) / len(recent_vols)
        cur_vol_1min = vols[-1]
        if avg_vol_1min > 0 and cur_vol_1min < avg_vol_1min * volume_mult:
            ScannerLogger.rejected(
                snap.code, snap.name, "BREAKOUT",
                f"거래량 미달 ({cur_vol_1min:,}주 < 평균 {avg_vol_1min:,.0f}주 × {volume_mult:.1f}배)",
            )
            return None
    elif len(vols) < 2 and volume_mult > 0:
        # 분봉 데이터 부족 시 로그만 남기고 계속 진행 (순위 기반 필터에 의존)
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"분봉 데이터 부족 ({len(vols)}/2 필요) — 순위 필터로 대체",
        )
        # return None 안 함 — 진행 계속

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
