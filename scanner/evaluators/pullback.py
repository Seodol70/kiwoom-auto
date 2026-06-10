"""
pullback.py — 눌림목(Pullback) 전략 신호 평가
"""
from typing import Optional, TYPE_CHECKING
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService
from .common import check_vwap_filter

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

def check_pullback_entry(
    snap: "StockSnapshot",
    cfg:  "SmartScannerConfig",
) -> Optional[str]:
    """
    상승 추세(trend_level >= 2) 종목이 EMA20 근처까지 눌렸을 때 진입.
    """
    tlv = int(getattr(snap, "trend_level", 0))
    _min_tlv = int(getattr(cfg, "pullback_min_trend_lv", 3))
    if tlv < _min_tlv:
        ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_TREND",
            f"추세 레벨 부족 — trend_lv={tlv} (요구: >={_min_tlv})")
        return None

    # 선행지표 체크 — 반등 시점에 매수 압력(bs/aw 등) 없으면 차단
    # JDM(0.25)보다 낮은 0.15: 눌림목은 rs가 낮아도 다른 지표로 통과 가능
    _leading = IndicatorService.get_leading_score(snap)
    _lead_thr = float(getattr(cfg, "pullback_leading_score_min", 0.15))
    if _leading is not None and _leading < _lead_thr:
        ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_LEADING",
            f"선행지표 부족 — score={_leading:.2f} (요구: >={_lead_thr:.2f})")
        return None

    # [2026-06-10] 체결 가속도(vel_ratio) 최소 기준 — 에너지 없는 반등 차단
    # 6/9 분석: vel<1.0 손실 5건, vel>1.0 수익 종목 다수 → PULLBACK은 최소 0.5 요구
    _vel = float(getattr(snap, "vel_ratio", 0.0))
    _vel_min = float(getattr(cfg, "pullback_vel_ratio_min", 0.5))
    if _vel_min > 0 and _vel < _vel_min:
        ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_VEL",
            f"반등 체결가속도 부족 — vel_ratio={_vel:.2f} (요구: >={_vel_min:.2f})")
        return None

    closes = snap.closes_1min
    if len(closes) < 20:
        return None

    # [FIX 2026-05-28] 일봉 정배열 락 (추세의 뼈대) — 미니 제미니 조언 반영
    # [FIX 2026-05-29] 일봉 0개이면 통과 — daily_refresh 미연결로 전종목 차단되는 문제 해소
    if len(snap.daily_closes) >= 23:
        _daily_ctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price)
        if getattr(cfg, "daily_ma20_filter_enabled", True):
            if not _daily_ctx["above_ma20"] and _daily_ctx["daily_ma20"] > 0:
                ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_DAILY_MA20",
                    f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {_daily_ctx['daily_ma20']:,.0f}")
                return None
        if getattr(cfg, "daily_ma20_slope_enabled", True):
            if not _daily_ctx.get("ma20_slope_up", True):
                ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_MA20_SLOPE",
                    f"일봉 20MA 기울기 하락 — 추세역배열 차단")
                return None

    ema20 = IndicatorService.calc_ema(closes, 20)
    rsi = IndicatorService.calc_rsi(closes, 14)
    if ema20 is None or rsi is None:
        return None

    # ── [2026-06-02 선택B] 진짜 눌림목 조건 강화 ────────────────────────
    # "하락 후 반등 전환점" 포착 — 기존은 단순 EMA 근처였음

    # 1. 직전 2봉 이상 하락했어야 함 (눌렸다는 증거)
    if len(closes) >= 4:
        _prev3, _prev2, _prev1 = closes[-4], closes[-3], closes[-2]
        _pullback_confirmed = (_prev2 < _prev3) or (_prev1 < _prev2)  # 최근 2봉 중 하나라도 하락
        if not _pullback_confirmed:
            ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_NO_DIP",
                f"직전 눌림 없음 — 연속 상승 중 ({_prev3:.0f}→{_prev2:.0f}→{_prev1:.0f})")
            return None

    # 2. 현재봉이 반등 시작 (상승 전환 확인)
    if len(closes) >= 2 and closes[-1] <= closes[-2]:
        ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_NO_BOUNCE",
            f"반등 미확인 — 현재봉 하락 중 ({closes[-2]:.0f}→{closes[-1]:.0f})")
        return None

    # 3. EMA20 이격 범위 확인 (-3% ~ +2.0%)
    # [확대] 하한 0.2%→-3.0%: 눌림이 EMA20 아래까지 내려왔다가 반등하는 경우도 포착
    dist = (snap.current_price - ema20) / ema20 * 100
    _dist_min = float(getattr(cfg, "pullback_dist_min_pct", -3.0))
    _dist_max = float(getattr(cfg, "pullback_dist_max_pct",  2.0))
    if not (_dist_min <= dist <= _dist_max):
        ScannerLogger.rejected(snap.code, snap.name, "PULLBACK",
            f"EMA20 이격 범위 벗어남 ({dist:.2f}%, 허용: {_dist_min:.1f}%~{_dist_max:.1f}%)")
        return None

    # 4. RSI 범위 — 완화: 45~72 (눌림 구간은 RSI가 낮아졌다가 반등)
    _rsi_min = float(getattr(cfg, "pullback_rsi_min", 45.0))
    _rsi_max = float(getattr(cfg, "pullback_rsi_max", 72.0))
    if not (_rsi_min <= rsi <= _rsi_max):
        ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_RSI",
            f"RSI 범위 벗어남 ({rsi:.1f}, 허용: {_rsi_min:.0f}~{_rsi_max:.0f})")
        return None

    # 5. 반등 에너지 확인: 현재봉 거래량 > 눌림 구간(직전 2봉) 평균
    vols = snap.volumes_1min
    if len(vols) >= 4:
        _dip_avg_vol = (vols[-3] + vols[-2]) / 2  # 눌리는 동안 거래량
        _bounce_vol  = vols[-1]                    # 반등 봉 거래량
        _energy_mult = float(getattr(cfg, "pullback_bounce_energy", 1.2))
        if _dip_avg_vol > 0 and _bounce_vol < _dip_avg_vol * _energy_mult:
            ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_ENERGY",
                f"반등 에너지 부족 — 반등봉 {_bounce_vol:,}주 < 눌림평균 {_dip_avg_vol:,.0f}주 × {_energy_mult}")
            return None

    # 4. VWAP 필터 — 활성화 (2026-05-13: 거짓 신호 필터링)
    r_vwap = check_vwap_filter(snap)
    if not r_vwap:
        return None

    # [2026-06-02] A전략: MTF 추세 일치 확인
    # 5분봉도 상승 방향이어야 진입 허용 — 1분봉 반등인데 5분봉 하락이면 차단
    if getattr(cfg, "mtf_enabled", True) and getattr(cfg, "pullback_mtf_check", True):
        mtf_bars = int(getattr(snap, "mtf_tf5_bars", 0))
        min_bars = int(getattr(cfg, "mtf_min_5min_bars", 3))
        if mtf_bars >= min_bars and not getattr(snap, "mtf_aligned", True):
            ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_MTF",
                f"5분봉 추세 불일치 — "
                f"1분EMA={getattr(snap,'mtf_tf1_slope',0):+.1f} "
                f"5분EMA={getattr(snap,'mtf_tf5_slope',0):+.1f}")
            return None

    # [2026-06-02] D전략: 호가 압력 확인
    # 매수2~3호가 물량이 매도2~3호가보다 두꺼워야 지지선 신뢰
    if getattr(cfg, "hoga_pressure_enabled", True) and getattr(snap, "hoga_ready", False):
        pressure = snap.hoga_pressure
        min_pressure = float(getattr(cfg, "hoga_pressure_min", 1.3))
        if pressure < min_pressure:
            ScannerLogger.rejected(snap.code, snap.name, "PULLBACK_HOGA",
                f"호가 압력 부족 — 압력비={pressure:.2f} < {min_pressure:.1f} "
                f"(매수2~3호가 {sum(snap.bid_qtys[1:3]):,}주 vs 매도 {sum(snap.ask_qtys[1:3]):,}주)")
            return None

    pressure_str = f" | 호가압력={snap.hoga_pressure:.2f}" if getattr(snap, "hoga_ready", False) else ""
    reason = f"[PULLBACK] EMA20지지({dist:.2f}%) | RSI {rsi:.1f} | 추세Lv{tlv} | {r_vwap}{pressure_str}"
    ScannerLogger.passed(snap.code, snap.name, "PULLBACK", reason)
    return reason
