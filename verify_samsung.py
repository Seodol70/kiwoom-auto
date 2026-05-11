#!/usr/bin/env python3
"""삼성전자 거래대금 단위 검증"""

# 로그에서 본 실제 값
trade_amt_shown = 19_600_000_000_000  # 19.6조원
current_price = 285_250

# 거래량 역산
volume = trade_amt_shown // current_price

print("=" * 60)
print("삼성전자(005930) 거래대금 검증")
print("=" * 60)
print(f"현재가: {current_price:,}원")
print(f"로그에 표시된 거래대금: {trade_amt_shown:,}원 (19.6조)")
print(f"역산된 거래량: {volume:,}주")
print()

print("=" * 60)
print("FID 13 raw 값이 얼마였는지 역산")
print("=" * 60)

# 만약 raw × 1,000,000 = 19.6조이면
raw_if_millions = trade_amt_shown // 1_000_000
print(f"만약 백만원 단위(raw × 1,000,000)라면:")
print(f"  FID 13 raw = {raw_if_millions:,}")
print()

# 만약 raw × 1,000 = 19.6조이면
raw_if_thousands = trade_amt_shown // 1_000
print(f"만약 천원 단위(raw × 1,000)라면:")
print(f"  FID 13 raw = {raw_if_thousands:,}")
print()

print("=" * 60)
print("현실성 검증")
print("=" * 60)
print(f"계산된 거래량: {volume:,}주")
print(f"실제 삼성전자 평균 일일 거래량: 약 2,000~3,000만주")
print()

if 20_000_000 <= volume <= 100_000_000:
    print("[OK] 현실적인 거래량 범위")
else:
    print("[ERROR] 비현실적인 거래량 (너무 큼)")
print()

# 최근 실제 삼성전자 거래대금 추정
print("=" * 60)
print("최근 삼성전자 실제 거래대금 추정 (2026-05-11)")
print("=" * 60)
realistic_volume = 30_000_000  # 약 3천만주
realistic_trade_amt = realistic_volume * current_price
print(f"예상 거래량: {realistic_volume:,}주")
print(f"예상 거래대금: {realistic_trade_amt:,}원 ({realistic_trade_amt/100_000_000:.1f}조)")
print()

print("=" * 60)
print("결론")
print("=" * 60)
print(f"로그의 19.6조 거래대금으로 계산하면 거래량이 {volume:,}주")
print(f"실제 거래대금은 약 {realistic_trade_amt:,}원 ({realistic_trade_amt/100_000_000:.1f}조) 정도가 현실적")
print()
print("▶ FID 13 raw 값이 19,600이었다면:")
print(f"  - raw × 1,000,000 = {19_600 * 1_000_000:,}원 (1,960조 - 너무 큼)")
print(f"  - raw × 1,000 = {19_600 * 1_000:,}원 (1,960만원 - 너무 작음)")
print()
print("▶ FID 13 raw 값이 196,000이었다면:")
print(f"  - raw × 1,000,000 = {196_000 * 1_000_000:,}원 (1.96조 - 현실적)")
print(f"  - raw × 1,000 = {196_000 * 1_000:,}원 (1.96억원 - 너무 작음)")
print()
print("결론: raw값이 196,000 정도이고, × 1,000,000(백만원)으로 계산하는 것이 맞음")
