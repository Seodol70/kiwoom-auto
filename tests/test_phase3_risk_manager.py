"""Phase 3-2: RiskManager 통합 테스트"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt5.QtWidgets import QApplication
from app.risk_manager import RiskManager


class MockOrderManager:
    """실제 RiskManager 생성을 허용하는 OrderManager Mock.
    order_filled 시그널은 MagicMock으로 대체하여 connect() 호출을 허용한다.
    """

    def __init__(self):
        self._daily_realized_pnl = 0
        self.positions  = {}               # RiskManager.check()에서 사용
        self.order_filled = MagicMock()    # RiskManager.__init__에서 connect

    @property
    def daily_realized_pnl(self):
        return self._daily_realized_pnl

    def set_pnl(self, pnl: int):
        self._daily_realized_pnl = pnl


class MockConfig:
    """테스트용 Config Mock"""

    daily_profit_lock_won = 100000  # 10만원 수익 달성 시 락
    daily_loss_cut_won = -100000    # -10만원 손실 도달 시 청산


def test_profit_locked_signal():
    """수익 목표 달성 시 신호 발행 테스트"""
    app = QApplication.instance() or QApplication([])

    order_mgr = MockOrderManager()
    scan_cfg = MockConfig()
    risk_mgr = RiskManager(order_mgr, scan_cfg, parent=None)

    signal_fired = {"profit_locked": False}

    def on_profit_locked():
        signal_fired["profit_locked"] = True

    risk_mgr.daily_profit_locked.connect(on_profit_locked)

    # 초기 상태
    assert not risk_mgr.is_new_entry_locked, "초기 상태 락이 활성화되어있음"
    assert not signal_fired["profit_locked"], "초기 신호 발행되지 않음"

    # PnL 상승 → 목표 달성
    order_mgr.set_pnl(100000)
    risk_mgr.check()

    assert risk_mgr.is_new_entry_locked, "수익 목표 달성 후 락이 활성화되지 않음"
    assert signal_fired["profit_locked"], "수익 목표 신호가 발행되지 않음"

    # 두 번째 호출 → 신호 중복 발행 방지
    signal_fired["profit_locked"] = False
    risk_mgr.check()
    assert not signal_fired["profit_locked"], "신호 중복 발행됨"

    print("[OK] 수익 목표 달성 신호 테스트 통과")


def test_loss_cut_signal():
    """손절 한도 도달 시 신호 발행 테스트"""
    app = QApplication.instance() or QApplication([])

    order_mgr = MockOrderManager()
    scan_cfg = MockConfig()
    risk_mgr = RiskManager(order_mgr, scan_cfg, parent=None)

    signal_fired = {"loss_cut": False}

    def on_loss_cut():
        signal_fired["loss_cut"] = True

    risk_mgr.daily_loss_cut.connect(on_loss_cut)

    # 초기 상태
    assert not risk_mgr.is_daily_loss_cut_done, "초기 상태 손절이 활성화되어있음"
    assert not signal_fired["loss_cut"], "초기 신호 발행되지 않음"

    # PnL 하강 → 손절 한도 도달
    order_mgr.set_pnl(-100000)
    risk_mgr.check()

    assert risk_mgr.is_daily_loss_cut_done, "손절 한도 도달 후 플래그가 활성화되지 않음"
    assert signal_fired["loss_cut"], "손절 신호가 발행되지 않음"

    # 두 번째 호출 → 신호 중복 발행 방지
    signal_fired["loss_cut"] = False
    risk_mgr.check()
    assert not signal_fired["loss_cut"], "신호 중복 발행됨"

    print("[OK] 손절 한도 신호 테스트 통과")


def test_manual_unlock():
    """수동 해제 기능 테스트"""
    app = QApplication.instance() or QApplication([])

    order_mgr = MockOrderManager()
    scan_cfg = MockConfig()
    risk_mgr = RiskManager(order_mgr, scan_cfg, parent=None)

    # 수익 락 상태
    order_mgr.set_pnl(100000)
    risk_mgr.check()
    assert risk_mgr.is_new_entry_locked, "수익 락 활성화 실패"

    # 수동 해제
    risk_mgr.unlock_entry_manual()
    assert not risk_mgr.is_new_entry_locked, "수동 해제 실패"
    assert risk_mgr._manual_unlock_active, "수동 해제 플래그 미설정"

    print("[OK] 수동 해제 기능 테스트 통과")


def test_reset():
    """자정 리셋 기능 테스트"""
    app = QApplication.instance() or QApplication([])

    order_mgr = MockOrderManager()
    scan_cfg = MockConfig()
    risk_mgr = RiskManager(order_mgr, scan_cfg, parent=None)

    # 여러 상태 설정
    order_mgr.set_pnl(100000)
    risk_mgr.check()
    risk_mgr.unlock_entry_manual()

    # 리셋 전 상태 확인
    assert risk_mgr._manual_unlock_active or risk_mgr.is_new_entry_locked, "설정 실패"

    # 리셋
    risk_mgr.reset()

    assert not risk_mgr.is_new_entry_locked, "락 리셋 실패"
    assert not risk_mgr.is_daily_loss_cut_done, "손절 리셋 실패"
    assert not risk_mgr._manual_unlock_active, "수동 해제 플래그 리셋 실패"

    print("[OK] 리셋 기능 테스트 통과")


if __name__ == "__main__":
    print("\n=== Phase 3-2: RiskManager Test Start ===\n")

    try:
        test_profit_locked_signal()
        test_loss_cut_signal()
        test_manual_unlock()
        test_reset()

        print("\n[OK] All RiskManager tests passed!\n")
    except AssertionError as e:
        print(f"\n[FAIL] Assertion failed: {e}\n")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n[FAIL] Test error: {e}\n")
        import traceback

        traceback.print_exc()
        sys.exit(1)
