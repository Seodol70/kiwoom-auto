from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING
from datetime import datetime

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class BreakoutStrategy(BaseStrategy):
    """
    돌파 매매 전략 (BREAKOUT).
    전일 종가 대비 특정 비율 이상 돌파 시 진입.
    """

    def __init__(self):
        super().__init__("BREAKOUT")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig) -> Optional[ScanSignal]:
        # 1. 기본 돌파 체크 (SignalEvaluator.check_breakout 로직)
        breakout_reason = self._check_breakout_core(snap, cfg)
        if not breakout_reason:
            return None

        # 2. 진입 게이트 체크 (SignalEvaluator.check_breakout_gate 로직)
        gate_reason = self._check_gate(snap, cfg)
        if not gate_reason:
            return None

        # 3. 신호 생성
        reason = f"{breakout_reason} | {gate_reason}"
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        
        # AI 피처 추출
        ai_features = IndicatorService.get_ai_features(snap, config=cfg)

        return ScanSignal(
            snap.code, snap.name, self.name, snap.current_price, reason,
            entry_candle_low=candle_low,
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
            is_warmup="[WARMUP]" in reason,
            values=ai_features
        )

    def _check_breakout_core(self, snap: StockSnapshot, cfg: SmartScannerConfig) -> Optional[str]:
        if snap.prev_close <= 0 or snap.current_price <= 0:
            return None

        threshold = snap.prev_close * (1 + cfg.breakout_ratio)
        if snap.current_price < threshold:
            ScannerLogger.rejected(snap.code, snap.name, self.name, 
                                   f"현재가 {snap.current_price:,} < 돌파기준 {threshold:,.0f}")
            return None

        # 거래량 체크
        avg_vol = snap.trade_amount / snap.current_price if snap.current_price else 0
        if snap.trade_amount > 0 and (avg_vol <= 0 or snap.volume < avg_vol * cfg.breakout_volume_mult):
            ScannerLogger.rejected(snap.code, snap.name, self.name, 
                                   f"거래량 부족 ({snap.volume:,} < 기준 {avg_vol * cfg.breakout_volume_mult:,.0f})")
            return None

        # 고점 대비 하락폭 차단
        pb_limit = cfg.breakout_pullback_from_high_pct
        tlv = int(getattr(snap, "trend_level", 0))
        if tlv >= 2: pb_limit = 5.0
        elif tlv == 1: pb_limit = 3.0

        if pb_limit > 0 and snap.high_price > 0:
            pullback = (snap.current_price - snap.high_price) / snap.high_price * 100
            if pullback <= -pb_limit:
                ScannerLogger.rejected(snap.code, snap.name, self.name, 
                                       f"고점({snap.high_price:,}) 대비 {pullback:.2f}% 하락 (기준 -{pb_limit:.1f}%)")
                return None

        # 1분봉 연속 상승 확인
        min_rising = cfg.breakout_min_rising_bars
        closes = snap.closes_1min
        if min_rising > 0 and len(closes) >= min_rising + 1:
            rising = all(closes[-(i + 1)] > closes[-(i + 2)] for i in range(min_rising))
            if not rising:
                ScannerLogger.rejected(snap.code, snap.name, self.name, f"1분봉 연속상승 {min_rising}개 미충족")
                return None

        return f"전일종가 {snap.prev_close:,} 대비 {cfg.breakout_ratio*100:.1f}% 돌파"

    def _check_gate(self, snap: StockSnapshot, cfg: SmartScannerConfig) -> Optional[str]:
        from scanner.signal_evaluator import _resolve_time_slot, _get_slot_value, check_vwap_filter
        
        now = datetime.now().time()
        if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
            ScannerLogger.rejected(snap.code, snap.name, f"{self.name}_TIME", "진입 허용 시간 아님")
            return None

        slot = _resolve_time_slot(now, cfg)
        
        # 1. 등락률 상한
        eff_ch_max = _get_slot_value(slot, cfg, "max_change_pct", cfg.max_change_pct)
        snap_chg = float(getattr(snap, "change_pct", 0) or 0)
        if snap_chg >= eff_ch_max:
            ScannerLogger.rejected(snap.code, snap.name, f"{self.name}_CHGPCT", f"[{slot}] 등락률 {snap_chg:.2f}% >= {eff_ch_max:.0f}%")
            return None

        # 2. 체결강도 하한
        eff_chejan = _get_slot_value(slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
        if snap.chejan_strength < eff_chejan:
            ScannerLogger.near_miss(snap.code, snap.name, f"{self.name}_CHEJAN", 
                                    actual=snap.chejan_strength, threshold=eff_chejan)
            return None

        # 3. 체결강도 상한 (과열 차단)
        chejan_max = getattr(cfg, "breakout_chejan_max_morning", 950.0) if slot == "MORNING" else getattr(cfg, "breakout_chejan_max", 800.0)
        if snap.chejan_strength >= chejan_max:
            ScannerLogger.near_miss(snap.code, snap.name, f"{self.name}_CHEJAN_MAX", actual=snap.chejan_strength, threshold=chejan_max)
            return None

        # 4. RSI 상한 (과매수 차단)
        rsi_max = getattr(cfg, "breakout_rsi_max", 80.0)
        if snap.rsi > 0 and snap.rsi >= rsi_max:
            ScannerLogger.near_miss(snap.code, snap.name, f"{self.name}_RSI_MAX", actual=snap.rsi, threshold=rsi_max)
            return None

        # 5. VWAP 필터
        r_vwap = check_vwap_filter(snap)
        if r_vwap is None:
            return None

        return f"Gate통과({slot}) | {r_vwap}"
