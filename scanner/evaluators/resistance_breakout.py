"""
resistance_breakout.py — 저항선(직전 N분 구간 고점) 돌파 신호 평가

배경(2026-06-30): "오를 종목 포착 3가지 신호" 중 ③번. 과거 BREAKOUT 전략
(scanner/evaluators/breakout.py)은 "전일종가 대비 +3%"를 기준선으로 삼아
신호가 11,128건/일(47%)까지 폭증하는 노이즈로 2026-06-02 제거됐다.
이번 설계는 기준선을 "직전 N분 구간의 실제 고점(국소 저항선)"으로 바꾸고,
같은 날 분석에서 가장 강한 단일 변별력을 보인 trend_level 게이트(lv0 0%
→lv3 43.6% 단조증가, position.log 173건 기준)와 검증된 거래량 동반 확인
(JDM의 check_volume_surge)을 결합해 허위양성을 줄인다.
"""
from typing import Optional, TYPE_CHECKING

from scanner.scanner_logger import ScannerLogger
from .common import check_volume_surge

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig


def check_resistance_breakout(
    snap: "StockSnapshot",
    cfg: "SmartScannerConfig",
) -> Optional[str]:
    """
    직전 N분 구간 고점(저항선)을 현재 봉이 처음으로 상회하는지 확인한다.
    """
    closes = list(snap.closes_1min or [])
    highs = list(snap.highs_1min or [])
    lookback = int(getattr(cfg, "rb_resistance_lookback_min", 20))

    if len(closes) < lookback + 1:
        ScannerLogger.rejected(snap.code, snap.name, "RB_DATA",
            f"1분봉 데이터 부족 ({len(closes)}/{lookback + 1})")
        return None

    # ── 1. 저항선 산출 — 현재 봉을 제외한 직전 N분 구간 고점
    if len(highs) >= lookback + 1:
        resistance = max(highs[-(lookback + 1):-1])
    else:
        resistance = max(closes[-(lookback + 1):-1])

    if resistance <= 0:
        return None

    # ── 2. 돌파 확인 — 현재가가 저항선을 처음 상회
    if snap.current_price <= resistance:
        return None

    # ── 3. trend_level 게이트
    # [근거 2026-06-30] position.log 173건 FIFO 매칭: lv0 0%, lv1 16.7%,
    # lv2 31.0%, lv3 43.6% — 단조 증가. lv0~1 표본은 8건뿐이라 확정 기준은
    # 아니지만, 신규 신호 도입 시점이라 보수적으로 lv2 이상만 허용한다.
    trend_lv = int(getattr(snap, "trend_level", 0))
    min_trend_lv = int(getattr(cfg, "rb_min_trend_level", 2))
    if trend_lv < min_trend_lv:
        ScannerLogger.rejected(snap.code, snap.name, "RB_TREND",
            f"추세 약함 — trend_lv{trend_lv} < {min_trend_lv} (저항선 {resistance:,.0f} 돌파했으나 차단)")
        return None

    # ── 4. 거래량 동반 확인 — 단순 가격 돌파만으로는 허위양성이 많다는
    # 과거 BREAKOUT 실패 원인을 반복하지 않기 위해 JDM과 동일한 검증된
    # 거래량 급증 게이트를 재사용한다.
    vol_mult = float(getattr(cfg, "rb_volume_surge_mult", 2.0))
    vol_lookback = int(getattr(cfg, "rb_volume_surge_lookback", 10))
    r_vol = check_volume_surge(snap, vol_mult, lookback=vol_lookback)
    if r_vol is None:
        ScannerLogger.rejected(snap.code, snap.name, "RB_VOL",
            f"거래량 동반 없음 — 저항선({resistance:,.0f}) 돌파했으나 거래량 미달")
        return None

    reason = (
        f"[RESISTANCE_BREAKOUT] 저항선({resistance:,.0f}) 돌파 "
        f"| 현재가 {snap.current_price:,} | trend_lv{trend_lv} | {r_vol}"
    )
    ScannerLogger.passed(snap.code, snap.name, "RESISTANCE_BREAKOUT", reason)
    return reason
