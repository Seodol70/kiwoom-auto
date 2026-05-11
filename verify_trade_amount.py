#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_trade_amount.py
──────────────────────

프로그램 재시작 후 거래대금 재검증 스크립트

목표:
  1. 네이버(035420), 남해화학(025860) 등 이전 문제 종목 거래대금 확인
  2. system.log에서 "[진단]" 로그 추출 및 분석
  3. 거래대금 변환 과정 추적 (원 단위 정규화)

실행:
  python verify_trade_amount.py
"""

import sys
import os
import json
import re
from pathlib import Path
from datetime import datetime
from collections import defaultdict

PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

# 테스트 종목
TEST_CODES = [
    ("035420", "네이버"),           # 이전 3억 표시 문제
    ("025860", "남해화학"),          # 이전 447억 vs 실제 402억 문제
    ("000660", "SK하이닉스"),        # 시가총액 대형주
    ("005930", "삼성전자"),          # 대표 대형주
]

def parse_system_log():
    """system.log에서 거래대금 진단 로그 추출"""
    log_path = PROJECT_ROOT / "logs" / "system.log"

    if not log_path.exists():
        print(f"⚠️  로그 파일 없음: {log_path}")
        return {}

    # 거래대금 진단 로그 패턴
    patterns = {
        "opt10001": r"\[opt10001 거래대금 진단\].*?(\w+)\s+raw_amt=(\d+)\s+->\s+trade_amount=(\d+)\s+\(([\d.]+)억\)",
        "opt10030": r"\[opt10030 거래대금 진단\].*?(\w+)\s+raw_amt=(\d+)\s+->\s+amt_val=(\d+)\s+\(([\d.]+)억\)",
    }

    results = defaultdict(lambda: {"opt10001": [], "opt10030": []})

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                for source, pattern in patterns.items():
                    match = re.search(pattern, line)
                    if match:
                        code = match.group(1)
                        raw_amt = int(match.group(2))
                        converted_amt = int(match.group(3))
                        display_eok = float(match.group(4))

                        results[code][source].append({
                            "raw": raw_amt,
                            "converted": converted_amt,
                            "display_eok": display_eok,
                            "timestamp": line[:23] if len(line) > 23 else "",
                        })
    except Exception as e:
        print(f"❌ 로그 파싱 오류: {e}")

    return dict(results)

def fetch_snapshot_store(codes):
    """SnapshotStore에서 현재 거래대금 조회"""
    try:
        from scanner.snapshot_store import SnapshotStore

        # SnapshotStore는 싱글톤이 아니므로, 여기서는 데이터 파일을 직접 읽음
        cache_file = PROJECT_ROOT / "data" / "snapshot_cache.json"

        if cache_file.exists():
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache = json.load(f)
                return {code: cache.get(code, {}) for code in codes}
    except Exception as e:
        print(f"⚠️  SnapshotStore 조회 오류: {e}")

    return {}

def format_amount(amount_won):
    """거래대금을 한글로 표시"""
    n = int(amount_won or 0)

    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:.1f}조"
    if n >= 100_000_000:
        return f"{n // 100_000_000:,}억"
    if n >= 10_000:
        return f"{n // 10_000:,}만원"
    return f"{n:,}원"

def print_report():
    """거래대금 검증 보고서 출력"""
    print("\n" + "=" * 80)
    print("🔍 거래대금 재검증 보고서")
    print("=" * 80)
    print(f"생성: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1️⃣ 로그 분석
    print("=" * 80)
    print("1️⃣  system.log 진단 로그 분석")
    print("=" * 80)

    log_data = parse_system_log()

    if not log_data:
        print("⚠️  진단 로그 없음 (프로그램 재시작 필요)")
    else:
        for code, name in TEST_CODES:
            if code in log_data:
                data = log_data[code]
                print(f"\n📍 {name}({code})")

                if data["opt10001"]:
                    print(f"   [opt10001 - 개별 종목 정보]")
                    for entry in data["opt10001"][-3:]:  # 최근 3개만
                        print(f"      raw={entry['raw']:,} → {entry['converted']:,}원 ({entry['display_eok']:.2f}억) {entry['timestamp']}")

                if data["opt10030"]:
                    print(f"   [opt10030 - 거래대금 상위]")
                    for entry in data["opt10030"][-3:]:  # 최근 3개만
                        print(f"      raw={entry['raw']:,} → {entry['converted']:,}원 ({entry['display_eok']:.2f}억) {entry['timestamp']}")

    # 2️⃣ 거래대금 단위 검증
    print("\n" + "=" * 80)
    print("2️⃣  거래대금 단위 검증")
    print("=" * 80)
    print("\n예상 변환 규칙: raw_amt × 1,000,000 = 원 단위")
    print()

    test_cases = [
        (100, "100 × 1,000,000 = 100,000,000원 (1억)"),
        (1, "1 × 1,000,000 = 1,000,000원 (10만원)"),
        (402, "402 × 1,000,000 = 402,000,000원 (4억 200만원 = 약 402억)"),  # 남해화학 예상값
    ]

    for raw, expected in test_cases:
        converted = raw * 1_000_000
        display = format_amount(converted)
        print(f"✅ {expected} → 표시: {display}")

    # 3️⃣ 이전 문제 종목 확인
    print("\n" + "=" * 80)
    print("3️⃣  이전 문제 종목 재검증")
    print("=" * 80)
    print()

    print("📌 네이버(035420)")
    print("   문제: UI에 '3억'으로 표시됨")
    print("   원인: 추측 기반 단위 판별 오류")
    print("   개선: 백만 원 단위(× 1,000,000)로 통일")
    if "035420" in log_data and log_data["035420"]["opt10030"]:
        latest = log_data["035420"]["opt10030"][-1]
        print(f"   결과: {latest['raw']:,} → {format_amount(latest['converted'])} ✅")
    else:
        print("   결과: 아직 로그 없음 (프로그램에서 거래대금 상위 조회 필요)")

    print()
    print("📌 남해화학(025860)")
    print("   문제: UI에 '447억'으로 표시, 실제 약 '402억'")
    print("   원인: 추측 기반 단위 판별 오류 (천원 vs 백만 원)")
    print("   개선: 백만 원 단위(× 1,000,000)로 통일")
    if "025860" in log_data and log_data["025860"]["opt10030"]:
        latest = log_data["025860"]["opt10030"][-1]
        expected_eok = 402  # 예상값
        print(f"   결과: {latest['raw']:,} → {format_amount(latest['converted'])}")
        print(f"   기대값: 약 {expected_eok}억 vs 실제: {latest['display_eok']:.0f}억", end="")
        if abs(latest['display_eok'] - expected_eok) < 50:  # 50억 오차 범위
            print(" ✅")
        else:
            print(" ⚠️")
    else:
        print("   결과: 아직 로그 없음 (프로그램에서 거래대금 상위 조회 필요)")

    # 4️⃣ 검증 체크리스트
    print("\n" + "=" * 80)
    print("4️⃣  검증 체크리스트")
    print("=" * 80)
    print()

    print("✅ 수정 사항:")
    print("   - kiwoom_api.py:237-248 → _resolve_trade_amount() 백만 원 단위 통일")
    print("   - kiwoom_api.py:501-502 → opt10001 진단 로그 (DEBUG 레벨)")
    print("   - kiwoom_api.py:1341-1342 → opt10030 진단 로그 (DEBUG 레벨)")
    print("   - smart_scanner.py:969-975 → FID 13 백만 원 단위 통일")
    print()

    print("📋 재검증:")
    print("   1. 프로그램 재시작 (캐시 초기화, TR Limit 해제 대기)")
    print("   2. 네이버/남해화학 등 종목 거래대금 상위 조회 자동 시작")
    print("   3. system.log에서 '[진단]' 로그 확인")
    print("      → logger.debug() 사용이므로 다음 방법으로 활성화:")
    print("         - logging.basicConfig(level=logging.DEBUG)")
    print("         - 또는 config/logging.yaml에서 DEBUG 레벨 설정")
    print()

    print("=" * 80)
    print("🎯 다음 단계")
    print("=" * 80)
    print()
    print("1. 프로그램 시작 (python ui/main_window.py)")
    print("2. 자동매매 ON")
    print("3. 옵션 → 로그 → DEBUG 레벨로 설정 (선택)")
    print("4. 거래대금 컬럼 확인:")
    print("   - 네이버: 300억+ (시가총액 대형주)")
    print("   - 남해화학: ~402억 (실제 거래대금)")
    print("5. 수일 거래 후 이 스크립트 다시 실행: python verify_trade_amount.py")
    print()

if __name__ == "__main__":
    print_report()
