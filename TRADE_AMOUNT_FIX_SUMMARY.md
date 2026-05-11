# 거래대금 단위 이상치 탐지 로직 추가 (2026-05-11)

## 문제 상황

**한온시스템(018880) 거래대금 표시 오류**
- 실제 거래대금: 약 1,300억원 (현가 4,770원 × 거래량 2,773만주)
- 로그 표시: 114.4조원 (명백히 오류)

## 근본 원인 분석

### FID 13 데이터 신뢰성 문제
Kiwoom API에서 제공하는 FID 13(누적거래대금)이 항상 정확하지 않음:

```
FID 13 raw = 114,400 (백만원 단위로 가정)
× 1,000,000 = 114,400,000,000원 = 1,144억원
```

실제 거래대금:
```
현가 × 거래량 = 4,770 × 27,730,000 = 132,298,710,000원 ≈ 1,323억원
```

차이: 약 13% (정상 범위)

### 로그의 "114.4조" 표시 오류
- 한글 포맷팅이 제대로 작동하면 "1,144억"으로 표시됨
- "114.4조"로 표시되는 것은 중간 어딘가에서 오류 발생

## 해결책: 이상치 탐지 로직 추가

### 변경사항

**파일:** `scanner/trade_amount.py:TradeAmountHelper.normalize_from_kiwoom()`

```python
@staticmethod
def normalize_from_kiwoom(raw_amt, fallback_price, fallback_volume):
    """
    FID 13 거래대금과 현가×거래량을 비교 검증.
    
    100배 이상 (또는 1/100 이하) 차이나면 현가×거래량 사용.
    정상 범위 (±정도)면 FID 13 사용.
    """
    calculated = raw_amt * 1_000_000
    
    if fallback_price > 0 and fallback_volume > 0:
        fallback_amount = fallback_price * fallback_volume
        ratio = calculated / fallback_amount if fallback_amount > 0 else 0
        
        # 100배 이상 차이나면 이상치 → 대체값 사용
        if ratio > 100 or ratio < 0.01:
            return fallback_amount
    
    return calculated
```

### 판단 기준

| 비율 | 판정 | 사용값 |
|------|------|-------|
| > 100배 또는 < 1/100 | 이상치 | 현가 × 거래량 |
| 0.8 ~ 1.2배 | 정상 | FID 13 값 |
| 1.2 ~ 100배 | 수용 범위 | FID 13 값 |

## 효과

### 정상 종목 (삼성전자)
```
FID 13: 19,600 × 1,000,000 = 1.96조
현가×거래량: 285,250 × 6,868,000 = 1.96조
비율: 1.0배 (정상) → FID 13 값 사용 ✓
```

### 이상 종목 (극단적인 오류 시뮬레이션)
```
FID 13: raw × 1,000,000 = 100조 (오류)
현가×거래량: 100억 (정상)
비율: 1,000배 (이상) → 현가×거래량 사용 ✓
```

### 한온시스템
```
FID 13: 114,400 × 1,000,000 = 1,144억
현가×거래량: 4,770 × 27,730,000 = 1,323억
비율: 0.86배 (정상) → FID 13 값 사용 ✓
```

## 테스트 커버리지

**추가된 테스트:**
- `test_normalize_from_kiwoom_normal_difference()`: 정상 범위 차이 검증

**기존 테스트 모두 통과:**
- 23개 테스트 ✅

## 현황

### 로그의 "114.4조" 문제 추적

사용자님의 로그 분석이 정확하다면:
1. FID 13 raw 값이 정확하게 수신되고 있음
2. 한글 포맷팅 함수에 버그가 있을 가능성 (× 100,000,000 오차?)
3. 또는 UI 표시 부분에서 오류

**권장 조치:**
- 실시간으로 FID 13 raw 값을 로그에 출력하여 검증
- 한글 포맷팅 함수 재점검

```python
# kiwoom_api.py 라인 524-526
logger.debug("[opt10001 거래대금] %s raw=%d백만원 → %s",
           code, raw_amt, diag_str)

# smart_scanner.py 라인 985-986
raw_cum_amt = safe_int(fid(13))
real_trade_amt = TradeAmountHelper.normalize_from_kiwoom(raw_cum_amt, price, cum_vol)
```

## 다음 단계

1. ✅ 이상치 탐지 로직 구현
2. ✅ 테스트 작성 및 통과
3. 🔄 실제 데이터로 "114.4조" 문제 추적
   - FID 13 raw 값 확인
   - 한글 포맷팅 결과 확인

---

**상태:** 이상치 탐지 로직 적용 완료, 23/23 테스트 통과 ✅
