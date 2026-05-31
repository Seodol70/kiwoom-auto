from __future__ import annotations
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.evaluators.overheat_pullback import OverheatPullbackEvaluator
from scanner.indicator_service import IndicatorService

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

_evaluator_cache: Optional[OverheatPullbackEvaluator] = None


class OverheatPullbackStrategy(BaseStrategy):
    """
    과열(Level 3) 후 눌림목(Level 1) 진입 전략 (OVERHEAT_PULLBACK).

    Phase 3 수동 검증 모드:
      - 신호 발생 시 자동 매수 없음 — 대시보드에 'OP:눌림목' 태그만 표시
      - enabled_strategies에 추가 시 signal_type="OVERHEAT_PULLBACK" 신호 발행
      - trading_controller에서 이 신호 타입을 수동 확인 후 자동매매 바인딩 예정
    """

    def __init__(self):
        super().__init__("OVERHEAT_PULLBACK")
        self._ev: Optional[OverheatPullbackEvaluator] = None

    def _get_evaluator(self, cfg: SmartScannerConfig) -> OverheatPullbackEvaluator:
        global _evaluator_cache
        if self._ev is None or (self._ev.config is not cfg):
            self._ev = OverheatPullbackEvaluator(cfg)
            _evaluator_cache = self._ev
        return self._ev

    def evaluate(
        self,
        snap: StockSnapshot,
        cfg: SmartScannerConfig,
        index_history: Optional[dict[str, list[float]]] = None,
    ) -> Optional[ScanSignal]:

        closes = list(snap.closes_1min or [])
        highs  = list(snap.highs_1min  or [])
        lows   = list(snap.lows_1min   or [])
        vols   = list(snap.volumes_1min or [])

        # 최소 데이터 조건 (EMA20 + ATR14 + 안전마진)
        if len(closes) < 35:
            return None

        # VWAP 지지 필터: 현재가 < VWAP 이면 하방 경직성 미확보
        vwap = float(getattr(snap, 'vwap', 0) or 0)
        if vwap > 0 and snap.current_price < vwap:
            return None

        # 분봉 딕셔너리 형식으로 변환 (거래대금 = 종가 × 거래량 근사)
        candle_history = []
        for i, c in enumerate(closes):
            h = highs[i] if i < len(highs) else c
            l = lows[i]  if i < len(lows)  else c
            v = vols[i]  if i < len(vols)  else 0
            candle_history.append({
                "close": c, "high": h, "low": l,
                "trading_value": c * v,
            })

        # [FIX 2026-05-28] 일봉 정배열 락 — 실제 일봉 데이터(snap.daily_closes) 우선 사용
        # 미니 제미니 조언: 1분봉이 우상향해도 일봉이 역배열이면 매물대에 막힘.
        # 실제 일봉으로 정확한 추세의 뼈대 검증 (이전: 1분봉 근사로 약했던 부분 강화)
        daily_ctx = {}
        if hasattr(snap, "daily_closes") and len(snap.daily_closes) >= 23:
            from scanner.indicator_service import IndicatorService
            daily_ctx = IndicatorService.get_daily_context(
                snap.daily_closes, snap.current_price
            )
        elif len(closes) >= 23:
            # 일봉 데이터 부족 시에만 1분봉 MA20 근사 (fallback)
            ma20_now  = sum(closes[-20:]) / 20
            ma20_prev = sum(closes[-23:-3]) / 20
            daily_ctx = {
                "ma20_slope_up": ma20_now >= ma20_prev,
                "above_ma20":    snap.current_price >= ma20_now,
                "daily_ma20":    ma20_now,
            }
        else:
            daily_ctx = {"ma20_slope_up": True, "above_ma20": True, "daily_ma20": 0.0}

        ev = self._get_evaluator(cfg)
        result = ev.evaluate(
            candle_history=candle_history,
            daily_info=daily_ctx,
            code=snap.code,
            name=snap.name,
        )

        if not result["is_buy_signal"]:
            return None

        debug = result.get("debug_info") or {}
        reason = (
            f"[OVERHEAT_PULLBACK] "
            f"과열Lv{debug.get('max_level_history', '?')}→눌림Lv{debug.get('current_level', '?')} | "
            f"ATR:{debug.get('atr14', '?')} | "
            f"Vol+{debug.get('volume_surge', 0):.1f}x | "
            f"MTF:{debug.get('mtf_strength', '?')}"
        )

        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)
        ai_features.update({
            "op_current_level":   debug.get("current_level", 0),
            "op_max_level":       debug.get("max_level_history", 0),
            "op_volume_surge":    debug.get("volume_surge", 0.0),
            "op_mtf_strength":    debug.get("mtf_strength", 0),
            "op_atr14":           debug.get("atr14", 0.0),
            "op_ema20":           debug.get("ema20", 0.0),
        })

        return ScanSignal(
            snap.code, snap.name, self.name, reason, snap.current_price,
            is_warmup=False,
            values=ai_features,
        )
