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

    # [REMOVED 2026-05-11-v3] FID 13 기반 분봉 거래량 필터 완전 제거
    # 사유: 분봉 거래량(1~100주대)이 너무 작아서 배수 필터(0.5배)가 의미 없음
    # 대체: 순위 기반 필터(min_daily_rank=100) + 체결강도 필터로 충분
    # 참고: check_breakout_gate()에서 체결강도(min_chejan_strength) 검사

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
    import logging
    logger = logging.getLogger(__name__)

    now = datetime.now().time()
    logger.warning("[check_breakout_gate] 시작: %s(%s) now=%s", snap.code, snap.name, now)

    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        msg = f"진입 허용 시간 아님 ({cfg.entry_start_time}~{cfg.entry_end_time})"
        logger.warning("[check_breakout_gate] 시간필터 거절: %s", msg)
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_TIME", msg)
        return None

    _slot       = _resolve_time_slot(now, cfg)
    _eff_ch_max = _get_slot_value(_slot, cfg, "max_change_pct", cfg.max_change_pct)
    _snap_chg   = float(getattr(snap, "change_pct", 0) or 0)
    logger.warning("[check_breakout_gate] 등락률 체크: snap=%s, max=%s, slot=%s",
                  _snap_chg, _eff_ch_max, _slot)
    if _snap_chg >= _eff_ch_max:
        msg = f"[{_slot}] 등락률 {_snap_chg:.2f}% ≥ 구간 상한 {_eff_ch_max:.0f}%"
        logger.warning("[check_breakout_gate] 등락률 거절: %s", msg)
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_CHGPCT", msg)
        return None

    _eff_chejan = _get_slot_value(_slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
    logger.warning("[check_breakout_gate] 체결강도 체크: snap=%s, min=%s, slot=%s",
                  snap.chejan_strength, _eff_chejan, _slot)
    if snap.chejan_strength < _eff_chejan:
        msg = f"[{_slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {_eff_chejan:.0f}%"
        logger.warning("[check_breakout_gate] 체결강도 거절: %s", msg)
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN",
            actual=snap.chejan_strength, threshold=_eff_chejan,
            reason=msg,
        )
        return None

    # BREAKOUT 체결강도 상한 — 슬롯별 차등화 (2026-05-12: OPENING 극단 완화)
    if _slot == "MORNING":
        _chejan_max = getattr(cfg, "breakout_chejan_max_morning", 950.0)
    elif _slot == "OPENING":
        _chejan_max = getattr(cfg, "breakout_chejan_max_opening", 1500.0)  # OPENING: 극한 완화
    else:
        _chejan_max = getattr(cfg, "breakout_chejan_max", 800.0)
    if snap.chejan_strength >= _chejan_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN_MAX",
            actual=snap.chejan_strength, threshold=_chejan_max,
            reason=f"[{_slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {_chejan_max:.0f}%",
        )
        return None

    # BREAKOUT RSI 상한 — OPENING 슬롯에서는 스킵 (2026-05-12: 극단 변동성 대응)
    if _slot != "OPENING":
        _rsi_max = getattr(cfg, "breakout_rsi_max", 80.0)
        if snap.rsi > 0 and snap.rsi >= _rsi_max:
            ScannerLogger.near_miss(
                snap.code, snap.name, "BREAKOUT_RSI_MAX",
                actual=snap.rsi, threshold=_rsi_max,
                reason=f"[{_slot}] RSI 과매수 차단 — {snap.rsi:.1f} ≥ {_rsi_max:.1f}",
            )
            return None

    # VWAP 필터 — 활성화 (2026-05-13: 거짓 신호 필터링)
    r_vwap = check_vwap_filter(snap)
    if not r_vwap:
        logger.warning("[check_breakout_gate] VWAP 거절: %s(%s)", snap.code, snap.name)
        return None

    result = f"[{_slot}] 체결강도 {snap.chejan_strength:.0f}% | 등락률 {_snap_chg:.1f}% | {r_vwap}"
    logger.warning("[check_breakout_gate] 완료(통과): %s(%s) → %s", snap.code, snap.name, result)
    return result
