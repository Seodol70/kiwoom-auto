import pytest
from unittest.mock import MagicMock
from datetime import time as dtime
from unittest.mock import patch
import logging

# 로깅 설정 (테스트 시 거절 사유 확인용)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scanner.scanner_logger")
logger.setLevel(logging.INFO)

from scanner.signal_evaluator import (
    check_breakout, check_jdm_entry, check_testa_alignment,
    check_volume_surge, check_chejan_strength, check_vwap_filter
)
from scanner.models import StockSnapshot
from scanner.config import SmartScannerConfig

@pytest.fixture
def base_config():
    return SmartScannerConfig()

@pytest.fixture
def base_snap():
    snap = MagicMock(spec=StockSnapshot)
    snap.code = "005930"
    snap.name = "삼성전자"
    snap.prev_close = 10000
    snap.current_price = 10500
    snap.open_price = 10100
    snap.high_price = 10600
    snap.low_price = 10000
    snap.volume = 100000
    snap.trade_amount = 1000000000
    snap.change_pct = 5.0
    snap.chejan_strength = 150.0
    snap.vwap = 10300
    snap.closes_1min = [10000] * 100
    snap.highs_1min = [10100] * 100
    snap.lows_1min = [9900] * 100
    snap.volumes_1min = [10000] * 100
    snap.rsi = 60.0
    snap.trend_level = 0
    snap.rank = 1
    snap.trade_amount = 1000000000
    snap.market_type = "KOSDAQ"
    
    # daily context
    snap.daily_high_prev = 10500
    snap.daily_low_prev = 9500
    snap.daily_closes = [9000] * 10 + [9500] * 10 + [10000] * 10 # Rising daily trend
    snap.exec_velocity_ratio = 2.0
    
    return snap

# ---------- Breakout Tests ----------

def test_check_breakout_success(base_snap):
    # 3% 돌파 조건 (10000 * 1.03 = 10300)
    base_snap.current_price = 10400
    base_snap.high_price = 10400  # 고점 대비 하락 필터 회피
    # 연속 상승 필터를 위해 1분봉 데이터에 상승 추세 반영
    base_snap.closes_1min = [10000] * 90 + [10100, 10200, 10300, 10400]
    reason = check_breakout(base_snap, breakout_ratio=0.03)
    assert reason is not None
    assert "10,400" in reason

def test_check_breakout_fail_below_threshold(base_snap):
    base_snap.current_price = 10200
    reason = check_breakout(base_snap, breakout_ratio=0.03)
    assert reason is None

def test_check_breakout_fail_rising_bars(base_snap):
    base_snap.current_price = 10400
    base_snap.high_price = 10400
    # 마지막 봉이 하락인 경우
    base_snap.closes_1min = [10300, 10500, 10400]
    reason = check_breakout(base_snap, breakout_ratio=0.03, min_rising_bars=2)
    assert reason is None

# ---------- JDM Tests ----------

def test_check_jdm_entry_opening_lite_mode(base_snap, base_config):
    # OPENING 슬롯 (09:05)에서 1분봉 수 부족 시 MTF 필터가 스킵되어야 함
    # (mtf_skip_opening=True 이므로 MTF가 차단해서는 안 됨)
    with patch("scanner.evaluators.jdm.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = dtime(9, 5)
        # MTF 비활성화하여 MTF 차단 없이 OPENING 기본 동작만 검증
        base_config.mtf_enabled = False
        base_config.exec_velocity_enabled = False  # vel 필터도 비활성화
        reason_no_mtf = check_jdm_entry(base_snap, base_config)

        # MTF 활성화 + OPENING 스킵 설정 시 동일 결과여야 함
        base_config.mtf_enabled = True
        base_config.mtf_skip_opening = True
        reason_with_mtf = check_jdm_entry(base_snap, base_config)

        # 두 경우 모두 MTF가 차단 원인이 되어서는 안 됨
        # (결과가 같으면 MTF가 OPENING에서 동작하지 않는 것)
        assert reason_no_mtf == reason_with_mtf, (
            f"OPENING에서 MTF가 결과를 바꿔서는 안 됨: "
            f"no_mtf={reason_no_mtf}, with_mtf={reason_with_mtf}"
        )

def test_check_jdm_entry_midday_strict(base_snap, base_config):
    # 점심 시간 (12:00) - GC 미충족 시 탈락
    with patch("scanner.evaluators.jdm.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = dtime(12, 0)
        base_snap.trend_level = 0
        # 역배열 상태 (MA short < MA long)
        base_snap.closes_1min = [11000] * 20 + [10000] * 30
        reason = check_jdm_entry(base_snap, base_config)
        assert reason is None

def test_check_jdm_entry_gc_override(base_snap, base_config):
    # 호가·MTF 필터가 hoga_ready=False 일 때 JDM 결과를 바꾸지 않음을 검증
    # (기존 캔들 조건과 무관하게, 신규 필터가 추가 차단을 일으키지 않아야 함)
    base_config.hoga_pressure_enabled = True
    base_config.mtf_enabled = True
    with patch("scanner.evaluators.jdm.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = dtime(12, 0)
        base_snap.trend_level = 3

        # 호가 비활성화 (hoga_ready=False) 상태에서 호가 필터 스킵 확인
        base_config.hoga_pressure_enabled = False
        result_no_hoga = check_jdm_entry(base_snap, base_config)

        base_config.hoga_pressure_enabled = True
        # hoga_ready=False → getattr(snap, 'hoga_ready', False) = MagicMock(falsy)
        result_with_hoga = check_jdm_entry(base_snap, base_config)

        # 호가 데이터 미수신 상태이면 결과가 동일해야 함 (호가 필터가 추가 차단하지 않음)
        assert result_no_hoga == result_with_hoga, (
            f"hoga_ready=False 상태에서 호가 필터가 결과를 바꿔서는 안 됨: "
            f"no_hoga={result_no_hoga}, with_hoga={result_with_hoga}"
        )

# ---------- Helper Tests ----------

def test_check_volume_surge(base_snap):
    # 10분 평균 10000, 현재 20000 (2배)
    base_snap.volumes_1min = [10000] * 10 + [20000]
    reason = check_volume_surge(base_snap, surge_mult=1.5, lookback=10)
    assert reason is not None
    assert "2.0배" in reason

def test_check_chejan_strength(base_snap):
    base_snap.chejan_strength = 150.0
    assert check_chejan_strength(base_snap, min_strength=110.0) is not None
    assert check_chejan_strength(base_snap, min_strength=160.0) is None

def test_check_testa_alignment(base_snap):
    # 정배열 셋팅 (MA10 > MA20 > MA50)
    # MA50: (10000*40 + 11000*10)/50 = 10200
    # MA20: (10000*10 + 11000*10)/20 = 10500
    # MA10: 11000
    closes = [10000] * 40 + [11000] * 10
    base_snap.closes_1min = closes
    base_snap.current_price = 11100
    base_snap.high_price = 11100
    # Spread: (11000 - 10200) / 10200 = 800 / 10200 = 7.8%
    reason = check_testa_alignment(base_snap, max_ma_spread=0.1) # 10% 허용
    assert reason is not None

def test_check_vwap_filter(base_snap):
    base_snap.vwap = 10000
    base_snap.current_price = 10100
    assert check_vwap_filter(base_snap) is not None

    base_snap.current_price = 9800
    assert check_vwap_filter(base_snap) is None

from unittest.mock import patch

# ---------- JDM_ENTRY_EARLY Tests (2026-06-19) ----------

from scanner.signal_evaluator import check_jdm_entry_early

def _make_early_snap(base_snap):
    """JDM_ENTRY_EARLY 전용 — 거래량급증 없이도 다른 체크를 통과하는 스냅샷."""
    # 거래량은 평탄하게 유지 (거래량급증 미충족 — EARLY가 대체해야 하는 상황)
    base_snap.volumes_1min = [10000] * 100
    base_snap.hoga_ready = False  # 호가 압력 필터 스킵
    # MA7/MA15 정배열(이격 0.10%+ 확보, RSI는 leading_score 부스트로 커버) — trend_level=3으로 GC_OVERRIDE 적용
    closes = []
    v = 10000
    for i in range(100):
        v += -10 if i % 2 else 25
        closes.append(v)
    base_snap.closes_1min = closes
    base_snap.opens_1min = closes
    base_snap.current_price = closes[-1]
    base_snap.trend_level = 3
    # 피봇 R2를 현재가 미만으로 — 전일 변동폭을 좁게 잡아 R2 돌파 조건 자연 충족
    base_snap.daily_high_prev = 10300
    base_snap.daily_low_prev = 10100
    base_snap.prev_close = 10200
    return base_snap

def test_check_jdm_entry_early_disabled_returns_none(base_snap, base_config):
    base_config.early_entry_enabled = False
    with patch("scanner.evaluators.jdm.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = dtime(11, 0)
        assert check_jdm_entry_early(_make_early_snap(base_snap), base_config) is None

def test_check_jdm_entry_early_no_trigger_blocks(base_snap, base_config):
    """선행점수도, 틱속도 가속도 약하면 EARLY는 차단되어야 함."""
    base_config.early_entry_enabled = True
    with patch("scanner.evaluators.jdm.datetime") as mock_dt, \
         patch("scanner.evaluators.jdm.IndicatorService.get_leading_score", return_value=0.10), \
         patch("scanner.evaluators.jdm.IndicatorService.calc_tick_vol_accel_score", return_value=0.0):
        mock_dt.now.return_value.time.return_value = dtime(11, 0)
        reason = check_jdm_entry_early(_make_early_snap(base_snap), base_config)
        assert reason is None

def test_check_jdm_entry_early_trigger_a_leading_score(base_snap, base_config):
    """A: 선행점수 단독 강세면 거래량급증 없이도 통과해야 함."""
    base_config.early_entry_enabled = True
    base_config.early_entry_leading_min = 0.50
    with patch("scanner.evaluators.jdm.datetime") as mock_dt, \
         patch("scanner.evaluators.jdm.IndicatorService.get_leading_score", return_value=0.65), \
         patch("scanner.evaluators.jdm.IndicatorService.calc_tick_vol_accel_score", return_value=0.0):
        mock_dt.now.return_value.time.return_value = dtime(11, 0)
        reason = check_jdm_entry_early(_make_early_snap(base_snap), base_config)
        assert reason is not None
        assert "EARLY-A" in reason

def test_check_jdm_entry_early_trigger_b_tick_accel(base_snap, base_config):
    """B: 틱속도 가속도 단독 강세면 거래량급증 없이도 통과해야 함.
    leading_score는 0.15(leading_score_min) 이상~0.50(A 트리거) 미만 구간으로 설정해
    A가 아니라 B만으로 통과하는지를 검증한다 — 0.15 미만이면 _jdm_build_ctx 자체가
    JDM_LEADING 단계에서 차단되어 버린다."""
    base_config.early_entry_enabled = True
    base_config.early_entry_tick_accel_min = 0.50
    with patch("scanner.evaluators.jdm.datetime") as mock_dt, \
         patch("scanner.evaluators.jdm.IndicatorService.get_leading_score", return_value=0.20), \
         patch("scanner.evaluators.jdm.IndicatorService.calc_tick_vol_accel_score", return_value=0.80):
        mock_dt.now.return_value.time.return_value = dtime(11, 0)
        reason = check_jdm_entry_early(_make_early_snap(base_snap), base_config)
        assert reason is not None
        assert "EARLY-B" in reason

def test_check_jdm_entry_early_does_not_skip_other_filters(base_snap, base_config):
    """EARLY도 EMA 이격·RSI 등 나머지 필터는 그대로 적용되어야 함 (거래량만 우회)."""
    base_config.early_entry_enabled = True
    snap = _make_early_snap(base_snap)
    snap.trend_level = 0
    # 역배열 상태로 MA 필터에서 차단되어야 함 (JDM_ENTRY와 동일한 필터 적용 검증) — _make_early_snap 이후 덮어씀
    snap.closes_1min = [11000] * 20 + [10000] * 30
    with patch("scanner.evaluators.jdm.datetime") as mock_dt, \
         patch("scanner.evaluators.jdm.IndicatorService.get_leading_score", return_value=0.90):
        mock_dt.now.return_value.time.return_value = dtime(12, 0)
        reason = check_jdm_entry_early(snap, base_config)
        assert reason is None
