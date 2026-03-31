@echo off
chcp 65001 > nul
echo ========================================
echo  키움 자동매매 — 모의투자 실행
echo  32-bit Python (venv32) 사용
echo ========================================
echo.

cd /d "%~dp0"

REM 32-bit Python (venv32) 존재 확인
if not exist "venv32\Scripts\python.exe" (
    echo [오류] venv32\Scripts\python.exe 를 찾을 수 없습니다.
    echo venv32 환경이 올바르게 설정되었는지 확인하세요.
    pause
    exit /b 1
)

REM 키움 API DLL 등록 확인 (선택)
echo [정보] 32-bit Python 경로: %~dp0venv32\Scripts\python.exe
echo [정보] 서버: 모의투자 (기본값, 로그인 창에서 변경 가능)
echo.

venv32\Scripts\python.exe ui\main_window.py

if errorlevel 1 (
    echo.
    echo [오류] 프로그램이 비정상 종료되었습니다.
    pause
)
