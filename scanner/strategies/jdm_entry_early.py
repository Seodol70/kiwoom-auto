from __future__ import annotations
import logging
import time
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.indicator_service import IndicatorService
from scanner.signal_evaluator import check_jdm_entry_early

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class JdmEntryEarlyStrategy(BaseStrategy):
    """
    [2026-06-19] JDM(Joseph) 조기 진입 전략.
    거래량급증(후행지표) 확정을 기다리지 않고 선행지표 단독 강세로 진입한다.
    smart_scanner 전략 루프에서 JDM_ENTRY 다음 순서로 평가되므로, JDM_ENTRY가
    이미 신호를 낸 경우(거래량 게이트 통과)에는 이 전략이 호출되지 않는다.
    모든 판정 로직은 signal_evaluator.check_jdm_entry_early()에 위임됨.
    """

    # 종목별 마지막 신호 시각 — 동일 종목 신호 스팸 방지 (JDM_ENTRY와 별도 네임스페이스)
    _last_signal_ts: dict[str, float] = {}

    def __init__(self):
        super().__init__("JDM_ENTRY_EARLY")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig,
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        """
        신호 판정을 수행하고 ScanSignal 객체를 생성하여 반환한다.
        """
        _cooldown = float(getattr(cfg, "signal_cooldown_sec", 60.0))
        _now_ts = time.monotonic()
        if _now_ts - JdmEntryEarlyStrategy._last_signal_ts.get(snap.code, 0.0) < _cooldown:
            return None

        reason = check_jdm_entry_early(snap, cfg)
        if reason is None:
            return None

        # AI 피처 추출 (학습용 데이터 수집) — JDM_ENTRY와 동일한 피처셋 유지
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        ai_features["li_bs"] = round(IndicatorService.calc_bid1_slope_score(
            list(getattr(snap, "bid1_history", None) or [])), 3)
        ai_features["li_vb"] = round(IndicatorService.calc_vol_burst_score(
            list(getattr(snap, "volumes_1min", None) or [])), 3)
        ai_features["li_cr"] = round(IndicatorService.calc_chejan_reversal_score(
            list(getattr(snap, "chejan_history", None) or [])), 3)
        ai_features["li_ca"] = round(IndicatorService.calc_chejan_acceleration(
            list(getattr(snap, "chejan_history", None) or [])), 3)
        ai_features["li_hp"] = round(IndicatorService.calc_hoga_pressure_score(
            int(getattr(snap, "total_ask_qty", 0) or 0),
            int(getattr(snap, "total_bid_qty", 0) or 0)), 3)
        ai_features["li_hv"] = round(IndicatorService.calc_hoga_velocity(
            list(getattr(snap, "bid_qty_sums_history", None) or []) or None), 3)
        ai_features["li_aw"] = round(IndicatorService.calc_ask1_wall_collapse_score(
            list(getattr(snap, "ask1_qty_history", None) or [])), 3)
        ai_features["li_tv"] = round(IndicatorService.calc_tick_vol_accel_score(
            list(getattr(snap, "tick_vol_history", None) or [])), 3)
        ai_features["li_rs"] = round(IndicatorService.calc_rs_leading_score(
            float(getattr(snap, "rs_score", 0.0) or 0.0)), 3)
        ai_features["li_leading"] = round(IndicatorService.get_leading_score(snap) or 0.0, 3)

        JdmEntryEarlyStrategy._last_signal_ts[snap.code] = _now_ts

        is_warmup = "WARMUP" in reason
        entry_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        change_pct = float(getattr(snap, "change_pct", 0) or 0)

        if entry_low > 0:
            ai_features["entry_candle_low"] = entry_low
        if change_pct != 0:
            ai_features["change_pct"] = change_pct

        return ScanSignal(
            snap.code, snap.name, self.name, reason, snap.current_price,
            is_warmup=is_warmup,
            values=ai_features
        )
