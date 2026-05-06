"""
키움 자동매매 - Qt 대시보드 실행

사용법:
  python run_qt.py        # 직접 실행
  python watchdog.py      # Watchdog으로 감시하며 실행
"""

import sys
import os

# Windows 터미널 한글 깨짐 방지 (UTF-8 설정)
if sys.platform == "win32":
    import io
    os.system("chcp 65001 > nul")
    sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8')

# 프로젝트 루트를 path에 추가 (하위 모듈의 절대 경로 임포트 보장)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import logging
import logging.handlers

# QApplication을 가장 먼저 생성 (모든 Qt 임포트보다 앞서야 함)
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt5")
from PyQt5.QtWidgets import QApplication

_app = QApplication(sys.argv)

# 이제 나머지 모듈 임포트
from kiwoom_api import KiwoomManager
from ui.main_window import launch


if __name__ == "__main__":
    # 로그 디렉토리 생성
    os.makedirs("logs", exist_ok=True)
    
    # 루트 로거 설정 (터미널 및 파일, 그리고 UI 로그 패널로 전달됨)
    console_handler = logging.StreamHandler()
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/system.log", maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
    )
    formatter = logging.Formatter(
        "%(asctime)s\t%(levelname)s\t%(name)s\t%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
    )

    from app.config_manager import config_manager as cfg
    logging.info("[시스템] 설정 관리자 준비 완료 (LiveReload 활성)")

    kiwoom = KiwoomManager()
    win = launch(kiwoom)
    
    # 로그인 프로세스 시작 (UI가 완전히 뜬 후 0.5초 뒤)
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(500, lambda: win.app_context.login_mgr.show_and_login())
    
    sys.exit(_app.exec_())
