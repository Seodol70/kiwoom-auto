"""
pullback.py — 눌림목(Pullback) 전략 신호 평가
"""
from typing import Optional, TYPE_CHECKING
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

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

    ema20 = IndicatorService.calc_ema(closes, 20)
    rsi = IndicatorService.calc_rsi(closes, 14)
    if ema20 is None or rsi is None:
        return None

    # 1. EMA20 근처 확인 (0% ~ +0.8% 이내)
    dist = (snap.current_price - ema20) / ema20 * 100
    if not (0.0 <= dist <= 0.8):
        return None

    # 2. RSI 과열 해소 확인 (40 ~ 58)
    if not (40.0 <= rsi <= 58.0):
        return None

    # 3. 거래량 확인 (일시적 거래 감소 확인)
    vols = snap.volumes_1min
    if len(vols) >= 5:
        avg_v5 = sum(vols[-6:-1]) / 5
        if vols[-1] > avg_v5 * 1.5:
             return None

    reason = f"[PULLBACK] EMA20지지({dist:.2f}%) | RSI {rsi:.1f} | 추세Lv{tlv}"
    ScannerLogger.passed(snap.code, snap.name, "PULLBACK", reason)
    return reason
