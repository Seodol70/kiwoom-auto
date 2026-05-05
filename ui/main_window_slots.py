# -*- coding: utf-8 -*-
"""
MainWindowSlots Mixin - 이벤트 처리 및 시그널 대응 전담
"""

from __future__ import annotations
import logging
import time as _time
from datetime import datetime, date as _date
from PyQt5.QtCore import pyqtSlot, QMetaObject, Qt as _Qt
from PyQt5.QtWidgets import QMessageBox, QDialog

from app.config_manager import config_manager as cfg
from ui.components.header_bar import ManualBuyDialog

logger = logging.getLogger(__name__)

class MainWindowSlots:
    """MainWindow의 이벤트 처리 및 슬롯 함수들을 담당하는 Mixin"""

    @pyqtSlot()
    def _on_connection_lost(self) -> None:
        """Watchdog: 연결 끊김 감지 — 헤더 상태 변경 + 로그"""
        self.header.set_connected("—", "연결끊김")
        self.append_log("🔴 [연결끊김] 키움 API 연결이 끊어졌습니다. 자동 재연결 시도 중...")

    @pyqtSlot()
    def _on_connection_recovered(self) -> None:
        """Watchdog: 재연결 성공 — 헤더 복원 + 로그"""
        acct = self.login_mgr.account
        mode = self.login_mgr.server_mode
        self.header.set_connected(acct, mode)
        self.append_log(f"🟢 [재연결] 키움 API 재연결 성공 — {acct} ({mode})")

    @pyqtSlot(str)
    def _on_reconnect_failed(self, reason: str) -> None:
        """Watchdog: 최대 재시도 초과 — 경고 + 사용자 안내"""
        self.append_log(f"⛔ [재연결 실패] {reason} — 수동 재시작이 필요합니다.")

    @pyqtSlot(str, str)
    def _on_login_success(self, account: str, mode: str) -> None:
        """로그인 성공 처리 및 시스템 가동"""
        _RISK2 = cfg.RISK
        self.header.set_connected(account, mode)
        self.append_log(f"로그인 성공 — {mode} / 계좌: {account}")

        # 재연결(reconnect_silent) 시 중복 시작 방지
        if getattr(self, "_already_started", False):
            if hasattr(self._kiwoom, "_account"): self._kiwoom._account = account
            if hasattr(self.order_mgr, "_account"): self.order_mgr._account = account
            self._port_worker.sync()
            self.append_log(f"[재연결] 계좌 재설정 완료 — {mode} / {account}")
            return

        self._already_started = True
        self._today_watch.clear()
        self._news_analyzer.reset_daily()
        
        _wu = float(_RISK2.get("sl_tp_warmup_sec", 45.0))
        self._sl_tp_warmup_end = _time.monotonic() + max(0.0, _wu)
        if _wu > 0:
            self.append_log(f"[리스크] 로그인 후 {_wu:.0f}초간 자동 손절·익절 보류 (잔고·시세 안정화)")
        
        if self._tg:
            self._tg.send(f"🚀 프로그램 시작됨\n계좌: {account}\n모드: {mode}")
        self.start_after_login()

    @pyqtSlot(dict)
    def _on_order_sent(self, d: dict) -> None:
        """주문 전송 완료 로그"""
        line = f"📤 [주문전송] {d.get('side', '주문')} — {d['name']}({d['code']}) {d['qty']}주"
        self.append_log(line)
        if self._tg: self._tg.send(line)

    @pyqtSlot(dict)
    def _on_order_filled(self, d: dict) -> None:
        """체결 완료 로그 및 후처리"""
        ab = d.get("avg_buy_price")
        if d.get("side") == "매도체결" and ab is not None:
            line = (f"✅ {d['side']} — {d['name']}({d['code']}) {d['filled_qty']}주 "
                    f"매수가 {ab:,}원 → 매도가 {d['filled_price']:,}원")
            self._today_watch.pop(d.get("code", ""), None)
        else:
            line = f"✅ {d['side']} — {d['name']}({d['code']}) {d['filled_qty']}주 @{d['filled_price']:,}원"
            
        self.append_log(line)
        if self._tg: self._tg.send(line)
        
        self._on_portfolio_refresh({
            "cash": self.order_mgr.cash,
            "positions": dict(self.order_mgr.positions),
        })
        self.trading_controller.on_fill_processed(d)

    @pyqtSlot(object)
    def _on_feedback_done(self, result) -> None:
        """피드백 완료 콜백 — UI 갱신 및 로그 출력"""
        pnl_str  = f"{result.total_realized:+,.0f}원"
        self.append_log(f"📊 [피드백] {result.total_trades}건 분석 완료 | 손익 {pnl_str}")
        
        if result.profitable:
            self.append_log("  └─ 수익 당일 — 파라미터 유지")
        elif result.adjustments:
            for adj in result.adjustments:
                arrow = "▲" if adj.new_val > adj.old_val else "▼"
                self.append_log(f"  └─ {adj.param}: {adj.old_val} {arrow} {adj.new_val} ({adj.reason})")
        
        for reason in result.skipped_reasons:
            self.append_log(f"  └─ [보류] {reason}")
        if result.report_path:
            self.append_log(f"  └─ 리포트: {result.report_path}")

        _tg = getattr(self, "_tg", None)
        if _tg:
            msg = result.telegram_msg if result.telegram_msg else f"[피드백] {result.date} 분석 완료 | 손익 {pnl_str}"
            try: _tg.send(msg)
            except: pass

    @pyqtSlot(bool)
    def _on_auto_trade_toggle(self, enabled: bool) -> None:
        """자동매매 토글 처리"""
        self.state.auto_trading = enabled
        if enabled: self._market_crash_off = False
        
        state = "시작" if enabled else "정지"
        self.append_log(f"{'🟢' if enabled else '🔴'} 자동매매 {state}")
        logger.info("[자동매매] 상태 변경: %s", state)

    @pyqtSlot(bool)
    def _on_overnight_mode_toggle(self, enabled: bool) -> None:
        """야간보유 모드 토글"""
        self.state.overnight_mode = enabled
        self._scan_cfg.overnight_mode_enabled = enabled
        
        state = "ON" if enabled else "OFF"
        icon  = "🌙" if enabled else "☀️"
        self.append_log(f"{icon} 야간보유 모드 {state}")
        logger.info("[overnight_mode] %s", state)

    @pyqtSlot()
    def _on_switch_real_requested(self) -> None:
        """실전/모의 서버 전환 요청"""
        msg = ("🚨 [서버 모드 전환] 안내\n\n전환 시 프로그램이 재시작됩니다. 계속하시겠습니까?")
        reply = QMessageBox.question(self, '서버 모드 전환', msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            import os
            current_is_real = (self.state.server_mode == "실전투자")
            target_mode = "0" if current_is_real else "1"
            with open("force_mode.tmp", "w") as f: f.write(target_mode)
            self.header._on_restart_clicked()

    @pyqtSlot()
    def _on_manual_unlock_requested(self) -> None:
        """사용자 수동 개입으로 일일 손익 락을 해제한다 (RiskManager 연동)."""
        self.risk_manager.unlock_entry_manual()
        self.header.set_risk_status("SAFE")
        self.append_log("🔓 [수동해제] 일일 손익 락 해제 완료 — 금일 자동 재락 일시 중단")

    @pyqtSlot()
    def _on_loss_cut(self) -> None:
        """RiskManager: 손절 한도 도달 알림"""
        QMessageBox.critical(self, "리스크 관리", "당일 손절 한도에 도달하여 모든 포지션을 청산하고 매수를 중단합니다.")
        self.append_log("🔴 [리스크] 당일 손절 한도 도달 — 시스템 가동 중지")

    @pyqtSlot()
    def _on_profit_locked(self) -> None:
        """RiskManager: 수익 목표 달성 알림"""
        QMessageBox.information(self, "수익 완료", "당일 수익 목표를 달성하여 신규 매수를 제한합니다. (보유 종목은 유지)")
        self.append_log("💰 [수익완료] 목표 수익 달성 — 신규 매수 제한")

    @pyqtSlot()
    def _run_feedback_loop(self) -> None:
        """피드백 엔진 실행 (QThread)"""
        from app.feedback_worker import FeedbackWorker
        self.append_log("📊 [피드백] 장 마감 분석 시작...")
        self._fb_thread = QThread(self)
        worker = FeedbackWorker()
        worker.moveToThread(self._fb_thread)
        self._fb_thread.started.connect(worker.run)
        worker.finished.connect(self._on_feedback_done)
        worker.finished.connect(self._fb_thread.quit)
        self._fb_thread.start()

    @pyqtSlot()
    def _on_day_reset(self) -> None:
        """장 시작 시 당일 상태 초기화"""
        self._already_started = False
        self._opened_today = False
        self._closed_today = False
        self._feedback_done_today = False
        self._today_watch.clear()
        self.risk_manager.reset()
        self.append_log("🌅 [일일 리셋] 당일 매매 상태를 초기화했습니다.")

    @pyqtSlot(dict)
    def _on_health_param_relax(self, params: dict) -> None:
        """가뭄 완화 파라미터 적용"""
        msg = "  ".join(f"{k}={v}" for k, v in params.items())
        if not hasattr(self, "_health_relax_msgs"): self._health_relax_msgs = []
        self._health_relax_msgs.append(msg)
        QMetaObject.invokeMethod(self, "_health_relax_ui", _Qt.QueuedConnection)

    @pyqtSlot()
    def _health_relax_ui(self) -> None:
        msgs = getattr(self, "_health_relax_msgs", [])
        while msgs:
            m = msgs.pop(0)
            self.append_log(f"🔧 [가뭄완화] 파라미터 자동 완화: {m}")

    @pyqtSlot(str, str, int)
    def _on_manual_buy(self, code: str, name: str, price: int) -> None:
        """수동 매수 다이얼로그 호출"""
        dlg = ManualBuyDialog(code, name, price, parent=self)
        if dlg.exec() == QDialog.Accepted:
            pass # ManualBuyDialog 내부에서 주문 처리 시그널 발생

    @pyqtSlot(dict)
    def _on_portfolio_refresh(self, data: dict) -> None:
        """포트폴리오 UI 일괄 갱신 (Slot)"""
        cash = data.get("cash", 0)
        self.header.update_cash(cash)
        self.portfolio_panel.refresh(data)

    @pyqtSlot(object)
    def _on_scan_signal(self, sig) -> None:
        """스캐너 신호 수신 (Slot)"""
        self.scanner_panel.add_signal(sig)
        self.append_log(f"🚨 [{sig.signal_type}] {sig.name}({sig.code}) 포착: {sig.reason}")
        self._today_watch[sig.code] = sig

    @pyqtSlot(str)
    def _on_scan_status_updated(self, status: str) -> None:
        """스캔 상태 텍스트 갱신"""
        self.scan_status.set_status(status)

    @pyqtSlot()
    def _on_auto_trade_started(self) -> None:
        """첫 신호 포착 등으로 자동매매가 실제 개시됨"""
        self.append_log("🚀 [엔진] 첫 신호 포착 — 실시간 자동매매 감시를 시작합니다.")

    @pyqtSlot()
    def _on_reload_config(self) -> None:
        """설정 파일 재로드"""
        cfg.load()
        self.append_log("⚙ [설정] adaptive_params.json 및 전역 설정을 재로드했습니다.")

    @pyqtSlot(str)
    def _on_manual_sell(self, code: str) -> None:
        """수동 매도 요청"""
        pos = self.order_mgr.positions.get(code)
        if pos:
            self.trading_controller.force_exit(code, "사용자 수동매도")
            self.append_log(f"📤 [수동매도] {pos.name}({code}) 시장가 청산 주문 전송")

    @pyqtSlot(str)
    def _on_code_selected(self, code: str) -> None:
        """종목 선택 시 차트 표시"""
        if not code: return
        
        # 1. 캔들 데이터 조회 (최근 100개, 1분봉)
        candles = self._kiwoom.get_min_candles(code, tick_unit=1, count=100)
        if not candles:
            # 공휴일/장전 등 데이터가 없으면 일봉이라도 시도
            candles = self._kiwoom.get_daily_candles(code, count=40)
            
        closes = [c['close'] for c in candles]
        volumes = [c['volume'] for c in candles]
        
        # 2. 종목명 및 포지션 정보
        name = self._kiwoom.get_stock_name(code)  # CP949 인코딩 보정 적용
        position = self.order_mgr.positions.get(code)
        
        # 3. 전략 파라미터 (트레일가, 손절%)
        sl_pct = float(getattr(self._scan_cfg, "jdm_stop_loss_pct", -1.5))
        trail_price = 0
        if position and hasattr(position, "trail_stop_price"):
            trail_price = position.trail_stop_price
            
        # 4. 차트 업데이트
        self.chart_panel.update_chart(
            closes=closes,
            volumes=volumes,
            code=code,
            name=name,
            position=position,
            trail_price=trail_price,
            sl_pct=sl_pct
        )
        logger.info("[차트] 종목 선택됨: %s(%s) - %d캔들 로드", name, code, len(closes))

    @pyqtSlot()
    def _on_market_opened(self) -> None:
        """장 시작 처리"""
        self._opened_today = True
        self.append_log("🔔 [장시작] 정규장이 시작되었습니다. 감시를 강화합니다.")

    @pyqtSlot()
    def _on_market_closing(self) -> None:
        """장 마감 임박 처리"""
        self._closed_today = True
        self.append_log("⌛ [장마감] 장 종료가 임박했습니다. 미체결 정리 및 당일청산을 준비합니다.")

    @pyqtSlot()
    def _on_feedback_triggered(self) -> None:
        """피드백 실행 (MarketScheduler에서 호출)"""
        self._run_feedback_loop()

    @pyqtSlot(float, float, float, float, bool)
    def _on_market_data_updated(self, kp_cur, kp_chg, kd_cur, kd_chg, is_crash) -> None:
        """시장 지수 UI 갱신"""
        self.header.set_index(kp_cur, kp_chg, kd_cur, kd_chg, is_crash)
        if is_crash and not self._market_crash_off:
            self.append_log("⚠ [지수급락] 시장 지수 급락 감지 — 신규 매수 일시 제한")

    @pyqtSlot()
    def _on_tg_status_requested(self) -> None:
        """텔레그램 상태 보고"""
        if not self._tg: return
        msg = f"📊 현재 상태\n당일손익: {self.order_mgr.daily_realized_pnl:,}원\n보유종목: {len(self.order_mgr.positions)}개\n자동매매: {'ON' if self.state.auto_trading else 'OFF'}"
        self._tg.send(msg)
