# 거래대금 계산 함수 현황 (2026-05-11)

## 📋 Summary

**현재 상태: 함수들이 흩어져 있음 (역할 분담 비효율)**

거래대금을 계산/변환하는 함수가 여러 파일에 분산:
- `_resolve_trade_amount()`: kiwoom_api.py (원본 데이터 변환)
- `format_trade_amount()`: universe.py (UI 표시용 포맷팅)
- `format_trade_amount_growth()`: universe.py (증가율 계산)
- `_trade_amount_diag()`: smart_scanner.py (진단 로그용)

**문제점:**
- 같은 단위 변환 로직이 여러 곳에 산재
- 새로운 변환이 필요하면 여러 파일 수정 필요
- 메인테넌스 어려움 (단위 혼동 위험)

---

## 🔍 현재 함수 목록

### 1️⃣ **`_resolve_trade_amount()` — 핵심 변환**

**파일:** [kiwoom_api.py:237-254](kiwoom_api.py#L237)

```python
def _resolve_trade_amount(raw_amt: int, price: int, volume: int) -> int:
    """거래대금을 원 단위로 정규화한다."""
    if raw_amt <= 0:
        return price * volume
    return raw_amt * 1_000_000  # 백만원 → 원
```

**역할:**
- Kiwoom API의 원본 데이터(백만원) → 원 단위 변환
- opt10001 거래대금 필드 처리
- **호출처:** `kiwoom_api.py:530` (opt10001 조회)

**문제점:**
- opt10030은 별도 처리 (smart_scanner.py:984에서 인라인 계산)

---

### 2️⃣ **opt10030 거래대금 처리 — 인라인**

**파일:** [smart_scanner.py:983-984](smart_scanner.py#L983)

```python
raw_cum_amt = safe_int(fid(13))
real_trade_amt = raw_cum_amt * 1_000_000 if raw_cum_amt > 0 else (price * cum_vol)
```

**역할:**
- 실시간 데이터 FID 13 처리 (누적거래대금)
- `_resolve_trade_amount()`와 동일 로직이 **중복**
- 대체값: `price × volume` (계산 기반)

**문제점:** ⚠️ **같은 로직 2번 구현** (DRY 위반)

---

### 3️⃣ **`format_trade_amount()` — UI 표시**

**파일:** [universe.py:196-207](universe.py#L196)

```python
@staticmethod
def format_trade_amount(amount_won: int) -> str:
    """거래대금을 읽기 편한 한글 형식으로 변환."""
    n = int(amount_won or 0)
    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:.1f}조"
    if n >= 100_000_000:
        return f"{n // 100_000_000:,}억"
    if n >= 10_000:
        return f"{n // 10_000:,}만원"
    return f"{n:,}원"
```

**역할:**
- 원 단위 → 한글 표기 (조, 억, 만원, 원)
- UI 패널, 로그에서 사용
- **호출처:** display.py, smart_scanner.py 진단 로그

---

### 4️⃣ **`format_trade_amount_growth()` — 증가율**

**파일:** [universe.py:263-273](universe.py#L263)

```python
def format_trade_amount_growth(current: int, baseline: Optional[int]) -> str:
    """거래대금 증가율(%) — baseline 이 없거나 0이면 '—'."""
    if baseline is None or baseline <= 0:
        return "증가율(9시대비) —"
    
    if current <= 0:
        return "0% (거래대금 없음)"
    
    growth_pct = (current - baseline) / baseline * 100
    arrow = "▲" if growth_pct > 0 else "▼" if growth_pct < 0 else "→"
    return f"{arrow} {abs(growth_pct):.1f}%"
```

**역할:**
- 9시 대비 거래대금 변화율 계산
- 로그 표시용 ("▲ 15.3%")

---

### 5️⃣ **`_trade_amount_diag()` — 진단 로그**

**파일:** [smart_scanner.py:330-340](smart_scanner.py#L330)

```python
def _trade_amount_diag(self, code: str, amt: int) -> str:
    """Pre-Filter 등 로그용: 조·억 표기 + 9시대비 증가율."""
    a = int(amt or 0)
    self._touch_trade_amt_baseline(code, a)
    
    # 포맷팅 (조·억원 자동 단위 결정)
    ta = self.universe_mgr.format_trade_amount(a)  # ← format_trade_amount() 호출
    
    # 증가율 계산
    gr = format_trade_amount_growth(a, self._amt_baseline.get(code))
    
    return f"{ta} / {gr}"
```

**역할:**
- 진단 로그 포맷 통합 (포맷팅 + 증가율)
- 이미 다른 함수들을 조합해서 호출

---

## 📊 함수 호출 관계도

```
Kiwoom API (백만원 단위)
    ↓
_resolve_trade_amount() [kiwoom_api.py:237]  ← opt10001 처리
    ↓
원 단위 저장 (SnapshotStore)
    ↓
┌─────────────────────────────────────┬──────────────────────────────┐
│                                     │                              │
format_trade_amount()                smart_scanner.py               
[universe.py:196]                    FID 13 처리 [line 984]
UI 표시용                            실시간 데이터 (인라인)
조·억·만원 자동 포맷                  × 1,000,000 중복 계산
│                                     │
↓                                     ↓
_trade_amount_diag()                _touch_trade_amt_baseline()
[smart_scanner.py:330]              [smart_scanner.py:325]
진단 로그 (조·억 + 증가율)            9시 기준값 저장

format_trade_amount_growth()         top_mgr.update()
[universe.py:263]                    [top_volume.py:22]
증가율 계산 (%)                       상위 N종목 추적
```

---

## 🔴 현재 문제점

### 1️⃣ **코드 중복: 원본 데이터 변환**

**중복 구현:**
```python
# kiwoom_api.py:254
return raw_amt * 1_000_000

# smart_scanner.py:984
real_trade_amt = raw_cum_amt * 1_000_000 if raw_cum_amt > 0 else ...
```

→ **같은 로직 2곳에 구현**

---

### 2️⃣ **불일치 위험: opt10001 vs opt10030**

**opt10001:**
- 처리: `_resolve_trade_amount()`에서 `× 1_000_000`
- 호출처: `kiwoom_api.py:530`

**opt10030:**
- 처리: 인라인 계산 `× 1_000_000`
- 호출처: `smart_scanner.py:984`

→ 미래에 단위 변경 시 **두 곳 모두 수정 필요**

---

### 3️⃣ **진단 로그 산재**

**거래대금 진단 로그가 3곳:**
1. `kiwoom_api.py:533` — opt10001 원본
2. `smart_scanner.py:330` — 진단 로그 (조·억·증가율)
3. `scanner/display.py` — UI 표시

→ 로그 경로 추적 어려움

---

## ✅ 권장 개선안

### **[Option A] 단일 통합 함수 (권장)**

**파일:** `scanner/trade_amount.py` (신규)

```python
"""거래대금 계산 및 변환 통합 모듈"""

class TradeAmountHelper:
    """거래대금 단위 변환 및 포맷팅 통합"""
    
    UNIT_WON = 1
    UNIT_MILLION_WON = 1_000_000
    
    @staticmethod
    def normalize_from_kiwoom(raw_amt: int, fallback_price: int = 0, 
                               fallback_volume: int = 0) -> int:
        """
        Kiwoom API 거래대금(백만원) → 원 단위
        
        Args:
            raw_amt: Kiwoom API의 거래대금 (백만원 단위, FID 13/14)
            fallback_price: raw_amt ≤ 0일 때 대체값 (현재가)
            fallback_volume: raw_amt ≤ 0일 때 대체값 (거래량)
        
        Returns:
            거래대금 (원 단위)
        """
        if raw_amt <= 0:
            return fallback_price * fallback_volume if fallback_price and fallback_volume else 0
        return raw_amt * TradeAmountHelper.UNIT_MILLION_WON
    
    @staticmethod
    def to_korean(amount_won: int) -> str:
        """원 → 한글 표기 (조, 억, 만원, 원)"""
        n = int(amount_won or 0)
        if n <= 0: return "0원"
        if n >= 1_000_000_000_000:
            return f"{n / 1_000_000_000_000:.1f}조"
        if n >= 100_000_000:
            return f"{n // 100_000_000:,}억"
        if n >= 10_000:
            return f"{n // 10_000:,}만원"
        return f"{n:,}원"
    
    @staticmethod
    def growth_rate(current: int, baseline: int) -> str:
        """거래대금 증가율 (%)"""
        if baseline <= 0:
            return "—"
        growth_pct = (current - baseline) / baseline * 100
        arrow = "▲" if growth_pct > 0 else "▼" if growth_pct < 0 else "→"
        return f"{arrow} {abs(growth_pct):.1f}%"
    
    @staticmethod
    def diagnostic_string(amount_won: int, baseline: int = 0) -> str:
        """진단용 통합 포맷 (조·억 + 증가율)"""
        korean = TradeAmountHelper.to_korean(amount_won)
        growth = TradeAmountHelper.growth_rate(amount_won, baseline)
        return f"{korean} / {growth}"
```

**마이그레이션:**
```python
# 기존 (분산)
_resolve_trade_amount(raw, price, vol)  # kiwoom_api.py
format_trade_amount(amt)                 # universe.py
format_trade_amount_growth(curr, base)  # universe.py

# 변경 후 (통합)
from scanner.trade_amount import TradeAmountHelper

TradeAmountHelper.normalize_from_kiwoom(raw, price, vol)
TradeAmountHelper.to_korean(amt)
TradeAmountHelper.growth_rate(curr, base)
```

---

### **[Option B] 기존 함수 통합 (최소 변경)**

**파일:** `universe.py` 확장

```python
class UniverseManager:
    # 기존 format_trade_amount(), format_trade_amount_growth() 유지
    
    @staticmethod
    def normalize_trade_amount(raw_kiwoom: int, fallback_price: int = 0, 
                                fallback_volume: int = 0) -> int:
        """통합 정규화 함수 (kiwoom_api.py, smart_scanner.py에서 호출)"""
        if raw_kiwoom <= 0:
            return fallback_price * fallback_volume if fallback_price and fallback_volume else 0
        return raw_kiwoom * 1_000_000
```

**변경점:**
```python
# kiwoom_api.py:254 대체
return UniverseManager.normalize_trade_amount(raw_amt, current_price, volume_v)

# smart_scanner.py:984 대체
real_trade_amt = UniverseManager.normalize_trade_amount(raw_cum_amt, price, cum_vol)
```

---

## 🎯 결론

### 현재 상태: 🟡 **기능은 정확하지만 구조가 산재**

✅ 모든 계산이 원 단위로 일관됨  
❌ 같은 로직이 여러 곳에 구현됨  
❌ 미래 유지보수 어려움  

### 권장:
**[Option A] 신규 `trade_amount.py` 모듈 생성** (장기적으로 좋음)

또는

**[Option B] `universe.py`에 `normalize_trade_amount()` 추가** (빠른 개선)

**시간:** Option B는 10분, Option A는 20분

---

**작성일:** 2026-05-11  
**상태:** 분석 완료 — 개선안 제시됨
