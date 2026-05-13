#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/analyze-trades 스킬 실행 엔진
거래 로그를 분석하여 신호별 손익, 차단 패턴, 필터 조정안을 도출한다.
"""

import sys
import os
os.environ['PYTHONIOENCODING'] = 'utf-8'
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple

def get_analysis_date(date_arg: str = None) -> str:
    """분석 대상 날짜 결정"""
    if date_arg:
        # 사용자 지정 날짜 검증
        try:
            datetime.strptime(date_arg, "%Y-%m-%d")
            return date_arg
        except ValueError:
            print(f"❌ 날짜 형식 오류: {date_arg} (YYYY-MM-DD 형식으로 입력)")
            sys.exit(1)

    # 최근 영업일 자동 추론
    # __file__ = d:\prj\kiwoom-auto\.claude\skills\analyze-trades\analyze_trades.py
    # parent.parent.parent.parent = d:\prj\kiwoom-auto
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    log_file = project_root / "logs" / "position.log"
    if not log_file.exists():
        print(f"[오류] {log_file} 파일을 찾을 수 없습니다.")
        sys.exit(1)

    with open(log_file, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    if not lines:
        print("❌ logs/position.log가 비어있습니다.")
        sys.exit(1)

    # 마지막 라인에서 날짜 추출
    for line in reversed(lines):
        match = re.search(r'(\d{4}-\d{2}-\d{2})', line)
        if match:
            return match.group(1)

    print("❌ position.log에서 날짜를 찾을 수 없습니다.")
    sys.exit(1)

def collect_position_data(date: str) -> List[Dict]:
    """포지션 생성/청산 데이터 수집"""
    positions = []
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    log_file = project_root / "logs" / "position.log"

    if not log_file.exists():
        return positions

    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if date not in line:
                continue

            # [포지션생성] 패턴
            if "[포지션생성]" in line:
                match = re.search(r'(\d{2}:\d{2}:\d{2}).*\((\d+)\).*체결가=(\d+)', line)
                if match:
                    code_match = re.search(r'\((\d+)\)', line)
                    name_match = re.search(r'(\S+)\(', line)
                    if code_match and name_match:
                        positions.append({
                            'time': match.group(1),
                            'code': code_match.group(1),
                            'name': name_match.group(1),
                            'type': 'BUY',
                            'price': int(match.group(3))
                        })

            # [포지션청산] 패턴
            elif "[포지션청산]" in line:
                match = re.search(r'(\d{2}:\d{2}:\d{2}).*\((\d+)\).*매도가=(\d+).*손익=([+-][\d.]+%)', line)
                if match:
                    code_match = re.search(r'\((\d+)\)', line)
                    name_match = re.search(r'(\S+)\(', line)
                    if code_match and name_match:
                        positions.append({
                            'time': match.group(1),
                            'code': code_match.group(1),
                            'name': name_match.group(1),
                            'type': 'SELL',
                            'price': int(match.group(3)),
                            'pnl': match.group(4)
                        })

    return positions

def collect_signal_data(date: str) -> List[Dict]:
    """신호 수신 데이터 수집"""
    signals = []
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    log_file = project_root / "logs" / "order.log"

    if not log_file.exists():
        return signals

    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if date not in line or "[신호수신]" not in line:
                continue

            match = re.search(
                r'(\d{2}:\d{2}:\d{2}).*\((\d+)\).*유형=(\w+)',
                line
            )
            if match:
                code_match = re.search(r'\((\d+)\)', line)
                name_match = re.search(r'(\S+)\(', line)
                if code_match and name_match:
                    signals.append({
                        'time': match.group(1),
                        'code': code_match.group(1),
                        'name': name_match.group(1),
                        'signal_type': match.group(3)
                    })

    return signals

def analyze_trades(date: str) -> Dict:
    """거래 분석 메인 로직"""
    positions = collect_position_data(date)
    signals = collect_signal_data(date)

    if not positions:
        return {'error': f'{date}에 거래 기록이 없습니다.'}

    # 포지션 쌍맺기 (매수-매도)
    buy_positions = {p['code']: p for p in positions if p['type'] == 'BUY'}
    sell_positions = {p['code']: p for p in positions if p['type'] == 'SELL'}

    trades = []
    signal_map = {s['code']: s['signal_type'] for s in signals}

    for code, buy in buy_positions.items():
        if code in sell_positions:
            sell = sell_positions[code]
            pnl_str = sell.get('pnl', 'N/A')
            pnl_pct = float(pnl_str.replace('%', '').replace('+', '')) if pnl_str != 'N/A' else 0

            trades.append({
                'time': buy['time'],
                'name': buy['name'],
                'code': code,
                'signal': signal_map.get(code, 'UNKNOWN'),
                'buy_price': buy['price'],
                'sell_price': sell['price'],
                'pnl_pct': pnl_pct,
                'pnl_str': pnl_str
            })

    # 통계 계산
    stats = {
        'total_trades': len(trades),
        'winning_trades': sum(1 for t in trades if t['pnl_pct'] > 0),
        'losing_trades': sum(1 for t in trades if t['pnl_pct'] < 0),
        'avg_pnl': sum(t['pnl_pct'] for t in trades) / len(trades) if trades else 0,
        'max_win': max((t['pnl_pct'] for t in trades), default=0),
        'max_loss': min((t['pnl_pct'] for t in trades), default=0),
    }

    # 신호별 분석
    signal_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'total_pnl': 0})
    for trade in trades:
        signal = trade['signal']
        signal_stats[signal]['count'] += 1
        if trade['pnl_pct'] > 0:
            signal_stats[signal]['wins'] += 1
        signal_stats[signal]['total_pnl'] += trade['pnl_pct']

    return {
        'date': date,
        'trades': trades,
        'stats': stats,
        'signal_stats': dict(signal_stats),
        'error': None
    }

def print_analysis(result: Dict) -> None:
    """분석 결과 출력"""
    if result.get('error'):
        print(f"[오류] {result['error']}")
        return

    date = result['date']
    stats = result['stats']
    signal_stats = result['signal_stats']
    trades = result['trades']

    print("\n" + "="*60)
    print(f"[거래 분석] {date}")
    print("="*60)

    # 거래 결과
    win_rate = (stats['winning_trades'] / stats['total_trades'] * 100) if stats['total_trades'] > 0 else 0
    print(f"\n[거래 결과]")
    print(f"  총 거래: {stats['total_trades']}건")
    print(f"  승률: {win_rate:.0f}% ({stats['winning_trades']}승 {stats['losing_trades']}패)")
    print(f"  평균 손익: {stats['avg_pnl']:+.2f}%")
    print(f"  최대 수익: {stats['max_win']:+.2f}%")
    print(f"  최대 손실: {stats['max_loss']:+.2f}%")

    # 신호별 분석
    print(f"\n[신호별 분석]")
    for signal, data in sorted(signal_stats.items()):
        if data['count'] == 0:
            continue
        win_rate = (data['wins'] / data['count'] * 100) if data['count'] > 0 else 0
        avg = data['total_pnl'] / data['count']
        print(f"  {signal:12} {data['count']}건 / 승률 {win_rate:.0f}% / 평균 {avg:+.2f}%")

    # 거래 상세
    print(f"\n[거래 상세]")
    print(f"{'시간':<8} {'종목':<15} {'신호':<10} {'매수':<7} {'매도':<7} {'손익':<8} {'결과':<4}")
    print("-" * 60)
    for trade in sorted(trades, key=lambda x: x['time']):
        result_str = "OK" if trade['pnl_pct'] > 0 else "NG"
        print(f"{trade['time']} {trade['name']:<12} {trade['signal']:<10} "
              f"{trade['buy_price']:<7} {trade['sell_price']:<7} {trade['pnl_str']:<8} {result_str:<4}")

    print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    date = get_analysis_date(date_arg)
    result = analyze_trades(date)
    print_analysis(result)

    # 분석 완료 후 자동으로 필터 튜닝 제안
    if not result.get('error'):
        print("\n[다음 단계]")
        print("필터 최적화 제안:")
        print("  /tune-filter")
