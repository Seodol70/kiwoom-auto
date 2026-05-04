"""
test_risk_manager_boundary.py — RiskManager 경계값 단위 테스트

대상:
- 수익 락: daily_profit_lock_won 정확히 도달 시 발동
- 손절 락: daily_loss_cut_won 정확히 도달 시 발동
- 한도 미달 시 미발동
- 자정 리셋 후 락 해제
- 수동 unlock / re-lock
- profit_lock + loss_cut 동시 체크
"""

import pytest
from unittest.mock import MagicMock
from datetime import datetime
from PyQt5.QtWidgets import QApplication

from app.risk_manager import RiskManager


# QApplication 인스턴스 (pyqtSignal emit에 필요)
@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────

def _make_rm(daily_pnl: float = 0.0, profit_lock: int = 100_000, loss_cut: int = 100_000):
    """
    RiskManager 생성 헬퍼.
    loss_cut은 양수로 전달 (내부에서 -loss_cut 비교).
    """
    order_mgr = MagicMock()
    order_mgr.daily_realized_pnl = daily_pnl

    scan_cfg = MagicMock()
    scan_cfg.daily_profit_lock_won = profit_lock
    scan_cfg.daily_loss_cut_won    = loss_cut

    # Mock session manager to return fresh state (not persisted)
    session_mgr = MagicMock()
    session_mgr.load.return_value = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "daily_realized_pnl": 0.0,
        "is_loss_cut_locked": False,
        "is_profit_locked": False,
        "timestamp": datetime.now().isoformat(),
    }

    rm = RiskManager(order_mgr=order_mgr, scan_cfg=scan_cfg, session_mgr=session_mgr)
    return rm, order_mgr, scan_cfg


# ── 수익 락 (Profit Lock) ─────────────────────────────────────────────────

class TestProfitLock:

    def test_below_threshold_no_lock(self, qapp):
        """99,999원 < 100,000원 목표 → 락 없음"""
        rm, order_mgr, _ = _make_rm(daily_pnl=99_999, profit_lock=100_000)
        rm.check()
        assert rm.is_new_entry_locked is False

    def test_exact_threshold_locks(self, qapp):
        """100,000원 == 목표 → 락 발동"""
        rm, order_mgr, _ = _make_rm(daily_pnl=100_000, profit_lock=100_000)
        rm.check()
        assert rm.is_new_entry_locked is True

    def test_above_threshold_locks(self, qapp):
        """100,001원 > 목표 → 락 발동"""
        rm, order_mgr, _ = _make_rm(daily_pnl=100_001, profit_lock=100_000)
        rm.check()
        assert rm.is_new_entry_locked is True

    def test_lock_emits_signal(self, qapp):
        """락 발동 시 daily_profit_locked 시그널 emit"""
        rm, order_mgr, _ = _make_rm(daily_pnl=100_000, profit_lock=100_000)
        fired = []
        rm.daily_profit_locked.connect(lambda: fired.append(1))
        rm.check()
        assert len(fired) == 1

    def test_lock_fires_only_once(self, qapp):
        """같은 상태에서 check() 2회 → 시그널 1회만"""
        rm, order_mgr, _ = _make_rm(daily_pnl=150_000, profit_lock=100_000)
        fired = []
        rm.daily_profit_locked.connect(lambda: fired.append(1))
        rm.check()
        rm.check()
        assert len(fired) == 1


# ── 손절 락 (Loss Cut) ───────────────────────────────────────────────────

class TestLossCut:

    def test_above_threshold_no_cut(self, qapp):
        """-99,999원 > -100,000원 기준 → 손절 없음"""
        rm, _, _ = _make_rm(daily_pnl=-99_999, loss_cut=100_000)
        rm.check()
        assert rm.is_daily_loss_cut_done is False

    def test_exact_threshold_cuts(self, qapp):
        """-100,000원 == -기준 → 손절 발동"""
        rm, _, _ = _make_rm(daily_pnl=-100_000, loss_cut=100_000)
        rm.check()
        assert rm.is_daily_loss_cut_done is True

    def test_below_threshold_cuts(self, qapp):
        """-100,001원 < -기준 → 손절 발동"""
        rm, _, _ = _make_rm(daily_pnl=-100_001, loss_cut=100_000)
        rm.check()
        assert rm.is_daily_loss_cut_done is True

    def test_loss_cut_emits_signal(self, qapp):
        """손절 발동 시 daily_loss_cut 시그널 emit"""
        rm, _, _ = _make_rm(daily_pnl=-100_000, loss_cut=100_000)
        fired = []
        rm.daily_loss_cut.connect(lambda: fired.append(1))
        rm.check()
        assert len(fired) == 1

    def test_loss_cut_fires_only_once(self, qapp):
        """같은 상태에서 check() 2회 → 시그널 1회만"""
        rm, _, _ = _make_rm(daily_pnl=-200_000, loss_cut=100_000)
        fired = []
        rm.daily_loss_cut.connect(lambda: fired.append(1))
        rm.check()
        rm.check()
        assert len(fired) == 1


# ── 리셋 ─────────────────────────────────────────────────────────────────

class TestReset:

    def test_reset_clears_profit_lock(self, qapp):
        """수익 락 후 reset() → 해제"""
        rm, _, _ = _make_rm(daily_pnl=100_000, profit_lock=100_000)
        rm.check()
        assert rm.is_new_entry_locked is True
        rm.reset()
        assert rm.is_new_entry_locked is False

    def test_reset_clears_loss_cut(self, qapp):
        """손절 락 후 reset() → 해제"""
        rm, _, _ = _make_rm(daily_pnl=-100_000, loss_cut=100_000)
        rm.check()
        assert rm.is_daily_loss_cut_done is True
        rm.reset()
        assert rm.is_daily_loss_cut_done is False

    def test_can_lock_again_after_reset(self, qapp):
        """reset 후 다시 한도 도달 → 재발동"""
        rm, order_mgr, _ = _make_rm(daily_pnl=100_000, profit_lock=100_000)
        fired = []
        rm.daily_profit_locked.connect(lambda: fired.append(1))
        rm.check()
        rm.reset()
        rm.check()
        assert len(fired) == 2  # 리셋 후 재발동


# ── 수동 unlock ───────────────────────────────────────────────────────────

class TestManualUnlock:

    def test_manual_unlock_releases(self, qapp):
        """수익 락 후 unlock_entry_manual() → 해제"""
        rm, _, _ = _make_rm(daily_pnl=100_000, profit_lock=100_000)
        rm.check()
        assert rm.is_new_entry_locked is True
        rm.unlock_entry_manual()
        assert rm.is_new_entry_locked is False

    def test_manual_unlock_blocks_relock(self, qapp):
        """수동 해제 후 check() 다시 호출 → manual_unlock_active=True이므로 재락 없음"""
        rm, order_mgr, _ = _make_rm(daily_pnl=100_000, profit_lock=100_000)
        rm.check()
        rm.unlock_entry_manual()
        fired = []
        rm.daily_profit_locked.connect(lambda: fired.append(1))
        rm.check()  # daily_pnl 여전히 100,000이지만 manual_unlock_active=True
        assert rm.is_new_entry_locked is False
        assert len(fired) == 0  # 재발동 없음

    def test_lock_entry_manual(self, qapp):
        """lock_entry_manual() → 강제 락"""
        rm, _, _ = _make_rm(daily_pnl=0)
        assert rm.is_new_entry_locked is False
        rm.lock_entry_manual()
        assert rm.is_new_entry_locked is True


# ── profit_lock + loss_cut 동시 ───────────────────────────────────────────

class TestSimultaneousCheck:

    def test_both_triggered_independently(self, qapp):
        """profit_lock과 loss_cut은 독립적으로 체크"""
        # 수익 상황 (profit lock만 발동)
        rm_profit, _, _ = _make_rm(daily_pnl=200_000, profit_lock=100_000, loss_cut=300_000)
        rm_profit.check()
        assert rm_profit.is_new_entry_locked is True
        assert rm_profit.is_daily_loss_cut_done is False

        # 손실 상황 (loss cut만 발동)
        rm_loss, _, _ = _make_rm(daily_pnl=-300_000, profit_lock=500_000, loss_cut=300_000)
        rm_loss.check()
        assert rm_loss.is_new_entry_locked is False
        assert rm_loss.is_daily_loss_cut_done is True
