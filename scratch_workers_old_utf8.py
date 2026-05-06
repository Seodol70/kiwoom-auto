from __future__ import annotations
import time
from datetime import datetime
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer


class ScannerWorker(QObject):
    """
    蹂꾨룄 QThread ?먯꽌 ?ㅽ뻾?섎뒗 ?ㅼ틦???좏샇 ?먮떒 猷⑦봽.


    SnapshotStore (DataFrame 罹먯떆) 留??쎈뒗????kiwoom TR ?몄텧 ?놁쓬.
    signal_detected ??(1) ?먯?: 吏곸쟾 ?ㅼ틪???녿뜕 ?좏샇媛 ?대쾲?먮쭔 耳쒖쭏 ??
    (2) 荑⑤떎?? ?숈씪 醫낅ぉ 留덉?留?emit ?댄썑 signal_cooldown_sec 珥덇? 吏???ㅼ뿉留??ы뿀??
    媛먯떆?쒖쓽 signal ?댁? ?ъ쟾???쒖?湲?議곌굔 留뚯” ?щ??앸? ?쒖떆?쒕떎.
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
        # 留ㅼ닔 ?좏샇: ?먯?(議곌굔 爰쇱쭚?믪폒吏? + 吏㏃? 荑⑤떎???숈씪 醫낅ぉ ??emit 媛꾧꺽)
        self._signal_prev_active: dict[str, bool] = {}
        self._signal_last_emit_mono: dict[str, float] = {}
        self._signal_cooldown_sec: float = float(
            getattr(cfg, "signal_cooldown_sec", 45.0)
        )
        # BREAKOUT watch-and-confirm: 利됱떆 留ㅼ닔 ???N遺?愿李???吏꾩엯
        # code ??{"first_time": float(monotonic), "first_price": int}
        self._breakout_pending: dict[str, dict] = {}
        # Phase 1 ?꾩묠 珥덈떒? ??PRE_SURGE ?꾨낫 肄붾뱶 吏묓빀 (OPENING ?щ’ OPENING_SCALP ?쇱슦?낆슜)
        self._pre_surge_candidates: set[str] = set()
        # 遺꾨떦 ?좏샇 諛쒗뻾 ?쒗븳 ??媛숈? 遺꾩뿉 ?덈Т 留롮? 醫낅ぉ ?숈떆 吏꾩엯 諛⑹?
        self._entry_minute: int   = -1   # 留덉?留??좏샇 諛쒗뻾 遺?minute)
        self._entry_count:  int   = 0    # ?대떦 遺꾩뿉 諛쒗뻾???좏샇 ??        self._entry_per_min: int  = int(getattr(cfg, "max_entries_per_minute", 1))
        # UI 媛깆떊 ?곕줈? ??QTableWidget ?щ젋?붾쭅??3珥?媛꾧꺽?쇰줈 ?쒗븳
        self._last_ui_rows: list = []
        self._last_ui_emit: float = 0.0
        self._UI_INTERVAL: float = 3.0


    def stop(self) -> None:
        """?ㅼ틦??猷⑦봽 以묐떒"""
        self._running = False
        logger.info("[ScannerWorker] 以묐떒 ?좏샇 ?섏떊")

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
        self.log_message.emit("[ScannerWorker] ?쒖옉 ??SnapshotStore ?곗씠???湲?以?..")
        _log.info("[ScannerWorker] run() 吏꾩엯")


        _empty_logged = False
        _heartbeat_last: float = 0.0
        _HEARTBEAT_INTERVAL: float = 60.0  # 1遺꾨쭏???앹〈 濡쒓렇
        _eod_warn_logged: bool = False      # EOD 李??쇨컙蹂댁쑀 OFF 寃쎄퀬 (1??
        while self._running:
            t0 = time.monotonic()


            # ?섑듃鍮꾪듃: 1遺꾨쭏??猷⑦봽 ?뺤긽 ?숈옉 ?뺤씤
            if t0 - _heartbeat_last >= _HEARTBEAT_INTERVAL:
                _heartbeat_last = t0
                _overnight = getattr(self._cfg, "overnight_mode_enabled", False)
                _log.info("[ScannerWorker] ??猷⑦봽 ?뺤긽 ?숈옉 以????쇨컙蹂댁쑀=%s | 媛먯떆=%d醫낅ぉ",
                          "ON" if _overnight else "OFF", len(self._store))


            top_df = self._store.top_by_trade_amount(self._cfg.display_top_n)

            if top_df.empty:
                if not _empty_logged:
                    self.log_message.emit(
                        "[ScannerWorker] SnapshotStore 鍮꾩뼱?덉쓬 ???곗씠???섏쭛 ?湲?以?
                    )
                    _log.debug("[ScannerWorker] SnapshotStore 鍮꾩뼱?덉쓬")
                    _empty_logged = True
                
                # 珥덇린 ?ㅽ뻾 ???곗씠?곌? ?놁쑝硫?1珥덈쭔 ?湲고븯怨??ㅼ떆 ?뺤씤 (湲곗〈?먮뒗 scan_interval 留뚰겮 ?湲고븿)
                time.sleep(1.0)
                continue


            _empty_logged = False
            rows = []
            signal_cnt = 0


            # ?먯젅/?듭젅? MainWindow._auto_sell_by_pnl ?먯꽌留?泥섎━?쒕떎.
            # (援? Worker媛 ??蹂댁쑀醫낅ぉ ?꾨웾??1珥덈쭏??寃?ы빐 HTS쨌?꾩씪 蹂댁쑀遺꾧퉴吏 ?쒖옉 吏곹썑 留ㅻ룄?섎뜕 臾몄젣 諛⑹?.


            # ?? 踰≫꽣???ъ쟾?꾪꽣 ??????????????????????????????????????????
            # DataFrame ?곗궛?쇰줈 ?쒓? ?뚰뙆 / ?묐큺 湲곗“ 誘몄땐議?醫낅ぉ??癒쇱? ?쒓굅.
            # 蹂댄넻 50醫낅ぉ ??5~15醫낅ぉ?쇰줈 以꾩뼱 ?댄썑 Python 猷⑦봽 鍮꾩슜 70~90% 媛먯냼.
            # ?깅씫瑜??곹븳(config RISK.max_change_pct) ?댁긽? ?꾨낫쨌媛먯떆?쒖뿉???쒖쇅.
            # [NEW] ?쒓컙? ?щ’ 湲곕컲 ?깅씫瑜??곹븳 ?숈쟻 ?좏깮 (2026-04-08)
            _now_t  = datetime.now().time()
            _slot   = _resolve_time_slot(_now_t, self._cfg)
            _max_ch = _get_slot_value(_slot, self._cfg, "max_change_pct",
                                      float(getattr(self._cfg, "max_change_pct", 15.0)))
            candidate_codes = set(self._store.prefilter_candidates(_max_ch))


            # Phase 1 ?꾨낫 珥덇린????OPENING ?щ’ 醫낅즺 ?????댁긽 ?좏슚?섏? ?딆쓬
            if _slot not in ("PRE", "OPENING") and self._pre_surge_candidates:
                _log.info("[Phase1] OPENING ?щ’ 醫낅즺 ??PRE_SURGE ?꾨낫 %d醫낅ぉ 珥덇린??,
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
                # pandas Series?먯꽌 媛??덉쟾?섍쾶 異붿텧
                cp = row.get("change_pct", 0)
                ch = float(cp) if cp else 0.0
                # [吏꾨떒] ?깅씫瑜좎씠 ?믪? 醫낅ぉ 濡쒓퉭
                if ch >= _max_ch:
                    _log.debug("[?좏샇?꾪꽣] %s ???깅씫瑜?%.2f%% >= ?곹븳 %.1f%% ?쒖쇅",
                               name, ch, _max_ch)
                    self._signal_prev_active[code] = False
                    continue
                sig_type = None
                reason = None
                _trend_text = "?곗씠?곕?議?


                # ??異붿꽭 怨꾩궛 ??紐⑤뱺 媛먯떆 醫낅ぉ?먯꽌 ?쒕룄 (candidate_codes ?щ? 臾닿?)
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

                        # [NEW] 媛뺤꽭 異붿꽭(Level 3) 吏꾩엯 ??濡쒓렇 異쒕젰 (?쇱そ 紐⑤땲?곕쭅 ?⑤꼸??
                        if snap.trend_level >= 3 and (snap.trend_prev_level is None or snap.trend_prev_level < 3):
                            self.log_message.emit(f"?뵦 [異붿꽭?ъ갑] {name}({code}) 媛뺤꽭 異붿꽭 吏꾩엯 (Level 3)")

                        _ema_now = _is.calc_ema(_cl, _ema_p)
                        _atr_now = _is.calc_atr(_hi, _lo, _cl, _atr_p)
                        _down_mult = float(getattr(self._cfg, "yosep_downtrend_block_atr", 0.8))
                        if _ema_now and _atr_now:
                            if snap.current_price < (_ema_now - _atr_now * _down_mult):
                                _trend_text = "?섎씫"
                            elif _tlv >= 3: _trend_text = "媛뺤꽭"
                            elif _tlv == 2: _trend_text = "?곸듅"
                            elif _tlv == 1: _trend_text = "?쎌꽭"
                            else:           _trend_text = "?〓낫"

                # ???좏샇 ?먮떒 ??candidate_codes?먮쭔 ?섑뻾
                # ???좏샇 ?먮떒 ??candidate_codes?먮쭔 ?섑뻾 (UI ?쒖떆??
                if code in candidate_codes:
                    sig_type, reason = self._evaluate_signal(
                        code, snap, row, candidate_codes, _slot, _now_t, _log, self._cfg, _is
                    )
                    # [2026-05-05 Refactor] ?섏궗寃곗젙 ?쇱썝??                    # 二쇰Ц ?좏샇 諛쒗뻾(emit)? SmartScanner?먯꽌 ?꾨떞?섎?濡??ш린?쒕뒗 ?ㅽ궢?⑸땲??
                    # signal_cnt += self._maybe_emit_signal(...)
                    if sig_type:
                        signal_cnt += 1 
                else:
                    self._signal_prev_active[code] = False

                # 媛먯떆??row 援ъ꽦
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
            # 媛먯떆?쒖뿉???щ씪吏?醫낅ぉ??BREAKOUT ?湲곕룄 ?뺣━
            for _c in list(self._breakout_pending.keys()):
                if _c not in seen_codes:
                    del self._breakout_pending[_c]


            # UI 媛깆떊 ?곕줈?:
            # - ?좏샇媛 ?덈줈 諛쒖깮?덇굅??3珥덇? 吏?ъ쓣 ?뚮쭔 emit
            # - ?곗씠???댁슜??媛숈쑝硫?QTableWidget 遺덊븘?뷀븳 ?щ젋?붾쭅 諛⑹?
            now_ui = time.monotonic()
            has_new_signal = signal_cnt > 0
            time_ok = (now_ui - self._last_ui_emit) >= self._UI_INTERVAL
            if rows and (has_new_signal or time_ok):
                self.watch_list_updated.emit(rows)
                self._last_ui_emit = now_ui
                _log.debug("[ScannerWorker] watch_list_updated %d醫낅ぉ (?좏샇 %d媛?", len(rows), signal_cnt)


            elapsed = time.monotonic() - t0
            # scan_interval? opt10030 二쇨린 ?ㅼ틪 媛꾧꺽 (60s) ???좏샇 媛먯? 猷⑦봽? 臾닿?
            # ScannerWorker??1珥덈쭏???ㅽ뻾?섏뿬 ?좏샇/異붿꽭 ?먮떒??鍮좊Ⅴ寃??좎?
            time.sleep(max(0.0, 1.0 - elapsed))


    def _evaluate_signal(self, code: str, snap, row, candidate_codes: set,
                         slot: str, now_t, _log, _cfg, _is) -> tuple[str | None, str | None]:
        """
        ?좏샇 ?먯젙 濡쒖쭅 遺꾨━ ??candidate_codes???랁븳 醫낅ぉ留??ㅽ뻾.


        Returns: (sig_type, reason) ???좏샇 ?놁쑝硫?(None, None)
        """
        sig_type = None
        reason = None


        if snap is None:
            _log.debug("[ScannerWorker] %s ?ㅻ깄???놁쓬", code)
            self._signal_prev_active[code] = False
            return sig_type, reason


        # ?? ?щ’蹂??좏샇 ?쇱슦??????????????????????????????????
        from scanner.signal_evaluator import (
            check_eod_entry, check_pre_surge, check_opening_scalp, check_jdm_entry,
            check_breakout, check_breakout_gate
        )
        from scanner.models import ScanSignal
        from scanner.indicator_service import IndicatorService


        # EOD 醫낃?留ㅻℓ 李?14:40~14:55) ??overnight_mode_enabled ???곗꽑 泥댄겕
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
                                     f"?쇰큺 20MA ?섎갑 ??{snap.current_price:,} < {_dg['daily_ma20']:,.0f}")
                        self._signal_prev_active[code] = False
                        _ma_ok = False
                if _ma_ok and getattr(self._cfg, "daily_ma60_filter_enabled", True):
                    if not _dg["above_ma60"] and _dg["daily_ma60"] > 0:
                        _SL.rejected(code, snap.name, "DAILY_MA60",
                                     f"?쇰큺 60MA ?섎갑 ??{snap.current_price:,} < {_dg['daily_ma60']:,.0f}")
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
                                 f"?쇰큺 20MA ?섎갑 ???꾩옱媛 {snap.current_price:,} "
                                 f"< 20MA {_dg['daily_ma20']:,.0f}")
                    self._signal_prev_active[code] = False
                    _ma_ok = False
            if _ma_ok and getattr(self._cfg, "daily_ma60_filter_enabled", True):
                if not _dg["above_ma60"] and _dg["daily_ma60"] > 0:
                    _SL.rejected(code, snap.name, "DAILY_MA60",
                                 f"?쇰큺 60MA ?섎갑 ???꾩옱媛 {snap.current_price:,} "
                                 f"< 60MA {_dg['daily_ma60']:,.0f} (以묎린 ?섎씫 異붿꽭)")
                    self._signal_prev_active[code] = False
                    _ma_ok = False


            if _ma_ok:
                # ?? BREAKOUT: 利됱떆留ㅼ닔 ???N遺?watch-and-confirm ??
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
                            "[BREAKOUT李⑤떒] %s(%s) 異붿꽭Lv%d < 理쒖냼Lv%d [%s] ???湲??깅줉 ?ㅽ궢",
                            snap.name, code, _tlevel_now, _min_trend_req, slot,
                        )


                    if not _breakout_trend_blocked:
                        pending = self._breakout_pending.get(code)
                        if pending is None:
                            _tlevel = int(getattr(snap, "trend_level", 0))


                            # Fast-Track 濡쒖쭅
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
                                _log.info("?? [Fast-Track] %s(%s) 媛뺣젰???섍툒 蹂대꼫????利됱떆 吏꾩엯 (0珥?", snap.name, code)
                            elif _is_opening_slot:
                                _eff_secs = 20.0
                                _log.info("?깍툘 [Fast-Track] %s(%s) OPENING ?щ’ ???湲??쒓컙 20珥??⑥텞", snap.name, code)
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
                                    "[BREAKOUT寃뚯씠?? %s(%s) ?앹꽦 ?쒖젏 gate ?ㅽ뙣 ???湲??깅줉 ?ㅽ궢",
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
                                    "[BREAKOUT?湲? %s(%s) %.0f????%.1f遺?愿李??쒖옉 (異붿꽭Lv%d)",
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
                                    "[BREAKOUT痍⑥냼] %s(%s) ?섎씫 %.2f%% ??%.1f%% ???湲??댁젣",
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
                                        f"利됱떆?뺤씤(異붿꽭Lv{_tlevel})"
                                        if _eff_secs == 0
                                        else f"{elapsed/60:.1f}遺??좎? ?뺤씤(異붿꽭Lv{_tlevel})"
                                    )
                                    reason = (
                                        f"{breakout_reason} | {_confirm_label}"
                                        f" (珥덇린媛 {fp:,}?믫쁽??{snap.current_price:,})"
                                        f" | {_gate}"
                                    )
                                    del self._breakout_pending[code]
                            else:
                                _log.debug(
                                    "[BREAKOUT愿李곗쨷] %s(%s) %.1f/%.1f遺?寃쎄낵, ?깅씫 %.2f%% (異붿꽭Lv%d)",
                                    snap.name, code, elapsed / 60, _eff_secs / 60,
                                    drawdown, _tlevel,
                                )
                    else:
                        if code in self._breakout_pending:
                            _log.info(
                                "[BREAKOUT?댁젣] %s(%s) ?뚰뙆 議곌굔 ?뚮㈇ ???湲?痍⑥냼",
                                snap.name, code,
                            )
                            del self._breakout_pending[code]


                # ?? JDM_ENTRY (LITE ?ы븿) ??
                if not reason:
                    reason = check_jdm_entry(snap, self._cfg)
                    if reason:
                        sig_type = "JDM_ENTRY"


        # ?? ?섍툒 ?먯닔 諛섏쁺 ??????????????????????????????????
        _iscore = snap.investor_score
        if sig_type and self._cfg.investor_filter_enabled:
            if _iscore == 1:
                reason = reason + " | ?섍툒???멸뎅??湲곌? ?쒕ℓ??"
                from scanner.scanner_logger import ScannerLogger as _SL
                _SL.passed(code, snap.name, "INVESTOR",
                           f"score=+1 ?멸뎅??{snap.foreign_net_buy:+d} "
                           f"湲곌?={snap.inst_net_buy:+d}")
            elif _iscore == -1:
                from scanner.scanner_logger import ScannerLogger as _SL
                _SL.rejected(code, snap.name, "INVESTOR",
                             f"score=-1 ?멸뎅??{snap.foreign_net_buy:+d} "
                             f"湲곌?={snap.inst_net_buy:+d} ???좏샇 李⑤떒")
                sig_type = None
                reason   = None


        return sig_type, reason


    def _maybe_emit_signal(self, snap, sig_type: str | None, reason: str | None,
                           code: str, _log, _tnow: float, _iscore: int) -> int:
        """
        ?먯? 媛먯? + ?좏샇 emit.


        Returns: 1 if ?좏샇 emit, 0 otherwise
        """
        _cool = self._signal_cooldown_sec
        _eff_cool = _cool * (0.5 if _iscore == 1 else 1.0)


        now_active = sig_type is not None
        prev_active = self._signal_prev_active.get(code, False)
        rising_edge = now_active and not prev_active
        last_emit = self._signal_last_emit_mono.get(code)
        cooldown_ok = (last_emit is None) or (_tnow - last_emit >= _eff_cool)


        # 遺꾨떦 ?좏샇 諛쒗뻾 ??媛깆떊
        _cur_min = datetime.now().minute
        if _cur_min != self._entry_minute:
            self._entry_minute = _cur_min
            self._entry_count  = 0


        _per_min_ok = (self._entry_count < self._entry_per_min)


        signal_emitted = 0
        if now_active and rising_edge and cooldown_ok and _per_min_ok:
            _log.info(
                "[ScannerWorker] ?좏샇 諛쒖깮: %s(%s) [%s] %s",
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
                "[遺꾨떦?쒗븳] %s(%s) [%s] ?ㅽ궢 ???대쾲 遺?%d/%d嫄?諛쒗뻾??,
                snap.name, code, sig_type, self._entry_count, self._entry_per_min,
            )
        elif now_active and rising_edge and not cooldown_ok:
            _log.debug(
                "[?좏샇?ㅽ궢] %s ??荑⑤떎??%.1fs 誘멸꼍怨?(eff=%.1fs)",
                code, _eff_cool, _eff_cool,
            )


        self._signal_prev_active[code] = now_active
        return signal_emitted


    def stop(self) -> None:
        self._running = False

    def cleanup_stale_data(self, active_codes: set[str]) -> int:
        """?ㅻ옒???대? ?곹깭(荑⑤떎?? ?湲??좏샇 ??瑜??뺣━?섏뿬 硫붾え由??꾩닔瑜?諛⑹??쒕떎."""
        import time as _time
        now_mono = _time.monotonic()
        cleaned = 0

        # 1. BREAKOUT ?湲???60遺??댁긽 寃쎄낵????ぉ ?쒓굅
        stale_bp = [
            c for c, v in list(self._breakout_pending.items())
            if (now_mono - v.get("first_time", now_mono)) > 3600
        ]
        for c in stale_bp:
            self._breakout_pending.pop(c, None)
            cleaned += 1

        # 2. ?좏샇 荑⑤떎????蹂댁쑀 以묒씠 ?꾨땶 & 留덉?留?emit 2?쒓컙 珥덇낵 ??ぉ ?쒓굅
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
    ?붽퀬 ?숆린???뚯빱 ??硫붿씤 ?ㅻ젅??QTimer 諛⑹떇 (Kiwoom OCX ?ㅻ젅??洹쒖튃 以??
    Part 3: balance + holdings瑜?350ms 媛꾧꺽 2-step?쇰줈 遺꾨━ (2026-04-27)
    ???곗냽 釉붾줈??8珥?6+2) ??遺꾨━ 釉붾줈??3+350ms+2珥?    """


    refresh_done = pyqtSignal(dict)
    log_message  = pyqtSignal(str)


    def __init__(self, order_manager, trading_controller=None, parent=None) -> None:
        super().__init__(parent)
        self._om = order_manager
        self._tc = trading_controller
        self._balance_result: dict = {}  # Step 1 寃곌낵 ?꾩떆 ???        self._timers: list[QTimer] = []  # ?앸챸二쇨린 愿由ъ슜 ??대㉧ 紐⑸줉


    def _schedule_retry(self, delay_ms: int, fn) -> None:
        """??대㉧濡?肄쒕갚 ?ㅼ?以? stop() ??痍⑥냼 媛?ν븯寃?愿由?"""
        t = QTimer(self)
        t.setSingleShot(True)
        t.timeout.connect(fn)
        t.start(delay_ms)
        self._timers.append(t)

    @pyqtSlot()
    def sync(self) -> None:
        """Step 1: balance TR留??ㅽ뻾 ??350ms ??Step 2 (holdings) ?ㅽ뻾."""
        _kw = getattr(self._om, "_kiwoom", None)

        # _tr_busy ?먮뒗 _scan_in_progress 以묒씠硫?3珥????ъ떆??        scan_busy = self._tc and getattr(self._tc, '_scan_in_progress', False)

        if (_kw and getattr(_kw, "_tr_busy", False)) or scan_busy:
            self._schedule_retry(3000, self.sync)
            return
        try:
            self._om._roll_daily_state_if_needed()
            balance = self._om._kiwoom.get_balance()
            if not balance:
                return  # TR 李⑤떒 ?먮뒗 ?쒕쾭 ?묐떟 ?놁쓬
            self._balance_result = balance
            # 350ms ??Step 2 ?ㅽ뻾 ??event loop???ㅻⅨ ?대깽??泥섎━ 媛??            self._schedule_retry(350, self._sync_step2)
        except Exception as e:
            self.log_message.emit(f"[?붽퀬媛깆떊 ?ㅻ쪟 step1] {e}")


    @pyqtSlot()
    def _sync_step2(self) -> None:
        """Step 2: holdings TR ???ъ???媛깆떊 ??UI ?쒓렇??"""
        _kw = getattr(self._om, "_kiwoom", None)
        if _kw and getattr(_kw, "_tr_busy", False):
            # ?ㅻⅨ TR???쇱뼱??寃쎌슦 ??1珥????ㅼ떆 ?쒕룄
            self._schedule_retry(1000, self._sync_step2)
            return
        try:
            cash = self._om._sync_with_balance(self._balance_result)
            
            # [CRITICAL] 留ㅻ룄 媛먯떆 ?붿쭊 媛?? ?붽퀬 ?숆린??吏곹썑 泥?궛 議곌굔(SL/TP/Trail) 泥댄겕
            if self._tc:
                self._tc.update_portfolio_prices()
                
            self.refresh_done.emit({
                "cash": cash,
                "positions": dict(self._om.positions),
            })
        except Exception as e:
            self.log_message.emit(f"[?붽퀬媛깆떊 ?ㅻ쪟 step2] {e}")


    def stop(self) -> None:
        """紐⑤뱺 ?덉빟????대㉧ 痍⑥냼 ??醫鍮?肄쒕갚 諛⑹?"""
        for t in self._timers:
            t.stop()
        self._timers.clear()




