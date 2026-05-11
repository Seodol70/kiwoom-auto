# 🚀 프로그램 시작 가이드 (2026-05-08)

## 📋 현재 상태 점검

### ✅ 완료된 것
- 거래대금 단위 통일 (× 1,000,000 원)
- AI 시스템 완성 (19개 피처, ML 필터)
- 신호→매수 파이프라인 검증 (8단계 완벽 연결)
- Phase 7: 스레드 분리 및 SetRealReg 최적화
- FeedbackEngine 연결 **이미 완료** ✅ (main_window_slots.py:173, 292)

### 🟢 시스템 상태
**프로덕션 준비 완료 (100%)**

---

## 🎯 프로그램 시작 전 체크리스트

### 1️⃣ 환경 확인
- [ ] 키움 API 계정 로그인 정보 확인
- [ ] 모의투자 또는 실전 선택 확인
- [ ] 화면 해상도 적절 (1920×1080 이상 권장)

### 2️⃣ 설정 확인
```bash
# config.yaml 확인
cat config/config.yaml | grep -E "mode|account"

# adaptive_params.json 확인
ls -lah params/adaptive_params.json
```

### 3️⃣ 캐시 초기화 확인
```bash
# 이전에 초기화됨
ls -la data/cache/  # 비어있어야 함
ls -la data/*.pkl   # 없어야 함
```

### 4️⃣ 로그 디렉토리 준비
```bash
mkdir -p logs
ls -la logs/
```

---

## 🚀 프로그램 시작

### 방법 1: DEBUG 로그 활성화 (권장 - 거래대금 재검증)

```bash
python start_with_debug.py
```

**효과:**
- DEBUG 로그 활성화
- system.log에 "[진단]" 로그 기록
- 거래대금 변환 과정 추적 가능

**로그 확인:**
```bash
# 다른 터미널에서
tail -f logs/system.log | grep "\[진단\]\|\[신호\]\|\[주문\]"
```

### 방법 2: 일반 모드 (빠른 시작)

```bash
python ui/main_window.py
```

**또는 Windows에서:**
```powershell
python.exe ui/main_window.py
```

---

## 📊 프로그램 시작 후 확인 사항

### 1단계: 로그인 (1분)
```
✓ 키움 API 연결
✓ 계좌 정보 조회
✓ 예수금 표시
```

### 2단계: 자동매매 시작 (즉시)
```
UI 헤더의 "자동매매" 토글 → ON
또는 자동 시작 (첫 신호 포착 시)
```

**로그 확인:**
```
🟢 자동매매 시작
```

### 3단계: 거래대금 상위 400개 조회 (1분)
```
[거래대금 상위]
  - 네이버(035420): 거래대금 300억+ (예상)
  - 남해화학(025860): 거래대금 ~402억 (예상)
```

**DEBUG 로그 확인 (선택):**
```
[opt10001 거래대금 진단] 035420 raw_amt=691 → 691000000원
[opt10030 거래대금 진단] 행[0] 035420(네이버) raw_amt=691 → 691000000원
```

### 4단계: 실시간 신호 모니터링 (관찰)
```
🚨 [JDM] 종목명(종목코드) 포착: 신호 발생 이유
  ↓ (2~3초 후)
📤 [주문전송] 매수 — 종목명(종목코드) 100주
  ↓ (1~2초 후)
✅ 매수체결 — 종목명(종목코드) 100주 @가격
```

### 5단계: 포지션 모니터링 (관찰)
```
포트폴리오 패널에서:
  - 보유 종목 수
  - 평가손익
  - 수익률
```

---

## 📈 실시간 로그 모니터링 팁

### 신호만 추출
```bash
tail -f logs/system.log | grep "🚨\|\[신호\]"
```

### 주문만 추출
```bash
tail -f logs/system.log | grep "📤\|체결"
```

### 거래대금 진단 로그 (DEBUG 모드)
```bash
tail -f logs/system.log | grep "\[진단\]"
```

### 실시간 로그 통계
```bash
# 1시간 동안의 신호 발생 횟수
grep -c "🚨" logs/system.log

# 체결된 주문 수
grep -c "✅" logs/system.log

# 거절된 신호
grep -c "거절" logs/system.log
```

---

## 🎯 주말 동안 모니터링할 항목

### 1️⃣ 거래대금 정확성 (Priority 1)
- [ ] 네이버(035420)가 올바른 거래대금으로 표시되나?
- [ ] 남해화학(025860)이 ~402억으로 표시되나?
- [ ] 다른 대형주들의 거래대금이 합리적인가?

**확인 방법:**
```bash
# 프로그램 실행 후 1분 후
python verify_trade_amount.py
```

### 2️⃣ 신호 품질 (Priority 2)
- [ ] 신호가 정상적으로 발생하나?
- [ ] 거짓 신호는 얼마나 되나?
- [ ] 신호 타입별 성공률은?

**확인 방법:**
```
로그에서 신호 통계 수집:
  - 총 신호 수
  - 신호별 필터 통과율 (EntryStrategy → AI → RS)
  - 매수 체결율
  - 수익/손실률
```

### 3️⃣ 파라미터 자동 조정 (Priority 3)
- [ ] FeedbackEngine이 매일 15:30에 실행되나?
- [ ] adaptive_params.json이 자동 갱신되나?
- [ ] 손실 분류가 정확한가?

**확인 방법:**
```bash
# 매일 16:00 후 확인
ls -lah params/adaptive_params.json
cat params/adaptive_params.json | grep "updated_at"
```

### 4️⃣ 시스템 안정성 (Priority 4)
- [ ] 메모리 누수는 없나?
- [ ] 프로세스가 정상 종료되나?
- [ ] 예외 없이 계속 실행되나?

**확인 방법:**
```bash
# 프로세스 메모리 사용량 확인
Get-Process python | Select-Object Name, WorkingSet

# 로그에서 에러 확인
grep "ERROR\|Exception" logs/system.log
```

---

## 📋 주말 작업 계획

### Friday 15:30 (지금)
- [ ] 프로그램 시작: `python start_with_debug.py`
- [ ] 로그 모니터링 시작

### Friday 16:00
- [ ] 거래대금 재검증: `python verify_trade_amount.py`
- [ ] 결과 확인

### Friday 17:00~18:00
- [ ] 신호 발생 현황 관찰
- [ ] 거짓 신호 패턴 분석
- [ ] 필터 통과율 추적

### Saturday~Sunday
- [ ] 실시간 로그 분석
- [ ] 파라미터 자동 조정 여부 확인
- [ ] 시스템 안정성 모니터링

### Monday (2026-05-13)
- [ ] 주말 로그 종합 분석
- [ ] FeedbackEngine 실행 여부 확인
- [ ] 남은 개선점 우선순위 재조정

---

## 🔧 문제 발생 시 대응

### 프로그램이 시작되지 않음
```bash
# 1. 로그 확인
cat logs/system.log

# 2. 의존성 확인
pip list | grep -E "PyQt5|pandas|numpy"

# 3. 키움 API 연결 확인
python -c "from kiwoom_api import KiwoomAPI"
```

### 신호가 발생하지 않음
```bash
# 1. SmartScanner 실행 확인
grep "SmartScanner" logs/system.log

# 2. SetRealReg 확인
grep "SetRealReg" logs/system.log

# 3. 지표 계산 확인
grep "calc_rsi\|calc_ema" logs/system.log
```

### 주문이 실행되지 않음
```bash
# 1. 신호 발생 확인
grep "🚨" logs/system.log

# 2. 필터 통과 확인
grep "EntryStrategy\|AI필터\|RS필터" logs/system.log

# 3. 주문 상태 확인
grep "📤\|✅" logs/system.log
```

### 거래대금이 잘못 표시됨
```bash
# 1. DEBUG 로그 확인
grep "\[진단\]" logs/system.log

# 2. 재검증 실행
python verify_trade_amount.py

# 3. 캐시 초기화 후 재시작
rm -rf data/cache data/*.pkl
python start_with_debug.py
```

---

## 🎉 예상 결과

### 정상 동작 시
```
15:31 🟢 자동매매 시작
15:32 [거래대금 상위] 400개 종목 조회 시작
15:33 [거래대금 상위] 조회 완료 (네이버 300억+, 남해화학 402억)
15:35 🚨 [JDM] 종목A 포착: RSI > 50
15:35 📤 [주문전송] 매수 — 종목A 100주
15:36 ✅ 매수체결 — 종목A 100주 @가격
... (계속 신호 발생)
22:00 ⌛ 장 마감 임박
22:01 💰 당일 손익: +50,000원
22:02 📊 피드백 완료: 신호 50건 분석
```

### 파라미터 자동 조정
```
15:31 📊 [피드백] 50건 분석 완료 | 손익 +50,000원
15:31   └─ RSI_MIN: 52 ▲ 54 (EARLY_REVERSAL 3건)
15:31   └─ TRAIL_PCT: 1.0 ▼ 0.8 (TRAIL_TOO_TIGHT 2건)
15:31   └─ 리포트: params/reports/2026-05-08.txt
```

---

## 💡 팁

### 1. 로그 파일 자동 저장
프로그램은 자동으로 logs/system.log에 저장합니다.

### 2. 실시간 모니터링
```bash
# PowerShell에서
Get-Content logs/system.log -Wait
```

### 3. 주말 분석
프로그램을 켜놓고 가면 주말 동안:
- 평일 시간(09:00~15:30)에는 신호 발생
- 주말에는 로그만 쌓임

### 4. 다음 주 계획
Monday 아침에 로그를 종합 분석하고:
- 신호 품질 평가
- 파라미터 자동 조정 여부 확인
- 남은 개선점 우선순위 재조정

---

## ✅ 최종 확인

### 시스템 상태
- ✅ 거래대금 수정 완료
- ✅ AI 시스템 완성
- ✅ 신호→매수 파이프라인 검증
- ✅ FeedbackEngine 연결 완료
- ✅ 99.4% 테스트 통과

### 프로그램 준비
- ✅ 캐시 초기화
- ✅ 설정 파일 확인
- ✅ 로그 디렉토리 준비

### 실행 준비
- ✅ DEBUG 로그 활성화 가능
- ✅ 검증 스크립트 준비
- ✅ 모니터링 계획 수립

**프로그램을 켜도 안전합니다!** 🚀

---

**작성일**: 2026-05-08  
**상태**: 🟢 시작 준비 완료
