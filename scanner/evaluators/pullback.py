"""
pullback.py — 눌림목(Pullback) 전략 신호 평가
"""
from typing import Optional, TYPE_CHECKING
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService
from .common import check_vwap_filter

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

def check_pullback_entry(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
) -> Optional[str]:
    """
    상승 추세(trend_level >= 2) 종목이 EMA20 근처까지 눌렸을 때 진입.
    """
    tlv = int(getattr(snap, "trend_level", 0))
    if tlv < 2:
        return None

    closes = snap.closes_1min
    if len(closes) < 20:
        return None

    # [FIX 2026-05-28] 일봉 정배열 락 (추세의 뼈대) — 미니 제미니 조언 반영
    # [FIX 2026-05-29] 일봉 0개이면 통과 — daily_refresh 미연결로 전종목 차단되는 문제 해소
    if len(snap.daily_closes) >= 23:
        _daily_ctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price)
        if getattr(cfg, "daily_ma20_filter_enabled", True):
            if not _daily_ctx["above_ma20"] and _daily_ctx["daily_ma20"] > 0:
                ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_DAILY_MA20",
                    f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {_daily_ctx['daily_ma20']:,.0f}")
                return None
        if getattr(cfg, "daily_ma20_slope_enabled", True):
            if not _daily_ctx.get("ma20_slope_up", True):
                ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_MA20_SLOPE",
                    f"일봉 20MA 기울기 하락 — 추세역배열 차단")
                return None

    ema20 = IndicatorService.calc_ema(closes, 20)
    rsi = IndicatorService.calc_rsi(closes, 14)
    if ema20 is None or rsi is None:
        return None

    # 1. EMA20 근처 확인 (0.2% ~ +2.0% 이내)
    # [FIX 2026-06-01] 하한 0.0→0.2% — 이격 0~0.2% 구간 승률 11%(9건 중 1건)
    # EMA20에 너무 바짝 붙은 경우 하락 중 일시적 접촉일 가능성 높음
    dist = (snap.current_price - ema20) / ema20 * 100
    if not (0.2 <= dist <= 2.0):
        ScannerLogger.rejected(snap.code, snap.name, "PULLBACK",
            f"EMA20 이격 범위 벗어남 ({dist:.2f}%, 허용: 0.2%~2.0%)")
        return None

    # 2. RSI 상승 모멘텀 확인 (50 ~ 70) — 2026-05-13: 40~58→50~70 (강한 상승만 진입)
    if not (50.0 <= rsi <= 70.0):
        return None

    # 3. 거래량 확인 (일시적 거래 감소 확인)
    vols = snap.volumes_1min
    if len(vols) >= 5:
        avg_v5 = sum(vols[-6:-1]) / 5
        if vols[-1] > avg_v5 * 1.5:
             return None

    # 4. VWAP 필터 — 활성화 (2026-05-13: 거짓 신호 필터링)
    r_vwap = check_vwap_filter(snap)
    if not r_vwap:
        return None

    reason = f"[PULLBACK] EMA20지지({dist:.2f}%) | RSI {rsi:.1f} | 추세Lv{tlv} | {r_vwap}"
    ScannerLogger.passed(snap.code, snap.name, "PULLBACK", reason)
    return reason
