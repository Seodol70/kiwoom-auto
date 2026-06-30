"""
test_evaluate_characterization.py — SmartScanner._evaluate() 캐릭터라이제이션 테스트

배경(리팩토링 1단계, 2026-06-30): _evaluate()(scanner/smart_scanner.py:852-996, 144줄)는
기본필터(쿨다운/등락률/시간) + 지표계산(요셉추세/60분봉/MTF) + 8개 전략 순차위임이
한 메서드에 혼재한다. 이 테스트는 향후(리팩토링 5단계) _check_basic_filters()/
_update_indicators() 등으로 Extract Method 할 때, "거래 로직을 단 한 줄도 바꾸지
않았다"는 것을 보증하는 안전망이다. strategy_map은 mock으로 교체해 전략 자체의
신호 판정 로직과 무관하게 _evaluate()의 오케스트레이션(필터링 -> 위임 -> 첫 신호에서
중단)만 검증한다.
"""

from datetime import time as dtime
from unittest.mock import MagicMock, patch

import pytest

from scanner.smart_scanner import SmartScanner, SmartScannerConfig
from scanner.models import StockSnapshot


class MockOcx:
    class _FakeSignal:
        def connect(self, fn):
            pass

    OnReceiveRealData = _FakeSignal()

    def dynamicCall(self, method, args):
        return ""


class MockKiwoom:
    def __init__(self):
        self._ocx = MockOcx()


def make_scanner(cfg=None):
    kiwoom = MockKiwoom()
    cfg = cfg or SmartScannerConfig()
    with patch("scanner.smart_scanner.PriorityWatchQueue") as mock_wq:
        mock_wq.return_value = MagicMock()
        scanner = SmartScanner(kiwoom, cfg)
    # [FIX] 실제 _emit()은 신호 발생 시 백그라운드 스레드에서
    # infra.db_manager.DatabaseManager()(싱글톤)를 호출한다. 다른 테스트가 그보다
    # 먼저 DatabaseManager(db_path="data/test_trading.db")로 초기화해도 싱글톤이라
    # __init__이 재실행되지 않아, 이 테스트가 실제 운영 DB(data/trading.db) 쪽
    # 싱글톤을 먼저 선점해버리면 다른 테스트의 DB 경로가 의도와 달라지는 사고가
    # 난다(test_sqlite_integration.py가 깨짐). _evaluate()의 오케스트레이션만
    # 검증하면 충분하므로 _emit은 항상 mock으로 막는다.
    scanner._emit = MagicMock()
    return scanner


def make_snap(code="005930", name="삼성전자", current_price=70_000, change_pct=2.0):
    return StockSnapshot(
        code=code, name=name, current_price=current_price,
        open_price=current_price - 500, high_price=current_price + 500,
        low_price=current_price - 1000, prev_close=current_price - 1000,
        volume=100_000, trade_amount=10_000_000_000, change_pct=change_pct,
    )


# ── 앞단 게이트 ──────────────────────────────────────────────────────────

def test_universe_paused_blocks_evaluation():
    """포지션 풀로 _universe_paused=True면 평가 자체를 스킵"""
    scanner = make_scanner()
    scanner._universe_paused = True
    scanner.strategy_map = {"JDM_ENTRY": MagicMock()}

    result = scanner._evaluate(make_snap())

    assert result is None
    scanner.strategy_map["JDM_ENTRY"].evaluate.assert_not_called()


def test_cooldown_blocks_repeated_evaluation_within_30sec():
    """동일 종목을 30초 이내 재평가하면 쿨다운에 걸려 None"""
    scanner = make_scanner()
    strat = MagicMock()
    strat.evaluate.return_value = None
    scanner.strategy_map = {"JDM_ENTRY": strat}
    scanner.cfg.enabled_strategies = ("JDM_ENTRY",)
    scanner.cfg.strategy_order = ("JDM_ENTRY",)

    snap = make_snap()
    scanner._evaluate(snap)  # 1차 평가 — _last_eval_ts 기록
    strat.evaluate.reset_mock()

    result = scanner._evaluate(snap)  # 즉시 재평가 — 쿨다운 안쪽

    assert result is None
    strat.evaluate.assert_not_called()


def test_exploration_mode_shortens_cooldown_to_half_second():
    """exploration_mode=True면 쿨다운이 0.5초로 단축된다 — 30초 쿨다운 시점엔
    이미 풀려서 평가가 다시 호출된다 (시간 흐름 없이 검증하기 위해 _last_eval_ts를
    0.6초 전으로 직접 세팅)"""
    import time as time_module
    scanner = make_scanner()
    scanner.cfg.exploration_mode = True
    strat = MagicMock()
    strat.evaluate.return_value = None
    scanner.strategy_map = {"JDM_ENTRY": strat}
    scanner.cfg.enabled_strategies = ("JDM_ENTRY",)
    scanner.cfg.strategy_order = ("JDM_ENTRY",)

    snap = make_snap()
    scanner._last_eval_ts = {snap.code: time_module.monotonic() - 0.6}

    scanner._evaluate(snap)

    strat.evaluate.assert_called_once()


def test_change_pct_below_min_blocks_evaluation():
    """등락률이 min_change_pct 미만이면 차단"""
    scanner = make_scanner()
    scanner.cfg.min_change_pct = -1.5
    scanner.strategy_map = {"JDM_ENTRY": MagicMock()}

    result = scanner._evaluate(make_snap(change_pct=-2.0))

    assert result is None
    scanner.strategy_map["JDM_ENTRY"].evaluate.assert_not_called()


def test_change_pct_at_or_above_max_blocks_evaluation():
    """등락률이 max_change_pct 이상이면 차단 (경계 포함, >=)"""
    scanner = make_scanner()
    scanner.cfg.max_change_pct = 22.0
    scanner.strategy_map = {"JDM_ENTRY": MagicMock()}

    result = scanner._evaluate(make_snap(change_pct=22.0))  # 경계값

    assert result is None
    scanner.strategy_map["JDM_ENTRY"].evaluate.assert_not_called()


def test_time_outside_entry_window_blocks_evaluation():
    """entry_start_time~entry_end_time 밖이면 차단"""
    scanner = make_scanner()
    scanner.cfg.entry_start_time = dtime(9, 0)
    scanner.cfg.entry_end_time = dtime(9, 30)
    scanner.strategy_map = {"JDM_ENTRY": MagicMock()}

    with patch("scanner.smart_scanner.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = dtime(15, 0)  # 윈도우 밖
        result = scanner._evaluate(make_snap())

    assert result is None
    scanner.strategy_map["JDM_ENTRY"].evaluate.assert_not_called()


# ── 전략 위임 오케스트레이션 ──────────────────────────────────────────────

def test_strategy_order_respected_and_stops_at_first_signal():
    """strategy_order 순서대로 평가하고, 신호가 나온 첫 전략에서 멈춘다(이후 전략 미평가)"""
    scanner = make_scanner()
    strat_a = MagicMock()
    strat_a.evaluate.return_value = None  # A는 신호 없음
    strat_b = MagicMock()
    fake_sig = MagicMock()
    strat_b.evaluate.return_value = fake_sig  # B가 신호 발생
    strat_c = MagicMock()
    scanner.strategy_map = {"A": strat_a, "B": strat_b, "C": strat_c}
    scanner.cfg.enabled_strategies = ("A", "B", "C")
    scanner.cfg.strategy_order = ("A", "B", "C")

    result = scanner._evaluate(make_snap())

    assert result == "B"
    strat_a.evaluate.assert_called_once()
    strat_b.evaluate.assert_called_once()
    strat_c.evaluate.assert_not_called()  # B에서 멈췄으므로 C는 평가 안 됨


def test_disabled_strategy_in_order_is_skipped():
    """strategy_order에 있어도 enabled_strategies에 없으면 평가하지 않는다"""
    scanner = make_scanner()
    strat_a = MagicMock()
    scanner.strategy_map = {"A": strat_a}
    scanner.cfg.enabled_strategies = ()  # A 비활성화
    scanner.cfg.strategy_order = ("A",)

    result = scanner._evaluate(make_snap())

    assert result is None
    strat_a.evaluate.assert_not_called()


def test_unknown_strategy_name_skipped_without_crash():
    """strategy_map에 없는 전략명이 strategy_order에 있어도 예외 없이 스킵"""
    scanner = make_scanner()
    strat_b = MagicMock()
    strat_b.evaluate.return_value = None
    scanner.strategy_map = {"B": strat_b}  # "A"는 strategy_map에 없음
    scanner.cfg.enabled_strategies = ("A", "B")
    scanner.cfg.strategy_order = ("A", "B")

    result = scanner._evaluate(make_snap())

    assert result is None  # 예외 없이 B까지 평가하고 신호 없음


def test_strategy_exception_does_not_crash_evaluate_loop():
    """한 전략에서 예외가 발생해도 _evaluate()가 죽지 않고 다음 전략으로 계속 진행"""
    scanner = make_scanner()
    strat_a = MagicMock()
    strat_a.evaluate.side_effect = RuntimeError("boom")
    strat_b = MagicMock()
    fake_sig = MagicMock()
    strat_b.evaluate.return_value = fake_sig
    scanner.strategy_map = {"A": strat_a, "B": strat_b}
    scanner.cfg.enabled_strategies = ("A", "B")
    scanner.cfg.strategy_order = ("A", "B")

    result = scanner._evaluate(make_snap())

    assert result == "B"  # A 예외 발생해도 B는 정상 평가됨


def test_signal_emitted_carries_trend_level_from_snap():
    """전략이 신호를 내면 sig.trend_level/trend_prev_level이 snap 값으로 채워진다"""
    scanner = make_scanner()
    strat = MagicMock()
    fake_sig = MagicMock()
    strat.evaluate.return_value = fake_sig
    scanner.strategy_map = {"A": strat}
    scanner.cfg.enabled_strategies = ("A",)
    scanner.cfg.strategy_order = ("A",)
    scanner._emit = MagicMock()

    snap = make_snap()
    snap.trend_level = 3
    snap.trend_prev_level = 1

    scanner._evaluate(snap)

    assert fake_sig.trend_level == 3
    assert fake_sig.trend_prev_level == 1
    scanner._emit.assert_called_once_with(fake_sig)
