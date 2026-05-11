#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
2026-05-10 과거 데이터로 신호 테스트
백테스트 엔진을 사용해서 지표/신호/필터 동작 확인
"""

import sys
from datetime import datetime, date
from pathlib import Path

# 프로젝트 루트 추가
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.simulator import BacktestSimulator
from scanner.config import SmartScannerConfig
from strategy.jang_dong_min import StrategyConfig

def main():
    print("=" * 60)
    print("신호 테스트: 2026-05-10 과거 데이터 백테스트")
    print("=" * 60)

    # 설정 로드
    scan_cfg = SmartScannerConfig()
    strat_cfg = StrategyConfig()

    print(f"\n📊 설정:")
    print(f"  - JDM_LIQUIDITY: 거래량 >= {getattr(scan_cfg, 'min_daily_volume', 100_000):,}주")
    print(f"  - CHEJAN_MAX: {scan_cfg.breakout_chejan_max:.0f}% (AFTERNOON), {scan_cfg.breakout_chejan_max_morning:.0f}% (MORNING)")
    print(f"  - 진입 시간: {scan_cfg.entry_start_time} ~ {scan_cfg.entry_end_time}")

    # 백테스트 실행
    print(f"\n⏳ 백테스트 시뮬레이션 중...")

    simulator = BacktestSimulator(
        start_date=date(2026, 5, 10),  # 어제
        end_date=date(2026, 5, 10),    # 어제
        initial_capital=10_000_000,
        scan_config=scan_cfg,
        strategy_config=strat_cfg,
    )

    results = simulator.run()

    print(f"\n✅ 백테스트 완료")
    print(f"  - 신호 감지: {results.get('signals', 0)}개")
    print(f"  - 진입 신호: {results.get('entries', 0)}개")
    print(f"  - 거래: {results.get('trades', 0)}건")
    print(f"  - 수익률: {results.get('return_pct', 0):.2f}%")

    return results

if __name__ == "__main__":
    main()
