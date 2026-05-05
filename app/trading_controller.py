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
        self._ctx = ctx  # AppState 참조 (선택적)
        self._auto_trading = False
        self._first_signal_received = False  # 첫 신호 여부 추적
        
        self._scan_in_progress = False
        self._market_crash_off = False
        self._kospi_chg_pct = 0.0
        self._kosdaq_chg_pct = 0.0
        self._kospi_cur = 0.0
        self._kosdaq_cur = 0.0

        self._strategy = JangDongMinStrategy(
            self._order_mgr, self._risk_mgr, self._scan_cfg, self._snap_store
        )
        
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
        """신호 필터링 (Phase1 태깅 + EntryStrategy 위임)"""
        # Phase1 태깅 및 한도 체크
        if sig.signal_type == "OPENING_SCALP":
            sig.entry_phase = 1
            _ph1_max = int(getattr(self._scan_cfg, "phase1_max_positions", 3))
            _ph1_count = sum(
                1 for p in self._order_mgr.positions.values()
                if getattr(p, "entry_phase", 0) == 1
            )
            if _ph1_count >= _ph1_max:
                self.signal_rejected.emit(
                    f"{sig.code}: Phase1 한도 — {_ph1_count}/{_ph1_max}"
                )
                self._record_signal(sig)
                return False
        else:
            sig.entry_phase = 2

        # 자동매매 자동 시작: 첫 신호 발생 시, 아직 활성화 안 됐고 급락 OFF 상태면
        if not self._auto_trading and not self._market_crash_off and not self._first_signal_received:
            self._first_signal_received = True
            self.auto_trade_started.emit()
            logger.info("[TradingController] 첫 신호 발생 — 자동매매 자동 시작 요청")

        passed, reason = self._strategy.should_entry(sig, self._auto_trading)

        if not passed:
            self.signal_rejected.emit(f"{sig.code}: {reason}")
            self._record_signal(sig)
            return False

        # ✅ AI 필터 검증 (필터 체인 마지막 단계)
        snap = self._snap_store.get_snapshot(sig.code) if self._snap_store else None
        if snap:
            from analysis.feature_engineer import extract_ml_features
            features = extract_ml_features(sig, snap, self._scan_cfg)
            
            # AI 판정 실행 (설정된 임계값 사용)
            ai_thr = float(getattr(self._scan_cfg, "ai_threshold", 0.5))
            ai_passed, win_rate = self._ai_filter.should_enter(features, threshold=ai_thr)
            
            msg = f"🤖 [AI분석] {sig.name}({sig.code}) 예상승률 {win_rate*100:.1f}%"
            if not ai_passed:
                self.log_message.emit(f"{msg} → 진입 부적합 (거절, 기준 {ai_thr*100:.0f}%)")
                self.signal_rejected.emit(f"{sig.code}: AI 거절 ({win_rate*100:.0f}%)")
                return False
            
            # 승인 시 로그 (모델이 준비된 경우만)
            if self._ai_filter.is_ready:
                self.log_message.emit(f"{msg} → 진입 승인 (기준 {ai_thr*100:.0f}%)")

        # ✅ RS 필터 검증 (지수 대비 강도)
        if snap:
            rs_score = features.get("rs_score", 0)
            rs_thr = float(getattr(self._scan_cfg, "rs_threshold", 0.0))
            if rs_score < rs_thr:
                self.log_message.emit(f"📉 [RS필터] {sig.name} RS={rs_score:.2f} (기준 {rs_thr:.2f}) → 거절")
                self.signal_rejected.emit(f"{sig.code}: RS 필터 거절 ({rs_score:.2f})")
                return False
            else:
                if getattr(self._scan_cfg, "exploration_mode", False):
                    self.log_message.emit(f"📈 [RS필터] {sig.name} RS={rs_score:.2f} (기준 {rs_thr:.2f}) → 데이터 수집 통과")
                else:
                    self.log_message.emit(f"📈 [RS필터] {sig.name} RS={rs_score:.2f} (지수 대비 강세) → 통과")

        # ✅ 모든 필터 통과 → 진입 신호 전송
        self._order_mgr.handle_signal(sig)
        self._record_signal(sig)
        return True

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
        """차트 표시용 데이터 조회 — 캔들, 포지션, 위험 파라미터"""
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
            # 1. 캔들 데이터 (1분봉 100개, 없으면 일봉 40개)
            candles = self._kiwoom.get_min_candles(code, tick_unit=1, count=100)
            if not candles:
                candles = self._kiwoom.get_daily_candles(code, count=40)

            if candles:
                result["closes"] = [c['close'] for c in candles]
                result["volumes"] = [c['volume'] for c in candles]

            # 2. 종목명
            result["name"] = self._kiwoom.get_stock_name(code)

            # 3. 포지션 정보
            pos = self._order_mgr.positions.get(code)
            result["position"] = pos

            # 4. 전략 파라미터
            result["sl_pct"] = float(getattr(self._scan_cfg, "jdm_stop_loss_pct", -1.5))

            # 5. 트레일 스탑가
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


    def check_and_exit_all(self) -> None:
        """모든 포지션 청산 판정 (매분 호출)"""
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
                # watch_q.subscribed 대신 방금 스캔된 top_codes를 우선 사용
                watch_codes = set(top_codes) if top_codes else set(getattr(self._smart_scanner.watch_q, "subscribed", set()))
                pos_codes = set(self._order_mgr.positions.keys())
                removed = self._snap_store.cleanup_stale_data(watch_codes | pos_codes)
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

    @pyqtSlot()
    def check_market_crash(self) -> None:
        """지수 급락 감지 및 신규 진입 차단 (60초마다)"""
        # 장 시작 전(08:30 이전)에는 지수 데이터 미제공 — 스킵
        from datetime import datetime, time as _time
        now_t = datetime.now().time()
        if now_t < _time(8, 30):
            logger.debug("[check_market_crash] 장 전 (%s) — 지수 조회 스킵", now_t.strftime("%H:%M"))
            return

        if getattr(self._kiwoom, '_tr_busy', False):
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(5_000, self.check_market_crash)
            return

        # 1. 지수 조회 (코스피, 코스닥)
        kp = self._kiwoom.get_index_info("001")
        kd = self._kiwoom.get_index_info("101")

        # 조회 실패 시 캐시된 값 유지
        if kp:
            self._kospi_cur = kp['current']
            self._kospi_chg_pct = kp['change_pct']
        else:
            logger.warning("[check_market_crash] KOSPI 조회 실패 — 기존값 유지 (%.2f)", self._kospi_cur)

        if kd:
            self._kosdaq_cur = kd['current']
            self._kosdaq_chg_pct = kd['change_pct']
        else:
            logger.warning("[check_market_crash] KOSDAQ 조회 실패 — 기존값 유지 (%.2f)", self._kosdaq_cur)
        
        # [NEW] RS 필터 및 지표 계산을 위해 Config에 지수 정보 동기화
        if self._scan_cfg:
            self._scan_cfg.kospi_chg_pct = self._kospi_chg_pct
            self._scan_cfg.kosdaq_chg_pct = self._kosdaq_chg_pct

        logger.info("[지수업데이트] KOSPI: %.2f(%.2f%%) / KOSDAQ: %.2f(%.2f%%)", 
                    kp['current'], kp['change_pct'], kd['current'], kd['change_pct'])

        # 2. 급락 여부 판단 (index_block_pct -1.5%보다 더 심한 -2.0% 기준)
        crash_limit = -2.0
        is_crash = (self._kospi_chg_pct <= crash_limit or self._kosdaq_chg_pct <= crash_limit)

        # 3. 급락 감지 신호 발행 (상태 변경은 슬롯에서 처리)
        if is_crash and not self._market_crash_off:
            self.market_crash_detected.emit(self._kospi_chg_pct, self._kosdaq_chg_pct)
        
        # UI 업데이트 신호 발생 (KOSPI 현재/%, KOSDAQ 현재/%, 급락여부)
        # 실패하더라도 0.0 또는 기존값을 보냄으로써 UI가 갱신되도록 함
        self.market_data_updated.emit(
            self._kospi_cur, self._kospi_chg_pct,
            self._kosdaq_cur, self._kosdaq_chg_pct,
            is_crash
        )
        logger.debug("[check_market_crash] market_data_updated 시그널 발행 완료 (is_crash=%s)", is_crash)

    @pyqtSlot(float, float)
    def _on_market_crash_detected(self, kospi_pct: float, kosdaq_pct: float) -> None:
        """지수 급락 신호 수신 — 자동매매 긴급 정지"""
        self._market_crash_off = True
        self._auto_trading = False
        self.log_message.emit(f"🔴 [지수급락] 코스피 {kospi_pct}% / 코스닥 {kosdaq_pct}% — 자동매매 긴급 정지")

    def check_overnight_gap(self) -> None:
        """EOD 포지션 익일 갭 확인"""
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
            for pos in positions.values():
                price = 0
                if self._snap_store:
                    snap = self._snap_store.get_snapshot(pos.code)
                    if snap and snap.current_price > 0:
                        price = snap.current_price
                
                if price <= 0:
                    price = self._kiwoom.get_current_price(pos.code)
                
                if price > 0 and pos.current_price != price:
                    pos.current_price = price
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
