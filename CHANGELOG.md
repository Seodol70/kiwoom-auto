# Changelog

모든 주요 변경사항을 이 파일에 기록합니다.

## [2.0.0] - 2026-05-29

### 🎉 Phase 2 리팩토링 완료 + 구식 메소드 제거

이번 릴리스는 KIWOOM-AUTO의 핵심 청산 로직을 Composite 패턴으로 완전히 리팩토링했습니다.

#### Added

**신규 파일:**
- `app/signal_filter.py` (388줄) - SignalFilterChain 구현
  - 9개 필터를 Composite 패턴으로 통합
  - 진입 신호 검증 자동화
  
- `app/exit_validator.py` (349줄) - ExitValidatorChain 구현
  - 7개 검증자를 Composite 패턴으로 통합
  - 청산 로직 중앙화
  
- `tests/test_signal_filter_chain.py` (407줄)
  - 9개 필터 테스트 + 체인 통합 테스트
  - 17/17 PASS
  
- `tests/test_exit_validator_chain.py` (431줄)
  - 7개 검증자 테스트 + 체인 통합 테스트 + 집계자 테스트
  - 22/22 PASS

**신규 메소드:**
- `OrderManager._rebuild_positions_from_holdings()` - Position 재구성 통합
- `ExitDecisionAggregator` - 청산 로직 중앙화

#### Changed

**SignalFilterChain (9개 필터):**
1. OverheatPullbackFilter - OVERHEAT_PULLBACK 신호 차단
2. MockSignalFilter - 테스트 신호(000003) 차단
3. OpeningTimeFilter - 09:00-10:00 1회/60초 제한
4. WeakSignalFilter - 09:30 이후 trend_level < 2 차단
5. EntryStrategyFilter - strategy.should_entry() 위임
6. InvestorFilter - 외국인+기관 순매도 > -1000주 차단
7. NewsFilter - 뉴스 감정 분석
8. AIFilter - ML 모델 스코링
9. RSFilter - 상대강도 < 임계값 차단

**ExitValidatorChain (7개 검증자):**
1. StrategyExitValidator - strategy.should_exit()/should_partial_exit() 위임
2. EODDayTargetValidator - 일중 수익률 목표 (손절/익절/분할익절)
3. EODGapValidator - 갭 분류 (갭다운/갭업/보합)
4. EODTrendBreakValidator - 일봉 정배열 파괴 감지
5. EODTimecutValidator - 09:30 타임컷 + 최소수익 확인
6. Phase1LiquidationValidator - 10:30 Phase1 강제정리
7. MarketCloseValidator - 15:10 장 마감 청산

**리팩토링:**
- `handle_signal()`: 211줄 → 40줄 (80% 축소)
- `tick_exit_check()`: ExitValidatorChain + ExitDecisionAggregator 사용

#### Removed

**제거된 메소드 (7개, 272줄):**

*Phase 1: MarketScheduler 신호 통합*
- ✗ `check_eod_daytime_targets()` (39줄) - EODDayTargetValidator로 통합
- ✗ `check_overnight_gap()` (28줄) - EODGapValidator로 통합
- ✗ `check_overnight_trend_break()` (28줄) - EODTrendBreakValidator로 통합
- ✗ `liquidate_phase1_positions()` (19줄) - Phase1LiquidationValidator로 통합
- ✗ `check_overnight_timecut()` (77줄) - EODTimecutValidator로 통합

*Phase 2: 중복 청산 메소드 제거*
- ✗ `check_and_exit_all()` (44줄) - tick_exit_check()로 통합

*Phase 3: RiskManager 신호 경로 통일*
- ✗ `liquidate_all_positions()` (37줄) - tick_exit_check()로 통합

#### Fixed

**버그 수정:**
- Position 메타데이터 보존 문제 해결
- 신호 필터 우회 가능성 제거
- 청산 검증 순서 명확화
- 상태 전이(overnight_held) 로직 정확화

#### Security

**보안 개선:**
- 필터 체인 검증으로 입력값 유효성 강화
- 청산 로직 중앙화로 예외 상황 처리 통일
- 신호 경로 단일화로 보안 감시 용이

---

## [1.9.x] - 2026-05-26

### Previous versions

이전 버전의 변경사항은 git log를 참조하세요.

---

## 배포 정보

| 항목 | 수치 |
|------|------|
| **전체 커밋** | 10개 |
| **구현 파일** | 4개 수정 |
| **테스트 파일** | 4개 추가 |
| **테스트 케이스** | 42개 (100% PASS) |
| **코드 라인** | +1,575줄 (테스트 포함) |
| **제거 라인** | -272줄 |
| **아키텍처** | Composite 패턴 |
| **호환성** | 100% (하위 호환성 유지) |

---

## 커밋 히스토리

### Phase 2 (리팩토링)
```
390ceed - Step 1: 포지션 재구성 코드 중복 제거
103d46b - Step 2: SignalFilterChain 패턴 구현 (9단계 필터 추출)
04b96fa - Step 3: ExitValidatorChain 패턴 구현 (청산 로직 추출)
```

### 구식 메소드 제거
```
66e8358 - Step 3 Phase 1: MarketScheduler 신호 통합 및 구식 메소드 제거
84bf7cf - Step 3 Phase 2: check_and_exit_all() 메소드 제거 및 통합
dd944e8 - Step 3 Phase 3: RiskManager 신호 경로 통일 및 liquidate_all_positions() 제거
```

---

## 알려진 이슈

현재 알려진 이슈는 없습니다. 발견 시 GitHub Issues를 통해 보고해주세요.

---

## 향후 계획

### Phase 3: 신규 기능 개발
- AI 필터 고도화
- 위험 관리 개선
- 신규 검증자 추가

### 성능 최적화
- 쿼리 성능 개선
- 메모리 사용량 최적화
- 신호 처리 대기시간 단축

### 추가 기능
- 시나리오 기반 테스트
- 스트레스 테스트
- 사용자 문서화

---

## 기여 가이드

새로운 기능이나 버그 수정을 기여하려면:

1. Fork 한다
2. Feature 브랜치 생성 (`git checkout -b feature/AmazingFeature`)
3. 변경사항 커밋 (`git commit -m 'Add AmazingFeature'`)
4. 브랜치에 Push (`git push origin feature/AmazingFeature`)
5. Pull Request 생성한다

---

## 라이선스

이 프로젝트는 MIT 라이선스 하에 배포됩니다. 자세한 내용은 LICENSE 파일을 참조하세요.

---

**마지막 업데이트**: 2026-05-29  
**버전**: v2.0.0  
**상태**: ✅ 프로덕션 준비 완료
