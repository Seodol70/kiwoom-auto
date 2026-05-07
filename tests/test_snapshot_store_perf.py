import time
import pandas as pd
from scanner.snapshot_store import SnapshotStore
from scanner.models import InternalStockState

def benchmark_snapshot_store():
    store = SnapshotStore()
    
    # 100종목 초기화
    codes = [f"{i:06d}" for i in range(1, 101)]
    initial_rows = []
    for code in codes:
        initial_rows.append({
            "code": code,
            "name": f"종목_{code}",
            "current_price": 10000,
            "open_price": 10000,
            "high_price": 10000,
            "low_price": 10000,
            "volume": 0,
            "trade_amount": 0,
            "prev_close": 10000,
            "change_pct": 0.0
        })
    
    store.bulk_update(initial_rows)
    print(f"--- Benchmark Start: 100 Stocks ---")

    # 1. update_price 성능 테스트 (10,000번 호출)
    start_t = time.monotonic()
    for _ in range(100):
        for code in codes:
            store.update_price(
                code=code,
                current_price=10100,
                high_price=10200,
                low_price=9900,
                open_price=10000,
                volume=1000,
                cum_vol=100000,
                cum_amt=1000000,
                prev_close=10000
            )
    end_t = time.monotonic()
    print(f"update_price (10,000 calls): {end_t - start_t:.4f}s")

    # 2. sync 성능 테스트 (100번 호출)
    start_t = time.monotonic()
    for _ in range(100):
        store.sync()
    end_t = time.monotonic()
    print(f"sync (100 calls): {end_t - start_t:.4f}s")

    # 3. get_snapshot 성능 테스트 (1,000번 호출)
    # 분봉 데이터 가공 (RSI 계산 유도)
    for code in codes:
        st = store.get_internal_state(code)
        st.mins = [float(10000 + i*10) for i in range(20)]

    start_t = time.monotonic()
    for _ in range(10):
        for code in codes:
            store.get_snapshot(code)
    end_t = time.monotonic()
    print(f"get_snapshot (1,000 calls): {end_t - start_t:.4f}s")

if __name__ == "__main__":
    benchmark_snapshot_store()
