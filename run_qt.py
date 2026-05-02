"""
키움 자동매매 — Qt 대시보드 실행

사용법:
  python run_qt.py        # 직접 실행
  python watchdog.py      # Watchdog으로 감시하며 실행
"""

import sys
import os
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

    kiwoom = KiwoomManager()
    launch(kiwoom)
