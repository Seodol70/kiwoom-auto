import ast
import os

with open('ui/main_window.py', encoding='utf-8') as f:
    lines = f.readlines()

def get_code(start, end):
    return "".join(lines[start-1:end])

imports = """\
from __future__ import annotations
import os, sys, time, threading, logging, logging.handlers
from datetime import datetime
from typing import Optional

import pyqtgraph as pg
from PyQt5.QtCore import Qt, QObject, QThread, QTimer, QEvent, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QSplitter,
    QFrame, QHeaderView, QSizePolicy, QProgressBar, QDoubleSpinBox, QSpinBox,
    QDialog, QDialogButtonBox, QComboBox, QGroupBox, QAction, QMenu
)

from config import TELEGRAM as _TG
from telegram_bot import TelegramBot
from scanner.smart_scanner import format_trade_amount_korean

"""

def write_comp(filename, classes):
    with open(f'ui/components/{filename}', 'w', encoding='utf-8') as f:
        f.write(imports)
        for cls in classes:
            f.write(get_code(cls[0], cls[1]) + "\n\n")

# From ast analysis
classes = {
    'HeaderBar': (70, 261),
    'ManualBuyDialog': (264, 362),
    '_hline': (365, 370),
    'ScannerPanel': (373, 529),
    'ChartPanel': (532, 787),
    '_NoWheelSpinBox': (790, 793),
    '_NoWheelDoubleSpinBox': (796, 799),
    'PortfolioPanel': (802, 1003),
    'ScanStatusBar': (1006, 1057),
    'InvestorPanel': (1060, 1180),
    'ScannerLogHandler': (1183, 1235),
    'SysLogQtHandler': (1238, 1317),
    'LogPanel': (1320, 1444)
}

write_comp('common.py', [classes['_hline'], classes['_NoWheelSpinBox'], classes['_NoWheelDoubleSpinBox']])
write_comp('header_bar.py', [classes['HeaderBar'], classes['ManualBuyDialog']])
write_comp('scanner_panel.py', [classes['ScannerPanel']])
write_comp('chart_panel.py', [classes['ChartPanel']])
write_comp('portfolio_panel.py', [classes['PortfolioPanel']])
write_comp('investor_panel.py', [classes['InvestorPanel'], classes['ScanStatusBar']])
write_comp('log_panel.py', [classes['ScannerLogHandler'], classes['SysLogQtHandler'], classes['LogPanel']])

print("Done extracting UI components")
