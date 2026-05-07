"""
eod.py — 종가매매(End-of-Day, EOD) 전략 신호 평가
"""
from typing import Optional, TYPE_CHECKING
from datetime import datetime, time as dtime
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

def check_eod_entry(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
) -> Optional[str]:
    """
    종가매매(EOD) 진입 신호 판단.
    """
    if not getattr(cfg, "overnight_mode_enabled", False):
        return None

    now = datetime.now().time()
    _start = getattr(cfg, "eod_entry_start", dtime(14, 40, 0))
    _end   = getattr(cfg, "eod_entry_end",   dtime(14, 55, 0))
    if not (_start <= now < _end):
        return None

    # ① 일봉 20MA 상방 + 신고가 근처
    _near_thr = float(getattr(cfg, "eod_near_high_threshold_pct", 3.0))
    _dctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price, _near_thr)

    if not _dctx["above_ma20"] and _dctx["daily_ma20"] > 0:
        ScannerLogger.rejected(snap.code, snap.name, "EOD_MA20", f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {_dctx['daily_ma20']:,.0f}")
        return None

    if not _dctx["near_high"]:
        ScannerLogger.rejected(snap.code, snap.name, "EOD_NEAR_HIGH", f"25일 신고가 근처 아님 — 현재가 {snap.current_price:,}, 25일고가 {_dctx['high_25d']:,.0f}")
        return None

    # ② 일봉 정배열
    _align = IndicatorService.check_daily_alignment(snap.daily_closes, snap.current_price)
    if not _align["is_aligned"]:
        ScannerLogger.rejected(snap.code, snap.name, "EOD_ALIGN", "일봉 정배열 미충족 (5MA > 10MA > 20MA)")
        return None

    # ②-b 분봉 추세 강도
    _eod_min_trend = int(getattr(cfg, "eod_min_trend_level", 2))
    _trend_lv = int(getattr(snap, "trend_level", 0))
    if _trend_lv < _eod_min_trend:
        ScannerLogger.rejected(snap.code, snap.name, "EOD_TREND", f"분봉 추세 미달 — level {_trend_lv} < {_eod_min_trend}")
        return None

    # ③ 당일 등락률
    _chg_min = float(getattr(cfg, "eod_change_pct_min", 2.0))
    _chg_max = float(getattr(cfg, "eod_change_pct_max", 10.0))
    chg = snap.change_pct
    if not (_chg_min <= chg <= _chg_max):
        ScannerLogger.rejected(snap.code, snap.name, "EOD_CHANGE", f"등락률 {chg:+.2f}% 범위 밖 (+{_chg_min}%~+{_chg_max}%)")
        return None

    # ④ 체결강도
    _str_min = float(getattr(cfg, "eod_strength_min", 115.0))
    if snap.chejan_strength < _str_min:
        ScannerLogger.rejected(snap.code, snap.name, "EOD_STRENGTH", f"체결강도 {snap.chejan_strength:.1f}% < 기준 {_str_min:.0f}%")
        return None

    # ⑤ 거래량
    _vol_ratio = float(getattr(cfg, "eod_volume_ratio_min", 1.5))
    _vols = snap.volumes_1min
    if _vols and len(_vols) >= 10:
        _avg_vol_1min = sum(_vols[-10:]) / 10.0
        _cur_vol_1min = _vols[-1] if _vols else 0
        if _avg_vol_1min > 0 and _cur_vol_1min < _avg_vol_1min * _vol_ratio:
            ScannerLogger.rejected(snap.code, snap.name, "EOD_VOLUME", f"최근 1분봉 거래량 {_cur_vol_1min:,} < 10분평균 {_avg_vol_1min:,.0f} × {_vol_ratio:.1f}배")
            return None

    reason = f"[EOD] 종가매매 진입 — 등락률 {chg:+.2f}% | 체결강도 {snap.chejan_strength:.1f}% | 신고가근처 | 일봉정배열↑"
    ScannerLogger.passed(snap.code, snap.name, "EOD_ENTRY", reason)
    return reason
