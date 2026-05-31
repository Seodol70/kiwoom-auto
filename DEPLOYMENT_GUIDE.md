# KIWOOM-AUTO 배포 가이드

## 1. 배포 전 검증

### 1.1 코드 품질 검증

```bash
# 문법 검증
python -m py_compile app/signal_filter.py
python -m py_compile app/exit_validator.py
python -m py_compile app/trading_controller.py
python -m py_compile ui/signal_manager.py
```

### 1.2 전체 테스트 실행

```bash
# pytest 설치
pip install pytest

# 전체 테스트 실행
pytest tests/test_signal_filter_chain.py tests/test_exit_validator_chain.py tests/test_phase3_integration_full.py -v
```

**예상 결과**: 42/42 PASS

### 1.3 의존성 확인

```bash
# 필수 패키지 설치
pip install PyQt5 numpy pandas scikit-learn
```

---

## 2. 배포 전 준비

### 2.1 Git 상태 확인

```bash
# 커밋 상태 확인
git status

# 최근 커밋 확인
git log --oneline -10

# 모든 변경사항이 커밋되었는지 확인
git diff-index --quiet HEAD --
```

### 2.2 배포 버전 결정

```
버전: v2.0.0 (Phase 2 리팩토링 완료)
태그: release-v2.0.0
```

### 2.3 릴리스 태그 생성

```bash
# 로컬에서 태그 생성
git tag -a release-v2.0.0 -m "Phase 2 리팩토링 & 구식 메소드 제거 완료"

# 태그 확인
git tag -l
```

---

## 3. 배포 실행 (GitHub Release)

### 3.1 GitHub Release 생성

```bash
# GitHub CLI 설치 (필요시)
# choco install gh (Windows)
# brew install gh (macOS)

# 로그인
gh auth login

# Release 생성
gh release create release-v2.0.0 \
  --title "KIWOOM-AUTO v2.0.0 - Phase 2 리팩토링 완료" \
  --notes-file RELEASE_NOTES.md
```

### 3.2 변경 로그 추가

Release Notes 자동 생성:
```bash
gh release create release-v2.0.0 \
  --generate-notes \
  --title "KIWOOM-AUTO v2.0.0"
```

---

## 4. 배포 후 검증

### 4.1 배포 상태 확인

```bash
# Release 확인
gh release view release-v2.0.0

# 태그 확인
git describe --tags --always
```

### 4.2 최종 테스트

배포 후 이 항목들을 검증합니다:

- ✅ 신호 진입 필터링 정상 작동
- ✅ 청산 검증 정상 작동
- ✅ 위험 관리 정상 작동
- ✅ EOD 상태 전이 정상 작동
- ✅ 전체 거래 흐름 정상 작동

---

## 5. 롤백 절차

문제 발생 시 이전 버전으로 롤백:

```bash
# 이전 태그 확인
git tag -l

# 이전 버전으로 체크아웃
git checkout 04b96fa

# 또는 이전 Release 복구
gh release delete release-v2.0.0
git tag -d release-v2.0.0
git push origin --delete release-v2.0.0
```

---

## 6. 배포 체크리스트

배포 전 이 항목들을 확인합니다:

### 코드 품질
- [ ] 모든 파일 문법 검증 완료
- [ ] 42/42 테스트 통과
- [ ] 의존성 설치 완료

### 문서화
- [ ] RELEASE_NOTES.md 작성 완료
- [ ] DEPLOYMENT_GUIDE.md 작성 완료
- [ ] API 문서 업데이트

### Git 상태
- [ ] 모든 변경사항 커밋됨
- [ ] 깔끔한 커밋 히스토리
- [ ] 릴리스 태그 준비

### 배포 실행
- [ ] GitHub Release 생성
- [ ] 변경 로그 작성
- [ ] 배포 후 검증 완료

---

## 7. 배포 후 모니터링

### 7.1 로그 확인

배포 직후 이 로그들을 확인합니다:

```bash
# 신호 처리 로그
- [SignalFilterChain] 필터 통과/거절
- [ExitValidatorChain] 청산 판정 결과

# 주의 사항
- 필터 거절 빈도 정상인지 확인
- 청산 실행 로그 정상인지 확인
```

### 7.2 성능 모니터링

```
메트릭:
- 신호 처리 대기시간: < 100ms
- 청산 판정 대기시간: < 100ms
- 메모리 사용량: < 500MB
```

---

## 8. 설치 지침 (사용자용)

### 8.1 사전 요구사항

- Python 3.8 이상
- Windows/macOS/Linux

### 8.2 설치 방법

```bash
# 저장소 클론
git clone https://github.com/your-repo/kiwoom-auto.git
cd kiwoom-auto

# 브랜치 확인
git checkout release-v2.0.0

# 의존성 설치
pip install -r requirements.txt

# 테스트 실행
pytest tests/ -v
```

### 8.3 설정

```bash
# 설정 파일 생성
cp config.example.yaml config.yaml

# 설정 편집
# - 계좌 번호
# - API 키
# - 거래 파라미터

# 실행
python main.py
```

---

## 9. FAQ

### Q: 배포 중 문제가 생기면?
A: 롤백 절차를 따라 이전 버전으로 복구하세요.

### Q: 새로운 기능을 배포하려면?
A: Phase 3 계획에 따라 진행하고, 같은 배포 절차를 따르세요.

### Q: 테스트가 실패하면?
A: 배포를 중단하고 문제를 해결한 후 새 커밋을 추가하세요.

---

**배포 날짜**: 2026-05-29  
**버전**: v2.0.0  
**상태**: ✅ 프로덕션 준비 완료
