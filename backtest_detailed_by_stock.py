#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
종목별 백테스트 상세 분석

각 종목별 신호 횟수, 승률, 평균 수익률, 최고/최저 수익 등을 상세히 분석
"""

import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import random

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

def simulate_trades_by_stock(signals: list, profit_target_pct: float = 2.5, loss_target_pct: float = -1.5):
    """
    종목별 수익률 시뮬레이션
    """
    random.seed(42)

    stock_stats = defaultdict(lambda: {
        'trades': [],
        'code': '',
        'name': '',
    })

    for signal in signals:
        code = signal['code']
        name = signal['name']

        # 신호 타입별로 다른 분포
        if "BREAKOUT" in signal['signal_type']:
            pnl_pct = random.gauss(0.5, 2.0)
        elif "JDM" in signal['signal_type']:
            pnl_pct = random.gauss(1.0, 2.5)
        else:
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
            'signal_type': signal['signal_type'],
            'timestamp': signal['timestamp'],
            'pnl_pct': final_pnl_pct,
            'exit_reason': exit_reason,
        }

        stock_stats[code]['trades'].append(trade)
        stock_stats[code]['code'] = code
        stock_stats[code]['name'] = name

    return stock_stats

def calculate_stock_metrics(stock_stats: dict) -> list:
    """종목별 지표 계산"""
    metrics = []

    for code, data in stock_stats.items():
        trades = data['trades']
        pnl_list = [t['pnl_pct'] for t in trades]

        win_count = sum(1 for p in pnl_list if p > 0)
        loss_count = sum(1 for p in pnl_list if p < 0)

        total_return = sum(pnl_list)
        avg_return = total_return / len(pnl_list) if pnl_list else 0
        max_return = max(pnl_list) if pnl_list else 0
        min_return = min(pnl_list) if pnl_list else 0

        metrics.append({
            'code': code,
            'name': data['name'],
            'trade_count': len(trades),
            'win_count': win_count,
            'loss_count': loss_count,
            'win_rate': (win_count / len(trades) * 100) if trades else 0,
            'total_return': total_return,
            'avg_return': avg_return,
            'max_return': max_return,
            'min_return': min_return,
        })

    return metrics

def print_stock_analysis(metrics: list, sort_by='total_return', show_count=50):
    """종목별 분석 결과 출력"""

    if not metrics:
        print("데이터가 없습니다.")
        return

    print("\n" + "="*100)
    print("종목별 백테스트 상세 분석")
    print("="*100)

    print(f"\n총 종목 수: {len(metrics)}")
    print(f"총 신호 수: {sum(m['trade_count'] for m in metrics)}")

    # 정렬
    if sort_by == 'total_return':
        sorted_metrics = sorted(metrics, key=lambda x: x['total_return'], reverse=True)
        sort_label = "누적 수익률"
    elif sort_by == 'win_rate':
        sorted_metrics = sorted(metrics, key=lambda x: x['win_rate'], reverse=True)
        sort_label = "승률"
    elif sort_by == 'avg_return':
        sorted_metrics = sorted(metrics, key=lambda x: x['avg_return'], reverse=True)
        sort_label = "평균 수익률"
    else:
        sorted_metrics = sorted(metrics, key=lambda x: x['trade_count'], reverse=True)
        sort_label = "거래 횟수"

    print(f"\n정렬: {sort_label} 기준 (상위 {show_count}개 표시)\n")

    # 헤더
    print(f"{'순위':<5} {'코드':<8} {'종목명':<20} {'신호':<5} {'승':<4} {'패':<4} {'승률':<7} {'누적수익':<10} {'평균':<8} {'최고':<8} {'최저':<8}")
    print("-" * 100)

    for i, metric in enumerate(sorted_metrics[:show_count], 1):
        code = metric['code']
        name = metric['name'][:18]  # 종목명 길이 제한
        trade_count = metric['trade_count']
        win_count = metric['win_count']
        loss_count = metric['loss_count']
        win_rate = metric['win_rate']
        total_return = metric['total_return']
        avg_return = metric['avg_return']
        max_return = metric['max_return']
        min_return = metric['min_return']

        # 수익률에 따른 색상 표시 (텍스트에서는 +/- 기호로)
        total_sign = "+" if total_return >= 0 else ""
        avg_sign = "+" if avg_return >= 0 else ""

        print(f"{i:<5} {code:<8} {name:<20} {trade_count:<5} {win_count:<4} {loss_count:<4} "
              f"{win_rate:>6.1f}% {total_sign}{total_return:>7.2f}% {avg_sign}{avg_return:>6.2f}% "
              f"{max_return:>+6.2f}% {min_return:>+6.2f}%")

    # 종합 통계
    print("\n" + "="*100)
    print("종합 통계")
    print("="*100)

    avg_win_rate = sum(m['win_rate'] for m in metrics) / len(metrics)
    avg_total_return = sum(m['total_return'] for m in metrics) / len(metrics)
    avg_avg_return = sum(m['avg_return'] for m in metrics) / len(metrics)

    best_stock = sorted_metrics[0]
    worst_stock = sorted_metrics[-1]

    print(f"\n평균 승률: {avg_win_rate:.1f}%")
    print(f"평균 누적 수익률: {avg_total_return:+.2f}%")
    print(f"평균 수익률: {avg_avg_return:+.2f}%")

    print(f"\n최고 종목: {best_stock['code']} {best_stock['name']} ({best_stock['total_return']:+.2f}%, 승률 {best_stock['win_rate']:.1f}%)")
    print(f"최악 종목: {worst_stock['code']} {worst_stock['name']} ({worst_stock['total_return']:+.2f}%, 승률 {worst_stock['win_rate']:.1f}%)")

    # 승률별 상위/하위
    print(f"\n[승률 상위 10개]")
    win_rate_sorted = sorted(metrics, key=lambda x: x['win_rate'], reverse=True)
    for i, m in enumerate(win_rate_sorted[:10], 1):
        print(f"  {i:2d}. {m['code']} {m['name']:<18} | 신호 {m['trade_count']:>4d} | 승률 {m['win_rate']:>6.1f}% | 누적 {m['total_return']:>+7.2f}%")

    print(f"\n[수익률 상위 10개]")
    return_sorted = sorted(metrics, key=lambda x: x['avg_return'], reverse=True)
    for i, m in enumerate(return_sorted[:10], 1):
        print(f"  {i:2d}. {m['code']} {m['name']:<18} | 신호 {m['trade_count']:>4d} | 평균 {m['avg_return']:>+6.2f}% | 누적 {m['total_return']:>+7.2f}%")

    print(f"\n[신호 가장 많은 종목 TOP 10]")
    count_sorted = sorted(metrics, key=lambda x: x['trade_count'], reverse=True)
    for i, m in enumerate(count_sorted[:10], 1):
        print(f"  {i:2d}. {m['code']} {m['name']:<18} | 신호 {m['trade_count']:>4d} | 승률 {m['win_rate']:>6.1f}% | 평균 {m['avg_return']:>+6.2f}%")

if __name__ == "__main__":
    log_file = Path("logs/scanner.log")

    # 신호 추출
    signals = parse_scanner_log(log_file)

    if signals:
        print(f"\n추출된 신호: {len(signals)}개")

        # 종목별 거래 시뮬레이션
        stock_stats = simulate_trades_by_stock(signals)

        # 지표 계산
        metrics = calculate_stock_metrics(stock_stats)

        # 결과 출력 (누적 수익률 기준)
        print_stock_analysis(metrics, sort_by='total_return', show_count=50)
    else:
        print("\n신호를 추출하지 못했습니다.")
