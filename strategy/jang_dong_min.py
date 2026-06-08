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
    stop_loss_pct: float = -2.0
    take_profit_pct: float = 5.0
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

    # [NEW 2026-05-26] 손절 이력 디스크 저장 경로 (당일 재시작 시 복원용)
    _LOSS_EXIT_FILE = "params/loss_exit_history.json"

    def __init__(self, order_mgr: OrderManager, risk_mgr: RiskManager, scan_cfg: Any, snap_store: Any = None):
        super().__init__(order_mgr, risk_mgr, scan_cfg)
        self._snap_store = snap_store

        # 손절 종목 추적 (동일 종목 재진입 방지): {code: loss_exit_time}
        self._loss_exit_dict: dict[str, datetime] = {}
        # [NEW 2026-05-26] 시작 시 디스크에서 당일 손절 이력 복원
        self._load_loss_exit_dict()

    def should_entry(self, sig: ScanSignal, auto_trading: bool) -> tuple[bool, str]:
        """진입 필터링 로직 (기존 app/strategy.py 로직 통합)"""
        # 1. 시스템 상태 체크
        # [FIX 2026-05-12] auto_trading 체크 제거 — 신호 즉시 처리 + 데이터 수집 목표
        # if not auto_trading:
        #     return False, "자동매매 OFF"

        # [FIX 2026-05-12] 손익한도 체크 제거 — 데이터 수집 목표상 모든 신호 포착
        # if self._risk_mgr.is_new_entry_locked:
        #     return False, "신규 매수 락 (손익한도)"
        #
        # if self._risk_mgr.is_daily_loss_cut_done:
        #     return False, "손절 한도 도달"

        # 2. 포지션 한도 체크
        max_pos = getattr(self._scan_cfg, "max_positions", getattr(self._order_mgr, "max_positions", 10))
        if len(self._order_mgr.positions) >= max_pos:
            return False, f"포지션 {max_pos}개 풀"

        # 3. 중복 진입 방지
        if sig.code in self._order_mgr.positions:
            return False, "이미 보유 중"

        # 4. [NEW 2026-05-19 / 강화 2026-05-26] 손절 종목 복구 대기 (동일 종목 재진입 방지 — 60분 냉각)
        # 5/26 분석: 빛과전자/스피어 등 1차 손절 후 즉시 재진입 → 재손절 패턴 발견 → 20분 → 60분 연장
        loss_exit_time = self._loss_exit_dict.get(sig.code)
        if loss_exit_time:
            elapsed_min = (datetime.now() - loss_exit_time).total_seconds() / 60.0
            loss_cooldown_min = float(getattr(self._scan_cfg, "loss_exit_cooldown_minutes", 60.0))
            if elapsed_min < loss_cooldown_min:
                remaining = loss_cooldown_min - elapsed_min
                return False, f"손절 복구 대기 ({remaining:.0f}분)"
            else:
                # 냉각 기간 종료 → 기록 삭제
                del self._loss_exit_dict[sig.code]

        # 5. 섹터 쏠림 확인
        sector = getattr(sig, "sector", "")
        if sector and self._has_sector_overweight(sector):
            return False, f"섹터 쏠림 ({sector})"

        # 6. 예수금 부족 체크 (기본 1주 기준 — 실제 주문은 OrderManager에서 결정)
        min_required_cash = sig.price * 1  # 최소 1주 매수 가능 여부 확인
        available_cash = self._order_mgr.available_cash
        if available_cash < min_required_cash:
            return False, f"예수금 부족 ({available_cash:,} < {min_required_cash:,})"

        return True, "OK"

    def _has_sector_overweight(self, sector: str) -> bool:
        sector_count = sum(
            1 for pos in self._order_mgr.positions.values()
            if getattr(pos, "sector", "") == sector
        )
        return sector_count >= 3

    def mark_loss_exit(self, pos: Any) -> None:
        """손절 종목을 기록하여 동일 종목 재진입 방지 (60분 냉각, 2026-05-26 강화)"""
        if not pos:
            return
        # 손절 (손익 < 0)일 때만 기록
        if float(getattr(pos, "price_change_pct_vs_avg", 0.0)) < 0:
            self._loss_exit_dict[pos.code] = datetime.now()
            from logging_config import order_log
            order_log.info("[전략] %s(%s) 손절 기록 — 60분간 재진입 차단", pos.code, pos.name)
            # [NEW 2026-05-26] 디스크 저장 (재시작 시 복원용)
            self._save_loss_exit_dict()

    def _save_loss_exit_dict(self) -> None:
        """[NEW 2026-05-26] 손절 이력 디스크 저장 — 프로그램 재시작 시 복원 가능

        형식: {"date": "2026-05-26", "entries": {"010170": "2026-05-26T09:02:07", ...}}
        """
        try:
            import json, os
            os.makedirs(os.path.dirname(self._LOSS_EXIT_FILE), exist_ok=True)
            data = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "entries": {
                    code: dt.isoformat() for code, dt in self._loss_exit_dict.items()
                },
            }
            with open(self._LOSS_EXIT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[손절이력저장실패] %s", e)

    def _load_loss_exit_dict(self) -> None:
        """[NEW 2026-05-26] 시작 시 디스크에서 당일 손절 이력 복원

        당일 데이터만 유효 (날짜가 다르면 무시).
        """
        try:
            import json, os
            if not os.path.exists(self._LOSS_EXIT_FILE):
                return
            with open(self._LOSS_EXIT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_date = data.get("date", "")
            today = datetime.now().strftime("%Y-%m-%d")
            if saved_date != today:
                logger.info("[손절이력로드] 날짜 불일치(%s ≠ %s) — 무시", saved_date, today)
                return
            entries = data.get("entries", {})
            for code, iso_str in entries.items():
                try:
                    self._loss_exit_dict[code] = datetime.fromisoformat(iso_str)
                except Exception:
                    pass
            if entries:
                logger.info("[손절이력로드] 당일 %d종목 복원", len(self._loss_exit_dict))
        except Exception as e:
            logger.warning("[손절이력로드실패] %s", e)

    def update_state(self, pos: Any) -> None:
        """현재가 기반 peak_price 갱신 (기존 ExitStrategy.update_peak_price 통합)"""
        if not pos or pos.current_price <= 0 or pos.avg_price <= 0:
            return
            
        activation = pos.avg_price * (1 + self._scan_cfg.trail_activation_pct / 100)
        if pos.current_price >= activation and pos.current_price > pos.peak_price:
            pos.peak_price = pos.current_price

    def _get_gap_dynamic_sl_tp(self, pos: Any) -> tuple[float, float]:
        """
        [2026-05-26] 갭 상승 크기에 비례한 동적 손절/익절 반환.
        장 초반(09:00~10:00) 진입 + 갭 ≥ 2% + gap_dynamic_sl_enabled=True 시 적용.
        반환: (sl_pct, tp_pct) — (0, 0)은 적용 안 함 (일반 ctx.sl_pct 사용).
        """
        if not getattr(self._scan_cfg, "gap_dynamic_sl_enabled", True):
            return 0.0, 0.0

        entry_time = getattr(pos, "entry_time", None)
        if not entry_time:
            return 0.0, 0.0

        from datetime import time as _t
        # 장 초반 갭 변동성 구간 (09:00~10:00) 진입 종목에 적용
        # 근거: 오늘(2026-05-26) 손절 12건 중 11건이 09:00~10:10 사이에 집중
        if not (_t(9, 0) <= entry_time.time() < _t(10, 0)):
            return 0.0, 0.0

        gap_pct = float(getattr(pos, "entry_gap_pct", 0.0) or 0.0)
        if gap_pct < 2.0:
            return 0.0, 0.0  # 갭 2% 미만은 일반 손절 적용

        tier1 = float(getattr(self._scan_cfg, "gap_sl_tier1_pct", 5.0))
        tier2 = float(getattr(self._scan_cfg, "gap_sl_tier2_pct", 10.0))

        if gap_pct < tier1:          # 갭 2~5%
            sl = float(getattr(self._scan_cfg, "gap_sl_tier1_stop", -2.0))
            tp = float(getattr(self._scan_cfg, "gap_tp_tier1_pct", 3.5))
        elif gap_pct < tier2:        # 갭 5~10%
            sl = float(getattr(self._scan_cfg, "gap_sl_tier2_stop", -2.5))
            tp = float(getattr(self._scan_cfg, "gap_tp_tier2_pct", 4.5))
        else:                        # 갭 10%+
            sl = float(getattr(self._scan_cfg, "gap_sl_tier3_stop", -3.0))
            tp = float(getattr(self._scan_cfg, "gap_tp_tier3_pct", 5.5))

        return sl, tp

    def should_exit(self, pos: Any, ctx: ExitContext) -> tuple[bool, str]:
        """청산 판정 로직 (기존 app/strategy.py 로직 통합)"""
        _is_eod_pre_gap = getattr(pos, "eod_trade", False) and not getattr(pos, "overnight_held", False)
        chg = float(pos.price_change_pct_vs_avg)
        # entry_time은 Hard Stop과 Time Cut에서 모두 참조하므로 함수 시작부에서 1회만 조회
        entry_time = getattr(pos, "entry_time", None)

        # [2026-05-26] 갭 동적 손절/익절 계산
        # 오늘 HPSP(-1.7% 손절 → 이후 +4.3%), 빛과전자(-1.1% 손절 → +3.9%), 대우건설(-1.3% → +4.6%)
        # 갭 종목은 장중 변동성이 크므로 고정 -1.5% 손절은 너무 좁음 → 갭 비례 확대
        _gap_sl, _gap_tp = self._get_gap_dynamic_sl_tp(pos)
        _use_gap_dynamic = _gap_sl != 0.0

        # 1. 하드 스탑 (절대 손절선) — EOD 포지션도 적용 (절대 손실 방어선 역할)
        # [P0-2 2026-05-21] OPENING 슬롯(09:00~09:30 진입) 종목은 -1.5%로 강화
        # [2026-05-26] 갭 동적 손절이 활성화된 경우 갭 기준 손절 사용 (더 넓은 여유 허용)
        # 갭 동적 시간 범위(09:00~10:00)와 OPENING 강화 범위(09:00~09:30)가 다름에 주의
        _hard_stop = float(self._scan_cfg.hard_stop_pct)
        if entry_time:
            from datetime import time as _dtime
            _et = entry_time.time()
            if _use_gap_dynamic:
                # 갭 동적 활성 (09:00~10:00 갭 종목): Hard Stop = gap_sl × 1.5
                # 예: gap_sl=-2.5% → hard_stop=-3.75% (음수 비교, min으로 더 깊은 값 선택)
                _hard_stop = min(_hard_stop, _gap_sl * 1.5)
            elif _dtime(9, 0) <= _et <= _dtime(9, 30):
                # 갭 비활성(갭<2%) + OPENING 진입: 기존 -1.5% 강화 유지
                _hard_stop = max(_hard_stop, -1.5)
        if chg <= _hard_stop:
            return True, f"Hard Stop ({_hard_stop:.1f}%)"

        # 2. 트레일 스탑 — 우선 순위 높음 (활성화되면 익절 무시)
        trail_price = 0
        if not _is_eod_pre_gap:
            trail_price = self.get_trail_price(pos)
            if trail_price > 0 and pos.current_price <= trail_price:
                return True, f"Trail Stop (Peak {pos.peak_price:,} -> {trail_price:,})"

        # 3. 익절 (Take Profit) — 트레일 스탑 미활성화 시에만
        if trail_price <= 0:  # 트레일 스탑이 활성화되지 않은 경우만
            if _use_gap_dynamic:
                # 갭 동적 익절 목표 사용
                _tp_pct = _gap_tp
                logger.debug("[갭동적익절] %s 갭%.1f%% → 손절%.1f%%/익절+%.1f%%",
                             getattr(pos, "name", "?"),
                             float(getattr(pos, "entry_gap_pct", 0.0)),
                             _gap_sl, _gap_tp)
            else:
                _tp_pct = float(getattr(self._scan_cfg, "jdm_take_profit_pct", getattr(self._scan_cfg, "take_profit_pct", 3.0)))
            if chg >= _tp_pct:
                return True, f"Take Profit ({_tp_pct:.1f}%)"

        # 4. 일반 손절 (EMA 보호 포함)
        # 갭 동적 활성 시 sl_pct를 갭 기준으로 교체
        _sl_pct = _gap_sl if _use_gap_dynamic else ctx.sl_pct
        if not _is_eod_pre_gap and chg <= _sl_pct:
            if self._check_ema_protection(pos):
                return False, "EMA20 Support (Hold)"
            tag = f"GAP동적({float(getattr(pos,'entry_gap_pct',0.0)):.0f}%갭)" if _use_gap_dynamic else ""
            return True, f"Stop Loss{' '+tag if tag else ''} ({_sl_pct:.1f}%)"

        # 5. 타임컷
        if ctx.time_cut_min > 0 and not getattr(pos, "eod_trade", False):
            strong_lv = int(getattr(self._scan_cfg, "strong_trend_hold_level", 3))
            exempt = (
                getattr(self._scan_cfg, "strong_trend_timecut_exempt", True)
                and int(getattr(pos, "trend_level", 0)) >= strong_lv
            )
            if not exempt:
                # entry_time은 L264에서 이미 정의됨 (재할당 불필요)
                if entry_time:
                    elapsed = (datetime.now() - entry_time).total_seconds() / 60
                    if elapsed >= ctx.time_cut_min:
                        return True, f"Time Cut ({elapsed:.1f}min)"

        # 6. 본절가 스탑 (분할익절 후 평단 이탈)
        if self._should_breakeven_stop(pos):
            return True, "본절가스탑"

        # 7. EMA20 이탈 청산
        if self._should_ema20_exit(pos):
            return True, "EMA20이탈"

        # 8. 추세소멸 익절
        if self._should_trend_decay(pos):
            return True, "추세소멸"

        # 9. 클라이맥스 탑 (단기 과열 익절)
        if self._should_climax_exit(pos):
            return True, "Climax Top (과열)"

        # 10. 거래량 실린 하락 (Distribution 차단)
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
                # 분할익절 완료 후 잔여 포지션은 Tier2 폭으로 여유 부여
                _tp = cfg.trail_pct_tier2 if getattr(pos, "partial_sold", False) else cfg.trail_pct_tier1
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

        # 진입 후 최소 3분은 보호 — 진입 직후 1분봉 노이즈로 즉시 청산되는 문제 방지
        entry_time = getattr(pos, "entry_time", None)
        if entry_time:
            elapsed_sec = (datetime.now() - entry_time).total_seconds()
            if elapsed_sec < 180:
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

# 기술지표 계산은 이제 scanner.indicator_service.IndicatorService 를 사용합니다.
