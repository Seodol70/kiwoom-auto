"""
Strategy — 매매 전략 엔진 (Entry / Exit 분리)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scanner.models import ScanSignal
    from app.risk_manager import RiskManager
    from order.order_manager import OrderManager

logger = logging.getLogger(__name__)

class EntryStrategy:
    """진입 필터링 전략 클래스"""
    def __init__(self, order_mgr: OrderManager, risk_mgr: RiskManager):
        self._order_mgr = order_mgr
        self._risk_mgr = risk_mgr

    def should_entry(self, sig: ScanSignal, auto_trading: bool) -> tuple[bool, str]:
        """진입 가능 여부 판단 및 사유 반환"""
        # 1. 자동매매 OFF
        if not auto_trading:
            return False, "자동매매 OFF"

        # 2. 포지션 한도 (5개)
        if len(self._order_mgr.positions) >= 5:
            return False, "포지션 5개 풀"

        # 3. 신규 매수 락 (RiskManager)
        if self._risk_mgr.is_new_entry_locked:
            return False, "신규 매수 락 (손익한도)"
        
        if self._risk_mgr.is_daily_loss_cut_done:
            return False, "손절 한도 도달"

        # 4. 섹터 쏠림 확인
        sector = getattr(sig, "sector", "")
        if sector and self._has_sector_overweight(sector):
            return False, f"섹터 쏠림 ({sector})"

        # 5. 예수금 부족
        required_cash = sig.price * sig.qty
        available_cash = self._order_mgr.available_cash
        if available_cash < required_cash:
            return False, f"예수금 부족 ({available_cash:,} < {required_cash:,})"

        # 6. 중복 진입 방지
        if sig.code in self._order_mgr.positions:
            return False, "이미 보유 중"

        return True, "OK"

    def _has_sector_overweight(self, sector: str) -> bool:
        sector_count = sum(
            1 for pos in self._order_mgr.positions.values()
            if getattr(pos, "sector", "") == sector
        )
        return sector_count >= 3

@dataclass
class ExitContext:
    """청산 판정용 파라미터 (시간대별 오버라이드)"""
    sl_pct: float
    trail_activation: float
    trail_tier1: float
    trail_tier2: float
    trail_tier3: float
    time_cut_min: int
    partial_profit_pct: float = 0.0
    atr_trail_enabled: bool = False

class ExitStrategy:
    """청산 판정 전략 클래스 (손절, 익절, 트레일링, 타임컷)"""
    def __init__(self, scan_cfg: SmartScannerConfig, snap_store: Any = None):
        self._scan_cfg = scan_cfg
        self._snap_store = snap_store

    def get_trail_price(self, pos: Any) -> int:
        """현재 포지션의 트레일 스탑 가격 계산 (MainWindow 차트 표시용 공유)"""
        if not pos or pos.peak_price <= 0 or pos.avg_price <= 0:
            return 0
            
        peak_chg = (pos.peak_price - pos.avg_price) / pos.avg_price * 100
        cfg = self._scan_cfg
        
        if peak_chg < cfg.trail_activation_pct:
            return 0
            
        # 트레일 폭 결정
        # Strong Trend (Level 3+) 인 경우 더 넓은 폭(Tier 2부터 시작) 적용 가능
        strong_lv = int(getattr(cfg, "strong_trend_hold_level", 3))
        is_strong = int(getattr(pos, "trend_level", 0)) >= strong_lv
        
        if is_strong:
            # 강한 추세: tier1(좁음) 건너뛰고 tier2(보통)부터 적용하여 일시적 흔들림 버티기
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

    def update_peak_price(self, pos: Any) -> None:
        """현재가 기반 peak_price 갱신 (트레일 활성화 기준 이상일 때만 추적)"""
        if not pos or pos.current_price <= 0 or pos.avg_price <= 0:
            return
            
        activation = pos.avg_price * (1 + self._scan_cfg.trail_activation_pct / 100)
        if pos.current_price >= activation and pos.current_price > pos.peak_price:
            pos.peak_price = pos.current_price

    def should_exit(self, pos: Any, ctx: ExitContext) -> tuple[bool, str]:
        """청산 여부 및 사유 판단"""
        # 1. 하드 스탑 (절대 손절선)
        chg = float(pos.price_change_pct_vs_avg)
        if chg <= self._scan_cfg.hard_stop_pct:
            return True, f"Hard Stop ({self._scan_cfg.hard_stop_pct:.1f}%)"
            
        # 2. 트레일 스탑
        trail_price = self.get_trail_price(pos)
        if trail_price > 0 and pos.current_price <= trail_price:
            return True, f"Trail Stop (Peak {pos.peak_price:,} -> {trail_price:,})"
            
        # 3. 일반 손절 (EMA 보호 로직 포함)
        if chg <= ctx.sl_pct:
            if self._check_ema_protection(pos):
                return False, "EMA20 Support (Hold)"
            return True, f"Stop Loss ({ctx.sl_pct:.1f}%)"
            
        # 4. 타임컷 (보유 시간 초과)
        if ctx.time_cut_min > 0:
            from datetime import datetime
            elapsed = (datetime.now() - pos.entry_time).total_seconds() / 60
            if elapsed >= ctx.time_cut_min:
                return True, f"Time Cut ({elapsed:.1f}min)"
                
        return False, "HOLD"

    def _check_ema_protection(self, pos: Any) -> bool:
        """EMA20 지지 여부 확인 (추세 상승장용 보류 로직)"""
        if not getattr(self._scan_cfg, "trend_protect_enabled", True):
            return False
        if not self._snap_store:
            return False
            
        snap = self._snap_store.get_snapshot(pos.code)
        if not snap or not snap.closes_1min or len(snap.closes_1min) < 20:
            return False
            
        from scanner.indicator_service import IndicatorService
        ema20 = IndicatorService.calc_ema(snap.closes_1min, 20)
        if ema20 and pos.current_price > ema20:
            # 현재가가 EMA20 위에 있으면 지지 중으로 판단하여 손절 보류
            return True
        return False
