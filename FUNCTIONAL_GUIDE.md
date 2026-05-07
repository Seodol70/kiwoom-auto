# Kiwoom 자동매매 시스템 — 기능 설명서

**버전**: 2.0 (Phase 6 완료)  
**마지막 업데이트**: 2026-05-07  
**목적**: 키움증권 Open API를 활용한 주식 자동 매매 시스템

---

## 📋 목차

1. [시스템 개요](#시스템-개요)
2. [인프라 및 환경 요구사항](#인프라-및-환경-요구사항)
3. [주요 기능](#주요-기능)
4. [아키텍처](#아키텍처)
5. [유즈케이스](#유즈케이스)
6. [시퀀스 다이어그램](#시퀀스-다이어그램)
7. [모듈별 상세 설명](#모듈별-상세-설명)
8. [데이터 흐름](#데이터-흐름)
9. [설정 및 파라미터](#설정-및-파라미터)

---

## 시스템 개요

### 목적
**키움 자동매매 시스템**은 기술적 지표와 시장 조건을 분석하여 자동으로 매수/매도를 실행하고, 실시간으로 포지션을 관리하는 Python 기반 시스템입니다.

### 핵심 특징
- **키움 Open API 통합**: 실시간 시세, 주문 실행, 잔고 조회
- **멀티스레드 스캔**: 전 종목을 병렬로 감시 (1초 주기)
- **스마트 필터링**: 기술적 지표 기반 자동 신호 생성
- **리스크 관리**: 손절, 익절, 일일 손실한도, 포지션 제한
- **세션 영속성**: 자동매매 중단 시 보유 포지션/손익 자동 복구
- **실시간 모니터링**: PyQt5 GUI 대시보드

---

## 인프라 및 환경 요구사항

### 개발 환경

#### Python 버전

- **권장**: Python 3.11 (3.9 이상)
- **이유**: PyQt5, pandas, numpy의 최신 버전 호환성 보장

#### 운영 환경

- **OS**: Windows 10 이상 (권장: Windows 11)
- **메모리**: 최소 4GB RAM (권장: 8GB 이상)
- **저장공간**: 최소 2GB (로그 + DB 포함)
- **네트워크**: 안정적인 인터넷 연결 (키움 API 통신)

### 필수 라이브러리

```
streamlit>=1.35.0              # 웹 대시보드 프레임워크
rich>=13.7.0                   # 터미널 시각화 (표, 텍스트)
streamlit-autorefresh>=1.0.1   # 자동 새로고침
plotly>=5.22.0                 # 대화형 차트
pandas>=2.2.0                  # 데이터 분석 (SnapshotStore)
numpy>=1.26.0                  # 수치 연산
python-dotenv>=1.0.0           # 환경 변수 관리 (.env)
PyQt5>=5.15.0                  # GUI 프레임워크 (메인 대시보드)
pyqtgraph>=0.13.0              # PyQt5 기반 그래프
pywin32>=306                   # Windows API (키움 COM 객체)
requests>=2.31.0               # HTTP 요청 (API 호출)
scikit-learn>=1.3.0            # ML 기반 신호 필터
joblib>=1.3.0                  # 병렬 처리
```

**설치 방법**:

```bash
# 가상환경 생성 (권장)
python -m venv venv32

# 활성화
venv32\Scripts\activate

# 라이브러리 설치
pip install -r requirements.txt
```

### Windows 환경 설정 (32bit Python 필수)

#### ⚠️ 중요: 32bit Python이 필요한 이유

**키움 Open API는 32bit Windows COM 객체입니다.**

- 64bit Python으로는 키움 API 호출 불가능
- `pywin32` 모듈로 COM 객체에 접근하려면 Python과 COM이 같은 비트수여야 함
- 따라서 **반드시 32bit Python을 사용**해야 합니다

#### 1단계: Miniconda 32bit 설치

**왜 Miniconda인가?**

- 가볍고 32bit 버전을 공식 제공
- numpy, pandas 등 과학 라이브러리 사전 컴파일 제공
- 패키지 설치 실패 가능성 최소화

**설치 절차**:

```
① 공식 페이지 접속: https://docs.conda.io/projects/miniconda/en/latest/

② 32bit 버전 다운로드:
   - Windows → Miniconda3-latest-Windows-x86.exe (32bit)
   - 주의: Windows-x86_64.exe는 64bit (❌ 사용하면 안 됨)

③ 설치 실행 (관리자 권한):
   - 설치 위치: C:\miniconda32 (기본값)
   - ✅ "Add Miniconda3 to my PATH" 체크
   - ✅ "Register Miniconda3 as my default Python 3.x" 체크

④ 설치 완료 후 명령프롬프트 재시작
```

**설치 확인**:

```powershell
# Python 버전 확인 (32bit 여부)
python --version
python -c "import struct; print(struct.calcsize('P') * 8)"
# 출력: 32 (32bit) ✅
# 출력: 64 (64bit) ❌ 재설치 필요
```

#### 2단계: 32bit 가상환경 생성

```powershell
# 기본 설치된 Python이 32bit인지 확인
python -c "import struct; print(f'Python {struct.calcsize(\"P\") * 8}bit')"

# 32bit 가상환경 생성
python -m venv venv32

# 가상환경 활성화
venv32\Scripts\activate

# (가상환경 내) pip 업그레이드
python -m pip install --upgrade pip setuptools wheel
```

**가상환경 구조**:

```
venv32/
├── Scripts/           # 실행 파일
│   ├── python.exe     # 32bit Python 인터프리터
│   ├── pip.exe        # 32bit pip
│   └── activate.bat   # 활성화 스크립트
├── Lib/              # 사이트 패키지
│   └── site-packages/
└── pyvenv.cfg       # 가상환경 설정
```

#### 3단계: 필수 라이브러리 설치 (32bit 호환)

```powershell
# 가상환경 활성화
venv32\Scripts\activate

# 핵심 라이브러리 먼저 설치 (numpy, pandas)
pip install numpy==1.26.0 pandas==2.2.0

# 키움 API 연동 필수
pip install pywin32==306

# PyQt5 GUI (32bit 특화 빌드 필요)
pip install PyQt5==5.15.0
pip install pyqtgraph==0.13.0

# 나머지 라이브러리
pip install -r requirements.txt

# pywin32 COM 등록 (매우 중요!)
python -m pip install --upgrade pywin32
python Scripts/pywin32_postinstall.py -install
```

**⚠️ pywin32 COM 등록이 실패하면**:

```powershell
# 명령프롬프트 (관리자 권한) 에서 수동 등록
python "C:\Users\{username}\AppData\Local\Programs\Python\Python311-32\Scripts\pywin32_postinstall.py" -install

# 또는 Python이 설치된 경로에서 직접
python C:\miniconda32\Scripts\pywin32_postinstall.py -install
```

#### 4단계: 키움 Open API 설치

```
① 키움증권 홈페이지에서 키움 Open API 다운로드
   (링크: https://openapi.kiwoom.com.kr)

② OpenAPI.exe 실행 (관리자 권한)
   - 추출 위치: C:\KHOpenAPI\ (기본 권장)

③ 설치 완료 후 확인:
   - 폴더 확인: C:\KHOpenAPI\ 존재?
   - 레지스트리: HKEY_LOCAL_MACHINE\SOFTWARE\Classes\KHOpenAPI.KHOpenAPIMgr
   - COM 객체 등록 완료

④ Windows 레지스트리에서 COM 객체 확인:
   - regedit.exe 실행
   - 경로: HKEY_CLASSES_ROOT\KHOpenAPI.KHOpenAPIMgr
   - 존재하면 설치 완료
```

**키움 API 바이너리 확인**:

```powershell
# C:\KHOpenAPI\ 폴더에 다음 파일들이 있는지 확인
dir C:\KHOpenAPI\
# 주요 파일:
#   - KHOpenAPI.dll (32bit, COM 객체)
#   - KHOpenAPI.exe (실행 파일)
#   - 여러 개의 데이터 파일 (.dat)
```

#### 5단계: Python에서 키움 API 테스트

```python
# test_kiwoom_import.py
import sys
print(f"Python {sys.version}")
print(f"Bit: {sys.maxsize > 2**32 and '64bit' or '32bit'}")

try:
    import win32com.client
    print("✅ pywin32 설치 완료")
    
    # 키움 COM 객체 생성
    kiwoom = win32com.client.Dispatch("KHOpenAPI.KHOpenAPIMgr")
    print("✅ 키움 COM 객체 생성 성공")
    
except Exception as e:
    print(f"❌ 오류: {e}")
```

**실행**:

```powershell
venv32\Scripts\activate
python test_kiwoom_import.py
```

**예상 출력**:

```
Python 3.11.0 (32bit) (main, ...)
Bit: 32bit
✅ pywin32 설치 완료
✅ 키움 COM 객체 생성 성공
```

#### 6단계: 환경 변수 설정

프로젝트 루트 디렉토리에 `.env` 파일 생성:

```
# .env 파일
LOG_DIR=logs
DB_PATH=data/positions.db
CONFIG_FILE=config.json
KIWOOM_API_PATH=C:\KHOpenAPI

# 알림 설정 (선택)
TELEGRAM_BOT_TOKEN=<YOUR_TOKEN>
TELEGRAM_CHAT_ID=<YOUR_ID>
```

#### 7단계: 관리자 권한 실행

```
키움 Open API는 보안상 관리자 권한이 필요합니다.

방법 A) 명령프롬프트 (관리자 권한):
  1. Windows + X → "Windows Terminal(관리자)" 선택
  2. 프로젝트 폴더로 이동
  3. venv32\Scripts\activate
  4. python run_qt.py

방법 B) 바탕화면 바로가기:
  1. run_qt.py → 마우스 우클릭 → "바로가기 만들기"
  2. 바로가기 속성 → "고급"
  3. ✅ "관리자 권한으로 실행" 체크 → 확인

방법 C) 배치 파일 (권장):
  run_admin.bat 파일 생성:
  
  @echo off
  @setlocal enabledelayedexpansion
  cd /d "%~dp0"
  call venv32\Scripts\activate.bat
  python run_qt.py
  pause
```

#### 8단계: DirectX/GPU 설정 (선택사항)

```
PyQt5 및 pyqtgraph 성능 최적화:

1. Windows 그래픽 설정:
   - 설정 → 시스템 → 디스플레이 → 그래픽
   - 또는 Windows + I → 검색: 그래픽 설정

2. Python.exe를 위한 GPU 할당:
   - "찾아보기" → venv32\Scripts\python.exe 선택
   - 추가 → 옵션 → 고성능 선택

3. (선택) NVIDIA GPU 가속:
   - nvidia-smi로 GPU 확인
   - CUDA Toolkit 11.8+ 설치 (선택)
```

### 32bit 환경 트러블슈팅

#### Q1: "ModuleNotFoundError: No module named 'win32com.client'"

```
원인: pywin32 설치 후 COM 등록 스크립트가 실행되지 않음

해결책:
1. 명령프롬프트 (관리자 권한) 실행
2. 가상환경 활성화: venv32\Scripts\activate
3. COM 등록: python -m pip install --upgrade pywin32
4. 등록 스크립트 실행:
   python Scripts/pywin32_postinstall.py -install
5. Python 재시작
```

#### Q2: "win32com.client.Dispatch() 실패 - 오류 메시지: 'KHOpenAPI.KHOpenAPIMgr'"

```
원인: 키움 API가 설치되지 않았거나 64bit Python에서 32bit COM 접근 시도

해결책:
1. Python 비트수 확인:
   python -c "import struct; print(struct.calcsize('P') * 8)"
   → 반드시 32 출력

2. 키움 API 설치 확인:
   dir C:\KHOpenAPI\
   → 파일 존재 확인

3. 레지스트리 확인 (regedit.exe):
   HKEY_CLASSES_ROOT → KHOpenAPI.KHOpenAPIMgr 검색
   → 존재하면 정상

4. 키움 API 재설치 (관리자 권한)
```

#### Q3: "ImportError: DLL load failed"

```
원인: numpy/pandas 등 C 확장 라이브러리가 32bit 바이너리가 아님

해결책:
1. 라이브러리 재설치 (32bit conda 채널 사용):
   pip install --force-reinstall --no-cache-dir numpy pandas
   
2. 또는 미니콘다 채널 사용:
   conda install numpy=1.26.0 pandas=2.2.0
```

#### Q4: "venv32 활성화 후에도 64bit Python 사용됨"

```
원인: 경로에 다른 Python이 먼저 등록되어 있음

확인:
where python
→ 첫 번째 경로가 venv32 내부여야 함

해결책:
1. 절대 경로 사용:
   C:\Users\username\path\to\venv32\Scripts\python.exe run_qt.py

2. 또는 환경 변수 PATH 정리:
   제어판 → 시스템 → 환경 변수
   → PATH에서 다른 Python 경로 제거
```

### 키움 API 호환성

| 항목 | 버전 | 상태 |
| --- | --- | --- |
| 키움 Open API | 1.0 이상 | ✅ 지원 |
| 키움증권 모의투자 | v3.0 | ✅ 지원 |
| 국내주식 실시간 | opt10001 | ✅ 지원 |
| 주식 기본정보 | opt10030 | ✅ 지원 |
| 주문 체결 정보 | opt10010 | ✅ 지원 |

### 성능 사양

| 항목 | 범위 | 비고 |
| --- | --- | --- |
| 동시 감시 종목 | ~200개 | SetRealReg 한계 |
| 신호 생성 주기 | 1초 | 시스템 부하에 따라 변동 |
| 포지션 추적 | 5개 제한 | 설정으로 조정 가능 |
| DB 용량 | ~50MB/월 | 로그 + 주문 기록 기준 |
| 메모리 사용 | ~400-600MB | pandas DataFrame 캐시 포함 |

### 설치 체크리스트

- [ ] Python 3.11+ 설치 완료
- [ ] requirements.txt 라이브러리 설치 완료
- [ ] 키움 Open API 설치 완료 (C:\KHOpenAPI\)
- [ ] .env 파일 생성 완료
- [ ] 관리자 권한으로 Python 실행 확인
- [ ] 키움증권 계정 로그인 가능 확인
- [ ] 포트 8501 (Streamlit) 사용 가능 확인

---

## 주요 기능

### 1. **실시간 시장 감시 (SmartScanner)**
```
전 종목 감시 → 신호 판단 → 매수 신호 → 자동 주문 실행
```
- **3단계 필터링**:
  1. Pre-Filter (09:00): 거래대금 상위 200개 종목 선정
  2. Real-time Scan (1초): 실시간 데이터로 신호 판단
  3. Final Signal: 신호 조건 확인 후 주문

### 2. **기술적 지표 분석 (지표 평가)**
- **MA (이동평균)**: MA7, MA15 추세 확인
- **RSI (상대강도지수)**: 시간대별 오버솔드/오버바이 판정
- **Bollinger Bands**: 변동성 기반 변곡점 감지
- **EMA (지수이동평균)**: 단기 추세 강도

### 3. **주문 실행 및 포지션 관리**
- 최대 5개 포지션 보유 제한
- 자동 손절 (-1.5%), 익절 (3.0%)
- 트레일링 스탑 (조기 청산 방지)
- 실시간 PnL 추적

### 4. **리스크 관리 (RiskManager)**
- 일일 손실 한도 설정 및 추적
- 신규 진입 잠금 (손실 누적 시)
- 수급 강도 필터 (과도한 수요 차단)
- 거래량 검증

### 5. **시장 스케줄링 (MarketScheduler)**
- 09:00: 자동매매 ON (Safety Switch)
- 09:00-14:30: 신호 감시 및 매수
- 15:19-15:20: **자동 야간 청산** (모든 보유 포지션 시장가 매도)
- 15:20: 자동매매 OFF

### 6. **세션 영속성**
- 자동매매 중단 → 보유 포지션/손익 DB에 저장
- 재시작 시 이전 상태 자동 복구
- 일일 손익 자동 추적

### 7. **모니터링 대시보드**
- 실시간 포지션 현황 (진입가, 손익, 수익률)
- 시장 조건 표시 (마켓인덱스, 수급 강도)
- 신호 로그 기록
- 건강도 진단 (프리징, 연결 상태)

---

## 아키텍처

### 계층 구조

```
┌─────────────────────────────────────────────────┐
│         UI Layer (PyQt5 MainWindow)             │
│  - 대시보드, 포지션 뷰, 신호 로그, 차트         │
└────────────────┬────────────────────────────────┘
                 │
┌─────────────────────────────────────────────────┐
│    Application Layer (TradingController)        │
│  - 신호 처리, 주문 검증, 리스크 체크              │
└────────────────┬────────────────────────────────┘
                 │
┌─────────────────────────────────────────────────┐
│   Business Logic Layer                          │
│  ├─ SmartScanner (실시간 신호 생성)             │
│  ├─ RiskManager (손실 관리)                     │
│  ├─ OrderManager (주문 실행)                    │
│  ├─ MarketScheduler (시장 스케줄)               │
│  └─ PnLTracker (손익 추적)                      │
└────────────────┬────────────────────────────────┘
                 │
┌─────────────────────────────────────────────────┐
│   Data & API Layer                              │
│  ├─ SnapshotStore (실시간 시세 캐시)            │
│  ├─ PositionRepository (포지션 DB)              │
│  ├─ KiwoomAPI (키움 API 호출)                   │
│  └─ DatabaseManager (SQLite)                    │
└─────────────────────────────────────────────────┘
```

### 모듈 조직

```
app/
  ├─ core.py                  # ApplicationContext (DI 컨테이너)
  ├─ state.py                 # 전역 상태 (AppState)
  ├─ market_scheduler.py       # 시장 시간 관리
  ├─ risk_manager.py          # 손실/노출도 관리
  ├─ trading_controller.py     # 신호 → 주문 오케스트레이션
  └─ config_manager.py        # 설정 로드

scanner/
  ├─ smart_scanner.py         # 핵심: 실시간 신호 생성 엔진
  ├─ snapshot_store.py        # 시세 캐시
  ├─ signal_evaluator.py      # 신호 판정 로직
  ├─ indicator_service.py      # 기술적 지표 계산
  └─ universe.py              # 종목 우주 필터

order/
  ├─ order_manager.py         # 주문 실행 및 관리
  ├─ order_executor.py        # API 호출
  ├─ position_repository.py    # 포지션 DB 관리
  └─ pnl_tracker.py           # 손익 계산

strategy/
  ├─ base.py                  # BaseStrategy (전략 인터페이스)
  └─ jang_dong_min.py         # 장동민 전략 (MA + RSI + BB)

ui/
  ├─ main_window.py           # PyQt5 메인 윈도우
  ├─ main_window_ui.py        # UI 컴포넌트
  └─ signal_manager.py        # 신호 표시 로직

infra/
  ├─ kiwoom_protocol.py       # 키움 API 프로토콜
  ├─ db_manager.py            # SQLite 관리
  └─ notification_manager.py  # 알림 (이메일, Telegram)
```

---

## 유즈케이스

### UC-1: 자동매매 시작
**액터**: 사용자  
**사전조건**: 프로그램 실행, 키움 로그인 완료  
**시나리오**:
1. MainWindow의 "자동매매 ON" 버튼 클릭
2. MarketScheduler가 시간 조건 확인
3. SmartScanner 시작 (09:00)
4. 실시간 신호 감시 (1초 주기)

**결과**: 조건 충족 시 자동으로 주문 실행

---

### UC-2: 신호 감지 및 주문 실행
**액터**: SmartScanner  
**사전조건**: 자동매매 ON, 시장 시간 (09:00~14:30)  
**시나리오**:
1. SmartScanner가 SetRealReg로 실시간 데이터 수신
2. SnapshotStore 갱신
3. SignalEvaluator가 기술적 지표 판정
   - MA 추세 확인 (MA7 > MA15)
   - RSI 오버솔드 판정 (시간대별 임계값)
   - 거래량/수급 강도 검증
4. 모든 조건 충족 → ScanSignal 생성
5. TradingController가 신호 수신
6. RiskManager가 포지션/손실 검증
7. OrderManager가 주문 실행 (매수 시장가)

**결과**: 신규 포지션 생성, 손익 추적 시작

---

### UC-3: 포지션 청산
**액ター**: TradingController / 자동 스케줄  
**사전조건**: 보유 포지션 존재  
**시나리오 A - 익절**:
1. 수익률이 3.0% 달성
2. OrderManager가 자동 매도 주문 실행

**시나리오 B - 손절**:
1. 손실률이 -1.5% 도달
2. OrderManager가 자동 매도 주문 실행

**시나리오 C - 야간 청산** (15:19~15:20):
1. MarketScheduler가 시간 감지
2. 모든 보유 포지션 시장가로 강제 청산
3. 포지션 정보 DB 저장 (세션 영속성)

**결과**: 포지션 제거, 손익 기록

---

### UC-4: 리스크 관리
**액터**: RiskManager  
**사전조건**: 자동매매 실행 중  
**시나리오**:
1. 누적 손실이 일일 한도 초과
2. RiskManager가 `is_daily_loss_cut_done` 플래그 설정
3. TradingController가 신규 진입 거절
4. 기존 보유 포지션만 청산 대기

**결과**: 손실 확대 방지

---

### UC-5: 세션 복구
**액터**: ApplicationContext  
**사전조건**: 이전 세션에 보유 포지션 존재  
**시나리오**:
1. 프로그램 재시작
2. PositionRepository가 DB에서 이전 포지션 로드
3. PnLTracker가 현재가로 손익 재계산
4. MainWindow 대시보드에 표시

**결과**: 보유 포지션/손익 자동 복구

---

## 시퀀스 다이어그램

### Seq-1: 프로그램 시작 → 준비

```
User
  │
  ├─→ run_qt.py (실행)
  │    │
  │    ├─→ ApplicationContext.__init__()
  │    │    ├─→ LoginManager (키움 로그인)
  │    │    ├─→ OrderManager (주문 준비)
  │    │    ├─→ SmartScanner 생성
  │    │    ├─→ RiskManager 생성
  │    │    ├─→ MarketScheduler 생성
  │    │    └─→ PositionRepository (기존 포지션 로드)
  │    │
  │    └─→ MainWindow.__init__()
  │         ├─→ UI 컴포넌트 초기화
  │         ├─→ 포지션 뷰 갱신
  │         └─→ 신호 로그 초기화
  │
  └─→ show() → 대시보드 표시
```

### Seq-2: 09:00 자동매매 ON

```
Time: 09:00
  │
  ├─→ MarketScheduler._check_market_time()
  │    │
  │    └─→ "09:00~09:01: Safety Switch ON" 로그
  │
  ├─→ SmartScanner.run() 시작
  │    │
  │    ├─→ [1단계] Pre-Filter (09:00, 1회만)
  │    │    ├─→ KiwoomAPI.GetCodeListByMarket()
  │    │    ├─→ 전 종목 거래대금 조회 (opt10030)
  │    │    ├─→ 거래대금 상위 200위 선정
  │    │    └─→ SnapshotStore에 적재
  │    │
  │    └─→ [2단계] Real-time Scan (1초 주기)
  │         └─→ (다음 Seq-3 참고)
  │
  └─→ UI 업데이트: "🟢 전략 실행 중"
```

### Seq-3: 신호 감지 및 주문 실행 (1초 주기)

```
SmartScanner.run() [메인 루프]
  │
  ├─→ SetRealReg (PriorityWatchQueue 기반)
  │    └─→ 감시 종목의 실시간 데이터 수신
  │
  ├─→ SnapshotStore.update(현재가, 거래량, ...)
  │    └─→ pandas DataFrame 갱신 (메모리 캐시)
  │
  ├─→ SignalEvaluator._evaluate_signal()
  │    │
  │    └─→ 각 종목별:
  │         ├─[1] 기술적 지표 계산 (IndicatorService)
  │         │     ├─ MA7, MA15, EMA10, EMA20 계산
  │         │     ├─ RSI (14) 계산 (시간대별 임계값 적용)
  │         │     └─ 거래량 SMA 계산
  │         │
  │         ├─[2] 기본 필터 (조건 검색)
  │         │     ├─ 거래대금 필터
  │         │     ├─ 변동성 필터
  │         │     ├─ 마켓인덱스 필터 (-1.5% 이상 하락 차단)
  │         │     └─ 종목명 필터 (펀드/보증금 제외)
  │         │
  │         ├─[3] 신호 필터 (매수 조건)
  │         │     ├─ MA 추세: MA7 > MA15 ✓
  │         │     ├─ RSI 조건: 시간대별 임계값 하향 돌파 ✓
  │         │     ├─ 거래량 검증: 20일 평균의 2.0배 ✓
  │         │     ├─ 체결강도 필터: ≥130% ✓
  │         │     └─ 수급 강도: 과도한 수요 차단 ✓
  │         │
  │         └─→ ✓ 모든 조건 충족 → ScanSignal 생성
  │
  ├─→ on_signal(ScanSignal) 콜백 호출
  │    │ [이곳에서 메인 스레드로 크로스스레드 신호 전송]
  │    │
  │    └─→ UI 스레드에서 처리:
  │         │
  │         ├─→ TradingController.on_signal()
  │         │    │
  │         │    ├─→ RiskManager.can_entry()
  │         │    │    ├─ 자동매매 ON? ✓
  │         │    │    ├─ 포지션 한도 5개 미만? ✓
  │         │    │    ├─ 손실 한도 미달성? ✓
  │         │    │    ├─ 중복 진입 검사 ✓
  │         │    │    └─ 신규 진입 락 해제? ✓
  │         │    │
  │         │    ├─→ ✓ 모든 검증 통과
  │         │    │
  │         │    └─→ OrderManager.buy()
  │         │         ├─ 가용 예수금 확인
  │         │         ├─ KiwoomAPI.SendOrder() (시장가 매수)
  │         │         ├─ PositionRepository.add_position()
  │         │         ├─ PnLTracker.start_tracking()
  │         │         └─ TradeAuditLogger.log_trade()
  │         │
  │         ├─→ MainWindow 업데이트
  │         │    ├─ 포지션 테이블 갱신
  │         │    ├─ 신호 로그 추가
  │         │    └─ 손익 업데이트
  │         │
  │         └─→ NotificationManager (이메일/Telegram)
  │
  └─→ [다음 1초 대기...]
```

### Seq-4: 포지션 청산 (익절/손절)

```
PnLTracker.check_exit_conditions() [1초마다 호출]
  │
  ├─→ 각 포지션별:
  │    │
  │    ├─[1] 수익 확인
  │    │     └─ 수익률 ≥ 3.0% → 익절 신호
  │    │
  │    └─[2] 손실 확인
  │          └─ 손실률 ≤ -1.5% → 손절 신호
  │
  ├─→ 청산 신호 발생 시:
  │    │
  │    └─→ OrderManager.sell()
  │         ├─ KiwoomAPI.SendOrder() (시장가 매도)
  │         ├─ PositionRepository.remove_position()
  │         ├─ PnLTracker.finalize_pnl()
  │         ├─ TradeAuditLogger.log_exit()
  │         └─ RiskManager.record_loss()
  │
  └─→ MainWindow 업데이트
       └─ 포지션 제거, 손익 기록 표시
```

### Seq-5: 15:19 자동 야간 청산

```
MarketScheduler._check_market_time() [1분마다]
  │
  ├─→ 시간 = 15:19?
  │    │
  │    └─→ YES:
  │         │
  │         ├─→ 모든 보유 포지션 조회
  │         │
  │         └─→ OrderManager._liquidate_all_positions()
  │              │
  │              ├─→ 각 포지션별:
  │              │    ├─ KiwoomAPI.SendOrder() (시장가 매도)
  │              │    └─ PositionRepository.remove_position()
  │              │
  │              ├─→ PositionRepository.save_to_db() [세션 영속성]
  │              │
  │              └─→ MainWindow 로그
  │                  └─ "15:19: 모든 보유 포지션 청산 완료"
  │
  └─→ 15:20: 자동매매 OFF
```

### Seq-6: 프로그램 재시작 후 세션 복구

```
ApplicationContext.__init__()
  │
  ├─→ PositionRepository.load_from_db()
  │    │
  │    └─→ 이전 세션의 보유 포지션 로드
  │
  ├─→ PnLTracker.recover_tracking()
  │    │
  │    └─→ 현재가로 손익 재계산
  │         ├─ KiwoomAPI.GetStockQuote()
  │         └─ 각 포지션의 손익률 계산
  │
  └─→ MainWindow 업데이트
       └─ 복구된 포지션/손익 표시
```

---

## 모듈별 상세 설명

### 1. SmartScanner (scanner/smart_scanner.py)

**책임**: 전 종목을 실시간으로 감시하고 매수 신호 생성

**주요 메서드**:
- `run()`: 메인 루프 (3단계 필터링)
- `_evaluate_signal()`: 신호 판정 (기술적 지표 + 필터)
- `on_signal`: 신호 발생 콜백

**데이터 흐름**:
```
SetRealReg(실시간) → SnapshotStore(갱신) → SignalEvaluator(판정) → on_signal(콜백)
```

**성능 특징**:
- 메모리 기반: pandas DataFrame으로 캐시
- 병렬 감시: 최대 200개 종목 동시 감시
- 냉각 시간: 신호 간 45초 (중복 신호 방지)

---

### 2. RiskManager (app/risk_manager.py)

**책임**: 손실 관리, 포지션 제한, 신규 진입 제어

**핵심 기능**:
- `can_entry()`: 신규 진입 가능 여부 판정
- `record_loss()`: 손실 기록 및 누적
- `is_daily_loss_cut_done`: 일일 손절 한도 도달 확인

**상태 추적**:
- 누적 손실 (`accumulated_loss`)
- 신규 진입 락 (`is_new_entry_locked`)
- 일일 손절 완료 (`is_daily_loss_cut_done`)

---

### 3. OrderManager (order/order_manager.py)

**책임**: 주문 실행, 포지션 생성/제거, 손익 추적

**주요 메서드**:
- `buy()`: 매수 주문 실행
- `sell()`: 매도 주문 실행
- `_liquidate_all_positions()`: 모든 포지션 청산

**포지션 추적**:
```python
self.positions = {
    "005930": Position(
        code="005930",
        qty=1,
        entry_price=70000.0,
        entry_time=datetime.now(),
        ...
    )
}
```

---

### 4. MarketScheduler (app/market_scheduler.py)

**책임**: 시장 시간 관리, 자동 스케줄 실행

**주요 이벤트**:
- 09:00~09:01: Safety Switch ON
- 09:00: Pre-Filter (거래대금 상위 200위)
- 09:00~14:30: 신호 감시 및 매수
- 15:19~15:20: 자동 야간 청산
- 15:20: 자동매매 OFF

---

### 5. PnLTracker (order/pnl_tracker.py)

**책임**: 실시간 손익 계산 및 추적

**계산 방식**:
```
손익 = (현재가 - 진입가) × 수량
손익률 = (현재가 - 진입가) / 진입가 × 100%
```

**익절/손절 판정**:
- 익절: 손익률 ≥ 3.0%
- 손절: 손익률 ≤ -1.5%

---

### 6. TradingController (app/trading_controller.py)

**책임**: 신호 → 주문 실행의 오케스트레이션

**실행 흐름**:
```
on_signal(신호)
  ├─ RiskManager.can_entry() 검증
  ├─ OrderManager.buy() 실행
  └─ MainWindow 업데이트
```

---

### 7. PositionRepository (order/position_repository.py)

**책임**: 포지션 DB 관리 (세션 영속성)

**DB 스키마**:
```sql
CREATE TABLE positions (
    code TEXT PRIMARY KEY,
    name TEXT,
    qty INTEGER,
    entry_price REAL,
    entry_time TIMESTAMP,
    ...
);
```

---

### 8. MainWindow (ui/main_window.py)

**책임**: PyQt5 GUI, 실시간 대시보드 표시

**주요 패널**:
- 포지션 테이블: 진입가, 손익, 수익률
- 신호 로그: 매수/매도 이벤트 기록
- 차트: 선정 종목의 기술적 지표 (MA, RSI 등)
- 정보 패널: 마켓인덱스, 수급 강도, 시간

---

## 데이터 흐름

### 데이터 소스 → 처리 → 출력

```
┌─────────────────────────────────────────────────────────────────┐
│                     키움 Open API                                │
├─────────────────┬──────────────────┬──────────────────┬──────────┤
│   SetRealReg    │  SendOrder       │  GetStockQuote   │ GetBalance
│  (실시간)        │  (주문 실행)      │  (현재가)         │  (예수금)
└────────┬────────┴────────┬─────────┴──────────┬──────┴────┬─────┘
         │                 │                     │           │
         ├─→ SmartScanner  ├─→ OrderManager     ├─→ PnL     ├─→ AppState
         │   (신호)        │   (주문 관리)       │   Tracker │   (거래)
         │                 │                     │           │
         └─→ SnapshotStore ├─→ Position         ├─→ Main    │
             (캐시)        │   Repository       │   Window   │
                           │   (DB 저장)        │   (표시)   │
                           │                     │           │
                           └─────────────────────┴───────────┘
```

### 신호 생성의 상세 흐름

```
실시간 시세 데이터
  │
  ├─→ SnapshotStore.update()
  │    └─ 현재가, 거래량, 체결강도 등 갱신
  │
  ├─→ IndicatorService.calculate()
  │    ├─ MA7, MA15 (이동평균)
  │    ├─ RSI14 (상대강도지수)
  │    ├─ EMA10, EMA20 (지수이동평균)
  │    └─ 거래량 SMA20
  │
  ├─→ SignalEvaluator._evaluate_signal()
  │    │
  │    ├─[Pass 1] 거래대금 필터
  │    │           └─ 거래대금 ≥ 최소값?
  │    │
  │    ├─[Pass 2] 기본 필터
  │    │           ├─ 마켓인덱스 필터 (-1.5% 이상 하락 제외)
  │    │           ├─ 변동성 필터 (금일 등락률)
  │    │           ├─ 종목명 필터 (펀드 제외)
  │    │           └─ 거래량 필터 (20일 평균 2.0배 이상)
  │    │
  │    └─[Pass 3] 신호 필터
  │               ├─ MA 추세 필터
  │               │  └─ MA7 > MA15?
  │               │
  │               ├─ RSI 필터 (시간대별)
  │               │  ├─ OPENING (09:00~10:00): RSI < 50
  │               │  ├─ MORNING (10:00~12:00): RSI < 52
  │               │  ├─ MIDDAY (12:00~14:00): RSI < 55
  │               │  └─ AFTERNOON (14:00~15:00): RSI < 58
  │               │
  │               ├─ 거래량 검증
  │               │  └─ 거래량 ≥ 20일 SMA × 2.0배?
  │               │
  │               ├─ 체결강도 필터
  │               │  └─ 체결강도 ≥ 130%?
  │               │
  │               └─ 수급 강도 필터
  │                  └─ 과도한 수요 차단?
  │
  ├─→ ✓ 모든 필터 통과
  │
  └─→ ScanSignal 생성
       └─ (code, name, timestamp, indicators, ...)
```

---

## 설정 및 파라미터

### 기본 설정 (config.py)

```python
# 전략 파라미터
MA_SHORT = 7                    # 단기 이동평균
MA_LONG = 15                    # 장기 이동평균
RSI_PERIOD = 14                 # RSI 기간

# 수익/손실
STOP_LOSS_PCT = -1.5            # 손절 -1.5%
TAKE_PROFIT_PCT = 3.0           # 익절 3.0%

# 시장 시간
MARKET_OPEN = "09:00"           # 자동매매 시작
MARKET_CLOSE = "15:20"          # 자동매매 종료
PRE_MARKET_END = "09:00"        # Pre-Filter 완료
LIQUIDATION_TIME = "15:19"      # 야간 청산 시간

# 포지션 관리
MAX_POSITIONS = 5               # 최대 포지션 수
SIGNAL_COOLDOWN_SEC = 45        # 신호 간 냉각 시간
```

### 적응형 파라미터 (params/adaptive_params.json)

```json
{
  "rsi_thresholds": {
    "OPENING": 50,
    "MORNING": 52,
    "MIDDAY": 55,
    "AFTERNOON": 58
  },
  "filters": {
    "min_trade_amount": 1000000000,
    "max_daily_change": 15,
    "min_trade_volume_ratio": 2.0,
    "min_strength_ratio": 130
  }
}
```

---

## 트러블슈팅

### 문제: 신호가 생성되지 않음
**원인 확인**:
1. 자동매매 ON 확인 (MainWindow)
2. 시간 확인 (09:00~14:30)
3. 로그 확인: `scanner.log` 의 필터 탈락 원인 분석
4. 마켓인덱스 확인 (-1.5% 이상 하락 시 신호 차단)

### 문제: 손익이 계산되지 않음
**원인 확인**:
1. 포지션 생성 확인 (MainWindow 포지션 테이블)
2. PnLTracker 상태 확인
3. 현재가 갱신 확인 (SnapshotStore)

### 문제: 자동 야간 청산이 실행되지 않음
**원인 확인**:
1. MarketScheduler 로그 확인
2. 시간 설정 확인 (15:19)
3. 보유 포지션 확인

---

## 향후 확장 계획

- 추가 기술적 지표 (MACD, Stochastic)
- ML 기반 신호 필터링
- 실시간 뉴스 감시 기능
- 고급 리스크 관리 (Value at Risk)
- 클라우드 백업

---

**마지막 수정**: 2026-05-07  
**담당자**: 자동매매 개발팀
