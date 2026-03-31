"""
파라미터 튜닝 — 여러 조합의 백테스트를 실행하여 최적 파라미터 찾기

실행: python backtest/parameter_tuning.py
"""

from __future__ import annotations

import sys
import os

# 프로젝트 루트 경로 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
from itertools import product

from backtest.engine import BacktestEngine


def generate_stock_data(days: int = 500, start_price: float = 50_000) -> pd.DataFrame:
    """랜덤 워크 기반 OHLCV 데이터 생성"""
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


def jang_dong_min_strategy(
    closes: list[float],
    idx: int,
    ma_short: int = 5,
    ma_long: int = 20,
    rsi_period: int = 14,
    rsi_oversold: float = 30.0,
    rsi_overbought: float = 70.0,
) -> str:
    """조정 가능한 파라미터를 갖는 장동민 전략"""
    from strategy.jang_dong_min import calc_ma, calc_rsi

    window = closes[: idx + 1]

    if len(window) < ma_long + 1:
        return "HOLD"

    ma_short_val = calc_ma(window, ma_short)
    ma_long_val  = calc_ma(window, ma_long)
    rsi          = calc_rsi(window, rsi_period)

    if any(v is None for v in [ma_short_val, ma_long_val, rsi]):
        return "HOLD"

    prev_window = window[:-1]
    prev_short  = calc_ma(prev_window, ma_short)
    prev_long   = calc_ma(prev_window, ma_long)

    if prev_short is None or prev_long is None:
        return "HOLD"

    golden_cross = prev_short <= prev_long and ma_short_val > ma_long_val
    dead_cross   = prev_short >= prev_long and ma_short_val < ma_long_val
    rsi_ok       = rsi_oversold < rsi < rsi_overbought

    if golden_cross and rsi_ok:
        return "BUY"
    if dead_cross:
        return "SELL"
    return "HOLD"


def tune_parameters() -> None:
    """파라미터 그리드 서치 및 결과 비교"""
    print("파라미터 튜닝 시작...")
    data = generate_stock_data(days=500)

    # 테스트할 파라미터 범위
    ma_shorts      = [3, 5, 7]
    ma_longs       = [15, 20, 25]
    rsi_periods    = [12, 14, 16]
    rsi_oversolids = [25, 30, 35]
    stop_losses    = [-1.5, -2.0, -2.5]
    take_profits   = [2.0, 3.0, 4.0]

    results = []

    # 전체 조합 개수
    total_combos = (
        len(ma_shorts) * len(ma_longs) * len(rsi_periods) *
        len(rsi_oversolids) * len(stop_losses) * len(take_profits)
    )
    print(f"총 {total_combos:,}개 조합 테스트 예정\n")

    combo_count = 0
    for (ma_short, ma_long, rsi_period, rsi_oversold,
         stop_loss, take_profit) in product(
        ma_shorts, ma_longs, rsi_periods, rsi_oversolids,
        stop_losses, take_profits
    ):
        combo_count += 1

        if ma_short >= ma_long:
            continue   # 단기 >= 장기 스킵

        engine = BacktestEngine(
            initial_capital=10_000_000,
            stop_loss_pct=stop_loss,
            take_profit_pct=take_profit,
        )

        # 전략 함수 정의 (클로저로 파라미터 캡처)
        def strategy(closes, idx):
            return jang_dong_min_strategy(
                closes, idx,
                ma_short=ma_short, ma_long=ma_long,
                rsi_period=rsi_period, rsi_oversold=rsi_oversold
            )

        result = engine.run(data, strategy)
        metrics = result.metrics

        results.append({
            "ma_short":     ma_short,
            "ma_long":      ma_long,
            "rsi_period":   rsi_period,
            "rsi_oversold": rsi_oversold,
            "stop_loss":    stop_loss,
            "take_profit":  take_profit,
            "total_trades": metrics.get("total_trades", 0),
            "win_rate":     metrics.get("win_rate_pct", 0),
            "total_return": metrics.get("total_return_pct", 0),
            "cagr":         metrics.get("cagr_pct", 0),
            "sharpe":       metrics.get("sharpe_ratio", 0),
            "mdd":          metrics.get("mdd_pct", 0),
        })

        if combo_count % 10 == 0:
            print(f"  진행률: {combo_count}/{total_combos}")

    # 결과 정리
    df = pd.DataFrame(results)

    # 수익률 기준 상위 10개
    print("\n" + "="*80)
    print("상위 10개 파라미터 (총수익률 기준)")
    print("="*80)
    top_10 = df.nlargest(10, "total_return")
    print(top_10.to_string(index=False))

    # 승률 기준 상위 10개
    print("\n" + "="*80)
    print("상위 10개 파라미터 (승률 기준)")
    print("="*80)
    top_wr = df.nlargest(10, "win_rate")
    print(top_wr.to_string(index=False))

    # Sharpe Ratio 기준 상위 10개 (위험조정 수익)
    print("\n" + "="*80)
    print("상위 10개 파라미터 (샤프 지수 기준 / 위험조정 수익)")
    print("="*80)
    top_sharpe = df.nlargest(10, "sharpe")
    print(top_sharpe.to_string(index=False))

    # 최적 파라미터 추천
    best = df.loc[df["total_return"].idxmax()]
    print("\n" + "="*80)
    print("🏆 최적 파라미터 (총수익률 최대)")
    print("="*80)
    print(f"""
MA 단기:         {int(best['ma_short'])}
MA 장기:         {int(best['ma_long'])}
RSI 기간:        {int(best['rsi_period'])}
RSI 과매도:      {best['rsi_oversold']}
손절:            {best['stop_loss']}%
익절:            {best['take_profit']}%

총 거래:         {int(best['total_trades'])}
총수익률:        {best['total_return']:.2f}%
CAGR:            {best['cagr']:.2f}%
승률:            {best['win_rate']:.2f}%
Sharpe:          {best['sharpe']:.2f}
MDD:             {best['mdd']:.2f}%
""")

    # CSV 저장
    csv_path = "backtest_parameter_tuning.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n📊 상세 결과 저장: {csv_path}")


if __name__ == "__main__":
    tune_parameters()
