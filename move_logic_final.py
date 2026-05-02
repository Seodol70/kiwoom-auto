import ast

with open('app/trading_controller.py', encoding='utf-8') as f:
    tc_code = f.read()

new_methods = """
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
"""

tc_code += new_methods

with open('app/trading_controller.py', 'w', encoding='utf-8') as f:
    f.write(tc_code)

# Replace the calls in main_window.py to point to trading_controller
with open('ui/main_window.py', encoding='utf-8') as f:
    mw_code = f.read()

import re
# Bridge functions in MainWindow to call TradingController
mw_code = re.sub(
    r"def _check_market_crash\(self\) -> None:\s+[\s\S]*?(?=def )", 
    "def _check_market_crash(self) -> None:\n    if hasattr(self, 'trading_controller'):\n        self.trading_controller.check_market_crash()\n\n", 
    mw_code
)

mw_code = re.sub(
    r"def _check_overnight_gap\(self\) -> None:\s+[\s\S]*?(?=def )", 
    "def _check_overnight_gap(self) -> None:\n    if hasattr(self, 'trading_controller'):\n        self.trading_controller.check_overnight_gap()\n\n", 
    mw_code
)

mw_code = re.sub(
    r"def _liquidate_phase1_positions\(self, forced: bool=False\) -> None:\s+[\s\S]*?(?=def )", 
    "def _liquidate_phase1_positions(self, forced: bool=False) -> None:\n    if hasattr(self, 'trading_controller'):\n        self.trading_controller.liquidate_phase1_positions(forced)\n\n", 
    mw_code
)

mw_code = re.sub(
    r"def _liquidate_all_positions\(self\) -> None:\s+[\s\S]*?(?=def )", 
    "def _liquidate_all_positions(self) -> None:\n    if hasattr(self, 'trading_controller'):\n        self.trading_controller.liquidate_all_positions()\n\n", 
    mw_code
)

with open('ui/main_window.py', 'w', encoding='utf-8') as f:
    f.write(mw_code)

print("Logic moved to TradingController and MainWindow updated.")
