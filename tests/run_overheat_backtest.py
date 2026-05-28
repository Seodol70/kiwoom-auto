"""
run_overheat_backtest.py — Phase 2 백테스트 실행 스크립트

ticks_YYYYMMDD.csv + fills_YYYYMMDD.jsonl 를 사용해
OverheatPullbackEvaluator의 실제 승률·적중률을 측정합니다.

실행:
  cd d:/prj/kiwoom-auto
  python tests/run_overheat_backtest.py
"""

import sys
import os
import json
import csv
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.evaluators.overheat_pullback import OverheatPullbackEvaluator
from scanner.evaluators.overheat_pullback_backtest import OverheatPullbackBacktester

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
TICKS_DIR = os.path.join(LOGS_DIR, "ticks")

# ── 분석 대상 날짜 ──────────────────────────────────────────────────────────────
BACKTEST_DATES = [
    "20260513",  # 31건 거래, 승률 23%
    "20260518",  # 62건 거래, 승률 21%
    "20260521",  # 35건 거래, 승률 31%
    "20260527",  # 12건 거래, 승률 17%
]

# ── 거래대금 임계치 (중소형주 위주이므로 50억 → 5억으로 완화) ───────────────────
# 실제 데이터 확인 후 튜닝 예정
TRADING_VALUE_THRESHOLD = 500_000_000  # 5억원

# ── 파라미터 그리드 (튜닝용) ────────────────────────────────────────────────────
PARAM_GRID = [
    {"level_3_threshold": 1.5, "volume_surge_mult": 2.0, "lookback_minutes": 10},
    {"level_3_threshold": 1.3, "volume_surge_mult": 1.5, "lookback_minutes": 10},
    {"level_3_threshold": 1.5, "volume_surge_mult": 1.5, "lookback_minutes": 15},
]


def load_fills(date_str: str) -> List[Dict]:
    """fills_YYYYMMDD.jsonl 로드."""
    path = os.path.join(LOGS_DIR, f"fills_{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line.strip()))
            except Exception:
                pass
    return rows


def load_ticks(date_str: str) -> Dict[str, List[Dict]]:
    """ticks_YYYYMMDD.csv 를 종목코드별로 인덱싱.

    Returns:
        {code: [{'ts': datetime, 'price': int, 'volume': int}, ...]}
    """
    path = os.path.join(TICKS_DIR, f"ticks_{date_str}.csv")
    if not os.path.exists(path):
        print(f"  ⚠ ticks 파일 없음: {path}")
        return {}

    ticks_by_code: Dict[str, List] = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row["code"].strip()
            try:
                ts = datetime.strptime(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {row['timestamp']}",
                                       "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                try:
                    ts = datetime.strptime(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {row['timestamp']}",
                                           "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
            ticks_by_code[code].append({
                "ts": ts,
                "price": int(float(row.get("price", 0))),
                "volume": int(float(row.get("volume", 0))),
            })
    return dict(ticks_by_code)


def aggregate_to_1min(ticks: List[Dict]) -> List[Dict]:
    """틱 데이터를 1분봉으로 집계.

    Returns:
        [{'close': int, 'high': int, 'low': int, 'trading_value': float, 'ts': datetime}, ...]
    """
    if not ticks:
        return []

    candles = {}
    for tick in ticks:
        ts = tick["ts"]
        minute_key = ts.replace(second=0, microsecond=0)
        price = tick["price"]
        volume = tick["volume"]

        if price <= 0:
            continue

        if minute_key not in candles:
            candles[minute_key] = {
                "ts": minute_key,
                "open": price, "high": price, "low": price, "close": price,
                "volume": 0, "trading_value": 0.0,
            }
        c = candles[minute_key]
        c["high"] = max(c["high"], price)
        c["low"] = min(c["low"], price)
        c["close"] = price
        c["volume"] += volume
        c["trading_value"] += price * volume

    return sorted(candles.values(), key=lambda x: x["ts"])


def estimate_daily_info(candles: List[Dict]) -> Dict[str, Any]:
    """당일 1분봉 데이터로 daily_info를 근사 추정.

    실제 시스템에서는 전일 일봉 데이터를 사용해야 하지만,
    백테스트에서는 당일 1분봉 20개 기준 MA20 기울기로 근사.
    """
    if len(candles) < 23:
        return {"ma20_slope_up": True}  # 데이터 부족 시 통과 처리

    closes = [c["close"] for c in candles]
    # MA20 현재값 vs 3분 전 값 비교
    ma20_now = sum(closes[-20:]) / 20
    ma20_prev = sum(closes[-23:-3]) / 20
    return {"ma20_slope_up": ma20_now >= ma20_prev}


def run_backtest_for_date(
    date_str: str,
    evaluator: OverheatPullbackEvaluator,
    backtester: OverheatPullbackBacktester,
) -> int:
    """단일 날짜 백테스트 실행. 처리한 거래 수를 반환."""
    print(f"\n📅 {date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} 분석 중...")

    fills = load_fills(date_str)
    if not fills:
        print(f"  fills 없음 — 스킵")
        return 0

    ticks_by_code = load_ticks(date_str)
    if not ticks_by_code:
        print(f"  ticks 없음 — 스킵")
        return 0

    # 종목별 fills 집계 (avg_price, peak_price 계산)
    code_fills: Dict[str, Dict] = {}
    for fill in fills:
        code = fill["code"]
        if code not in code_fills:
            code_fills[code] = {
                "name": fill["name"],
                "avg_price": fill["avg_price"],
                "realized": fill.get("realized", 0),
                "sell_prices": [],
            }
        code_fills[code]["sell_prices"].append(fill["sell_price"])

    processed = 0
    for code, fill_info in code_fills.items():
        ticks = ticks_by_code.get(code, [])
        if not ticks:
            continue

        candles = aggregate_to_1min(ticks)
        if len(candles) < 25:
            continue

        # 진입 전 1분봉으로 평가 (진입 시점 기준 앞의 데이터만 사용)
        # 진입 시각을 알기 위해 fill의 ts에서 역산 (fills는 청산 기록이므로)
        # 진입은 대략 장 시작 후 몇 분 이내로 가정 → 전체 봉 사용
        avg_price = fill_info["avg_price"]
        sell_prices = fill_info["sell_prices"]
        peak_price = max(sell_prices) if sell_prices else avg_price
        name = fill_info["name"]

        daily_info = estimate_daily_info(candles)

        # 진입 직전 시점 (봉 전체 사용 — 백테스트 근사)
        result = backtester.analyze_historical_trade(
            code=code,
            name=name,
            trade_date=f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
            entry_price=float(avg_price),
            peak_price=float(peak_price),
            candle_history=candles,
            daily_info=daily_info,
        )

        status = "✅ 신호" if result["signal_matched"] else "  ·"
        profit_pct = result["profit_pct"]
        profit_sym = "+" if profit_pct > 0 else ""
        print(f"  {status} {code} {name[:10]:10s} | "
              f"매입{avg_price:,}→고점{peak_price:,} | "
              f"손익 {profit_sym}{profit_pct:.2f}% | "
              f"{result['signal_reason']}")
        processed += 1

    return processed


def run_parameter_grid_search(
    date_str: str,
    ticks_by_code: Dict,
    fills: List,
) -> List[Dict]:
    """파라미터 그리드 서치 실행."""
    results = []

    # 종목별 캔들 미리 계산
    candle_map = {}
    code_fills: Dict[str, Dict] = {}
    for fill in fills:
        code = fill["code"]
        if code not in code_fills:
            code_fills[code] = {
                "name": fill["name"],
                "avg_price": fill["avg_price"],
                "sell_prices": [],
            }
        code_fills[code]["sell_prices"].append(fill["sell_price"])

    for code in code_fills:
        ticks = ticks_by_code.get(code, [])
        if ticks:
            candles = aggregate_to_1min(ticks)
            if len(candles) >= 25:
                candle_map[code] = candles

    for params in PARAM_GRID:
        # 파라미터별 evaluator 생성 (설정 주입)
        class TempConfig:
            pass
        cfg = TempConfig()
        cfg.overheat_min_trading_value_5m_avg = TRADING_VALUE_THRESHOLD
        cfg.overheat_level_3_threshold = params["level_3_threshold"]
        cfg.overheat_volume_surge_mult = params["volume_surge_mult"]
        cfg.overheat_lookback_minutes = params["lookback_minutes"]

        evaluator = OverheatPullbackEvaluator(cfg)
        bt = OverheatPullbackBacktester(evaluator)

        for code, fill_info in code_fills.items():
            candles = candle_map.get(code)
            if not candles:
                continue
            daily_info = estimate_daily_info(candles)
            avg_price = fill_info["avg_price"]
            peak_price = max(fill_info["sell_prices"]) if fill_info["sell_prices"] else avg_price
            bt.analyze_historical_trade(
                code=code, name=fill_info["name"], trade_date=date_str,
                entry_price=float(avg_price), peak_price=float(peak_price),
                candle_history=candles, daily_info=daily_info,
            )

        results.append({
            "params": params,
            "hit_rate": bt.calculate_hit_rate(),
            "win_rate": bt.calculate_win_rate(),
            "avg_profit": bt.calculate_average_profit(),
            "signal_count": sum(1 for r in bt.test_results if r["signal_matched"]),
            "total": len(bt.test_results),
        })

    return results


def main():
    print("=" * 70)
    print("  Phase 2: OverheatPullback 백테스팅")
    print(f"  대상 날짜: {', '.join(BACKTEST_DATES)}")
    print(f"  거래대금 임계치: {TRADING_VALUE_THRESHOLD/1e8:.0f}억원")
    print("=" * 70)

    # 기본 파라미터로 evaluator 생성
    class DefaultCfg:
        overheat_min_trading_value_5m_avg = TRADING_VALUE_THRESHOLD
        overheat_level_3_threshold = 1.5
        overheat_volume_surge_mult = 2.0
        overheat_lookback_minutes = 10

    evaluator = OverheatPullbackEvaluator(DefaultCfg())
    backtester = OverheatPullbackBacktester(evaluator)

    total_processed = 0
    for date_str in BACKTEST_DATES:
        n = run_backtest_for_date(date_str, evaluator, backtester)
        total_processed += n

    if total_processed == 0:
        print("\n⚠ 처리된 거래 없음 — 데이터 경로 확인 필요")
        return

    # 전체 결과 리포트
    print()
    backtester.print_summary_report()

    # 파라미터 튜닝 제안
    suggestions = backtester.suggest_parameter_tuning()
    print("\n[파라미터 튜닝 제안]")
    if suggestions["recommendations"]:
        for rec in suggestions["recommendations"]:
            print(f"  • {rec['param']}: {rec['current']} → {rec['suggested']}")
            print(f"    이유: {rec['reason']}")
    else:
        print("  → 현재 파라미터 적절 (추가 튜닝 불필요)")

    # 그리드 서치 (가장 최신 날짜 기준)
    print("\n[파라미터 그리드 서치 — 최신 날짜 기준]")
    latest_date = BACKTEST_DATES[-1]
    fills = load_fills(latest_date)
    ticks = load_ticks(latest_date)
    if fills and ticks:
        grid_results = run_parameter_grid_search(latest_date, ticks, fills)
        print(f"  {'파라미터':<45} {'Hit%':>6} {'Win%':>6} {'AvgProfit':>10} {'신호':>5}")
        print("  " + "-" * 75)
        for g in sorted(grid_results, key=lambda x: x["win_rate"], reverse=True):
            p = g["params"]
            param_str = f"Lv3≥{p['level_3_threshold']} vol≥{p['volume_surge_mult']}x look{p['lookback_minutes']}m"
            print(f"  {param_str:<45} {g['hit_rate']*100:5.1f}% {g['win_rate']*100:5.1f}% "
                  f"{g['avg_profit']:+9.2f}% {g['signal_count']:>3}/{g['total']}")
    else:
        print(f"  {latest_date} 데이터 없음")

    # JSON 저장
    out_path = os.path.join(LOGS_DIR, "overheat_backtest_result.json")
    backtester.export_results_to_json(out_path)
    print(f"\n✅ 완료! 상세 결과: {out_path}")


if __name__ == "__main__":
    main()
