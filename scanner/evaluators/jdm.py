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
    leading_score: float = 0.0  # 선행 지표 복합 점수 — RSI/추세 조건 완화 판단에 사용

def _jdm_build_ctx(snap: "StockSnapshot", cfg: "SmartScannerConfig") -> Optional["_JdmCtx"]:
    """슬롯·유효 파라미터 계산. 조기 차단 조건 해당 시 None 반환."""
    # [FIX 2026-05-11] FID 13 거래대금 부정확 + rank=0 문제
    # → 거래대금 필터 임시 비활성화, 거래량 기반으로 대체
    # ── 거래량 기반 유동성 필터 (거래대금 대체) — 슬롯별 차등화 (2026-05-12)
    now = datetime.now().time()
    slot_temp = _resolve_time_slot(now, cfg)
    if slot_temp == "OPENING":
        min_volume = getattr(cfg, 'min_daily_volume_opening', 50_000)   # OPENING: 50k
    elif slot_temp == "AFTERNOON":
        min_volume = getattr(cfg, 'min_daily_volume_afternoon', 90_000) # AFTERNOON: 거래량 자연 감소
    else:
        min_volume = getattr(cfg, 'min_daily_volume', 100_000)          # 기타: 100k
    if snap.volume > 0 and snap.volume < min_volume:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_LIQUIDITY",
            f"거래량 미달 (volume={snap.volume:,}, 기준={min_volume:,})")
        return None

    # [방향 A 2026-06-01] 최근 상승도 차단 — 체결강도 연동 허용 범위 확대
    # [2026-06-05] 선행점수 >= 0.30이면 면제 — 지수 역행 급등 종목은 진짜 강세
    _c1min = [c for c in snap.closes_1min if c > 0]
    if len(_c1min) >= 6 and _c1min[-2] > 0 and _c1min[-6] > 0:
        recent_1min_chg = (_c1min[-1] - _c1min[-2]) / _c1min[-2] * 100
        recent_5min_chg = (_c1min[-1] - _c1min[-6]) / _c1min[-6] * 100

        recent_1min_max = float(getattr(cfg, "recent_candle_max_1min_pct", 2.0))
        recent_5min_max = float(getattr(cfg, "recent_candle_max_5min_pct", 5.0))

        _chejan_for_surge = float(snap.chejan_strength) if hasattr(snap, 'chejan_strength') else 0
        _surge_chejan_thr = float(getattr(cfg, "surge_chejan_bonus_threshold", 900.0))
        if _chejan_for_surge >= _surge_chejan_thr:
            recent_1min_max = float(getattr(cfg, "recent_candle_max_1min_pct_strong", 3.0))
            recent_5min_max = float(getattr(cfg, "recent_candle_max_5min_pct_strong", 15.0))

        # 선행점수 >= 0.30이면 급등 차단 면제 — 호가/거래량/체결 선행 신호가 강하면 정점이 아닌 상승 지속 판단
        _leading_for_surge = IndicatorService.get_leading_score(snap)
        _leading_surge_exempt = float(getattr(cfg, "surge_exempt_leading_min", 0.30))
        if _leading_for_surge is not None and _leading_for_surge >= _leading_surge_exempt:
            pass  # 선행점수 충분 → RECENT_SURGE 면제, 이후 JDM_LEADING이 최종 판정
        else:
            if recent_1min_chg >= recent_1min_max:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_RECENT_SURGE",
                    f"1분 급등 차단 — {recent_1min_chg:+.2f}% (상한 {recent_1min_max:.1f}%, 체결강도 {_chejan_for_surge:.0f}%, 선행={_leading_for_surge:.2f})")
                return None

            if recent_5min_chg >= recent_5min_max:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_RECENT_SURGE",
                    f"5분 급등 차단 — {recent_5min_chg:+.2f}% (상한 {recent_5min_max:.1f}%, 체결강도 {_chejan_for_surge:.0f}%, 선행={_leading_for_surge:.2f})")
                return None

    # ── 체결강도 가속도 (모멘텀 방향) 필터
    # 체결강도가 "지금 높다"는 것만으로는 부족 — 상승 중이어야 진입 타이밍
    # 최근 3틱 평균 < 이전 3틱 평균의 80% → 모멘텀 소멸, 진입 포기
    if getattr(cfg, "chejan_accel_check_enabled", True):
        _hist = list(getattr(snap, "chejan_history", None) or [])
        if len(_hist) >= 8:
            _recent_avg = sum(_hist[-3:]) / 3
            _prev_avg   = sum(_hist[-6:-3]) / 3
            _drop_thr   = float(getattr(cfg, "chejan_accel_min_ratio", 0.80))
            if _prev_avg > 0 and (_recent_avg / _prev_avg) < _drop_thr:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_CHEJAN_DECEL",
                    f"체결강도 하락 중 — 최근:{_recent_avg:.0f}% / 이전:{_prev_avg:.0f}% "
                    f"= {_recent_avg/_prev_avg:.2f}x (기준 ≥ {_drop_thr:.2f}x)")
                return None

    # ── 선행 지표 복합 점수 필터
    # 체결강도 반등 + 거래량 축적 + 호가 압력 합산
    # None = 데이터 부족(장 초반) → 생략, 0.0~1.0 = 유효
    _leading = IndicatorService.get_leading_score(snap)
    _ctx_leading = 0.0  # ctx에 전달할 선행 점수 (후속 함수에서 RSI 완화 판단에 사용)
    if _leading is not None:
        _leading_thr = float(getattr(cfg, "leading_score_min", 0.25))
        if _leading < _leading_thr:
            _bs = IndicatorService.calc_bid1_slope_score(list(getattr(snap, 'bid1_history', [])))
            _vb = IndicatorService.calc_vol_burst_score(list(getattr(snap, 'volumes_1min', [])))
            _cr = IndicatorService.calc_chejan_reversal_score(list(getattr(snap, 'chejan_history', [])))
            _hp = IndicatorService.calc_hoga_pressure_score(int(getattr(snap, 'total_ask_qty', 0)), int(getattr(snap, 'total_bid_qty', 0)))
            _aw = IndicatorService.calc_ask1_wall_collapse_score(list(getattr(snap, 'ask1_qty_history', [])))
            _tv = IndicatorService.calc_tick_vol_accel_score(list(getattr(snap, 'tick_vol_history', [])))
            ScannerLogger.rejected(snap.code, snap.name, "JDM_LEADING",
                f"선행점수 미달 — {_leading:.2f} < {_leading_thr:.2f} "
                f"(매수1호가기울기:{_bs:.2f} 거래량폭발:{_vb:.2f} 체결반등:{_cr:.2f} 호가:{_hp:.2f} 매도벽급감:{_aw:.2f} 틱속도:{_tv:.2f})")
            return None
        _ctx_leading = _leading  # 임계값 통과: 점수를 ctx에 전달

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


    return _JdmCtx(
        now=now, slot=slot,
        eff_chejan=eff_chejan, eff_vol_mult=eff_vol_mult,
        eff_rsi_min=eff_rsi_min, eff_ma_spread=eff_ma_spread,
        scoring_bonus=scoring_bonus, trend_lv=trend_lv,
        candle_skip_lv=candle_skip_lv, lite_mode=lite_mode,
        closes=closes, highs=highs, lows=lows,
        leading_score=_ctx_leading,
    )

def _jdm_check_trend_and_ma(
    snap: "StockSnapshot", cfg: "SmartScannerConfig", ctx: "_JdmCtx"
) -> Optional[tuple[str, str]]:
    """요셉 추세 필터 + MA 골든크로스/이격도 체크. (spread_tag, rsi_tag) 또는 None 반환."""
    closes, highs, lows = ctx.closes, ctx.highs, ctx.lows

    # ── 요셉 추세 필터 (축소: 일봉 역배열만 차단, 추세 요구치는 완화)
    # [FIX 2026-06-04 Phase3] trend_lv 요구치 축소. 선행 신호(leading_score)가 우선 → 추세는 보조.
    # 하지만 일봉이 역배열(매도압력)이면 차단 — 추세 붕괴 방지.
    if getattr(cfg, "daily_alignment_enabled", True) and len(snap.daily_closes) >= 20:
        align = IndicatorService.check_daily_alignment(snap.daily_closes, snap.current_price)
        if not align["is_aligned"]:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_DAILY_ALIGN",
                f"일봉 역배열 — MA5:{align['ma5']:.0f} < MA10:{align['ma10']:.0f} < MA20:{align['ma20']:.0f}")
            return None
        

    # ── MTF(멀티타임프레임) 추세 일치 필터
    # 1분봉 상승 신호인데 5분봉이 하락 중인 경우 차단 — 고점 진입 방지
    if getattr(cfg, "mtf_enabled", True) and getattr(cfg, "mtf_block_on_misalign", True):
        _skip_mtf = False
        # [FIX 2026-06-04] OPENING 슬롯 MTF 스킵 제거 — 5분봉 추세 방향 필수 확인
        # 기존: OPENING은 5분봉 데이터 부족으로 스킵 → 추세 역행 진입 허용 → 손실
        # 변경: 5분봉 3개 미만이면 자연 스킵(아래 _min_bars 체크), 있으면 반드시 확인
        # 1분봉 추세가 매우 강하면(Lv3+) 스킵 — 폭발적 강세는 MTF 무시
        _skip_lv = int(getattr(cfg, "mtf_skip_tf1_trend_lv", 3))
        if int(getattr(snap, "mtf_tf1_trend", 0)) >= _skip_lv:
            _skip_mtf = True
        # 5분봉 수 부족하면 스킵
        _min_bars = int(getattr(cfg, "mtf_min_5min_bars", 3))
        if int(getattr(snap, "mtf_tf5_bars", 0)) < _min_bars:
            _skip_mtf = True

        if not _skip_mtf and not getattr(snap, "mtf_aligned", True):
            _tf1_slope = getattr(snap, "mtf_tf1_slope", 0.0)
            _tf5_slope = getattr(snap, "mtf_tf5_slope", 0.0)
            _tf5_bars  = getattr(snap, "mtf_tf5_bars", 0)
            # 5분봉이 강한 상승 중이면 1분봉 일시 눌림 허용 (눌림목 패턴)
            _mtf_tf5_strong = float(getattr(cfg, "mtf_tf5_strong_slope", 5.0))
            if _tf5_slope >= _mtf_tf5_strong:
                ScannerLogger.passed(snap.code, snap.name, "JDM_MTF_PULLBACK",
                    f"5분EMA강세({_tf5_slope:+.1f}) 1분눌림({_tf1_slope:+.1f}) 허용")
            else:
                ScannerLogger.rejected(
                    snap.code, snap.name, "JDM_MTF",
                    f"MTF 추세 불일치 [{ctx.slot}] — "
                    f"1분EMA기울기={_tf1_slope:+.1f} "
                    f"5분EMA기울기={_tf5_slope:+.1f} "
                    f"(5분봉{_tf5_bars}개)"
                )
                return None

    # ── 60분봉 추세 방향 필터
    # [2026-06-02] 60분봉이 하락 방향이면 1분봉 반등이어도 차단 — 큰 그림 역행 방지
    if getattr(cfg, "h1_trend_enabled", True) and getattr(cfg, "h1_trend_filter", True):
        _h1_closes = getattr(snap, "h1_closes", [])
        if len(_h1_closes) >= int(getattr(cfg, "h1_min_bars", 5)):
            _h1_slope = float(getattr(snap, "h1_slope", 0.0))
            _h1_trend = int(getattr(snap, "h1_trend", 0))
            _h1_rsi   = getattr(snap, "h1_rsi", None)

            # 60분봉 EMA 기울기가 하락이고 trend_lv도 0이면 차단
            # (단, 1분봉 trend_lv가 3 이상인 강한 폭등은 스킵 — 갭 상승 등)
            # (단, H1 RSI ≤ 20 극단적 과매도는 반등 가능성으로 스킵)
            # (단, 5분봉 EMA 강세 상승 중이면 60분봉 하락 방향 우회 — 단기 추세 역전 진행 중)
            _skip_h1 = ctx.trend_lv >= int(getattr(cfg, "h1_skip_tf1_trend_lv", 3))
            _h1_oversold = _h1_rsi is not None and _h1_rsi <= 20.0
            _h1_tf5_strong_slope = float(getattr(cfg, "mtf_tf5_strong_slope", 5.0))
            _h1_tf5_bypass = float(getattr(snap, "mtf_tf5_slope", 0.0)) >= _h1_tf5_strong_slope
            if _h1_tf5_bypass and _h1_slope < 0 and _h1_trend == 0:
                _tf5s = float(getattr(snap, "mtf_tf5_slope", 0.0))
                ScannerLogger.passed(snap.code, snap.name, "JDM_H1_TF5_BYPASS",
                    f"5분EMA강세({_tf5s:+.1f}) 60분봉하락 우회 — H1={_h1_slope:+.1f}")
            if not _skip_h1 and not _h1_oversold and not _h1_tf5_bypass and _h1_slope < 0 and _h1_trend == 0:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_H1_TREND",
                    f"60분봉 하락 방향 — EMA기울기={_h1_slope:+.1f} trend_lv={_h1_trend} "
                    f"RSI={_h1_rsi:.0f}" if _h1_rsi else
                    f"60분봉 하락 방향 — EMA기울기={_h1_slope:+.1f} trend_lv={_h1_trend}")
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

        # [FIX 2026-05-27] MA 이격 상한 100% 무력화 제거 — 5/27 정점 진입 원인.
        # 이전엔 OPENING/GC_OVERRIDE 시 100%까지 허용 → 나무기술 +96.57% 이격 진입 등 사고.
        # 완화는 약하게만(1.5배), 100% 무력화는 안 함.
        if is_gc_override or ctx.slot == "OPENING":
            max_ma_spread *= 1.5  # 1.5배까지만 완화 (100% 무력화 X)

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

    # [방향 B 2026-06-01] WARMUP 모드 진입 조건 강화
    # 1분봉 부족 → RSI·캔들 패턴 없이 진입 → 검증 미흡 → 즉시 손실
    # 체결강도·거래대금 기준을 높여 강한 에너지가 확인된 경우만 허용
    if ctx.is_warmup:
        _warmup_chejan_min = float(getattr(cfg, "jdm_warmup_chejan_min", 920.0))  # 일반 900 → 920
        if snap.chejan_strength < _warmup_chejan_min:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_VOL",
                f"WARMUP 체결강도 부족 — {snap.chejan_strength:.0f}% < {_warmup_chejan_min:.0f}% (WARMUP 강화 기준)")
            return None

    # ── 거래량 체크
    # [FIX 2026-06-01] OPENING 슬롯에서 거래량 부족 시 차단
    # PS일렉트로닉스 2946주 거래량으로 진입 → 유동성 부족 → 매도 미체결 반복
    # OPENING + 거래량 부족 = 최악의 조합 (장 초반 변동성 + 낮은 유동성)
    r_vol = check_volume_surge(snap, ctx.eff_vol_mult, getattr(cfg, "volume_surge_lookback", 10))
    if r_vol is None:
        if ctx.slot == "OPENING":
            ScannerLogger.rejected(snap.code, snap.name, "JDM_VOL",
                f"OPENING 거래량 미달 차단 — {snap.volumes_1min[-1] if snap.volumes_1min else 0}주 (유동성 부족)")
            return None
        # OPENING 외 슬롯: 진입 허용 (다른 필터가 충분히 제한)
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

    # ── 체결강도 체크 (하한 + 상한 동시 적용)
    jdm_chejan_max = float(getattr(cfg, "jdm_chejan_max_opening" if ctx.slot == "OPENING" else "jdm_chejan_max",
                                   1500.0))
    r_chej = check_chejan_strength(snap, ctx.eff_chejan, max_strength=jdm_chejan_max)
    if r_chej is None:
        if snap.chejan_strength < ctx.eff_chejan:
            ScannerLogger.near_miss(snap.code, snap.name, "JDM_CHEJAN",
                actual=snap.chejan_strength, threshold=ctx.eff_chejan,
                reason=f"[{ctx.slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {ctx.eff_chejan:.0f}%")
        else:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_CHEJAN_MAX",
                f"[{ctx.slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {jdm_chejan_max:.0f}%")
        return None

    # ── 현재가/EMA10 이격 과열 체크 (단일화)
    # [2026-06-02] EMA10/EMA20 이격 체크 제거 — MA7/MA15 이격 체크와 중복
    # 현재가/EMA10 이격만 유지 — 고점 진입 방지 핵심 역할
    ema_s_period       = getattr(cfg, "ema_disp_short",          10)
    price_ema_disp_max = getattr(cfg, "price_ema_disp_max_pct",  3.0)
    if ctx.trend_lv >= ctx.candle_skip_lv:
        price_ema_disp_max = float(getattr(cfg, "price_ema_disp_max_pct_trend", 6.0))
    if ctx.is_warmup:
        price_ema_disp_max *= 1.5
    if ctx.slot == "OPENING":
        price_ema_disp_max *= 1.3

    if len(closes) >= ema_s_period:
        ema_s = IndicatorService.calc_ema(closes, ema_s_period)
        if ema_s is not None and ema_s > 0:
            price_ema_disp = (snap.current_price - ema_s) / ema_s * 100
            if price_ema_disp >= price_ema_disp_max:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_PRICE_EMA",
                    f"현재가/EMA{ema_s_period} 이격 과열 — {price_ema_disp:.2f}% ≥ {price_ema_disp_max:.1f}%")
                return None

    # ── 선행 패턴 사전 계산 (RSI 완화 판단에 사용)
    from scanner.evaluators.common import (
        check_flag_pattern, check_cup_and_handle,
        check_three_soldiers, check_volume_dry_up,
    )
    r_flag     = check_flag_pattern(snap)
    r_cup      = check_cup_and_handle(snap)
    r_soldiers = check_three_soldiers(snap)
    r_dry_up   = check_volume_dry_up(snap)
    r_precursor = r_flag or r_cup or r_soldiers or r_dry_up

    # ── RSI 체크 (상한만 사용 — 하한 제거, leading_score가 없으면 신호가 안 나므로)
    # [FIX 2026-06-04 Phase3] RSI 하한 제거, 상한만 체크. leading_score >= 0.25가 필수이므로 하한은 불필요.
    rsi = getattr(ctx, "_rsi", None)
    if not ctx.lite_mode and rsi is not None:
        eff_rsi_high = cfg.jdm_rsi_high  # 기본 60 (변경: 70→60, 상한만 사용)

        # 선행 강세 시 RSI 상한 완화
        _ls_strong_thr = float(getattr(cfg, "leading_score_strong", 0.50))
        _ls_weak_thr   = float(getattr(cfg, "leading_score_min", 0.05))
        if ctx.leading_score >= _ls_strong_thr:
            _rsi_high_boost = float(getattr(cfg, "jdm_rsi_high_strong_leading", 75.0))
            eff_rsi_high = max(eff_rsi_high, _rsi_high_boost)
            ScannerLogger.passed(snap.code, snap.name, "JDM_LEADING_BOOST",
                f"선행 강세 → RSI 상한 완화 (상한={eff_rsi_high:.0f}) "
                f"leading={ctx.leading_score:.2f} RSI={rsi:.1f}")
        elif ctx.leading_score >= _ls_weak_thr:
            # 약한 선행 신호도 RSI 상한 소폭 완화 (70→75)
            _rsi_high_weak = float(getattr(cfg, "jdm_rsi_high_weak_leading", 75.0))
            eff_rsi_high = max(eff_rsi_high, _rsi_high_weak)
            ScannerLogger.passed(snap.code, snap.name, "JDM_LEADING_WEAK_BOOST",
                f"약한선행 → RSI 상한 완화 (상한={eff_rsi_high:.0f}) "
                f"leading={ctx.leading_score:.2f} RSI={rsi:.1f}")

        # 과매수만 차단 (RSI >= 상한)
        if rsi >= eff_rsi_high:
            ScannerLogger.near_miss(snap.code, snap.name, "JDM_RSI",
                actual=rsi, threshold=eff_rsi_high,
                reason=f"[{ctx.slot}] RSI 과매수 차단 — {rsi:.1f}% >= {eff_rsi_high:.0f}% "
                       f"(leading={ctx.leading_score:.2f})")
            return None

    # ── 캔들 패턴 (정보 기록용 — 진입 차단 없음)
    if not ctx.lite_mode:
        r_engulf = check_bullish_engulfing(snap)
        r_pinbar = check_bullish_pin_bar(snap)
        if ctx.trend_lv >= ctx.candle_skip_lv:
            candle_reason = f"TREND_SKIP(lv{ctx.trend_lv})"
            if r_precursor:
                candle_reason = f"{r_precursor}+TREND(lv{ctx.trend_lv})"
        elif r_precursor:
            candle_reason = r_precursor
        elif r_engulf or r_pinbar:
            candle_reason = r_engulf or r_pinbar
        else:
            candle_reason = "NO_PATTERN"
    else:
        candle_reason = r_precursor or "LITE(캔들패턴스킵)"

    return (r_vol, r_chej, candle_reason)

def _jdm_check_daily_context(
    snap: "StockSnapshot", cfg: "SmartScannerConfig", ctx: "_JdmCtx"
) -> Optional[dict]:
    """피봇 R2 + 일봉 정배열 + 일봉 20MA 체크. daily_ctx dict 또는 None 반환."""
    # OPENING 슬롯에서는 일봉 필터 모두 스킵 (2026-05-12: 극단 완화, 학습 데이터 수집)
    if ctx.slot == "OPENING":
        near_high_thr = float(getattr(cfg, "daily_near_high_threshold_pct", 3.0))
        daily_ctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price, near_high_thr)
        return daily_ctx

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

    # 일봉 MA20 기울기 — OPENING 슬롯에서는 스킵 (오늘 일봉 형성 중, 의미 없음)
    if getattr(cfg, "daily_ma20_slope_enabled", True) and ctx.slot != "OPENING":
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

    # [2026-06-02] D전략: 호가 압력 필터 — 진입 마지막 관문
    # 매수2~3호가 물량 > 매도2~3호가 물량 × min_ratio 이어야 지지선 신뢰
    _hoga_ready = getattr(snap, "hoga_ready", False)
    if getattr(cfg, "hoga_pressure_enabled", True) and _hoga_ready:
        pressure = float(getattr(snap, "hoga_pressure", 1.0))
        _hoga_min_key = "hoga_pressure_min_opening" if ctx.slot == "OPENING" else "hoga_pressure_min"
        min_pressure = float(getattr(cfg, _hoga_min_key, getattr(cfg, "hoga_pressure_min", 1.3)))
        if pressure < min_pressure:
            _bid_vol = sum(list(getattr(snap, "bid_qtys", [0]*5))[1:3])
            _ask_vol = sum(list(getattr(snap, "ask_qtys", [0]*5))[1:3])
            ScannerLogger.rejected(snap.code, snap.name, "JDM_HOGA",
                f"[{ctx.slot}] 호가 압력 부족 — 압력비={pressure:.2f} < {min_pressure:.1f} "
                f"(매수2~3호가 {_bid_vol:,}주 vs 매도 {_ask_vol:,}주)")
            return None


    pressure_tag = ""
    if _hoga_ready:
        _slope_val = float(getattr(snap, "bid1_slope", 0.0))
        pressure_tag = (f" | 호가압력={float(getattr(snap, 'hoga_pressure', 1.0)):.2f}"
                        f" | 호가기울기={_slope_val:+.3f}%")

    mode_tag  = "JDM_LITE" if ctx.lite_mode else "JDM"
    warm_tag  = " | [WARMUP]" if ctx.is_warmup else ""
    reason = (
        f"[{ctx.slot}][{mode_tag}] {r_vol} | {r_chej} | {spread_tag} | {rsi_tag} "
        f"| {candle_reason}{warm_tag}{pressure_tag} | 📈신고가근처(TP↑)"
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
