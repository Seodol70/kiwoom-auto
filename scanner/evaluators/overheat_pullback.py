"""
overheat_pullback.py — 과열 후 눌림목 포착 스킬 (Overheat → Pullback Entry)

아키텍처 설계:
  1. 최근 10분 내 Level 3 (극강 과열) 발생 이력 확인
  2. 현재 상태가 Level 1 (약한 상승) 로 회복
  3. 일봉 MA20 우상향 + 거래대금 가속도 안전장치 동시 만족

효과:
  - 고점 물리개 방지 (Level 3 회피)
  - 휩소 필터링 (거래대금 + 일봉 트렌드 검증)
  - 승률 개선: 단순 눌림목 대비 +15~20%p 예상
"""

from typing import Optional, Dict, Any, List, TYPE_CHECKING
import numpy as np
from scanner.indicator_service import IndicatorService
from scanner.scanner_logger import ScannerLogger

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig


class OverheatPullbackEvaluator:
    """
    과열(Level 3) 후 눌림목(Level 1) 진입 시그널 평가기.

    OOP 구조로 설계하여 기존 함수형 evaluator들과 독립적으로 동작.
    """

    # 기본 파라미터 (config에서 오버라이드 가능)
    DEFAULT_EMA_PERIOD = 20
    DEFAULT_ATR_PERIOD = 14
    DEFAULT_LOOKBACK_MINUTES = 10  # 최근 10분 동안 과열 발생 이력 추적
    DEFAULT_MIN_TRADING_VALUE_5M_AVG = 5_000_000_000  # 50억원 (대형주 기준)
    DEFAULT_VOLUME_SURGE_MULT = 2.0  # 이전 5분 대비 200% 이상
    DEFAULT_LEVEL_3_THRESHOLD = 1.5  # distance >= 1.5 ATR
    DEFAULT_LEVEL_1_THRESHOLD_MIN = 0.3  # distance >= 0.3 ATR
    DEFAULT_LEVEL_1_THRESHOLD_MAX = 1.0  # distance < 1.0 ATR

    def __init__(self, config: Optional['SmartScannerConfig'] = None):
        """
        Args:
            config: SmartScannerConfig 객체. None이면 기본값 사용.
        """
        self.config = config
        # config에서 파라미터 추출 (있으면), 없으면 기본값 사용
        self.ema_period = getattr(config, 'overheat_ema_period', self.DEFAULT_EMA_PERIOD) if config else self.DEFAULT_EMA_PERIOD
        self.atr_period = getattr(config, 'overheat_atr_period', self.DEFAULT_ATR_PERIOD) if config else self.DEFAULT_ATR_PERIOD
        self.lookback_minutes = getattr(config, 'overheat_lookback_minutes', self.DEFAULT_LOOKBACK_MINUTES) if config else self.DEFAULT_LOOKBACK_MINUTES
        self.min_trading_value_5m = getattr(config, 'overheat_min_trading_value_5m_avg', self.DEFAULT_MIN_TRADING_VALUE_5M_AVG) if config else self.DEFAULT_MIN_TRADING_VALUE_5M_AVG
        self.volume_surge_mult = getattr(config, 'overheat_volume_surge_mult', self.DEFAULT_VOLUME_SURGE_MULT) if config else self.DEFAULT_VOLUME_SURGE_MULT
        self.level_3_threshold = getattr(config, 'overheat_level_3_threshold', self.DEFAULT_LEVEL_3_THRESHOLD) if config else self.DEFAULT_LEVEL_3_THRESHOLD
        self.level_1_min = getattr(config, 'overheat_level_1_min', self.DEFAULT_LEVEL_1_THRESHOLD_MIN) if config else self.DEFAULT_LEVEL_1_THRESHOLD_MIN
        self.level_1_max = getattr(config, 'overheat_level_1_max', self.DEFAULT_LEVEL_1_THRESHOLD_MAX) if config else self.DEFAULT_LEVEL_1_THRESHOLD_MAX

    def _estimate_mtf_strength(
        self,
        closes: List[float],
    ) -> int:
        """
        [추가 아이디어] 다중 타임프레임(MTF) 강도 추정.

        5분봉 기준 MA5(최근 5개)가 MA20(최근 20개)을 위로 돌파했는지 확인.
        실전에서는 실제 5분봉 데이터를 받아야 하지만, 여기서는 1분봉 5개 합산으로 근사.

        Return:
            int: 0 (약함) ~ 2 (강함)
        """
        if len(closes) < 25:
            return 0  # 데이터 부족

        # 1분봉 5개 = 약 5분 (근사)
        # 1분봉 20개 = 약 20분 (근사)
        ma5_approx = np.mean(closes[-5:])
        ma20_approx = np.mean(closes[-20:])

        if ma5_approx > ma20_approx:
            # MA5 > MA20: 단기 강세 (MTF 정렬)
            return 2
        elif ma5_approx > ma20_approx * 0.98:
            # MA5가 MA20의 98% 이상 (거의 교점)
            return 1
        else:
            # MA5 < MA20: 단기 약세
            return 0

    def _calculate_trend_level(
        self,
        current_price: float,
        ema_value: float,
        ema_prev: float,
        atr: float,
    ) -> int:
        """
        현재 시점의 추세 레벨(0~3)을 계산한다.

        Args:
            current_price: 현재가
            ema_value: 현재 EMA20 값
            ema_prev: 이전 EMA20 값
            atr: ATR14 값

        Returns:
            int: Level 0~3 (0=추세 없음, 1=약한상승, 2=중간상승, 3=강한상승)
        """
        if atr <= 0:
            return 0

        # 조건: 현재가 > EMA20 AND EMA20 상향
        is_above_ema = current_price > ema_value
        is_ema_up = ema_value > ema_prev

        if not (is_above_ema and is_ema_up):
            return 0

        # 거리를 ATR로 정규화
        distance = (current_price - ema_value) / atr

        if distance >= self.level_3_threshold:
            return 3
        elif distance >= 1.0:
            return 2
        elif distance >= self.level_1_min:
            return 1
        else:
            return 0

    def _extract_candle_data(
        self,
        candle_history: List[Dict[str, Any]],
    ) -> tuple[List[float], List[float], List[float], List[float]]:
        """
        분봉 데이터를 OHLCV + 거래대금으로 정렬하여 추출한다.

        Args:
            candle_history: [{'close': 100, 'high': 102, 'low': 99, 'trading_value': 5000000}, ...]

        Returns:
            (close_prices, high_prices, low_prices, trading_values)

        Raises:
            ValueError: 필수 필드 누락 또는 데이터 타입 오류
        """
        try:
            closes = []
            highs = []
            lows = []
            trading_values = []

            for candle in candle_history:
                # 필수 필드 검증
                if not all(k in candle for k in ['close', 'high', 'low', 'trading_value']):
                    raise ValueError(f"Missing required field in candle: {candle}")

                c = float(candle['close'])
                h = float(candle['high'])
                l = float(candle['low'])
                tv = float(candle['trading_value'])

                # 데이터 유효성 검증
                if c <= 0 or h <= 0 or l <= 0 or tv < 0:
                    raise ValueError(f"Invalid price/value in candle: {candle}")
                if h < max(c, l) or l > min(c, h):
                    raise ValueError(f"Invalid OHLC relationship in candle: {candle}")

                closes.append(c)
                highs.append(h)
                lows.append(l)
                trading_values.append(tv)

            return closes, highs, lows, trading_values

        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"Invalid candle data: {str(e)}")

    def _validate_inputs(
        self,
        candle_history: List[Dict[str, Any]],
        daily_info: Dict[str, Any],
    ) -> Optional[str]:
        """
        입력 데이터의 유효성을 검증하고, 불가하면 거절 사유를 반환한다.

        Args:
            candle_history: 1분봉 리스트
            daily_info: 일봉 정보 딕셔너리

        Returns:
            str: 거절 사유 (유효하면 None)
        """
        # 일봉 정배열 필터 (추세의 뼈대)
        if not daily_info.get('ma20_slope_up', False):
            return "REJECTED_DAILY_TREND_DOWN"

        # 최소 데이터 요구량: EMA20(20) + ATR14(14) + 안전마진 = 최소 35개
        if len(candle_history) < self.ema_period + self.atr_period + 1:
            return "INSUFFICIENT_DATA"

        return None

    def evaluate(
        self,
        candle_history: List[Dict[str, Any]],
        daily_info: Dict[str, Any],
        code: str = "",
        name: str = "",
    ) -> Dict[str, Any]:
        """
        과열 후 눌림목 진입 신호를 평가한다.

        Args:
            candle_history: 1분봉 데이터 (최소 35개, {'close', 'high', 'low', 'trading_value'})
            daily_info: 일봉 정보 {'ma20_slope_up': bool, ...}
            code: 종목코드 (로깅용)
            name: 종목명 (로깅용)

        Returns:
            {
                'is_buy_signal': bool,
                'reason': str,
                'debug_info': {
                    'current_level': int,
                    'level_history': List[int],
                    'atr14': float,
                    'volume_surge': float,
                    'ema20': float,
                    ...
                } | None
            }
        """
        # ──────────────────────────────────────────────────────────────────
        # 1. 입력 검증
        # ──────────────────────────────────────────────────────────────────
        validation_error = self._validate_inputs(candle_history, daily_info)
        if validation_error:
            return {
                "is_buy_signal": False,
                "reason": validation_error,
                "debug_info": None,
            }

        # ──────────────────────────────────────────────────────────────────
        # 2. 분봉 데이터 추출
        # ──────────────────────────────────────────────────────────────────
        try:
            closes, highs, lows, trading_values = self._extract_candle_data(candle_history)
        except ValueError as e:
            return {
                "is_buy_signal": False,
                "reason": "DATA_EXTRACTION_ERROR",
                "debug_info": None,
            }

        # ──────────────────────────────────────────────────────────────────
        # 3. 핵심 지표 계산
        # ──────────────────────────────────────────────────────────────────
        ema20 = IndicatorService.calc_ema(closes, self.ema_period)
        ema20_prev = IndicatorService.calc_ema(closes[:-1], self.ema_period) if len(closes) > self.ema_period else None
        atr14 = IndicatorService.calc_atr(highs, lows, closes, self.atr_period)

        if ema20 is None or ema20_prev is None or atr14 is None or atr14 <= 0:
            return {
                "is_buy_signal": False,
                "reason": "INDICATOR_CALC_ERROR",
                "debug_info": None,
            }

        # ──────────────────────────────────────────────────────────────────
        # 3-1. [추가 아이디어] MTF 강도 평가
        # ──────────────────────────────────────────────────────────────────
        mtf_strength = self._estimate_mtf_strength(closes)
        # MTF 강도는 신호 발생 조건은 아니지만, debug_info에 포함하여
        # 실전 모니터링 시 신호의 신뢰도를 시각화할 수 있음.

        # ──────────────────────────────────────────────────────────────────
        # 4. 최근 N분 레벨 이력 계산
        # ──────────────────────────────────────────────────────────────────
        recent_levels = []
        lookback_period = min(self.lookback_minutes, len(closes) - self.ema_period)

        for idx in range(-lookback_period, 0):
            curr_price = closes[idx]
            curr_ema = IndicatorService.calc_ema(closes[:len(closes) + idx + 1], self.ema_period)
            prev_ema = IndicatorService.calc_ema(closes[:len(closes) + idx], self.ema_period)

            if curr_ema is None or prev_ema is None:
                recent_levels.append(0)
                continue

            level = self._calculate_trend_level(curr_price, curr_ema, prev_ema, atr14)
            recent_levels.append(level)

        current_level = recent_levels[-1] if recent_levels else 0

        # ──────────────────────────────────────────────────────────────────
        # 5. [안전장치 B] 거래대금 가속도 필터
        # ──────────────────────────────────────────────────────────────────
        if len(trading_values) < 10:
            return {
                "is_buy_signal": False,
                "reason": "INSUFFICIENT_VOLUME_DATA",
                "debug_info": None,
            }

        recent_5m_avg = np.mean(trading_values[-5:])
        prev_5m_avg = np.mean(trading_values[-10:-5])

        # 분자가 0일 경우 대비
        if prev_5m_avg <= 0:
            return {
                "is_buy_signal": False,
                "reason": "VOLUME_BASELINE_ERROR",
                "debug_info": None,
            }

        volume_surge_ratio = recent_5m_avg / prev_5m_avg

        # 거래대금 기준치 미달 또는 가속도 부족
        if recent_5m_avg < self.min_trading_value_5m or volume_surge_ratio < self.volume_surge_mult:
            return {
                "is_buy_signal": False,
                "reason": "REJECTED_VOLUME_ACCELERATION",
                "debug_info": {
                    "recent_5m_avg": round(recent_5m_avg, 0),
                    "min_threshold": self.min_trading_value_5m,
                    "volume_surge": round(volume_surge_ratio, 2),
                    "required_surge": self.volume_surge_mult,
                },
            }

        # ──────────────────────────────────────────────────────────────────
        # 6. 핵심 로직: 과열 후 눌림목 조건 검증
        # ──────────────────────────────────────────────────────────────────
        # 조건 A: 현재 상태가 Level 1 (약한 상승, 안정권)
        is_current_level_safe = self.level_1_min <= current_level <= 1

        # 조건 B: 최근 lookback 기간 내에 Level 3 (극강 과열) 발생 이력
        # (현재 인덱스 제외 — 과열 이후 회복된 상태여야 함)
        had_hyper_trend = 3 in recent_levels[:-1] if len(recent_levels) > 1 else False

        if is_current_level_safe and had_hyper_trend:
            # ✅ 매수 신호 확정
            ScannerLogger.passed(
                code, name, "OVERHEAT_PULLBACK",
                f"과열({max(recent_levels)}) → 눌림목({current_level}) | ATR:{atr14:.1f} | Vol:+{volume_surge_ratio:.1f}x"
            )
            return {
                "is_buy_signal": True,
                "reason": "CONFIRMED_PULLBACK_ENTRY",
                "debug_info": {
                    "current_level": current_level,
                    "max_level_history": max(recent_levels) if recent_levels else 0,
                    "level_history": recent_levels,
                    "atr14": round(atr14, 2),
                    "ema20": round(ema20, 0),
                    "volume_surge": round(volume_surge_ratio, 2),
                    "recent_5m_avg_val": round(recent_5m_avg, 0),
                    "mtf_strength": mtf_strength,  # [추가] MA5 > MA20 여부 (0~2)
                },
            }

        # 거절 사유 분류
        if not is_current_level_safe:
            reason = f"WAITING_FOR_PULLBACK_LV{current_level}"
        elif not had_hyper_trend:
            reason = "NO_PRIOR_OVERHEAT"
        else:
            reason = "UNKNOWN_CONDITION"

        return {
            "is_buy_signal": False,
            "reason": reason,
            "debug_info": None,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 호환성 레이어: 기존 함수형 인터페이스 제공 (신호 평가기와 호환)
# ──────────────────────────────────────────────────────────────────────────────

_global_evaluator = None  # 싱글톤 인스턴스 캐시


def get_evaluator(config: Optional['SmartScannerConfig'] = None) -> OverheatPullbackEvaluator:
    """
    글로벌 평가기 인스턴스를 반환한다 (싱글톤).

    config가 변경되면 새 인스턴스를 생성.
    """
    global _global_evaluator
    if _global_evaluator is None or (_global_evaluator.config != config):
        _global_evaluator = OverheatPullbackEvaluator(config)
    return _global_evaluator


def check_overheat_pullback_entry(
    snap: 'StockSnapshot',
    cfg: 'SmartScannerConfig',
) -> Optional[str]:
    """
    기존 신호 평가 함수형 인터페이스 호환.

    Args:
        snap: StockSnapshot 객체 (1분봉 데이터 포함)
        cfg: SmartScannerConfig 객체

    Returns:
        str: 신호 사유 문자열 (거절 시 None)
    """
    closes = list(getattr(snap, 'closes_1min', None) or [])
    if len(closes) < 35:
        return None

    highs   = list(getattr(snap, 'highs_1min',   None) or [])
    lows    = list(getattr(snap, 'lows_1min',    None) or [])
    volumes = list(getattr(snap, 'volumes_1min', None) or [])

    # 거래대금 복원: 종가 × 거래량 (근사)
    candle_history = []
    for i, c in enumerate(closes):
        h = highs[i]   if i < len(highs)   else c
        l = lows[i]    if i < len(lows)    else c
        v = volumes[i] if i < len(volumes) else 0
        candle_history.append({'close': c, 'high': h, 'low': l, 'trading_value': c * v})

    # VWAP 지지 필터: 현재가 < VWAP 이면 하방 경직성 미확보 → 거절
    vwap = float(getattr(snap, 'vwap', 0) or 0)
    if vwap > 0 and snap.current_price < vwap:
        return None

    # 일봉 정보 추출 (실제 일봉 데이터 우선, fallback: 1분봉 MA20 근사)
    daily_closes = getattr(snap, 'daily_closes', None) or []
    if len(daily_closes) >= 23:
        daily_info = IndicatorService.get_daily_context(daily_closes, snap.current_price)
    elif len(closes) >= 23:
        ma20_now  = sum(closes[-20:]) / 20
        ma20_prev = sum(closes[-23:-3]) / 20
        daily_info = {
            "ma20_slope_up": ma20_now >= ma20_prev,
            "above_ma20":    snap.current_price >= ma20_now,
            "daily_ma20":    ma20_now,
        }
    else:
        daily_info = {"ma20_slope_up": True, "above_ma20": True, "daily_ma20": 0.0}

    result = get_evaluator(cfg).evaluate(
        candle_history=candle_history,
        daily_info=daily_info,
        code=snap.code,
        name=snap.name,
    )

    if result['is_buy_signal']:
        debug = result.get('debug_info') or {}
        reason = result['reason']
        if debug:
            reason += f" | Lv{debug.get('current_level', '?')} | Vol+{debug.get('volume_surge', 0):.1f}x"
        return reason

    return None
