from __future__ import annotations
import time
from datetime import datetime
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer


class ScannerWorker(QObject):
    """
    별도 QThread 에서 실행되는 스캐너 신호 판단 루프.


    SnapshotStore (DataFrame 캐시) 만 읽는다 — kiwoom TR 호출 없음.
    signal_detected 는 (1) 에지: 직전 스캔에 없던 신호가 이번에만 켜질 때,
    (2) 쿨다운: 동일 종목 마지막 emit 이후 signal_cooldown_sec 초가 지난 뒤에만 재허용.
    감시표의 signal 열은 여전히 “지금 조건 만족 여부”를 표시한다.
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
        # 매수 신호: 에지(조건 꺼짐→켜짐) + 짧은 쿨다운(동일 종목 재 emit 간격)
        self._signal_prev_active: dict[str, bool] = {}
        self._signal_last_emit_mono: dict[str, float] = {}
        self._signal_cooldown_sec: float = float(
            getattr(cfg, "signal_cooldown_sec", 45.0)
        )
        # BREAKOUT watch-and-confirm: 즉시 매수 대신 N분 관찰 후 진입
        # code → {"first_time": float(monotonic), "first_price": int}
        self._breakout_pending: dict[str, dict] = {}
        # Phase 1 아침 초단타 — PRE_SURGE 후보 코드 집합 (OPENING 슬롯 OPENING_SCALP 라우팅용)
        self._pre_surge_candidates: set[str] = set()
        # 분당 신호 발행 제한 — 같은 분에 너무 많은 종목 동시 진입 방지
        self._entry_minute: int   = -1   # 마지막 신호 발행 분(minute)
        self._entry_count:  int   = 0    # 해당 분에 발행한 신호 수
        self._entry_per_min: int  = int(getattr(cfg, "max_entries_per_minute", 1))
        # UI 갱신 쓰로틀 — QTableWidget 재렌더링을 3초 간격으로 제한
        self._last_ui_rows: list = []
        self._last_ui_emit: float = 0.0
        self._UI_INTERVAL: float = 3.0


    @pyqtSlot()
    def run(self) -> None:
        import logging as _logging
        _log = _logging.getLogger("ScannerWorker")
        from scanner.signal_evaluator import (
            check_breakout, check_jdm_entry, check_breakout_gate,
            check_pre_surge, check_opening_scalp, check_opening_surge, check_eod_entry,
            _resolve_time_slot, _get_slot_value,
        )
        from scanner.universe import is_pure_equity_name

        from scanner.indicator_service import IndicatorService
        _gts = IndicatorService.get_trend_status
        self._running = True
        self.log_message.emit("[ScannerWorker] 시작 — SnapshotStore 데이터 대기 중...")
        _log.info("[ScannerWorker] run() 진입")


        _empty_logged = False
        _heartbeat_last: float = 0.0
        _HEARTBEAT_INTERVAL: float = 60.0  # 1분마다 생존 로그
        _eod_warn_logged: bool = False      # EOD 창 야간보유 OFF 경고 (1회)
        while self._running:
            t0 = time.monotonic()


            # 하트비트: 1분마다 루프 정상 동작 확인
            if t0 - _heartbeat_last >= _HEARTBEAT_INTERVAL:
                _heartbeat_last = t0
                _overnight = getattr(self._cfg, "overnight_mode_enabled", False)
                _log.info("[ScannerWorker] ♥ 루프 정상 동작 중 — 야간보유=%s | 감시=%d종목",
                          "ON" if _overnight else "OFF", len(self._store))


            top_df = self._store.top_by_trade_amount(self._cfg.display_top_n)


            if top_df.empty:
                if not _empty_logged:
                    self.log_message.emit(
                        "[ScannerWorker] SnapshotStore 비어있음 — 데이터 수집 대기 중"
                    )
                    _log.debug("[ScannerWorker] SnapshotStore 비어있음")
                    _empty_logged = True
                
                # 초기 실행 시 데이터가 없으면 1초만 대기하고 다시 확인 (기존에는 scan_interval 만큼 대기함)
                time.sleep(1.0)
                continue


            _empty_logged = False
            rows = []
            signal_cnt = 0


            # 손절/익절은 MainWindow._auto_sell_by_pnl 에서만 처리한다.
            # (구) Worker가 전 보유종목 전량을 1초마다 검사해 HTS·전일 보유분까지 시작 직후 매도하던 문제 방지.


            # ── 벡터화 사전필터 ──────────────────────────────────────────
            # DataFrame 연산으로 시가 돌파 / 양봉 기조 미충족 종목을 먼저 제거.
            # 보통 50종목 → 5~15종목으로 줄어 이후 Python 루프 비용 70~90% 감소.
            # 등락률 상한(config RISK.max_change_pct) 이상은 후보·감시표에서 제외.
            # [NEW] 시간대 슬롯 기반 등락률 상한 동적 선택 (2026-04-08)
            _now_t  = datetime.now().time()
            _slot   = _resolve_time_slot(_now_t, self._cfg)
            _max_ch = _get_slot_value(_slot, self._cfg, "max_change_pct",
                                      float(getattr(self._cfg, "max_change_pct", 15.0)))
            candidate_codes = set(self._store.prefilter_candidates(_max_ch))


            # Phase 1 후보 초기화 — OPENING 슬롯 종료 후 더 이상 유효하지 않음
            if _slot not in ("PRE", "OPENING") and self._pre_surge_candidates:
                _log.info("[Phase1] OPENING 슬롯 종료 — PRE_SURGE 후보 %d종목 초기화",
                          len(self._pre_surge_candidates))
                self._pre_surge_candidates.clear()


            seen_codes = set(top_df.index)
            _cool = self._signal_cooldown_sec
            _tnow = time.monotonic()
            _confirm_secs = float(getattr(self._cfg, "breakout_confirm_minutes", 3.0)) * 60.0
            _cancel_pct   = float(getattr(self._cfg, "breakout_cancel_drawdown_pct", -0.5))


            from scanner.indicator_service import IndicatorService as _is
            for code, row in top_df.iterrows():
                name = str(row.get("name", ""))
                if not is_pure_equity_name(name):
                    continue
                # pandas Series에서 값 안전하게 추출
                cp = row.get("change_pct", 0)
                ch = float(cp) if cp else 0.0
                # [진단] 등락률이 높은 종목 로깅
                if ch >= _max_ch:
                    _log.debug("[신호필터] %s — 등락률 %.2f%% >= 상한 %.1f%% 제외",
                               name, ch, _max_ch)
                    self._signal_prev_active[code] = False
                    continue
                sig_type = None
                reason = None
                _trend_text = "데이터부족"


                # ① 추세 계산 — 모든 감시 종목에서 시도 (candidate_codes 여부 무관)
                snap = self._store.get_snapshot(code)
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
                        _tlv = _gts(
                            closes=_cl, highs=_hi, lows=_lo, volumes=_vl,
                            ema_period=_ema_p, atr_period=_atr_p,
                            volume_lookback=_vol_lb,
                        )
                        snap.trend_prev_level = snap.trend_level
                        snap.trend_level = int(_tlv)
                        self._store.update_trend_level(code, int(_tlv))

                        _ema_now = _is.calc_ema(_cl, _ema_p)
                        _atr_now = _is.calc_atr(_hi, _lo, _cl, _atr_p)
                        _down_mult = float(getattr(self._cfg, "yosep_downtrend_block_atr", 0.8))
                        if _ema_now and _atr_now:
                            if snap.current_price < (_ema_now - _atr_now * _down_mult):
                                _trend_text = "하락"
                            elif _tlv >= 3: _trend_text = "강세"
                            elif _tlv == 2: _trend_text = "상승"
                            elif _tlv == 1: _trend_text = "약세"
                            else:           _trend_text = "횡보"

                # ② 신호 판단 — candidate_codes에만 수행
                if code in candidate_codes:
                    sig_type, reason = self._evaluate_signal(
                        code, snap, row, candidate_codes, _slot, _now_t, _log, self._cfg, _is
                    )
                    signal_cnt += self._maybe_emit_signal(
                        snap, sig_type, reason, code, _log, _tnow, snap.investor_score
                    )
                else:
                    self._signal_prev_active[code] = False

                # 감시표 row 구성
                rows.append({
                    "code":           code,
                    "name":           name,
                    "price":          snap.current_price,
                    "change_pct":     ch,
                    "trade_amount":   snap.trade_amount,
                    "signal":         sig_type or "",
                    "investor_score": snap.investor_score,
                    "foreign_net":    snap.foreign_net,
                    "inst_net":       snap.inst_net,
                    "trend_level":    snap.trend_level,
                    "trend_prev":     snap.trend_prev_level,
                    "chejan":         snap.chejan_strength,
                    "trend_text":     _trend_text,
                })
            for _c in list(self._signal_prev_active.keys()):
                if _c not in seen_codes:
                    del self._signal_prev_active[_c]
            # 감시표에서 사라진 종목의 BREAKOUT 대기도 정리
            for _c in list(self._breakout_pending.keys()):
                if _c not in seen_codes:
                    del self._breakout_pending[_c]


            # UI 갱신 쓰로틀:
            # - 신호가 새로 발생했거나 3초가 지났을 때만 emit
            # - 데이터 내용이 같으면 QTableWidget 불필요한 재렌더링 방지
            now_ui = time.monotonic()
            has_new_signal = signal_cnt > 0
            time_ok = (now_ui - self._last_ui_emit) >= self._UI_INTERVAL
            if rows and (has_new_signal or time_ok):
                self.watch_list_updated.emit(rows)
                self._last_ui_emit = now_ui
                _log.debug("[ScannerWorker] watch_list_updated %d종목 (신호 %d개)", len(rows), signal_cnt)


            elapsed = time.monotonic() - t0
            # scan_interval은 opt10030 주기 스캔 간격 (60s) — 신호 감지 루프와 무관
            # ScannerWorker는 1초마다 실행하여 신호/추세 판단을 빠르게 유지
            time.sleep(max(0.0, 1.0 - elapsed))


    def _evaluate_signal(self, code: str, snap, row, candidate_codes: set,
                         slot: str, now_t, _log, _cfg, _is) -> tuple[str | None, str | None]:
        """
        신호 판정 로직 분리 — candidate_codes에 속한 종목만 실행.


        Returns: (sig_type, reason) — 신호 없으면 (None, None)
        """
        sig_type = None
        reason = None


        if snap is None:
            _log.debug("[ScannerWorker] %s 스냅샷 없음", code)
            self._signal_prev_active[code] = False
            return sig_type, reason


        # ── 슬롯별 신호 라우팅 ────────────────────────────────
        from scanner.signal_evaluator import (
            check_eod_entry, check_pre_surge, check_opening_scalp, check_jdm_entry,
            check_breakout, check_breakout_gate
        )
        from scanner.models import ScanSignal
        from scanner.indicator_service import IndicatorService


        # EOD 종가매매 창(14:40~14:55) — overnight_mode_enabled 시 우선 체크
        _eod_start = getattr(self._cfg, "eod_entry_start", None)
        _eod_end   = getattr(self._cfg, "eod_entry_end", None)
        _eod_time_match = (
            _eod_start is not None and _eod_end is not None
            and _eod_start <= now_t < _eod_end
        )
        _is_eod_window = (
            getattr(self._cfg, "overnight_mode_enabled", False)
            and _eod_time_match
        )


        if _is_eod_window:
            reason = check_eod_entry(snap, self._cfg)
            if reason:
                sig_type = "EOD_ENTRY"


        elif slot == "PRE":
            reason = check_pre_surge(snap, self._cfg)
            if reason:
                sig_type = "PRE_SURGE"
                self._pre_surge_candidates.add(code)


        elif slot == "OPENING":
            _phase1_min = int(getattr(self._cfg, "phase1_min_candles", 3))
            if (code in self._pre_surge_candidates
                    and len(snap.closes_1min) >= _phase1_min):
                reason = check_opening_scalp(snap, self._cfg)
                if reason:
                    sig_type = "OPENING_SCALP"


            if not sig_type and len(snap.closes_1min) >= (self._cfg.jdm_ma_short + 1):
                from scanner.indicator_service import IndicatorService as _is_gate
                from scanner.scanner_logger import ScannerLogger as _SL
                _dg = _is_gate.check_daily_alignment(snap.daily_closes, snap.current_price)
                _ma_ok = True
                if getattr(self._cfg, "daily_ma20_filter_enabled", True):
                    if not _dg["above_ma20"] and _dg["daily_ma20"] > 0:
                        _SL.rejected(code, snap.name, "DAILY_MA20",
                                     f"일봉 20MA 하방 — {snap.current_price:,} < {_dg['daily_ma20']:,.0f}")
                        self._signal_prev_active[code] = False
                        _ma_ok = False
                if _ma_ok and getattr(self._cfg, "daily_ma60_filter_enabled", True):
                    if not _dg["above_ma60"] and _dg["daily_ma60"] > 0:
                        _SL.rejected(code, snap.name, "DAILY_MA60",
                                     f"일봉 60MA 하방 — {snap.current_price:,} < {_dg['daily_ma60']:,.0f}")
                        self._signal_prev_active[code] = False
                        _ma_ok = False
                if _ma_ok:
                    reason = check_jdm_entry(snap, self._cfg)
                    if reason:
                        sig_type = "JDM_ENTRY"


        else:
            # MORNING / MIDDAY / AFTERNOON
            from scanner.indicator_service import IndicatorService as _is_gate
            from scanner.scanner_logger import ScannerLogger as _SL
            _dg = _is_gate.check_daily_alignment(snap.daily_closes, snap.current_price)
            _ma_ok = True
            if getattr(self._cfg, "daily_ma20_filter_enabled", True):
                if not _dg["above_ma20"] and _dg["daily_ma20"] > 0:
                    _SL.rejected(code, snap.name, "DAILY_MA20",
                                 f"일봉 20MA 하방 — 현재가 {snap.current_price:,} "
                                 f"< 20MA {_dg['daily_ma20']:,.0f}")
                    self._signal_prev_active[code] = False
                    _ma_ok = False
            if _ma_ok and getattr(self._cfg, "daily_ma60_filter_enabled", True):
                if not _dg["above_ma60"] and _dg["daily_ma60"] > 0:
                    _SL.rejected(code, snap.name, "DAILY_MA60",
                                 f"일봉 60MA 하방 — 현재가 {snap.current_price:,} "
                                 f"< 60MA {_dg['daily_ma60']:,.0f} (중기 하락 추세)")
                    self._signal_prev_active[code] = False
                    _ma_ok = False


            if _ma_ok:
                # ── BREAKOUT: 즉시매수 대신 N분 watch-and-confirm ──
                _tnow = time.monotonic()
                _confirm_secs = float(getattr(self._cfg, "breakout_confirm_minutes", 3.0)) * 60.0
                _cancel_pct   = float(getattr(self._cfg, "breakout_cancel_drawdown_pct", -0.5))


                breakout_reason = check_breakout(
                    snap,
                    self._cfg.breakout_ratio,
                    self._cfg.breakout_volume_mult,
                    float(getattr(self._cfg, "breakout_pullback_from_high_pct", 1.5)),
                    int(getattr(self._cfg, "breakout_min_rising_bars", 2)),
                )


                if breakout_reason:
                    _tlevel_now = int(getattr(snap, "trend_level", 0))
                    if slot == "AFTERNOON":
                        _min_trend_req = int(getattr(self._cfg,
                            "yosep_min_trend_level_afternoon", 3))
                    else:
                        _min_trend_req = int(getattr(self._cfg,
                            "yosep_min_trend_level", 1))
                    _breakout_trend_blocked = _tlevel_now < _min_trend_req
                    if _breakout_trend_blocked:
                        if code in self._breakout_pending:
                            del self._breakout_pending[code]
                        _log.debug(
                            "[BREAKOUT차단] %s(%s) 추세Lv%d < 최소Lv%d [%s] — 대기 등록 스킵",
                            snap.name, code, _tlevel_now, _min_trend_req, slot,
                        )


                    if not _breakout_trend_blocked:
                        pending = self._breakout_pending.get(code)
                        if pending is None:
                            _tlevel = int(getattr(snap, "trend_level", 0))


                            # Fast-Track 로직
                            _is_opening_slot = (slot == "OPENING")
                            _fast_track_0s = False


                            _rank = getattr(snap, "rank", 0)
                            if _rank and _rank > 0 and _rank <= int(getattr(self._cfg, "scoring_rank_bonus", 10)):
                                _fast_track_0s = True


                            _surge_lookback = int(getattr(self._cfg, "volume_surge_lookback", 10))
                            if snap.volumes_1min and len(snap.volumes_1min) >= _surge_lookback + 1:
                                _avg_vol = sum(snap.volumes_1min[-(_surge_lookback+1):-1]) / _surge_lookback
                                _cur_vol = snap.volumes_1min[-1]
                                if _avg_vol > 0 and (_cur_vol / _avg_vol) >= float(getattr(self._cfg, "scoring_vol_surge_bonus", 2.0)):
                                    _fast_track_0s = True


                            if _fast_track_0s:
                                _eff_secs = 0.0
                                _log.info("🚀 [Fast-Track] %s(%s) 강력한 수급 보너스 — 즉시 진입 (0초)", snap.name, code)
                            elif _is_opening_slot:
                                _eff_secs = 20.0
                                _log.info("⏱️ [Fast-Track] %s(%s) OPENING 슬롯 — 대기 시간 20초 단축", snap.name, code)
                            elif _tlevel >= 3:
                                _eff_secs = float(getattr(self._cfg,
                                    "breakout_confirm_minutes_trend3", 0.0)) * 60.0
                            elif _tlevel >= 2:
                                _eff_secs = float(getattr(self._cfg,
                                    "breakout_confirm_minutes_trend2", 1.0)) * 60.0
                            elif _tlevel >= 1:
                                _eff_secs = float(getattr(self._cfg,
                                    "breakout_confirm_minutes_trend1", 0.0)) * 60.0
                            else:
                                _eff_secs = _confirm_secs


                            _gate_at_create = check_breakout_gate(snap, self._cfg)
                            if _gate_at_create is None:
                                _log.debug(
                                    "[BREAKOUT게이트] %s(%s) 생성 시점 gate 실패 — 대기 등록 스킵",
                                    snap.name, code,
                                )
                            else:
                                self._breakout_pending[code] = {
                                    "first_time":   _tnow,
                                    "first_price":  snap.current_price,
                                    "confirm_secs": _eff_secs,
                                    "trend_level":  _tlevel,
                                    "gate_reason":  _gate_at_create,
                                }
                                _log.info(
                                    "[BREAKOUT대기] %s(%s) %.0f원 — %.1f분 관찰 시작 (추세Lv%d)",
                                    snap.name, code, snap.current_price,
                                    _eff_secs / 60, _tlevel,
                                )
                        else:
                            elapsed   = _tnow - pending["first_time"]
                            fp        = pending["first_price"]
                            _eff_secs = pending.get("confirm_secs", _confirm_secs)
                            _tlevel   = pending.get("trend_level", 0)
                            drawdown  = (snap.current_price - fp) / fp * 100 if fp > 0 else 0.0
                            if drawdown <= _cancel_pct:
                                _log.info(
                                    "[BREAKOUT취소] %s(%s) 하락 %.2f%% ≤ %.1f%% — 대기 해제",
                                    snap.name, code, drawdown, _cancel_pct,
                                )
                                del self._breakout_pending[code]
                            elif elapsed >= _eff_secs:
                                if _eff_secs == 0.0:
                                    _gate = pending.get("gate_reason")
                                else:
                                    _gate = check_breakout_gate(snap, self._cfg)
                                if _gate is None:
                                    del self._breakout_pending[code]
                                else:
                                    sig_type = "BREAKOUT"
                                    _confirm_label = (
                                        f"즉시확인(추세Lv{_tlevel})"
                                        if _eff_secs == 0
                                        else f"{elapsed/60:.1f}분 유지 확인(추세Lv{_tlevel})"
                                    )
                                    reason = (
                                        f"{breakout_reason} | {_confirm_label}"
                                        f" (초기가 {fp:,}→현재 {snap.current_price:,})"
                                        f" | {_gate}"
                                    )
                                    del self._breakout_pending[code]
                            else:
                                _log.debug(
                                    "[BREAKOUT관찰중] %s(%s) %.1f/%.1f분 경과, 등락 %.2f%% (추세Lv%d)",
                                    snap.name, code, elapsed / 60, _eff_secs / 60,
                                    drawdown, _tlevel,
                                )
                    else:
                        if code in self._breakout_pending:
                            _log.info(
                                "[BREAKOUT해제] %s(%s) 돌파 조건 소멸 — 대기 취소",
                                snap.name, code,
                            )
                            del self._breakout_pending[code]


                # ── JDM_ENTRY (LITE 포함) ──
                if not reason:
                    reason = check_jdm_entry(snap, self._cfg)
                    if reason:
                        sig_type = "JDM_ENTRY"


        # ── 수급 점수 반영 ──────────────────────────────────
        _iscore = snap.investor_score
        if sig_type and self._cfg.investor_filter_enabled:
            if _iscore == 1:
                reason = reason + " | 수급↑(외국인+기관 순매수)"
                from scanner.scanner_logger import ScannerLogger as _SL
                _SL.passed(code, snap.name, "INVESTOR",
                           f"score=+1 외국인={snap.foreign_net_buy:+d} "
                           f"기관={snap.inst_net_buy:+d}")
            elif _iscore == -1:
                from scanner.scanner_logger import ScannerLogger as _SL
                _SL.rejected(code, snap.name, "INVESTOR",
                             f"score=-1 외국인={snap.foreign_net_buy:+d} "
                             f"기관={snap.inst_net_buy:+d} — 신호 차단")
                sig_type = None
                reason   = None


        return sig_type, reason


    def _maybe_emit_signal(self, snap, sig_type: str | None, reason: str | None,
                           code: str, _log, _tnow: float, _iscore: int) -> int:
        """
        에지 감지 + 신호 emit.


        Returns: 1 if 신호 emit, 0 otherwise
        """
        _cool = self._signal_cooldown_sec
        _eff_cool = _cool * (0.5 if _iscore == 1 else 1.0)


        now_active = sig_type is not None
        prev_active = self._signal_prev_active.get(code, False)
        rising_edge = now_active and not prev_active
        last_emit = self._signal_last_emit_mono.get(code)
        cooldown_ok = (last_emit is None) or (_tnow - last_emit >= _eff_cool)


        # 분당 신호 발행 수 갱신
        _cur_min = datetime.now().minute
        if _cur_min != self._entry_minute:
            self._entry_minute = _cur_min
            self._entry_count  = 0


        _per_min_ok = (self._entry_count < self._entry_per_min)


        signal_emitted = 0
        if now_active and rising_edge and cooldown_ok and _per_min_ok:
            _log.info(
                "[ScannerWorker] 신호 발생: %s(%s) [%s] %s",
                snap.name, code, sig_type, reason,
            )
            from scanner.smart_scanner import ScanSignal
            from scanner.indicator_service import IndicatorService as _is_ctx
            _near_thr = float(getattr(self._cfg, "daily_near_high_threshold_pct", 3.0))
            _dctx = _is_ctx.check_daily_alignment(snap.daily_closes, snap.current_price)
            _is_eod_sig = (sig_type == "EOD_ENTRY")
            _sig = ScanSignal(snap.code, snap.name, sig_type,
                              snap.current_price, reason,
                              near_daily_high=_dctx["near_high"],
                              daily_ma20=_dctx["daily_ma20"],
                              eod_trade=_is_eod_sig)
            _audit = getattr(self, "_audit", None)
            if _audit is not None:
                _audit.log_signal(_sig, snap, self._cfg)
            self.signal_detected.emit(_sig)
            signal_emitted = 1
            self._entry_count += 1
            self._signal_last_emit_mono[code] = _tnow
        elif now_active and rising_edge and cooldown_ok and not _per_min_ok:
            _log.info(
                "[분당제한] %s(%s) [%s] 스킵 — 이번 분 %d/%d건 발행됨",
                snap.name, code, sig_type, self._entry_count, self._entry_per_min,
            )
        elif now_active and rising_edge and not cooldown_ok:
            _log.debug(
                "[신호스킵] %s — 쿨다운 %.1fs 미경과 (eff=%.1fs)",
                code, _eff_cool, _eff_cool,
            )


        self._signal_prev_active[code] = now_active
        return signal_emitted


    def stop(self) -> None:
        self._running = False

    def cleanup_stale_data(self, active_codes: set[str]) -> int:
        """오래된 내부 상태(쿨다운, 대기 신호 등)를 정리하여 메모리 누수를 방지한다."""
        import time as _time
        now_mono = _time.monotonic()
        cleaned = 0

        # 1. BREAKOUT 대기 — 60분 이상 경과한 항목 제거
        stale_bp = [
            c for c, v in list(self._breakout_pending.items())
            if (now_mono - v.get("first_time", now_mono)) > 3600
        ]
        for c in stale_bp:
            self._breakout_pending.pop(c, None)
            cleaned += 1

        # 2. 신호 쿨다운 — 보유 중이 아닌 & 마지막 emit 2시간 초과 항목 제거
        stale_emit = [
            c for c, t in list(self._signal_last_emit_mono.items())
            if c not in active_codes and (now_mono - t) > 7200
        ]
        for c in stale_emit:
            self._signal_last_emit_mono.pop(c, None)
            self._signal_prev_active.pop(c, None)
            cleaned += 1

        return cleaned








class PortfolioWorker(QObject):
    """
    잔고 동기화 워커 — 메인 스레드 QTimer 방식 (Kiwoom OCX 스레드 규칙 준수)
    Part 3: balance + holdings를 350ms 간격 2-step으로 분리 (2026-04-27)
    → 연속 블로킹 8초(6+2) → 분리 블로킹 3+350ms+2초
    """


    refresh_done = pyqtSignal(dict)
    log_message  = pyqtSignal(str)


    def __init__(self, order_manager, parent=None) -> None:
        super().__init__(parent)
        self._om = order_manager
        self._balance_result: dict = {}  # Step 1 결과 임시 저장


    @pyqtSlot()
    def sync(self) -> None:
        """Step 1: balance TR만 실행 → 350ms 후 Step 2 (holdings) 실행."""
        _kw = getattr(self._om, "_kiwoom", None)
        _mw = self.parent()
        
        # _tr_busy 또는 _scan_in_progress 중이면 3초 뒤 재시도
        if (_kw and getattr(_kw, "_tr_busy", False)) or (_mw and getattr(_mw, "_scan_in_progress", False)):
            QTimer.singleShot(3000, self.sync)
            return
        try:
            self._om._roll_daily_state_if_needed()
            balance = self._om._kiwoom.get_balance()
            if not balance:
                return  # TR 차단 또는 서버 응답 없음
            self._balance_result = balance
            # 350ms 뒤 Step 2 실행 — event loop이 다른 이벤트 처리 가능
            QTimer.singleShot(350, self._sync_step2)
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step1] {e}")


    @pyqtSlot()
    def _sync_step2(self) -> None:
        """Step 2: holdings TR → 포지션 갱신 → UI 시그널."""
        _kw = getattr(self._om, "_kiwoom", None)
        if _kw and getattr(_kw, "_tr_busy", False):
            # 다른 TR이 끼어든 경우 — 1초 뒤 다시 시도
            QTimer.singleShot(1000, self._sync_step2)
            return
        try:
            cash = self._om._sync_with_balance(self._balance_result)
            self.refresh_done.emit({
                "cash": cash,
                "positions": dict(self._om.positions),
            })
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류 step2] {e}")


    def stop(self) -> None:
        pass   # QTimer 정지는 MainWindow에서 처리




