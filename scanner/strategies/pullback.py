from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class PullbackStrategy(BaseStrategy):
    """
    눌림목 진입 전략 (PULLBACK).
    상승 추세 중 EMA20 근처로 눌림이 발생할 때 진입.
    """

    def __init__(self):
        super().__init__("PULLBACK")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig, 
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        tlv = int(getattr(snap, "trend_level", 0))
        if tlv < 2: return None

        closes = snap.closes_1min
        if len(closes) < 20: return None

        ema20 = IndicatorService.calc_ema(closes, 20)
        rsi = IndicatorService.calc_rsi(closes, 14)
        if ema20 is None or rsi is None: return None

        # 1. EMA20 근처 확인 (0% ~ +0.8% 이내)
        dist = (snap.current_price - ema20) / ema20 * 100
        if not (0.0 <= dist <= 0.8): return None

        # 2. RSI 과열 해소 확인 (40 ~ 58)
        if not (40.0 <= rsi <= 58.0): return None

        # 3. 거래량 확인 (일시적 거래 감소)
        vols = snap.volumes_1min
        if len(vols) >= 5:
            avg_v5 = sum(vols[-6:-1]) / 5
            if vols[-1] > avg_v5 * 1.5: return None

        reason = f"[PULLBACK] EMA20지지({dist:.2f}%) | RSI {rsi:.1f} | 추세Lv{tlv}"

        # AI 피처 추출
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        # 추가 메타 정보 저장
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        change_pct = float(getattr(snap, "change_pct", 0) or 0)
        if candle_low > 0:
            ai_features["entry_candle_low"] = candle_low
        if change_pct != 0:
            ai_features["change_pct"] = change_pct

        return ScanSignal(
            snap.code, snap.name, self.name, snap.current_price, reason,
            is_warmup=False,
            values=ai_features
        )
