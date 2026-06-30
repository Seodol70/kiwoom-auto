"""
test_trading_controller_eod_exits.py — EOD(종가매매) 청산 4종 메서드 검증

배경(2026-06-29): ui/signal_manager.py에서 MarketScheduler의 EOD 전용 신호 4개가
전부 tc.tick_exit_check(check_and_exit_all)에만 연결되어 있어, app/trading_controller.py에
이미 구현된 check_eod_daytime_targets/check_overnight_gap/check_overnight_trend_break/
check_overnight_timecut이 한 번도 호출되지 않는 연결 결함이 있었다. 신호 연결은
수리했지만(ui/signal_manager.py), 4개 메서드 자체의 트리거 조건이 의도대로 동작하는지는
별도로 검증되지 않았으므로 이 테스트로 명문화한다.
"""

from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

from app.trading_controller import TradingController


class MockPosition:
    """EOD 청산 테스트용 Position Mock"""

    def __init__(
        self,
        code: str,
        name: str,
        qty: int,
        avg_price: int,
        current_price: int,
        eod_trade: bool = True,
        overnight_held: bool = False,
    ):
        self.code = code
        self.name = name
        self.qty = qty
        self.avg_price = avg_price
        self.current_price = current_price
        self.peak_price = current_price
        self.entry_time = None
        self.eod_trade = eod_trade
        self.overnight_held = overnight_held

    @property
    def price_change_pct_vs_avg(self) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price * 100


class MockOrderManager:
    def __init__(self):
        self.positions = {}
        self._audit = None
        self.mark_stop_loss = MagicMock()
        self.force_exit = MagicMock()
        self.sell = MagicMock()

    def is_pending(self, code: str) -> bool:
        return False


class MockConfig:
    hard_stop_pct = -2.0
    stop_loss_pct = -1.5
    jdm_take_profit_pct = 3.0
    partial_profit_pct = 1.5
    eod_gap_up_exit_pct = 2.0
    eod_gap_down_exit_pct = -1.5
    eod_timecut_min_pct = 1.0


class MockRiskManager:
    def __init__(self):
        self.daily_loss_cut = MagicMock()
        self.daily_profit_locked = MagicMock()

    @property
    def is_new_entry_locked(self):
        return False

    @property
    def is_daily_loss_cut_done(self):
        return False


@pytest.fixture
def controller():
    QApplication.instance() or QApplication([])
    order_mgr = MockOrderManager()
    tc = TradingController(order_mgr=order_mgr, scan_cfg=MockConfig(), risk_mgr=MockRiskManager(), parent=None)
    return tc, order_mgr


# ── check_overnight_gap (Stage 1: 익일 갭 체크) ────────────────────────────

def test_overnight_gap_up_triggers_exit(controller):
    """갭 상승 2.0% 이상이면 즉시 익절"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 71_500, eod_trade=True)  # +2.14%
    om.positions["005930"] = pos

    tc.check_overnight_gap()

    om.force_exit.assert_called_once()
    assert "EOD 갭익절" in om.force_exit.call_args.kwargs.get("reason", om.force_exit.call_args.args[-1] if om.force_exit.call_args.args else "")


def test_overnight_gap_down_triggers_stop_loss(controller):
    """갭 하락 -1.5% 이하면 즉시 손절 + mark_stop_loss"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 68_900, eod_trade=True)  # -1.57%
    om.positions["005930"] = pos

    tc.check_overnight_gap()

    om.mark_stop_loss.assert_called_once_with("005930")
    om.force_exit.assert_called_once()


def test_overnight_gap_neutral_sets_overnight_held(controller):
    """갭이 -1.5%~+2.0% 사이(보합)면 overnight_held=True로 전환, 청산하지 않음"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 70_100, eod_trade=True)  # +0.14%
    om.positions["005930"] = pos

    tc.check_overnight_gap()

    assert pos.overnight_held is True
    om.force_exit.assert_not_called()


def test_overnight_gap_skips_non_eod_positions(controller):
    """eod_trade=False 포지션은 평가 대상에서 제외"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 68_000, eod_trade=False)
    om.positions["005930"] = pos

    tc.check_overnight_gap()

    om.force_exit.assert_not_called()


# ── check_eod_daytime_targets (Stage 2: 당일 일중 손익) ────────────────────

def test_eod_daytime_stop_loss(controller):
    """eod_trade=True & overnight_held=False, 손익률 <= -1.5%면 손절"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 68_900, eod_trade=True, overnight_held=False)  # -1.57%
    om.positions["005930"] = pos

    tc.check_eod_daytime_targets()

    om.mark_stop_loss.assert_called_once_with("005930")
    om.force_exit.assert_called_once()


def test_eod_daytime_full_take_profit(controller):
    """손익률 >= 3.0%면 완전 익절"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 72_200, eod_trade=True, overnight_held=False)  # +3.14%
    om.positions["005930"] = pos

    tc.check_eod_daytime_targets()

    om.force_exit.assert_called_once()


def test_eod_daytime_overnight_held_excluded(controller):
    """overnight_held=True(익일 보합 전환분)는 당일 타겟 평가 대상에서 제외"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 68_900, eod_trade=True, overnight_held=True)
    om.positions["005930"] = pos

    tc.check_eod_daytime_targets()

    om.force_exit.assert_not_called()


# ── check_overnight_trend_break (Stage 3: 일봉 정배열 파괴) ────────────────

def test_overnight_trend_break_triggers_exit_when_alignment_broken():
    """overnight_held=True 포지션의 일봉 정배열이 깨지면 강제 청산 + overnight_held 리셋"""
    QApplication.instance() or QApplication([])
    om = MockOrderManager()
    tc = TradingController(order_mgr=om, scan_cfg=MockConfig(), risk_mgr=MockRiskManager(), parent=None)

    pos = MockPosition("005930", "삼성전자", 10, 70_000, 69_000, eod_trade=True, overnight_held=True)
    # 정배열이 깨지는 하락 시퀀스(최근일이 더 낮음 → 5MA < 10MA < 20MA).
    # current_price는 check_daily_alignment()가 daily_closes 끝에 그대로 append하므로
    # daily_closes와 동일 스케일(100대)로 맞춰야 MA 계산이 왜곡되지 않는다.
    pos.current_price = 87.0
    pos._snapshot_daily_closes = [100.0 - i * 0.5 for i in range(25)]
    om.positions["005930"] = pos

    tc.check_overnight_trend_break()

    om.force_exit.assert_called_once()
    assert pos.overnight_held is False


def test_overnight_trend_break_holds_when_alignment_intact():
    """일봉 정배열이 유지되면 청산하지 않음"""
    QApplication.instance() or QApplication([])
    om = MockOrderManager()
    tc = TradingController(order_mgr=om, scan_cfg=MockConfig(), risk_mgr=MockRiskManager(), parent=None)

    pos = MockPosition("005930", "삼성전자", 10, 70_000, 70_000, eod_trade=True, overnight_held=True)
    # current_price를 daily_closes(100대)와 동일 스케일로 맞춤 — test 위 케이스와 동일 이유
    pos.current_price = 113.0
    pos._snapshot_daily_closes = [100.0 + i * 0.5 for i in range(25)]  # 우상향 정배열 유지
    om.positions["005930"] = pos

    tc.check_overnight_trend_break()

    om.force_exit.assert_not_called()
    assert pos.overnight_held is True


# ── check_overnight_timecut (Stage 4: 익일 09:30 타임컷) ───────────────────

def test_overnight_timecut_triggers_when_profit_below_threshold(controller):
    """overnight_held=True, 수익률 < eod_timecut_min_pct(1.0%)면 강제 청산"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 70_300, eod_trade=True, overnight_held=True)  # +0.43%
    om.positions["005930"] = pos

    tc.check_overnight_timecut()

    om.force_exit.assert_called_once()


def test_overnight_timecut_holds_when_profit_meets_threshold(controller):
    """수익률 >= 1.0%면 타임컷 면제(트레일/일반 로직에 위임)"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 70_800, eod_trade=True, overnight_held=True)  # +1.14%
    om.positions["005930"] = pos

    tc.check_overnight_timecut()

    om.force_exit.assert_not_called()


def test_overnight_timecut_skips_non_overnight_held(controller):
    """overnight_held=False(아직 갭체크 전)는 타임컷 평가 대상이 아님"""
    tc, om = controller
    pos = MockPosition("005930", "삼성전자", 10, 70_000, 68_000, eod_trade=True, overnight_held=False)
    om.positions["005930"] = pos

    tc.check_overnight_timecut()

    om.force_exit.assert_not_called()
