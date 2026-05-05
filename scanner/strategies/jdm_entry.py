from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING, Tuple, Dict
from datetime import datetime, time as dtime
from dataclasses import dataclass

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

@dataclass
class JdmCtx:
    """JDM 전략 내부 컨텍스트"""
    now:           dtime
    slot:          str
    eff_chejan:    float
    eff_vol_mult:  float
    eff_rsi_min:   float
    eff_ma_spread: float
    scoring_bonus: bool
    trend_lv:      int
    candle_skip_lv: int
    lite_mode:     bool
    closes:        list
    highs:         list
    lows:          list
    is_warmup:     bool = False
    _rsi:          Optional[float] = None

class JdmStrategy(BaseStrategy):
    """
    JDM(Joseph) 진입 전략.
    복합적인 수급, 추세, 지표 필터를 통과해야 함.
    """

    def __init__(self):
        super().__init__("JDM_ENTRY")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig, 
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        # 1. 컨텍스트 빌드
        ctx = self._build_ctx(snap, cfg)
        if ctx is None:
            return None

        # 2. MA 골든크로스 + 추세 필터
        ma_result = self._check_trend_and_ma(snap, cfg, ctx)
        if ma_result is None:
            return None
        spread_tag, rsi_tag = ma_result

        # 3. 실행 품질 체크 (가장 복잡한 필터군)
        exec_result = self._check_execution_quality(snap, cfg, ctx)
        if exec_result is None:
            return None
        r_vol, r_chej, candle_reason = exec_result

        # 4. 일봉 컨텍스트 체크
        daily_ctx = self._check_daily_context(snap, cfg, ctx)
        if daily_ctx is None:
            return None

        # 5. 신호 생성
        mode_tag = "JDM_LITE" if ctx.lite_mode else "JDM"
        near_tag = " | 📈신고가근처(TP↑)" if daily_ctx["near_high"] else ""
        warm_tag = " | [WARMUP]" if ctx.is_warmup else ""
        reason = f"[{ctx.slot}][{mode_tag}]{warm_tag} {r_vol} | {r_chej} | {spread_tag} | {rsi_tag} | {candle_reason}{near_tag}"
        
        ScannerLogger.passed(snap.code, snap.name, mode_tag, reason)
        
        # AI 피처 추출
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        return ScanSignal(
            snap.code, snap.name, self.name, snap.current_price, reason,
            entry_candle_low=int(snap.lows_1min[-1]) if snap.lows_1min else 0,
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
            is_warmup=ctx.is_warmup,
            values=ai_features
        )

    def _build_ctx(self, snap: StockSnapshot, cfg: SmartScannerConfig) -> Optional[JdmCtx]:
        from scanner.signal_evaluator import _resolve_time_slot, _get_slot_value
        
        # 수급 절대치 필터
        amt = snap.trade_amount
        rank = getattr(snap, "rank", 999)
        if rank > cfg.min_daily_rank and amt < cfg.min_trade_amount:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_LIQUIDITY", f"수급부족(Rank{rank}, {amt/1e8:.1f}억)")
            return None

        # 시가 대비 상승도
        if snap.open_price > 0:
            surge = (snap.current_price - snap.open_price) / snap.open_price * 100
            surge_cap = float(cfg.entry_open_surge_max)
            trend_lvl = int(getattr(snap, "trend_level", 0))
            if trend_lvl >= 2: surge_cap = max(surge_cap, 15.0)
            if surge >= surge_cap:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_SURGE", f"시가대비 {surge:.1f}% 상승(제한 {surge_cap}%)")
                return None

        now = datetime.now().time()
        if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
            return None

        slot = _resolve_time_slot(now, cfg)
        return JdmCtx(
            now=now, slot=slot,
            eff_chejan=_get_slot_value(slot, cfg, "min_chejan_strength", cfg.min_chejan_strength),
            eff_vol_mult=_get_slot_value(slot, cfg, "volume_surge_mult", cfg.volume_1min_surge_mult),
            eff_rsi_min=_get_slot_value(slot, cfg, "jdm_rsi_entry_min", cfg.jdm_rsi_entry_min),
            eff_ma_spread=float(getattr(cfg, "jdm_ma_spread_pct", 0.15)),
            scoring_bonus=(rank <= 10),
            trend_lv=int(getattr(snap, "trend_level", 0)),
            candle_skip_lv=int(getattr(cfg, "jdm_candle_skip_trend_level", 2)),
            lite_mode=(slot == "OPENING"),
            closes=snap.closes_1min, highs=snap.highs_1min, lows=snap.lows_1min
        )

    def _check_trend_and_ma(self, snap: StockSnapshot, cfg: SmartScannerConfig, ctx: JdmCtx) -> Optional[Tuple[str, str]]:
        closes = ctx.closes
        if ctx.lite_mode:
            ma_s = IndicatorService.calc_ma(closes, cfg.jdm_ma_short)
            pma_s = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_short)
            if ma_s is None or pma_s is None: return None
            if not (ma_s > pma_s and snap.current_price > ma_s):
                ScannerLogger.rejected(snap.code, snap.name, "JDM_LITE", f"MA{cfg.jdm_ma_short} 하락 또는 현재가 하단")
                return None
            return (f"MA{cfg.jdm_ma_short}↑", "")
        else:
            ma_s = IndicatorService.calc_ma(closes, cfg.jdm_ma_short)
            ma_l = IndicatorService.calc_ma(closes, cfg.jdm_ma_long)
            pma_s = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_short)
            pma_l = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_long)
            rsi = IndicatorService.calc_rsi(closes, 14)
            if any(v is None for v in [ma_s, ma_l, pma_s, pma_l, rsi]): return None
            
            golden = pma_s <= pma_l and ma_s > ma_l
            if not golden:
                if ctx.trend_lv >= 2 and ma_s > ma_l:
                    pass # GC 오버라이드
                else:
                    ScannerLogger.rejected(snap.code, snap.name, "JDM", "GC 미충족")
                    return None
            
            spread_pct = (ma_s - ma_l) / ma_l * 100 if ma_l > 0 else 0
            if spread_pct < ctx.eff_ma_spread:
                ScannerLogger.rejected(snap.code, snap.name, "JDM", f"이격부족({spread_pct:.2f}%)")
                return None
            
            ctx._rsi = rsi
            return (f"MA{cfg.jdm_ma_short}/{cfg.jdm_ma_long}({spread_pct:.2f}%)", f"RSI{rsi:.0f}")

    def _check_execution_quality(self, snap: StockSnapshot, cfg: SmartScannerConfig, ctx: JdmCtx) -> Optional[Tuple[str, str, str]]:
        from scanner.signal_evaluator import (
            check_volume_surge, check_chejan_strength, check_indicator_warmup,
            check_bullish_engulfing, check_bullish_pin_bar
        )
        closes, highs, lows = ctx.closes, ctx.highs, ctx.lows
        
        # 0. 워밍업
        warmup = check_indicator_warmup(snap, 15)
        ctx.is_warmup = bool(warmup)

        # 1. 거래량
        r_vol = check_volume_surge(snap, ctx.eff_vol_mult, getattr(cfg, "volume_surge_lookback", 10))
        if r_vol is None: return None
        
        # 2. 체결 가속도
        vel_mult = float(getattr(cfg, "exec_velocity_mult", 1.8))
        if snap.exec_velocity_ratio > 0 and snap.exec_velocity_ratio < vel_mult:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_EXEC_VEL", f"가속도부족({snap.exec_velocity_ratio:.2f})")
            return None

        # 3. 체결강도
        r_chej = check_chejan_strength(snap, ctx.eff_chejan)
        if r_chej is None: return None
        
        chejan_max = float(getattr(cfg, "jdm_chejan_max_opening" if ctx.slot == "OPENING" else "jdm_chejan_max", 700.0))
        if snap.chejan_strength >= chejan_max:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_CHEJAN_MAX", f"강도과열({snap.chejan_strength:.0f}%)")
            return None

        # 4. EMA 이격
        if len(closes) >= 20:
            ema10 = IndicatorService.calc_ema(closes, 10)
            ema20 = IndicatorService.calc_ema(closes, 20)
            if ema10 and ema20 and ema20 > 0:
                disp = (ema10 - ema20) / ema20 * 100
                if disp >= 3.0: # 기본 3% 상한
                    ScannerLogger.rejected(snap.code, snap.name, "JDM_EMA", f"EMA이격과열({disp:.1f}%)")
                    return None

        # 5. RSI 상한
        if not ctx.lite_mode and ctx._rsi is not None:
            rsi_high = 80.0 # 기본
            if ctx.is_warmup: rsi_high = 88.0
            if ctx._rsi >= rsi_high:
                ScannerLogger.near_miss(snap.code, snap.name, "JDM_RSI", actual=ctx._rsi, threshold=rsi_high)
                return None

        # 6. 캔들 패턴
        if not ctx.lite_mode:
            if ctx.trend_lv >= ctx.candle_skip_lv:
                candle_reason = "TREND_SKIP"
            else:
                r_engulf = check_bullish_engulfing(snap)
                r_pinbar = check_bullish_pin_bar(snap)
                if r_engulf is None and r_pinbar is None:
                    ScannerLogger.rejected(snap.code, snap.name, "JDM_CANDLE", "패턴미충족")
                    return None
                candle_reason = r_engulf or r_pinbar
        else:
            candle_reason = "LITE_SKIP"

        return (r_vol, r_chej, candle_reason)

    def _check_daily_context(self, snap: StockSnapshot, cfg: SmartScannerConfig, ctx: JdmCtx) -> Optional[Dict]:
        if not ctx.lite_mode and cfg.pivot_r2_enabled:
            r2 = IndicatorService.calc_pivot_r2(snap.daily_high_prev, snap.daily_low_prev, snap.prev_close)
            if r2 > 0 and snap.current_price < r2:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_PIVOT", f"R2미충족({snap.current_price:,}<{r2:,.0f})")
                return None

        if cfg.daily_alignment_enabled and len(snap.daily_closes) >= 20:
            align = IndicatorService.check_daily_alignment(snap.daily_closes, snap.current_price)
            if not align["is_aligned"]:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_ALIGN", "일봉역배열")
                return None

        near_high_thr = float(getattr(cfg, "daily_near_high_threshold_pct", 3.0))
        daily_ctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price, near_high_thr)
        
        if getattr(cfg, "daily_ma20_filter_enabled", True):
            if not daily_ctx["above_ma20"] and daily_ctx["daily_ma20"] > 0:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_DAILY_MA20", "일봉20MA하단")
                return None

        return daily_ctx
