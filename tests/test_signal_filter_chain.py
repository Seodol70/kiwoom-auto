"""신호 필터 체인 테스트 (Step 2)"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock

from app.signal_filter import (
    SignalFilterChain,
    SignalFilterContext,
    OverheatPullbackFilter,
    MockSignalFilter,
    OpeningTimeFilter,
    WeakSignalFilter,
    EntryStrategyFilter,
    InvestorFilter,
    NewsFilter,
    AIFilter,
    RSFilter,
)


# ============================================================================
# Mock 객체 정의
# ============================================================================


class MockSnapshot:
    """StockSnapshot 모의 객체"""
    def __init__(self, code="005930", trend_level=2, foreign_net=100, inst_net=100, rs_score=0.5):
        self.code = code
        self.name = "삼성전자"
        self.trend_level = trend_level
        self.foreign_net_buy = foreign_net
        self.inst_net_buy = inst_net
        self.rs_score = rs_score


class MockSnapshotStore:
    """SnapshotStore 모의 객체"""
    def __init__(self):
        self.snapshots = {}

    def get_snapshot(self, code):
        return self.snapshots.get(code)

    def update_investor(self, code, foreign_net, inst_net):
        snap = self.snapshots.get(code)
        if snap:
            snap.foreign_net_buy = foreign_net
            snap.inst_net_buy = inst_net


class MockOrderManager:
    """OrderManager 모의 객체"""
    def __init__(self):
        self.positions = {}
        self._strategy = Mock()
        self._auto_trading = False
        self._ai_filter = Mock()
        self._ai_filter.should_enter = Mock(return_value=(True, 0.7))
        self._ai_filter.is_ready = True


class MockConfig:
    """SmartScannerConfig 모의 객체"""
    def __init__(self):
        self.phase1_max_positions = 3
        self.ai_threshold = 0.5
        self.rs_threshold = 0.0
        self.exploration_mode = False


class MockSignal:
    """ScanSignal 모의 객체"""
    def __init__(self, code="005930", name="삼성전자", signal_type="JDM_ENTRY", emitted_at=None):
        self.code = code
        self.name = name
        self.signal_type = signal_type
        self.entry_phase = 0
        self.reason = "test"
        self.price = 50000
        self.emitted_at = emitted_at  # None이면 EntryStrategyFilter가 datetime.now() 사용


# ============================================================================
# 필터별 단위 테스트
# ============================================================================


class TestOverheatPullbackFilter:
    def test_reject_when_loss_cut_active(self):
        """손절컷 활성 시 OVERHEAT_PULLBACK 차단"""
        filter = OverheatPullbackFilter()
        sig = MockSignal(signal_type="OVERHEAT_PULLBACK")
        risk_mgr = Mock()
        risk_mgr.is_loss_cut = True
        risk_mgr.is_profit_lock = False
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=risk_mgr,
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed
        assert "손절컷" in reason

    def test_reject_when_profit_lock_active(self):
        """이익잠금 활성 시 OVERHEAT_PULLBACK 차단"""
        filter = OverheatPullbackFilter()
        sig = MockSignal(signal_type="OVERHEAT_PULLBACK")
        risk_mgr = Mock()
        risk_mgr.is_loss_cut = False
        risk_mgr.is_profit_lock = True
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=risk_mgr,
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed
        assert "이익잠금" in reason

    def test_pass_when_no_risk_issues(self):
        """리스크 이슈 없을 때 OVERHEAT_PULLBACK 통과"""
        filter = OverheatPullbackFilter()
        sig = MockSignal(signal_type="OVERHEAT_PULLBACK")
        risk_mgr = Mock()
        risk_mgr.is_loss_cut = False
        risk_mgr.is_profit_lock = False
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=risk_mgr,
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed

    def test_accept_other_signals(self):
        """OVERHEAT_PULLBACK 이외 신호는 무조건 통과"""
        filter = OverheatPullbackFilter()
        sig = MockSignal(signal_type="JDM_ENTRY")
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed


class TestMockSignalFilter:
    def test_reject_mock_code_000003(self):
        filter = MockSignalFilter()
        sig = MockSignal(code="000003", name="Test")
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed

    def test_reject_mock_name(self):
        filter = MockSignalFilter()
        sig = MockSignal(code="005930", name="MagicMock")
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed

    def test_accept_real_signals(self):
        filter = MockSignalFilter()
        sig = MockSignal(code="005930", name="삼성전자")
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed


class TestOpeningTimeFilter:
    def test_reject_multiple_entries_in_60sec(self):
        filter = OpeningTimeFilter()
        sig = MockSignal()
        now = datetime.strptime("09:05", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day
        )
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
            now=now,
            opening_entry_times=[now - timedelta(seconds=30)],  # 30초 전
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed

    def test_accept_after_60sec(self):
        filter = OpeningTimeFilter()
        sig = MockSignal()
        now = datetime.strptime("09:05", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day
        )
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
            now=now,
            opening_entry_times=[now - timedelta(seconds=120)],  # 120초 전
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed

    def test_skip_filter_outside_opening_hours(self):
        filter = OpeningTimeFilter()
        sig = MockSignal()
        now = datetime.strptime("11:00", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day
        )
        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
            now=now,
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed


class TestWeakSignalFilter:
    def test_reject_weak_signal_after_0930(self):
        filter = WeakSignalFilter()
        sig = MockSignal()
        now = datetime.strptime("09:31", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day
        )
        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot(trend_level=1)

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=snap_store,
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
            now=now,
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed

    def test_accept_strong_signal_after_0930(self):
        filter = WeakSignalFilter()
        sig = MockSignal()
        now = datetime.strptime("09:31", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day
        )
        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot(trend_level=2)

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=snap_store,
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
            now=now,
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed

    def test_skip_filter_before_0930(self):
        filter = WeakSignalFilter()
        sig = MockSignal()
        now = datetime.strptime("09:15", "%H:%M").replace(
            year=datetime.now().year,
            month=datetime.now().month,
            day=datetime.now().day
        )

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=MockSnapshotStore(),
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
            now=now,
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed


class TestInvestorFilter:
    def test_reject_both_selling(self):
        filter = InvestorFilter()
        sig = MockSignal()
        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot(foreign_net=-1000, inst_net=-1000)

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=snap_store,
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed

    def test_accept_buying(self):
        filter = InvestorFilter()
        sig = MockSignal()
        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot(foreign_net=1000, inst_net=1000)

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=snap_store,
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed


class TestRSFilter:
    def test_reject_low_rs_score(self):
        filter = RSFilter()
        sig = MockSignal()
        cfg = MockConfig()
        cfg.rs_threshold = 0.5
        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot(rs_score=0.3)

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=snap_store,
            trading_cfg=cfg,
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert not passed

    def test_accept_high_rs_score(self):
        filter = RSFilter()
        sig = MockSignal()
        cfg = MockConfig()
        cfg.rs_threshold = 0.3
        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot(rs_score=0.5)

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=snap_store,
            trading_cfg=cfg,
            risk_mgr=Mock(),
        )

        passed, reason = filter.validate(sig, ctx)
        assert passed


# ============================================================================
# 필터 체인 통합 테스트
# ============================================================================


class TestSignalFilterChain:
    def test_chain_stops_at_first_failure(self):
        """체인은 첫 실패에서 중단해야 함"""
        chain = SignalFilterChain()
        sig = MockSignal(code="000003")  # Mock code → 2번째 필터에서 차단

        snap_store = MockSnapshotStore()
        snap_store.snapshots["000003"] = MockSnapshot()

        ctx = SignalFilterContext(
            order_mgr=MockOrderManager(),
            snap_store=snap_store,
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = chain.validate(sig, ctx)
        assert not passed
        assert "테스트신호" in reason or "Mock" in reason

    def test_chain_all_pass_returns_true(self):
        """모든 필터 통과 시 True 반환"""
        chain = SignalFilterChain()
        sig = MockSignal(code="005930", signal_type="JDM_ENTRY")

        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot()

        order_mgr = MockOrderManager()
        order_mgr._strategy.should_entry = Mock(return_value=(True, ""))

        ctx = SignalFilterContext(
            order_mgr=order_mgr,
            snap_store=snap_store,
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
        )

        passed, reason = chain.validate(sig, ctx)
        assert passed
        assert reason == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
