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
    # 장 초반 (09:05)
    with patch("scanner.evaluators.jdm.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = dtime(9, 5)
        # 09:05분은 OPENING 슬롯 (lite_mode)
        # lite_mode 발동을 위해 캔들 개수를 10개로 설정
        base_snap.closes_1min = [10000] * 9 + [10100]
        # 거래량 급증 (평균 10000 -> 현재 20000)
        base_snap.volumes_1min = [10000] * 10 + [20000]
        base_snap.current_price = 10200
        base_snap.high_price = 10200
        base_snap.rsi = 50.0
        base_snap.trend_level = 1
        
        reason = check_jdm_entry(base_snap, base_config)
        assert reason is not None

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
    # 점심 시간이라도 추세 Lv3이면 GC 없이 진입 허용
    # adaptive_params.json이 candle_skip_trend_level=99로 설정하므로 테스트에서 2로 명시
    base_config.jdm_candle_skip_trend_level = 2
    with patch("scanner.evaluators.jdm.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = dtime(12, 0)
        base_snap.trend_level = 3
        # R2 필터 통과를 위해 전일 변동성 축소 (R2=10100 < current_price=10350)
        base_snap.daily_high_prev = 10050
        base_snap.daily_low_prev = 9950

        # RSI < 70 유지를 위해 완만한 상승 패턴 사용
        # ma_s(7) ≈ 10329, ma_l(15) ≈ 10257, spread ≈ 0.70%, RSI ≈ 67
        base_snap.closes_1min = [10000] * 35 + [10100, 10150, 10100, 10200, 10250, 10200, 10250, 10300, 10250, 10300, 10350, 10300, 10350, 10400, 10350]
        # Phase A 거래대금 필터 3.0배 통과 (직전 5봉 평균의 3.5배 거래대금)
        base_snap.volumes_1min = [10000] * 50 + [35000]
        base_snap.opens_1min = [10000] * 100  # 갭 리버설 패턴 체크 위해 추가
        # highs/lows를 closes와 일관성 있게 설정
        base_snap.highs_1min = [10050] * 35 + [10150, 10200, 10150, 10250, 10300, 10250, 10300, 10350, 10300, 10350, 10400, 10350, 10400, 10450, 10400]
        base_snap.lows_1min  = [9950]  * 35 + [10050, 10100, 10050, 10150, 10200, 10150, 10200, 10250, 10200, 10250, 10300, 10250, 10300, 10350, 10300]
        base_snap.current_price = 10350
        base_snap.high_price = 10400
        reason = check_jdm_entry(base_snap, base_config)
        assert reason is not None

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
