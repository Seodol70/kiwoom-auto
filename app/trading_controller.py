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




from strategy.base import ExitContext
from strategy.jang_dong_min import JangDongMinStrategy
from app.signal_filter import SignalFilterChain, SignalFilterContext




class TradingController(QObject):
    """
    거래 컨트롤러 — 신호 필터링 + 청산 판정.


    - handle_signal(): 신호 6단계 필터 체인
    - check_exit_*(): 포지션별 청산 조건 판정
    """


    signal_rejected = pyqtSignal(str)
    """신호 거절 사유"""


    log_message = pyqtSignal(str)
    """청산/스캔/시스템 로그 메시지"""

    market_data_updated = pyqtSignal(float, float, float, float, bool)
    """코스피(현재, %), 코스닥(현재, %), 급락여부"""

    scan_status_updated = pyqtSignal(str, bool)
    """상태 메시지, 완료여부"""

    daily_refresh_requested = pyqtSignal(list)
    """일봉 데이터 수집 요청 (codes)"""

    auto_trade_started = pyqtSignal()
    """첫 감시 신호 발생으로 자동매매가 자동 시작될 때 발행"""

    market_crash_detected = pyqtSignal(float, float)
    """지수 급락 감지 (코스피 %, 코스닥 %)"""


    def __init__(
        self,
        kiwoom=None,
        order_mgr=None,
        scan_cfg=None,
        risk_mgr=None,
        smart_scanner=None,
        snap_store=None,
        health_monitor=None,
        news_analyzer=None,
        ctx=None,
        parent=None,
    ):
        super().__init__(parent)
        self._kiwoom = kiwoom
        self._order_mgr = order_mgr
        self._scan_cfg = scan_cfg
        self._risk_mgr = risk_mgr
        self._smart_scanner = smart_scanner
        self._snap_store = snap_store
        self._health_monitor = health_monitor
        # [NEW 2026-05-26] 뉴스 감정 분석기 (호재/악재) — 매매 결정 가중치
        self._news_analyzer = news_analyzer
        self._ctx = ctx  # AppState 참조 (선택적)
        self._auto_trading = False
        self._first_signal_received = False  # 첫 신호 여부 추적

        # [P0-1 2026-05-21] 개장 직후 30분(09:00~09:30) 분당 1건 진입 제한
        # 어제 09:04~09:07 사이 5건 무차별 진입 → 모두 손실 사례 방지
        self._opening_entry_times: list = []  # 09:00~09:30 진입 시각 기록
        
        self._scan_in_progress = False
        self._market_crash_off = False
        self._kospi_chg_pct = 0.0
        self._kosdaq_chg_pct = 0.0
        self._kospi_cur = 0.0
        self._kosdaq_cur = 0.0

        self._strategy = JangDongMinStrategy(
            self._order_mgr, self._risk_mgr, self._scan_cfg, self._snap_store
        )

        # [AI] AI 필터 초기화 (ML 모델 기반 신호 판정)
        from app.ai_filter import AIFilter
        self._ai_filter = AIFilter()

        # [Step 2 2026-05-29] 신호 필터 체인 초기화
        self._filter_chain = SignalFilterChain()

        # [NEW] SmartScanner 신호를 주문 모듈과 연결 (2026-05-07 수정)
        if self._smart_scanner:
            self._smart_scanner.signal_detected.connect(self._on_signal_from_scanner)
            # [FIX 2026-05-12] 크로스 스레드 signal emit 문제 해결: callback 직접 등록
            self._smart_scanner._on_signal_callback = self._on_signal_from_scanner

    def _on_signal_from_scanner(self, sig) -> None:
        """SmartScanner에서 발생한 신호를 처리하여 주문 실행"""
        if not sig or not self._order_mgr:
            return

        try:
            # [2026-05-22] 신호처리 시작 WARNING 로그 제거 (신호당 1건, 메인 스레드 부하)
            # TradingController.handle_signal()을 호출하여 전체 필터 검증을 먼저 거치도록 수정
            self.handle_signal(sig)
        except Exception as e:
            logger.error("[신호처리 오류] %s", e)

    def force_update_stock(self, code: str) -> None:
        """특정 종목의 정보를 즉시 강제 갱신한다 (사용자 클릭 시)."""
        if not code: return

        logger.info("[강제갱신] %s 정보 요청 중...", code)

        # [NEW] SetRealReg 캐시 우선 사용 (최신 실시간 데이터)
        snap = self._snap_store.get_snapshot(code)
        if snap and snap.current_price > 0:
            logger.info("[강제갱신] %s SetRealReg 캐시 사용 (현재가: %d원)", code, snap.current_price)
            info = {
                "name": snap.name,
                "current_price": snap.current_price,
                "open": snap.open_price,
                "high": snap.high_price,
                "low": snap.low_price,
                "volume": snap.volume,
                "trade_amount": snap.trade_amount,
                "change_pct": snap.change_pct,
                "prev_close": snap.prev_close,
            }
        else:
            # Fallback: opt10001 조회
            info = self._kiwoom.get_stock_info(code)

        if info and info.get("current_price", 0) > 0:
            # 1. SnapshotStore 갱신
            self._snap_store.update_price(
                code=code,
                current_price=info["current_price"],
                open_price=info.get("open", 0),
                high_price=info.get("high", 0),
                low_price=info.get("low", 0),
                volume=info.get("volume", 0),
                trade_amount=info.get("trade_amount", 0),
                change_pct=info.get("change_pct", 0.0),
                prev_close=info.get("prev_close", 0)
            )
            
            # 2. InternalState (고속 캐시) 동기화
            st = self._snap_store.get_internal_state(code)
            if st:
                st.current_price = info["current_price"]
                st.prev_close = info.get("prev_close", 0)
                st.change_pct = info.get("change_pct", 0.0)
                st.trade_amount = info.get("trade_amount", 0)
            
            self.log_message.emit(f"✅ [{info['name']}] 데이터 강제 갱신 완료 (현재가: {info['current_price']:,}원)")
        else:
            self.log_message.emit(f"❌ [{code}] 데이터 강제 갱신 실패 (네트워크 또는 마켓 확인)")
        
        # [AI] 신호 필터 초기화
        from app.ai_filter import AIFilter
        self._ai_filter = AIFilter()

        # 리스크 매니저 신호 연결
        if self._risk_mgr:
            self._risk_mgr.daily_loss_cut.connect(self.liquidate_all_positions)
            self._risk_mgr.daily_profit_locked.connect(
                lambda: self.log_message.emit("💰 [리스크] 당일 수익 목표 달성 — 신규 매수 차단")
            )


    @pyqtSlot(bool)
    def set_auto_trading(self, enabled: bool) -> None:
        self._auto_trading = enabled
        logger.info("[TradingController] 자동매매 %s", "ON" if enabled else "OFF")

    def set_risk_params(self, tp: float = None, sl: float = None) -> None:
        """UI(SpinBox)에서 변경된 익절/손절 기준을 실시간 반영"""
        if tp is not None:
            # ConfigManager(self._scan_cfg)의 런타임 값 업데이트
            self._scan_cfg.set_runtime("take_profit_pct", tp)
            logger.info("[TradingController] 익절 기준 실시간 변경: %.2f%%", tp)
        if sl is not None:
            self._scan_cfg.set_runtime("stop_loss_pct", sl)
            logger.info("[TradingController] 손절 기준 실시간 변경: %.2f%%", sl)

    # ─── 신호 필터링 ──────────────────────────────────────────────────


    @pyqtSlot(object)
    def handle_signal(self, sig: ScanSignal) -> bool:
        """
        신호 필터링 체인 실행.

        9단계 필터를 순차 실행하고, 모두 통과 시 주문 관리자로 위임.
        """
        # 자동매매 OFF 상태 체크 (사용자가 완전히 비활성화한 경우)
        if not self._auto_trading:
            self.signal_rejected.emit(f"{sig.code}: 자동매매 OFF 상태")
            self._record_signal(sig)
            return False

        # 최대 포지션 한도 체크
        if self._order_mgr and len(self._order_mgr.positions) >= getattr(self._order_mgr, 'max_positions', 5):
            self.signal_rejected.emit(f"{sig.code}: 최대포지션 초과 ({len(self._order_mgr.positions)}/{getattr(self._order_mgr, 'max_positions', 5)})")
            self._record_signal(sig)
            return False

        # 중복 진입 방지
        if self._order_mgr and sig.code in self._order_mgr.positions:
            self.signal_rejected.emit(f"{sig.code}: 중복 진입 방지")
            self._record_signal(sig)
            return False

        # 필터 컨텍스트 구성
        ctx = SignalFilterContext(
            order_mgr=self._order_mgr,
            snap_store=self._snap_store,
            trading_cfg=self._scan_cfg,
            risk_mgr=self._risk_mgr,
            app_state=self._ctx,
            kiwoom=self._kiwoom,
            news_analyzer=self._news_analyzer,
            opening_entry_times=self._opening_entry_times,
        )

        # 필터 체인 실행
        passed, reason = self._filter_chain.validate(sig, ctx)

        if not passed:
            self.signal_rejected.emit(reason)
            self._record_signal(sig)
            return False

        # 자동매매 자동 시작: 첫 신호 발생 시
        if not self._auto_trading and not self._market_crash_off and not self._first_signal_received:
            self._first_signal_received = True
            self.auto_trade_started.emit()
            logger.info("[TradingController] 첫 신호 발생 — 자동매매 자동 시작 요청")

        # ✅ 모든 필터 통과 → 주문 관리자로 위임
        self._order_mgr.handle_signal(sig)
        self._record_signal(sig)

        # 개장 1시간 내 진입 시각 기록
        if self._is_opening_hour():
            self._opening_entry_times.append(datetime.now())

        return True

    def _is_opening_hour(self) -> bool:
        """09:00~10:00 시간대 확인"""
        now = datetime.now()
        opening_start = datetime.strptime("09:00", "%H:%M").time()
        opening_end = datetime.strptime("10:00", "%H:%M").time()
        return opening_start <= now.time() <= opening_end

    def _record_signal(self, sig) -> None:
        """HealthMonitor에 신호 기록 (매매 여부 무관)"""
        if self._health_monitor is not None:
            self._health_monitor.record_signal(sig.code, sig.name, sig.signal_type)

    def on_fill_processed(self, fill_dict: dict) -> None:
        """매도 체결 시 손익 기록 (HealthMonitor 위임)"""
        if fill_dict.get("side") != "매도체결" or self._health_monitor is None:
            return
        _ab = fill_dict.get("avg_buy_price") or 0
        _fp = fill_dict.get("filled_price", 0)
        _fq = fill_dict.get("filled_qty", 0)
        _pnl = (_fp - _ab) * _fq if _ab and _fp and _fq else 0.0
        from analysis.health_monitor import TradeRecord
        self._health_monitor.record_trade(TradeRecord(
            code=fill_dict.get("code", ""),
            pnl=float(_pnl),
            entry_time=str(fill_dict.get("entry_time", "")),
            exit_time=str(fill_dict.get("filled_time", "")),
            reason=fill_dict.get("reason", ""),
        ))

    def on_market_closing(self) -> None:
        """장마감 시 전일 거래량 캐시 저장"""
        if self._smart_scanner is not None:
            try:
                self._smart_scanner.save_prev_volumes()
                logger.info("[15:20] prev_volumes 저장 완료")
            except Exception as _e:
                logger.warning("[15:20] prev_volumes 저장 실패: %s", _e)


    def manual_sell(self, code: str, name: str, qty: int) -> tuple[bool, str]:
        """수동 매도 — 검증 후 시장가 매도. 반환: (성공여부, 로그메시지)"""
        pos = self._order_mgr.positions.get(code)
        if pos is None:
            return False, f"⚠ 수동매도 오류 — {name}({code}) 포지션 없음"
        if qty <= 0 or qty > pos.qty:
            return False, f"⚠ 수동매도 오류 — 수량 {qty}주 (보유 {pos.qty}주)"
        self._order_mgr.sell(code, name, qty, price=0)
        return True, f"[수동매도] {name}({code}) {qty}주 시장가 요청"

    def manual_buy(self, code: str, name: str, qty: int, price: int = 0, order_type: str = "03") -> tuple[bool, str]:
        """수동 매수 — 반환: (성공여부, 로그메시지)"""
        if qty <= 0:
            return False, f"⚠ 수동매수 오류 — 수량 {qty}주 부족"
        
        if not self._kiwoom or not self._kiwoom.get_login_state():
            return False, "⚠ 수동매수 실패 — 키움 API 미연결 상태입니다"

        # OrderManager의 buy 메서드 호출 (order_type: "03"=시장가, "00"=지정가)
        order_no = self._order_mgr.buy(code, name, qty, price=price, order_type=order_type)
        
        otype_str = "시장가" if order_type == "03" else f"지정가({price:,}원)"
        
        if not order_no or order_no == "0":
            return False, f"❌ [수동매수] {name}({code}) 주문 전송 실패 (API 리턴값 확인 필요)"
            
        return True, f"✅ [수동매수] {name}({code}) {qty}주 {otype_str} 요청 완료 (ID: {order_no})"

    def get_chart_data(self, code: str) -> dict[str, Any]:
        """차트 표시용 데이터 조회 — 1분봉 데이터 로드"""
        result = {
            "code": code,
            "closes": [],
            "volumes": [],
            "name": "",
            "position": None,
            "trail_price": 0,
            "sl_pct": -1.5,
        }

        if not code:
            return result

        try:
            # 1분봉 100개 로드
            candles = self._kiwoom.get_min_candles(code, tick_unit=1, count=100)
            if candles:
                result["closes"] = [c.get("close", 0) for c in candles]
                result["volumes"] = [c.get("volume", 0) for c in candles]
                logger.info("[차트] %s 1분봉 %d개 로드 완료", code, len(candles))
            else:
                # 폴백: 실시간 감시 데이터에서 현재가 1개 사용
                if hasattr(self._smart_scanner, 'store') and self._smart_scanner.store:
                    snap = self._smart_scanner.store.get_snapshot(code)
                    if snap and snap.current_price > 0:
                        result["closes"] = [snap.current_price]
                        result["volumes"] = [snap.volume if hasattr(snap, 'volume') else 0]
                        logger.info("[차트] %s 1분봉 로드 실패 — 실시간 데이터 폴백", code)

            # 종목명 조회
            if not result["closes"]:  # 데이터 없으면 종목명만
                result["name"] = self._kiwoom.get_stock_name(code)
            else:
                snap = self._smart_scanner.store.get_snapshot(code) if (hasattr(self._smart_scanner, 'store') and self._smart_scanner.store) else None
                result["name"] = snap.name if (snap and hasattr(snap, 'name')) else self._kiwoom.get_stock_name(code)

            # 포지션 정보
            pos = self._order_mgr.positions.get(code)
            result["position"] = pos

            # 전략 파라미터
            result["sl_pct"] = float(getattr(self._scan_cfg, "jdm_stop_loss_pct", -1.5))

            # 트레일 스탑가
            if pos and hasattr(pos, "trail_stop_price"):
                result["trail_price"] = pos.trail_stop_price

            return result
        except Exception as e:
            logger.error("[차트데이터] 조회 오류 — %s: %s", code, e)
            return result

    def tick_investor_refresh(self) -> bool:
        """수급 갱신 타이머 콜백 — 시간·상태 조건 충족 시 TR 호출. 반환: 조회 여부"""
        if not getattr(self._scan_cfg, "investor_filter_enabled", False):
            return False
        if self._smart_scanner is None:
            return False
        from datetime import datetime, time
        now = datetime.now().time()
        if not (time(9, 0) <= now <= time(15, 30)):
            return False
        if getattr(self._kiwoom, "_tr_busy", False):
            return False  # 호출부에서 재시도 처리
        self._smart_scanner.trigger_investor_refresh()
        return True

    # ─── 필터 헬퍼 ──────────────────────────────────────────────────




    # ─── 포지션 청산 판정 ──────────────────────────────────────────────

    @pyqtSlot()
    def tick_exit_check(self) -> None:
        """
        [Option A 2026-05-27] 독립 5초 타이머에서 호출되는 청산 평가.

        기존엔 update_portfolio_prices() → check_and_exit_all() 흐름이 잔고 워커
        (opw00001 60초 주기)에 종속됐음. 잔고 워커가 멈추면 청산도 멈춰서
        2026-05-27 빛과전자 -52,211원 사례 발생 (11분간 청산 평가 0회).

        이 메서드는:
        1. SnapshotStore 실시간 가격으로 보유 포지션 current_price 갱신
        2. check_and_exit_all() 직접 호출 (손절/익절 트리거)
        3. AppState 갱신 (UI에 현재가/평가손익 반영)
        — 잔고 동기화와 완전히 분리되어 독립적으로 작동.
        """
        if not self._order_mgr or not self._order_mgr.positions:
            return
        try:
            # 1. 보유 포지션 가격만 빠르게 갱신 (잔고 동기화 X, 가벼움)
            for pos in self._order_mgr.positions.values():
                if self._snap_store:
                    snap = self._snap_store.get_snapshot(pos.code)
                    if snap and snap.current_price > 0 and pos.current_price != snap.current_price:
                        pos.current_price = snap.current_price

            # 2. AppState 갱신 (UI에 평가손익 반영)
            if self._ctx:
                self._ctx.update_portfolio(self._order_mgr.cash, dict(self._order_mgr.positions))

            # 3. 청산 평가 (가장 중요한 부분 — 손절/익절 트리거)
            self.check_and_exit_all()
        except Exception as e:
            logger.warning("[tick_exit_check] 오류: %s", e)

    def check_and_exit_all(self) -> None:
        """모든 포지션 청산 판정 (5초 주기 호출)"""
        count = 0
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
            # 상태 갱신 (peak_price 등)
            self._strategy.update_state(pos)

            should_exit, reason = self._strategy.should_exit(pos, exit_ctx)
            if should_exit:
                self.log_message.emit(f"🚀 [청산] {pos.name}({pos.code}) {reason}")
                if any(x in reason for x in ["Stop Loss", "Hard Stop", "본절가스탑"]):
                    self._order_mgr.mark_stop_loss(pos.code)
                    # [NEW 2026-05-19] 손절 종목 재진입 방지 (20분 냉각)
                    self._strategy.mark_loss_exit(pos)
                self._order_mgr.sell(pos.code, pos.name, sell_qty, price=0)
                count += 1
                continue

            do_partial, ratio = self._strategy.should_partial_exit(pos, exit_ctx)
            if do_partial:
                self.log_message.emit(f"🔀 [분할익절] {pos.name}({pos.code}) {ratio*100:.0f}% 매도")
                self._order_mgr.partial_exit(pos.code, pos.name, sell_ratio=ratio, reason="분할익절")
                count += 1
                continue



    def _get_exit_context(self, now: datetime) -> ExitContext:
        """현재 시간에 따른 청산 파라미터 조회"""
        now_min = now.hour * 60 + now.minute
        _is_opening = (9 * 60) <= now_min < (9.5 * 60)
        _is_midday = (11 * 60) <= now_min < (13 * 60)
        _is_afternoon = (13 * 60) <= now_min < (14.5 * 60)  # 13:00~14:30


        partial_profit_pct = float(getattr(self._scan_cfg, "partial_profit_pct", 0.0))
        atr_trail_enabled = getattr(self._scan_cfg, "atr_trail_enabled", False)


        if _is_opening:
            return ExitContext(
                sl_pct=float(
                    getattr(self._scan_cfg, "stop_loss_pct_opening", self._scan_cfg.jdm_stop_loss_pct)
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
                    getattr(self._scan_cfg, "stop_loss_pct_midday", self._scan_cfg.jdm_stop_loss_pct)
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
        elif _is_afternoon:
            # 오후(13:00~14:30) — 변동성 높음, 더 보수적인 청산 정책
            return ExitContext(
                sl_pct=float(
                    getattr(self._scan_cfg, "stop_loss_pct_afternoon", -1.0)
                ),
                trail_activation=self._scan_cfg.trail_activation_pct,
                trail_tier1=float(
                    getattr(
                        self._scan_cfg, "trail_pct_tier1", 0.8
                    )
                ),
                trail_tier2=self._scan_cfg.trail_pct_tier2,
                trail_tier3=self._scan_cfg.trail_pct_tier3,
                time_cut_min=int(
                    getattr(
                        self._scan_cfg, "time_cut_minutes_afternoon", 15
                    )
                ),
                partial_profit_pct=partial_profit_pct,
                atr_trail_enabled=atr_trail_enabled,
            )
        else:
            return ExitContext(
                sl_pct=getattr(self._scan_cfg, "jdm_stop_loss_pct", -1.2),
                trail_activation=self._scan_cfg.trail_activation_pct,
                trail_tier1=self._scan_cfg.trail_pct_tier1,
                trail_tier2=self._scan_cfg.trail_pct_tier2,
                trail_tier3=self._scan_cfg.trail_pct_tier3,
                time_cut_min=self._scan_cfg.time_cut_minutes,
                partial_profit_pct=partial_profit_pct,
                atr_trail_enabled=atr_trail_enabled,
            )


    # ─── 상태 제어 ──────────────────────────────────────────────────

    @property
    def auto_trading(self) -> bool:
        """자동매매 활성화 여부"""
        return self._auto_trading


    # ─── 시간대별 특별 관리 ──────────────────────────────────────────


    # ─── 스캔 및 시장 감시 (MainWindow 에서 이전) ──────────────────────────
    
    @pyqtSlot()
    def run_periodic_scan(self) -> None:
        """주기적 종목 스캔 실행 (1분마다)"""
        if self._scan_in_progress:
            logger.debug("[TradingController] 이전 스캔 진행 중 — 스킵")
            return

        if getattr(self._kiwoom, '_tr_busy', False):
            logger.info("[TradingController] TR 처리 중 (%s) — 3s 후 재시도",
                        getattr(self._kiwoom, '_tr_current_rq', '?'))
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(3_000, self.run_periodic_scan)
            return

        self._scan_in_progress = True
        self.log_message.emit(f"[스캔] 주기 스캔 시작 — {datetime.now():%H:%M:%S}")
        self.scan_status_updated.emit("스캔 중...", False)

        try:
            # HealthMonitor ACK
            if self._health_monitor:
                self._health_monitor.ack()

            # 지수 급락 감지 (코스피/코스닥 지수 갱신)
            self.check_market_crash()

            top_codes = []
            if self._smart_scanner:
                top_codes = self._smart_scanner.run_periodic_scan(on_progress=None)

            if self._health_monitor:
                self._health_monitor.ack()

            total_watched = len(self._snap_store) if self._snap_store else 0
            self.log_message.emit(f"[스캔] 완료 — 전체 {total_watched}종목 모니터링")
            self.scan_status_updated.emit(f"완료 / {total_watched}종목", True)

            # SnapshotStore 메모리 정리 (감시 목록 + 보유 포지션 외 제거)
            if self._snap_store and self._smart_scanner:
                # 감시 대상 + 보유 종목 + UI 표시용 상위 종목 유지 (메모리 정리 방지)
                watch_codes = set(top_codes) if top_codes else set(getattr(self._smart_scanner.watch_q, "subscribed", set()))
                ui_codes = set(self._snap_store.top_by_trade_amount(120).index)
                pos_codes = set(self._order_mgr.positions.keys())
                removed = self._snap_store.cleanup_stale_data(watch_codes | pos_codes | ui_codes)
                if removed:
                    logger.info("[스캔] SnapshotStore 메모리 정리 — %d종목 제거", removed)

            # 일봉 갱신 대기 목록 처리 요청
            if self._smart_scanner:
                _pending = list(getattr(self._smart_scanner, "_daily_refresh_pending", []))[:10]
                if _pending:
                    self._smart_scanner._daily_refresh_pending = []
                    self.daily_refresh_requested.emit(_pending)
        except Exception as e:
            logger.exception("[TradingController] run_periodic_scan 오류")
            self.log_message.emit(f"[스캔 오류] {e}")
            self.scan_status_updated.emit(f"오류: {e}", True)
        finally:
            self._scan_in_progress = False

    @pyqtSlot(str, float, float)
    def on_realtime_index_updated(self, idx_name: str, price: float, pct: float) -> None:
        """[NEW] SmartScanner로부터 실시간 지수 수신 (상시 감시)"""
        if idx_name == "KOSPI":
            self._kospi_cur = price
            self._kospi_chg_pct = pct
        else:
            self._kosdaq_cur = price
            self._kosdaq_chg_pct = pct
        
        # 급락 여부 즉시 판단
        self._evaluate_market_crash()

    @pyqtSlot()
    def check_market_crash(self) -> None:
        """지수 급락 감지 및 신규 진입 차단 (60초마다 폴링)"""
        # 장 시작 전(08:30 이전)에는 지수 데이터 미제공 — 스킵
        from datetime import datetime, time as _time
        now_t = datetime.now().time()
        if now_t < _time(8, 30):
            return

        if getattr(self._kiwoom, '_tr_busy', False):
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(5_000, self.check_market_crash)
            return

        # 1. 지수 조회 (코스피, 코스닥)
        kp = self._kiwoom.get_index_info("001")
        kd = self._kiwoom.get_index_info("101")

        if kp:
            self._kospi_cur = kp['current']
            self._kospi_chg_pct = kp['change_pct']
        if kd:
            self._kosdaq_cur = kd['current']
            self._kosdaq_chg_pct = kd['change_pct']
        
        # 급락 여부 판단 및 UI 갱신
        self._evaluate_market_crash()

    def _evaluate_market_crash(self) -> None:
        """지수 데이터를 기반으로 급락 여부를 판단하고 상태를 갱신한다."""
        if self._scan_cfg:
            self._scan_cfg.kospi_chg_pct = self._kospi_chg_pct
            self._scan_cfg.kosdaq_chg_pct = self._kosdaq_chg_pct

        # 급락 여부 판단 (기준: -3.0% — 2026-05-12: -2.0→-3.0, 학습 데이터 축적)
        crash_limit = -3.0
        is_crash = (self._kospi_chg_pct <= crash_limit or self._kosdaq_chg_pct <= crash_limit)

        # 급락 감지 시 신호 발행 (이미 중지된 상태면 중복 발행 방지)
        if is_crash and not self._market_crash_off:
            self.market_crash_detected.emit(self._kospi_chg_pct, self._kosdaq_chg_pct)
        
        # UI 업데이트 신호 발생
        self.market_data_updated.emit(
            self._kospi_cur, self._kospi_chg_pct,
            self._kosdaq_cur, self._kosdaq_chg_pct,
            is_crash
        )

    @pyqtSlot(float, float)
    def _on_market_crash_detected(self, kospi_pct: float, kosdaq_pct: float) -> None:
        """지수 급락 신호 수신 — 학습 데이터 수집 중이므로 무시 (2026-05-12)"""
        # 2026-05-12: 지수 급락 자동 정지 비활성화 (손해 감수하면서 데이터 수집)
        return

    def check_eod_daytime_targets(self) -> None:
        """EOD 포지션 당일 수익률 목표 확인 (Stage 2)"""
        _tp_pct = float(getattr(self._scan_cfg, 'take_profit_pct', 2.5))
        _pp_pct = float(getattr(self._scan_cfg, 'partial_profit_pct', 1.5))
        _sl_pct = float(getattr(self._scan_cfg, 'stop_loss_pct', -1.5))

        eod_positions = [(code, pos) for code, pos in list(self._order_mgr.positions.items())
                         if getattr(pos, 'eod_trade', False) and not getattr(pos, 'overnight_held', False)]

        for code, pos in eod_positions:
            if getattr(pos, 'avg_price', 0) <= 0:
                continue
            chg_pct = float(pos.price_change_pct_vs_avg)

            # 손절 우선 (도미노 방지)
            if chg_pct <= _sl_pct:
                self.log_message.emit(f'🔴 [EOD일중손절] {pos.name}({code}) {chg_pct:+.2f}% <= {_sl_pct:.1f}% — {pos.qty}주 시장가 매도')
                if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                    self._order_mgr._audit.log_sell_decision(code, f'EOD 일중손절 {chg_pct:+.2f}%', pos.current_price)
                self._order_mgr.mark_stop_loss(code)
                self._order_mgr.force_exit(code, pos.name, pos.qty, reason=f'EOD 일중손절 {chg_pct:+.2f}%')
            # 완전 익절 (목표 달성)
            elif chg_pct >= _tp_pct:
                self.log_message.emit(f'🟢 [EOD일중익절] {pos.name}({code}) {chg_pct:+.2f}% >= {_tp_pct:.1f}% — {pos.qty}주 시장가 매도')
                if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                    self._order_mgr._audit.log_sell_decision(code, f'EOD 일중익절 {chg_pct:+.2f}%', pos.current_price)
                self._order_mgr.force_exit(code, pos.name, pos.qty, reason=f'EOD 일중익절 {chg_pct:+.2f}%')
            # 분할 익절 (중간 목표)
            elif chg_pct >= _pp_pct and not getattr(pos, 'partial_taken', False):
                half_qty = max(1, pos.qty // 2)
                self.log_message.emit(f'⭐ [EOD분할익절] {pos.name}({code}) {chg_pct:+.2f}% >= {_pp_pct:.1f}% — {half_qty}주 시장가 매도')
                if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                    self._order_mgr._audit.log_sell_decision(code, f'EOD 분할익절 {chg_pct:+.2f}%', pos.current_price)
                try:
                    self._order_mgr.sell(code, pos.name, half_qty, price=0)
                    pos.partial_taken = True
                except Exception as e:
                    self.log_message.emit(f'⚠️ [EOD분할익절실패] {pos.name}({code}): {e}')

    def check_overnight_gap(self) -> None:
        """EOD 포지션 익일 갭 확인 (Stage 1)"""
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

    def check_overnight_trend_break(self) -> None:
        """EOD 포지션 익일 추세 체크 (Stage 3: 일봉 정배열 파괴 시 강제 청산)"""
        from scanner.indicator_service import IndicatorService

        eod_overnight = [(code, pos) for code, pos in list(self._order_mgr.positions.items())
                         if getattr(pos, 'eod_trade', False) and getattr(pos, 'overnight_held', False)]
        if not eod_overnight:
            return

        for code, pos in eod_overnight:
            # 포지션에 저장된 일봉 데이터 사용 (또는 snapshot에서)
            daily_closes = getattr(pos, '_snapshot_daily_closes', None)
            if not daily_closes or len(daily_closes) < 20:
                continue

            current_price = float(getattr(pos, 'current_price', 0))
            if current_price <= 0:
                continue

            # 일봉 정배열 재확인
            align_ctx = IndicatorService.check_daily_alignment(daily_closes, current_price)
            if not align_ctx.get('is_aligned', False):
                self.log_message.emit(f'📉 [EOD추세파괴] {pos.name}({code}) 일봉 정배열 깨짐 — {pos.qty}주 즉시 시장가 매도')
                if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                    self._order_mgr._audit.log_sell_decision(code, 'EOD 추세파괴 (일봉정배열)', pos.current_price)
                self._order_mgr.force_exit(code, pos.name, pos.qty, reason='EOD 추세파괴 (일봉정배열)')
                pos.overnight_held = False

    def liquidate_phase1_positions(self, forced: bool=False) -> None:
        """Phase1 모닝 스캘핑 포지션 정리"""
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
        """장 마감 전 모든 포지션 청산 (EOD 제외)"""
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

    @pyqtSlot()
    def update_portfolio_prices(self) -> None:
        """보유 종목 현재가를 실시간 스냅샷 우선으로 갱신한다."""
        positions = self._order_mgr.positions
        if not positions:
            return
        
        try:
            missing_codes = []
            for pos in positions.values():
                price = 0
                if self._snap_store:
                    snap = self._snap_store.get_snapshot(pos.code)
                    if snap and snap.current_price > 0:
                        price = snap.current_price
                
                if price > 0:
                    if pos.current_price != price:
                        pos.current_price = price
                else:
                    # 실시간 데이터가 없는 경우
                    missing_codes.append(pos.code)
                    # 1순위: API 직접 조회 (TR 소모) - 하지만 이미 실시간 등록을 시도했을 것이므로 최소화
                    # 여기서는 안전장치로 기존값 유지 또는 1회성 조회만 수행
                    if pos.current_price <= 0:
                        price = self._kiwoom.get_current_price(pos.code)
                        if price > 0:
                            pos.current_price = price

            # 실시간 데이터가 누락된 종목이 있다면 스캐너에게 긴급 등록 요청
            if missing_codes and self._smart_scanner:
                # 30초마다 한 번씩만 호출되도록 SmartScanner 내부에서 관리됨
                logger.debug("[TradingController] 보유종목 %d개 실시간 데이터 누락 -> 등록 확인 요청", len(missing_codes))
                # run_periodic_scan이 아니더라도 다음 watch_q 갱신 주기에 반영되도록 함
                
        except Exception as e:
            logger.warning("[TradingController] 포트폴리오 가격 갱신 실패: %s", e)
            return

        # 갱신된 데이터 발행 (AppState 및 UI 시그널)
        _data = {
            "cash":      self._order_mgr.cash,
            "positions": dict(positions),
        }
        if self._ctx:
            self._ctx.update_portfolio(_data["cash"], _data["positions"])

        # 청산 판정 및 미체결 관리
        self.check_and_exit_all()
        self._order_mgr._check_failed_sells()
        self._order_mgr._check_pending_buys()

    def refresh_daily_candles(self, codes: list, idx: int) -> None:
        """
        일봉 데이터를 QTimer 체인으로 1종목씩 비동기 갱신.
        """
        if idx >= len(codes):
            logger.info("[TradingController] 일봉갱신 완료 — %d종목 처리", len(codes))
            return

        if getattr(self._kiwoom, "_tr_busy", False):
            from PyQt5.QtCore import QTimer
            logger.debug("[TradingController] 일봉갱신 TR 사용 중 — %s 스킵 후 다음 종목", codes[idx])
            QTimer.singleShot(350, lambda: self.refresh_daily_candles(codes, idx + 1))
            return

        # HealthMonitor ACK
        if self._health_monitor:
            self._health_monitor.ack()

        code = codes[idx]
        try:
            candles = self._kiwoom.get_daily_candles(code, count=120)
            if candles:
                self._snap_store.set_daily_candles(code, candles)
                logger.debug("[TradingController] 일봉갱신 %s 완료 (%d개)", code, len(candles))
        except Exception as e:
            logger.warning("[TradingController] 일봉갱신 %s 실패: %s", code, e)

        from PyQt5.QtCore import QTimer
        QTimer.singleShot(350, lambda: self.refresh_daily_candles(codes, idx + 1))
    def check_overnight_timecut(self) -> None:
        """
        EOD 포지션 익일 09:30 타임컷.
        overnight_held = True 이고 수익률 eod_timecut_min_pct 미달이면 강제 청산.
        """
        _min_pct = float(getattr(self._scan_cfg, "eod_timecut_min_pct", 1.0))

        for code, pos in list(self._order_mgr.positions.items()):
            if not getattr(pos, "overnight_held", False):
                continue
            chg = float(pos.price_change_pct_vs_avg)
            if chg < _min_pct:
                self.log_message.emit(
                    f"⏱️ [EOD타임컷] {pos.name}({code}) 09:30 수익 {chg:+.2f}% < {_min_pct:.1f}% — "
                    f"{pos.qty}주 강제 청산"
                )
                if hasattr(self._order_mgr, '_audit') and self._order_mgr._audit:
                    self._order_mgr._audit.log_sell_decision(
                        code, f"EOD 타임컷 09:30 수익 {chg:+.2f}% (기준 {_min_pct:.1f}%)", pos.current_price
                    )
                self._order_mgr.force_exit(code, pos.name, pos.qty,
                                          reason=f"EOD 타임컷 09:30 ({chg:+.2f}%)")
                logger.info("[EOD타임컷] %s(%s) %+.2f%%", pos.name, code, chg)
