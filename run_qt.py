"""
키움 자동매매 — Qt 대시보드 실행

사용법:
  python run_qt.py        # 직접 실행
  python watchdog.py      # Watchdog으로 감시하며 실행
"""

import sys
import os

# QApplication을 가장 먼저 생성 (모든 Qt 임포트보다 앞서야 함)
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt5")
from PyQt5.QtWidgets import QApplication

_app = QApplication(sys.argv)

# 이제 나머지 모듈 임포트
from kiwoom_api import KiwoomManager
from ui.main_window import launch


if __name__ == "__main__":
    kiwoom = KiwoomManager()
    launch(kiwoom)
