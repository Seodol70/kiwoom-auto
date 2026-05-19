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
