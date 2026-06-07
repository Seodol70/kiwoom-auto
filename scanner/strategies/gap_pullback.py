"""
gap_pullback.py — C전략: 갭 상승 첫 눌림목 진입

동작 원리:
  1. 당일 시가가 전일 종가 대비 +2~8% 갭 상승한 종목 대상
  2. 장 시작 후 1~4봉 안에 발생한 첫 음봉(눌림) 확인
  3. 현재가가 그 음봉의 고가를 돌파하면 진입
  4. 갭 아래(시가 -1%)로 내려가면 무효 → 진입 안 함

핵심 아이디어:
  "갭 상승 = 세력의 의도 확인, 눌림목 = 뒤늦은 매도·차익실현,
   고점 재돌파 = 세력이 다시 받아주는 신호"
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


class GapPullbackStrategy(BaseStrategy):
    """C전략: 갭 상승 첫 눌림목 진입."""

    # 종목별 마지막 신호 시각 — _emit() 쿨다운과 별개로 전략 레벨에서 차단
    _last_signal_ts: dict[str, float] = {}

    def __init__(self):
        super().__init__("GAP_PULLBACK")

    def evaluate(
        self,
        snap: "StockSnapshot",
        cfg:  "SmartScannerConfig",
        index_history: Optional[dict[str, list[float]]] = None,
    ) -> Optional["ScanSignal"]:

        if not getattr(cfg, "gap_pullback_enabled", True):
            return None

        # ── 종목별 쿨다운 (기본 300초 = 5분) — 동일 종목 신호 스팸 방지
        _cooldown = float(getattr(cfg, "gap_pullback_cooldown_sec", 300.0))
        _now_ts = time.monotonic()
        _last_ts = GapPullbackStrategy._last_signal_ts.get(snap.code, 0.0)
        if _now_ts - _last_ts < _cooldown:
            return None

        # ── 시간 제한: 09:30~10:30 (갭 첫 눌림 발생 구간)
        now_t = datetime.now().time()
        start_t = dtime(*[int(x) for x in getattr(cfg, "gap_pullback_start", "9:30").split(":")])
        end_t   = dtime(*[int(x) for x in getattr(cfg, "gap_pullback_end",   "10:30").split(":")])
        if not (start_t <= now_t <= end_t):
            return None

        closes  = snap.closes_1min
        opens   = snap.opens_1min
        highs   = snap.highs_1min
        lows    = snap.lows_1min

        if len(closes) < 5:
            return None

        # ── 1. 갭 상승 확인
        prev_close = snap.prev_close
        open_price = snap.open_price
        if prev_close <= 0 or open_price <= 0:
            return None

        gap_pct = (open_price - prev_close) / prev_close * 100
        gap_min = float(getattr(cfg, "gap_pullback_min_pct", 2.0))
        gap_max = float(getattr(cfg, "gap_pullback_max_pct", 8.0))
        if not (gap_min <= gap_pct <= gap_max):
            ScannerLogger.rejected(snap.code, snap.name, "GAP_PULLBACK",
                f"갭 범위 벗어남 {gap_pct:.1f}% (허용 {gap_min}~{gap_max}%)")
            return None

        # ── 2. 첫 눌림(음봉) 탐색: 최근 8봉 내 음봉 찾기
        lookback = min(8, len(closes))
        bearish_idx = None
        for i in range(len(closes) - lookback, len(closes)):
            if i < 0:
                continue
            if len(opens) > i and closes[i] < opens[i]:  # 음봉
                bearish_idx = i
                break  # 첫 번째 음봉만 사용

        if bearish_idx is None:
            ScannerLogger.rejected(snap.code, snap.name, "GAP_PULLBACK",
                f"최근 {lookback}봉 내 음봉 없음 (갭 {gap_pct:.1f}%)")
            return None

        # ── 3. 음봉 고점 돌파 확인
        bearish_high = highs[bearish_idx] if len(highs) > bearish_idx else 0
        if bearish_high <= 0:
            return None

        current = snap.current_price
        if current <= bearish_high:
            ScannerLogger.rejected(snap.code, snap.name, "GAP_PULLBACK",
                f"음봉 고점 미돌파 (현재 {current:,} ≤ 음봉고점 {bearish_high:,})")
            return None

        # ── 4. 갭 무효화 차단: 현재가가 시가 -1% 미만이면 갭 붕괴
        gap_invalid_floor = open_price * (1 - getattr(cfg, "gap_pullback_floor_pct", 1.0) / 100)
        if current < gap_invalid_floor:
            ScannerLogger.rejected(snap.code, snap.name, "GAP_PULLBACK",
                f"갭 붕괴 — 현재가 {current:,} < 시가×{100-getattr(cfg,'gap_pullback_floor_pct',1.0):.0f}% {gap_invalid_floor:,.0f}")
            return None

        # ── 5. 거래대금 급증 확인 (음봉 이후 회복 봉에서 에너지 확인)
        vols = snap.volumes_1min
        surge_mult = float(getattr(cfg, "gap_pullback_vol_surge", 1.5))
        if len(vols) >= 6:
            avg_v = sum(vols[-6:-1]) / 5
            if avg_v > 0 and vols[-1] < avg_v * surge_mult:
                ScannerLogger.rejected(snap.code, snap.name, "GAP_PULLBACK",
                    f"거래량 미달 — 현재봉 {vols[-1]:,} < 평균 {avg_v:,.0f} × {surge_mult}")
                return None

        # ── 6. MTF 추세 일치 (방향1 연동)
        if getattr(cfg, "mtf_enabled", True) and not getattr(snap, "mtf_aligned", True):
            mtf_bars = int(getattr(snap, "mtf_tf5_bars", 0))
            if mtf_bars >= int(getattr(cfg, "mtf_min_5min_bars", 3)):
                ScannerLogger.rejected(snap.code, snap.name, "GAP_PULLBACK_MTF",
                    f"5분봉 추세 불일치 (5분EMA기울기={getattr(snap,'mtf_tf5_slope',0):+.1f})")
                return None

        reason = (
            f"[GAP_PULLBACK] 갭{gap_pct:.1f}%↑ | "
            f"음봉고점 {bearish_high:,} 돌파 → {current:,} | "
            f"시가 {open_price:,}"
        )
        ScannerLogger.passed(snap.code, snap.name, "GAP_PULLBACK", reason)

        # 쿨다운 타임스탬프 갱신
        GapPullbackStrategy._last_signal_ts[snap.code] = _now_ts

        ai_features = IndicatorService.get_ai_features(snap, index_history=index_history, config=cfg)
        ai_features["gap_pct"] = gap_pct
        ai_features["bearish_high"] = float(bearish_high)

        return ScanSignal(
            snap.code, snap.name, self.name, reason, current,
            is_warmup=False,
            values=ai_features,
        )
