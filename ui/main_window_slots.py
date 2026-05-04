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
        self.log_panel.append(f"로그인 성공 — {mode} / 계좌: {account}")

        # 재연결(reconnect_silent) 시 중복 시작 방지
        if getattr(self, "_already_started", False):
            if hasattr(self._kiwoom, "_account"): self._kiwoom._account = account
            if hasattr(self.order_mgr, "_account"): self.order_mgr._account = account
            self._port_worker.sync()
            self.log_panel.append(f"[재연결] 계좌 재설정 완료 — {mode} / {account}")
            return

        self._already_started = True
        self._today_watch.clear()
        self._news_analyzer.reset_daily()
        
        _wu = float(_RISK2.get("sl_tp_warmup_sec", 45.0))
        self._sl_tp_warmup_end = _time.monotonic() + max(0.0, _wu)
        if _wu > 0:
            self.log_panel.append(f"[리스크] 로그인 후 {_wu:.0f}초간 자동 손절·익절 보류 (잔고·시세 안정화)")
        
        if self._tg:
            self._tg.send(f"🚀 프로그램 시작됨\n계좌: {account}\n모드: {mode}")
        self.start_after_login()

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
            
        self.log_panel.append(line)
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
        self.log_panel.append(f"📊 [피드백] {result.total_trades}건 분석 완료 | 손익 {pnl_str}")
        
        if result.profitable:
            self.log_panel.append("  └─ 수익 당일 — 파라미터 유지")
        elif result.adjustments:
            for adj in result.adjustments:
                arrow = "▲" if adj.new_val > adj.old_val else "▼"
                self.log_panel.append(f"  └─ {adj.param}: {adj.old_val} {arrow} {adj.new_val} ({adj.reason})")
        
        for reason in result.skipped_reasons:
            self.log_panel.append(f"  └─ [보류] {reason}")
        if result.report_path:
            self.log_panel.append(f"  └─ 리포트: {result.report_path}")

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
        self.log_panel.append(f"{'🟢' if enabled else '🔴'} 자동매매 {state}")
        logger.info("[자동매매] 상태 변경: %s", state)

    @pyqtSlot(bool)
    def _on_overnight_mode_toggle(self, enabled: bool) -> None:
        """야간보유 모드 토글"""
        self.state.overnight_mode = enabled
        self._scan_cfg.overnight_mode_enabled = enabled
        
        state = "ON" if enabled else "OFF"
        icon  = "🌙" if enabled else "☀️"
        self.log_panel.append(f"{icon} 야간보유 모드 {state}")
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
            self.log_panel.append(f"🔧 [가뭄완화] 파라미터 자동 완화: {m}")

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
        positions = data.get("positions", {})
        self.header.update_cash(cash)
        self.portfolio_panel.update_data(cash, positions)

    @pyqtSlot(object)
    def _on_scan_signal(self, sig) -> None:
        """스캐너 신호 수신 (Slot)"""
        self.scanner_panel.add_signal(sig)
        self.log_panel.append(f"🚨 [{sig.signal_type}] {sig.name}({sig.code}) 포착: {sig.reason}")
        self._today_watch[sig.code] = sig
