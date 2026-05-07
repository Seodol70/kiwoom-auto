from __future__ import annotations
import logging
from typing import Optional, Tuple
from datetime import datetime, time as dtime

from scanner.models import StockSnapshot
from scanner.config import SmartScannerConfig
from scanner.scanner_logger import ScannerLogger
from scanner.indicator_service import IndicatorService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 신호 판단 함수 (순수 함수)
# ---------------------------------------------------------------------------


def check_breakout(
    snap:                    StockSnapshot,
    breakout_ratio:          float = 0.03,  # 2026-04-03 재강화: 1% → 3% (가짜 돌파 방지)
    volume_mult:             float = 1.0,   # 2026-04-03: 1.5 → 1.0 (거래량 완화)
    pullback_from_high_pct:  float = 1.5,   # 당일 고점 대비 N% 이상 하락 시 차단 (0=비활성)
    min_rising_bars:         int   = 2,     # 최근 N개 1분봉 연속 상승 요구 (0=비활성)
) -> Optional[str]:
    if snap.prev_close <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT", "prev_close=0")
        return None


    threshold = snap.prev_close * (1 + breakout_ratio)


    if snap.current_price < threshold:
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"현재가 {snap.current_price:,} < 돌파기준 {threshold:,.0f}",
        )
        return None


    # [RELAXED] 신고가 갱신 requirement 제거 (조건문 2026-04-03)
    # 이유: 당일 11:00 이전 시점에 신고가 도달은 극히 드문 사건.
    #       대신 전일 종가 돌파만으로 신호 판정 — 더 자주 거래 기회 제공
    # (과거) if snap.current_price < snap.high_price: return None


    avg_vol = snap.trade_amount / snap.current_price if snap.current_price else 0
    # 거래대금이 충분하면 거래량 체크, 부족하면 통과 (선택적 필터)
    if snap.trade_amount > 0 and (avg_vol <= 0 or snap.volume < avg_vol * volume_mult):
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"거래량 부족 ({snap.volume:,} < 기준 {avg_vol * volume_mult:,.0f})",
        )
        return None


    # ── ① 당일 고점 대비 하락폭 차단 ─────────────────────────────────────
    # 현재가가 당일 고점에서 pullback_from_high_pct% 이상 내려와 있으면 하락 추세로 판단
    if pullback_from_high_pct > 0 and snap.high_price > 0:
        pullback = (snap.current_price - snap.high_price) / snap.high_price * 100
        if pullback <= -pullback_from_high_pct:
            ScannerLogger.rejected(
                snap.code, snap.name, "BREAKOUT",
                f"고점({snap.high_price:,}) 대비 {pullback:.2f}% 하락 중 "
                f"(차단기준 -{pullback_from_high_pct:.1f}%) — 하락추세",
            )
            return None


    # ── ② 1분봉 연속 상승 확인 ───────────────────────────────────────────
    # 최근 min_rising_bars개 봉이 모두 직전 봉 대비 상승이어야 통과
    closes = snap.closes_1min
    if min_rising_bars > 0 and len(closes) >= min_rising_bars + 1:
        rising = all(
            closes[-(i + 1)] > closes[-(i + 2)]
            for i in range(min_rising_bars)
        )
        if not rising:
            recent = [int(closes[-(i + 1)]) for i in range(min(min_rising_bars + 1, len(closes)))]
            recent_str = " → ".join(f"{p:,}" for p in reversed(recent))
            ScannerLogger.rejected(
                snap.code, snap.name, "BREAKOUT",
                f"1분봉 연속상승 {min_rising_bars}개 미충족 ({recent_str}) — 하락/횡보",
            )
            return None


    # ✅ 모든 조건 통과
    reason = (
        f"전일종가 {snap.prev_close:,} 대비 {breakout_ratio*100:.1f}% 돌파 "
        f"| 현재가 {snap.current_price:,}"
    )
    ScannerLogger.passed(snap.code, snap.name, "BREAKOUT", reason)
    return reason




def check_testa_alignment(
    snap: StockSnapshot,
    max_ma_spread: float = 0.05,   # MA10-MA50 이격도 상한 (5%) — 과열 설거지 방지
) -> Optional[str]:
    """
    테스타 정배열 확인: MA10 > MA20 > MA50 + 이격도 과열 필터.


    조건:
      ① MA10 > MA20 > MA50   (정배열)
      ② (MA10 - MA50) / MA50 ≤ max_ma_spread   (이격 과열 차단)
         → MA10 이 MA50 보다 5% 이상 높으면 이미 급등 종료 구간 (설거지 위험)


    1분봉 종가 50개 이상 필요.
    """
    closes = snap.closes_1min
    if len(closes) < 50:
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"1분봉 데이터 부족 ({len(closes)}/50)",
        )
        return None


    ma10 = IndicatorService.calc_ma(closes, 10)
    ma20 = IndicatorService.calc_ma(closes, 20)
    ma50 = IndicatorService.calc_ma(closes, 50)


    if any(v is None for v in [ma10, ma20, ma50]):
        ScannerLogger.rejected(snap.code, snap.name, "TESTA", "MA 계산 실패")
        return None


    if not (ma10 > ma20 > ma50):
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"정배열 미충족 MA10={ma10:.0f} MA20={ma20:.0f} MA50={ma50:.0f}",
        )
        return None


    # 이격도 과열 체크 — (MA10 - MA50) / MA50 > max_ma_spread 이면 탈락
    spread = (ma10 - ma50) / ma50 if ma50 > 0 else 0.0
    if spread > max_ma_spread:
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"MA 이격 과열 {spread:.1%} > {max_ma_spread:.0%} "
            f"(MA10={ma10:.0f} MA50={ma50:.0f}) — 설거지 위험",
        )
        return None


    reason = (
        f"정배열 MA10={ma10:.0f} > MA20={ma20:.0f} > MA50={ma50:.0f} "
        f"이격={spread:.1%}"
    )
    ScannerLogger.passed(snap.code, snap.name, "TESTA", reason)
    return reason




def check_jdm_open_breakout(
    snap: StockSnapshot,
    cfg: SmartScannerConfig,
    min_body_ratio: float = 0.7,   # 양봉 몸통 비율 하한 — 윗꼬리 가짜 돌파 차단
) -> Optional[str]:
    """
    장동민 개선형: OR 3조건 + 양봉 몸통 비율 필터.


    조건 0 (기존): current_price > open_price  (시가 돌파)
    조건 A (V자반등): current_price > prev_close AND current_price >= open_price * prev_close_min_ratio
                    → 어제 가격을 돌파하며 V자 반등, 시가 대비 -2% 이내 제한
    조건 B (VI직전): current_price >= high_price AND change_pct >= vi_approach_chg_pct
                   → 이미 1차 상승 후 고점 재돌파, VI 달려가는 주도주


    세 조건 중 하나라도 통과하면, 양봉 몸통 비율 필터까지 체크 후 통과.
    """
    if snap.open_price <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_OPEN", "시가/현재가 0")
        return None


    # OR 3조건 검사
    cond0 = snap.current_price > snap.open_price
    cond_a = (snap.current_price > snap.prev_close and
              snap.current_price >= snap.open_price * cfg.prev_close_min_ratio)
    cond_b = (snap.current_price >= snap.high_price and
              snap.change_pct >= cfg.vi_approach_chg_pct)


    condition_met = False
    condition_reason = ""


    if cond0:
        condition_met = True
        condition_reason = "시가돌파"
    elif cond_a:
        condition_met = True
        condition_reason = "V자반등"
    elif cond_b:
        condition_met = True
        condition_reason = "VI직전"


    if not condition_met:
        detail = (
            f"3조건 불만족: "
            f"시가돌파({cond0}) V자반등({cond_a}) VI직전({cond_b}) "
            f"현재={snap.current_price:,} 시가={snap.open_price:,} "
            f"전일={snap.prev_close:,} 고가={snap.high_price:,} 등락={snap.change_pct:.1f}%"
        )
        ScannerLogger.rejected(snap.code, snap.name, "JDM_OPEN", detail)
        return None


    # 양봉 몸통 비율 체크
    candle_range = snap.high_price - snap.low_price
    if candle_range > 0:
        body_ratio = (snap.current_price - snap.open_price) / candle_range
        if body_ratio < min_body_ratio:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_OPEN",
                f"{condition_reason} 통과했으나 몸통 비율 부족 {body_ratio:.0%} < {min_body_ratio:.0%}",
            )
            return None


    breakout_pct = (snap.current_price - snap.open_price) / snap.open_price * 100
    body_ratio_str = (
        f" 몸통={((snap.current_price - snap.open_price) / candle_range):.0%}"
        if candle_range > 0 else ""
    )
    reason = (
        f"{condition_reason} 현재가={snap.current_price:,} > "
        f"시가={snap.open_price:,}(+{breakout_pct:.2f}%){body_ratio_str}"
    )
    ScannerLogger.passed(snap.code, snap.name, "JDM_OPEN", reason)
    return reason




# [NEW] 신규 필터 함수 3개 — JDM 신호 품질 강화 (4중 필터)


def check_volume_surge(
    snap: StockSnapshot,
    surge_mult: float = 1.5,
    lookback: int = 10,
) -> Optional[str]:
    """
    [개선] 직전 N분 평균 거래량 대비 surge_mult 배 이상인지 확인.


    기존: 직전 5분 평균 대비 1.5배
    개선: 직전 lookback분(기본 10분) 평균 대비 surge_mult배(기본 5.0배)
    → 더 강력한 수급 확인 (가짜 신호 필터링 강화)
    """
    vols = snap.volumes_1min
    # 데이터 부족: lookback+1개 필요 (현재 1분 + 과거 lookback분)
    if len(vols) < lookback + 1:
        return None


    # 직전 lookback분 평균
    avg_lookback = sum(vols[-(lookback+1):-1]) / lookback
    if avg_lookback <= 0:
        return None


    cur = vols[-1]
    if cur < avg_lookback * surge_mult:
        ScannerLogger.rejected(
            snap.code, snap.name, "VOL_SURGE",
            f"거래량 {cur:,} / {lookback}분평균 {avg_lookback:,.0f} ({cur/avg_lookback:.1f}배 < {surge_mult}배)"
        )
        return None


    return f"거래량급증{cur:,}주({cur/avg_lookback:.1f}배)"




def check_chejan_strength(
    snap: StockSnapshot,
    min_strength: float = 110.0,
) -> Optional[str]:
    """[NEW] 체결강도 min_strength% 이상 확인 (매수 수급 우위)."""
    if snap.chejan_strength < min_strength:
        ScannerLogger.rejected(snap.code, snap.name, "CHEJAN",
                               f"체결강도 {snap.chejan_strength:.0f}% < {min_strength:.0f}%")
        return None
    return f"체결강도{snap.chejan_strength:.0f}%"




def check_disparity_from_ma(
    snap: StockSnapshot,
    ma_period: int   = 20,
    max_pct: float   = 5.0,
) -> Optional[str]:
    """[NEW] 1분봉 MA(ma_period) 대비 이격도 max_pct% 이내 확인 (과열 차단)."""
    closes = snap.closes_1min
    if len(closes) < ma_period:
        return None   # 데이터 부족 시 bypass (초반 20분간 허용)
    ma = IndicatorService.calc_ma(closes, ma_period)
    if ma is None or ma <= 0:
        return None
    disp = (snap.current_price - ma) / ma * 100
    if disp > max_pct:
        ScannerLogger.rejected(snap.code, snap.name, "DISPARITY",
                               f"MA{ma_period} 이격도 {disp:.1f}% > {max_pct:.1f}%")
        return None
    return f"MA{ma_period}이격{disp:.1f}%"




def check_ema20_filter(snap: StockSnapshot, period: int = 20) -> Optional[str]:
    """
    EMA20 추세 필터 — 현재가가 20분 EMA 위에 있어야 진입 허용.


    완성된 1분봉 closes_1min 기준으로 EMA20 계산.
    현재가 > EMA20 이면 상승 추세로 판단, 통과.
    """
    closes = snap.closes_1min
    if len(closes) < period:
        ScannerLogger.rejected(snap.code, snap.name, "EMA20",
                               f"데이터 부족 ({len(closes)}/{period})")
        return None
    ema20 = IndicatorService.calc_ema(closes, period)
    if ema20 is None:
        return None
    if snap.current_price <= ema20:
        ScannerLogger.rejected(
            snap.code, snap.name, "EMA20",
            f"현재가 {snap.current_price:,} ≤ EMA20 {ema20:,.0f} — 하락 추세",
        )
        return None
    return f"EMA20상단(현재가={snap.current_price:,}/EMA20={ema20:,.0f})"


def check_vwap_filter(snap: StockSnapshot) -> Optional[str]:
    """VWAP 필터 — 현재가가 당일 VWAP 위에 있어야 진입 허용."""
    # [NEW] 우선순위: snap.vwap (당일 누적 데이터 기반 True VWAP)
    # 이 방식은 장중 재시작 시에도 100% 정확한 VWAP을 보장함.
    vwap = snap.vwap
    
    # 누적 데이터가 없으면 분봉 기반으로 fallback (최초 데이터 수신 전 대비)
    if vwap is None:
        closes = snap.closes_1min
        vols = snap.volumes_1min
        if closes and vols and len(closes) == len(vols):
            vwap = IndicatorService.calc_vwap(np.array(closes), np.array(vols))
            
    if vwap is None:
        return None
        
    # [LEARNING_MODE] 공격적 데이터 수집을 위해 VWAP 아래 0.5%까지는 허용
    vwap_margin = 0.995 
    if snap.current_price < vwap * vwap_margin:
        ScannerLogger.rejected(
            snap.code, snap.name, "VWAP",
            f"현재가 {snap.current_price:,} < VWAP {vwap:,.0f} (허용범위 미달) — 평균단가 하방",
        )
        return None
        
    return f"VWAP상단({snap.current_price:,}/{vwap:,.0f})"




def check_bullish_engulfing(snap: StockSnapshot) -> Optional[str]:
    """
    상승 장악형(Bullish Engulfing) 완성 여부 확인.


    완성된 마지막 두 1분봉 기준:
      ① 직전 봉이 음봉 (open > close)
      ② 현재 봉 시가 ≤ 직전 봉 종가 (갭다운 or 동가 출발)
      ③ 현재 봉 종가 > 직전 봉 시가 (완전 장악)


    Returns:
        패턴 설명 문자열 or None
    """
    c = snap.closes_1min
    o = snap.opens_1min
    if len(c) < 2 or len(o) < 2:
        return None
    prev_o, prev_c = o[-2], c[-2]
    curr_o, curr_c = o[-1], c[-1]
    if prev_c >= prev_o:          # 직전 봉이 양봉이면 패턴 불성립
        return None
    if curr_o <= prev_c and curr_c > prev_o:
        return f"상승장악형(직전음봉:{prev_o:.0f}→{prev_c:.0f} / 현재:{curr_o:.0f}→{curr_c:.0f})"
    return None




def check_bullish_pin_bar(snap: StockSnapshot, min_tail_ratio: float = 0.55) -> Optional[str]:
    """
    강세 핀바(Bullish Pin Bar) 완성 여부 확인.


    완성된 마지막 1분봉 기준:
      ① 하단 꼬리 길이 ≥ 전체 범위의 min_tail_ratio (기본 55%)
      ② 종가 ≥ 봉 중간값 ((고가 + 저가) / 2) — 회복 확인


    Returns:
        패턴 설명 문자열 or None
    """
    c = snap.closes_1min
    h = snap.highs_1min
    l = snap.lows_1min
    o = snap.opens_1min
    if len(c) < 1 or len(h) < 1 or len(l) < 1 or len(o) < 1:
        return None
    curr_c, curr_h, curr_l, curr_o = c[-1], h[-1], l[-1], o[-1]
    total_range = curr_h - curr_l
    if total_range <= 0:
        return None
    body_low    = min(curr_o, curr_c)
    lower_tail  = body_low - curr_l
    mid_price   = (curr_h + curr_l) / 2
    tail_ratio  = lower_tail / total_range
    if tail_ratio >= min_tail_ratio and curr_c >= mid_price:
        return f"강세핀바(하꼬리{tail_ratio*100:.0f}%,저가:{curr_l:.0f})"
    return None




def check_breakout_gate(snap: "StockSnapshot", cfg: SmartScannerConfig) -> Optional[str]:
    """
    BREAKOUT 확인 후 진입 가능 여부를 검증하는 공통 게이트.


    check_jdm_entry 와 동일한 시장 안전 필터를 BREAKOUT 경로에도 적용한다.
      ① 지수 등락률 차단 (index_block_pct)
      ② 진입 허용 시각
      ③ 시간대 슬롯 기반 등락률 상한 (max_change_pct_*)
      ④ 시간대 슬롯 기반 체결강도 하한 (공포 장세 상향 포함)
      ⑤ 손절 블랙리스트는 handle_signal() 에서 처리하므로 여기선 생략


    Returns:
        None   → 진입 거부 (ScannerLogger 에 이유 기록됨)
        reason → 거부 없음 (추가 필터 통과 이유 문자열)
    """
    # ① 진입 허용 시각
    now = datetime.now().time()
    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_TIME",
            f"진입 허용 시간 아님 ({cfg.entry_start_time}~{cfg.entry_end_time})")
        return None


    # ③ 시간대 슬롯 기반 등락률 상한
    _slot       = _resolve_time_slot(now, cfg)
    _eff_ch_max = _get_slot_value(_slot, cfg, "max_change_pct", cfg.max_change_pct)
    _snap_chg   = float(getattr(snap, "change_pct", 0) or 0)
    if _snap_chg >= _eff_ch_max:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_CHGPCT",
            f"[{_slot}] 등락률 {_snap_chg:.2f}% ≥ 구간 상한 {_eff_ch_max:.0f}%")
        return None


    # ④ 시간대 슬롯 기반 체결강도
    _eff_chejan = _get_slot_value(_slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
    if snap.chejan_strength < _eff_chejan:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN",
            actual=snap.chejan_strength, threshold=_eff_chejan,
            reason=f"[{_slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {_eff_chejan:.0f}%",
        )
        return None


    # ⑤ 체결강도 상한 — 극과열 고점 차단
    # MORNING 슬롯: 갭업 후 체결강도 정상화 중인 종목 허용 (950%), 나머지 슬롯: 800%
    if _slot == "MORNING":
        _chejan_max = getattr(cfg, "breakout_chejan_max_morning", 950.0)
    else:
        _chejan_max = getattr(cfg, "breakout_chejan_max", 800.0)
    if snap.chejan_strength >= _chejan_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN_MAX",
            actual=snap.chejan_strength, threshold=_chejan_max,
            reason=f"[{_slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {_chejan_max:.0f}%",
        )
        return None


    # ⑥ RSI 상한 — 과매수 고점 차단 (snap.rsi > 0 인 경우만 적용)
    _rsi_max = getattr(cfg, "breakout_rsi_max", 80.0)
    if snap.rsi > 0 and snap.rsi >= _rsi_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_RSI_MAX",
            actual=snap.rsi, threshold=_rsi_max,
            reason=f"[{_slot}] RSI 과매수 차단 — {snap.rsi:.1f} ≥ {_rsi_max:.1f}",
        )
        return None

    # ⑦ VWAP 필터 — 평균단가 상단 확인
    r_vwap = check_vwap_filter(snap)
    if r_vwap is None:
        return None

    return f"[{_slot}] 체결강도 {snap.chejan_strength:.0f}% | 등락률 {_snap_chg:.1f}% | {r_vwap}"




def _resolve_time_slot(now: "dtime", cfg: SmartScannerConfig) -> str:
    """
    현재 시각을 기준으로 매매 시간 슬롯 문자열을 반환한다.


    Returns:
        "PRE"       — 08:00 ~ 09:00 (시간외 단일가, 캔들 없음)
        "OPENING"   — 09:00 ~ 09:30 (장 초반, MA정배열 미확인 구간)
        "MORNING"   — 09:30 ~ 11:00 (핵심 오전, 표준 기준)
        "MIDDAY"    — 11:00 ~ 13:00 (점심, 중간 강화)
        "AFTERNOON" — 13:00 ~ 14:30 (오후, 고점 차단)
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




def _get_slot_value(slot: str, cfg: SmartScannerConfig, param_base: str, fallback: float) -> float:
    """
    슬롯과 파라미터 기본명으로 구간별 값을 반환한다.


    예) param_base="max_change_pct", slot="AFTERNOON"
        → cfg.max_change_pct_afternoon (없으면 fallback)
    """
    return float(getattr(cfg, f"{param_base}_{slot.lower()}", fallback))




def check_pre_surge(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    PRE_SURGE — 08:00~09:00 시간외 단일가 구간.


    캔들 데이터 없이 등락률·체결강도·거래량만으로 판단한다.
    주문은 09:00 단일가 일괄체결됨을 유의.


    통과 조건:
      ① 지수 차단 없음 (index_block_pct 초과)
      ② pre_surge_chg_min ≤ 등락률 < pre_surge_chg_max
      ③ 체결강도 ≥ pre_surge_chejan_min
      ④ 거래량 > 0
    """
    chg     = float(snap.change_pct or 0)
    chg_min = getattr(cfg, "pre_surge_chg_min",  2.0)
    chg_max = getattr(cfg, "pre_surge_chg_max", 20.0)
    if not (chg_min <= chg < chg_max):
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE",
            f"등락률 범위 미충족 — {chg:+.2f}% (기준 {chg_min:.1f}%~{chg_max:.1f}%)")
        return None


    chejan_min = getattr(cfg, "pre_surge_chejan_min", 110.0)
    if snap.chejan_strength < chejan_min:
        ScannerLogger.near_miss(
            snap.code, snap.name, "PRE_SURGE",
            actual=snap.chejan_strength, threshold=chejan_min,
            reason=f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%",
        )
        return None


    # ⑤ 체결강도 상한 — 이미 극단 과열(고점) 종목 차단
    chejan_max = getattr(cfg, "pre_surge_chejan_max", 700.0)
    if snap.chejan_strength >= chejan_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "PRE_SURGE",
            actual=snap.chejan_strength, threshold=chejan_max,
            reason=f"체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {chejan_max:.0f}%",
        )
        return None


    # ⑥ RSI 상한 — 과매수 구간(고점) 진입 차단 (RSI=0 은 미계산이므로 스킵)
    rsi_max = getattr(cfg, "pre_surge_rsi_max", 88.0)
    if snap.rsi > 0 and snap.rsi >= rsi_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "PRE_SURGE",
            actual=snap.rsi, threshold=rsi_max,
            reason=f"RSI 과매수 차단 — {snap.rsi:.1f} ≥ {rsi_max:.1f}",
        )
        return None


    if snap.volume <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE", "거래량 없음")
        return None


    return (
        f"PRE_SURGE 시간외 등락 {chg:+.2f}% "
        f"/ 체결강도 {snap.chejan_strength:.0f}% "
        f"/ 거래량 {snap.volume:,}"
    )




def check_opening_surge(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    OPENING_SURGE — 09:00~09:16 정규장 초반 (1분봉 < 8개).


    MA/RSI 데이터 부족 구간에서 등락률·체결강도·거래량으로 빠르게 판단한다.
    entry_open_surge_max_opening(기본 7%)으로 고점 진입 방지.


    통과 조건:
      ① 지수 차단 없음
      ② 시가 대비 상승 < entry_open_surge_max_opening
      ③ opening_surge_chg_min ≤ 등락률 < max_change_pct_opening
      ④ 체결강도 ≥ opening_surge_chejan_min
      ⑤ 최근 1분 거래량 ≥ 직전 평균 × opening_surge_vol_mult (데이터 있을 때만)
    """
    # 시가 대비 상승 상한 (OPENING 전용 완화값)
    surge_max = getattr(cfg, "entry_open_surge_max_opening",
                        getattr(cfg, "entry_open_surge_max", 7.0))
    if snap.open_price > 0:
        surge_from_open = (snap.current_price - snap.open_price) / snap.open_price * 100
        if surge_from_open >= surge_max:
            ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
                f"시가 대비 이미 {surge_from_open:.2f}% 상승 ≥ 상한 {surge_max:.1f}%")
            return None


    chg     = float(snap.change_pct or 0)
    chg_min = getattr(cfg, "opening_surge_chg_min", 1.0)
    chg_max = getattr(cfg, "max_change_pct_opening", getattr(cfg, "max_change_pct", 20.0))
    if not (chg_min <= chg < chg_max):
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"등락률 범위 미충족 — {chg:+.2f}% (기준 {chg_min:.1f}%~{chg_max:.1f}%)")
        return None


    chejan_min = getattr(cfg, "opening_surge_chejan_min", 120.0)
    if snap.chejan_strength < chejan_min:
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%")
        return None


    # 거래량 급증 체크 (분봉 데이터 2개 이상일 때만)
    vol_mult = getattr(cfg, "opening_surge_vol_mult", 1.2)
    vols = list(snap.volumes_1min) if snap.volumes_1min else []
    if len(vols) >= 2:
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
        if avg_vol > 0 and vols[-1] < avg_vol * vol_mult:
            ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
                f"거래량 미달 — {vols[-1]:,} < 평균 {avg_vol:,.0f} × {vol_mult:.1f}")
            return None


    return (
        f"OPENING_SURGE 등락 {chg:+.2f}% "
        f"/ 체결강도 {snap.chejan_strength:.0f}% "
        f"/ 거래량 {snap.volume:,}"
    )




def check_opening_scalp(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    Phase 1 모닝 스캘핑 진입 신호 (09:00~09:30).


    PRE_SURGE 신호가 발생한 종목을 장 시작 후 추적 매수한다.
    MA 데이터가 충분하지 않은 구간이므로 조건을 단순화한다.


    진입 조건:
      1. 1분봉 ≥ phase1_min_candles (기본 3개, ≈09:03 이후)
      2. 현재가 ≥ 시가 (갭업 방향 유지)
      3. 시가 대비 상승 ≤ phase1_open_rise_max (기본 8%, 이미 너무 오른 종목 차단)
      4. 체결강도 phase1_chejan_min ~ phase1_chejan_max 범위
      5. 전일 대비 등락률 ≤ phase1_change_pct_max (기본 15%)
    """
    # ① 1분봉 최소 개수 — 데이터 안정화 대기
    min_candles = int(getattr(cfg, "phase1_min_candles", 3))
    if len(snap.closes_1min) < min_candles:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CANDLES",
            f"1분봉 {len(snap.closes_1min)}개 < 최소 {min_candles}개 — 대기 중")
        return None


    # ② 시가 방향 확인 (현재가 ≥ 시가)
    if snap.open_price > 0 and snap.current_price < snap.open_price:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_DIRECTION",
            f"시가 하방 — 현재가 {snap.current_price:,} < 시가 {snap.open_price:,}")
        return None


    # ③ 시가 대비 상승 상한
    open_rise_max = float(getattr(cfg, "phase1_open_rise_max", 8.0))
    if snap.open_price > 0:
        open_rise = (snap.current_price - snap.open_price) / snap.open_price * 100
        if open_rise > open_rise_max:
            ScannerLogger.rejected(snap.code, snap.name, "SCALP_OPEN_RISE",
                f"시가 대비 {open_rise:.1f}% 상승 > 상한 {open_rise_max:.1f}% — 고점 차단")
            return None
    else:
        open_rise = 0.0


    # ④ 체결강도 범위
    chejan_min = float(getattr(cfg, "phase1_chejan_min", 120.0))
    chejan_max = float(getattr(cfg, "phase1_chejan_max", 700.0))
    if snap.chejan_strength < chejan_min:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHEJAN",
            f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%")
        return None
    if snap.chejan_strength >= chejan_max:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHEJAN",
            f"체결강도 과열 — {snap.chejan_strength:.0f}% ≥ {chejan_max:.0f}%")
        return None


    # ⑤ 전일 대비 등락률 상한
    chg_max = float(getattr(cfg, "phase1_change_pct_max", 15.0))
    if snap.change_pct > chg_max:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHANGE",
            f"등락률 {snap.change_pct:.1f}% > 상한 {chg_max:.1f}%")
        return None


    reason = (
        f"[SCALP] PRE_SURGE 추적 진입 — 시가 대비 +{open_rise:.1f}%"
        f" | 체결강도 {snap.chejan_strength:.0f}%"
        f" | 등락률 {snap.change_pct:+.1f}%"
        f" | 1분봉 {len(snap.closes_1min)}개"
    )
    ScannerLogger.passed(snap.code, snap.name, "OPENING_SCALP", reason)
    return reason




def check_eod_entry(
    snap: "StockSnapshot",
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    종가매매(EOD) 진입 신호 판단.


    진입 조건:
      1. overnight_mode_enabled = True
      2. 현재 시각이 eod_entry_start ~ eod_entry_end (기본 14:40~14:55)
      3. 일봉 20MA 상방 (현재가 ≥ daily_ma20)
      4. 25일 신고가 근처 (current_price ≥ high_25d × (1 - threshold%))
      5. 일봉 정배열 (MA5 > MA10 > MA20)
      6. 당일 등락률 eod_change_pct_min ~ eod_change_pct_max (기본 +2% ~ +10%)
      7. 체결강도 ≥ eod_strength_min (기본 115%)
      8. 거래량 ≥ 전일 평균 × eod_volume_ratio_min (기본 1.5배)


    Returns:
        신호 이유 문자열 (통과) 또는 None (차단)
    """
    if not getattr(cfg, "overnight_mode_enabled", False):
        return None


    now = datetime.now().time()
    _start = getattr(cfg, "eod_entry_start", dtime(14, 40, 0))
    _end   = getattr(cfg, "eod_entry_end",   dtime(14, 55, 0))
    if not (_start <= now < _end):
        return None


    # ① 일봉 20MA 상방 + 신고가 근처
    _near_thr = float(getattr(cfg, "eod_near_high_threshold_pct", 3.0))
    _dctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price, _near_thr)


    if not _dctx["above_ma20"] and _dctx["daily_ma20"] > 0:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_MA20",
            f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {_dctx['daily_ma20']:,.0f}",
        )
        return None


    if not _dctx["near_high"]:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_NEAR_HIGH",
            f"25일 신고가 근처 아님 — 현재가 {snap.current_price:,}, "
            f"25일고가 {_dctx['high_25d']:,.0f} (기준 -{_near_thr:.1f}%)",
        )
        return None


    # ② 일봉 정배열
    _align = IndicatorService.check_daily_alignment(snap.daily_closes, snap.current_price)
    if not _align["is_aligned"]:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_ALIGN",
            f"일봉 정배열 미충족 (5MA > 10MA > 20MA)",
        )
        return None


    # ②-b 분봉 추세 강도 (Medium 이상 — 종가 직전까지 추세 유지 확인)
    _eod_min_trend = int(getattr(cfg, "eod_min_trend_level", 2))
    _trend_lv = int(getattr(snap, "trend_level", 0))
    if _trend_lv < _eod_min_trend:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_TREND",
            f"분봉 추세 미달 — level {_trend_lv} < {_eod_min_trend} (Medium 이상 필요)",
        )
        return None


    # ③ 당일 등락률
    _chg_min = float(getattr(cfg, "eod_change_pct_min", 2.0))
    _chg_max = float(getattr(cfg, "eod_change_pct_max", 10.0))
    chg = snap.change_pct
    if not (_chg_min <= chg <= _chg_max):
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_CHANGE",
            f"등락률 {chg:+.2f}% 범위 밖 (기준 +{_chg_min:.1f}% ~ +{_chg_max:.1f}%)",
        )
        return None


    # ④ 체결강도
    _str_min = float(getattr(cfg, "eod_strength_min", 115.0))
    if snap.chejan_strength < _str_min:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_STRENGTH",
            f"체결강도 {snap.chejan_strength:.1f}% < 기준 {_str_min:.0f}%",
        )
        return None


    # ⑤ 거래량 (당일 1분봉 평균 대비 배수 — 최근 10분 기준)
    _vol_ratio = float(getattr(cfg, "eod_volume_ratio_min", 1.5))
    _vols = snap.volumes_1min
    if _vols and len(_vols) >= 10:
        _avg_vol_1min = sum(_vols[-10:]) / 10.0
        _cur_vol_1min = _vols[-1] if _vols else 0
        if _avg_vol_1min > 0 and _cur_vol_1min < _avg_vol_1min * _vol_ratio:
            ScannerLogger.rejected(
                snap.code, snap.name, "EOD_VOLUME",
                f"최근 1분봉 거래량 {_cur_vol_1min:,} < 10분평균 {_avg_vol_1min:,.0f} × {_vol_ratio:.1f}배",
            )
            return None


    reason = (
        f"[EOD] 종가매매 진입 — 등락률 {chg:+.2f}% | 체결강도 {snap.chejan_strength:.1f}% "
        f"| 25일신고가 {_dctx['high_25d']:,.0f}원 근처 | 일봉정배열↑ "
        f"| 20MA {_dctx['daily_ma20']:,.0f}원 상방"
    )
    ScannerLogger.passed(snap.code, snap.name, "EOD_ENTRY", reason)
    return reason




def check_indicator_warmup(snap: StockSnapshot, min_candles: int = 15) -> Optional[str]:
    """[NEW] 지표 워밍업 체크 — 캔들 개수가 부족한 장 초반 신뢰도 낮은 지표 차단."""
    count = len(snap.closes_1min)
    if count < min_candles:
        # 데이터가 아예 없으면 차단, 약간(10~14개) 있으면 로그 기록 후 진행 여부 결정
        if count < 10:
            return f"WARMUP_LACK({count})"
    return None

# ---------------------------------------------------------------------------
# check_jdm_entry 내부 컨텍스트 (서브 함수 간 공유 파라미터)
# ---------------------------------------------------------------------------
from dataclasses import dataclass, field as dc_field

@dataclass
class _JdmCtx:
    """check_jdm_entry 서브 함수들이 공유하는 계산된 파라미터."""
    now:           "dtime"
    slot:          str
    eff_chejan:    float
    eff_vol_mult:  float
    eff_rsi_min:   float
    eff_ma_spread: float
    scoring_bonus: bool
    trend_lv:      int
    candle_skip_lv: int
    lite_mode:     bool
    closes:        list
    highs:         list
    lows:          list
    is_warmup:     bool = False


def _jdm_build_ctx(snap: StockSnapshot, cfg: SmartScannerConfig) -> Optional["_JdmCtx"]:
    """슬롯·유효 파라미터 계산. 조기 차단 조건 해당 시 None 반환."""
    # ── 수급 절대치 필터
    if hasattr(cfg, 'min_daily_rank') and cfg.min_daily_rank:
        rank = snap.rank if hasattr(snap, 'rank') else None
        amt  = snap.trade_amount if hasattr(snap, 'trade_amount') else 0
        if not (rank is not None and rank > 0 and rank <= cfg.min_daily_rank) \
                and amt < cfg.min_trade_amount:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_LIQUIDITY",
                f"수급 부족",
            )
            return None

    # ── 시가 대비 상승도 차단
    if snap.open_price > 0:
        surge_from_open = (snap.current_price - snap.open_price) / snap.open_price * 100
        _surge_cap       = float(cfg.entry_open_surge_max)
        _surge_override  = int(getattr(cfg, "surge_trend_override_level", 2))
        _surge_trend_max = float(getattr(cfg, "surge_trend_max_pct", 15.0))
        _snap_trend_lvl  = int(getattr(snap, "trend_level", 0))
        if _surge_override > 0 and _snap_trend_lvl >= _surge_override:
            _surge_cap = max(_surge_cap, _surge_trend_max)
        if surge_from_open >= _surge_cap:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_SURGE",
                f"시가 대비 이미 상승 — 고점 진입 차단",
            )
            return None

    now = datetime.now().time()
    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_TIME",
            f"진입 허용 시간 아님",
        )
        return None

    # ── 슬롯 기반 유효 파라미터 산출
    slot          = _resolve_time_slot(now, cfg)
    if slot == "PRE":
        return None
    eff_ch_max    = _get_slot_value(slot, cfg, "max_change_pct",     cfg.max_change_pct)
    eff_chejan    = _get_slot_value(slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
    eff_vol_mult  = _get_slot_value(slot, cfg, "volume_surge_mult",   cfg.volume_1min_surge_mult)
    eff_rsi_min   = _get_slot_value(slot, cfg, "jdm_rsi_entry_min",   cfg.jdm_rsi_entry_min)
    trend_lv      = int(getattr(snap, "trend_level", 0))
    candle_skip_lv = int(getattr(cfg, "jdm_candle_skip_trend_level", 2))

    if trend_lv >= candle_skip_lv:
        rsi_trend_min = float(getattr(cfg, "jdm_rsi_entry_min_trend", 45.0))
        if rsi_trend_min < eff_rsi_min:
            eff_rsi_min = rsi_trend_min

    # ── 스코어링 보너스
    eff_ma_spread  = float(getattr(cfg, "jdm_ma_spread_pct", 0.15))
    scoring_bonus  = False
    rank_bonus     = int(getattr(cfg, "scoring_rank_bonus", 10))
    _rank          = snap.rank if hasattr(snap, 'rank') else None
    if _rank is not None and _rank > 0 and _rank <= rank_bonus:
        scoring_bonus = True
    surge_lookback = int(getattr(cfg, "volume_surge_lookback", 10))
    vol_bonus_mult = float(getattr(cfg, "scoring_vol_surge_bonus", 2.0))
    if snap.volumes_1min and len(snap.volumes_1min) >= surge_lookback + 1:
        avg_v = sum(snap.volumes_1min[-(surge_lookback+1):-1]) / surge_lookback
        cur_v = snap.volumes_1min[-1]
        if avg_v > 0 and (cur_v / avg_v) >= vol_bonus_mult:
            scoring_bonus = True
    if scoring_bonus:
        eff_rsi_min   = min(eff_rsi_min, 40.0)
        eff_ma_spread = min(eff_ma_spread, 0.10)

    # ── 등락률 상한 체크
    snap_chg        = float(getattr(snap, "change_pct", 0) or 0)
    chg_cap         = float(eff_ch_max)
    surge_override2 = int(getattr(cfg, "surge_trend_override_level", 2))
    surge_trend_max2 = float(getattr(cfg, "surge_trend_max_pct", 15.0))
    snap_trend_lv2  = int(getattr(snap, "trend_level", 0))
    if surge_override2 > 0 and snap_trend_lv2 >= surge_override2:
        chg_cap = max(chg_cap, surge_trend_max2)
    if snap_chg >= chg_cap:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_CHGPCT",
            f"[{slot}] 등락률 {snap_chg:.2f}% ≥ 구간 상한 {chg_cap:.0f}% (trend={snap_trend_lv2})",
        )
        return None

    if slot == "PRE":
        return None

    # ── 캔들 데이터 준비
    closes    = list(snap.closes_1min or [])
    highs     = list(snap.highs_1min  or [])
    lows      = list(snap.lows_1min   or [])
    need_long  = cfg.jdm_ma_long  + 1
    need_short = cfg.jdm_ma_short + 1
    lite_mode  = slot == "OPENING" and need_short <= len(closes) < need_long
    need       = need_short if lite_mode else need_long

    if len(closes) < need:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM",
            f"1분봉 데이터 부족 ({len(closes)}/{need}"
            + (" [OPENING_LITE 대기]" if slot == "OPENING" else "") + ")",
        )
        return None

    if len(closes) >= 2 and closes[-2] > 0:
        slip_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        slip_max = getattr(cfg, "slippage_block_pct", 3.0)
        if slip_pct >= slip_max:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_SLIP",
                f"슬리피지 차단 — 직전 1분봉 대비 {slip_pct:.2f}% 급등 (상한 {slip_max:.1f}%)",
            )
            return None

    return _JdmCtx(
        now=now, slot=slot,
        eff_chejan=eff_chejan, eff_vol_mult=eff_vol_mult,
        eff_rsi_min=eff_rsi_min, eff_ma_spread=eff_ma_spread,
        scoring_bonus=scoring_bonus, trend_lv=trend_lv,
        candle_skip_lv=candle_skip_lv, lite_mode=lite_mode,
        closes=closes, highs=highs, lows=lows,
    )


def _jdm_check_trend_and_ma(
    snap: StockSnapshot, cfg: SmartScannerConfig, ctx: "_JdmCtx"
) -> "Optional[tuple[str, str]]":
    """요셉 추세 필터 + MA 골든크로스/이격도 체크. (spread_tag, rsi_tag) 또는 None 반환."""
    closes, highs, lows = ctx.closes, ctx.highs, ctx.lows

    # ── 요셉 추세 필터
    if getattr(cfg, "yosep_trend_enabled", True):
        if ctx.slot == "AFTERNOON":
            min_trend = int(getattr(cfg, "yosep_min_trend_level_afternoon", 3))
        elif ctx.slot == "OPENING":
            min_trend = int(getattr(cfg, "yosep_min_trend_level_opening", 0))
        else:
            min_trend = int(getattr(cfg, "yosep_min_trend_level", 1))
        if ctx.trend_lv < min_trend:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_TREND",
                f"요셉 추세 미달 [{ctx.slot}] — level {ctx.trend_lv} < {min_trend}",
            )
            return None
        ema_p    = int(getattr(cfg, "yosep_ema_period", 20))
        atr_p    = int(getattr(cfg, "yosep_atr_period", 14))
        down_mult = float(getattr(cfg, "yosep_downtrend_block_atr", 0.8))
        if len(closes) >= ema_p and len(highs) >= atr_p + 1 and len(lows) >= atr_p + 1:
            ema20 = IndicatorService.calc_ema(closes, ema_p)
            atr14 = IndicatorService.calc_atr(highs, lows, closes, atr_p)
            if ema20 is not None and atr14 is not None and atr14 > 0:
                if snap.current_price < (ema20 - atr14 * down_mult):
                    ScannerLogger.rejected(
                        snap.code, snap.name, "JDM_TREND_DOWN",
                        f"하락 추세 강세 — 현재가 {snap.current_price:,} < EMA{ema_p} {ema20:,.0f} - ATR{atr_p}×{down_mult:.1f}",
                    )
                    return None

    # ── MA 체크 (라이트 모드 vs 풀 모드)
    rsi: Optional[float] = None
    if ctx.lite_mode:
        ma_s  = IndicatorService.calc_ma(closes,      cfg.jdm_ma_short)
        pma_s = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_short)
        if ma_s is None or pma_s is None:
            return None
        if not (ma_s > pma_s and snap.current_price > ma_s):
            ScannerLogger.rejected(snap.code, snap.name, "JDM_LITE",
                f"MA{cfg.jdm_ma_short} 상승 미충족 — "
                f"이전 {pma_s:.0f}→현재 {ma_s:.0f}, 현재가 {snap.current_price:,}")
            return None
        spread_tag = f"MA{cfg.jdm_ma_short}↑ {pma_s:.0f}→{ma_s:.0f}"
        rsi_tag    = ""
    else:
        ma_s  = IndicatorService.calc_ma(closes,      cfg.jdm_ma_short)
        ma_l  = IndicatorService.calc_ma(closes,      cfg.jdm_ma_long)
        rsi   = IndicatorService.calc_rsi(closes, 14)
        pma_s = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_short)
        pma_l = IndicatorService.calc_ma(closes[:-1], cfg.jdm_ma_long)
        if any(v is None for v in [ma_s, ma_l, rsi, pma_s, pma_l]):
            return None
        golden        = pma_s <= pma_l and ma_s > ma_l
        gc_override   = int(getattr(cfg, "jdm_golden_cross_trend_override", 2))
        if not golden:
            if gc_override > 0 and ctx.trend_lv >= gc_override and ma_s > ma_l:
                ScannerLogger.passed(snap.code, snap.name, "JDM_GC_OVERRIDE",
                    f"골든크로스 없지만 추세Lv{ctx.trend_lv}+MA정배열 진입 허용 "
                    f"(직전{pma_s:.0f}/{pma_l:.0f}→현재{ma_s:.0f}/{ma_l:.0f})")
            else:
                ScannerLogger.rejected(snap.code, snap.name, "JDM",
                    f"골든크로스 미충족 (직전MA:{pma_s:.0f}/{pma_l:.0f} → 현재MA:{ma_s:.0f}/{ma_l:.0f})")
                return None
        if ctx.now >= cfg.ma_alignment_time and not (ma_s > ma_l):
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 정배열 미충족(09:30+) — MA{cfg.jdm_ma_short}:{ma_s:.0f} ≤ MA{cfg.jdm_ma_long}:{ma_l:.0f}")
            return None
        spread_abs = float(ma_s) - float(ma_l)
        spread_pct = (spread_abs / float(ma_l) * 100) if float(ma_l) > 0 else 0
        if ma_s <= ma_l:
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 정배열 미충족 (Whipsaw 방지) — MA{cfg.jdm_ma_short}:{ma_s:.0f} <= MA{cfg.jdm_ma_long}:{ma_l:.0f}")
            return None
        eff_ma_spread = ctx.eff_ma_spread
        if ctx.slot == "OPENING" or ctx.is_warmup:
            eff_ma_spread *= 0.5
        if spread_pct < eff_ma_spread:
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 이격 부족 ({spread_pct:.2f}% < 최소 {eff_ma_spread:.2f}%"
                + (" [Scoring Bonus]" if ctx.scoring_bonus else "") + ")")
            return None
        if spread_pct > float(cfg.jdm_ma_spread_max_pct):
            ScannerLogger.rejected(snap.code, snap.name, "JDM",
                f"MA 이격 과열 ({spread_pct:.2f}% > 상한 {cfg.jdm_ma_spread_max_pct:.1f}%)")
            return None
        spread_tag = f"MA{cfg.jdm_ma_short}/{cfg.jdm_ma_long} {ma_s:.0f}/{ma_l:.0f} ({spread_pct:.2f}%)"
        rsi_tag    = f"RSI{rsi:.0f}"

    # rsi를 ctx에 저장 (실행품질 체크에서 재사용)
    ctx._rsi = rsi  # type: ignore[attr-defined]
    return (spread_tag, rsi_tag)


def _jdm_check_execution_quality(
    snap: StockSnapshot, cfg: SmartScannerConfig, ctx: "_JdmCtx"
) -> "Optional[tuple[str, str, str]]":
    """거래량·체결강도·EMA 이격·RSI·캔들 패턴 체크. (r_vol, r_chej, candle_reason) 또는 None."""
    closes, highs, lows = ctx.closes, ctx.highs, ctx.lows

    # 워밍업 체크
    warmup_reason = check_indicator_warmup(snap, 15)
    ctx.is_warmup = bool(warmup_reason)

    # ── 거래량 체크
    r_vol = check_volume_surge(snap, ctx.eff_vol_mult, getattr(cfg, "volume_surge_lookback", 10))
    if r_vol is None:
        return None

    # ── 체결 가속도 필터
    skip_exec_vel = ctx.slot == "OPENING" and getattr(cfg, "exec_velocity_disabled_opening", False)
    if getattr(cfg, "exec_velocity_enabled", True) and not skip_exec_vel:
        vel_mult = float(getattr(cfg, "exec_velocity_mult", 1.8))
        if snap.exec_velocity_ratio > 0 and snap.exec_velocity_ratio < vel_mult:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_EXEC_VEL",
                f"[{ctx.slot}] 체결 가속도 미달 — {snap.exec_velocity_ratio:.2f}배 < {vel_mult:.1f}배",
            )
            return None

    # ── 체결강도 체크
    r_chej = check_chejan_strength(snap, ctx.eff_chejan)
    if r_chej is None:
        ScannerLogger.near_miss(snap.code, snap.name, "JDM_CHEJAN",
            actual=snap.chejan_strength, threshold=ctx.eff_chejan,
            reason=f"[{ctx.slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {ctx.eff_chejan:.0f}%")
        return None
    jdm_chejan_max = float(getattr(cfg, "jdm_chejan_max_opening" if ctx.slot == "OPENING" else "jdm_chejan_max",
                                   1200.0 if ctx.slot == "OPENING" else 700.0))
    if snap.chejan_strength >= jdm_chejan_max:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_CHEJAN_MAX",
            f"[{ctx.slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {jdm_chejan_max:.0f}%")
        return None

    # ── EMA 이격 과열 체크
    ema_s_period      = getattr(cfg, "ema_disp_short",         10)
    ema_l_period      = getattr(cfg, "ema_disp_long",          20)
    ema_disp_max      = getattr(cfg, "ema_disp_max_pct",       3.0)
    price_ema_disp_max = getattr(cfg, "price_ema_disp_max_pct", 3.0)
    if ctx.trend_lv >= ctx.candle_skip_lv:
        ema_disp_max       = float(getattr(cfg, "ema_disp_max_pct_trend",       7.0))
        price_ema_disp_max = float(getattr(cfg, "price_ema_disp_max_pct_trend", 6.0))
    if ctx.is_warmup:
        ema_disp_max *= 1.5
        price_ema_disp_max *= 1.5
    if len(closes) >= ema_l_period:
        ema_s = IndicatorService.calc_ema(closes, ema_s_period)
        ema_l = IndicatorService.calc_ema(closes, ema_l_period)
        if ema_s is not None and ema_l is not None and ema_l > 0:
            ema_disp_pct = (ema_s - ema_l) / ema_l * 100
            if ema_disp_pct >= ema_disp_max:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_EMA",
                    f"EMA10/EMA20 이격 과열 — {ema_disp_pct:.2f}% ≥ {ema_disp_max:.1f}%")
                return None
            if ema_s > 0:
                price_ema_disp = (snap.current_price - ema_s) / ema_s * 100
                if price_ema_disp >= price_ema_disp_max:
                    ScannerLogger.rejected(snap.code, snap.name, "JDM_PRICE_EMA",
                        f"현재가/EMA{ema_s_period} 이격 과열 — {price_ema_disp:.2f}% ≥ {price_ema_disp_max:.1f}%")
                    return None

    # ── RSI 체크
    rsi = getattr(ctx, "_rsi", None)
    if not ctx.lite_mode and rsi is not None:
        eff_rsi_high = cfg.jdm_rsi_high
        if ctx.trend_lv >= ctx.candle_skip_lv:
            eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_trend", 80.0))
            if len(closes) >= 20 and len(highs) >= 15 and len(lows) >= 15:
                ema20_b = IndicatorService.calc_ema(closes, 20)
                atr14_b = IndicatorService.calc_atr(highs, lows, closes, 14)
                if (ema20_b is not None and atr14_b is not None and atr14_b > 0
                        and snap.current_price > ema20_b + atr14_b * 1.5):
                    eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_breakout", 82.0))
            if ctx.slot == "OPENING" and ctx.trend_lv >= 3:
                eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_opening_trend3", 83.0))
        if ctx.is_warmup:
            eff_rsi_high = 88.0
        if not (ctx.eff_rsi_min <= rsi < eff_rsi_high):
            thresh = ctx.eff_rsi_min if rsi < ctx.eff_rsi_min else eff_rsi_high
            ScannerLogger.near_miss(snap.code, snap.name, "JDM_RSI",
                actual=rsi, threshold=thresh,
                reason=f"[{ctx.slot}] RSI 범위 초과 — 현재 {rsi:.1f}% (허용 {ctx.eff_rsi_min:.0f}~{eff_rsi_high:.0f}%, trend_lv={ctx.trend_lv})")
            return None

    # ── 캔들 패턴
    if not ctx.lite_mode:
        if ctx.trend_lv >= ctx.candle_skip_lv:
            candle_reason = f"TREND_SKIP(lv{ctx.trend_lv})"
        else:
            r_engulf = check_bullish_engulfing(snap)
            r_pinbar = check_bullish_pin_bar(snap)
            if ctx.is_warmup and r_engulf is None and r_pinbar is None:
                if snap.current_price > snap.open_price and snap.current_price >= snap.high_prev:
                    candle_reason = "AGGRESSIVE_BREAKOUT"
                else:
                    ScannerLogger.rejected(snap.code, snap.name, "JDM_CANDLE", "워밍업 양봉 돌파 미충족")
                    return None
            elif r_engulf is None and r_pinbar is None:
                ScannerLogger.rejected(snap.code, snap.name, "JDM_CANDLE",
                    f"캔들 패턴 미충족 (상승장악형·강세핀바 불성립, trend_lv={ctx.trend_lv} < {ctx.candle_skip_lv})")
                return None
            else:
                candle_reason = r_engulf or r_pinbar
    else:
        candle_reason = "LITE(캔들패턴스킵)"

    # ── 체결강도 최종 재확인
    if snap.chejan_strength < ctx.eff_chejan:
        ScannerLogger.near_miss(snap.code, snap.name, "JDM_CHEJAN_FINAL",
            actual=snap.chejan_strength, threshold=ctx.eff_chejan,
            reason=f"[{ctx.slot}] 체결강도 최종 재확인 미충족 — 현재 {snap.chejan_strength:.0f}% < {ctx.eff_chejan:.0f}%")
        return None

    return (r_vol, r_chej, candle_reason)


def _jdm_check_daily_context(
    snap: StockSnapshot, cfg: SmartScannerConfig, ctx: "_JdmCtx"
) -> "Optional[dict]":
    """피봇 R2 + 일봉 정배열 + 일봉 20MA 체크. daily_ctx dict 또는 None 반환."""
    closes = ctx.closes

    if not ctx.lite_mode and cfg.pivot_r2_enabled:
        r2 = IndicatorService.calc_pivot_r2(snap.daily_high_prev, snap.daily_low_prev, snap.prev_close)
        if r2 > 0 and snap.current_price < r2:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_PIVOT",
                f"피봇 R2 미돌파 (현재가={snap.current_price:,} < R2={r2:,.0f})")
            return None

    if cfg.daily_alignment_enabled and len(snap.daily_closes) >= 20:
        align = IndicatorService.check_daily_alignment(snap.daily_closes, snap.current_price)
        if not align["is_aligned"]:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_ALIGN",
                f"일봉 정배열 미충족 (5MA > 10MA > 20MA, 데이터={len(snap.daily_closes)}개)")
            return None

    near_high_thr = float(getattr(cfg, "daily_near_high_threshold_pct", 3.0))
    daily_ctx = IndicatorService.get_daily_context(snap.daily_closes, snap.current_price, near_high_thr)

    if getattr(cfg, "daily_ma20_filter_enabled", True):
        if not daily_ctx["above_ma20"] and daily_ctx["daily_ma20"] > 0:
            ScannerLogger.rejected(snap.code, snap.name, "JDM_DAILY_MA20",
                f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {daily_ctx['daily_ma20']:,.0f}")
            return None

    if getattr(cfg, "daily_ma20_slope_enabled", True):
        if not daily_ctx.get("ma20_slope_up", True):
            ScannerLogger.rejected(snap.code, snap.name, "JDM_MA20_SLOPE",
                f"일봉 20MA 기울기 하락 — 추세추종 진입 차단 (20MA={daily_ctx['daily_ma20']:,.0f})")
            return None

    return daily_ctx


def check_jdm_entry(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    JDM_ENTRY 통합 게이트 (ScannerWorker / SmartScanner._evaluate 공통).

    ① 시장/수급/시간 조건 (_jdm_build_ctx)
    ② MA 골든크로스 + 추세 필터 (_jdm_check_trend_and_ma)
    ③ 거래량·체결강도·EMA이격·RSI·캔들패턴 (_jdm_check_execution_quality)
    ④ 피봇 R2 + 일봉 정배열/20MA (_jdm_check_daily_context)
    """
    ctx = _jdm_build_ctx(snap, cfg)
    if ctx is None:
        return None

    ma_result = _jdm_check_trend_and_ma(snap, cfg, ctx)
    if ma_result is None:
        return None
    spread_tag, rsi_tag = ma_result

    exec_result = _jdm_check_execution_quality(snap, cfg, ctx)
    if exec_result is None:
        return None
    r_vol, r_chej, candle_reason = exec_result

    daily_ctx = _jdm_check_daily_context(snap, cfg, ctx)
    if daily_ctx is None:
        return None

    mode_tag  = "JDM_LITE" if ctx.lite_mode else "JDM"
    near_tag  = " | 📈신고가근처(TP↑)" if daily_ctx["near_high"] else ""
    warm_tag  = " | [WARMUP]" if ctx.is_warmup else ""
    reason    = f"[{ctx.slot}][{mode_tag}]{warm_tag} {r_vol} | {r_chej} | {spread_tag} | {rsi_tag} | {candle_reason}{near_tag}"
    ScannerLogger.passed(snap.code, snap.name, mode_tag, reason)
    return reason


def check_pullback_entry(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    [NEW] 눌림목 진입 신호 (Pullback Entry).
    
    상승 추세(trend_level >= 2) 종목이 EMA20 근처까지 눌렸을 때 진입.
    추세 상승장에서 '달리는 말에 올라타기' 대신 '잠시 쉬어갈 때'를 포착.
    """
    tlv = int(getattr(snap, "trend_level", 0))
    if tlv < 2:
        return None  # 최소 Medium Trend 이상 필요

    closes = snap.closes_1min
    if len(closes) < 20:
        return None

    ema20 = IndicatorService.calc_ema(closes, 20)
    rsi = IndicatorService.calc_rsi(closes, 14)
    if ema20 is None or rsi is None:
        return None

    # 1. EMA20 근처 확인 (0% ~ +0.8% 이내)
    dist = (snap.current_price - ema20) / ema20 * 100
    if not (0.0 <= dist <= 0.8):
        return None

    # 2. RSI 과열 해소 확인 (40 ~ 55)
    # 이미 너무 과열된 상태(70+)에서의 눌림은 위험하므로 배제
    if not (40.0 <= rsi <= 58.0):
        return None

    # 3. 거래량 확인 (일시적 거래 감소 확인 - 패닉 셀링 방지)
    vols = snap.volumes_1min
    if len(vols) >= 5:
        avg_v5 = sum(vols[-6:-1]) / 5
        if vols[-1] > avg_v5 * 1.5:
             # 거래량이 실린 하락은 눌림목이 아니라 추세 붕괴일 수 있음
             return None

    reason = f"[PULLBACK] EMA20지지({dist:.2f}%) | RSI {rsi:.1f} | 추세Lv{tlv}"
    ScannerLogger.passed(snap.code, snap.name, "PULLBACK", reason)
    return reason

