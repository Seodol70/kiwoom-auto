"""
testa.py — 테스타(Testa) 정배열 신호 평가
"""
from typing import Optional, TYPE_CHECKING
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

if TYPE_CHECKING:
    from scanner.models import StockSnapshot

def check_testa_alignment(
    snap: "StockSnapshot",
    max_ma_spread: float = 0.05,
) -> Optional[str]:
    """
    테스타 정배열 확인: MA10 > MA20 > MA50 + 이격도 과열 필터.
    """
    closes = snap.closes_1min
    if len(closes) < 50:
        ScannerLogger.rejected(snap.code, snap.name, "TESTA", f"1분봉 데이터 부족 ({len(closes)}/50)")
        return None

    ma10 = IndicatorService.calc_ma(closes, 10)
    ma20 = IndicatorService.calc_ma(closes, 20)
    ma50 = IndicatorService.calc_ma(closes, 50)

    if any(v is None for v in [ma10, ma20, ma50]):
        ScannerLogger.rejected(snap.code, snap.name, "TESTA", "MA 계산 실패")
        return None

    if not (ma10 > ma20 > ma50):
        ScannerLogger.rejected(snap.code, snap.name, "TESTA",
            f"정배열 미충족 MA10={ma10:.0f} MA20={ma20:.0f} MA50={ma50:.0f}")
        return None

    spread = (ma10 - ma50) / ma50 if ma50 > 0 else 0.0
    if spread > max_ma_spread:
        ScannerLogger.rejected(snap.code, snap.name, "TESTA",
            f"MA 이격 과열 {spread:.1%} > {max_ma_spread:.0%} — 설거지 위험")
        return None

    return f"TESTA_ALIGNED(Spread={spread:.1%})"
