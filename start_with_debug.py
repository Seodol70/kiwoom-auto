#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
start_with_debug.py
──────────────────

거래대금 재검증을 위해 DEBUG 로그 레벨로 프로그램을 시작합니다.

실행:
  python start_with_debug.py
"""

import sys
import os
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

# 로그 디렉토리 생성
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# 🔧 DEBUG 로그 레벨 설정
logging.basicConfig(
    level=logging.DEBUG,  # ← DEBUG로 변경 (기본은 INFO)
    format='[%(asctime)s] [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "system.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)

logger = logging.getLogger(__name__)
logger.info("=" * 80)
logger.info("🚀 프로그램 시작 (DEBUG 로그 레벨)")
logger.info("=" * 80)
logger.info(f"작업 디렉토리: {PROJECT_ROOT}")
logger.info(f"로그 파일: {LOG_DIR / 'system.log'}")
logger.info("")
logger.info("📋 거래대금 재검증 모드")
logger.info("  - system.log에 '[진단]' 로그로 거래대금 변환 과정 기록")
logger.info("  - 네이버(035420), 남해화학(025860) 등 거래대금 확인")
logger.info("")
logger.info("검증 후: python verify_trade_amount.py 실행")
logger.info("=" * 80)
logger.info("")

# 메인 프로그램 시작
try:
    from ui.main_window import MainWindow
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    logger.info("✅ UI 창 열림 — 자동매매 ON 후 거래대금 확인")
    logger.info("")

    sys.exit(app.exec_())

except Exception as e:
    logger.error("❌ 프로그램 시작 오류: %s", e, exc_info=True)
    sys.exit(1)
