"""Phase 3 실제 동작 통합 테스트 — Mock MainWindow와의 상호작용"""

import sys
from pathlib import Path
from datetime import datetime, time
from dataclasses import dataclass
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer, QObject, pyqtSignal, pyqtSlot
from app.market_scheduler import MarketScheduler
from app.risk_manager import RiskManager
from app.trading_controller import TradingController


# ═══════════════════════════════════════════════════════════════════════
# Mock 객체들
# ═══════════════════════════════════════════════════════════════════════


class MockOrderManager:
    order_filled = MagicMock()   # pyqtSignal stub (RiskManager.__init__ 연결용)

    def __init__(self):
        self.positions = {}
        self.available_cash = 1000000
        self.max_positions = 5
        self._pending = {}
        self._daily_realized_pnl = 0
        self.signals_handled = []
        # [Step 2] SignalFilterChain을 위한 필터 의존성
        self._strategy = MagicMock()
        self._strategy.should_entry = MagicMock(return_value=(True, ""))
        self._ai_filter = MagicMock()
        self._ai_filter.should_enter = MagicMock(return_value=(True, 0.7))
        self._ai_filter.is_ready = False
        self._auto_trading = False

    @property
    def daily_realized_pnl(self):
        return self._daily_realized_pnl

    def is_pending(self, code):
        return code in self._pending

    def handle_signal(self, sig):
        self.signals_handled.append({
            "code": sig.code,
            "name": sig.name,
            "price": sig.price,
            "type": sig.signal_type,
        })

    def mark_stop_loss(self, code):
        pass

    def force_exit(self, code, name, qty, reason=""):
        pass

    def sell(self, code, name, qty, price=0):
        pass


class MockSnapshot:
    """StockSnapshot Mock"""
    def __init__(self):
        self.trend_level = 2
        self.foreign_net_buy = 100
        self.inst_net_buy = 100
        self.rs_score = 0.5
        self.closes_1min = [50000, 50100, 50200]


class MockSnapshotStore:
    """SnapshotStore Mock"""
    def get_snapshot(self, code):
        return MockSnapshot()

    def update_investor(self, code, foreign_net, inst_net):
        pass


class MockConfig:
    daily_profit_lock_won = 100000
    daily_loss_cut_won = -100000
    hard_stop_pct = -2.0
    stop_loss_pct = -1.2
    trail_activation_pct = 1.0
    trail_pct_tier1 = 1.5
    trail_pct_tier2 = 2.5
    trail_tier1_max = 1.5
    trail_tier2_max = 2.5
    trail_pct_tier3 = 3.5
    time_cut_minutes = 25
    strong_trend_hold_level = 3
    strong_trend_timecut_exempt = True
    trend_protect_enabled = True
    # [Step 2] SignalFilterChain을 위한 설정
    phase1_max_positions = 3
    ai_threshold = 0.5
    rs_threshold = 0.0
    exploration_mode = False


def _make_fresh_app_state():
    """Fresh AppState mock 생성"""
    app_state = MagicMock()
    app_state.profit_locked = False
    app_state.loss_cut_locked = False
    app_state.daily_realized_pnl = 0.0
    return app_state


@dataclass
class MockSignal:
    code: str
    name: str
    price: int
    qty: int = 1
    signal_type: str = "JDM_ENTRY"
    reason: str = "테스트"
    sector: str = ""
    entry_phase: int = 2


class MockMainWindow(QObject):
    """Phase 3와의 실제 상호작용을 시뮬레이션하는 Mock MainWindow"""

    def __init__(self):
        super().__init__()
        self.order_mgr = MockOrderManager()
        self._scan_cfg = MockConfig()
        self._auto_trading = False
        self.events_log = []

        # [Step 2] SignalFilterChain을 위한 의존성
        self._snap_store = MockSnapshotStore()
        self._kiwoom = MagicMock()
        self._news_analyzer = None

        # Phase 3: Application Layer
        self.market_scheduler = MarketScheduler(self)
        self.risk_manager = RiskManager(self.order_mgr, self._scan_cfg, self, app_state=_make_fresh_app_state())
        self.trading_controller = TradingController(
            kiwoom=self._kiwoom,
            order_mgr=self.order_mgr,
            scan_cfg=self._scan_cfg,
            risk_mgr=self.risk_manager,
            snap_store=self._snap_store,
            news_analyzer=self._news_analyzer,
            parent=self
        )

        # 신호 연결
        self._setup_signals()

        # 상태 플래그
        self._opened_today = False
        self._closed_today = False

    def _setup_signals(self):
        """Phase 3 신호 연결"""
        self.market_scheduler.market_opened.connect(self._on_market_opened)
        self.market_scheduler.market_closing.connect(self._on_market_closing)
        self.market_scheduler.day_reset.connect(self._on_day_reset)

        self.risk_manager.daily_profit_locked.connect(self._on_profit_locked)
        self.risk_manager.daily_loss_cut.connect(self._on_loss_cut)

    def _on_market_opened(self):
        self.events_log.append(("MARKET_OPENED", datetime.now()))
        self._opened_today = True
        self._auto_trading = True

    def _on_market_closing(self):
        self.events_log.append(("MARKET_CLOSING", datetime.now()))
        self._closed_today = True
        self._auto_trading = False

    def _on_day_reset(self):
        self.events_log.append(("DAY_RESET", datetime.now()))
        self._opened_today = False
        self._closed_today = False

    def _on_profit_locked(self):
        self.events_log.append(("PROFIT_LOCKED", datetime.now()))

    def _on_loss_cut(self):
        self.events_log.append(("LOSS_CUT", datetime.now()))

    def process_signal(self, sig: MockSignal) -> bool:
        """신호 처리 (실제 _on_scan_signal 시뮬레이션)"""
        # [Step 2] RiskManager의 신규 매수 락 체크
        if self.risk_manager.is_new_entry_locked:
            self.events_log.append(("SIGNAL_REJECTED_BY_RISK", sig.code))
            return False

        self.trading_controller.set_auto_trading(self._auto_trading)
        result = self.trading_controller.handle_signal(sig)
        self.events_log.append(("SIGNAL_PROCESSED", sig.code, result))
        return result

    def simulate_market_timer(self):
        """매분 타이머 시뮬레이션"""
        self.market_scheduler._on_timer_timeout()
        self.risk_manager.check()
        self.events_log.append(("TIMER_TICK", datetime.now()))


# ═══════════════════════════════════════════════════════════════════════
# 테스트
# ═══════════════════════════════════════════════════════════════════════


def test_full_trading_flow():
    """전체 거래 흐름 테스트"""
    print("\n[TEST 1] 전체 거래 흐름\n")

    app = QApplication.instance() or QApplication([])
    win = MockMainWindow()

    # 1. 장 시작 신호
    win.market_scheduler._opened_today = False
    win.market_scheduler._on_timer_timeout()

    # 시뮬레이션: 08:00 타이머 발화 (수동 호출)
    win.market_scheduler.market_opened.emit()
    assert win._opened_today, "시장 개시 신호 미수신"
    assert win._auto_trading, "자동매매 미시작"
    print("[OK] 08:00 시장 개시 신호 처리")

    # 2. 신호 수신 및 처리
    sig = MockSignal(code="005930", name="삼성전자", price=70000)
    result = win.process_signal(sig)
    assert result, "신호 필터링 실패 (자동매매 ON)"
    assert len(win.order_mgr.signals_handled) > 0, "신호 처리 미기록"
    print("[OK] 신호 수신 및 처리")

    # 3. 손익 한도 테스트 — 수익 락
    win.order_mgr._daily_realized_pnl = 100000  # 10만원 수익
    win.risk_manager.check()
    assert win.risk_manager.is_new_entry_locked, "수익 락 미작동"
    assert ("PROFIT_LOCKED", win.market_scheduler._on_timer_timeout.__self__.__dict__.get('_on_timer_timeout')) is None or len([e for e in win.events_log if e[0] == "PROFIT_LOCKED"]) > 0
    print("[OK] 수익 목표 달성 시 신규 매수 차단")

    # 4. 신호 거절 (락 상태)
    sig2 = MockSignal(code="000660", name="SK하이닉스", price=125000)
    result2 = win.process_signal(sig2)
    assert not result2, "신규 매수 락이 작동하지 않음"
    print("[OK] 신규 매수 락 작동 확인")

    # 5. 수동 해제
    win.risk_manager.unlock_entry_manual()
    assert not win.risk_manager.is_new_entry_locked, "수동 해제 실패"
    print("[OK] 수동 해제 작동")

    # 6. 리셋
    win.risk_manager.reset()
    assert not win.risk_manager.is_new_entry_locked, "리셋 실패"
    print("[OK] 자정 리셋 작동")

    # 7. 장 마감 신호
    win.market_scheduler.market_closing.emit()
    assert win._closed_today, "장 마감 신호 미수신"
    assert not win._auto_trading, "자동매매 미종료"
    print("[OK] 15:20 장 마감 신호 처리")

    print(f"\n이벤트 로그: {len(win.events_log)}개\n")
    for i, event in enumerate(win.events_log[-5:]):
        print(f"  [{i}] {event}")

    return True


def test_signal_filtering_chain():
    """신호 필터 체인 테스트"""
    print("\n[TEST 2] 신호 필터 체인\n")

    app = QApplication.instance() or QApplication([])
    win = MockMainWindow()
    win._auto_trading = True

    # 필터 1: 자동매매 OFF
    win._auto_trading = False
    sig = MockSignal(code="005930", name="삼성전자", price=70000)
    result = win.process_signal(sig)
    assert not result, "자동매매 OFF 필터 실패"
    print("[OK] 필터 1: 자동매매 OFF")

    # 필터 2: 포지션 5개 풀
    win._auto_trading = True
    for i in range(5):
        pos = type('Position', (), {
            'code': f'00{1000+i}',
            'qty': 10,
            'current_price': 100000,
            'avg_price': 100000,
        })()
        win.order_mgr.positions[f'00{1000+i}'] = pos

    sig = MockSignal(code="005930", name="삼성전자", price=70000)
    result = win.process_signal(sig)
    assert not result, "포지션 5개 풀 필터 실패"
    print("[OK] 필터 2: 포지션 5개 풀")

    # 필터 3: 손익 락
    win.order_mgr.positions.clear()
    win.order_mgr._daily_realized_pnl = 100000
    win.risk_manager.check()
    sig = MockSignal(code="005930", name="삼성전자", price=70000)
    result = win.process_signal(sig)
    assert not result, "손익 락 필터 실패"
    print("[OK] 필터 3: 신규 매수 락")

    # 필터 4: 중복 진입
    win.risk_manager.reset()
    win.order_mgr.positions["005930"] = type('Position', (), {'code': '005930'})()
    sig = MockSignal(code="005930", name="삼성전자", price=70000)
    result = win.process_signal(sig)
    assert not result, "중복 진입 필터 실패"
    print("[OK] 필터 4: 중복 진입 방지")

    # 필터 통과
    win.order_mgr.positions.clear()
    sig = MockSignal(code="000660", name="SK하이닉스", price=125000)
    result = win.process_signal(sig)
    assert result, "신호 필터 통과 실패"
    print("[OK] 필터 통과 - 신호 처리")

    return True


def test_timer_simulation():
    """타이머 시뮬레이션 테스트"""
    print("\n[TEST 3] 타이머 시뮬레이션\n")

    app = QApplication.instance() or QApplication([])
    win = MockMainWindow()

    # 타이머 시뮬레이션 5회
    for i in range(5):
        win.simulate_market_timer()

    assert len(win.events_log) >= 5, "타이머 이벤트 미기록"
    print(f"[OK] {len(win.events_log)}개 타이머 틱 기록됨")

    return True


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    print("\n" + "="*60)
    print("Phase 3 실제 동작 통합 테스트")
    print("="*60)

    try:
        test_full_trading_flow()
        test_signal_filtering_chain()
        test_timer_simulation()

        print("\n" + "="*60)
        print("ALL TESTS PASSED!")
        print("="*60 + "\n")

    except AssertionError as e:
        print(f"\n[FAIL] {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        import traceback

        traceback.print_exc()
        sys.exit(1)
