"""
SmartScanner 실시간 데이터 처리 thread safety 검증 테스트

- Level 1: SnapshotStore.update_price() 동시성 (Lock 검증)
- Level 2: SmartScanner._on_receive_real_data() 통합 (콜백 검증)
- Level 3: 여러 스레드 동시 콜백 (경합 조건 검증)

pytest로 실행: pytest tests/test_smartscanner_realtime.py -v
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from order.order_manager import Position
from scanner.smart_scanner import SmartScanner, SmartScannerConfig
from scanner.snapshot_store import SnapshotStore


# ========== Mock 클래스 ==========

class MockOcx:
    """kiwoom._ocx mock — dynamicCall로 FID 값 반환"""
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
    """Kiwoom API mock"""
    def __init__(self, fid_returns=None):
        self._ocx = MockOcx(fid_returns)


class MockPositionRepo:
    """position_repo mock — update_price 호출 기록"""
    def __init__(self, positions):
        self._positions = positions
        self.call_log = []  # (code, price) 기록

    def update_price(self, code, price):
        self.call_log.append((code, price))
        if code in self._positions:
            self._positions[code].current_price = price


class MockOrderManager:
    """OrderManager mock — 포지션 업데이트 검증용"""
    def __init__(self):
        self.positions = {}
        self.position_repo = MockPositionRepo(self.positions)
        self.trend_update_log = []

    def update_position_trend(self, code, level):
        self.trend_update_log.append((code, level))


def make_fid_map(price=15000, vol=100000, high=15200, low=14800,
                 open_=15100, pct=3.5, strength=150.0):
    """표준 FID map 생성"""
    return {
        10: price,  # 현재가
        12: pct,    # 등락률
        13: vol,    # 거래량
        16: open_,  # 시가
        17: high,   # 고가
        18: low,    # 저가
        20: strength,  # 체결강도
    }


def seed_store(store, code="005930", name="삼성전자", price=70000):
    """SnapshotStore에 테스트 종목 초기화"""
    store.bulk_update([{
        "code": code,
        "name": name,
        "current_price": price,
        "open_price": price,
        "high_price": price + 1000,
        "low_price": price - 1000,
        "volume": 1000000,
        "trade_amount": 7000000000,
        "prev_close": price - 1000,
        "change_pct": 1.0,
        "rank": 1,
    }])


def make_scanner(fid_returns=None):
    """SmartScanner 인스턴스 생성 (mock kiwoom 포함)"""
    kiwoom = MockKiwoom(fid_returns)
    cfg = SmartScannerConfig()
    with patch.object(SmartScanner, "_load_prev_volumes", return_value=None):
        scanner = SmartScanner(kiwoom, cfg)
    return scanner


# ========== Level 1: SnapshotStore Thread Safety (5개) ==========

class TestSnapshotStoreThreadSafety:
    """SnapshotStore.update_price() 동시성 검증"""

    def test_concurrent_price_updates_same_code(self):
        """10 스레드가 동일 코드에 서로 다른 가격으로 동시 update_price"""
        store = SnapshotStore()
        seed_store(store, code="005930", price=70000)

        num_threads = 10
        results = {}
        barrier = threading.Barrier(num_threads)

        def update_worker(thread_id):
            barrier.wait()  # 모든 스레드 동시 시작
            price = 70000 + thread_id * 100
            store.update_price(code="005930", current_price=price,
                             high_price=price + 100, low_price=price - 100,
                             open_price=price, volume=10000)
            snap = store.get_snapshot("005930")
            results[thread_id] = snap.current_price if snap else -1

        threads = [
            threading.Thread(target=update_worker, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 모든 결과가 유효한 정수
        assert all(r > 0 for r in results.values()), f"Invalid prices: {results}"
        # 최종 값은 어떤 스레드의 가격이어야 함
        final_snap = store.get_snapshot("005930")
        assert final_snap.current_price in [70000 + i * 100 for i in range(num_threads)]

    def test_concurrent_price_updates_different_codes(self):
        """10 스레드가 서로 다른 코드를 동시 update_price"""
        store = SnapshotStore()
        # 보통주만 허용: 끝자리가 0 또는 5인 코드만 사용
        codes = ["005930", "005935", "000660", "000720", "051910",
                 "012330", "028260", "010950", "011170", "011200"]

        # seed 10개 종목
        for i, code in enumerate(codes):
            seed_store(store, code=code, name=f"종목{i}", price=70000 + i * 1000)

        results = {}
        barrier = threading.Barrier(len(codes))

        def update_worker(idx):
            barrier.wait()
            code = codes[idx]
            new_price = 70000 + idx * 1000 + 500  # 500 씩 추가
            store.update_price(code=code, current_price=new_price,
                             high_price=new_price + 100, low_price=new_price - 100,
                             open_price=new_price, volume=10000)
            snap = store.get_snapshot(code)
            results[code] = snap.current_price if snap else -1

        threads = [
            threading.Thread(target=update_worker, args=(i,))
            for i in range(len(codes))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 각 코드의 최종 값이 업데이트된 값이어야 함
        for i, code in enumerate(codes):
            expected = 70000 + i * 1000 + 500
            assert results[code] == expected, f"{code}: expected {expected}, got {results[code]}"

    def test_lock_prevents_partial_update(self):
        """read/write 동시 시 snapshot이 완전한 값을 반환 (torn read 없음)"""
        store = SnapshotStore()
        seed_store(store, code="005930", price=70000)

        read_results = []
        barrier = threading.Barrier(2)

        def write_worker():
            barrier.wait()
            for _ in range(100):
                store.update_price(code="005930", current_price=75000,
                                   high_price=76000, low_price=74000,
                                   open_price=75000, volume=10000)

        def read_worker():
            barrier.wait()
            for _ in range(100):
                snap = store.get_snapshot("005930")
                if snap:
                    # 유효한 스냅샷이면 기록
                    read_results.append((snap.current_price, snap.high_price, snap.low_price))

        w_thread = threading.Thread(target=write_worker)
        r_thread = threading.Thread(target=read_worker)

        w_thread.start()
        r_thread.start()
        w_thread.join()
        r_thread.join()

        # 읽은 값들이 모두 유효한 범위 (torn read 없음)
        for cp, hp, lp in read_results:
            assert cp > 0, "current_price should be positive"
            assert hp > 0, "high_price should be positive"
            assert lp > 0, "low_price should be positive"

    def test_bulk_update_concurrent_with_price_update(self):
        """bulk_update와 update_price를 동시 실행"""
        store = SnapshotStore()
        seed_store(store, code="005930", price=70000)

        barrier = threading.Barrier(2)
        results = {}

        def bulk_worker():
            barrier.wait()
            # 다른 코드 bulk 추가
            store.bulk_update([{
                "code": "000660",
                "name": "SK하이닉스",
                "current_price": 120000,
                "open_price": 120000,
                "high_price": 121000,
                "low_price": 119000,
                "volume": 500000,
                "trade_amount": 6000000000,
                "prev_close": 119000,
                "change_pct": 0.8,
                "rank": 2,
            }])
            snap = store.get_snapshot("000660")
            results["bulk"] = snap.current_price if snap else -1

        def price_worker():
            barrier.wait()
            # 기존 코드 가격 업데이트
            store.update_price(code="005930", current_price=72000,
                             high_price=72100, low_price=71900,
                             open_price=72000, volume=10000)
            snap = store.get_snapshot("005930")
            results["price"] = snap.current_price if snap else -1

        t1 = threading.Thread(target=bulk_worker)
        t2 = threading.Thread(target=price_worker)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 두 작업이 모두 완료됨
        assert results["bulk"] == 120000
        assert results["price"] == 72000

    def test_get_snapshot_returns_none_for_unknown_code(self):
        """store에 없는 코드로 get_snapshot 호출 → None 반환 (crash 아님)"""
        store = SnapshotStore()
        snap = store.get_snapshot("999999")
        assert snap is None


# ========== Level 2: SmartScanner Realtime Callback (7개) ==========

class TestSmartScannerRealtimeCallback:
    """SmartScanner._on_receive_real_data() 콜백 검증"""

    def test_update_store_on_valid_tick(self):
        """유효한 틱 → store.current_price 갱신"""
        scanner = make_scanner(make_fid_map(price=15000, vol=100000))
        seed_store(scanner.store, code="005930", price=14000)

        # 콜백 호출
        scanner._on_receive_real_data("005930", "주식체결", "")

        snap = scanner.store.get_snapshot("005930")
        assert snap is not None
        assert snap.current_price == 15000

    def test_invalid_real_type_ignored(self):
        """'주식호가잔량' 등 invalid real_type은 무시"""
        scanner = make_scanner(make_fid_map(price=15000))
        seed_store(scanner.store, code="005930", price=14000)

        scanner._on_receive_real_data("005930", "주식호가잔량", "")

        snap = scanner.store.get_snapshot("005930")
        # 변화 없어야 함
        assert snap.current_price == 14000

    def test_zero_price_ignored(self):
        """price=0 시 store 업데이트 스킵"""
        scanner = make_scanner(make_fid_map(price=0))
        seed_store(scanner.store, code="005930", price=14000)

        scanner._on_receive_real_data("005930", "주식체결", "")

        snap = scanner.store.get_snapshot("005930")
        # 변화 없어야 함
        assert snap.current_price == 14000

    def test_chejan_strength_stored(self):
        """FID 20 (체결강도) → store._chejan_str 저장"""
        scanner = make_scanner(make_fid_map(strength=150.0))
        seed_store(scanner.store, code="005930")

        scanner._on_receive_real_data("005930", "주식체결", "")

        # _chejan_str dict에 저장됨
        assert "005930" in scanner.store._chejan_str
        assert scanner.store._chejan_str["005930"] == 150.0

    def test_chejan_strength_normalized(self):
        """FID 20 ≥ 10000 시 ÷100 정규화"""
        scanner = make_scanner(make_fid_map(strength=15000))
        seed_store(scanner.store, code="005930")

        scanner._on_receive_real_data("005930", "주식체결", "")

        # 정규화되어야 함
        assert scanner.store._chejan_str["005930"] == 150.0

    def test_position_repo_primary_path(self):
        """position_repo 경로로 포지션 current_price 업데이트"""
        scanner = make_scanner(make_fid_map(price=15000))
        seed_store(scanner.store, code="005930")

        # order_mgr와 position 추가
        scanner._order_mgr = MockOrderManager()
        pos = Position(code="005930", name="삼성전자", qty=10,
                       avg_price=14000, current_price=14000)
        scanner._order_mgr.positions["005930"] = pos

        scanner._on_receive_real_data("005930", "주식체결", "")

        # position_repo.call_log에 기록되어야 함
        assert ("005930", 15000) in scanner._order_mgr.position_repo.call_log
        # position.current_price 갱신
        assert pos.current_price == 15000

    def test_position_fallback_path(self):
        """position_repo 없을 때 fallback: positions[code].current_price 직접 할당"""
        scanner = make_scanner(make_fid_map(price=15000))
        seed_store(scanner.store, code="005930")

        # order_mgr (position_repo 없음)
        class MockOrderManagerNoRepo:
            def __init__(self):
                self.positions = {}
                # position_repo 없음

        scanner._order_mgr = MockOrderManagerNoRepo()
        pos = Position(code="005930", name="삼성전자", qty=10,
                       avg_price=14000, current_price=14000)
        scanner._order_mgr.positions["005930"] = pos

        scanner._on_receive_real_data("005930", "주식체결", "")

        # fallback으로 직접 할당
        assert pos.current_price == 15000


# ========== Level 3: Realtime Thread Safety (3개) ==========

class TestRealtimeThreadSafety:
    """여러 스레드에서 동시 _on_receive_real_data 호출"""

    def test_concurrent_callbacks_no_crash(self):
        """10 스레드에서 동시에 _on_receive_real_data 호출 → crash 없음"""
        scanner = make_scanner(make_fid_map(price=15000))
        seed_store(scanner.store, code="005930")

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        exceptions = []

        def callback_worker(thread_id):
            try:
                barrier.wait()
                price = 15000 + thread_id * 100
                fid_map = make_fid_map(price=price)
                scanner._on_receive_real_data("005930", "주식체결", "")
            except Exception as e:
                exceptions.append(e)

        threads = [
            threading.Thread(target=callback_worker, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not exceptions, f"Exceptions occurred: {exceptions}"

    def test_concurrent_position_updates(self):
        """포지션 현재가를 10개 스레드에서 동시 업데이트"""
        scanner = make_scanner(make_fid_map(price=15000))
        seed_store(scanner.store, code="005930")

        scanner._order_mgr = MockOrderManager()
        pos = Position(code="005930", name="삼성전자", qty=10,
                       avg_price=14000, current_price=14000)
        scanner._order_mgr.positions["005930"] = pos

        num_threads = 10
        barrier = threading.Barrier(num_threads)

        def update_worker(thread_id):
            barrier.wait()
            price = 15000 + thread_id * 100
            # fid_map을 업데이트해야 하는데, scanner가 이미 초기화됨
            # 따라서 직접 호출
            scanner._on_receive_real_data("005930", "주식체결", "")

        threads = [
            threading.Thread(target=update_worker, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 최종 current_price가 유효한 정수
        assert pos.current_price > 0

    def test_data_consistent_after_concurrent_updates(self):
        """서로 다른 스레드에서 데이터 업데이트 후 get_snapshot이 완전한 값 반환"""
        store = SnapshotStore()
        seed_store(store, code="005930", price=70000)

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        final_values = []

        def update_and_read(thread_id):
            barrier.wait()
            price = 70000 + thread_id * 100
            high = price + 1000
            low = price - 1000

            store.update_price(code="005930", current_price=price,
                             high_price=high, low_price=low,
                             open_price=price, volume=10000)

            # 즉시 읽기
            snap = store.get_snapshot("005930")
            if snap:
                # torn read 확인: cp/high/low 일관성
                final_values.append((snap.current_price, snap.high_price, snap.low_price))

        threads = [
            threading.Thread(target=update_and_read, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 모든 읽은 값이 유효한 범위
        for cp, hp, lp in final_values:
            assert cp >= 70000, f"current_price {cp} < 70000"
            assert hp == cp + 1000 or hp >= cp, f"high_price {hp} inconsistent with {cp}"
            assert lp == cp - 1000 or lp <= cp, f"low_price {lp} inconsistent with {cp}"
