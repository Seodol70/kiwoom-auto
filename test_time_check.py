#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
현재 시간 확인
"""

from datetime import datetime, time as dtime
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from scanner.config import SmartScannerConfig

cfg = SmartScannerConfig()

now = datetime.now().time()
print(f"현재 시간: {now}")
print(f"진입 가능 시간: {cfg.entry_start_time} ~ {cfg.entry_end_time}")
print(f"범위 내? {cfg.entry_start_time <= now <= cfg.entry_end_time}")
