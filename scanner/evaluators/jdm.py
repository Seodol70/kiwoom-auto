"""
jdm.py — JDM(Joseph Dynamic Momentum) 전략 신호 평가
"""
from typing import Optional, TYPE_CHECKING, Tuple
from datetime import datetime, time as dtime
from dataclasses import dataclass

from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService
from .common import (
    _resolve_time_slot, _get_slot_value, check_volume_surge,
    check_chejan_strength, check_indicator_warmup,
    check_bullish_engulfing, check_bullish_pin_bar
)

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

@dataclass
class _JdmCtx:
    """check_jdm_entry 서브 함수들이 공유하는 계산된 파라미터."""
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

def _jdm_build_ctx(snap: "StockSnapshot", cfg: "SmartScannerConfig") -> Optional["_JdmCtx"]:
    """슬롯·유효 파라미터 계산. 조기 차단 조건 해당 시 None 반환."""
    # [FIX 2026-05-11] FID 13 거래대금 부정확 + rank=0 문제
    # → 거래대금 필터 임시 비활성화, 거래량 기반으로 대체
    # ── 거래량 기반 유동성 필터 (거래대금 대체)
    min_volume = getattr(cfg, 'min_daily_volume', 100_000)  # 기본값: 10만주
    if snap.volume > 0 and snap.volume < min_volume:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_LIQUIDITY",
            f"거래량 미달 (volume={snap.volume:,}, 기준={min_volume:,})")
        return None

    # ── 시가 대비 상승도 차단
    if snap.open_price > 0:
        surge_from_open = (snap.current_price - snap.open_price) / snap.open_price * 100
        _surge_cap       = float(cfg.entry_open_surge_max)
        _surge_override  = int(getattr(cfg, "surge_trend_override_level", 2))
        _surge_trend_max = float(getattr(cfg, "surge_trend_max_pct", 15.0))
        _snap_trend_lvl  = int(getattr(snap, "trend_level", 0))
        if _surge_override > 0 and _snap_trend_lvl >= _surge_override:
            _surge_cap = max(_surge_cap, _surge_trend_max)
        if surge_from_open >= _surge_cap:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_SURGE", "시가 대비 이미 상승 — 고점 진입 차단")
            return None

    now = datetime.now().time()
    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        ScannerLogger.rejected(snap.code, snap.name, "JDM_TIME", "진입 허용 시간 아님")
        return None

    # ── 슬롯 기반 유효 파라미터 산출
    slot          = _resolve_time_slot(now, cfg)
    if slot == "PRE":
        return None
    
    eff_ch_max    = _get_slot_value(slot, cfg, "max_change_pct",     cfg.max_change_pct)
    eff_chejan    = _get_slot_value(slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
    eff_vol_mult  = _get_slot_value(slot, cfg, "volume_surge_mult",   cfg.volume_1min_surge_mult)
    eff_rsi_min   = _get_slot_value(slot, cfg, "jdm_rsi_entry_min",   cfg.jdm_rsi_entry_min)
    trend_lv      = int(getattr(snap, "trend_level", 0))
    candle_skip_lv = int(getattr(cfg, "jdm_candle_skip_trend_level", 2))

    if trend_lv >= candle_skip_lv:
        rsi_trend_min = float(getattr(cfg, "jdm_rsi_entry_min_trend", 45.0))
        if rsi_trend_min < eff_rsi_min:
            eff_rsi_min = rsi_trend_min

    # ── 스코어링 보너스
    eff_ma_spread  = float(getattr(cfg, "jdm_ma_spread_pct", 0.15))
    scoring_bonus  = False
    rank_bonus     = int(getattr(cfg, "scoring_rank_bonus", 10))
    _rank          = snap.rank if hasattr(snap, 'rank') else None
    if _rank is not None and _rank > 0 and _rank <= rank_bonus:
        scoring_bonus = True
        
    surge_lookback = int(getattr(cfg, "volume_surge_lookback", 10))
    vol_bonus_mult = float(getattr(cfg, "scoring_vol_surge_bonus", 2.0))
    if snap.volumes_1min and len(snap.volumes_1min) >= surge_lookback + 1:
        avg_v = sum(snap.volumes_1min[-(surge_lookback+1):-1]) / surge_lookback
        cur_v = snap.volumes_1min[-1]
        if avg_v > 0 and (cur_v / avg_v) >= vol_bonus_mult:
            scoring_bonus = True
            
    if scoring_bonus:
        eff_rsi_min   = min(eff_rsi_min, 40.0)
        eff_ma_spread = min(eff_ma_spread, 0.10)

    # ── 등락률 상한 체크
    snap_chg        = float(getattr(snap, "change_pct", 0) or 0)
    chg_cap         = float(eff_ch_max)
    surge_override2 = int(getattr(cfg, "surge_trend_override_level", 2))
    surge_trend_max2 = float(getattr(cfg, "surge_trend_max_pct", 15.0))
    snap_trend_lv2  = int(getattr(snap, "trend_level", 0))
    if surge_override2 > 0 and snap_trend_lv2 >= surge_override2:
        chg_cap = max(chg_cap, surge_trend_max2)
    if snap_chg >= chg_cap:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_CHGPCT",
            f"[{slot}] 등락률 {snap_chg:.2f}% ≥ 구간 상한 {chg_cap:.0f}% (trend={snap_trend_lv2})")
        return None

    # ── 캔들 데이터 준비
    closes    = list(snap.closes_1min or [])
    highs     = list(snap.highs_1min  or [])
    lows      = list(snap.lows_1min   or [])
    need_long  = cfg.jdm_ma_long  + 1
    need_short = cfg.jdm_ma_short + 1
    lite_mode  = slot == "OPENING" and need_short <= len(closes) < need_long
    need       = need_short if lite_mode else need_long

    if len(closes) < need:
        ScannerLogger.rejected(snap.code, snap.name, "JDM",
            f"1분봉 데이터 부족 ({len(closes)}/{need}" + (" [OPENING_LITE 대기]" if slot == "OPENING" else "") + ")")
        return None

    if len(closes) >= 2 and closes[-2] > 0:
        slip_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        slip_max = getattr(cfg, "slippage_block_pct", 3.0)
        if slip_pct >= slip_max:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_SLIP",
                f"슬리피지 차단 — 직전 1분봉 대비 {slip_pct:.2f}% 급등 (상한 {slip_max:.1f}%)")
            return None

    return _JdmCtx(
        now=now, slot=slot,
        eff_chejan=eff_chejan, eff_vol_mult=eff_vol_mult,
        eff_rsi_min=eff_rsi_min, eff_ma_spread=eff_ma_spread,
        scoring_bonus=scoring_bonus, trend_lv=trend_lv,
        candle_skip_lv=candle_skip_lv, lite_mode=lite_mode,
        closes=closes, highs=highs, lows=lows,
    )

def _jdm_check_trend_and_ma(
    snap: "StockSnapshot", cfg: "SmartScannerConfig", ctx: "_JdmCtx"
) -> Optional[tuple[str, str]]:
    """요셉 추세 필터 + MA 골든크로스/이격도 체크. (spread_tag, rsi_tag) 또는 None 반환."""
    closes, highs, lows = ctx.closes, ctx.highs, ctx.lows

    # ── 요셉 추세 필터
    if getattr(cfg, "yosep_trend_enabled", True):
        if ctx.slot == "AFTERNOON":
            min_trend = int(getattr(cfg, "yosep_min_trend_level_afternoon", 3))
        elif ctx.slot == "OPENING":
            min_trend = int(getattr(cfg, "yosep_min_trend_level_opening", 0))
        else:
            min_trend = int(getattr(cfg, "yosep_min_trend_level", 1))
        if ctx.trend_lv < min_trend:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_TREND",
                f"요셉 추세 미달 [{ctx.slot}] — level {ctx.trend_lv} < {min_trend}")
            return None
        
        ema_p    = int(getattr(cfg, "yosep_ema_period", 20))
        atr_p    = int(getattr(cfg, "yosep_atr_period", 14))
        down_mult = float(getattr(cfg, "yosep_downtrend_block_atr", 0.8))
        if len(closes) >= ema_p and len(highs) >= atr_p + 1 and len(lows) >= atr_p + 1:
            ema20 = IndicatorService.calc_ema(closes, ema_p)
            atr14 = IndicatorService.calc_atr(highs, lows, closes, atr_p)
            if ema20 is not None and atr14 is not None and atr14 > 0:
                if snap.current_price < (ema20 - atr14 * down_mult):
                    ScannerLogger.rejected(snap.code, snap.name, "JDM_TREND_DOWN",
                        f"하락 추세 강세 — 현재가 {snap.current_price:,} < EMA{ema_p} {ema20:,.0f} - ATR{atr_p}×{down_mult:.1f}")
                    return None

    # ── MA 체크 (라이트 모드 vs 풀 모드)
    rsi: Optional[float] = None
    if ctx.lite_mode:
        ma_s  = IndicatorService.calc_ma(closes,      cfg.jdm_ma_short)
        pma_s = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_short)
        if ma_s is None or pma_s is None:
            return None
        if not (ma_s > pma_s and snap.current_price > ma_s):
            ScannerLogger.rejected(snap.code, snap.name, "JDM_LITE",
                f"MA{cfg.jdm_ma_short} 상승 미충족 — 이전 {pma_s:.0f}→현재 {ma_s:.0f}, 현재가 {snap.current_price:,}")
            return None
        spread_tag = f"MA{cfg.jdm_ma_short}↑ {pma_s:.0f}→{ma_s:.0f}"
        rsi_tag    = ""
    else:
        ma_s  = IndicatorService.calc_ma(closes,      cfg.jdm_ma_short)
        ma_l  = IndicatorService.calc_ma(closes,      cfg.jdm_ma_long)
        rsi   = IndicatorService.calc_rsi(closes, 14)
        pma_s = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_short)
        pma_l = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_long)
        if any(v is None for v in [ma_s, ma_l, rsi, pma_s, pma_l]):
            return None
        
        golden = pma_s <= pma_l and ma_s > ma_l
        # [FIX 2026-05-11] GC_OVERRIDE 기준 완화: Lv2+ → Lv1+ (공격적 완화)
        gc_override = int(getattr(cfg, "jdm_golden_cross_trend_override", 1))
        is_gc_override = False
        if not golden:
            if gc_override > 0 and ctx.trend_lv >= gc_override and ma_s > ma_l:
                is_gc_override = True
                ScannerLogger.passed(snap.code, snap.name, "JDM_GC_OVERRIDE",
                    f"골든크로스 없지만 추세Lv{ctx.trend_lv}+MA정배열 진입 허용 (직전{pma_s:.0f}/{pma_l:.0f}→현재{ma_s:.0f}/{ma_l:.0f})")
            else:
                ScannerLogger.rejected(snap.code, snap.name, "JDM",
                    f"골든크로스 미충족 (직전MA:{pma_s:.0f}/{pma_l:.0f} → 현재MA:{ma_s:.0f}/{ma_l:.0f})")
                return None
        
        if ctx.now >= cfg.ma_alignment_time and not (ma_s > ma_l):
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 정배열 미충족(09:30+) — MA{cfg.jdm_ma_short}:{ma_s:.0f} ≤ MA{cfg.jdm_ma_long}:{ma_l:.0f}")
            return None
            
        spread_abs = float(ma_s) - float(ma_l)
        spread_pct = (spread_abs / float(ma_l) * 100) if float(ma_l) > 0 else 0
        if ma_s <= ma_l:
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 정배열 미충족 (Whipsaw 방지) — MA{cfg.jdm_ma_short}:{ma_s:.0f} <= MA{cfg.jdm_ma_long}:{ma_l:.0f}")
            return None
            
        eff_ma_spread = ctx.eff_ma_spread
        if ctx.slot == "OPENING" or ctx.is_warmup:
            eff_ma_spread *= 0.5
        if spread_pct < eff_ma_spread:
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 이격 부족 ({spread_pct:.2f}% < 최소 {eff_ma_spread:.2f}%)")
            return None
        max_ma_spread = float(getattr(cfg, f"jdm_ma_spread_max_pct_{ctx.slot.lower()}", cfg.jdm_ma_spread_max_pct))
        
        # [NEW] OPENING 갭상승 또는 GC_OVERRIDE 강세 종목은 SMA 이격 상한을 완화
        # (과거 데이터가 포함된 SMA 한계 보완. 실제 과열은 이후 EMA 이격에서 필터링됨)
        if is_gc_override or ctx.slot == "OPENING":
            max_ma_spread = max(max_ma_spread, 100.0)

        if spread_pct > max_ma_spread:
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 이격 과열 ({spread_pct:.2f}% > 상한 {max_ma_spread:.1f}%)")
            return None
        spread_tag = f"MA{cfg.jdm_ma_short}/{cfg.jdm_ma_long} {ma_s:.0f}/{ma_l:.0f} ({spread_pct:.2f}%)"
        rsi_tag    = f"RSI{rsi:.0f}"

    ctx._rsi = rsi
    return (spread_tag, rsi_tag)

def _jdm_check_execution_quality(
    snap: "StockSnapshot", cfg: "SmartScannerConfig", ctx: "_JdmCtx"
) -> Optional[tuple[str, str, str]]:
    """거래량·체결강도·EMA 이격·RSI·캔들 패턴 체크. (r_vol, r_chej, candle_reason) 또는 None."""
    closes, highs, lows = ctx.closes, ctx.highs, ctx.lows

    # 워밍업 체크
    warmup_reason = check_indicator_warmup(snap, 15)
    ctx.is_warmup = bool(warmup_reason)

    # [FIX 2026-05-11] 거래량 필터 완화 — 거래량 미달 시에도 진입 허용 (경고 로그만 기록)
    # ── 거래량 체크
    r_vol = check_volume_surge(snap, ctx.eff_vol_mult, getattr(cfg, "volume_surge_lookback", 10))
    if r_vol is None:
        # 거래량 부족하지만 진입 허용 (다른 필터가 충분히 제한)
        r_vol = f"거래량부족_허용({snap.volumes_1min[-1] if snap.volumes_1min else 0}주)"
        ScannerLogger.near_miss(snap.code, snap.name, "JDM_VOL", r_vol)

    # ── 체결 가속도 필터
    skip_exec_vel = ctx.slot == "OPENING" and getattr(cfg, "exec_velocity_disabled_opening", False)
    if getattr(cfg, "exec_velocity_enabled", True) and not skip_exec_vel:
        vel_mult = float(getattr(cfg, f"exec_velocity_mult_{ctx.slot.lower()}",
                                 getattr(cfg, "exec_velocity_mult", 1.8)))
        if snap.exec_velocity_ratio > 0 and snap.exec_velocity_ratio < vel_mult:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_EXEC_VEL",
                f"[{ctx.slot}] 체결 가속도 미달 — {snap.exec_velocity_ratio:.2f}배 < {vel_mult:.1f}배")
            return None

    # ── 체결강도 체크
    r_chej = check_chejan_strength(snap, ctx.eff_chejan)
    if r_chej is None:
        ScannerLogger.near_miss(snap.code, snap.name, "JDM_CHEJAN",
            actual=snap.chejan_strength, threshold=ctx.eff_chejan,
            reason=f"[{ctx.slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {ctx.eff_chejan:.0f}%")
        return None
        
    jdm_chejan_max = float(getattr(cfg, "jdm_chejan_max_opening" if ctx.slot == "OPENING" else "jdm_chejan_max",
                                   1200.0 if ctx.slot == "OPENING" else 700.0))
    if snap.chejan_strength >= jdm_chejan_max:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_CHEJAN_MAX",
            f"[{ctx.slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {jdm_chejan_max:.0f}%")
        return None

    # ── EMA 이격 과열 체크
    ema_s_period      = getattr(cfg, "ema_disp_short",         10)
    ema_l_period      = getattr(cfg, "ema_disp_long",          20)
    ema_disp_max      = getattr(cfg, "ema_disp_max_pct",       3.0)
    price_ema_disp_max = getattr(cfg, "price_ema_disp_max_pct", 3.0)
    if ctx.trend_lv >= ctx.candle_skip_lv:
        ema_disp_max       = float(getattr(cfg, "ema_disp_max_pct_trend",       7.0))
        price_ema_disp_max = float(getattr(cfg, "price_ema_disp_max_pct_trend", 6.0))
    if ctx.is_warmup:
        ema_disp_max *= 1.5
        price_ema_disp_max *= 1.5
        
    if len(closes) >= ema_l_period:
        ema_s = IndicatorService.calc_ema(closes, ema_s_period)
        ema_l = IndicatorService.calc_ema(closes, ema_l_period)
        if ema_s is not None and ema_l is not None and ema_l > 0:
            ema_disp_pct = (ema_s - ema_l) / ema_l * 100
            if ema_disp_pct >= ema_disp_max:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_EMA",
                    f"EMA10/EMA20 이격 과열 — {ema_disp_pct:.2f}% ≥ {ema_disp_max:.1f}%")
                return None
            if ema_s > 0:
                price_ema_disp = (snap.current_price - ema_s) / ema_s * 100
                if price_ema_disp >= price_ema_disp_max:
                    ScannerLogger.rejected(snap.code, snap.name, "JDM_PRICE_EMA",
                        f"현재가/EMA{ema_s_period} 이격 과열 — {price_ema_disp:.2f}% ≥ {price_ema_disp_max:.1f}%")
                    return None

    # ── RSI 체크
    rsi = getattr(ctx, "_rsi", None)
    if not ctx.lite_mode and rsi is not None:
        eff_rsi_high = cfg.jdm_rsi_high
        if ctx.trend_lv >= ctx.candle_skip_lv:
            eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_trend", 80.0))
            if len(closes) >= 20 and len(highs) >= 15 and len(lows) >= 15:
                ema20_b = IndicatorService.calc_ema(closes, 20)
                atr14_b = IndicatorService.calc_atr(highs, lows, closes, 14)
                if (ema20_b is not None and atr14_b is not None and atr14_b > 0
                        and snap.current_price > ema20_b + atr14_b * 1.5):
                    eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_breakout", 82.0))
            if ctx.slot == "OPENING" and ctx.trend_lv >= 3:
                eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_opening_trend3", 83.0))
        if ctx.is_warmup:
            eff_rsi_high = 88.0
        if not (ctx.eff_rsi_min <= rsi < eff_rsi_high):
            thresh = ctx.eff_rsi_min if rsi < ctx.eff_rsi_min else eff_rsi_high
            ScannerLogger.near_miss(snap.code, snap.name, "JDM_RSI",
                actual=rsi, threshold=thresh,
                reason=f"[{ctx.slot}] RSI 범위 초과 — 현재 {rsi:.1f}% (허용 {ctx.eff_rsi_min:.0f}~{eff_rsi_high:.0f}%, trend_lv={ctx.trend_lv})")
            return None

    # ── 캔들 패턴
    if not ctx.lite_mode:
        if ctx.trend_lv >= ctx.candle_skip_lv:
            candle_reason = f"TREND_SKIP(lv{ctx.trend_lv})"
        else:
            r_engulf = check_bullish_engulfing(snap)
            r_pinbar = check_bullish_pin_bar(snap)
            if ctx.is_warmup and r_engulf is None and r_pinbar is None:
                if snap.current_price > snap.open_price and snap.current_price >= snap.high_prev:
                    candle_reason = "AGGRESSIVE_BREAKOUT"
                else:
                    ScannerLogger.rejected(snap.code, snap.name, "JDM_CANDLE", "워밍업 양봉 돌파 미충족")
                    return None
            elif r_engulf is None and r_pinbar is None:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_CANDLE",
                    f"캔들 패턴 미충족 (상승장악형·강세핀바 불성립, trend_lv={ctx.trend_lv} < {ctx.candle_skip_lv})")
                return None
            else:
                candle_reason = r_engulf or r_pinbar
    else:
        candle_reason = "LITE(캔들패턴스킵)"

    # ── 체결강도 최종 재확인
    if snap.chejan_strength < ctx.eff_chejan:
        ScannerLogger.near_miss(snap.code, snap.name, "JDM_CHEJAN_FINAL",
            actual=snap.chejan_strength, threshold=ctx.eff_chejan,
            reason=f"[{ctx.slot}] 체결강도 최종 재확인 미충족 — 현재 {snap.chejan_strength:.0f}% < {ctx.eff_chejan:.0f}%")
        return None

    return (r_vol, r_chej, candle_reason)

def _jdm_check_daily_context(
    snap: "StockSnapshot", cfg: "SmartScannerConfig", ctx: "_JdmCtx"
) -> Optional[dict]:
    """피봇 R2 + 일봉 정배열 + 일봉 20MA 체크. daily_ctx dict 또는 None 반환."""
    if not ctx.lite_mode and cfg.pivot_r2_enabled:
        r2 = IndicatorService.calc_pivot_r2(snap.daily_high_prev, snap.daily_low_prev, snap.prev_close)
        if r2 > 0 and snap.current_price < r2:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_PIVOT",
                f"피봇 R2 미돌파 (현재가={snap.current_price:,} < R2={r2:,.0f})")
            return None

    if cfg.daily_alignment_enabled and len(snap.daily_closes) >= 20:
        align = IndicatorService.check_daily_alignment(snap.daily_closes, snap.current_price)
        if not align["is_aligned"]:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_ALIGN",
                f"일봉 정배열 미충족 (5MA > 10MA > 20MA, 데이터={len(snap.daily_closes)}개)")
            return None

    near_high_thr = float(getattr(cfg, "daily_near_high_threshold_pct", 3.0))
    daily_ctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price, near_high_thr)

    if getattr(cfg, "daily_ma20_filter_enabled", True):
        if not daily_ctx["above_ma20"] and daily_ctx["daily_ma20"] > 0:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_DAILY_MA20",
                f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {daily_ctx['daily_ma20']:,.0f}")
            return None

    if getattr(cfg, "daily_ma20_slope_enabled", True):
        if not daily_ctx.get("ma20_slope_up", True):
            ScannerLogger.rejected(snap.code, snap.name, "JDM_MA20_SLOPE",
                f"일봉 20MA 기울기 하락 — 추세추종 진입 차단 (20MA={daily_ctx['daily_ma20']:,.0f})")
            return None

    return daily_ctx

def check_jdm_entry(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
) -> Optional[str]:
    """
    JDM_ENTRY 통합 게이트.
    """
    ctx = _jdm_build_ctx(snap, cfg)
    if ctx is None:
        return None

    ma_result = _jdm_check_trend_and_ma(snap, cfg, ctx)
    if ma_result is None:
        return None
    spread_tag, rsi_tag = ma_result

    exec_result = _jdm_check_execution_quality(snap, cfg, ctx)
    if exec_result is None:
        return None
    r_vol, r_chej, candle_reason = exec_result

    daily_ctx = _jdm_check_daily_context(snap, cfg, ctx)
    if daily_ctx is None:
        return None

    mode_tag  = "JDM_LITE" if ctx.lite_mode else "JDM"
    warm_tag  = " | [WARMUP]" if ctx.is_warmup else ""
    reason = (
        f"[{ctx.slot}][{mode_tag}] {r_vol} | {r_chej} | {spread_tag} | {rsi_tag} "
        f"| {candle_reason}{warm_tag} | 📈신고가근처(TP↑)"
    )
    ScannerLogger.passed(snap.code, snap.name, "JDM_ENTRY", reason)
    return reason

def check_jdm_open_breakout(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
    min_body_ratio: float = 0.7,
) -> Optional[str]:
    """
    장동민 개선형: OR 3조건 + 양봉 몸통 비율 필터.
    """
    if snap.open_price <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_OPEN", "시가/현재가 0")
        return None

    # OR 3조건 검사
    cond0 = snap.current_price > snap.open_price
    cond_a = (snap.current_price > snap.prev_close and
              snap.current_price >= snap.open_price * cfg.prev_close_min_ratio)
    cond_b = (snap.current_price >= snap.high_price and
              snap.change_pct >= cfg.vi_approach_chg_pct)

    condition_met = False
    condition_reason = ""

    if cond0:
        condition_met = True
        condition_reason = "시가돌파"
    elif cond_a:
        condition_met = True
        condition_reason = "V자반등"
    elif cond_b:
        condition_met = True
        condition_reason = "VI직전"

    if not condition_met:
        return None

    # 양봉 몸통 비율 체크
    candle_range = snap.high_price - snap.low_price
    if candle_range > 0:
        body_ratio = (snap.current_price - snap.open_price) / candle_range
        if body_ratio < min_body_ratio:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_OPEN",
                f"{condition_reason} 통과했으나 몸통 비율 부족 {body_ratio:.0%} < {min_body_ratio:.0%}")
            return None

    breakout_pct = (snap.current_price - snap.open_price) / snap.open_price * 100
    body_ratio_str = f" 몸통={((snap.current_price - snap.open_price) / candle_range):.0%}" if candle_range > 0 else ""
    reason = f"{condition_reason} 현재가={snap.current_price:,} > 시가={snap.open_price:,}(+{breakout_pct:.2f}%){body_ratio_str}"
    ScannerLogger.passed(snap.code, snap.name, "JDM_OPEN", reason)
    return reason
