@echo off
chcp 65001 >nul
cls

echo ============================================================
echo   KIWOOM AUTO TRADING SYSTEM
echo ============================================================
echo.
echo [Start] Launching...
cd /d "%~dp0"

if not exist "venv32\Scripts\python.exe" (
    echo.
    echo [ERROR] venv32 not found!
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo [WARNING] .env file not found!
    echo Please copy .env.example and add your Kiwoom credentials.
    echo.
    pause
    exit /b 1
)

echo [OK] venv32 found
echo [OK] .env file found
echo.
echo ============================================================
echo [Running] Python 3.11 (32-bit) mode...
echo ============================================================
echo.

"venv32\Scripts\python.exe" run_qt.py

echo.
echo ============================================================
echo [Done] Program closed.
echo ============================================================
echo.
pause
