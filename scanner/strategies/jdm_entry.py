from __future__ import annotations
import logging
from typing import Optional, TYPE_CHECKING

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.indicator_service import IndicatorService
from scanner.signal_evaluator import check_jdm_entry

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class JdmStrategy(BaseStrategy):
    """
    JDM(Joseph) 진입 전략.
    복합적인 수급, 추세, 지표 필터를 통과해야 함.
    모든 판정 로직은 signal_evaluator.check_jdm_entry()에 위임됨.
    """

    def __init__(self):
        super().__init__("JDM_ENTRY")

    def evaluate(self, snap: StockSnapshot, cfg: SmartScannerConfig,
                 index_history: Optional[dict[str, list[float]]] = None) -> Optional[ScanSignal]:
        """
        신호 판정을 수행하고 ScanSignal 객체를 생성하여 반환한다.
        """
        # 핵심 판정 로직 위임
        reason = check_jdm_entry(snap, cfg)
        # [2026-05-22] WARNING 로그 2건 제거 (종목당 1건 발생, 메인 스레드 부하)
        if reason is None:
            return None

        # AI 피처 추출 (학습용 데이터 수집)
        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)

        # 선행지표 원시값 기록 — B안: 진입 시점 선행지표 vs 청산 수익률 추적용
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
        ai_features["li_leading"] = round(IndicatorService.get_leading_score(snap) or 0.0, 3)

        # 신호 생성
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
