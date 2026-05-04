# -*- coding: utf-8 -*-
"""
장동민 전략 - 90분 단기 매매
BaseStrategy를 상속받아 구현된 구체적인 전략 클래스
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any, TYPE_CHECKING
import numpy as np

from strategy.base import BaseStrategy, ExitContext

if TYPE_CHECKING:
    from scanner.models import ScanSignal
    from app.risk_manager import RiskManager
    from order.order_manager import OrderManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    """전략 파라미터 — 백테스트 최적화 결과 적용"""
    ma_short: int = 7
    ma_long: int = 15
    rsi_period: int = 14
    rsi_oversold: float = 35.0
    rsi_overbought: float = 70.0
    bb_period: int = 20
    bb_std: float = 2.0
    holding_minutes: int = 60
    stop_loss_pct: float = -1.2
    take_profit_pct: float = 3.0
    order_qty: int = 1

# ---------------------------------------------------------------------------
# 상태
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """보유 포지션"""
    code: str
    name: str
    qty: int
    entry_price: float
    entry_time: datetime
    stop_loss: float = 0.0
    take_profit: float = 0.0


@dataclass
class StrategyState:
    """전략 실행 상태"""
    position: Optional[Position] = None
    last_signal: str = "NONE"   # BUY / SELL / HOLD / NONE
    last_updated: Optional[datetime] = None
    candles: list = field(default_factory=list)  # OHLCV 캔들 데이터

# ---------------------------------------------------------------------------
# 전략 클래스 구현
# ---------------------------------------------------------------------------

class JangDongMinStrategy(BaseStrategy):
    """
    장동민 전략 구현체.
    기술적 지표와 추세 분석을 결합한 단기 매매 전략입니다.
    """

    def __init__(self, order_mgr: OrderManager, risk_mgr: RiskManager, scan_cfg: Any, snap_store: Any = None):
        super().__init__(order_mgr, risk_mgr, scan_cfg)
        self._snap_store = snap_store

    def should_entry(self, sig: ScanSignal, auto_trading: bool) -> tuple[bool, str]:
        """진입 필터링 로직 (기존 app/strategy.py 로직 통합)"""
        # 1. 시스템 상태 체크
        if not auto_trading:
            return False, "자동매매 OFF"

        if self._risk_mgr.is_new_entry_locked:
            return False, "신규 매수 락 (손익한도)"
        
        if self._risk_mgr.is_daily_loss_cut_done:
            return False, "손절 한도 도달"

        # 2. 포지션 한도 체크
        max_pos = getattr(self._order_mgr, "max_positions", 5)
        if len(self._order_mgr.positions) >= max_pos:
            return False, f"포지션 {max_pos}개 풀"

        # 3. 중복 진입 방지
        if sig.code in self._order_mgr.positions:
            return False, "이미 보유 중"

        # 4. 섹터 쏠림 확인
        sector = getattr(sig, "sector", "")
        if sector and self._has_sector_overweight(sector):
            return False, f"섹터 쏠림 ({sector})"

        # 5. 예수금 부족 체크
        required_cash = sig.price * sig.qty
        available_cash = self._order_mgr.available_cash
        if available_cash < required_cash:
            return False, f"예수금 부족 ({available_cash:,} < {required_cash:,})"

        return True, "OK"

    def _has_sector_overweight(self, sector: str) -> bool:
        sector_count = sum(
            1 for pos in self._order_mgr.positions.values()
            if getattr(pos, "sector", "") == sector
        )
        return sector_count >= 3

    def update_state(self, pos: Any) -> None:
        """현재가 기반 peak_price 갱신 (기존 ExitStrategy.update_peak_price 통합)"""
        if not pos or pos.current_price <= 0 or pos.avg_price <= 0:
            return
            
        activation = pos.avg_price * (1 + self._scan_cfg.trail_activation_pct / 100)
        if pos.current_price >= activation and pos.current_price > pos.peak_price:
            pos.peak_price = pos.current_price

    def should_exit(self, pos: Any, ctx: ExitContext) -> tuple[bool, str]:
        """청산 판정 로직 (기존 app/strategy.py 로직 통합)"""
        _is_eod_pre_gap = getattr(pos, "eod_trade", False) and not getattr(pos, "overnight_held", False)
        chg = float(pos.price_change_pct_vs_avg)

        # 1. 하드 스탑 (절대 손절선)
        if chg <= self._scan_cfg.hard_stop_pct:
            return True, f"Hard Stop ({self._scan_cfg.hard_stop_pct:.1f}%)"

        # 2. 트레일 스탑
        if not _is_eod_pre_gap:
            trail_price = self.get_trail_price(pos)
            if trail_price > 0 and pos.current_price <= trail_price:
                return True, f"Trail Stop (Peak {pos.peak_price:,} -> {trail_price:,})"

        # 3. 일반 손절 (EMA 보호 포함)
        if not _is_eod_pre_gap and chg <= ctx.sl_pct:
            if self._check_ema_protection(pos):
                return False, "EMA20 Support (Hold)"
            return True, f"Stop Loss ({ctx.sl_pct:.1f}%)"

        # 4. 타임컷
        if ctx.time_cut_min > 0 and not getattr(pos, "eod_trade", False):
            strong_lv = int(getattr(self._scan_cfg, "strong_trend_hold_level", 3))
            exempt = (
                getattr(self._scan_cfg, "strong_trend_timecut_exempt", True)
                and int(getattr(pos, "trend_level", 0)) >= strong_lv
            )
            if not exempt:
                entry_time = getattr(pos, "entry_time", None)
                if entry_time:
                    elapsed = (datetime.now() - entry_time).total_seconds() / 60
                    if elapsed >= ctx.time_cut_min:
                        return True, f"Time Cut ({elapsed:.1f}min)"

        # 5. 본절가 스탑 (분할익절 후 평단 이탈)
        if self._should_breakeven_stop(pos):
            return True, "본절가스탑"

        # 6. EMA20 이탈 청산
        if self._should_ema20_exit(pos):
            return True, "EMA20이탈"

        # 7. 추세소멸 익절
        if self._should_trend_decay(pos):
            return True, "추세소멸"

        # 8. 클라이맥스 탑 (단기 과열 익절)
        if self._should_climax_exit(pos):
            return True, "Climax Top (과열)"

        # 9. 거래량 실린 하락 (Distribution 차단)
        if self._should_distribution_exit(pos):
            return True, "Distribution (세력이탈)"

        return False, "HOLD"

    def should_partial_exit(self, pos: Any, ctx: ExitContext) -> tuple[bool, float]:
        """분할 익절 여부 판단"""
        if not getattr(self._scan_cfg, "partial_profit_enabled", False):
            return False, 0.0
        if getattr(pos, "partial_sold", False):
            return False, 0.0
        if ctx.partial_profit_pct <= 0:
            return False, 0.0
        if float(pos.price_change_pct_vs_avg) >= ctx.partial_profit_pct:
            ratio = float(getattr(self._scan_cfg, "partial_sell_ratio", 0.30))
            return True, ratio
        return False, 0.0

    # ─── 내부 유틸리티 ──────────────────────────────────────────────────

    def get_trail_price(self, pos: Any) -> int:
        """트레일 스탑 가격 계산"""
        if not pos or pos.peak_price <= 0 or pos.avg_price <= 0:
            return 0
            
        peak_chg = (pos.peak_price - pos.avg_price) / pos.avg_price * 100
        cfg = self._scan_cfg
        
        if peak_chg < cfg.trail_activation_pct:
            return 0
            
        strong_lv = int(getattr(cfg, "strong_trend_hold_level", 3))
        is_strong = int(getattr(pos, "trend_level", 0)) >= strong_lv
        
        if is_strong:
            if peak_chg < cfg.trail_tier2_max:
                _tp = cfg.trail_pct_tier2
            else:
                _tp = cfg.trail_pct_tier3
        else:
            if peak_chg < cfg.trail_tier1_max:
                _tp = cfg.trail_pct_tier1
            elif peak_chg < cfg.trail_tier2_max:
                _tp = cfg.trail_pct_tier2
            else:
                _tp = cfg.trail_pct_tier3
                
        return int(pos.peak_price * (1 - _tp / 100))

    def _should_breakeven_stop(self, pos: Any) -> bool:
        if not getattr(self._scan_cfg, "breakeven_stop_enabled", False):
            return False
        if not getattr(pos, "partial_sold", False):
            return False
        buffer_pct = float(getattr(self._scan_cfg, "breakeven_stop_buffer_pct", 0.0))
        return float(pos.price_change_pct_vs_avg) <= buffer_pct

    def _should_ema20_exit(self, pos: Any) -> bool:
        if not getattr(self._scan_cfg, "ema20_exit_enabled", False):
            return False
        if not self._snap_store:
            return False
        snap = self._snap_store.get_snapshot(pos.code)
        if snap is None:
            return False
        closes = list(getattr(snap, "closes_1min", []) or [])
        if len(closes) < 20:
            return False
        from scanner.indicator_service import IndicatorService
        ema20 = IndicatorService.calc_ema(closes, 20)
        if not ema20:
            return False
        buffer_pct = float(getattr(self._scan_cfg, "ema20_exit_buffer_pct", 0.0))
        return pos.current_price < ema20 * (1 - buffer_pct / 100)

    def _should_trend_decay(self, pos: Any) -> bool:
        if getattr(pos, "eod_trade", False):
            return False
        if float(pos.price_change_pct_vs_avg) <= 0:
            return False
        if self._order_mgr is None:
            return False
        return self._order_mgr.should_exit_on_trend_decay(pos.code)

    def _check_ema_protection(self, pos: Any) -> bool:
        if not getattr(self._scan_cfg, "trend_protect_enabled", True):
            return False
        if not self._snap_store:
            return False
        snap = self._snap_store.get_snapshot(pos.code)
        if not snap or not snap.closes_1min or len(snap.closes_1min) < 20:
            return False
        from scanner.indicator_service import IndicatorService
        ema20 = IndicatorService.calc_ema(snap.closes_1min, 20)
        return bool(ema20 and pos.current_price > ema20)

    def _should_climax_exit(self, pos: Any) -> bool:
        """단기 급등 후 거래량 폭발 시 익절"""
        if float(pos.price_change_pct_vs_avg) < 10.0:  # 최소 10% 이상 수익 시에만 고려
            return False
            
        snap = self._snap_store.get_snapshot(pos.code) if self._snap_store else None
        if not snap or not snap.volumes_1min:
            return False
            
        # 최근 20분 평균 거래량 대비 4배 이상 폭발
        vols = list(snap.volumes_1min)
        if len(vols) < 21:
            return False
            
        avg_vol = np.mean(vols[-21:-1])
        cur_vol = vols[-1]
        
        # 주가 급등(당일 20% 이상 or 진입 후 15% 이상) + 거래량 4배
        is_surge = snap.change_pct >= 20.0 or float(pos.price_change_pct_vs_avg) >= 15.0
        is_vol_climax = cur_vol >= avg_vol * 4.0
        
        return is_surge and is_vol_climax

    def _should_distribution_exit(self, pos: Any) -> bool:
        """거래량 실린 음봉/하락 시 탈출 (손절가 도달 전이라도)"""
        snap = self._snap_store.get_snapshot(pos.code) if self._snap_store else None
        if not snap or not snap.volumes_1min or len(snap.volumes_1min) < 2:
            return False
            
        # 현재가가 직전가 대비 하락 (음봉 기조)
        price_drop = pos.current_price < snap.closes_1min[-1]
        
        # 거래량 평소보다 2.5배 이상 (세력 이탈 의심)
        avg_vol = np.mean(snap.volumes_1min[-21:-1]) if len(snap.volumes_1min) >= 21 else snap.volumes_1min[0]
        cur_vol = snap.volumes_1min[-1]
        is_high_vol = cur_vol >= avg_vol * 2.5
        
        # 수익권이 아닐 때 거래량 실린 하락은 위험 신호
        # 수익권일 때는 트레일 스탑이 있으므로 조금 더 여유를 줌
        if float(pos.price_change_pct_vs_avg) < 0 and price_drop and is_high_vol:
            return True
            
        return False

# ---------------------------------------------------------------------------
# 기술적 지표 계산 (유틸리티)
# ---------------------------------------------------------------------------

def calc_ma(closes: list[float], period: int) -> Optional[float]:
    from scanner.indicator_service import IndicatorService
    return IndicatorService.calc_ma(closes, period)

def calc_ema(closes: list[float], period: int) -> Optional[float]:
    from scanner.indicator_service import IndicatorService
    return IndicatorService.calc_ema(closes, period)

def calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
    from scanner.indicator_service import IndicatorService
    return IndicatorService.calc_atr(highs, lows, closes, period)

def get_trend_status(closes: list[float], highs: list[float], lows: list[float], volumes: list[int], **kwargs) -> int:
    from scanner.indicator_service import IndicatorService
    return IndicatorService.get_trend_status(closes, highs, lows, volumes, **kwargs)

def calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    arr = np.array(closes[-(period + 1):], dtype=np.float64)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

def calc_bollinger_bands(closes: list[float], period: int = 20, std_mult: float = 2.0) -> Optional[tuple[float, float, float]]:
    if len(closes) < period:
        return None
    arr = np.array(closes[-period:], dtype=np.float64)
    middle = float(arr.mean())
    std = float(arr.std())
    return middle + std_mult * std, middle, middle - std_mult * std

def calc_pivot_r2(prev_high: int, prev_low: int, prev_close: int) -> float:
    if prev_high <= 0 or prev_low <= 0 or prev_close <= 0:
        return 0.0
    pivot = (prev_high + prev_low + prev_close) / 3.0
    return pivot + (prev_high - prev_low)

def check_daily_alignment(daily_closes: list[float]) -> bool:
    if len(daily_closes) < 20:
        return False
    ma5 = sum(daily_closes[-5:]) / 5
    ma10 = sum(daily_closes[-10:]) / 10
    ma20 = sum(daily_closes[-20:]) / 20
    return ma5 > ma10 > ma20
