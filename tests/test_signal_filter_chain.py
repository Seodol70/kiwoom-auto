"""신호 필터 체인 테스트 (Step 2)"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock

from app.signal_filter import (
    SignalFilterChain,
    SignalFilterContext,
    OverheatPullbackFilter,
    OpeningTimeFilter,
    WeakSignalFilter,
    EntryStrategyFilter,
    AIFilter,
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


class TestOpeningTimeFilter:
    # [FIX 2026-06-10] OpeningTimeFilter가 09:00~09:30을 "완전 차단"으로 강화하면서
    # (커밋 16028c5) 09:00~09:30 구간은 opening_entry_times를 보지 않고 무조건 거부한다.
    # 이 두 테스트는 "60초 내 1건 제한" 로직을 검증하려는 의도인데 now=09:05를 써서
    # 실제로는 완전 차단 분기만 타고 있었다(60초 제한 로직은 검증되지 않은 채 우연히
    # 통과/실패했음). 60초 제한이 적용되는 09:30~10:00 구간으로 옮겨 의도대로 검증한다.
    def test_reject_multiple_entries_in_60sec(self):
        filter = OpeningTimeFilter()
        sig = MockSignal()
        now = datetime.strptime("09:45", "%H:%M").replace(
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
        now = datetime.strptime("09:45", "%H:%M").replace(
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


# ============================================================================
# 필터 체인 통합 테스트
# ============================================================================


class TestSignalFilterChain:
    def test_chain_stops_at_first_failure(self):
        """체인은 첫 실패에서 중단해야 함"""
        chain = SignalFilterChain()
        sig = MockSignal(code="005930", signal_type="JDM_ENTRY")

        snap_store = MockSnapshotStore()
        snap_store.snapshots["005930"] = MockSnapshot(trend_level=0)  # WeakSignalFilter에서 차단

        order_mgr = MockOrderManager()
        order_mgr._strategy.should_entry = Mock(return_value=(True, ""))

        ctx = SignalFilterContext(
            order_mgr=order_mgr,
            snap_store=snap_store,
            trading_cfg=MockConfig(),
            risk_mgr=Mock(),
            now=datetime.strptime("10:00", "%H:%M").replace(
                year=datetime.now().year,
                month=datetime.now().month,
                day=datetime.now().day
            ),
        )

        passed, reason = chain.validate(sig, ctx)
        assert not passed
        assert "약한신호" in reason or "trend_lv" in reason

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
