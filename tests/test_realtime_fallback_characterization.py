"""
test_realtime_fallback_characterization.py — SmartScanner._on_receive_real_data()
기준가(prev_close) 5단계 fallback + 호가잔량 5단계 파싱 캐릭터라이제이션

배경(리팩토링 1단계, 2026-06-30): _on_receive_real_data()(scanner/smart_scanner.py:
1093-1303, 210줄)는 FID파싱+기준가복구(5단계 fallback)+거래대금/등락률/체결강도갱신+
호가잔량업데이트+포지션실시간신호가 한 메서드에 혼재한다. 기존 test_smartscanner_realtime.py는
기본 흐름만 다루고 기준가 fallback 단계별 동작과 호가 5단계 파싱은 다루지 않는다.

기준가 fallback 순서(코드상 순서 그대로):
  1) change_amt(FID11) != 0 -> price - change_amt
  2) (실패) 기존 snapshot의 prev_close 재사용
  3) (실패) kiwoom.get_current_price(code)
  4) (실패) kiwoom.get_master_price(code)
  5) (실패) open_price(FID16)로 대체
  6) (실패) price(현재가) 그대로 대체
  7) 그 뒤에도 prev_close<=0이면 kiwoom.get_master_last_price(code) 한 번 더 시도
     (6번에서 이미 price로 채워지므로 사실상 도달 불가능한 분기 — 이 테스트로 명문화)

이 테스트는 향후(리팩토링 5단계) _on_receive_real_data()를 _resolve_prev_close() 등으로
Extract Method 할 때, fallback 순서가 단 하나도 바뀌지 않았음을 보증하는 안전망이다.
"""

from unittest.mock import MagicMock, patch

import pytest

from scanner.smart_scanner import SmartScanner, SmartScannerConfig


class MockOcx:
    class _FakeSignal:
        def connect(self, fn):
            pass

    OnReceiveRealData = _FakeSignal()

    def __init__(self, fid_returns=None):
        self._fid = fid_returns or {}

    def dynamicCall(self, method, args):
        if method == "GetCommRealData(QString, int)":
            fid = args[1]
            return str(self._fid.get(fid, "0"))
        return ""


class MockKiwoom:
    """기준가 fallback 단계별 응답을 제어할 수 있는 Kiwoom mock"""

    def __init__(self, fid_returns=None, current_price=0, master_price=0, master_last_price=0):
        self._ocx = MockOcx(fid_returns)
        self._current_price = current_price
        self._master_price = master_price
        self._master_last_price = master_last_price
        self.get_current_price_calls = []
        self.get_master_price_calls = []
        self.get_master_last_price_calls = []

    def get_current_price(self, code):
        self.get_current_price_calls.append(code)
        return self._current_price

    def get_master_price(self, code):
        self.get_master_price_calls.append(code)
        return self._master_price

    def get_master_last_price(self, code):
        self.get_master_last_price_calls.append(code)
        return self._master_last_price


def seed_store(store, code="005930", name="삼성전자", price=70000, prev_close=None):
    store.bulk_update([{
        "code": code, "name": name,
        "current_price": price, "open_price": price,
        "high_price": price + 1000, "low_price": price - 1000,
        "volume": 1_000_000, "trade_amount": 7_000_000_000,
        "prev_close": prev_close if prev_close is not None else price - 1000,
        "change_pct": 1.0, "rank": 1,
    }])
    # [발견] bulk_update()는 prev_close가 None/0이거나 change_pct가 0이면 둘 중 하나로
    # 자동 보정한다(snapshot_store.py:150-159, 244-249) — 0을 그대로 유지시킬 방법이
    # bulk_update 경로에는 없다. 또한 _on_receive_real_data()의 2단계 fallback
    # ("기존 snapshot의 prev_close 재사용", smart_scanner.py:1208)은 internal state가
    # 아니라 store.get_snapshot()(DataFrame 기반)에서 읽으므로, prev_close=0을 강제하려면
    # internal state뿐 아니라 DataFrame 컬럼값도 함께 0으로 덮어써야 한다.
    if prev_close == 0:
        store._get_state(code).prev_close = 0
        with store._lock:
            store._df.at[code, "prev_close"] = 0


def make_scanner(kiwoom):
    cfg = SmartScannerConfig()
    with patch("scanner.smart_scanner.PriorityWatchQueue") as mock_wq:
        mock_wq.return_value = MagicMock()
        scanner = SmartScanner(kiwoom, cfg)
    return scanner


def make_fid_map(price=15000, change_amt=0, pct=0.0, open_=15100, high=15200, low=14800, strength=100.0):
    return {10: price, 11: change_amt, 12: pct, 13: 0, 16: open_, 17: high, 18: low, 20: strength}


def internal_prev_close(scanner, code="005930") -> int:
    """[발견] SnapshotStore.get_snapshot()의 prev_close는 DataFrame(self._df)에서 읽지만,
    _on_receive_real_data() -> update_price()는 InternalStockState(st.prev_close)만 갱신하고
    DataFrame은 건드리지 않는다. 따라서 실시간 틱으로 갱신된 prev_close를 확인하려면
    get_snapshot()이 아니라 내부 state를 직접 봐야 한다 — 이 자체가 캐릭터라이제이션
    가치가 있는 발견이라 헬퍼로 명문화한다."""
    return scanner.store._get_state(code).prev_close


def internal_change_pct(scanner, code="005930") -> float:
    return scanner.store._get_state(code).change_pct


# ── 기준가 fallback 1단계: change_amt 정상 ──────────────────────────────

def test_prev_close_stage1_from_change_amt():
    """FID11(전일대비금액)이 0이 아니면 price-change_amt로 즉시 확정"""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=500))
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=10000)  # 기존 prev_close와 다른 값

    scanner._on_receive_real_data("005930", "주식체결", "")

    assert internal_prev_close(scanner) == 14500  # 15000 - 500
    assert kiwoom.get_current_price_calls == []  # 1단계에서 확정, fallback 미호출


# ── 기준가 fallback 2단계: 기존 snapshot 재사용 ──────────────────────────

def test_prev_close_stage2_reuses_existing_snapshot():
    """change_amt=0이면 기존 snapshot의 prev_close를 재사용"""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=0))
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=13500)

    scanner._on_receive_real_data("005930", "주식체결", "")

    assert internal_prev_close(scanner) == 13500  # 기존값 그대로
    assert kiwoom.get_current_price_calls == []  # 2단계에서 확정


# ── 기준가 fallback 3단계: get_current_price ─────────────────────────────

def test_prev_close_stage3_falls_back_to_get_current_price():
    """change_amt=0 + 기존 snapshot prev_close도 0이면 get_current_price() 호출"""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=0), current_price=12000)
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=0)

    scanner._on_receive_real_data("005930", "주식체결", "")

    assert internal_prev_close(scanner) == 12000
    assert kiwoom.get_current_price_calls == ["005930"]
    assert kiwoom.get_master_price_calls == []  # 3단계에서 확정, 4단계 미호출


# ── 기준가 fallback 4단계: get_master_price ──────────────────────────────

def test_prev_close_stage4_falls_back_to_get_master_price():
    """1~3단계 모두 실패 시 get_master_price() 호출"""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=0), current_price=0, master_price=11000)
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=0)

    scanner._on_receive_real_data("005930", "주식체결", "")

    assert internal_prev_close(scanner) == 11000
    assert kiwoom.get_master_price_calls == ["005930"]


# ── 기준가 fallback 5단계: open_price 대체 ───────────────────────────────

def test_prev_close_stage5_falls_back_to_open_price():
    """1~4단계 모두 실패 시 시가(open_price)로 대체"""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=0, open_=14800), current_price=0, master_price=0)
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=0)

    scanner._on_receive_real_data("005930", "주식체결", "")

    assert internal_prev_close(scanner) == 14800  # FID16(시가)


# ── 기준가 fallback 6단계: 현재가로 최종 대체 ────────────────────────────

def test_prev_close_stage6_ultimate_fallback_to_current_price():
    """1~5단계 모두 실패(open_price도 0)면 현재가(price)를 기준가로 임시 설정"""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=0, open_=0), current_price=0, master_price=0)
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=0)

    scanner._on_receive_real_data("005930", "주식체결", "")

    assert internal_prev_close(scanner) == 15000  # price 그대로


def test_prev_close_stage6_means_master_last_price_never_called():
    """[발견] 6단계(price 대체)에서 prev_close가 항상 >0(price>0 보장됨, 그 전에
    price<=0이면 이미 함수가 반환됨)이 되므로, 코드상 그 뒤에 있는 get_master_last_price()
    fallback은 실질적으로 절대 호출되지 않는 죽은 분기다."""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=0, open_=0), current_price=0, master_price=0,
                         master_last_price=99999)
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=0)

    scanner._on_receive_real_data("005930", "주식체결", "")

    assert internal_prev_close(scanner) == 15000  # get_master_last_price(99999)가 아니라 price(15000)
    assert kiwoom.get_master_last_price_calls == []  # 호출 자체가 안 됨


# ── 호가잔량 5단계 파싱 ───────────────────────────────────────────────────

def test_hoga_updates_total_and_5tier_ask_bid():
    """주식호가잔량 수신 시 총잔량(FID121/125) + 1~5호가 가격/수량이 모두 store에 반영된다"""
    fid_map = {
        121: 5000, 125: 6000,  # 매도총잔량/매수총잔량
        41: 10100, 43: 10200, 45: 10300, 47: 10400, 49: 10500,  # 매도1~5호가 가격
        61: 100, 63: 200, 65: 300, 67: 400, 69: 500,            # 매도1~5호가 수량
        51: 10000, 53: 9900, 55: 9800, 57: 9700, 59: 9600,      # 매수1~5호가 가격
        71: 150, 73: 250, 75: 350, 77: 450, 79: 550,            # 매수1~5호가 수량
    }
    kiwoom = MockKiwoom(fid_map)
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=10000)

    scanner._on_receive_real_data("005930", "주식호가잔량", "")

    snap = scanner.store.get_snapshot("005930")
    assert snap.total_ask_qty == 5000
    assert snap.total_bid_qty == 6000
    assert snap.ask_prices == [10100, 10200, 10300, 10400, 10500]
    assert snap.ask_qtys == [100, 200, 300, 400, 500]
    assert snap.bid_prices == [10000, 9900, 9800, 9700, 9600]
    assert snap.bid_qtys == [150, 250, 350, 450, 550]


def test_hoga_does_not_touch_current_price():
    """호가잔량 처리는 현재가(price)나 등락률을 갱신하지 않고 호가만 갱신한다"""
    kiwoom = MockKiwoom({121: 100, 125: 200})
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000)

    scanner._on_receive_real_data("005930", "주식호가잔량", "")

    snap = scanner.store.get_snapshot("005930")
    assert snap.current_price == 14000  # 변화 없음


# ── 등락률 직접계산 fallback ──────────────────────────────────────────────

def test_pct_recalculated_when_fid12_is_zero_but_prev_close_known():
    """FID12(등락률)가 0인데 기준가가 확정되면 (price-prev_close)/prev_close*100으로 재계산"""
    kiwoom = MockKiwoom(make_fid_map(price=15000, change_amt=500, pct=0.0))
    scanner = make_scanner(kiwoom)
    seed_store(scanner.store, price=14000, prev_close=10000)

    scanner._on_receive_real_data("005930", "주식체결", "")

    # prev_close = 15000-500 = 14500, pct = (15000-14500)/14500*100 = 3.45
    assert internal_prev_close(scanner) == 14500
    assert round(internal_change_pct(scanner), 2) == 3.45
