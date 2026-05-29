#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""에러 캡처용 테스트 스크립트"""

import sys
import os
import traceback

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    print("=" * 60)
    print("[1/5] Python version check...")
    print(f"Python: {sys.version}")
    print(f"Platform: {sys.platform}")

    print("\n[2/5] PyQt5 import...")
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt5")
    from PyQt5.QtWidgets import QApplication
    _app = QApplication(sys.argv)
    print("[OK] PyQt5")

    print("\n[3/5] Project modules import...")
    from kiwoom_api import KiwoomManager
    from ui.main_window import launch
    print("[OK] Project modules")

    print("\n[4/5] KiwoomManager init...")
    kiwoom = KiwoomManager()
    print("[OK] KiwoomManager")

    print("\n[5/5] MainWindow launch...")
    win = launch(kiwoom)
    print("[OK] MainWindow")

    print("\n" + "=" * 60)
    print("모든 초기화 성공! 프로그램 시작 중...")
    print("=" * 60)

    from PyQt5.QtCore import QTimer
    QTimer.singleShot(500, lambda: win.app_context.login_mgr.show_and_login())

    sys.exit(_app.exec_())

except Exception as e:
    print("\n" + "=" * 60)
    print("ERROR 발생!")
    print("=" * 60)
    print(f"\n에러 타입: {type(e).__name__}")
    print(f"에러 메시지: {str(e)}")
    print("\n전체 스택 트레이스:")
    print("-" * 60)
    traceback.print_exc()
    print("-" * 60)

    input("\n엔터를 눌러 종료하세요...")
