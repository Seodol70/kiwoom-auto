"""
common.py — 공용 신호 평가 유틸리티 및 필터
"""
from typing import Optional, TYPE_CHECKING
from datetime import datetime, time as dtime
from scanner.scanner_logger import ScannerLogger

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

def _resolve_time_slot(now: dtime, cfg: "SmartScannerConfig") -> str:
    """
    현재 시각을 기준으로 매매 시간 슬롯 문자열을 반환한다.
    """
    pre_end = getattr(cfg, "pre_market_end", dtime(9, 0, 0))
    if now < pre_end:
        return "PRE"
    if now < cfg.ma_alignment_time:
        return "OPENING"
    if now < cfg.slot_morning_end:
        return "MORNING"
    if now < cfg.slot_midday_end:
        return "MIDDAY"
    return "AFTERNOON"

def _get_slot_value(slot: str, cfg: "SmartScannerConfig", param_base: str, fallback: float) -> float:
    """
    슬롯과 파라미터 기본명으로 구간별 값을 반환한다.
    """
    return float(getattr(cfg, f"{param_base}_{slot.lower()}", fallback))

def check_volume_surge(
    snap: "StockSnapshot", 
    surge_mult: float, 
    lookback: int = 10
) -> Optional[str]:
    """
    최근 1분 거래량이 직전 N분 평균 대비 급증했는지 확인.
    """
    vols = list(snap.volumes_1min) if snap.volumes_1min else []
    if len(vols) < 2:
        return None

    actual_lookback = min(len(vols) - 1, lookback)
    recent_vols = vols[-(actual_lookback + 1):-1]
    avg_vol = sum(recent_vols) / len(recent_vols)

    if avg_vol > 0 and vols[-1] >= avg_vol * surge_mult:
        return f"거래량급증{vols[-1]:,}주({vols[-1]/avg_vol:.1f}배)"
    
    return None

def check_trade_amount_surge(
    snap: "StockSnapshot",
    accel_mult: float = 2.0,
    lookback: int = 5,
) -> Optional[str]:
    """
    거래대금 급증 확인: 현재 봉 거래대금 >= 최근 N봉 평균 × accel_mult
    거래대금 = 종가 × 거래량 (근사)

    거래량 급증과 달리 '금액 기준'이므로 소형주 속임수 필터 효과가 큼.
    """
    closes  = list(snap.closes_1min  or [])
    volumes = list(snap.volumes_1min or [])

    need = lookback + 1
    if len(closes) < need or len(volumes) < need:
        return None

    # 현재 봉 거래대금
    curr_amount = closes[-1] * volumes[-1]

    # 직전 lookback개 봉 평균 거래대금
    past_amounts = [
        closes[-(i + 1)] * volumes[-(i + 1)]
        for i in range(1, lookback + 1)
    ]
    avg_amount = sum(past_amounts) / len(past_amounts)

    if avg_amount <= 0:
        return None

    ratio = curr_amount / avg_amount
    if ratio >= accel_mult:
        return f"거래대금급증({ratio:.1f}배)"

    return None

def check_chejan_strength(
    snap: "StockSnapshot",
    min_strength: float,
    max_strength: float = 1000.0
) -> Optional[str]:
    """
    체결강도 범위 체크.
    """
    val = snap.chejan_strength
    if val < min_strength:
        return None
    if val >= max_strength:
        return None
    return f"STRENGTH({val:.0f}%)"

def check_vwap_filter(snap: "StockSnapshot") -> Optional[str]:
    """
    현재가가 VWAP(거래량 가중 평균 가격) 상단에 있는지 확인.
    """
    if not snap.vwap or snap.vwap <= 0:
        return "VWAP_N/A"
    
    if snap.current_price >= snap.vwap:
        return f"VWAP_OK({snap.current_price/snap.vwap*100-100:+.1f}%)"
    
    ScannerLogger.rejected(snap.code, snap.name, "VWAP",
        f"VWAP 하단 — 현재가 {snap.current_price:,} < VWAP {snap.vwap:,.0f}")
    return None

def check_indicator_warmup(snap: "StockSnapshot", min_candles: int = 15) -> Optional[str]:
    """
    지표 계산을 위한 최소 캔들 데이터 확보 여부 확인.
    """
    count = len(snap.closes_1min)
    if count < min_candles:
        return f"WARMUP_LACK({count})"
    return None

def check_bullish_engulfing(snap: "StockSnapshot") -> Optional[str]:
    """
    상승장악형 패턴 확인 (직전 음봉을 현재 양봉이 몸통으로 감쌈).
    """
    closes = snap.closes_1min
    opens  = snap.opens_1min
    if len(closes) < 2 or len(opens) < 2:
        return None
    
    p_open, p_close = opens[-2], closes[-2]
    c_open, c_close = opens[-1], closes[-1]
    
    # 1. 직전 캔들이 음봉
    if p_close >= p_open:
        return None
    # 2. 현재 캔들이 양봉
    if c_close <= c_open:
        return None
    # 3. 현재 몸통이 직전 몸통을 완전히 감쌈
    if c_close >= p_open and c_open <= p_close:
        return "BULLISH_ENGULFING"
    
    return None

def check_bullish_pin_bar(snap: "StockSnapshot", min_tail_ratio: float = 0.55) -> Optional[str]:
    """
    강세 핀바(망치형) 확인 (아랫꼬리가 몸통+윗꼬리보다 훨씬 김).
    """
    c = snap.current_price
    o = snap.opens_1min[-1] if snap.opens_1min else c
    h = snap.highs_1min[-1]  if snap.highs_1min  else c
    l = snap.lows_1min[-1]   if snap.lows_1min   else c
    
    rng = h - l
    if rng <= 0:
        return None
        
    body_top = max(o, c)
    body_bottom = min(o, c)
    lower_tail = body_bottom - l
    
    # 아랫꼬리 비중 확인
    if lower_tail / rng >= min_tail_ratio:
        return "BULLISH_PINBAR"
    
    return None

def check_disparity_from_ma(
    snap: "StockSnapshot",
    ma_period: int   = 20,
    max_pct: float   = 5.0,
) -> Optional[str]:
    """1분봉 MA(ma_period) 대비 이격도 max_pct% 이내 확인 (과열 차단)."""
    closes = snap.closes_1min
    if len(closes) < ma_period:
        return None
    ma = IndicatorService.calc_ma(closes, ma_period)
    if ma is None or ma <= 0:
        return None
    disp = (snap.current_price - ma) / ma * 100
    if disp > max_pct:
        ScannerLogger.rejected(snap.code, snap.name, "DISPARITY",
                               f"MA{ma_period} 이격도 {disp:.1f}% > {max_pct:.1f}%")
        return None
    return f"MA{ma_period}이격{disp:.1f}%"

def check_ema20_filter(snap: "StockSnapshot", period: int = 20) -> Optional[str]:
    """
    EMA20 추세 필터 — 현재가가 20분 EMA 위에 있어야 진입 허용.
    """
    closes = snap.closes_1min
    if len(closes) < period:
        ScannerLogger.rejected(snap.code, snap.name, "EMA20", f"데이터 부족 ({len(closes)}/{period})")
        return None
    ema20 = IndicatorService.calc_ema(closes, period)
    if ema20 is None:
        return None
    if snap.current_price <= ema20:
        ScannerLogger.rejected(snap.code, snap.name, "EMA20", f"현재가 {snap.current_price:,} ≤ EMA20 {ema20:,.0f} — 하락 추세")
        return None
    return f"EMA20상단(현재가={snap.current_price:,}/EMA20={ema20:,.0f})"


# ─────────────────────────────────────────────────────────────────────────────
# [선행 캔들 패턴 2026-06-01] 오르기 전에 발견하는 4가지 패턴
#
# 기존 패턴(Engulfing, PinBar)은 이미 오른 뒤 확인하는 '사후 검증'이었다면,
# 아래 4가지는 상승 직전 누적·조정 단계에서 진입 타점을 포착하는 '선행 패턴'이다.
# ─────────────────────────────────────────────────────────────────────────────

def check_flag_pattern(
    snap: "StockSnapshot",
    pole_bars: int = 5,
    flag_bars: int = 5,
    pole_min_pct: float = 3.0,
    correction_max_pct: float = 3.0,
    vol_shrink_ratio: float = 0.7,
) -> Optional[str]:
    """
    깃발형(Flag Pattern) — 급등 후 저거래량 조정 → 재상승 직전 타점.

    구조:
        [깃대] 최근 pole_bars 봉 동안 pole_min_pct% 이상 급등
        [깃발] 이후 flag_bars 봉 동안 0 ~ -correction_max_pct% 조정
               + 거래량 vol_shrink_ratio 이하로 감소
        [현재] 마지막 봉이 조정 구간 고점 돌파 시 신호

    예: 5분간 +4% 급등 → 3분간 -1.5% 조정(거래량 50% 감소) → 현재 돌파 → 진입
    """
    closes  = list(snap.closes_1min  or [])
    volumes = list(snap.volumes_1min or [])
    need = pole_bars + flag_bars + 1
    if len(closes) < need or len(volumes) < need:
        return None

    # ① 깃대: pole_bars 전~flag_bars 전 구간 상승률
    pole_start = closes[-(need)]
    pole_end   = closes[-(flag_bars + 1)]
    if pole_start <= 0:
        return None
    pole_rise = (pole_end - pole_start) / pole_start * 100
    if pole_rise < pole_min_pct:
        return None

    # ② 깃발: 이후 flag_bars 봉 조정 (소폭 하락 또는 횡보)
    flag_high = max(closes[-(flag_bars + 1):-1])
    flag_low  = min(closes[-(flag_bars + 1):-1])
    correction = (closes[-2] - pole_end) / pole_end * 100
    if not (-correction_max_pct <= correction <= 0.5):
        return None

    # ③ 거래량 수축: 깃발 구간 평균 < 깃대 구간 평균 × vol_shrink_ratio
    vol_pole = sum(volumes[-(need):-(flag_bars + 1)]) / pole_bars
    vol_flag = sum(volumes[-(flag_bars + 1):-1]) / flag_bars
    if vol_pole <= 0 or vol_flag > vol_pole * vol_shrink_ratio:
        return None

    # ④ 현재 봉이 깃발 고점 돌파
    if snap.current_price <= flag_high:
        return None

    return (f"FLAG({pole_rise:+.1f}%↑→조정{correction:.1f}%"
            f"→돌파{snap.current_price:,})")


def check_cup_and_handle(
    snap: "StockSnapshot",
    cup_bars: int = 15,
    handle_bars: int = 5,
    cup_depth_min: float = 2.0,
    cup_depth_max: float = 15.0,
    handle_retrace_max: float = 0.5,
) -> Optional[str]:
    """
    컵앤핸들(Cup & Handle) — U자 조정 후 소폭 핸들 → 돌파 직전 타점.

    구조:
        [컵]    cup_bars 봉: 고점 → 하락 → 회복 (U자형)
        [핸들]  이후 handle_bars 봉: 컵 우측 고점 대비 소폭 조정
        [현재]  핸들 저점 이상 유지하며 컵 우측 고점 근접 시 신호

    컵 깊이: cup_depth_min ~ cup_depth_max% 사이
    핸들 조정: 컵 깊이의 handle_retrace_max(50%) 이내
    """
    closes = list(snap.closes_1min or [])
    need = cup_bars + handle_bars + 1
    if len(closes) < need:
        return None

    cup_region  = closes[-(need):-(handle_bars)]
    hdl_region  = closes[-(handle_bars):-1]
    curr        = snap.current_price

    if not cup_region or not hdl_region:
        return None

    cup_left_high  = cup_region[0]
    cup_right_high = cup_region[-1]
    cup_bottom     = min(cup_region)

    if cup_left_high <= 0 or cup_bottom <= 0:
        return None

    # ① 컵 왼쪽·오른쪽 고점이 비슷해야 함 (U자 대칭)
    symmetry = abs(cup_left_high - cup_right_high) / cup_left_high * 100
    if symmetry > 5.0:
        return None

    # ② 컵 깊이 확인
    cup_depth = (cup_left_high - cup_bottom) / cup_left_high * 100
    if not (cup_depth_min <= cup_depth <= cup_depth_max):
        return None

    # ③ 핸들: 컵 우측 고점 대비 소폭 조정
    hdl_low = min(hdl_region)
    handle_drop = (cup_right_high - hdl_low) / cup_right_high * 100
    if handle_drop > cup_depth * handle_retrace_max:
        return None

    # ④ 현재가가 핸들 저점 이상 + 컵 우측 고점의 98% 이상 (돌파 근접)
    if curr < hdl_low:
        return None
    if curr < cup_right_high * 0.98:
        return None

    return (f"CUP_HANDLE(컵깊이{cup_depth:.1f}%"
            f"→핸들{handle_drop:.1f}%조정"
            f"→현재{curr:,})")


def check_three_soldiers(
    snap: "StockSnapshot",
    lookback: int = 5,
    soldier_min_body_ratio: float = 0.5,
    pullback_bars: int = 2,
    pullback_max_pct: float = 2.0,
) -> Optional[str]:
    """
    3봉 연속 상승 후 눌림(Three White Soldiers + Pullback) — 재상승 직전 타점.

    구조:
        [3병사]  lookback 봉 중 3개 이상의 실체 있는 양봉 연속
        [눌림]   이후 pullback_bars 봉 동안 pullback_max_pct% 이내 조정
        [현재]   조정 후 재상승 시작 신호

    각 양봉의 몸통 비중 ≥ soldier_min_body_ratio (실체 있는 진짜 양봉)
    """
    closes = list(snap.closes_1min or [])
    opens  = list(snap.opens_1min  or [])
    highs  = list(snap.highs_1min  or [])

    need = lookback + pullback_bars + 1
    if len(closes) < need or len(opens) < need:
        return None

    soldier_region = list(zip(
        opens[-(need):-(pullback_bars + 1)],
        closes[-(need):-(pullback_bars + 1)],
        highs[-(need):-(pullback_bars + 1)],
    ))
    pullback_region = closes[-(pullback_bars + 1):-1]

    # ① 3봉 이상 연속 양봉 확인 (실체 비중 ≥ 50%)
    soldier_count = 0
    max_streak = 0
    streak = 0
    prev_close = None
    for o, c, h in soldier_region:
        rng = h - min(o, c)
        body = abs(c - o)
        is_solid_bull = (c > o and rng > 0 and body / rng >= soldier_min_body_ratio)
        is_higher = (prev_close is None or c > prev_close)
        if is_solid_bull and is_higher:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
        prev_close = c

    if max_streak < 3:
        return None

    # ② 3병사 정점
    soldier_peak = max(closes[-(need):-(pullback_bars + 1)])

    # ③ 눌림: 정점 대비 소폭 하락
    pb_low = min(pullback_region) if pullback_region else soldier_peak
    pullback_pct = (soldier_peak - pb_low) / soldier_peak * 100
    if pullback_pct > pullback_max_pct or pullback_pct < 0.1:
        return None

    # ④ 현재가가 눌림 저점 위로 회복
    if snap.current_price <= pb_low:
        return None

    return (f"THREE_SOLDIERS(3연상승→눌림{pullback_pct:.1f}%"
            f"→회복{snap.current_price:,})")


def check_volume_dry_up(
    snap: "StockSnapshot",
    base_bars: int = 10,
    dry_bars: int = 5,
    dry_ratio: float = 0.4,
    price_range_max_pct: float = 2.0,
    breakout_min_vol_ratio: float = 2.0,
) -> Optional[str]:
    """
    거래량 바닥 다지기(Volume Dry-Up) — 거래량 급감 + 가격 횡보 → 방향성 폭발 직전.

    구조:
        [기준]  base_bars 봉의 평균 거래량 계산
        [수축]  이후 dry_bars 봉: 거래량이 기준의 dry_ratio(40%) 이하로 줄어들면서
                가격은 price_range_max_pct(2%) 이내 횡보
        [폭발]  현재 봉: 거래량이 수축 구간 평균의 breakout_min_vol_ratio(2배) 이상
                + 현재가 상승

    거래량이 말라붙다가 터지는 순간 = 세력 매집 완료 + 상승 시작 신호
    """
    closes  = list(snap.closes_1min  or [])
    volumes = list(snap.volumes_1min or [])

    need = base_bars + dry_bars + 1
    if len(closes) < need or len(volumes) < need:
        return None

    base_vols = volumes[-(need):-(dry_bars + 1)]
    dry_vols  = volumes[-(dry_bars + 1):-1]
    dry_closes = closes[-(dry_bars + 1):-1]
    curr      = snap.current_price
    curr_vol  = volumes[-1]

    if not base_vols or not dry_vols:
        return None

    avg_base = sum(base_vols) / len(base_vols)
    avg_dry  = sum(dry_vols)  / len(dry_vols)

    if avg_base <= 0:
        return None

    # ① 거래량 수축: dry 구간 평균이 base 구간의 dry_ratio 이하
    if avg_dry > avg_base * dry_ratio:
        return None

    # ② 가격 횡보: dry 구간 가격 변동이 price_range_max_pct 이내
    if not dry_closes:
        return None
    price_hi = max(dry_closes)
    price_lo = min(dry_closes)
    if price_lo <= 0:
        return None
    price_range = (price_hi - price_lo) / price_lo * 100
    if price_range > price_range_max_pct:
        return None

    # ③ 현재 봉 거래량 폭발: dry 평균의 breakout_min_vol_ratio 배 이상
    if avg_dry <= 0 or curr_vol < avg_dry * breakout_min_vol_ratio:
        return None

    # ④ 현재가 상승 확인 (수축 구간 최고가 이상)
    if curr <= price_hi:
        return None

    shrink_pct = (1 - avg_dry / avg_base) * 100
    vol_surge  = curr_vol / avg_dry

    return (f"VOL_DRY_UP(거래량{shrink_pct:.0f}%수축"
            f"→{vol_surge:.1f}배폭발"
            f"→{curr:,}돌파)")
