"""
장동민 전략 - 90분 단기 매매
기술적 지표(이동평균선, RSI, 볼린저 밴드 등)를 활용한 단기 매매 전략
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import logging

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    """전략 파라미터 — 백테스트 최적화 결과 적용"""
    # 이동평균선 — 최적값: MA5→7, MA20→15
    ma_short: int = 7          # 단기 이동평균 기간 (최적화됨)
    ma_long: int = 15          # 장기 이동평균 기간 (최적화됨)

    # RSI — 유지
    rsi_period: int = 14
    rsi_oversold: float = 35.0   # 최적화됨: 30→35 (더 높은 역추세 신호)
    rsi_overbought: float = 70.0

    # 볼린저 밴드
    bb_period: int = 20
    bb_std: float = 2.0

    # 매매 조건 — 공격형으로 강화됨 (야간 포지션 보유 방지)
    holding_minutes: int = 60    # 단축됨: 90분 → 60분 (빨리 나가기)
    stop_loss_pct: float = -1.2   # 손절 타이트: -1.5% → -1.2% (공격적 진입 시 손실 방어)
    take_profit_pct: float = 3.0  # 익절: 3.0% (절반 매도 구현은 별도)

    # 주문
    order_qty: int = 1         # 기본 주문 수량


# ---------------------------------------------------------------------------
# 상태
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """보유 포지션"""
    code: str
    name: str
    qty: int
    entry_price: float
    entry_time: datetime
    stop_loss: float = 0.0
    take_profit: float = 0.0


@dataclass
class StrategyState:
    """전략 실행 상태"""
    position: Optional[Position] = None
    last_signal: str = "NONE"   # BUY / SELL / HOLD / NONE
    last_updated: Optional[datetime] = None
    candles: list = field(default_factory=list)  # OHLCV 캔들 데이터


# ---------------------------------------------------------------------------
# 기술적 지표 계산
# ---------------------------------------------------------------------------

def calc_ma(closes: list[float], period: int) -> Optional[float]:
    """단순 이동평균(SMA) — numpy 가속"""
    if len(closes) < period:
        return None
    return float(np.mean(closes[-period:]))


def calc_ema(closes: list[float], period: int) -> Optional[float]:
    """지수이동평균(EMA) — 전체 시계열을 순차 계산 (Wilder smoothing)

    k = 2 / (period + 1)
    EMA_t = price_t * k + EMA_{t-1} * (1 - k)
    초기값: 첫 period개의 단순평균
    """
    if len(closes) < period:
        return None
    arr = np.array(closes, dtype=np.float64)
    k = 2.0 / (period + 1)
    ema = float(arr[:period].mean())
    for price in arr[period:]:
        ema = float(price) * k + ema * (1.0 - k)
    return ema


def calc_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> Optional[float]:
    """ATR(Average True Range) — Wilder smoothing 기반."""
    if period <= 0:
        return None
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return None

    h = np.array(highs[-(period + 1):], dtype=np.float64)
    l = np.array(lows[-(period + 1):], dtype=np.float64)
    c = np.array(closes[-(period + 1):], dtype=np.float64)

    prev_close = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)))
    if tr.size < period:
        return None
    atr = float(tr[:period].mean())
    # period와 동일 길이면 초기값만으로 충분; 일반식 유지해 향후 확장 대비
    alpha = 1.0 / period
    for v in tr[period:]:
        atr = (1.0 - alpha) * atr + alpha * float(v)
    return atr


def get_trend_status(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[int],
    *,
    ema_period: int = 20,
    atr_period: int = 14,
    volume_lookback: int = 20,
) -> int:
    """
    고도화된 추세 강도(0~3) 판정 알고리즘 — IndicatorService 위임
    """
    from scanner.indicator_service import IndicatorService
    return IndicatorService.get_trend_status(
        closes, highs, lows, volumes, ema_period, atr_period, volume_lookback
    )


    
    # [필수] 상승 추세 기초 조건: 가격 > EMA AND EMA 기울기 > 0
    if cur_price <= ema_now or ema_now <= ema_prev:
        return 0

    # 1️⃣ 거리 점수 (Distance Score)
    dist_atr = (cur_price - ema_now) / atr
    
    # 2️⃣ 가속도 점수 (Slope Acceleration)
    slope_now = ema_now - ema_prev
    slope_prev = ema_prev - ema_prev2
    is_accelerating = (slope_now > slope_prev)

    # 3️⃣ 수급 점수 (Volume Score)
    need_vol = volume_lookback + 1
    has_vol = (len(volumes) >= need_vol and any(v > 0 for v in volumes[-need_vol:]))
    vol_ratio = 1.0
    if has_vol:
        avg_vol = float(np.mean(np.array(volumes[-(need_vol):-1], dtype=np.float64)))
        if avg_vol > 0:
            vol_ratio = float(volumes[-1]) / avg_vol
        else:
            has_vol = False

    # 4️⃣ 박스권 응축 확인 (Consolidation) - 변동성이 극도로 낮아진 상태
    # ATR이 가격의 0.4% 이하이면 응축으로 판단
    is_consolidating = (atr / cur_price < 0.004)

    # ── 종합 판정 ──
    # [Level 3: Strong] 강력한 추세 + 수급 + 가속 (또는 응축 후 돌파)
    if dist_atr >= 1.2 and vol_ratio >= 1.5 and is_accelerating:
        return 3
    if dist_atr >= 0.8 and vol_ratio >= 2.0 and is_consolidating:
        # 박스권 응축 후 거래량 실린 돌파는 거리가 짧아도 강력
        return 3
    if dist_atr >= 1.5 and vol_ratio >= 1.2:
        return 3
    
    # [Level 2: Medium] 안정적 추세 + 수급
    if dist_atr >= 0.7 and vol_ratio >= 1.1:
        return 2
    if dist_atr >= 1.0: # 수급 부족해도 거리가 충분하면 2단계
        return 2
        
    # [Level 1: Weak] 초기 추세
    if dist_atr >= 0.2:
        return 1
        
    return 0


def calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """RSI(Relative Strength Index) — numpy 가속"""
    if len(closes) < period + 1:
        return None
    arr    = np.array(closes[-(period + 1):], dtype=np.float64)
    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def calc_bollinger_bands(
    closes: list[float], period: int = 20, std_mult: float = 2.0
) -> Optional[tuple[float, float, float]]:
    """볼린저 밴드 (upper, middle, lower) — numpy 가속"""
    if len(closes) < period:
        return None
    arr    = np.array(closes[-period:], dtype=np.float64)
    middle = float(arr.mean())
    std    = float(arr.std())
    return middle + std_mult * std, middle, middle - std_mult * std


def calc_indicators(closes: list[float], cfg: StrategyConfig) -> dict:
    """모든 지표를 한번에 계산해 반환"""
    return {
        "ma_short": calc_ma(closes, cfg.ma_short),
        "ma_long": calc_ma(closes, cfg.ma_long),
        "rsi": calc_rsi(closes, cfg.rsi_period),
        "bb": calc_bollinger_bands(closes, cfg.bb_period, cfg.bb_std),
    }


def calc_pivot_r2(prev_high: int, prev_low: int, prev_close: int) -> float:
    """
    피봇 2차 저항선(R2) 계산.
    전일 고가, 저가, 종가를 기반으로 당일 목표가를 계산한다.

    공식:
        P = (고 + 저 + 종) / 3
        R2 = P + (고 - 저)

    Args:
        prev_high: 전일 고가
        prev_low: 전일 저가
        prev_close: 전일 종가

    Returns:
        피봇 R2 값 (float). 입력값이 0 이하면 0.0 반환
    """
    if prev_high <= 0 or prev_low <= 0 or prev_close <= 0:
        return 0.0
    pivot = (prev_high + prev_low + prev_close) / 3.0
    return pivot + (prev_high - prev_low)


def check_daily_alignment(daily_closes: list[float]) -> bool:
    """
    일봉 정배열 확인 (5일 MA > 10일 MA > 20일 MA).
    상승추세의 시작 또는 확립 단계를 판별한다.

    Args:
        daily_closes: 최신순 일봉 종가 리스트
                      (예: [100, 99, 98, ...] 형태로 최신부터 과거순)

    Returns:
        True if 5일 MA > 10일 MA > 20일 MA (정배열 확립)
        False otherwise (데이터 부족 포함)
    """
    if len(daily_closes) < 20:
        return False

    ma5 = sum(daily_closes[:5]) / 5
    ma10 = sum(daily_closes[:10]) / 10
    ma20 = sum(daily_closes[:20]) / 20

    return ma5 > ma10 > ma20


def get_daily_context(
    daily_closes: list[float],
    current_price: float,
    near_high_threshold_pct: float = 3.0,
) -> dict:
    """
    일봉 데이터 기반 매매 맥락 정보를 반환한다.

    Args:
        daily_closes:            최신순 일봉 종가 리스트 (최대 120개)
        current_price:           현재 분봉 현재가
        near_high_threshold_pct: 신고가 근처 판정 기준 (%) — 25일 최고가 대비 이내

    Returns:
        dict:
            above_ma20  (bool)  — 현재가 ≥ 일봉 20MA  → 단기 추세 기준
            above_ma60  (bool)  — 현재가 ≥ 일봉 60MA  → 중기 추세 기준
            near_high   (bool)  — 25일 신고가 근처(overhead 매물대 없음)
            daily_ma20  (float) — 일봉 20MA 값 (0 = 데이터 부족)
            daily_ma60  (float) — 일봉 60MA 값 (0 = 데이터 부족)
            high_25d    (float) — 최근 25일 최고 종가 (0 = 데이터 부족)
    """
    result = {
        "above_ma20": True, "above_ma60": True,
        "near_high": False,
        "daily_ma20": 0.0, "daily_ma60": 0.0,
        "high_25d": 0.0,
        "ma20_slope_up": True,   # 일봉 20MA 우상향 여부 (데이터 부족 시 fail-open)
    }

    if len(daily_closes) < 20 or current_price <= 0:
        # 데이터 부족 → 필터 통과 (fail-open)
        return result

    daily_ma20 = sum(daily_closes[:20]) / 20
    result["daily_ma20"] = daily_ma20
    result["above_ma20"] = current_price >= daily_ma20

    # MA20 기울기: 3거래일 전 MA20 대비 현재 MA20이 우상향인지 확인
    # daily_closes는 최신순 정렬 → [0]=오늘, [3]=3거래일 전
    if len(daily_closes) >= 23:   # MA20(20개) + 3일 오프셋 = 23개 필요
        ma20_3d_ago = sum(daily_closes[3:23]) / 20
        result["ma20_slope_up"] = daily_ma20 > ma20_3d_ago
    # 23개 미만이면 fail-open(True) 유지

    # MA60 — 데이터가 60개 이상일 때만 계산, 부족하면 fail-open
    if len(daily_closes) >= 60:
        daily_ma60 = sum(daily_closes[:60]) / 60
        result["daily_ma60"] = daily_ma60
        result["above_ma60"] = current_price >= daily_ma60

    # 신고가 근처: 최근 25일 최고가 대비 near_high_threshold_pct 이내
    n = min(25, len(daily_closes))
    high_25d = max(daily_closes[:n])
    result["high_25d"] = high_25d
    if high_25d > 0:
        result["near_high"] = current_price >= high_25d * (1.0 - near_high_threshold_pct / 100.0)

    return result
