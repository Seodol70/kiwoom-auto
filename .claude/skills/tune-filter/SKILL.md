---
metadata:
  name: tune-filter
  description: 거래 분석 데이터를 기반으로 필터 파라미터 최적화 제안. /analyze-trades 직후 자동으로 호출되어 BREAKOUT 허위양성, 체결강도 수준, 냉각기 기간 등 조정안을 제시하고 사용자 승인 후 scanner/config.py에 적용.
  model: haiku
---

## 입력

선택적 인자:
- 없음: 최근 분석(memory/phase9_*_analysis.md)의 조정안 자동 추출 및 제시
- 필터명: 특정 필터만 상세 분석 (예: `/tune-filter breakout`, `/tune-filter chejan`)

## 작업 흐름

1. **최근 분석 파일 로드**: memory/phase9_*_analysis.md 중 가장 최신 파일 읽음
2. **조정 필요 여부 판단**: 데이터 크기, 승률, 차단 패턴 분석
3. **파라미터별 제안 생성**: 각 필터에 대해 현재값 → 권장값 + 근거 제시
4. **사용자 승인**: 어떤 조정을 적용할지 선택
5. **자동 적용**: scanner/config.py 수정 + 변경 이력 메모리 저장

## 주의사항

- 데이터 부족 시 제안 안 함 (최소 5일, 50건 거래 권장)
- 사용자 확인 없이 절대 자동 적용 금지
- 각 조정안에는 근거 수치 반드시 포함
- 변경 이력은 memory/phase9_*_tuning_log.md에 기록

## 조정 대상 파라미터

**Scanner Config 우선순위:**
1. `breakout_pullback_from_high_pct` (고점 대비 조정율) — 허위양성 높을 때 강화
2. `min_chejan_strength_*` (체결강도 하한) — 약세 신호 필터링
3. `cooldown_duration_minutes` (냉각기 기간) — 연속 손절 빈도 대비 조정
4. `ai_filter_threshold` (AI 필터 임계값) — 예상승률 기준
5. `rs_filter_threshold` (RS 필터 임계값) — 상대강도 기준

**Strategy Config 검토:**
- `take_profit_pct` / `stop_loss_pct` — 손익률 목표 vs 실제 청산 비율

## 출력 형식

```
다음 조정안이 도출되었습니다:

1. breakout_pullback_from_high_pct: 5.0% → 3.0%
   근거: BREAKOUT 6건 중 5건 손절 (83% 허위양성)
         고점 대비 -3% 이내 신호만 승률 높음 (KBI메탈 +0.61%)
   영향: BREAKOUT 신호 ~30% 감소, 하지만 신뢰도 +50%p

2. min_chejan_strength_morning: 90% → 110%
   근거: 현재 90%는 너무 관대, 약세 신호까지 포착
         BREAKOUT 차단 신호 5건 모두 chejan < 100%
   영향: 신호 ~20% 감소, 거짓 신호 차단

적용할까요?
[1] 모두 적용
[2] 1번만
[3] 2번만
[4] 기록만 (적용 안 함)
```

## 메모리 저장

변경 시 `phase9_YYYY-MM-DD_tuning_log.md` 자동 생성:

```markdown
---
name: phase9_YYYY-MM-DD_tuning
description: YYYY-MM-DD 필터 조정 이력 — 변경된 파라미터 + 적용 결과
metadata:
  type: project
---

## 필터 조정 이력 — YYYY-MM-DD

### 적용된 조정
1. breakout_pullback_from_high_pct: 5.0% → 3.0%
   적용일: 2026-05-12 15:30
   근거: BREAKOUT 83% 허위양성

### 다음 검토 예정
1. chejan_strength: 90% → 110% (데이터 5일 추가 수집 후)

### 적용 결과 (2~3일 후)
- BREAKOUT 신호: 10/day → 7/day (30% 감소)
- 승률: 0% → ?? % (확인 중)
```

`MEMORY.md`에 인덱스 추가:
```
- [필터 조정 이력](phase9_YYYY-MM-DD_tuning.md) — breakout: 5.0→3.0, 체결강도 검토 중
```
