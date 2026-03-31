"""
Watchdog — 프로세스 감시 및 자동 재시작

역할:
  app.py를 subprocess로 실행 후 지속적으로 모니터링.
  프로세스가 비정상 종료 시 자동으로 재시작.

사용법:
  python watchdog.py
  (app.py 대신 이 스크립트를 실행)
"""

import subprocess
import sys
import time

APP_CMD = [sys.executable, "run_qt.py"]  # Streamlit 대신 Qt 버전 사용
CHECK_INTERVAL = 10  # 초
RESTART_DELAY = 3    # 초


def run():
    """watchdog 메인 루프."""
    proc = None

    while True:
        # 프로세스 실행 중이지 않으면 시작
        if proc is None or proc.poll() is not None:
            print(f"[watchdog] 앱 실행 시작...")
            proc = subprocess.Popen(APP_CMD)
            print(f"[watchdog] PID={proc.pid}")

        # 10초마다 체크
        time.sleep(CHECK_INTERVAL)

        # 프로세스가 죽었으면 재시작 대기
        if proc.poll() is not None:
            print(f"[watchdog] ⚠️ 비정상 종료 감지 (exit_code={proc.returncode})")
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    try:
        print("[watchdog] 시작됨")
        run()
    except KeyboardInterrupt:
        print("[watchdog] 사용자가 중지함")
