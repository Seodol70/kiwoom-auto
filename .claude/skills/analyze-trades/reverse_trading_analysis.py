#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
역매매 분석: 실제 거래를 모두 반대로 했으면 이익이 났을까?
"""

import json
import sys
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# 출력 인코딩 설정
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def load_fills(date_str):
    """특정 날짜의 fills 로그 로드"""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    log_file = project_root / "logs" / f"fills_{date_str}.jsonl"

    if not log_file.exists():
        return []

    trades = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                try:
                    trades.append(json.loads(line))
                except:
                    pass
    return trades

def analyze_reverse_trading(trades):
    """
    실제 거래를 역매매했을 경우 손익 분석

    실제: 12780에 매수 → 13080에 매도 → +300원 수익
    역매매: 12780에 매도 → 13080에 매수 → -300원 손실

    → 역매매에서는 매수가와 매도가의 역할을 바꿈
    """

    # 종목별로 거래 그룹화 (같은 종목의 여러 거래를 추적)
    by_code = defaultdict(list)
    for trade in trades:
        by_code[trade['code']].append(trade)

    actual_total = 0
    reverse_total = 0

    for code, code_trades in by_code.items():
        # 모든 거래의 손익 합산
        for trade in code_trades:
            actual_pnl = trade['realized']
            # 역매매: 매수가와 매도가를 뒤집음
            # 실제 매수가에 반대 포지션을 취함
            avg_price = trade['avg_price']
            sell_price = trade['sell_price']
            qty = trade['qty']

            # 실제 손익: (매도가 - 매수가) × 수량
            # 역매매 손익: (매수가 - 매도가) × 수량 = -실제손익
            reverse_pnl = -actual_pnl

            actual_total += actual_pnl
            reverse_total += reverse_pnl

    return {
        'actual_total': actual_total,
        'reverse_total': reverse_total,
        'num_trades': len(trades),
        'by_code': by_code
    }

def print_analysis(start_date, end_date):
    """지정 기간 역매매 분석"""
    print(f"\n{'='*70}")
    print(f"📊 역매매 분석 — {start_date} ~ {end_date}")
    print(f"{'='*70}\n")

    from datetime import datetime, timedelta

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    actual_grand_total = 0
    reverse_grand_total = 0
    total_trades = 0

    current = start
    daily_results = []

    while current <= end:
        date_str = current.strftime("%Y%m%d")
        trades = load_fills(date_str)

        if trades:
            result = analyze_reverse_trading(trades)
            actual_total = result['actual_total']
            reverse_total = result['reverse_total']
            num_trades = result['num_trades']

            actual_grand_total += actual_total
            reverse_grand_total += reverse_total
            total_trades += num_trades

            daily_results.append({
                'date': current.strftime("%m-%d"),
                'trades': num_trades,
                'actual': actual_total,
                'reverse': reverse_total,
                'diff': reverse_total - actual_total
            })

            print(f"📅 {current.strftime('%m-%d (%a)')}")
            print(f"   거래 수: {num_trades}건")
            print(f"   실제 손익: {actual_total:+,}원")
            print(f"   역매매 손익: {reverse_total:+,}원")
            if reverse_total > actual_total:
                print(f"   ⚠️  역매매가 {reverse_total - actual_total:+,}원 더 나음!")
            print()

        current += timedelta(days=1)

    print(f"{'='*70}")
    print(f"【 1주일 종합 】")
    print(f"{'='*70}")
    print(f"총 거래: {total_trades}건")
    print(f"\n실제 손익:   {actual_grand_total:+,}원")
    print(f"역매매 손익: {reverse_grand_total:+,}원")
    print(f"\n차이: {reverse_grand_total - actual_grand_total:+,}원")

    if reverse_grand_total > actual_grand_total:
        print(f"\n🚨 역매매가 {reverse_grand_total - actual_grand_total:+,}원 더 많은 손실!")
        print("→ 우리가 하는 매매 방향이 맞다는 뜻 (거짓양성 문제지, 방향이 틀린 건 아님)")
    elif reverse_grand_total < actual_grand_total:
        print(f"\n⚠️  역매매가 {actual_grand_total - reverse_grand_total:+,}원 더 수익!")
        print("→ 시스템이 반대 신호를 주고 있다는 뜻 (심각한 문제)")
    else:
        print(f"\n동일 손익 (우연)")

    print(f"\n{'='*70}\n")

    # 일일 상세
    print("【 일일 상세 】")
    print(f"{'날짜':<8} {'거래':<6} {'실제':<12} {'역매매':<12} {'판정':<10}")
    print("-" * 58)
    for day in daily_results:
        diff = day['diff']
        if diff > 0:
            verdict = "역 우수"
        elif diff < 0:
            verdict = "실제 우수"
        else:
            verdict = "동일"
        print(f"{day['date']:<8} {day['trades']:<6} {day['actual']:>+11,} {day['reverse']:>+11,} {verdict:<10}")

if __name__ == "__main__":
    print_analysis("2026-05-12", "2026-05-18")
