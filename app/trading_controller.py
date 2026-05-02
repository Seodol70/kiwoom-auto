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




from app.strategy import EntryStrategy, ExitStrategy, ExitContext




class TradingController(QObject):
    """
    거래 컨트롤러 — 신호 필터링 + 청산 판정.


    - handle_signal(): 신호 6단계 필터 체인
    - check_exit_*(): 포지션별 청산 조건 판정
    """


    signal_rejected = pyqtSignal(str)
    """신호 거절 사유"""


    log_message = pyqtSignal(str)
    """청산 판정 로그 메시지"""


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

        from app.strategy import EntryStrategy, ExitStrategy
        self._entry_strategy = EntryStrategy(self._order_mgr, self._risk_mgr)
        self._exit_strategy = ExitStrategy(self._scan_cfg, self._snap_store)


    @pyqtSlot(bool)
    def set_auto_trading(self, enabled: bool) -> None:
        self._auto_trading = enabled
        logger.info("[TradingController] 자동매매 %s", "ON" if enabled else "OFF")

    # ─── 신호 필터링 ──────────────────────────────────────────────────


    @pyqtSlot(object)
    def handle_signal(self, sig: ScanSignal) -> bool:
        """신호 필터링 (EntryStrategy 위임)"""
        passed, reason = self._entry_strategy.should_entry(sig, self._auto_trading)
        
        if not passed:
            self.signal_rejected.emit(f"{sig.code}: {reason}")
            return False

        # ✅ 모든 필터 통과 → 진입 신호 전송
        self._order_mgr.handle_signal(sig)
        return True


    # ─── 필터 헬퍼 ──────────────────────────────────────────────────




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
            # peak_price 갱신
            self._exit_strategy.update_peak_price(pos)

            # [REFACTORED] ExitStrategy 사용 (손절, 트레일링, 타임컷 통합)
            should_exit, reason = self._exit_strategy.should_exit(pos, exit_ctx)
            
            if should_exit:
                self.log_message.emit(f"🚀 [청산] {pos.name}({pos.code}) {reason}")
                
                # 손절 계열인 경우 블랙리스트 등록 (당일 재진입 방지)
                if any(x in reason for x in ["Stop Loss", "Hard Stop", "Candle Stop"]):
                    self._order_mgr.mark_stop_loss(pos.code)
                    
                self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
                count += 1
                continue

            # 나머지 특수 청산 로직 (분할익절 등 - 향후 ExitStrategy로 완전 통합 가능)
            if self._check_partial_profit(pos, sell_qty, exit_ctx):
                count += 1
                continue
            if self._check_breakeven_stop(pos, sell_qty):
                count += 1
                continue
            if self._check_ema20_exit(pos, sell_qty):
                count += 1
                continue
            if self._check_trend_decay(pos, sell_qty):
                count += 1
                continue



    def _get_exit_context(self, now: datetime) -> ExitContext:
        """현재 시간에 따른 청산 파라미터 조회"""
        now_min = now.hour * 60 + now.minute
        _is_opening = (9 * 60) <= now_min < (9.5 * 60)
        _is_midday = (11 * 60) <= now_min < (13 * 60)


        partial_profit_pct = float(getattr(self._scan_cfg, "partial_profit_pct", 0.0))
        atr_trail_enabled = getattr(self._scan_cfg, "atr_trail_enabled", False)


        if _is_opening:
            return ExitContext(
                sl_pct=float(
                    getattr(self._scan_cfg, "stop_loss_pct_opening", self._scan_cfg.stop_loss_pct)
                ),
                trail_activation=self._scan_cfg.trail_activation_pct,
                trail_tier1=self._scan_cfg.trail_pct_tier1,
                trail_tier2=self._scan_cfg.trail_pct_tier2,
                trail_tier3=self._scan_cfg.trail_pct_tier3,
                time_cut_min=self._scan_cfg.time_cut_minutes,
                partial_profit_pct=partial_profit_pct,
                atr_trail_enabled=atr_trail_enabled,
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
                trail_tier3=self._scan_cfg.trail_pct_tier3,
                time_cut_min=int(
                    getattr(
                        self._scan_cfg, "time_cut_minutes_midday", self._scan_cfg.time_cut_minutes
                    )
                ),
                partial_profit_pct=partial_profit_pct,
                atr_trail_enabled=atr_trail_enabled,
            )
        else:
            return ExitContext(
                sl_pct=self._scan_cfg.stop_loss_pct,
                trail_activation=self._scan_cfg.trail_activation_pct,
                trail_tier1=self._scan_cfg.trail_pct_tier1,
                trail_tier2=self._scan_cfg.trail_pct_tier2,
                trail_tier3=self._scan_cfg.trail_pct_tier3,
                time_cut_min=self._scan_cfg.time_cut_minutes,
                partial_profit_pct=partial_profit_pct,
                atr_trail_enabled=atr_trail_enabled,
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
                trail_pct = ctx.trail_tier3
        else:
            if peak_chg < self._scan_cfg.trail_tier1_max:
                trail_pct = ctx.trail_tier1
            elif peak_chg < self._scan_cfg.trail_tier2_max:
                trail_pct = ctx.trail_tier2
            else:
                trail_pct = ctx.trail_tier3


        trail_price = int(pos.peak_price * (1 - trail_pct / 100))


        # ATR 기반 트레일 (더 촘촘한 쪽 선택)
        if ctx.atr_trail_enabled and self._snap_store:
            snap = self._snap_store.get_snapshot(pos.code)
            if snap:
                highs = list(getattr(snap, "highs_1min", []) or [])
                lows = list(getattr(snap, "lows_1min", []) or [])
                closes = list(getattr(snap, "closes_1min", []) or [])
                if len(highs) >= 14 and len(lows) >= 14 and len(closes) >= 14:
                    from scanner.indicator_service import IndicatorService
                    atr = IndicatorService.calc_atr(highs, lows, closes, 14)
                    if atr:
                        atr_multiplier = float(
                            getattr(self._scan_cfg, "atr_trail_multiplier", 1.5)
                        )
                        atr_trail_price = int(pos.peak_price - atr * atr_multiplier)
                        trail_price = max(trail_price, atr_trail_price)


        # EMA 트레일링 로직
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


    def _check_partial_profit(self, pos, sell_qty: int, ctx: ExitContext) -> bool:
        """분할익절: 목표 수익률 도달 시 일부 수량 매도"""
        if not getattr(self._scan_cfg, "partial_profit_enabled", False):
            return False


        if getattr(pos, "partial_sold", False):
            return False


        if ctx.partial_profit_pct <= 0:
            return False


        chg = float(pos.price_change_pct_vs_avg)
        if chg >= ctx.partial_profit_pct:
            sell_ratio = float(getattr(self._scan_cfg, "partial_sell_ratio", 0.30))
            logger.info(
                "[분할익절] %s(%s) 수익 %.2f%% ≥ %.2f%% — %.0f%% 매도",
                pos.name,
                pos.code,
                chg,
                ctx.partial_profit_pct,
                sell_ratio * 100,
            )
            self._order_mgr.partial_exit(pos.code, pos.name, sell_ratio=sell_ratio, reason="분할익절")
            return True
        return False


    def _check_breakeven_stop(self, pos, sell_qty: int) -> bool:
        """본절가 스탑: 분할익절 후 평단 이탈 시 전량 매도"""
        if not getattr(self._scan_cfg, "breakeven_stop_enabled", False):
            return False


        if not getattr(pos, "partial_sold", False):
            return False


        buffer_pct = float(getattr(self._scan_cfg, "breakeven_stop_buffer_pct", 0.0))
        chg = float(pos.price_change_pct_vs_avg)
        if chg <= buffer_pct:
            logger.info(
                "[본절가스탑] %s(%s) 수익 %.2f%% ≤ %.2f%% — 전량 청산",
                pos.name,
                pos.code,
                chg,
                buffer_pct,
            )
            self._order_mgr.mark_stop_loss(pos.code)
            self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
            return True
        return False


    def _check_ema20_exit(self, pos, sell_qty: int) -> bool:
        """EMA20 이탈 청산: 현재가가 EMA20 아래로 내려가면 매도"""
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
        ema20_threshold = ema20 * (1 - buffer_pct / 100)


        if pos.current_price < ema20_threshold:
            logger.info(
                "[EMA20이탈] %s(%s) 현재가 %d < EMA20 %.0f — %d주 청산",
                pos.name,
                pos.code,
                pos.current_price,
                ema20_threshold,
                sell_qty,
            )
            self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
            return True
        return False


    def _check_trend_decay(self, pos, sell_qty: int) -> bool:
        """추세소멸 익절: 요셉 추세 지표 소멸 감지"""
        # EOD 포지션 스킵
        if getattr(pos, "eod_trade", False):
            return False


        # 손실 구간 스킵 (이익만 익절)
        chg = float(pos.price_change_pct_vs_avg)
        if chg <= 0:
            return False


        # OrderManager의 should_exit_on_trend_decay 호출
        if self._order_mgr.should_exit_on_trend_decay(pos.code):
            logger.info(
                "[추세소멸] %s(%s) 추세 소멸 감지, 수익 %.2f%% — %d주 익절",
                pos.name,
                pos.code,
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


    # ─── 시간대별 특별 관리 ──────────────────────────────────────────


    def check_market_crash(self) -> None:
        return


    def check_overnight_gap(self) -> None:
        import logging as _log
        _logger = _log.getLogger(__name__)
        _gap_up = float(getattr(self._scan_cfg, 'eod_gap_up_exit_pct', 2.0))
        _gap_dn = float(getattr(self._scan_cfg, 'eod_gap_down_exit_pct', -1.5))
        eod_positions = [(code, pos) for code, pos in list(self._order_mgr.positions.items()) if getattr(pos, 'eod_trade', False)]
        if not eod_positions:
            return
        

        self.log_message.emit(f'🌅 [EOD갭체크] {len(eod_positions)}개 오버나잇 포지션 갭 확인...')
        for code, pos in eod_positions:
            if getattr(pos, 'avg_price', 0) <= 0:
                continue
            chg = float(pos.price_change_pct_vs_avg)
            if chg >= _gap_up:
                self.log_message.emit(f'🟢 [EOD갭익절] {pos.name}({code}) 갭 상승 {chg:+.2f}% >= {_gap_up:.1f}% — {pos.qty}주 즉시 시장가 매도')
                if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                    self._order_mgr._audit.log_sell_decision(code, f'EOD 갭익절 {chg:+.2f}%', pos.current_price)
                self._order_mgr.force_exit(code, pos.name, pos.qty, reason=f'EOD 갭익절 {chg:+.2f}%')
            elif chg <= _gap_dn:
                self.log_message.emit(f'🔴 [EOD갭손절] {pos.name}({code}) 갭 하락 {chg:+.2f}% <= {_gap_dn:.1f}% — {pos.qty}주 즉시 시장가 매도')
                if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                    self._order_mgr._audit.log_sell_decision(code, f'EOD 갭손절 {chg:+.2f}%', pos.current_price)
                self._order_mgr.mark_stop_loss(code)
                self._order_mgr.force_exit(code, pos.name, pos.qty, reason=f'EOD 갭손절 {chg:+.2f}%')
            else:
                pos.overnight_held = True
                self.log_message.emit(f'⏳ [EOD보합] {pos.name}({code}) 갭 {chg:+.2f}% — 트레일 스탑 모드로 전환')


    def liquidate_phase1_positions(self, forced: bool=False) -> None:
        import logging as _log_module
        _logger = _log_module.getLogger(__name__)
        _trail_drop = float(getattr(self._scan_cfg, 'phase1_trail_drop_pct', 1.0))
        for code, pos in list(self._order_mgr.positions.items()):
            if getattr(pos, 'entry_phase', 0) != 1:
                continue
            if getattr(pos, 'qty', 0) <= 0:
                continue
            if forced:
                self._order_mgr.force_exit(code, pos.name, pos.qty, reason='Phase1 10:30 강제청산')
                self.log_message.emit(f'⏱ [Phase1강제청산] {pos.name}({code}) {pos.qty}주 — 10:30 타임컷')
            else:
                if getattr(pos, 'peak_price', 0) <= 0 or getattr(pos, 'current_price', 0) <= 0:
                    continue
                drop_pct = (pos.peak_price - pos.current_price) / pos.peak_price * 100
                if drop_pct >= _trail_drop:
                    self._order_mgr.force_exit(code, pos.name, pos.qty, reason=f'Phase1 trail -{_trail_drop:.1f}%')
                    self.log_message.emit(f'📉 [Phase1트레일] {pos.name}({code}) 고점 {pos.peak_price:,} → 현재 {pos.current_price:,} (-{drop_pct:.1f}%) 청산')


    def liquidate_all_positions(self) -> None:
        from datetime import date as _date
        import logging as _log
        _logger = _log.getLogger(__name__)
        if getattr(self, '_liquidate_in_progress', False):
            return
        self._liquidate_in_progress = True
        try:
            positions = list(self._order_mgr.positions.items())
            if not positions:
                self.log_message.emit('💤 보유 포지션 없음 — 청산 생략')
                return
            targets = []
            for code, pos in positions:
                if getattr(pos, 'eod_trade', False):
                    self.log_message.emit(f'🌙 [EOD유지] {pos.name}({code}) — 종가매매 포지션, 당일 청산 제외')
                    continue
                q = getattr(pos, 'qty_buy_today_app', 0) or 0
                if q <= 0 and (not getattr(pos, 'opened_by_app', False)):
                    continue
                sell_qty = min(pos.qty, q) if q > 0 else pos.qty
                if sell_qty > 0:
                    targets.append((code, pos, sell_qty))
            

            if not targets:
                return
            

            self.log_message.emit(f'🔴 [자동청산 시작] 오늘 앱 매수 {len(targets)}종목만 청산...')
            for code, pos, sell_qty in targets:
                try:
                    if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                        self._order_mgr._audit.log_sell_decision(code, 'Day Close 15:19 강제청산', pos.current_price)
                    self._order_mgr.sell(code, pos.name, sell_qty, price=0)
                    self.log_message.emit(f'  └─ {pos.name}({code}) {sell_qty}주 시장가 매도 주문')
                except Exception as e:
                    self.log_message.emit(f'  ⚠️ {pos.name}({code}) 청산 실패: {e}')
        finally:
            self._liquidate_in_progress = False
