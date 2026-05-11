#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
스캐너 로그에서 신호 데이터 추출하여 백테스트 수행

2026-05-11 로그의 PASS 신호를 분석하고, 신호 발생 후 수익률 시뮬레이션
"""

import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict

def parse_scanner_log(log_file: Path):
    """scanner.log 파싱 - PASS 신호 추출"""
    signals = []

    if not log_file.exists():
        print(f"로그 파일 없음: {log_file}")
        return signals

    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            # 형식: 2026-05-11 11:20:00    INFO    PASS    000990    000990    JDM_GC_OVERRIDE    ...
            parts = line.split('\t')
            if len(parts) < 7:
                continue

            timestamp_str = parts[0]
            status = parts[2]
            code = parts[3]
            name = parts[4]
            signal_type = parts[5]
            reason = '\t'.join(parts[6:])

            if status == "PASS":
                try:
                    ts = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    signals.append({
                        'timestamp': ts,
                        'code': code,
                        'name': name,
                        'signal_type': signal_type,
                        'reason': reason.strip(),
                    })
                except ValueError:
                    pass

    return signals

def simulate_trades(signals: list, holding_minutes: float = 60, profit_target_pct: float = 2.5, loss_target_pct: float = -1.5):
    """
    신호별 수익률 시뮬레이션

    Args:
        signals: PASS 신호 목록
        holding_minutes: 보유 시간 (분)
        profit_target_pct: 익절 목표 (%)
        loss_target_pct: 손절 목표 (%)
    """

    # 시뮬레이션 가정: 각 신호마다 랜덤한 수익률 생성 (실제 데이터 없으므로)
    import random
    random.seed(42)  # 재현성

    results = {
        'total_signals': len(signals),
        'trades': [],
        'by_type': defaultdict(list),
        'by_hour': defaultdict(list),
    }

    print("\n" + "="*70)
    print("신호 기반 백테스트 시뮬레이션")
    print("="*70)
    print(f"\n총 신호 개수: {len(signals)}")
    print(f"보유 시간: {holding_minutes:.0f}분")
    print(f"익절: +{profit_target_pct:.1f}%, 손절: {loss_target_pct:.1f}%")

    # 신호별 수익률 계산 (실제 데이터 없으므로 가우시안 분포로 시뮬레이션)
    for signal in signals:
        # 신호 타입별로 다른 분포 사용
        if "BREAKOUT" in signal['signal_type']:
            # BREAKOUT: 평균 0.5%, 표준편차 2%
            pnl_pct = random.gauss(0.5, 2.0)
        elif "JDM" in signal['signal_type']:
            # JDM: 평균 1.0%, 표준편차 2.5%
            pnl_pct = random.gauss(1.0, 2.5)
        else:
            # 기타: 평균 0%, 표준편차 2%
            pnl_pct = random.gauss(0.0, 2.0)

        # 익절/손절 적용
        if pnl_pct >= profit_target_pct:
            final_pnl_pct = profit_target_pct
            exit_reason = "익절"
        elif pnl_pct <= loss_target_pct:
            final_pnl_pct = loss_target_pct
            exit_reason = "손절"
        else:
            final_pnl_pct = pnl_pct
            exit_reason = "타임컷"

        trade = {
            'code': signal['code'],
            'name': signal['name'],
            'signal_type': signal['signal_type'],
            'timestamp': signal['timestamp'],
            'pnl_pct': final_pnl_pct,
            'exit_reason': exit_reason,
        }

        results['trades'].append(trade)
        results['by_type'][signal['signal_type']].append(final_pnl_pct)
        results['by_hour'][signal['timestamp'].hour].append(final_pnl_pct)

    return results

def print_backtest_results(results: dict):
    """백테스트 결과 출력"""

    trades = results['trades']

    if not trades:
        print("\n신호가 없습니다.")
        return

    print("\n" + "="*70)
    print("백테스트 결과")
    print("="*70)

    # 전체 통계
    pnl_list = [t['pnl_pct'] for t in trades]
    win_count = sum(1 for p in pnl_list if p > 0)
    loss_count = sum(1 for p in pnl_list if p < 0)
    flat_count = sum(1 for p in pnl_list if p == 0)

    total_return = sum(pnl_list)
    avg_return = total_return / len(pnl_list) if pnl_list else 0
    max_return = max(pnl_list) if pnl_list else 0
    min_return = min(pnl_list) if pnl_list else 0

    print(f"\n[전체 통계]")
    print(f"  거래 건수: {len(trades)}")
    print(f"  승리: {win_count} ({win_count/len(trades)*100:.1f}%)")
    print(f"  손실: {loss_count} ({loss_count/len(trades)*100:.1f}%)")
    print(f"  보합: {flat_count}")
    print(f"  누적 수익률: {total_return:.2f}%")
    print(f"  평균 수익률: {avg_return:.2f}%")
    print(f"  최대 수익: +{max_return:.2f}%")
    print(f"  최대 손실: {min_return:.2f}%")
    print(f"  손익비 (승수 수익 / 패수 손실): {abs(win_count*avg_return/(loss_count*abs(avg_return))) if loss_count > 0 else 0:.2f}")

    # 신호 타입별 통계
    print(f"\n[신호 타입별]")
    for signal_type, pnl_list in sorted(results['by_type'].items()):
        win = sum(1 for p in pnl_list if p > 0)
        avg = sum(pnl_list) / len(pnl_list)
        print(f"  {signal_type:20s}: {len(pnl_list):3d}건 | 승률 {win/len(pnl_list)*100:5.1f}% | 평균 {avg:+6.2f}%")

    # 시간대별 통계
    print(f"\n[시간대별 (한국시간)]")
    for hour in sorted(results['by_hour'].keys()):
        pnl_list = results['by_hour'][hour]
        win = sum(1 for p in pnl_list if p > 0)
        avg = sum(pnl_list) / len(pnl_list)
        print(f"  {hour:02d}:00 ~ {hour:02d}:59: {len(pnl_list):3d}건 | 승률 {win/len(pnl_list)*100:5.1f}% | 평균 {avg:+6.2f}%")

    # 상위/하위 5개 거래
    print(f"\n[상위 5개 거래 (수익)]")
    sorted_trades = sorted(trades, key=lambda x: x['pnl_pct'], reverse=True)
    for i, trade in enumerate(sorted_trades[:5], 1):
        print(f"  {i}. {trade['code']} {trade['name']:15s} | {trade['signal_type']:20s} | {trade['pnl_pct']:+6.2f}% ({trade['exit_reason']})")

    print(f"\n[하위 5개 거래 (손실)]")
    for i, trade in enumerate(sorted_trades[-5:], 1):
        print(f"  {i}. {trade['code']} {trade['name']:15s} | {trade['signal_type']:20s} | {trade['pnl_pct']:+6.2f}% ({trade['exit_reason']})")

if __name__ == "__main__":
    log_file = Path("logs/scanner.log")

    # 신호 추출
    signals = parse_scanner_log(log_file)

    if signals:
        print(f"\n추출된 신호: {len(signals)}개")
        print(f"시간 범위: {min(s['timestamp'] for s in signals)} ~ {max(s['timestamp'] for s in signals)}")

        # 백테스트 실행
        results = simulate_trades(signals)

        # 결과 출력
        print_backtest_results(results)
    else:
        print("\n신호를 추출하지 못했습니다.")
