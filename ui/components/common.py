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


def _hline() -> QFrame:
    """수평 구분선."""
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f




class _NoWheelSpinBox(QSpinBox):
    """마우스 휠로 값이 바뀌지 않는 SpinBox — 테이블 스크롤 우선"""
    def wheelEvent(self, e):
        e.ignore()




class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    """마우스 휠로 값이 바뀌지 않는 DoubleSpinBox — 테이블 스크롤 우선"""
    def wheelEvent(self, e):
        e.ignore()




