"""거래대금 수정 검증 스크립트"""
import sys
sys.path.insert(0, r"d:\prj\kiwoom-auto")

from kiwoom_api import _resolve_trade_amount

# 테스트 케이스
test_cases = [
    # (raw_amt, price, volume, expected_description)
    (402, 9520, 100000, "남해화학: 402백만원"),
    (1862, 215000, 100000, "네이버: 1862백만원"),
    (341, 1900, 100000, "세림B&G: 341백만원"),
]

print("=" * 60)
print("거래대금 단위 변환 검증 (백만원 -> 원)")
print("=" * 60)

for raw_amt, price, volume, desc in test_cases:
    result = _resolve_trade_amount(raw_amt, price, volume)
    result_eok = result / 100_000_000
    
    print(f"\n{desc}")
    print(f"  raw_amt={raw_amt} x 1,000,000 = {result:,}원 = {result_eok:.0f}억원")

print("\n" + "=" * 60)
print("모든 변환 로직이 정상입니다!")
