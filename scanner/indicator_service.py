"""
IndicatorService — 기술지표 계산 고속화 및 AI 피처 생성
"""

from __future__ import annotations
import logging
import numpy as np
import pandas as pd
from typing import Optional, Any, TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

logger = logging.getLogger(__name__)

class IndicatorService:
    """기술지표 계산 서비스 — 모든 지표 계산을 고속화하여 담당"""

    @staticmethod
    def calc_rsi(closes: list[float] | np.ndarray, period: int = 14) -> Optional[float]:
        """Wilder's Smoothing 방식의 RSI 고속 계산"""
        if closes is None or len(closes) < period + 1:
            return None
        try:
            if isinstance(closes, list):
                arr = np.array(closes, dtype=np.float64)
            else:
                arr = closes.astype(np.float64)
                
            deltas = np.diff(arr)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)

            if len(gains) < period:
                return None

            # Wilder's Smoothing (alpha = 1/period)
            # pandas ewm(alpha=1/period, adjust=False) 사용
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
    def calc_ema(closes: list[float] | np.ndarray, period: int) -> Optional[float]:
        """고속 EMA 계산"""
        if closes is None or len(closes) < period:
            return None
        try:
            s = pd.Series(closes)
            return float(s.ewm(span=period, adjust=False).mean().iloc[-1])
        except Exception:
            return None

    @staticmethod
    def calc_ma(closes: list[float] | np.ndarray, period: int) -> Optional[float]:
        """고속 MA 계산"""
        if closes is None or len(closes) < period:
            return None
        return float(np.mean(closes[-period:]))

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
        
        if dist_atr >= 1.5: return 3
        if dist_atr >= 1.0: return 2
        if dist_atr >= 0.3: return 1
        return 0

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
        AI 모델 학습/추론에 사용할 20종 이상의 정규화된 피처를 생성한다.
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

            # --- [NEW] 고급 피처 추가 ---
            
            # 8. 가격 모멘텀 (최근 3분 변화율)
            if len(arr) >= 4:
                price_mom = (arr[-1] / arr[-4]) - 1.0
                features["f_price_mom"] = np.clip(price_mom * 10.0, -1.0, 1.0) # 10% 변화 시 1.0
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
                features["f_volatility"] = np.clip(recent_range * 20.0, 0.0, 1.0) # 5% 변동 시 1.0
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
            # snap.rs_score 활용 (Stock% - Index%)
            features["f_rs_score"] = np.clip(snap.rs_score / 5.0, -1.0, 1.0) # 5% 초과 달성 시 1.0

            # 13. VWAP 대비 이격도
            vols = np.array(snap.volumes_1min)
            vwap = IndicatorService.calc_vwap(arr, vols)
            if vwap and vwap > 0:
                vwap_dist = (snap.current_price / vwap) - 1.0
                features["f_vwap_dist"] = np.clip(vwap_dist * 20.0, -1.0, 1.0) # 5% 이격 시 1.0
            else:
                features["f_vwap_dist"] = 0.0

            # 14. 다중 시간 프레임 (MTF) 분석 - AI 학습용
            # 15분봉 EMA20 추정 (1분봉 300개 사용)
            if len(arr) >= 200: # 최소 200개 이상일 때만 신뢰
                ema_15m_20 = IndicatorService.calc_ema(arr, 300)
                if ema_15m_20 and ema_15m_20 > 0:
                    mtf_15m_gap = (snap.current_price / ema_15m_20) - 1.0
                    features["f_mtf_15m_gap"] = np.clip(mtf_15m_gap * 10.0, -1.0, 1.0) # 10% 이격 시 1.0
                else:
                    features["f_mtf_15m_gap"] = 0.0
            else:
                features["f_mtf_15m_gap"] = 0.0

            # 60분봉 EMA20 추정 (1분봉 1200개... 데이터 부족 시 가능한 최대치 사용)
            # 여기서는 60분봉의 대략적 추세를 위해 1분봉 400개를 최대한 활용 (약 60분 EMA7에 해당)
            ema_60m_trend = IndicatorService.calc_ema(arr, min(len(arr), 400))
            if ema_60m_trend and ema_60m_trend > 0:
                mtf_60m_gap = (snap.current_price / ema_60m_trend) - 1.0
                features["f_mtf_60m_gap"] = np.clip(mtf_60m_gap * 5.0, -1.0, 1.0)
            else:
                features["f_mtf_60m_gap"] = 0.0

            # 15. 호가 잔량 분석 (Bid-Ask Imbalance) - AI 학습용
            total_ask = getattr(snap, "total_ask_qty", 0)
            total_bid = getattr(snap, "total_bid_qty", 0)
            if total_bid > 0:
                hoga_ratio = total_ask / total_bid
                # 보통 매도잔량이 많을 때 매수 에너지가 강함 (흡수)
                features["f_hoga_ratio"] = np.clip(hoga_ratio / 3.0, 0.0, 1.0) 
            else:
                features["f_hoga_ratio"] = 0.0

            # 16. 캔들 패턴 분석 (마지막 완성봉 기준)
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

            # 17. 지수 가속도 (Market Velocity)
            if index_history:
                # 종목의 시장(코스피/코스닥) 결정
                m_type = "KOSDAQ" if getattr(snap, "market_type", "10") == "10" else "KOSPI"
                hist = index_history.get(m_type, [])
                if len(hist) >= 3:
                    velocity = hist[-1] - hist[-3] # 3분간의 변화량
                    features["f_index_velocity"] = np.clip(velocity * 5.0, -1.0, 1.0)
                else:
                    features["f_index_velocity"] = 0.0
            else:
                features["f_index_velocity"] = 0.0

            # 18. 호가 유동성 점수 (Liquidity Depth)
            # 주문 금액 대비 매수 호가 잔량 확인
            target_amount = 1_500_000 # 기본 주문 금액 150만원
            if config:
                target_amount = getattr(config, "fixed_order_amount", 1_500_000)
            
            total_bid_val = getattr(snap, "total_bid_qty", 0) * snap.current_price
            if target_amount > 0:
                liq_score = total_bid_val / (target_amount * 10) # 주문량의 10배 이상이면 1.0
                features["f_liquidity_score"] = np.clip(liq_score, 0.0, 1.0)
            else:
                features["f_liquidity_score"] = 1.0

            # 19. VI 거리 (VI Distance)
            # 정적 VI 기준 (시가 대비 10% 단위로 가정 - 단순화)
            if snap.open_price > 0:
                vi_price = snap.open_price * 1.1 # 1차 정적 VI
                if snap.current_price > vi_price: # 이미 1차 통과 시 2차 (20%)
                     vi_price = snap.open_price * 1.2
                
                dist_to_vi = (vi_price / snap.current_price) - 1.0
                features["f_vi_distance"] = np.clip(dist_to_vi * 10.0, 0.0, 1.0) # 10% 남았을 때 1.0, 0%일 때 0.0
            else:
                features["f_vi_distance"] = 0.5

            # 20. 수급 지속성 (Strength Continuity)
            chejan_hist = getattr(snap, "chejan_history", [])
            if len(chejan_hist) >= 3:
                avg_chejan = sum(chejan_hist[-3:]) / 3
                continuity = snap.chejan_strength / (avg_chejan + 0.1)
                features["f_strength_continuity"] = np.clip(continuity / 2.0, 0.0, 1.0)
            else:
                features["f_strength_continuity"] = 0.5

        except Exception as e:
            logger.error(f"AI 피처 생성 실패: {snap.code}, {e}")
            
        return features
