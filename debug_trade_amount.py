#!/usr/bin/env python3
"""
한온시스템(018880) 거래대금 디버깅 스크립트

문제: 114.4조원으로 표시되는데, 실제는 130억원대
검증: FID 13 raw 값 확인
"""

# 한온시스템 실제 데이터
code = "018880"
name = "한온시스템"
current_price = 5_700  # 현재가
volume = 27_730_000    # 거래량 (2,773만주)
expected_trade_amt = 130_000_000_000  # 예상 거래대금 (130억원)

print("=" * 80)
print(f"한온시스템({code}) 거래대금 검증")
print("=" * 80)
print(f"현재가: {current_price:,}원")
print(f"거래량: {volume:,}주")
print(f"예상 거래대금: {expected_trade_amt:,}원 ({expected_trade_amt/1e8:.1f}억)")
print()

# 로그에 표시된 값: 114.4조
logged_amount = 114_400_000_000_000  # 114.4조

print("=" * 80)
print("역산: FID 13 raw 값이 얼마였는가?")
print("=" * 80)

# 만약 × 1,000,000이면
raw_if_millions = logged_amount / 1_000_000
print(f"× 1,000,000 적용 시 raw = {raw_if_millions:,.0f}")
print(f"  → {raw_if_millions / 10_000:.0f}만")
print()

# 만약 × 1,000이면
raw_if_thousands = logged_amount / 1_000
print(f"× 1,000 적용 시 raw = {raw_if_thousands:,.0f}")
print()

# 실제 필요한 raw 값
raw_needed_for_millions = expected_trade_amt / 1_000_000
raw_needed_for_thousands = expected_trade_amt / 1_000

print("=" * 80)
print("올바른 값이 되려면?")
print("=" * 80)
print(f"× 1,000,000 적용 시 raw = {raw_needed_for_millions:,.0f}")
print(f"× 1,000 적용 시 raw = {raw_needed_for_thousands:,.0f}")
print()

# 로그의 114.4조가 맞는지 역산
print("=" * 80)
print("로그의 114.4조가 맞다면...")
print("=" * 80)

# 현재가 × 거래량으로 계산
calculated_amt = current_price * volume
print(f"현재가 × 거래량 = {current_price:,} × {volume:,} = {calculated_amt:,}원")
print(f"  = {calculated_amt/1e8:.1f}억원")
print()

# 114.4조를 역산
print(f"로그 값: {logged_amount:,}원 = {logged_amount/1e12:.1f}조")
print(f"계산값: {calculated_amt:,}원 = {calculated_amt/1e12:.1f}조")
print()

# 비율 확인
ratio = logged_amount / calculated_amt
print(f"비율: {logged_amount:,} / {calculated_amt:,} = {ratio:.0f}배")
print()

# 혹시 FID 13이 누적거래량인가?
print("=" * 80)
print("혹시 FID 13이 cumulative volume이라면?")
print("=" * 80)

cum_vol_if_raw_is_logged = logged_amount / current_price
print(f"FID 13 = cumulative volume이라면:")
print(f"  raw (누적거래량) = {logged_amount:,} / {current_price:,} = {cum_vol_if_raw_is_logged:,.0f}주")
print(f"  실제 거래량: {volume:,}주")
print(f"  비율: {cum_vol_if_raw_is_logged / volume:.0f}배")
print()

# 결론
print("=" * 80)
print("결론")
print("=" * 80)

# TradeAmountHelper.normalize_from_kiwoom(raw_amt, price, volume)를 확인
# 만약 raw_amt가 114400이면?
if raw_if_thousands == 114_400:
    print(f"✓ FID 13 raw = 114,400 (천 단위)")
    print(f"  TradeAmountHelper.normalize_from_kiwoom(114400, 5700, 27730000)")
    print(f"  = 114400 × 1,000,000 = {114_400 * 1_000_000:,}원 (❌ 틀림)")
    print()
    print(f"수정안: raw = 130 (백만원 단위 직접)")
    print(f"  TradeAmountHelper.normalize_from_kiwoom(130, 5700, 27730000)")
    print(f"  = 130 × 1,000,000 = {130 * 1_000_000:,}원 (✓ 맞음)")
