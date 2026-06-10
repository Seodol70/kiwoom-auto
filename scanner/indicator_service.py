"""
IndicatorService — 기술지표 계산 고속화 및 AI 피처 생성
"""

import logging
import numpy as np
import pandas as pd
from functools import lru_cache
from typing import Optional, Any, TYPE_CHECKING, Dict, Union

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class IndicatorService:
    """기술지표 계산 서비스 — 모든 지표 계산을 고속화하여 담당"""

    @staticmethod
    @lru_cache(maxsize=1024)
    def _calc_rsi_cached(closes_tuple: tuple[float, ...], period: int) -> Optional[float]:
        """내부 캐시용 RSI 계산"""
        try:
            arr = np.array(closes_tuple, dtype=np.float64)
            deltas = np.diff(arr)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)
            if len(gains) < period: return None
            s_gains = pd.Series(gains)
            s_losses = pd.Series(losses)
            avg_gain = s_gains.ewm(alpha=1.0/period, adjust=False).mean().iloc[-1]
            avg_loss = s_losses.ewm(alpha=1.0/period, adjust=False).mean().iloc[-1]
            if avg_loss == 0: return 100.0
            rs = avg_gain / avg_loss
            return float(100.0 - (100.0 / (1.0 + rs)))
        except Exception:
            return None

    @staticmethod
    def calc_rsi(closes: list[float] | np.ndarray, period: int = 14) -> Optional[float]:
        if closes is None or len(closes) < period + 1: return None
        return IndicatorService._calc_rsi_cached(tuple(closes), period)

    @staticmethod
    @lru_cache(maxsize=1024)
    def _calc_ema_cached(closes_tuple: tuple[float, ...], period: int) -> Optional[float]:
        try:
            s = pd.Series(closes_tuple)
            return float(s.ewm(span=period, adjust=False).mean().iloc[-1])
        except Exception:
            return None

    @staticmethod
    def calc_ema(closes: list[float] | np.ndarray, period: int) -> Optional[float]:
        if closes is None or len(closes) < period: return None
        return IndicatorService._calc_ema_cached(tuple(closes), period)

    @staticmethod
    @lru_cache(maxsize=1024)
    def _calc_ma_cached(closes_tuple: tuple[float, ...], period: int) -> Optional[float]:
        try:
            return float(np.mean(closes_tuple[-period:]))
        except Exception:
            return None

    @staticmethod
    def calc_ma(closes: list[float] | np.ndarray, period: int) -> Optional[float]:
        if closes is None or len(closes) < period: return None
        return IndicatorService._calc_ma_cached(tuple(closes), period)

    @staticmethod
    def calc_vwap(prices: np.ndarray, volumes: np.ndarray) -> Optional[float]:
        """당일 VWAP(거래량 가중 평균 가격) 산출"""
        if len(prices) == 0 or len(prices) != len(volumes):
            return None
        
        # Cumulative Sum(Price * Volume) / Cumulative Sum(Volume)
        # 단기 매매에서는 당일 전체 데이터를 대상으로 함
        p_v = prices * volumes
        sum_pv = np.sum(p_v)
        sum_v = np.sum(volumes)
        
        if sum_v <= 0:
            return None
            
        return float(sum_pv / sum_v)

    @staticmethod
    def calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
        """Wilder's Smoothing 방식의 ATR 고속 계산"""
        if not closes or len(closes) < period + 1: return None
        try:
            h = np.array(highs)
            l = np.array(lows)
            c = np.array(closes)
            
            tr1 = h[1:] - l[1:]
            tr2 = np.abs(h[1:] - c[:-1])
            tr3 = np.abs(l[1:] - c[:-1])
            
            tr = np.maximum.reduce([tr1, tr2, tr3])
            
            # Wilder's Smoothing
            s_tr = pd.Series(tr)
            atr = s_tr.ewm(alpha=1.0/period, adjust=False).mean().iloc[-1]
            
            return float(atr)
        except Exception:
            return None

    @staticmethod
    def calc_bollinger_bands(closes: list[float] | np.ndarray, period: int = 20, std_mult: float = 2.0) -> Optional[dict[str, float]]:
        if len(closes) < period: return None
        try:
            arr = np.array(closes[-period:], dtype=np.float64)
            mid = float(arr.mean())
            std = float(arr.std())
            return {
                "upper": mid + std_mult * std,
                "middle": mid,
                "lower": mid - std_mult * std
            }
        except Exception:
            return None

    @staticmethod
    def get_trend_status(closes: list[float], highs: list[float], lows: list[float], volumes: list[int], **kwargs) -> int:
        """추세 강도 판정 (0~3) 최적화"""
        ema_period = kwargs.get("ema_period", 20)
        atr_period = kwargs.get("atr_period", 14)
        
        if len(closes) < ema_period + 2: return 0
        
        ema_now = IndicatorService.calc_ema(closes, ema_period)
        ema_prev = IndicatorService.calc_ema(closes[:-1], ema_period)
        atr = IndicatorService.calc_atr(highs, lows, closes, atr_period)
        
        if not ema_now or not ema_prev or not atr: return 0
        
        cur_price = closes[-1]
        if cur_price <= ema_now or ema_now <= ema_prev: return 0
        
        dist_atr = (cur_price - ema_now) / atr

        # dist_atr > 2.5: 가격이 EMA20에서 ATR의 2.5배 이상 이격 → 과열 의심
        # Lv3(즉시진입)을 부여하지 않고 Lv2로 하향하여 진입 관찰 시간 확보
        if dist_atr > 2.5: return 2
        if dist_atr >= 1.5: return 3
        if dist_atr >= 1.0: return 2
        if dist_atr >= 0.3: return 1
        return 0

    # ── 추세 모멘텀 / 개장 관찰 점수 ────────────────────────────────────────────

    @staticmethod
    def calc_trend_momentum(trend_lv_history: list[int]) -> float:
        """추세 이력 기반 모멘텀 점수 (0.0~1.0).

        "Lv0→1→2→3으로 단계적으로 쌓였는가"를 측정.
        우연히 한 번 Lv3인 종목과 꾸준히 올라온 종목을 구분.

        점수 산출 방식:
          1. 연속 상승 길이 (consecutive_up):
             hist = [..., 1, 2, 2, 3] → 뒤에서부터 이전 값 이하로 꺾이기 전까지 길이
          2. 최근 최고점 (peak_lv): 이력 내 최고 trend_lv
          3. 하락 없이 유지된 비율 (stability): 내림차순 전환 횟수 기반 페널티

        반환:
          0.0 — 이력 없거나 단발 Lv3 (내림 후 Lv3)
          0.3 — 최근 2틱 상승 + peak Lv2 이상
          0.6 — 3틱 이상 연속 상승 + peak Lv3
          1.0 — 5틱 이상 연속 상승 + peak Lv3 + 하락 전환 0회
        """
        hist = list(trend_lv_history)
        if len(hist) < 2:
            return 0.0

        # 뒤에서부터 "엄격 상승(>)" 연속 길이 측정
        # [0,0,0,3] → 마지막 1틱만 상승, consecutive_up=1
        # [0,1,2,3] → 3틱 연속 상승, consecutive_up=3
        consecutive_up = 0
        for i in range(len(hist) - 1, 0, -1):
            if hist[i] > hist[i - 1]:
                consecutive_up += 1
            else:
                break

        peak_lv = max(hist)

        # 전체 이력에서 하락 전환 횟수 (내림 페널티)
        drop_count = sum(1 for i in range(1, len(hist)) if hist[i] < hist[i - 1])
        stability = max(0.0, 1.0 - drop_count * 0.2)

        # 기본 점수: 연속 상승 길이 × peak 보정
        # consecutive_up=0 → 직전 틱 대비 상승 없음 (유지 or 하락)
        if consecutive_up >= 4 and peak_lv >= 3:
            base = 1.0
        elif consecutive_up >= 3 and peak_lv >= 3:
            base = 0.75
        elif consecutive_up >= 3 and peak_lv >= 2:
            base = 0.55
        elif consecutive_up >= 2 and peak_lv >= 2:
            base = 0.40
        elif consecutive_up >= 1 and peak_lv >= 3:
            base = 0.25  # 단발 Lv3 — 최근 1틱만 상승
        elif consecutive_up >= 1 and peak_lv >= 2:
            base = 0.15
        elif consecutive_up >= 1:
            base = 0.08
        elif peak_lv >= 3:
            base = 0.30  # 현재 틱 유지지만 이력 내 Lv3 존재 (단계 상승 후 유지)
        elif peak_lv >= 2:
            base = 0.12  # Lv2까지는 올랐으나 현재 하락/유지
        else:
            base = 0.02  # Lv1 이하 유지/하락

        return round(min(base * stability, 1.0), 3)

    @staticmethod
    def calc_opening_watch_score(
        trend_lv_history: list[int],
        leading_score: float,
        chejan_history: list[float],
        vel_ratio: float,
        elapsed_minutes: float,
    ) -> float:
        """개장/시작 후 30분 관찰 점수 (0.0~1.0).

        30분 동안 종목이 얼마나 일관되게 강세 신호를 쌓아왔는지 측정.
        단순 현재 지표가 아닌 "시간에 걸쳐 누적된 품질"을 반영.

        구성 요소:
          A. 추세 모멘텀 (40%): 단계적 상승 연속성
          B. 선행 점수 현재값 (25%): leading_score (매수압력, 매도벽 등)
          C. 체결강도 평균 (20%): 관찰 기간 내 평균 체결강도 정규화
          D. 체결 가속도 (15%): vel_ratio 정규화

        elapsed_minutes: 프로그램 시작 또는 09:00 이후 경과 분 수.
          0~5분: 점수 50% 할인 (데이터 부족 → 과신 방지)
          5~15분: 선형 보간으로 할인 해제
          15분+: 할인 없음

        반환: 0.0(관찰 부족/약세) ~ 1.0(모든 구성요소 강세)
        """
        # A. 추세 모멘텀
        momentum = IndicatorService.calc_trend_momentum(trend_lv_history)

        # B. 선행 점수 (None이면 0.0 처리)
        lead = float(leading_score) if leading_score is not None else 0.0

        # C. 체결강도 평균 정규화 (100% 기준, 200%이면 1.0)
        if chejan_history:
            avg_chejan = sum(chejan_history) / len(chejan_history)
        else:
            avg_chejan = 100.0
        chejan_score = min(max((avg_chejan - 100.0) / 100.0, 0.0), 1.0)

        # D. 체결 가속도 정규화 (vel_ratio 2.0이면 1.0)
        vel_score = min(vel_ratio / 2.0, 1.0)

        raw = (
            momentum   * 0.40 +
            lead       * 0.25 +
            chejan_score * 0.20 +
            vel_score  * 0.15
        )

        # 경과 시간 할인: 5분 미만 50%, 5~15분 선형 회복
        if elapsed_minutes < 5.0:
            discount = 0.5
        elif elapsed_minutes < 15.0:
            discount = 0.5 + 0.5 * (elapsed_minutes - 5.0) / 10.0
        else:
            discount = 1.0

        return round(min(raw * discount, 1.0), 3)

    # ── 선행 지표 (Leading Indicators) ─────────────────────────────────────────

    @staticmethod
    def calc_rs_leading_score(rs_score: float) -> float:
        """
        지수 대비 상대강도 점수 (0.0~1.0) — 폭락장 역행 상승 포착.

        rs_score = stock.change_pct - index.change_pct
        KOSPI -8%, 종목 +2%  → rs_score = +10.0 → 1.0 (극강)
        KOSPI -3%, 종목 +1%  → rs_score = +4.0  → 0.75
        KOSPI  0%, 종목 +1%  → rs_score = +1.0  → 0.35
        rs_score < +0.5      → 0.0 (지수보다 약하거나 비슷)

        스마트머니가 폭락 속에서도 특정 종목을 적극 매집한다는 의미.
        기존 지표(체결강도, 거래량, 호가)와 독립적인 시장 맥락 신호.
        """
        if rs_score >= 8.0:  return 1.0
        if rs_score >= 5.0:  return 0.85
        if rs_score >= 3.0:  return 0.65
        if rs_score >= 1.5:  return 0.45
        if rs_score >= 0.5:  return 0.25
        return 0.0

    @staticmethod
    def calc_chejan_reversal_score(chejan_history: list) -> float:
        """
        체결강도 바닥 반등 점수 (0.0~1.0).

        탐지: 이전 5틱이 조용하다가(< 115%) 최근 3틱이 갑자기 130%+로 상승.
        이는 매수세가 막 점화되는 초입 순간을 잡는 선행 신호.
        이미 130% 상태가 오래된 종목은 점수 0 (이미 늦음).
        """
        if len(chejan_history) < 8:
            return 0.0
        recent_avg = sum(chejan_history[-3:]) / 3
        older_avg  = sum(chejan_history[-8:-3]) / 5
        if older_avg <= 0:
            return 0.0
        was_quiet  = older_avg < 115.0
        # 하락장 적응: 130%→110% (전약후강이면 충분)
        now_active = recent_avg > 110.0
        rise_ratio = recent_avg / older_avg
        if was_quiet and now_active and rise_ratio > 1.15:
            return min((rise_ratio - 1.0) * 2.5, 1.0)
        return 0.0

    @staticmethod
    def calc_chejan_acceleration(chejan_history: list) -> float:
        """
        체결강도 가속도 점수 (0.0~1.0).

        탐지: 반등 후 계속 강해지는가 (반등 후 추세 확인).
        최근 3틱이 이전 3틱보다 계속 강해야 함 = 진정한 매수세.
        """
        if len(chejan_history) < 7:
            return 0.0
        recent_3 = chejan_history[-3:]
        prior_3  = chejan_history[-6:-3]

        recent_avg = sum(recent_3) / 3
        prior_avg  = sum(prior_3) / 3

        if prior_avg <= 0:
            return 0.0

        # 최근이 이전보다 강해졌는가?
        accel_ratio = recent_avg / prior_avg
        if accel_ratio > 1.05:  # 5% 이상 강해짐
            return min((accel_ratio - 1.0) * 2.0, 1.0)
        return 0.0

    @staticmethod
    def calc_hoga_velocity(bid_qty_sums_history: list[int] | None) -> float:
        """
        호가 매수 속도 점수 (0.0~1.0).

        탐지: 1~5호가 매수잔량 합계가 지속적으로 증가하는가 (10스냅 이상 이력 필요).
        최근 5스냅 평균 > 이전 5스냅 평균 → 매수 잔량 증가 추세.
        """
        if bid_qty_sums_history is None or len(bid_qty_sums_history) < 10:
            return 0.0

        recent_avg = sum(bid_qty_sums_history[-5:]) / 5  # 최근 5스냅
        prior_avg  = sum(bid_qty_sums_history[-10:-5]) / 5  # 이전 5스냅

        if prior_avg <= 0:
            return 0.0

        velocity_ratio = recent_avg / prior_avg
        if velocity_ratio > 1.1:  # 10% 이상 증가
            return min((velocity_ratio - 1.0) * 2.0, 1.0)
        return 0.0

    @staticmethod
    def calc_vol_burst_score(volumes: list) -> float:
        """
        거래량 폭증 가속도 점수 (0.0~1.0) — 선행 지표.

        탐지: 최근 2분 평균이 직전 3분 평균 대비 2배↑ 이상 가속.
        "조용함" 조건 제거 — 이미 활성 종목도 갑자기 더 터지면 포착.
        2.0배=0.5점, 4.0배=1.0점.
        """
        if len(volumes) < 5:
            return 0.0
        recent_2 = sum(volumes[-2:]) / 2
        prior_3  = sum(volumes[-5:-2]) / 3
        if prior_3 <= 0:
            return 0.0
        burst_ratio = recent_2 / prior_3
        if burst_ratio >= 2.0:
            return min((burst_ratio - 2.0) / 2.0 + 0.5, 1.0)
        return 0.0

    @staticmethod
    def calc_accumulation_score(volumes: list, closes: list) -> float:
        """
        거래량 축적(Accumulation) 점수 (0.0~1.0).

        탐지: 거래량은 2배↑ 폭증하는데 가격은 1.5% 이내로 안 오름.
        스마트머니가 가격을 올리지 않고 조용히 매집 중 → 곧 상승 압력 개방.
        """
        if len(volumes) < 11 or len(closes) < 6:
            return 0.0
        vol_recent = sum(volumes[-5:])
        vol_prior  = sum(volumes[-10:-5])
        if vol_prior <= 0:
            return 0.0
        vol_surge  = vol_recent / vol_prior
        price_chg  = abs((closes[-1] / closes[-6]) - 1.0) * 100 if closes[-6] > 0 else 999.0
        if vol_surge >= 2.0 and price_chg < 1.5:
            vol_score   = min((vol_surge - 2.0) / 3.0, 1.0)
            price_score = 1.0 - min(price_chg / 1.5, 1.0)
            return (vol_score + price_score) / 2.0
        return 0.0

    @staticmethod
    def calc_bid1_slope_score(bid1_history: list) -> float:
        """
        매수1호가 우상향 기울기 점수 (0.0~1.0) — 선행 지표.

        탐지: 최근 5틱 동안 매수1호가가 지속적으로 올라가는가.
        살 사람이 점점 더 높은 가격을 제시 = 가장 강한 수요 선행 신호.
        +0.05% 이상부터 점수 발생, +0.30% 이상이면 1.0.
        단조 상승 비율(monotone_ratio)로 최종 보정 — 진동이 많으면 할인.
        """
        h = [p for p in list(bid1_history) if p > 0]
        if len(h) < 5:
            return 0.0
        h = h[-5:]
        slope_pct = (h[-1] - h[0]) / h[0] * 100
        if slope_pct < 0.05:
            return 0.0
        # 4쌍 중 상승한 쌍의 비율 — 50% 미만이면 진동이 심한 것으로 간주
        ascending = sum(1 for i in range(len(h) - 1) if h[i + 1] >= h[i])
        monotone_ratio = ascending / (len(h) - 1)
        if monotone_ratio < 0.50:
            return 0.0
        base = min((slope_pct - 0.05) / 0.25, 1.0)
        return base * monotone_ratio

    @staticmethod
    def calc_tick_vol_accel_score(tick_vol_history: list) -> float:
        """
        틱 단위 체결속도 가속도 점수 (0.0~1.0) — 선행 지표.

        1분봉 집계를 기다리지 않고 즉시 체결 폭발 감지.
        최근 5틱 평균 / 이전 5틱 평균 >= 2.0이면 점수 발생.
        5.0배 이상이면 1.0.
        """
        h = [v for v in list(tick_vol_history) if v > 0]
        if len(h) < 10:
            return 0.0
        recent = sum(h[-5:]) / 5
        prior  = sum(h[-10:-5]) / 5
        if prior <= 0:
            return 0.0
        ratio = recent / prior
        if ratio >= 2.0:
            return min((ratio - 2.0) / 3.0, 1.0)
        return 0.0

    @staticmethod
    def calc_ask1_wall_collapse_score(ask1_qty_history: list) -> float:
        """
        매도1호가 수량 급감 점수 (0.0~1.0) — 선행 지표.

        매도벽이 얇아지는 순간 = 상승 돌파 직전.
        최근 5틱에서 peak 대비 현재가 50% 이하면 점수 발생.
        peak 대비 80% 감소(현재=20%)이면 1.0.
        """
        h = [q for q in list(ask1_qty_history) if q > 0]
        if len(h) < 5:
            return 0.0
        h = h[-5:]
        peak = max(h[:-1])  # 직전 4틱 중 최대 (현재 제외)
        current = h[-1]
        if peak <= 0:
            return 0.0
        collapse_ratio = 1.0 - (current / peak)  # 0=유지, 1=완전소멸
        if collapse_ratio >= 0.50:  # 50% 이상 감소부터 점수
            return min((collapse_ratio - 0.50) / 0.30, 1.0)
        return 0.0

    @staticmethod
    def calc_hoga_pressure_score(total_ask_qty: int, total_bid_qty: int) -> float:
        """
        호가 매수 압력 점수 (0.0~1.0).

        매수잔량 비율 > 55%이면 양수 점수. 55%=0.0, 75%=1.0.
        실시간 호가창에서 사려는 사람이 팔려는 사람보다 많음을 직접 반영.
        """
        total = total_ask_qty + total_bid_qty
        if total <= 0:
            return 0.0
        bid_ratio = total_bid_qty / total
        if bid_ratio > 0.55:
            return min((bid_ratio - 0.55) * 5.0, 1.0)
        return 0.0

    @staticmethod
    def get_leading_score(snap: 'StockSnapshot') -> Optional[float]:
        """
        복합 선행 점수 (0.0~1.0) — 우상향 가능성 예측.

        [방향 원칙]
        "지금 막 거래량과 매수세가 폭발하기 시작하는 순간"을 포착.
        이미 오른 후 지표가 높은 것(후행)이 아니라,
        조용하다가 갑자기 터지는 변화의 초입을 잡는다(선행).

        PRIMARY 조건 (하나 이상 필수):
          매수1호가 우상향 ≥ 0.30   — 살 사람이 점점 더 높은 가격 제시 (가장 직접적 선행)
          거래량 폭발(방향보정) ≥ 0.40 — 가격 상승 중 거래량 폭발 (패닉셀 할인 후)
          체결강도 반등 ≥ 0.25     — 체결강도 바닥→110%+ 반등 (매수세 점화)
          기관/외인 전환 ≥ 0.50   — 기관 방향 전환
          호가 압력(보조 확인 필수) — hp ≥ 0.50 + (bs/cr/vb/tv 중 1개 이상)
          매도벽 급감 ≥ 0.50       — 매도1호가 수량 50%↑ 급감 (돌파 직전)
          틱속도 가속(방향보정) ≥ 0.50 — 가격 상승 중 틱속도 폭발
          RS ≥ 0.65 (rs_score ≥ +3.0%) — 폭락장 역행 상승 (스마트머니 컨텍스트 단독 진입)

        PRIMARY 없으면 0.0 반환 → 진입 차단.

        가중합 (PRIMARY 통과 후):
          매수1호가 기울기(단조성 보정)  19% — 수요자가 직접 가격 올려가며 사는 신호
          매도벽 급감                   19% — 상승 돌파 직전 저항 소멸
          지수 대비 상대강도(RS)        15% — 폭락장 역행 상승 = 스마트머니 매집 컨텍스트
          틱속도 가속(방향보정)         12% — 가격 상승 중 체결 폭발 확인
          거래량 폭발(방향보정)         11% — 분봉 거래량 추세 확인
          체결강도 반등                 10% — 매수세 점화 확인
          체결강도 가속도                5% — 반등 후 계속 강해지는가
          기관/외인 전환                 5% — 스마트머니 방향 전환
          호가 압력                      2% — 실시간 매수 우위
          호가 속도                      1% — 매수잔량 증가 추세
          거래량 축적                    1% — 스마트머니 매집 (보조)

        데이터 부족 시 None 반환 → 호출처에서 체크 생략.
        """
        hist = list(getattr(snap, 'chejan_history', None) or [])
        vols = list(getattr(snap, 'volumes_1min',  None) or [])
        if len(hist) < 8 or len(vols) < 11:
            return None  # 데이터 부족 → 체크 생략

        closes_1m = list(getattr(snap, 'closes_1min', None) or [])
        cr  = IndicatorService.calc_chejan_reversal_score(hist)
        ca  = IndicatorService.calc_chejan_acceleration(hist)
        vb  = IndicatorService.calc_vol_burst_score(vols)
        ac  = IndicatorService.calc_accumulation_score(vols, closes_1m)
        hp  = IndicatorService.calc_hoga_pressure_score(
            int(getattr(snap, 'total_ask_qty', 0) or 0),
            int(getattr(snap, 'total_bid_qty', 0) or 0),
        )
        bid_qty_sums_hist = list(getattr(snap, 'bid_qty_sums_history', None) or [])
        hv  = IndicatorService.calc_hoga_velocity(bid_qty_sums_hist if bid_qty_sums_hist else None)
        iv  = min(float(getattr(snap, 'inv_flip_score', 0.0) or 0.0), 1.0)
        bs  = IndicatorService.calc_bid1_slope_score(
            list(getattr(snap, 'bid1_history', None) or []))
        aw  = IndicatorService.calc_ask1_wall_collapse_score(
            list(getattr(snap, 'ask1_qty_history', None) or []))
        tv  = IndicatorService.calc_tick_vol_accel_score(
            list(getattr(snap, 'tick_vol_history', None) or []))

        # 지수 대비 상대강도 (RS): 폭락장 역행 상승 = 스마트머니 매집 신호
        rs  = IndicatorService.calc_rs_leading_score(float(getattr(snap, 'rs_score', 0.0) or 0.0))

        # [개선 1] vb/tv 방향성 보정: 가격 하락 중이면 70% 할인 (패닉셀 false positive 방지)
        # 데이터 부족 시 보정 생략 (보수적 채택)
        if len(closes_1m) >= 3:
            price_up = closes_1m[-1] > closes_1m[-3]
        else:
            price_up = True
        vb_dir = vb if price_up else vb * 0.3
        tv_dir = tv if price_up else tv * 0.3

        # [개선 2] hp PRIMARY 단독 통과 방지: 호가잔량 조작 방어
        # hp 단독으로 PRIMARY를 통과하려면 체결/거래량 보조 신호 1개 이상 필요
        hp_primary = (hp >= 0.50) and (bs >= 0.10 or cr >= 0.10 or vb_dir >= 0.15 or tv_dir >= 0.15)

        # [개선 3] aw PRIMARY 단독 통과 방지: 매도벽 급감은 저항 소멸 신호, 매수 압력 신호 아님
        # 실증 2026-06-09: 이노인스트루먼트(-8,324원), KoAct바이오(-2,402원) aw단독 통과 후 즉시 손절
        aw_primary = (aw >= 0.50) and (bs >= 0.10 or cr >= 0.15 or vb_dir >= 0.15 or tv_dir >= 0.15)

        # [개선 4] rs PRIMARY 단독 통과 방지: RS는 시장 컨텍스트, 즉각 매수 압력 신호 아님
        # 실증 2026-06-09: 코스모로보틱스(-5,971원), 이미지스(-3,049원) rs=1.0인데 bs/vb/aw 모두 0
        rs_primary = (rs >= 0.65) and (bs >= 0.10 or cr >= 0.15 or vb_dir >= 0.15 or aw >= 0.20 or tv_dir >= 0.15)

        # PRIMARY 조건: "막 불붙기 시작"하는 신호 중 하나 이상 필수
        primary_ok = (
            (bs >= 0.30) or (vb_dir >= 0.40) or (cr >= 0.25) or
            (iv >= 0.50) or hp_primary or aw_primary or (tv_dir >= 0.50) or
            rs_primary
        )
        if not primary_ok:
            return 0.0

        # [개선 4] RS 추가 (15%): bs/aw 각 3%p씩 인하, tv 3%p 인하
        # 합계: 0.19+0.19+0.15+0.12+0.11+0.10+0.05+0.05+0.02+0.01+0.01 = 1.00
        return (
            bs    * 0.19 + aw    * 0.19 +
            rs    * 0.15 +
            tv_dir* 0.12 + vb_dir* 0.11 +
            cr    * 0.10 + ca    * 0.05 +
            iv    * 0.05 + hp    * 0.02 +
            hv    * 0.01 + ac    * 0.01
        )

    @staticmethod
    def calc_pivot_r2(prev_high: int, prev_low: int, prev_close: int) -> float:
        """피봇 2차 저항선(R2) 계산. P=(고+저+종)/3, R2=P+(고-저)"""
        if prev_high <= 0 or prev_low <= 0 or prev_close <= 0:
            return 0.0
        pivot = (prev_high + prev_low + prev_close) / 3.0
        return pivot + (prev_high - prev_low)

    @staticmethod
    def check_daily_alignment(daily_closes: list[float], current_price: Optional[float] = None) -> dict:
        """일봉 정배열 확인"""
        res = {"is_aligned": False, "ma5": 0.0, "ma10": 0.0, "ma20": 0.0}
        if len(daily_closes) < 20: return res
        
        closes = list(daily_closes)
        if current_price:
            closes.append(current_price)
            
        res["ma5"] = IndicatorService.calc_ma(closes, 5) or 0.0
        res["ma10"] = IndicatorService.calc_ma(closes, 10) or 0.0
        res["ma20"] = IndicatorService.calc_ma(closes, 20) or 0.0
        
        res["is_aligned"] = res["ma5"] > res["ma10"] > res["ma20"]
        return res

    @staticmethod
    def get_daily_context(
        daily_closes: list[float],
        current_price: float,
        near_high_threshold_pct: float = 3.0,
    ) -> dict:
        """일봉 데이터 기반 매매 맥락 정보 반환"""
        result = {
            "above_ma20": True, "near_high": False, 
            "daily_ma20": 0.0, "high_25d": 0.0,
            "above_ma60": True, "daily_ma60": 0.0,
            "ma20_slope_up": True
        }

        if len(daily_closes) < 20 or current_price <= 0:
            return result

        arr = np.array(daily_closes)
        # 최근 20일 이동평균
        daily_ma20 = np.mean(arr[-20:])
        result["daily_ma20"] = float(daily_ma20)
        result["above_ma20"] = current_price >= daily_ma20

        # MA20 기울기
        if len(arr) >= 23:
            ma20_prev = np.mean(arr[-23:-3])
            result["ma20_slope_up"] = daily_ma20 > ma20_prev

        # MA60
        if len(arr) >= 60:
            daily_ma60 = np.mean(arr[-60:])
            result["daily_ma60"] = float(daily_ma60)
            result["above_ma60"] = current_price >= daily_ma60

        # 25일 신고가
        n = min(25, len(arr))
        high_25d = np.max(arr[-n:])
        result["high_25d"] = float(high_25d)
        if high_25d > 0:
            result["near_high"] = current_price >= high_25d * (1.0 - near_high_threshold_pct / 100.0)

        return result

    @staticmethod
    def get_h1_trend(
        h1_closes: list[float],
        h1_highs:  list[float] | None = None,
        h1_lows:   list[float] | None = None,
    ) -> dict:
        """60분봉 추세 판정.

        반환 dict:
          trend   (int)   — trend_lv 0~3 (get_trend_status 기준)
          slope   (float) — EMA10 기울기 (양수=상승, 음수=하락)
          rsi     (float|None) — RSI14
          above_ema20 (bool) — 현재가(마지막 종가)가 EMA20 위인가
          direction (str) — "UP" | "DOWN" | "FLAT"
        """
        result = {
            "trend": 0, "slope": 0.0, "rsi": None,
            "above_ema20": False, "direction": "FLAT",
        }
        if len(h1_closes) < 5:
            return result

        h = h1_highs or h1_closes
        l = h1_lows  or h1_closes

        # trend_lv
        result["trend"] = IndicatorService.get_trend_status(h1_closes, h, l, [])

        # EMA10 기울기
        if len(h1_closes) >= 10:
            ema_now  = IndicatorService.calc_ema(h1_closes, 10)
            ema_prev = IndicatorService.calc_ema(h1_closes[:-1], 10)
            if ema_now and ema_prev:
                result["slope"] = ema_now - ema_prev

        # EMA20 위/아래
        if len(h1_closes) >= 20:
            ema20 = IndicatorService.calc_ema(h1_closes, 20)
            if ema20:
                result["above_ema20"] = h1_closes[-1] > ema20

        # RSI
        if len(h1_closes) >= 15:
            result["rsi"] = IndicatorService.calc_rsi(h1_closes, 14)

        # 방향
        if result["slope"] > 0 and result["above_ema20"]:
            result["direction"] = "UP"
        elif result["slope"] < 0 or not result["above_ema20"]:
            result["direction"] = "DOWN"
        else:
            result["direction"] = "FLAT"

        return result

    @staticmethod
    def build_5min_closes(closes_1min: list[float], volumes_1min: list[int] | None = None) -> tuple[list[float], list[int]]:
        """1분봉 closes/volumes를 5분봉으로 집계한다.
        반환: (5분봉 종가 리스트, 5분봉 거래량 리스트) — 최신 봉이 마지막.
        현재 미완성 봉(나머지 < 5개)은 포함하지 않는다.
        """
        n = len(closes_1min)
        full_bars = n // 5
        if full_bars == 0:
            return [], []
        c5, v5 = [], []
        vols = volumes_1min if volumes_1min and len(volumes_1min) == n else [0] * n
        for i in range(full_bars):
            start = i * 5
            end = start + 5
            c5.append(closes_1min[end - 1])        # 봉 종가 = 5번째 1분봉 종가
            v5.append(sum(vols[start:end]))
        return c5, v5

    @staticmethod
    def get_mtf_trend(
        closes_1min: list[float],
        volumes_1min: list[int] | None = None,
        highs_1min: list[float] | None = None,
        lows_1min: list[float] | None = None,
    ) -> dict:
        """멀티타임프레임 추세 판정.

        반환 dict:
          aligned     (bool)  — 1분/5분 추세 방향이 일치하는가
          tf1_slope   (float) — 1분봉 EMA10 기울기 (현재 - 1봉 전, 양수=상승)
          tf5_slope   (float) — 5분봉 EMA10 기울기
          tf1_trend   (int)   — 1분봉 trend_lv (0~3)
          tf5_trend   (int)   — 5분봉 trend_lv (0~3)
          tf5_bars    (int)   — 사용 가능한 5분봉 수
        """
        result = {
            "aligned": False,
            "tf1_slope": 0.0,
            "tf5_slope": 0.0,
            "tf1_trend": 0,
            "tf5_trend": 0,
            "tf5_bars": 0,
        }
        if len(closes_1min) < 10:
            return result

        # ── 1분봉 지표
        ema1_now  = IndicatorService.calc_ema(closes_1min, 10)
        ema1_prev = IndicatorService.calc_ema(closes_1min[:-1], 10)
        if ema1_now and ema1_prev:
            result["tf1_slope"] = ema1_now - ema1_prev

        # 1분봉 trend_lv
        h1 = highs_1min or closes_1min
        l1 = lows_1min  or closes_1min
        result["tf1_trend"] = IndicatorService.get_trend_status(closes_1min, h1, l1, volumes_1min or [])

        # ── 5분봉 집계
        c5, v5 = IndicatorService.build_5min_closes(closes_1min, volumes_1min)
        result["tf5_bars"] = len(c5)
        if len(c5) < 3:
            # 5분봉 부족 — 1분봉만으로 판단 (aligned=True 로 차단하지 않음)
            result["aligned"] = result["tf1_slope"] > 0
            return result

        ema5_now  = IndicatorService.calc_ema(c5, min(10, len(c5)))
        ema5_prev = IndicatorService.calc_ema(c5[:-1], min(10, len(c5) - 1)) if len(c5) > 1 else None
        if ema5_now and ema5_prev:
            result["tf5_slope"] = ema5_now - ema5_prev

        # 5분봉 trend_lv (highs/lows 없으면 closes로 근사)
        h5 = [max(closes_1min[i*5:(i+1)*5]) for i in range(len(c5))] if highs_1min and len(highs_1min) == len(closes_1min) else c5
        l5 = [min(closes_1min[i*5:(i+1)*5]) for i in range(len(c5))] if lows_1min  and len(lows_1min)  == len(closes_1min) else c5
        result["tf5_trend"] = IndicatorService.get_trend_status(c5, h5, l5, v5)

        # ── 방향 일치 판정
        # 조건: 1분봉 EMA 상승 AND 5분봉 EMA 상승 (둘 다 기울기 양수)
        tf1_up = result["tf1_slope"] > 0
        tf5_up = result["tf5_slope"] > 0
        result["aligned"] = tf1_up and tf5_up

        return result

    @staticmethod
    def get_technical_summary(snap: 'StockSnapshot', cfg: 'SmartScannerConfig') -> dict[str, Any]:
        """종목의 모든 기술적 상태를 통합 반환"""
        closes = snap.closes_1min
        summary = {
            "rsi": IndicatorService.calc_rsi(closes, 14),
            "ema20": IndicatorService.calc_ema(closes, 20),
            "ma20": IndicatorService.calc_ma(closes, 20),
            "daily": IndicatorService.get_daily_context(snap.daily_closes, snap.current_price),
            "trend_level": snap.trend_level
        }
        bb = IndicatorService.calc_bollinger_bands(closes, 20)
        if bb: summary["bb"] = bb
        return summary

    @staticmethod
    def get_ai_features(snap: 'StockSnapshot', index_history: dict[str, list[float]] = None, config: any = None) -> dict[str, float]:
        """
        AI 모델 학습/추론에 사용할 19종의 정규화된 피처를 생성한다.

        피처 정의 (ML Trainer와 동기화):
          1-7: 기본 (RSI, EMA20, BB, VolSurge, 등락률, 체결강도, 추세)
          8-15: 고급 (가격모멘텀, 당일위치, 변동성, MA정배열, RS스코어, VWAP, MTF)
          16-19: 캔들 패턴 (Body, UpperTail, LowerTail) + 호가비율
        """
        features = {}
        closes = snap.closes_1min
        if not closes or len(closes) < 20:
            return {}

        try:
            arr = np.array(closes)
            # 1. RSI (0~1)
            rsi = IndicatorService.calc_rsi(arr, 14)
            features["f_rsi"] = (rsi / 100.0) if rsi is not None else 0.5

            # 2. 이평선 이격도 (현재가/EMA20 - 1.0)
            ema20 = IndicatorService.calc_ema(arr, 20)
            if ema20 and ema20 > 0:
                gap = (snap.current_price / ema20) - 1.0
                features["f_ema20_gap"] = np.clip(gap * 5.0, -1.0, 1.0)
            else:
                features["f_ema20_gap"] = 0.0

            # 3. 볼린저 밴드 위치 (Percent B)
            bb = IndicatorService.calc_bollinger_bands(arr, 20)
            if bb and (bb["upper"] - bb["lower"]) > 0:
                pct_b = (snap.current_price - bb["lower"]) / (bb["upper"] - bb["lower"])
                features["f_pct_b"] = np.clip(pct_b, 0.0, 1.0)
            else:
                features["f_pct_b"] = 0.5

            # 4. 거래량 Surge (최근 1분 / 직전 20분 평균)
            vols = snap.volumes_1min
            if len(vols) >= 20:
                avg_vol = np.mean(vols[-20:-1])
                features["f_vol_surge"] = min(vols[-1] / avg_vol, 10.0) / 10.0 if avg_vol > 0 else 0.0
            else:
                features["f_vol_surge"] = 0.0

            # 5. 등락률 (0~30% -> 0~1)
            features["f_change_pct"] = np.clip(snap.change_pct / 30.0, -1.0, 1.0)

            # 6. 체결강도 (0~500 -> 0~1)
            features["f_strength"] = np.clip(snap.chejan_strength / 500.0, 0.0, 1.0)

            # 7. 추세 단계 (0~3 -> 0~1)
            features["f_trend"] = snap.trend_level / 3.0

            # 8. 가격 모멘텀 (최근 3분 변화율)
            if len(arr) >= 4:
                price_mom = (arr[-1] / arr[-4]) - 1.0
                features["f_price_mom"] = np.clip(price_mom * 10.0, -1.0, 1.0)
            else:
                features["f_price_mom"] = 0.0

            # 9. 당일 가격 범위 내 위치 (Intra-day Position)
            if snap.high_price > snap.low_price:
                intra_pos = (snap.current_price - snap.low_price) / (snap.high_price - snap.low_price)
                features["f_intra_pos"] = np.clip(intra_pos, 0.0, 1.0)
            else:
                features["f_intra_pos"] = 0.5

            # 10. 최근 10분 변동성 (Volatility)
            if len(arr) >= 10:
                recent_range = (np.max(arr[-10:]) - np.min(arr[-10:])) / snap.current_price
                features["f_volatility"] = np.clip(recent_range * 20.0, 0.0, 1.0)
            else:
                features["f_volatility"] = 0.0

            # 11. 이평선 정배열도 (MA Alignment)
            ma5 = np.mean(arr[-5:])
            ma10 = np.mean(arr[-10:])
            ma20 = np.mean(arr[-20:])
            if ma5 > ma10 > ma20:
                features["f_ma_align"] = 1.0
            elif ma5 > ma10:
                features["f_ma_align"] = 0.5
            else:
                features["f_ma_align"] = 0.0

            # 12. 시장 지수 대비 강도 (Relative Strength)
            features["f_rs_score"] = np.clip(snap.rs_score / 5.0, -1.0, 1.0)

            # 13. VWAP 대비 이격도
            vols_arr = np.array(snap.volumes_1min)
            vwap = IndicatorService.calc_vwap(arr, vols_arr)
            if vwap and vwap > 0:
                vwap_dist = (snap.current_price / vwap) - 1.0
                features["f_vwap_dist"] = np.clip(vwap_dist * 20.0, -1.0, 1.0)
            else:
                features["f_vwap_dist"] = 0.0

            # 14. MTF 15분봉 이격도
            if len(arr) >= 200:
                ema_15m_20 = IndicatorService.calc_ema(arr, 300)
                if ema_15m_20 and ema_15m_20 > 0:
                    mtf_15m_gap = (snap.current_price / ema_15m_20) - 1.0
                    features["f_mtf_15m_gap"] = np.clip(mtf_15m_gap * 10.0, -1.0, 1.0)
                else:
                    features["f_mtf_15m_gap"] = 0.0
            else:
                features["f_mtf_15m_gap"] = 0.0

            # 15. MTF 60분봉 이격도
            ema_60m_trend = IndicatorService.calc_ema(arr, min(len(arr), 400))
            if ema_60m_trend and ema_60m_trend > 0:
                mtf_60m_gap = (snap.current_price / ema_60m_trend) - 1.0
                features["f_mtf_60m_gap"] = np.clip(mtf_60m_gap * 5.0, -1.0, 1.0)
            else:
                features["f_mtf_60m_gap"] = 0.0

            # 16-18. 캔들 패턴 분석 (Body, UpperTail, LowerTail)
            c_list = snap.closes_1min
            o_list = snap.opens_1min
            if len(c_list) >= 1 and len(o_list) >= 1:
                curr_c, curr_o = c_list[-1], o_list[-1]
                curr_h = snap.highs_1min[-1] if snap.highs_1min else curr_c
                curr_l = snap.lows_1min[-1] if snap.lows_1min else curr_c

                candle_range = curr_h - curr_l
                if candle_range > 0:
                    features["f_candle_body"] = (curr_c - curr_o) / candle_range
                    features["f_candle_upper_tail"] = (curr_h - max(curr_o, curr_c)) / candle_range
                    features["f_candle_lower_tail"] = (min(curr_o, curr_c) - curr_l) / candle_range
                else:
                    features["f_candle_body"] = 0.0
                    features["f_candle_upper_tail"] = 0.0
                    features["f_candle_lower_tail"] = 0.0
            else:
                features["f_candle_body"] = 0.0
                features["f_candle_upper_tail"] = 0.0
                features["f_candle_lower_tail"] = 0.0

            # 19. 호가 잔량 비율 (Bid-Ask Imbalance)
            total_ask = getattr(snap, "total_ask_qty", 0)
            total_bid = getattr(snap, "total_bid_qty", 0)
            if total_bid > 0:
                hoga_ratio = total_ask / total_bid
                features["f_hoga_ratio"] = np.clip(hoga_ratio / 3.0, 0.0, 1.0)
            else:
                features["f_hoga_ratio"] = 0.0

        except Exception as e:
            logger.error(f"AI 피처 생성 실패: {snap.code}, {e}")

        return features
