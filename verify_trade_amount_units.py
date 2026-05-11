#!/usr/bin/env python3
"""
거래대금 단위 일관성 검증 스크립트

목표:
  1. 모든 거래대금 비교 코드 찾기
  2. 각 코드의 임계값 단위 확인
  3. 단위 혼동 위험 지역 표시
"""

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
from pathlib import Path
from typing import List, Tuple

# Windows 콘솔 인코딩 수정
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ============================================================================
# 거래대금 비교 패턴 감지
# ============================================================================

TRADE_AMOUNT_PATTERNS = [
    r'trade_amount\s*[<>=!]+',      # trade_amount >= ... 등
    r'\.trade_amount\s*[<>=!]+',    # snap.trade_amount < ... 등
    r'min_trade_amount',             # min_trade_amount 설정값
    r'trade_amount\s*\*',            # trade_amount * 배수
    r'trade_amount\s*/',             # trade_amount / 나눗셈
]

# 단위별 판별 헬퍼
UNIT_MARKERS = {
    '백만원(1e6)': [r'1_?0{6}', r'1e6', r'\* 1e6'],
    '원(1e0)': [r'e9\b', r'e8\b', r'50\s*e9', r'100\s*e9', r'billions?'],
    '억원(1e8)': [r'1_?0{8}', r'1e8'],
}

# ============================================================================
# 검색 함수
# ============================================================================

def find_trade_amount_usages(directory: str) -> List[Tuple[str, int, str, str]]:
    """
    거래대금 관련 코드 찾기.

    Returns:
        [(파일경로, 라인번호, 라인내용, 패턴)]
    """
    results = []
    scan_dir = Path(directory)

    for py_file in scan_dir.rglob('*.py'):
        # 테스트 파일 제외
        if 'test_' in py_file.name:
            continue

        try:
            with open(py_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line_no, line in enumerate(f, start=1):
                    # 주석 제외
                    if line.strip().startswith('#'):
                        continue

                    for pattern in TRADE_AMOUNT_PATTERNS:
                        if re.search(pattern, line, re.IGNORECASE):
                            results.append((
                                str(py_file.relative_to(directory)),
                                line_no,
                                line.strip(),
                                pattern
                            ))
                            break
        except Exception as e:
            print(f"[ERR] {py_file}: {e}")

    return results

# ============================================================================
# 단위 판별 함수
# ============================================================================

def infer_unit(line: str) -> str:
    """라인 내용으로 단위 판별."""
    # 명시적 주석 확인
    if '원' in line and ('단위' in line or '₩' in line):
        if '억' in line:
            return "억원(명시)"
        else:
            return "원(명시)"

    # 값 기반 추론
    for unit, markers in UNIT_MARKERS.items():
        for marker in markers:
            if re.search(marker, line):
                return unit

    return "불명확"

# ============================================================================
# 메인 검증
# ============================================================================

def main():
    root = "."

    print("=" * 80)
    print("거래대금 단위 일관성 검증")
    print("=" * 80)
    print()

    usages = find_trade_amount_usages(root)

    if not usages:
        print("❌ 거래대금 비교 코드를 찾을 수 없습니다.")
        return 1

    print(f"✅ {len(usages)}개의 거래대금 관련 코드 발견\n")

    # 파일별 그룹핑
    by_file = {}
    for fpath, lno, line, pat in usages:
        if fpath not in by_file:
            by_file[fpath] = []
        by_file[fpath].append((lno, line, pat))

    # 파일별 출력
    for fpath in sorted(by_file.keys()):
        print(f"\n📄 {fpath}")
        print("-" * 80)

        for lno, line, pat in sorted(by_file[fpath], key=lambda x: x[0]):
            unit = infer_unit(line)
            marker = "🔴" if "불명확" in unit else "🟢"
            print(f"  {marker} Line {lno:4d}: {unit:15s} | {line[:70]}")

    # 요약
    print("\n" + "=" * 80)
    print("체크리스트")
    print("=" * 80)

    unclear_count = sum(
        1 for f, lines in by_file.items()
        for lno, line, pat in lines
        if "불명확" in infer_unit(line)
    )

    print(f"✅ 명확한 단위: {len(usages) - unclear_count}")
    print(f"🔴 불명확한 단위: {unclear_count}")

    if unclear_count > 0:
        print("\n⚠️  불명확한 영역:")
        for fpath in sorted(by_file.keys()):
            for lno, line, pat in sorted(by_file[fpath], key=lambda x: x[0]):
                if "불명확" in infer_unit(line):
                    print(f"   → {fpath}:{lno}")

    # 권장 사항
    print("\n" + "=" * 80)
    print("권장 개선")
    print("=" * 80)
    print("""
1️⃣  constants.py에 상수 정의:
   TRADE_AMOUNT_MIN_WON = 50e9        # 50억원
   TRADE_AMOUNT_THRESHOLD = 100e9     # 100억원

2️⃣  StockSnapshot에 변환 프로퍼티 추가:
   @property
   def trade_amount_won(self) -> int:
       return self.trade_amount

   @property
   def trade_amount_billion_won(self) -> float:
       return self.trade_amount / 1e8

3️⃣  모든 비교 코드에 주석 추가:
   # 거래대금 (원 단위)
   if snap.trade_amount > TRADE_AMOUNT_MIN_WON:
""")

    return 0 if unclear_count == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
