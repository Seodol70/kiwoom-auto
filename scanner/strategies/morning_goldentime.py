"""
morning_goldentime.py — E전략: 오전 골든타임 집중 매매 (09:00~09:30)

타임슬롯:
  Phase 2 (09:00~09:10) — 시가 돌파 + 호가 압력
    • 현재가가 시가를 돌파하고 상승 중인 종목
    • 매수 잔량이 매도 잔량보다 N배 이상 (장 초반 매수벽 확인)
    • 거래대금 기준 이상 (소형주 배제)
    • 갭 상승(2~8%) 종목 우대 (갭 돌파는 강한 신호)

  Phase 3 (09:10~09:30) — 첫 파동 눌림목 + VWAP 지지
    • 장 초반 고점 후 눌림 발생 → VWAP 위에서 반등 확인
    • EMA20 위에서 지지받는 눌림
    • 거래량 급감 후 회복 (눌림의 진정성 확인)
    • 추세 레벨 lv2 이상 (단기 추세 존재해야)

대시보드에서 morning_goldentime_enabled 토글로 활성화/비활성화.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, TYPE_CHECKING
from datetime import datetime, time as dtime

from scanner.strategies.base import BaseStrategy
from scanner.models import ScanSignal
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)


class MorningGoldentimeStrategy(BaseStrategy):
    """E전략: 오전 골든타임 (09:00~09:30) 집중 매매."""

    _last_signal_ts: dict[str, float] = {}

    def __init__(self):
        super().__init__("MORNING_GOLDENTIME")

    # ──────────────────────────────────────────────────────────────────────────
    def evaluate(
        self,
        snap: "StockSnapshot",
        cfg:  "SmartScannerConfig",
        index_history: Optional[dict[str, list[float]]] = None,
    ) -> Optional["ScanSignal"]:

        if not getattr(cfg, "morning_goldentime_enabled", False):
            return None

        # 종목별 쿨다운 (기본 180초)
        _cooldown = float(getattr(cfg, "morning_goldentime_cooldown_sec", 180.0))
        _now_ts = time.monotonic()
        _last_ts = MorningGoldentimeStrategy._last_signal_ts.get(snap.code, 0.0)
        if _now_ts - _last_ts < _cooldown:
            return None

        # 시간 제한: 09:00~09:30
        now_t = datetime.now().time()
        phase2_start = dtime(9, 0, 0)
        phase2_end   = dtime(9, 10, 0)
        phase3_end   = dtime(9, 30, 0)

        if not (phase2_start <= now_t <= phase3_end):
            return None

        in_phase2 = now_t <= phase2_end

        # ── 공통 사전조건 ──────────────────────────────────────────────────────
        open_price = int(getattr(snap, "open_price", 0) or 0)
        current    = int(getattr(snap, "current_price", 0) or 0)
        prev_close = int(getattr(snap, "prev_close", 0) or 0)

        if open_price <= 0 or current <= 0:
            return None

        # 거래대금 최소 (기본 30억)
        trade_amt = float(getattr(snap, "trade_amount", 0) or 0)
        _min_amt  = float(getattr(cfg, "morning_goldentime_min_trade_amount", 3_000_000_000))
        if trade_amt < _min_amt:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME",
                f"거래대금 미달 {trade_amt/1e8:.1f}억 < {_min_amt/1e8:.0f}억")
            return None

        # 지수 급락 차단 (공통 설정 index_block_pct 재사용)
        _idx_blk = float(getattr(cfg, "index_block_pct", -2.0))
        kospi_chg  = float(getattr(cfg, "kospi_chg_pct",  0.0))
        kosdaq_chg = float(getattr(cfg, "kosdaq_chg_pct", 0.0))
        if kospi_chg <= _idx_blk or kosdaq_chg <= _idx_blk:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME",
                f"지수 급락 차단 KOSPI={kospi_chg:.1f}% KOSDAQ={kosdaq_chg:.1f}%")
            return None

        # ── Phase 2 (09:00~09:10): 시가 돌파 ─────────────────────────────────
        if in_phase2:
            return self._eval_phase2(snap, cfg, current, open_price, prev_close,
                                     trade_amt, index_history, _now_ts)

        # ── Phase 3 (09:10~09:30): 첫 파동 눌림목 ────────────────────────────
        return self._eval_phase3(snap, cfg, current, open_price, prev_close,
                                 trade_amt, index_history, _now_ts)

    # ──────────────────────────────────────────────────────────────────────────
    def _eval_phase2(
        self, snap, cfg, current, open_price, prev_close,
        trade_amt, index_history, now_ts: float,
    ) -> Optional["ScanSignal"]:
        """09:00~09:10: 시가 돌파 + 호가 압력"""

        # 1. 시가 돌파 (현재가 > 시가)
        if current <= open_price:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P2",
                f"시가 미돌파 현재가={current:,} ≤ 시가={open_price:,}")
            return None

        # 2. 시가 대비 상승률 상한 (과열 차단 — 이미 너무 오른 종목 배제)
        _open_rise = (current - open_price) / open_price * 100
        _open_rise_max = float(getattr(cfg, "morning_goldentime_p2_open_rise_max", 5.0))
        if _open_rise > _open_rise_max:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P2",
                f"시가 대비 과열 {_open_rise:.1f}% > {_open_rise_max:.1f}%")
            return None

        # 3. 호가 압력 — 매수잔량 > 매도잔량 × N배
        total_ask = int(getattr(snap, "total_ask_qty", 0) or 0)
        total_bid = int(getattr(snap, "total_bid_qty", 0) or 0)
        _hoga_mult = float(getattr(cfg, "morning_goldentime_p2_hoga_mult", 1.5))
        if total_ask > 0 and total_bid < total_ask * _hoga_mult:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P2",
                f"호가 압력 미달 bid={total_bid:,} < ask×{_hoga_mult} {total_ask * _hoga_mult:,.0f}")
            return None

        # 4. 갭 상승 여부 (우대 조건, 차단은 아님)
        gap_pct = 0.0
        if prev_close > 0:
            gap_pct = (open_price - prev_close) / prev_close * 100

        _gap_min = float(getattr(cfg, "morning_goldentime_p2_gap_min", 2.0))
        _gap_max = float(getattr(cfg, "morning_goldentime_p2_gap_max", 8.0))
        gap_ok = (_gap_min <= gap_pct <= _gap_max)

        # 5. 체결강도 하한
        chejan = float(getattr(snap, "chejan_strength", 0.0) or 0.0)
        _chejan_min = float(getattr(cfg, "morning_goldentime_p2_chejan_min", 110.0))
        if chejan < _chejan_min:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P2",
                f"체결강도 미달 {chejan:.0f}% < {_chejan_min:.0f}%")
            return None

        gap_tag = f" [갭↑{gap_pct:.1f}%]" if gap_ok else ""
        reason = (
            f"[MORNING_GT_P2] 시가돌파 +{_open_rise:.1f}%{gap_tag} | "
            f"호가압력 bid={total_bid:,}/ask={total_ask:,} | "
            f"체결강도 {chejan:.0f}% | 거래대금 {trade_amt/1e8:.0f}억"
        )
        ScannerLogger.passed(snap.code, snap.name, "MORNING_GOLDENTIME", reason)
        MorningGoldentimeStrategy._last_signal_ts[snap.code] = now_ts

        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)
        ai_features["mg_phase"] = 2.0
        ai_features["mg_open_rise"] = _open_rise
        ai_features["mg_gap_pct"] = gap_pct

        return ScanSignal(
            snap.code, snap.name, self.name, reason, current,
            is_warmup=False,
            values=ai_features,
        )

    # ──────────────────────────────────────────────────────────────────────────
    def _eval_phase3(
        self, snap, cfg, current, open_price, prev_close,
        trade_amt, index_history, now_ts: float,
    ) -> Optional["ScanSignal"]:
        """09:10~09:30: 첫 파동 눌림목 + VWAP 지지"""

        closes  = snap.closes_1min
        volumes = snap.volumes_1min
        lows    = snap.lows_1min

        if len(closes) < 5 or len(volumes) < 5:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P3",
                "캔들 데이터 부족 (5봉 미만)")
            return None

        # 1. 추세 레벨 최소 lv2 (yosep 비활성 시 trend_level=0으로 고정되므로 스킵)
        _yosep_on = bool(getattr(cfg, "yosep_trend_enabled", True))
        trend_lv = int(getattr(snap, "trend_level", 0) or 0)
        _min_lv  = int(getattr(cfg, "morning_goldentime_p3_min_trend_lv", 2))
        if _yosep_on and trend_lv < _min_lv:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P3",
                f"추세 레벨 미달 lv{trend_lv} < lv{_min_lv}")
            return None

        # 2. VWAP 위에서 지지 확인
        vwap = float(getattr(snap, "vwap", 0) or 0)
        if vwap > 0 and current < vwap:
            ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P3",
                f"VWAP 하단 현재가={current:,} < VWAP={vwap:,.0f}")
            return None

        # 3. 눌림목 확인: 최근 장중 고점 대비 현재 하락 (눌림 발생)
        if len(closes) >= 3:
            intraday_high = max(closes[-6:]) if len(closes) >= 6 else max(closes)
            _pullback_pct = (current - intraday_high) / intraday_high * 100
            _pb_min = float(getattr(cfg, "morning_goldentime_p3_pullback_min", -5.0))
            _pb_max = float(getattr(cfg, "morning_goldentime_p3_pullback_max", -0.5))
            if not (_pb_min <= _pullback_pct <= _pb_max):
                ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P3",
                    f"눌림 범위 벗어남 {_pullback_pct:.1f}% (허용 {_pb_min}~{_pb_max}%)")
                return None
        else:
            _pullback_pct = 0.0
            intraday_high = current

        # 4. 거래량 급감 확인 (눌림의 진정성): 최근 2봉 평균이 5봉 평균보다 낮으면 OK
        _vol_decay_enabled = bool(getattr(cfg, "morning_goldentime_p3_vol_decay_check", True))
        if _vol_decay_enabled and len(volumes) >= 5:
            avg5  = sum(volumes[-5:]) / 5
            avg2  = sum(volumes[-2:]) / 2
            _decay_max = float(getattr(cfg, "morning_goldentime_p3_vol_decay_max", 0.8))
            if avg5 > 0 and avg2 > avg5 * _decay_max:
                ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P3",
                    f"거래량 급감 미확인 avg2={avg2:.0f} ≥ avg5×{_decay_max} {avg5*_decay_max:.0f}")
                return None

        # 5. 반등 에너지: 직전봉이 양봉
        if len(closes) >= 2:
            opens_arr = snap.opens_1min
            last_open  = opens_arr[-1] if opens_arr and len(opens_arr) >= 1 else 0
            last_close = closes[-1]
            if last_open > 0 and last_close < last_open:
                ScannerLogger.rejected(snap.code, snap.name, "MORNING_GOLDENTIME_P3",
                    f"직전봉 음봉 (반등 미확인) O={last_open:,} C={last_close:,}")
                return None

        vwap_tag = f" VWAP+{(current/vwap-1)*100:.1f}%" if vwap > 0 else ""
        reason = (
            f"[MORNING_GT_P3] 눌림{_pullback_pct:.1f}%(고점:{intraday_high:,}→{current:,}) | "
            f"추세Lv{trend_lv}{vwap_tag} | "
            f"거래대금 {trade_amt/1e8:.0f}억"
        )
        ScannerLogger.passed(snap.code, snap.name, "MORNING_GOLDENTIME", reason)
        MorningGoldentimeStrategy._last_signal_ts[snap.code] = now_ts

        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)
        ai_features["mg_phase"] = 3.0
        ai_features["mg_pullback_pct"] = _pullback_pct
        ai_features["mg_vwap_dist"] = (current / vwap - 1.0) if vwap > 0 else 0.0

        return ScanSignal(
            snap.code, snap.name, self.name, reason, current,
            is_warmup=False,
            values=ai_features,
        )
