from __future__ import annotations
import time
import logging
from datetime import datetime
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

logger = logging.getLogger("scanner.worker")

class ScannerWorker(QObject):
    """
    별도 QThread 에서 실행되는 스캐너 신호 판단 루프.
    """
    signal_detected    = pyqtSignal(object)        # ScanSignal
    watch_list_updated = pyqtSignal(list)         # list[dict]
    log_message        = pyqtSignal(str)

    def __init__(self, store, cfg, order_mgr, parent=None) -> None:
        super().__init__(parent)
        self._store      = store
        self._cfg        = cfg
        self._order_mgr  = order_mgr
        self._running    = False
        self._signal_prev_active: dict[str, bool] = {}
        self._signal_last_emit_mono: dict[str, float] = {}
        self._signal_cooldown_sec: float = float(getattr(cfg, "signal_cooldown_sec", 45.0))
        self._breakout_pending: dict[str, dict] = {}
        self._pre_surge_candidates: set[str] = set()
        self._entry_minute: int   = -1
        self._entry_count:  int   = 0
        self._entry_per_min: int  = int(getattr(cfg, "max_entries_per_minute", 1))
        self._last_ui_rows: list = []
        self._last_ui_emit: float = 0.0
        self._UI_INTERVAL: float = 1.0

    def stop(self) -> None:
        self._running = False
        logger.info("[ScannerWorker] 중단 신호 수신")

    @pyqtSlot()
    def run(self) -> None:
        if self._running:
            return
        
        # 터미널(콘솔) 강제 출력
        print("\n" + "="*60)
        print("📡 [ScannerWorker] run() 진입 - 엔진 시동 중...")
        print("="*60 + "\n")
        
        self._running = True
        # root 로거를 사용하여 터미널 출력 보장
        _log = logging.getLogger()

        try:
            self.log_message.emit("📡 [스캐너] 워커 모듈 로딩 중...")
            try:
                from scanner.signal_evaluator import (
                    check_breakout, check_jdm_entry, check_breakout_gate,
                    check_pre_surge, check_opening_scalp, check_opening_surge, check_eod_entry,
                    _resolve_time_slot, _get_slot_value,
                )
                from scanner.universe import is_pure_equity_name
                from scanner.indicator_service import IndicatorService as _is
                from scanner.scanner_logger import ScannerLogger as _SL
                _gts = _is.get_trend_status
            except Exception as ie:
                _log.critical("[ScannerWorker] 모듈 임포트 실패: %s", ie, exc_info=True)
                self.log_message.emit(f"❌ [스캐너] 모듈 로딩 실패: {ie}")
                return

            _log.info("[ScannerWorker] 초기화 성공 - 루프 시작")

            _empty_logged = False

            while self._running:
                t0 = time.monotonic()

                # 3. 데이터 확보
                # [FIX] 대시보드 시인성을 위해 최소 200개 종목을 넉넉하게 가져옴 (지수 제외 후 약 130~150개 노출 의도)
                _top_n = max(200, int(getattr(self._cfg, "display_top_n", 50)))
                top_df = self._store.top_by_trade_amount(_top_n)
                if top_df.empty:
                    if not _empty_logged:
                        _log.warning("[ScannerWorker] SnapshotStore 비어있음 — 데이터 대기 중")
                        _empty_logged = True
                    time.sleep(1.0)
                    continue
                _empty_logged = False

                rows = []
                signal_cnt = 0
                
                # ── 벡터화 사전필터 ──────────────────────────────────────────
                _now_t  = datetime.now().time()
                _slot   = _resolve_time_slot(_now_t, self._cfg)
                _max_ch = _get_slot_value(_slot, self._cfg, "max_change_pct",
                                          float(getattr(self._cfg, "max_change_pct", 15.0)))
                candidate_codes = set(self._store.prefilter_candidates(_max_ch))
                
                # Phase 1 후보 초기화
                if _slot not in ("PRE", "OPENING") and self._pre_surge_candidates:
                    _log.info("[Phase1] OPENING 슬롯 종료 — PRE_SURGE 후보 %d종목 초기화", len(self._pre_surge_candidates))
                    self._pre_surge_candidates.clear()

                seen_codes = set(top_df.index)
                _cool = self._signal_cooldown_sec
                _tnow = time.monotonic()
                _confirm_secs = float(getattr(self._cfg, "breakout_confirm_minutes", 3.0)) * 60.0
                _cancel_pct   = float(getattr(self._cfg, "breakout_cancel_drawdown_pct", -0.5))

                for code, row in top_df.iterrows():
                    # [DEBUG] 워커 생존 신고 및 데이터 개수 확인 (첫 10개 종목에 대해서만 1회성 로그)
                    if not _empty_logged:
                        _empty_logged = True
                        self.log_message.emit(f"🚀 [스캐너] {len(top_df)}개 종목 분석 시작...")
                        
                    try:
                        # [FIX] 종목명 추출 및 한글 깨짐 보정
                        name = str(row.get("name", "")).strip()
                        try:
                            if name and not any(ord(c) > 0x8800 for c in name):
                                name = name.encode('latin-1').decode('cp949')
                        except Exception:
                            pass
                            
                        if not is_pure_equity_name(name):
                            continue
                        
                        seen_codes.add(code)
                        snap = self._store.get_snapshot(code)
                        # [SIMPLIFY] 복잡한 로직 제거: 스냅샷 데이터를 기본으로 사용
                        snap = self._store.get_snapshot(code)
                        if snap is None: continue
                        
                        st = self._store.get_internal_state(code)
                        
                        # 키움 API/스냅샷의 값을 그대로 신뢰 (0이면 0인 대로 표시)
                        _pc = snap.prev_close
                        _cp = snap.current_price
                        _ta = snap.trade_amount
                        ch  = snap.change_pct
                        
                        # [🚑 긴급 진단 로그] 005930(삼성), 086520(에코프로), 076610(해성), 251370(와이엠티)
                        if code in ("005930", "086520", "076610", "251370"):
                             _log.debug("[UI진단] %s | pc=%d | cp=%d | ta=%d | ch=%.2f%%", 
                                       code, _pc, _cp, _ta, ch)

                        # 데이터 무결성 체크 (전략 분석용)
                        is_data_valid = (_pc > 0 and _cp > 0)
                        
                        is_trade_candidate = (ch < _max_ch)
                        if not is_trade_candidate:
                            self._signal_prev_active[code] = False
                        
                        sig_type = None
                        reason = None
                        _trend_text = "데이터부족"

                        # 추세 계산
                        if snap is not None and getattr(self._cfg, "yosep_trend_enabled", True):
                            _ema_p  = int(getattr(self._cfg, "yosep_ema_period",      20))
                            _atr_p  = int(getattr(self._cfg, "yosep_atr_period",      14))
                            _vol_lb = int(getattr(self._cfg, "yosep_volume_lookback", 20))
                            _need   = max(_ema_p + 1, _atr_p + 1)
                            _cl = list(snap.closes_1min or [])
                            _hi = list(snap.highs_1min or [])
                            _lo = list(snap.lows_1min or [])
                            _vl = list(snap.volumes_1min or [])
                            if len(_cl) >= _need and len(_hi) >= _need and len(_lo) >= _need:
                                _tlv = _gts(closes=_cl, highs=_hi, lows=_lo, volumes=_vl,
                                            ema_period=_ema_p, atr_period=_atr_p, volume_lookback=_vol_lb)
                                snap.trend_prev_level = snap.trend_level
                                snap.trend_level = int(_tlv)
                                self._store.update_trend_level(code, int(_tlv))

                                if snap.trend_level >= 3 and (snap.trend_prev_level is None or snap.trend_prev_level < 3):
                                    self.log_message.emit(f"🔥 [추세포착] {name}({code}) 강세 추세 진입 (Level 3)")

                                _ema_now = _is.calc_ema(_cl, _ema_p)
                                _atr_now = _is.calc_atr(_hi, _lo, _cl, _atr_p)
                                if _ema_now and _atr_now:
                                    _down_mult = float(getattr(self._cfg, "yosep_downtrend_block_atr", 0.8))
                                    if snap.current_price < (_ema_now - _atr_now * _down_mult):
                                        _trend_text = "하락"
                                    elif _tlv >= 3: _trend_text = "강세"
                                    elif _tlv == 2: _trend_text = "상승"
                                    elif _tlv == 1: _trend_text = "약세"
                                    else:           _trend_text = "횡보"

                        # 신호 판단
                        if is_data_valid and code in candidate_codes:
                            sig_type, reason = self._evaluate_signal(code, snap, row, candidate_codes, _slot, _tnow, _log, self._cfg, _is)
                            if sig_type:
                                signal_cnt += self._maybe_emit_signal(snap, sig_type, reason, code, _log, _tnow, snap.investor_score)
                        else:
                            self._signal_prev_active[code] = False

                        # 媛먯떆??row 援ъ꽦 (Snapshot ?곗씠???곗꽑)
                        # 媛먯떆??row 援ъ꽦 (InternalState ?곗씠???곗꽑)
                        rows.append({
                            "code":           code,
                            "name":           name,
                            "price":          _cp,
                            "change_pct":     ch,
                            "trade_amount":   _ta,
                            "signal":         sig_type or "",
                            "investor_score": st.inv_score if st else snap.investor_score,
                            "foreign_net":    st.inv_foreign if st else snap.foreign_net,
                            "inst_net":       st.inv_inst if st else snap.inst_net,
                            "trend_level":    st.trend_level if st else snap.trend_level,
                            "trend_prev":     st.trend_prev_level if st else snap.trend_prev_level,
                            "chejan":         st.chejan_str if st else snap.chejan_strength,
                            "trend_text":     _trend_text,
                        })
                    except Exception as e:
                        _log.error("[ScannerWorker] 종목 처리 에러 (%s): %s", code, e)
                        continue

                # UI 갱신
                now_ui = time.monotonic()
                if rows and (signal_cnt > 0 or (now_ui - self._last_ui_emit) >= self._UI_INTERVAL):
                    self.watch_list_updated.emit(rows)
                    self._last_ui_emit = now_ui

                elapsed = time.monotonic() - t0
                time.sleep(max(0.01, 1.0 - elapsed))

        except Exception as e:
            _log.critical("[ScannerWorker] 치명적 에러로 루프 중단: %s", e, exc_info=True)
            self.log_message.emit(f"❌ [스캐너] 치명적 에러: {e}")
        finally:
            self._running = False
            _log.info("[ScannerWorker] 워커 종료")

    def _evaluate_signal(self, code: str, snap, row, candidate_codes: set,
                         slot: str, tnow: float, _log, _cfg, _is) -> tuple[str | None, str | None]:
        from scanner.signal_evaluator import (
            check_eod_entry, check_pre_surge, check_opening_scalp, check_jdm_entry,
            check_breakout, check_breakout_gate
        )
        sig_type = None
        reason = None

        if snap is None:
            return None, None

        if slot == "EOD":
            reason = check_eod_entry(snap, _cfg)
            if reason: sig_type = "EOD_ENTRY"
        elif slot == "PRE":
            reason = check_pre_surge(snap, _cfg)
            if reason:
                sig_type = "PRE_SURGE"
                self._pre_surge_candidates.add(code)
        elif slot == "OPENING":
            _phase1_min = int(getattr(_cfg, "phase1_min_candles", 3))
            if code in self._pre_surge_candidates and len(snap.closes_1min) >= _phase1_min:
                reason = check_opening_scalp(snap, _cfg)
                if reason: sig_type = "OPENING_SCALP"
            if not sig_type:
                reason = check_jdm_entry(snap, _cfg)
                if reason: sig_type = "JDM_ENTRY"
        else:
            # MORNING / MIDDAY / AFTERNOON
            from scanner.scanner_logger import ScannerLogger as _SL
            _dg = _is.check_daily_alignment(snap.daily_closes, snap.current_price)
            _ma_ok = True
            if getattr(_cfg, "daily_ma20_filter_enabled", True):
                if not _dg.get("above_ma20", False) and _dg.get("daily_ma20", 0) > 0:
                    _SL.rejected(code, snap.name, "JDM_DAILY_MA20", f"일봉 20MA 하방")
                    _ma_ok = False
            if _ma_ok and getattr(_cfg, "daily_ma60_filter_enabled", True):
                if not _dg.get("above_ma60", False) and _dg.get("daily_ma60", 0) > 0:
                    _SL.rejected(code, snap.name, "JDM_DAILY_MA60", f"일봉 60MA 하방")
                    _ma_ok = False
            
            if _ma_ok:
                breakout_reason = check_breakout(snap, _cfg.breakout_ratio, _cfg.breakout_volume_mult)
                if breakout_reason:
                    pending = self._breakout_pending.get(code)
                    if pending is None:
                        _gate = check_breakout_gate(snap, _cfg)
                        if _gate:
                            self._breakout_pending[code] = {"first_time": tnow, "first_price": snap.current_price}
                            _log.info("[BREAKOUT대기] %s(%s) 관찰 시작", snap.name, code)
                    else:
                        elapsed = tnow - pending["first_time"]
                        _conf = float(getattr(_cfg, "breakout_confirm_minutes", 3.0)) * 60.0
                        if elapsed >= _conf:
                            sig_type = "BREAKOUT"
                            reason = f"{breakout_reason} | {elapsed/60:.1f}분 유지"
                            del self._breakout_pending[code]
                
                if not sig_type:
                    reason = check_jdm_entry(snap, _cfg)
                    if reason: sig_type = "JDM_ENTRY"

        return sig_type, reason

    def _maybe_emit_signal(self, snap, sig_type: str, reason: str, code: str, _log, _tnow: float, _iscore: int) -> int:
        _cool = self._signal_cooldown_sec
        prev_active = self._signal_prev_active.get(code, False)
        last_emit = self._signal_last_emit_mono.get(code)
        cooldown_ok = (last_emit is None) or (_tnow - last_emit >= _cool)
        
        _cur_min = datetime.now().minute
        if _cur_min != self._entry_minute:
            self._entry_minute = _cur_min
            self._entry_count = 0
        
        if sig_type and not prev_active and cooldown_ok and self._entry_count < self._entry_per_min:
            _log.info("[ScannerWorker] 신호 발생: %s(%s) [%s] %s", snap.name, code, sig_type, reason)
            from scanner.smart_scanner import ScanSignal
            self.signal_detected.emit(ScanSignal(code, snap.name, sig_type, snap.current_price, reason))
            self._entry_count += 1
            self._signal_last_emit_mono[code] = _tnow
            self._signal_prev_active[code] = True
            return 1
        
        self._signal_prev_active[code] = (sig_type is not None)
        return 0

class PortfolioWorker(QObject):
    refresh_done = pyqtSignal(dict)
    log_message  = pyqtSignal(str)

    def __init__(self, order_manager, trading_controller=None, parent=None) -> None:
        super().__init__(parent)
        self._om = order_manager
        self._tc = trading_controller
        self._balance_result: dict = {}
        self._timers: list[QTimer] = []

    def _schedule_retry(self, delay_ms: int, fn) -> None:
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(fn)
        t.start(delay_ms)
        self._timers.append(t)

    @pyqtSlot()
    def sync(self) -> None:
        _kw = getattr(self._om, "_kiwoom", None)
        scan_busy = self._tc and getattr(self._tc, '_scan_in_progress', False)
        if (_kw and getattr(_kw, "_tr_busy", False)) or scan_busy:
            self._schedule_retry(3000, self.sync)
            return
        try:
            self._om._roll_daily_state_if_needed()
            balance = self._om._kiwoom.get_balance()
            if not balance: return
            self._balance_result = balance
            self._schedule_retry(350, self._sync_step2)
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step1] {e}")

    @pyqtSlot()
    def _sync_step2(self) -> None:
        _kw = getattr(self._om, "_kiwoom", None)
        if _kw and getattr(_kw, "_tr_busy", False):
            self._schedule_retry(1000, self._sync_step2)
            return
        try:
            cash = self._om._sync_with_balance(self._balance_result)
            if self._tc: self._tc.update_portfolio_prices()
            self.refresh_done.emit({"cash": cash, "positions": dict(self._om.positions)})
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step2] {e}")

    def stop(self) -> None:
        for t in self._timers: t.stop()
        self._timers.clear()
