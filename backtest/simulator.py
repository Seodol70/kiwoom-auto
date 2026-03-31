"""
백테스트 실행 예제 — 골든크로스 / 장동민 전략 비교

실행: python -m backtest.simulator
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.report import build_chart, print_metrics, save_report


# ---------------------------------------------------------------------------
# 샘플 데이터 생성
# ---------------------------------------------------------------------------

def generate_stock_data(days: int = 500, start_price: float = 50_000) -> pd.DataFrame:
    """랜덤 워크 기반 OHLCV 데이터 생성 (거래일 기준)"""
    rng = np.random.default_rng(seed=42)
    log_returns = rng.normal(loc=0.0003, scale=0.015, size=days)
    closes = start_price * np.exp(np.cumsum(log_returns))

    noise = lambda scale: rng.uniform(1 - scale, 1 + scale, days)
    opens  = closes * noise(0.005)
    highs  = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.01, days))
    lows   = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.01, days))
    vols   = rng.integers(100_000, 1_000_000, days)

    index = pd.bdate_range(start="2023-01-01", periods=days)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=index,
    )


# ---------------------------------------------------------------------------
# 전략 함수 — 골든크로스
# ---------------------------------------------------------------------------

def golden_cross_strategy(closes: list[float], idx: int) -> str:
    """MA5 > MA20 골든크로스 매수 / 데드크로스 매도"""
    if idx < 20:
        return "HOLD"
    window = closes[: idx + 1]
    ma5  = sum(window[-5:])  / 5
    ma20 = sum(window[-20:]) / 20
    prev_ma5  = sum(window[-6:-1]) / 5
    prev_ma20 = sum(window[-21:-1]) / 20

    if prev_ma5 <= prev_ma20 and ma5 > ma20:
        return "BUY"
    if prev_ma5 >= prev_ma20 and ma5 < ma20:
        return "SELL"
    return "HOLD"


# ---------------------------------------------------------------------------
# 전략 함수 — 장동민 (MA + RSI + 볼린저 밴드)
# ---------------------------------------------------------------------------

def jang_dong_min_strategy(closes: list[float], idx: int) -> str:
    """
    최적화된 파라미터를 사용한 장동민 전략.
    MA골든크로스 + RSI 범위 진입, MA데드크로스 청산.
    """
    from strategy.jang_dong_min import StrategyConfig, calc_ma, calc_rsi

    # 백테스트 최적 파라미터 적용
    cfg = StrategyConfig(
        ma_short=7,
        ma_long=15,
        rsi_period=14,
        rsi_oversold=35.0,
        rsi_overbought=70.0,
    )
    window = closes[: idx + 1]

    if len(window) < cfg.ma_long + 1:
        return "HOLD"

    ma_short = calc_ma(window, cfg.ma_short)
    ma_long  = calc_ma(window, cfg.ma_long)
    rsi      = calc_rsi(window, cfg.rsi_period)

    if any(v is None for v in [ma_short, ma_long, rsi]):
        return "HOLD"

    prev_window  = window[:-1]
    prev_short   = calc_ma(prev_window, cfg.ma_short)
    prev_long    = calc_ma(prev_window, cfg.ma_long)

    if prev_short is None or prev_long is None:
        return "HOLD"

    golden_cross = prev_short <= prev_long and ma_short > ma_long
    dead_cross   = prev_short >= prev_long and ma_short < ma_long
    rsi_ok       = cfg.rsi_oversold < rsi < cfg.rsi_overbought

    if golden_cross and rsi_ok:
        return "BUY"
    if dead_cross:
        return "SELL"
    return "HOLD"


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def run_comparison() -> None:
    data = generate_stock_data(days=500)

    # 백테스트 최적 파라미터: 손절 -1.5%, 익절 4.0%
    engine = BacktestEngine(
        initial_capital=10_000_000,
        stop_loss_pct=-1.5,      # 최적화됨
        take_profit_pct=4.0,     # 최적화됨
    )

    strategies = {
        "골든크로스": golden_cross_strategy,
        "장동민":     jang_dong_min_strategy,
    }

    for name, fn in strategies.items():
        print(f"\n{'='*44}")
        print(f"  전략: {name}")
        result = engine.run(data, fn)
        print_metrics(result)

        save_report(
            result,
            price_data=data,
            path=f"backtest_report_{name}.html",
            title=f"백테스트 — {name} 전략",
        )


if __name__ == "__main__":
    run_comparison()
