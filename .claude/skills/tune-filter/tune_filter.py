#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/tune-filter 스킬 실행 엔진
거래 분석 결과를 기반으로 필터 파라미터 최적화 제안 및 자동 적용
"""

import sys
import os
import re
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

os.environ['PYTHONIOENCODING'] = 'utf-8'

def get_latest_analysis() -> Optional[Dict]:
    """최근 분석 파일 로드"""
    memory_dir = Path("C:/Users/seodo/.claude/projects/d--prj-kiwoom-auto/memory")

    if not memory_dir.exists():
        print("[오류] memory 디렉토리를 찾을 수 없습니다.")
        return None

    # phase9_*_analysis.md 파일 찾기
    analysis_files = sorted(memory_dir.glob("phase9_*_analysis.md"), reverse=True)

    if not analysis_files:
        print("[정보] 분석 파일이 없습니다. 먼저 /analyze-trades를 실행하세요.")
        return None

    latest_file = analysis_files[0]

    with open(latest_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 파일명에서 날짜 추출
    match = re.search(r'phase9_(\d{4}-\d{2}-\d{2})', latest_file.name)
    date = match.group(1) if match else "UNKNOWN"

    return {
        'date': date,
        'file': latest_file.name,
        'content': content
    }

def extract_stats(analysis: Dict) -> Dict:
    """분석 파일에서 통계 추출"""
    content = analysis['content']
    stats = {}

    # 거래 통계 추출
    if '총 거래' in content or '진입' in content:
        # 간단한 패턴 매칭
        match = re.search(r'진입[:\s]+(\d+)건?', content)
        if match:
            stats['total_trades'] = int(match.group(1))

        match = re.search(r'승률[:\s]*(\d+)%', content)
        if match:
            stats['win_rate'] = int(match.group(1))

        match = re.search(r'BREAKOUT[:\s]*진입\s*(\d+)', content)
        if match:
            stats['breakout_count'] = int(match.group(1))

        match = re.search(r'PULLBACK[:\s]*진입\s*(\d+)', content)
        if match:
            stats['pullback_count'] = int(match.group(1))

    return stats

def generate_tuning_suggestions(stats: Dict) -> List[Dict]:
    """통계 기반 튜닝 제안 생성"""
    suggestions = []

    # 1. BREAKOUT 필터 조정
    if stats.get('breakout_count', 0) >= 3:
        breakout_rate = stats.get('breakout_count', 0) / stats.get('total_trades', 1)
        if breakout_rate > 0.5 and stats.get('win_rate', 0) < 25:
            suggestions.append({
                'priority': 1,
                'param': 'breakout_pullback_from_high_pct',
                'current': 5.0,
                'suggested': 3.0,
                'reason': f'BREAKOUT 신호 {breakout_rate*100:.0f}% 중심이나 승률 {stats.get("win_rate", 0)}%로 낮음. 고점 -3% 이내로 강화하면 신뢰도 향상',
                'impact': 'BREAKOUT 신호 ~30% 감소, 거짓신호 차단'
            })

    # 2. 체결강도 필터 조정
    if stats.get('total_trades', 0) >= 5:
        if stats.get('win_rate', 0) < 30:
            suggestions.append({
                'priority': 2,
                'param': 'min_chejan_strength_morning',
                'current': 90,
                'suggested': 110,
                'reason': f'현재 90%는 약세 신호까지 포착. 승률 {stats.get("win_rate", 0)}%이므로 필터 강화 필요',
                'impact': '약세 신호 차단, 신호 ~20% 감소'
            })

    # 3. 데이터 부족 경고
    if stats.get('total_trades', 0) < 10:
        suggestions.append({
            'priority': 0,
            'param': '_info',
            'reason': f'현재 거래 {stats.get("total_trades", 0)}건으로 데이터 부족 (권장 50건). 3~5일 추가 수집 후 조정 추천',
            'impact': 'none'
        })

    return sorted(suggestions, key=lambda x: x['priority'])

def print_suggestions(date: str, suggestions: List[Dict]) -> None:
    """제안 출력"""
    print("\n" + "="*60)
    print(f"[필터 최적화 제안] {date}")
    print("="*60)

    # 정보성 메시지
    info_msgs = [s for s in suggestions if s['param'] == '_info']
    for msg in info_msgs:
        print(f"\n[정보] {msg['reason']}")

    # 실제 조정 제안
    tune_suggestions = [s for s in suggestions if s['param'] != '_info']

    if not tune_suggestions:
        print("\n현재 조정 대상 필터 없음. 더 많은 데이터를 기다리세요.")
        print("="*60 + "\n")
        return

    print("\n[조정 제안]\n")
    for i, sugg in enumerate(tune_suggestions, 1):
        print(f"{i}. {sugg['param']}")
        print(f"   현재: {sugg['current']} → 제안: {sugg['suggested']}")
        print(f"   근거: {sugg['reason']}")
        print(f"   영향: {sugg['impact']}\n")

    print("적용할까요?")
    print("[1] 모두 적용")
    print("[2] 선택 적용 (번호로 입력: 1,2)")
    print("[3] 기록만 (적용 안 함)")
    print()

def get_user_choice(tune_suggestions: List[Dict]) -> Tuple[int, List[int]]:
    """사용자 선택 입력"""
    while True:
        try:
            choice = input("선택: ").strip()

            if choice == "1":
                return 1, list(range(len(tune_suggestions)))
            elif choice == "2":
                nums_input = input("번호 입력 (쉼표 구분, 예: 1,2): ").strip()
                selected = [int(n.strip()) - 1 for n in nums_input.split(',')]
                if all(0 <= n < len(tune_suggestions) for n in selected):
                    return 2, selected
                else:
                    print("범위 벗어난 번호입니다. 다시 입력하세요.")
            elif choice == "3":
                return 3, []
            else:
                print("[1], [2], [3] 중에서 선택하세요.")
        except (ValueError, IndexError):
            print("올바른 입력이 아닙니다. 다시 시도하세요.")

def apply_suggestions(config_file: str, suggestions: List[Dict], selected_indices: List[int]) -> bool:
    """선택된 조정안을 config.py에 적용"""
    config_path = Path(config_file)

    if not config_path.exists():
        print(f"[오류] {config_file}을 찾을 수 없습니다.")
        return False

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 각 제안 적용
        for idx in selected_indices:
            sugg = suggestions[idx]
            param = sugg['param']
            new_value = sugg['suggested']

            # config.py에서 파라미터 찾기 (다양한 형식 지원)
            patterns = [
                rf"({param}\s*:\s*){sugg['current']}",
                rf"({param}\s*=\s*){sugg['current']}",
            ]

            replaced = False
            for pattern in patterns:
                if re.search(pattern, content):
                    content = re.sub(pattern, rf"\g<1>{new_value}", content)
                    replaced = True
                    break

            if replaced:
                print(f"✓ {param}: {sugg['current']} → {new_value}")
            else:
                print(f"✗ {param} 패턴을 찾을 수 없습니다.")

        # 파일 저장
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(content)

        print(f"\n[성공] {config_file}이 업데이트되었습니다.")
        return True

    except Exception as e:
        print(f"[오류] 파일 수정 실패: {e}")
        return False

def save_tuning_log(date: str, suggestions: List[Dict], selected_indices: List[int], choice: int) -> None:
    """조정 이력을 메모리에 저장"""
    memory_dir = Path("C:/Users/seodo/.claude/projects/d--prj-kiwoom-auto/memory")
    log_file = memory_dir / f"phase9_{date}_tuning_log.md"

    applied_suggestions = [suggestions[i] for i in selected_indices] if choice != 3 else []

    log_content = f"""---
name: phase9_{date.replace('-', '_')}_tuning
description: {date} 필터 조정 이력 — 변경된 파라미터 + 적용 결과
metadata:
  type: project
---

# 필터 조정 이력 — {date}

"""

    if choice == 1 or choice == 2:
        log_content += "## 적용된 조정\n\n"
        for i, sugg in enumerate(applied_suggestions, 1):
            log_content += f"{i}. {sugg['param']}: {sugg['current']} → {sugg['suggested']}\n"
            log_content += f"   근거: {sugg['reason']}\n"
            log_content += f"   적용일: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    else:
        log_content += "## 제안됨 (미적용)\n\n"
        log_content += "사용자가 적용을 거부했습니다.\n\n"

    log_content += "## 다음 검토 예정\n\n"
    log_content += "1. 적용된 조정 결과 모니터링 (3~5일)\n"
    log_content += "2. 추가 데이터 축적 후 재평가\n"

    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(log_content)
        print(f"\n[기록] {log_file.name}에 저장되었습니다.")
    except Exception as e:
        print(f"[경고] 로그 저장 실패: {e}")

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None

    # 최근 분석 파일 로드
    analysis = get_latest_analysis()

    if not analysis:
        sys.exit(1)

    date = analysis['date']

    # 통계 추출
    stats = extract_stats(analysis)

    if not stats:
        print(f"[경고] {date} 분석에서 통계를 추출할 수 없습니다.")
        print(f"파일: {analysis['file']}")
        sys.exit(1)

    # 제안 생성
    suggestions = generate_tuning_suggestions(stats)

    # 조정 대상 필터만 분리
    tune_suggestions = [s for s in suggestions if s['param'] != '_info']

    # 제안 출력
    print_suggestions(date, suggestions)

    if not tune_suggestions:
        sys.exit(0)

    # 사용자 입력 받기
    choice, selected_indices = get_user_choice(tune_suggestions)

    # 선택에 따른 처리
    if choice == 1:  # 모두 적용
        print("\n[실행] 모든 조정안을 적용합니다...\n")
        apply_suggestions("scanner/config.py", tune_suggestions, selected_indices)
        save_tuning_log(date, tune_suggestions, selected_indices, choice)
    elif choice == 2:  # 선택 적용
        print("\n[실행] 선택된 조정안을 적용합니다...\n")
        apply_suggestions("scanner/config.py", tune_suggestions, selected_indices)
        save_tuning_log(date, tune_suggestions, selected_indices, choice)
    else:  # 기록만
        print("\n[기록] 조정을 적용하지 않습니다.")
        save_tuning_log(date, tune_suggestions, [], choice)

    print("\n[완료] 작업이 종료되었습니다.")
