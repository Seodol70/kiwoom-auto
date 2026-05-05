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


class ChartPanel(QWidget):
    """우하단 — 1분봉 차트 + 종목 판단 정보 패널"""


    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)


        # ── 좌: 차트 영역 ──────────────────────────────────────────────
        chart_w = QWidget()
        chart_lay = QVBoxLayout(chart_w)
        chart_lay.setContentsMargins(0, 0, 0, 0)
        chart_lay.setSpacing(0)


        self._lbl_code = QLabel("  종목 차트")
        self._lbl_code.setObjectName("panel_title")
        chart_lay.addWidget(self._lbl_code)

        # [NEW] pyqtgraph 전역 설정 (한글 폰트 깨짐 방지 및 안티앨리어싱)
        pg.setConfigOptions(antialias=True)
        # GraphicsLayoutWidget 생성 전 전역 폰트 설정 시도
        _font = QFont("Malgun Gothic", 9)
        
        self._gw = pg.GraphicsLayoutWidget()
        chart_lay.addWidget(self._gw)


        # 가격 플롯 (상단 70%)
        self._price_plot = self._gw.addPlot(row=0, col=0)
        self._price_plot.showGrid(x=True, y=True, alpha=0.15)
        self._price_plot.getAxis("left").setWidth(70)
        self._price_plot.getAxis("left").setTickFont(_font)
        self._price_plot.getAxis("bottom").setTickFont(_font)
        self._price_plot.getAxis("bottom").setStyle(showValues=False)
        # 축 레이블 폰트 설정
        for axis_name in ["left", "bottom"]:
            axis = self._price_plot.getAxis(axis_name)
            axis.label.setFont(_font)


        self._fill_base  = self._price_plot.plot(pen=None)
        self._price_line = self._price_plot.plot(pen=pg.mkPen("#74b9ff", width=2))
        self._price_fill = pg.FillBetweenItem(
            self._fill_base, self._price_line,
            brush=pg.mkBrush(116, 185, 255, 25),
        )
        self._price_plot.addItem(self._price_fill)


        # MA7 / MA15 (JDM 전략 기준)
        self._ma7_line  = self._price_plot.plot(pen=pg.mkPen("#ffeaa7", width=1.5))
        self._ma15_line = self._price_plot.plot(pen=pg.mkPen("#a29bfe", width=1.5))


        # 수평선: 현재가(파랑점선) / 매수가(노랑) / 트레일가(주황점선) / 고점(초록점선) / 손절(빨강점선)
        self._curr_line  = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#74b9ff", width=1, style=Qt.DashLine))
        self._buy_line   = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#ffeaa7", width=2, style=Qt.SolidLine))
        self._trail_line = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#fab387", width=1, style=Qt.DashLine))   # 주황 — 트레일 스탑가
        self._peak_line  = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#a6e3a1", width=1, style=Qt.DotLine))    # 초록 점선 — 고점
        self._sl_line    = pg.InfiniteLine(angle=0, movable=False,
            pen=pg.mkPen("#f38ba8", width=1, style=Qt.DashLine))
        for line in [self._curr_line, self._buy_line, self._trail_line, self._peak_line, self._sl_line]:
            self._price_plot.addItem(line)
        self._buy_line.setVisible(False)
        self._trail_line.setVisible(False)
        self._peak_line.setVisible(False)
        self._sl_line.setVisible(False)


        # 범례
        leg = self._price_plot.addLegend(offset=(10, 10))
        # 범례 텍스트 폰트 설정
        leg.setLabelTextSize("9pt")


        # 거래량 플롯 (하단 30%)
        self._volume_plot = self._gw.addPlot(row=1, col=0)
        self._volume_plot.showGrid(x=False, y=True, alpha=0.15)
        self._volume_plot.getAxis("left").setWidth(70)
        self._volume_plot.getAxis("left").setTickFont(_font)
        self._volume_plot.getAxis("bottom").setTickFont(_font)
        self._volume_plot.setLabel("bottom", "분봉 (분)")
        # 축 레이블 폰트 설정
        for axis_name in ["left", "bottom"]:
            axis = self._volume_plot.getAxis(axis_name)
            axis.label.setFont(_font)
        self._volume_plot.setXLink(self._price_plot)
        self._vol_bars = pg.BarGraphItem(x=[], height=[], width=0.7, pen=None)
        self._volume_plot.addItem(self._vol_bars)


        self._gw.ci.layout.setRowStretchFactor(0, 7)
        self._gw.ci.layout.setRowStretchFactor(1, 3)
        root.addWidget(chart_w, stretch=6)


        # ── 우: 정보 패널 ──────────────────────────────────────────────
        info_w = QWidget()
        info_w.setObjectName("chart_info_panel")
        info_lay = QVBoxLayout(info_w)
        info_lay.setContentsMargins(12, 12, 12, 12)
        info_lay.setSpacing(6)


        def _lbl(text="—", bold=False, size=9, color=None):
            l = QLabel(text)
            f = QFont("Malgun Gothic", size)
            f.setBold(bold)
            l.setFont(f)
            l.setWordWrap(True)
            if color:
                l.setStyleSheet(f"color: {color};")
            return l


        def _sep():
            f = QFrame()
            f.setFrameShape(QFrame.HLine)
            f.setStyleSheet("color: #313244; margin: 2px 0;")
            return f


        self._i_name   = _lbl("종목 선택", bold=True, size=10)
        self._i_signal = _lbl("신호: —", size=8, color="#a6e3a1")
        info_lay.addWidget(self._i_name)
        info_lay.addWidget(self._i_signal)
        info_lay.addWidget(_sep())


        self._i_buy    = _lbl("매수가: —", size=9)
        self._i_curr   = _lbl("현재가: —", size=9)
        self._i_pnl    = _lbl("손익: —", bold=True, size=10)
        info_lay.addWidget(self._i_buy)
        info_lay.addWidget(self._i_curr)
        info_lay.addWidget(self._i_pnl)
        info_lay.addWidget(_sep())


        self._i_hold   = _lbl("보유: —", size=9)
        self._i_remain = _lbl("남은 시간: —", size=9)
        info_lay.addWidget(self._i_hold)
        info_lay.addWidget(self._i_remain)
        info_lay.addWidget(_sep())


        self._i_peak  = _lbl("고점: —", size=8, color="#a6e3a1")
        self._i_trail = _lbl("트레일: —", size=8, color="#fab387")
        self._i_sl    = _lbl("손절까지: —", size=8, color="#f38ba8")
        info_lay.addWidget(self._i_peak)
        info_lay.addWidget(self._i_trail)
        info_lay.addWidget(self._i_sl)
        info_lay.addStretch()
        root.addWidget(info_w, stretch=2)


    @staticmethod
    def _rolling_mean(arr, window: int):
        import numpy as np
        a = np.array(arr, dtype=float)
        result = np.empty(len(a))
        kernel = np.ones(window) / window
        full = np.convolve(a, kernel, mode="full")[:len(a)]
        for i in range(min(window - 1, len(a))):
            result[i] = a[: i + 1].mean()
        result[window - 1:] = full[window - 1:]
        return result


    def update_chart(
        self,
        closes: list,
        volumes: list,
        code: str,
        name: str,
        position=None,
        trail_price: int = 0,
        sl_pct: float = -1.5,
        signal_reason: str = None,
    ) -> None:
        """1분봉 데이터 + 포지션 정보로 차트와 정보 패널을 갱신한다."""
        self._lbl_code.setText(f"  {'📈' if position else '👁️'} {name}  ({code})")


        # ── 차트 갱신 ────────────────────────────────────────────────
        if len(closes) >= 2:
            import numpy as np
            x = list(range(len(closes)))
            self._price_line.setData(x=x, y=closes)
            self._fill_base.setData(x=x, y=[min(closes)] * len(closes))
            self._curr_line.setValue(closes[-1])
            if len(closes) >= 7:
                self._ma7_line.setData(x=x, y=self._rolling_mean(closes, 7))
            if len(closes) >= 15:
                self._ma15_line.setData(x=x, y=self._rolling_mean(closes, 15))
            if volumes:
                vols = volumes[:len(closes)]
                avg_vol = float(np.mean(vols)) if vols else 1.0
                self._vol_bars.setOpts(
                    x=x[:len(vols)], height=vols, width=0.7,
                    brushes=[
                        pg.mkBrush("#a6e3a1") if v >= avg_vol else pg.mkBrush("#585b70")
                        for v in vols
                    ],
                )


        # ── 정보 패널 갱신 ───────────────────────────────────────────
        curr = closes[-1] if closes else 0


        if position:
            avg  = position.avg_price
            curr = position.current_price or curr
            qty  = position.qty
            peak = position.peak_price or 0


            sl_price = int(avg * (1 + sl_pct / 100))


            self._buy_line.setValue(avg);   self._buy_line.setVisible(True)
            self._sl_line.setValue(sl_price); self._sl_line.setVisible(True)
            if peak > 0:
                self._peak_line.setValue(peak);   self._peak_line.setVisible(True)
            else:
                self._peak_line.setVisible(False)
            if trail_price > 0:
                self._trail_line.setValue(trail_price); self._trail_line.setVisible(True)
            else:
                self._trail_line.setVisible(False)


            pnl      = (curr - avg) * qty
            pnl_pct  = (curr - avg) / avg * 100 if avg else 0
            sign     = "+" if pnl >= 0 else ""
            color    = "#a6e3a1" if pnl >= 0 else "#f38ba8"


            dist_sl_pct = (curr - sl_price) / curr * 100 if curr else 0


            from datetime import datetime as _dt
            hold_str = "—"
            remain_str = "—"
            if hasattr(position, "entry_time") and position.entry_time:
                held = int((_dt.now() - position.entry_time).total_seconds() / 60)
                hold_str   = f"{held}분 경과"
                remain_str = f"{max(0, 60 - held)}분 남음"


            # 트레일 정보 텍스트
            if peak > 0 and trail_price > 0:
                peak_chg_pct  = (peak - avg) / avg * 100 if avg else 0
                trail_chg_pct = (trail_price - avg) / avg * 100 if avg else 0
                peak_txt  = f"고점:  {peak:,}원  (+{peak_chg_pct:.2f}%)"
                trail_txt = f"트레일가:  {trail_price:,}원  ({trail_chg_pct:+.2f}%)"
            elif peak > 0:
                peak_chg_pct = (peak - avg) / avg * 100 if avg else 0
                peak_txt  = f"고점:  {peak:,}원  (+{peak_chg_pct:.2f}%)"
                trail_txt = "트레일:  대기 중 (고점 미달성)"
            else:
                peak_txt  = f"고점:  — (활성화 대기)"
                trail_txt = "트레일:  —"


            self._i_name.setText(f"📈 {name}\n({code})")
            self._i_signal.setText(f"신호: {signal_reason or '앱 매수'}")
            self._i_buy.setText(f"매수가:  {avg:,}원")
            self._i_curr.setText(f"현재가:  {curr:,}원")
            self._i_pnl.setText(f"손익:  {sign}{pnl:,}원  ({sign}{pnl_pct:.2f}%)")
            self._i_pnl.setStyleSheet(f"color: {color}; font-weight: bold;")
            self._i_hold.setText(f"보유: {hold_str}")
            self._i_remain.setText(f"홀딩: {remain_str}  (최대 60분)")
            self._i_peak.setText(peak_txt)
            self._i_trail.setText(trail_txt)
            self._i_sl.setText(f"손절까지:  {-dist_sl_pct:.2f}%  ({sl_price:,}원)")
        else:
            self._buy_line.setVisible(False)
            self._trail_line.setVisible(False)
            self._peak_line.setVisible(False)
            self._sl_line.setVisible(False)


            self._i_name.setText(f"👁️ {name}\n({code})")
            self._i_signal.setText(f"신호: {signal_reason or '감시 중'}")
            self._i_buy.setText("매수가:  —  (미보유)")
            self._i_curr.setText(f"현재가:  {curr:,}원" if curr else "현재가:  —")
            self._i_pnl.setText("손익:  —")
            self._i_pnl.setStyleSheet("color: #6c7086;")
            self._i_hold.setText("보유:  —")
            self._i_remain.setText("홀딩:  —")
            self._i_peak.setText("고점:  —")
            self._i_trail.setText("트레일:  —")
            self._i_sl.setText(f"손절 기준:  {sl_pct:.1f}%")




