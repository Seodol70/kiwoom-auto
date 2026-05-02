"""Trading controller — 신호 필터링 + 청산 전략"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

if TYPE_CHECKING:
    from order.order_manager import OrderManager
    from scanner.models import ScanSignal
    from scanner.smart_scanner import SmartScannerConfig
    from app.risk_manager import RiskManager

logger = logging.getLogger(__name__)


@dataclass
class ExitContext:
    """청산 판정용 파라미터 (시간대별 오버라이드)"""

    sl_pct: float
    """손절 기준 수익률 (%)"""

    trail_activation: float
    """트레일 활성화 수익 (%)"""

    trail_tier1: float
    """트레일 1단계 폭 (%)"""

    trail_tier2: float
    """트레일 2단계 폭 (%)"""

    time_cut_min: int
    """타임컷 분"""


class TradingController(QObject):
    """
    거래 컨트롤러 — 신호 필터링 + 청산 판정.

    - handle_signal(): 신호 6단계 필터 체인
    - check_exit_*(): 포지션별 청산 조건 판정
    """

    signal_rejected = pyqtSignal(str)
    """신호 거절 사유"""

    def __init__(
        self,
        order_mgr,
        scan_cfg,
        risk_mgr,
        snap_store=None,
        parent=None,
    ):
        super().__init__(parent)
        self._order_mgr = order_mgr
        self._scan_cfg = scan_cfg
        self._risk_mgr = risk_mgr
        self._snap_store = snap_store
        self._auto_trading = False

    # ─── 신호 필터링 ──────────────────────────────────────────────────

    @pyqtSlot(object)
    def handle_signal(self, sig: ScanSignal) -> bool:
        """
        신호 필터링 체인 (6단계).

        Args:
            sig: SmartScanner 발행 신호

        Returns:
            True if signal passes all filters

        필터 순서 (critical — 변경 금지):
            1. _auto_trading 플래그 확인
            2. 포지션 수 한도 (5개)
            3. 신규 매수 락 확인 (loss cut / profit lock)
            4. 섹터 쏠림 확인
            5. 예수금 부족 확인
            6. 이미 보유 종목 중복 진입 방지
        """

        # 1️⃣ 자동매매 OFF → 신호 무시
        if not self._auto_trading:
            self.signal_rejected.emit(f"{sig.code}: 자동매매 OFF")
            return False

        # 2️⃣ 포지션 5개 풀
        if len(self._order_mgr.positions) >= 5:
            self.signal_rejected.emit(f"{sig.code}: 포지션 5개 풀")
            return False

        # 3️⃣ 손익 락 확인
        if self._risk_mgr.is_new_entry_locked:
            self.signal_rejected.emit(f"{sig.code}: 신규 매수 락 (손익한도)")
            return False

        if self._risk_mgr.is_daily_loss_cut_done:
            self.signal_rejected.emit(f"{sig.code}: 손절 한도 도달")
            return False

        # 4️⃣ 섹터 쏠림 확인
        sector = getattr(sig, "sector", "")
        if sector and self._has_sector_overweight(sector):
            self.signal_rejected.emit(f"{sig.code}: 섹터 쏠림 ({sector})")
            return False

        # 5️⃣ 예수금 부족 확인
        required_cash = sig.price * sig.qty
        available_cash = self._order_mgr.available_cash
        if available_cash < required_cash:
            self.signal_rejected.emit(
                f"{sig.code}: 예수금 부족 ({available_cash:,} < {required_cash:,})"
            )
            return False

        # 6️⃣ 중복 진입 방지
        if sig.code in self._order_mgr.positions:
            self.signal_rejected.emit(f"{sig.code}: 이미 보유 중")
            return False

        # ✅ 모든 필터 통과 → 진입 신호
        self._order_mgr.handle_signal(sig)
        return True

    # ─── 필터 헬퍼 ──────────────────────────────────────────────────

    def _has_sector_overweight(self, sector: str) -> bool:
        """섹터 쏠림 확인 (동일 섹터 3개 이상 보유)"""
        sector_count = sum(
            1
            for pos in self._order_mgr.positions.values()
            if getattr(pos, "sector", "") == sector
        )
        return sector_count >= 3

    # ─── 포지션 청산 판정 ──────────────────────────────────────────────

    def check_and_exit_all(self) -> None:
        """모든 포지션 청산 판정 (매분 호출)"""
        # 현재 시간 슬롯 감지
        now = datetime.now()
        exit_ctx = self._get_exit_context(now)

        for code, pos in list(self._order_mgr.positions.items()):
            if self._order_mgr.is_pending(code):
                continue

            qty_today = getattr(pos, "qty_buy_today_app", 0) or 0
            if qty_today <= 0:
                qty_today = pos.qty
            sell_qty = min(pos.qty, qty_today)
            if sell_qty <= 0:
                continue

            # 청산 판정 순서 (hard stop부터 시작)
            if self._check_hard_stop(pos, sell_qty):
                continue
            if self._check_candle_stop(pos, sell_qty):
                continue
            if self._check_stop_loss(pos, sell_qty, exit_ctx):
                continue
            if self._check_trail_stop(pos, sell_qty, exit_ctx):
                continue
            if self._check_time_cut(pos, sell_qty, exit_ctx):
                continue

    def _get_exit_context(self, now: datetime) -> ExitContext:
        """현재 시간에 따른 청산 파라미터 조회"""
        now_min = now.hour * 60 + now.minute
        _is_opening = (9 * 60) <= now_min < (9.5 * 60)
        _is_midday = (11 * 60) <= now_min < (13 * 60)

        if _is_opening:
            return ExitContext(
                sl_pct=float(
                    getattr(self._scan_cfg, "stop_loss_pct_opening", self._scan_cfg.stop_loss_pct)
                ),
                trail_activation=self._scan_cfg.trail_activation_pct,
                trail_tier1=self._scan_cfg.trail_pct_tier1,
                trail_tier2=self._scan_cfg.trail_pct_tier2,
                time_cut_min=self._scan_cfg.time_cut_minutes,
            )
        elif _is_midday:
            return ExitContext(
                sl_pct=float(
                    getattr(self._scan_cfg, "stop_loss_pct_midday", self._scan_cfg.stop_loss_pct)
                ),
                trail_activation=float(
                    getattr(
                        self._scan_cfg,
                        "trail_activation_pct_midday",
                        self._scan_cfg.trail_activation_pct,
                    )
                ),
                trail_tier1=float(
                    getattr(
                        self._scan_cfg, "trail_pct_tier1_midday", self._scan_cfg.trail_pct_tier1
                    )
                ),
                trail_tier2=float(
                    getattr(
                        self._scan_cfg, "trail_pct_tier2_midday", self._scan_cfg.trail_pct_tier2
                    )
                ),
                time_cut_min=int(
                    getattr(
                        self._scan_cfg, "time_cut_minutes_midday", self._scan_cfg.time_cut_minutes
                    )
                ),
            )
        else:
            return ExitContext(
                sl_pct=self._scan_cfg.stop_loss_pct,
                trail_activation=self._scan_cfg.trail_activation_pct,
                trail_tier1=self._scan_cfg.trail_pct_tier1,
                trail_tier2=self._scan_cfg.trail_pct_tier2,
                time_cut_min=self._scan_cfg.time_cut_minutes,
            )

    # ─── 개별 청산 판정 함수들 ──────────────────────────────────────────

    def _check_hard_stop(self, pos, sell_qty: int) -> bool:
        """Hard Stop: 손실 기준 즉시 강제 매도"""
        chg = float(pos.price_change_pct_vs_avg)
        if chg <= self._scan_cfg.hard_stop_pct:
            logger.warning(
                "[Hard Stop] %s(%s) 손실률 %.2f%% — 강제 매도 %d주",
                pos.name,
                pos.code,
                chg,
                sell_qty,
            )
            self._order_mgr.mark_stop_loss(pos.code)
            self._order_mgr.force_exit(
                pos.code, pos.name, sell_qty, reason=f"Hard Stop {self._scan_cfg.hard_stop_pct:.1f}%"
            )
            return True
        return False

    def _check_candle_stop(self, pos, sell_qty: int) -> bool:
        """캔들 저가 손절: 진입 캔들 저점 이탈"""
        # EOD 포지션 (갭 체크 이전)은 스킵
        _is_eod_pre_gap = getattr(pos, "eod_trade", False) and not getattr(
            pos, "overnight_held", False
        )
        if _is_eod_pre_gap:
            return False

        if pos.candle_stop_price > 0 and pos.current_price <= pos.candle_stop_price:
            logger.info(
                "[캔들손절] %s(%s) 현재가 %d ≤ 손절가 %d — %d주 매도",
                pos.name,
                pos.code,
                pos.current_price,
                pos.candle_stop_price,
                sell_qty,
            )
            self._order_mgr.mark_stop_loss(pos.code)
            self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
            return True
        return False

    def _check_stop_loss(self, pos, sell_qty: int, ctx: ExitContext) -> bool:
        """손절: 설정값 하한 도달"""
        # EOD 포지션 (갭 체크 이전)은 스킵
        _is_eod_pre_gap = getattr(pos, "eod_trade", False) and not getattr(
            pos, "overnight_held", False
        )
        if _is_eod_pre_gap:
            return False

        chg = float(pos.price_change_pct_vs_avg)
        if chg <= ctx.sl_pct:
            # EMA20 지지 확인 (보호 기능)
            if self._check_ema_protection(pos):
                logger.debug("[눌림목 보류] %s(%s) EMA20 지지 중", pos.name, pos.code)
                return False

            logger.info(
                "[손절] %s(%s) 손실률 %.2f%% — %d주 매도",
                pos.name,
                pos.code,
                chg,
                sell_qty,
            )
            self._order_mgr.mark_stop_loss(pos.code)
            self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
            return True
        return False

    def _check_trail_stop(self, pos, sell_qty: int, ctx: ExitContext) -> bool:
        """트레일 스탑: 고점 대비 하락"""
        # EOD 포지션 (갭 체크 이전)은 스킵
        _is_eod_pre_gap_trail = getattr(pos, "eod_trade", False) and not getattr(
            pos, "overnight_held", False
        )
        if _is_eod_pre_gap_trail:
            return False

        if pos.peak_price <= 0:
            return False

        chg = float(pos.price_change_pct_vs_avg)
        peak_chg = (pos.peak_price - pos.avg_price) / pos.avg_price * 100

        # 트레일 활성화 확인
        if peak_chg < ctx.trail_activation:
            return False

        # 트레일 폭 결정 (Strong Trend 포지션 특별 처리)
        strong_lv = int(getattr(self._scan_cfg, "strong_trend_hold_level", 3))
        is_strong_trend = int(getattr(pos, "trend_level", 0)) >= strong_lv

        if is_strong_trend:
            # Strong Trend: tier1 건너뛰고 tier2부터
            if peak_chg < self._scan_cfg.trail_tier2_max:
                trail_pct = ctx.trail_tier2
            else:
                trail_pct = self._scan_cfg.trail_pct_tier3
        else:
            if peak_chg < self._scan_cfg.trail_tier1_max:
                trail_pct = ctx.trail_tier1
            elif peak_chg < self._scan_cfg.trail_tier2_max:
                trail_pct = ctx.trail_tier2
            else:
                trail_pct = self._scan_cfg.trail_pct_tier3

        trail_price = int(pos.peak_price * (1 - trail_pct / 100))

        # 현재가가 트레일가 이하인가?
        if pos.current_price <= trail_price:
            # EMA20 지지 확인
            if self._check_ema_protection(pos):
                logger.debug("[눌림목 보류] %s(%s) EMA20 지지 중 (트레일)", pos.name, pos.code)
                return False

            trend_tag = "[Strong홀딩]" if is_strong_trend else ""
            logger.info(
                "[트레일스탑%s] %s(%s) 현재가 %d ≤ 트레일가 %d — %d주 청산",
                trend_tag,
                pos.name,
                pos.code,
                pos.current_price,
                trail_price,
                sell_qty,
            )
            self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
            return True

        return False

    def _check_time_cut(self, pos, sell_qty: int, ctx: ExitContext) -> bool:
        """타임컷: 경과 시간 기준 강제 청산"""
        # EOD 포지션은 타임컷 제외
        if getattr(pos, "eod_trade", False):
            return False

        # Strong Trend 포지션 타임컷 면제
        strong_lv = int(getattr(self._scan_cfg, "strong_trend_hold_level", 3))
        timecut_exempt = (
            getattr(self._scan_cfg, "strong_trend_timecut_exempt", True)
            and int(getattr(pos, "trend_level", 0)) >= strong_lv
        )
        if timecut_exempt:
            return False

        entry_time = getattr(pos, "entry_time", None)
        if not entry_time:
            return False

        elapsed_min = (datetime.now() - entry_time).total_seconds() / 60
        if elapsed_min >= ctx.time_cut_min:
            chg = float(pos.price_change_pct_vs_avg)
            logger.info(
                "[타임컷] %s(%s) 경과 %d분, 수익 %.2f%% — %d주 강제 청산",
                pos.name,
                pos.code,
                int(elapsed_min),
                chg,
                sell_qty,
            )
            self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
            return True

        return False

    # ─── 보호 기능 ──────────────────────────────────────────────────

    def _check_ema_protection(self, pos) -> bool:
        """EMA20 지지 확인 — 추세 지지 중이면 청산 보류"""
        if not getattr(self._scan_cfg, "trend_protect_enabled", True):
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
        if ema20 and pos.current_price > ema20:
            return True

        return False

    # ─── 상태 제어 ──────────────────────────────────────────────────

    def set_auto_trading(self, enabled: bool) -> None:
        """자동매매 플래그 설정"""
        self._auto_trading = enabled

    @property
    def auto_trading(self) -> bool:
        """자동매매 활성화 여부"""
        return self._auto_trading
