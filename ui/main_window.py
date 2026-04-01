"""
MainWindow — 통합 대시보드 (PyQt5 · Deep Dark)

레이아웃
  ┌─────────────────────────────────────────────────────────┐
  │  HEADER : 계좌 / 서버 모드 / 연결 상태 / 당일 손익     │
  ├────────────┬──────────────────────────┬─────────────────┤
  │  SCANNER   │       CHART              │   PORTFOLIO     │
  │  (좌 패널) │  (캔들 + MA + Volume)   │   (우 패널)    │
  │  포착 종목 │                          │   보유 현황    │
  ├────────────┴──────────────────────────┴─────────────────┤
  │  LOG : 주문 전송 / 체결 / 스캐너 이벤트               │
  └─────────────────────────────────────────────────────────┘

스레딩 설계
  메인 스레드 : Qt 이벤트 루프 + Kiwoom OCX (QAxWidget)
  ScannerWorker(QThread) : SnapshotStore 읽기 + 신호 판단 (순수 Python)
  PortfolioWorker(QThread) : 잔고 동기화 (kiwoom TR 호출 — QMetaObject 경유)

  규칙: UI 위젯 갱신은 반드시 메인 스레드 pyqtSlot 에서만 수행
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt5")
import pyqtgraph as pg
from PyQt5.QtCore import (
    Qt, QObject, QThread, QTimer,
    pyqtSignal, pyqtSlot,
)
from PyQt5.QtGui import QColor, QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QTextEdit, QSplitter, QFrame, QHeaderView,
    QSizePolicy, QProgressBar, QDoubleSpinBox, QSpinBox,
)

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TELEGRAM as _TG
from telegram_bot import TelegramBot
from scanner.smart_scanner import format_trade_amount_korean

# pyqtgraph Dark 설정 (import 직후 바로)
pg.setConfigOption("background", "#0d0d14")
pg.setConfigOption("foreground", "#cdd6f4")


# ---------------------------------------------------------------------------
# ── 스레드 워커 ──────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class ScannerWorker(QObject):
    """
    별도 QThread 에서 실행되는 스캐너 신호 판단 루프.

    SnapshotStore (DataFrame 캐시) 만 읽는다 — kiwoom TR 호출 없음.
    signal_detected 는 (1) 에지: 직전 스캔에 없던 신호가 이번에만 켜질 때,
    (2) 쿨다운: 동일 종목 마지막 emit 이후 signal_cooldown_sec 초가 지난 뒤에만 재허용.
    감시표의 signal 열은 여전히 “지금 조건 만족 여부”를 표시한다.
    """

    signal_detected    = pyqtSignal(object)        # ScanSignal
    watch_list_updated = pyqtSignal(list)         # list[dict]
    log_message        = pyqtSignal(str)

    def __init__(self, store, cfg, order_mgr, parent=None) -> None:
        super().__init__(parent)
        self._store      = store
        self._cfg        = cfg
        self._order_mgr  = order_mgr
        self._running    = False
        # 매수 신호: 에지(조건 꺼짐→켜짐) + 짧은 쿨다운(동일 종목 재 emit 간격)
        self._signal_prev_active: dict[str, bool] = {}
        self._signal_last_emit_mono: dict[str, float] = {}
        self._signal_cooldown_sec: float = float(
            getattr(cfg, "signal_cooldown_sec", 45.0)
        )
        # UI 갱신 쓰로틀 — QTableWidget 재렌더링을 3초 간격으로 제한
        self._last_ui_rows: list = []
        self._last_ui_emit: float = 0.0
        self._UI_INTERVAL: float = 3.0

    @pyqtSlot()
    def run(self) -> None:
        import logging as _logging
        _log = _logging.getLogger("ScannerWorker")
        from scanner.smart_scanner import check_breakout, check_jdm_entry, is_pure_equity_name
        self._running = True
        self.log_message.emit("[ScannerWorker] 시작 — SnapshotStore 데이터 대기 중...")
        _log.info("[ScannerWorker] run() 진입")

        _empty_logged = False
        while self._running:
            t0 = time.monotonic()

            top_df = self._store.top_by_trade_amount(self._cfg.display_top_n)

            if top_df.empty:
                if not _empty_logged:
                    self.log_message.emit(
                        "[ScannerWorker] SnapshotStore 비어있음 — opt10030 스캔 대기 중"
                    )
                    _log.debug("[ScannerWorker] SnapshotStore 비어있음")
                    _empty_logged = True
                elapsed = time.monotonic() - t0
                time.sleep(max(0.0, self._cfg.scan_interval - elapsed))
                continue

            _empty_logged = False
            rows = []
            signal_cnt = 0

            # 손절/익절은 MainWindow._auto_sell_by_pnl 에서만 처리한다.
            # (구) Worker가 전 보유종목 전량을 1초마다 검사해 HTS·전일 보유분까지 시작 직후 매도하던 문제 방지.

            # ── 벡터화 사전필터 ──────────────────────────────────────────
            # DataFrame 연산으로 시가 돌파 / 양봉 기조 미충족 종목을 먼저 제거.
            # 보통 50종목 → 5~15종목으로 줄어 이후 Python 루프 비용 70~90% 감소.
            # 등락률 상한(config RISK.max_change_pct) 이상은 후보·감시표에서 제외.
            _max_ch = float(getattr(self._cfg, "max_change_pct", 15.0))
            candidate_codes = set(self._store.prefilter_candidates(_max_ch))

            seen_codes = set(top_df.index)
            _cool = self._signal_cooldown_sec
            _tnow = time.monotonic()

            for code, row in top_df.iterrows():
                name = str(row.get("name", ""))
                if not is_pure_equity_name(name):
                    continue
                # pandas Series에서 값 안전하게 추출
                cp = row.get("change_pct", 0)
                ch = float(cp) if cp else 0.0
                # [진단] 등락률이 높은 종목 로깅
                if ch >= _max_ch:
                    _log.debug("[신호필터] %s — 등락률 %.2f%% >= 상한 %.1f%% 제외",
                               name, ch, _max_ch)
                    self._signal_prev_active[code] = False
                    continue
                sig_type = None
                reason = None
                # 사전필터 탈락 종목은 신호 판단 생략 (watch_list에는 등락 상한 미만만 표시)
                if code in candidate_codes:
                    snap = self._store.get_snapshot(code)
                    if snap is None:
                        _log.debug("[ScannerWorker] %s 스냅샷 없음", code)
                        self._signal_prev_active[code] = False
                    else:
                        # 신호 판단 (메모리 연산만)
                        reason = check_breakout(snap, self._cfg.breakout_ratio,
                                                self._cfg.breakout_volume_mult)
                        sig_type = "BREAKOUT" if reason else None

                        if not reason:
                            reason = check_jdm_entry(snap, self._cfg)
                            if reason:
                                sig_type = "JDM_ENTRY"

                        now_active = sig_type is not None
                        prev_active = self._signal_prev_active.get(code, False)
                        rising_edge = now_active and not prev_active
                        last_emit = self._signal_last_emit_mono.get(code)
                        cooldown_ok = (last_emit is None) or (
                            _tnow - last_emit >= _cool
                        )
                        if now_active and rising_edge and cooldown_ok:
                            _log.info(
                                "[ScannerWorker] 신호 발생: %s(%s) [%s] %s",
                                snap.name, code, sig_type, reason,
                            )
                            from scanner.smart_scanner import ScanSignal
                            self.signal_detected.emit(
                                ScanSignal(snap.code, snap.name, sig_type,
                                           snap.current_price, reason)
                            )
                            signal_cnt += 1
                            self._signal_last_emit_mono[code] = _tnow
                        elif now_active and rising_edge and not cooldown_ok:
                            _log.debug(
                                "[신호스킵] %s — 쿨다운 %.1fs 미경과",
                                code, _cool,
                            )

                        self._signal_prev_active[code] = now_active
                else:
                    self._signal_prev_active[code] = False

                p = row.get("current_price", 0)
                a = row.get("trade_amount", 0)
                rows.append({
                    "code":         code,
                    "name":         str(row.get("name", "")),
                    "price":        int(p) if p else 0,
                    "change_pct":   ch,
                    "trade_amount": int(a) if a else 0,
                    "signal":       sig_type or "",
                })

            for _c in list(self._signal_prev_active.keys()):
                if _c not in seen_codes:
                    del self._signal_prev_active[_c]

            # UI 갱신 쓰로틀:
            # - 신호가 새로 발생했거나 3초가 지났을 때만 emit
            # - 데이터 내용이 같으면 QTableWidget 불필요한 재렌더링 방지
            now_ui = time.monotonic()
            has_new_signal = signal_cnt > 0
            time_ok = (now_ui - self._last_ui_emit) >= self._UI_INTERVAL
            if rows and (has_new_signal or time_ok):
                self.watch_list_updated.emit(rows)
                self._last_ui_emit = now_ui
                _log.debug("[ScannerWorker] watch_list_updated %d종목 (신호 %d개)", len(rows), signal_cnt)

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, self._cfg.scan_interval - elapsed))

    def stop(self) -> None:
        self._running = False




class PortfolioWorker(QObject):
    """잔고 동기화 워커 — 메인 스레드 QTimer 방식 (Kiwoom OCX 스레드 규칙 준수)"""

    refresh_done = pyqtSignal(dict)
    log_message  = pyqtSignal(str)

    def __init__(self, order_manager, parent=None) -> None:
        super().__init__(parent)
        self._om = order_manager

    @pyqtSlot()
    def sync(self) -> None:
        """메인 스레드에서 QTimer 로 호출된다."""
        try:
            cash = self._om.sync_balance()
            self.refresh_done.emit({
                "cash":      cash,
                "positions": dict(self._om.positions),
            })
        except Exception as e:
            self.log_message.emit(f"[잔고갱신 오류] {e}")

    def stop(self) -> None:
        pass   # QTimer 정지는 MainWindow에서 처리


# ---------------------------------------------------------------------------
# ── UI 패널 위젯 ─────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class HeaderBar(QWidget):
    """상단 상태 바 — Safety Switch 포함"""

    # 자동매매 ON/OFF 상태 변경 시 MainWindow 로 전달
    auto_trade_toggled = pyqtSignal(bool)   # True = 시작, False = 정지
    exit_requested = pyqtSignal()           # 프로그램 종료 요청

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(52)
        self.setObjectName("header_bar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(16)

        self._lbl_title = QLabel("📈 키움 자동매매")
        self._lbl_title.setFont(QFont("Malgun Gothic", 12, QFont.Bold))
        self._lbl_title.setObjectName("lbl_title")

        self._lbl_account = self._make("계좌: —")
        self._lbl_mode    = self._make("—")
        self._lbl_conn    = self._make("● 미연결")
        self._lbl_conn.setObjectName("conn_off")
        self._lbl_pnl     = self._make("당일 실현손익: —")

        # ── Safety Switch ───────────────────────────────────────────────
        self._btn_auto = QPushButton("▶ 자동매매 시작")
        self._btn_auto.setObjectName("btn_auto_off")
        self._btn_auto.setCheckable(True)
        self._btn_auto.setChecked(False)
        self._btn_auto.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_auto.setFixedSize(140, 30)
        self._btn_auto.clicked.connect(self._on_auto_clicked)

        # ── 종료 버튼 ─────────────────────────────────────────────────
        self._btn_exit = QPushButton("⏻ 종료")
        self._btn_exit.setObjectName("btn_exit")
        self._btn_exit.setFont(QFont("Malgun Gothic", 9, QFont.Bold))
        self._btn_exit.setFixedSize(75, 30)
        self._btn_exit.clicked.connect(self._on_exit_clicked)

        lay.addWidget(self._lbl_title)
        lay.addStretch()
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_account)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_mode)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_conn)
        lay.addWidget(self._divider())
        lay.addWidget(self._lbl_pnl)
        lay.addWidget(self._divider())
        lay.addWidget(self._btn_auto)
        lay.addWidget(self._btn_exit)

    def _make(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Malgun Gothic", 9))
        return lbl

    def _divider(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setObjectName("v_divider")
        return f

    def _on_auto_clicked(self, checked: bool) -> None:
        if checked:
            self._btn_auto.setText("⏹ 자동매매 정지")
            self._btn_auto.setObjectName("btn_auto_on")
        else:
            self._btn_auto.setText("▶ 자동매매 시작")
            self._btn_auto.setObjectName("btn_auto_off")
        # QSS objectName 변경 즉시 반영
        self._btn_auto.style().unpolish(self._btn_auto)
        self._btn_auto.style().polish(self._btn_auto)
        self.auto_trade_toggled.emit(checked)

    def _on_exit_clicked(self) -> None:
        """프로그램 종료 버튼 클릭"""
        self.exit_requested.emit()

    def set_connected(self, account: str, mode: str) -> None:
        self._lbl_account.setText(f"계좌: {account}")
        self._lbl_mode.setText(f"{'🟠 실전' if mode == '실전투자' else '🟢 모의'}")
        self._lbl_conn.setText("● 연결됨")
        self._lbl_conn.setObjectName("conn_on")
        self._lbl_conn.style().unpolish(self._lbl_conn)
        self._lbl_conn.style().polish(self._lbl_conn)

    def set_pnl(self, pnl: int) -> None:
        sign = "+" if pnl >= 0 else ""
        self._lbl_pnl.setText(f"당일 실현손익: {sign}{pnl:,}원")
        color = "#f38ba8" if pnl < 0 else "#a6e3a1"
        self._lbl_pnl.setStyleSheet(f"color: {color};")
        self._lbl_pnl.setToolTip(
            "잔고 동기화 시 opt10074 계좌 당일 실현손익에, 그 이후 앱에서 받은 매도 체결 손익을 더한 값입니다."
        )


class ScannerPanel(QWidget):
    """좌측 — 스캐너 포착 종목 리스트"""

    row_clicked = pyqtSignal(str)   # 선택 종목코드

    # 스캐너: 전일 대비 당일 등락률(%) — 보유현황의 '수익률'(평단 대비)과 구분
    _HEADERS = ["종목코드", "종목명", "현재가", "당일등락률", "거래대금", "신호"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        title = QLabel("  🔍 스캐너 감시 종목")
        title.setObjectName("panel_title")
        lay.addWidget(title)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        # 컬럼별 최적 너비 (글자 크기 비례)
        col_widths = [68, 110, 82, 62, 100, 72]  # 코드/명/가/등락/거래대금/신호
        for i, w in enumerate(col_widths):
            hdr.resizeSection(i, w)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)          # 종목명 늘어남
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents) # 신호 자동
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.cellClicked.connect(self._on_click)
        lay.addWidget(self._table)

    @pyqtSlot(list)
    def refresh(self, rows: list[dict]) -> None:
        # 행 수가 다를 때만 setRowCount (비싼 작업)
        if self._table.rowCount() != len(rows):
            self._table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            change = row["change_pct"]
            color  = QColor("#f38ba8") if change < 0 else QColor("#a6e3a1")
            has_sig = bool(row.get("signal"))
            bg_color = QColor("#2a1a2e") if has_sig else None

            texts = [
                row["code"],
                row["name"],
                f"{row['price']:,}",
                f"{change:+.2f}%",
                format_trade_amount_korean(int(row.get("trade_amount") or 0)),
                row.get("signal", ""),
            ]
            for c, text in enumerate(texts):
                existing = self._table.item(r, c)
                # 텍스트가 바뀐 경우만 새 아이템 생성 (변경 없으면 스킵)
                if existing and existing.text() == text:
                    continue
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignVCenter |
                    (Qt.AlignRight if c >= 2 else Qt.AlignLeft)
                )
                if c in (2, 3):
                    item.setForeground(color)
                if bg_color:
                    item.setBackground(bg_color)
                self._table.setItem(r, c, item)

    def _on_click(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item:
            self.row_clicked.emit(item.text())


class ChartPanel(QWidget):
    """우하단 — 1분봉 가격/MA/거래량 차트"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._lbl_code = QLabel("  종목 차트")
        self._lbl_code.setObjectName("panel_title")
        lay.addWidget(self._lbl_code)

        self._gw = pg.GraphicsLayoutWidget()
        lay.addWidget(self._gw)

        # ── 가격 플롯 (상단 70%) ──────────────────────────────────────────
        self._price_plot = self._gw.addPlot(row=0, col=0)
        self._price_plot.showGrid(x=True, y=True, alpha=0.15)
        self._price_plot.getAxis("left").setWidth(70)
        self._price_plot.getAxis("bottom").setStyle(showValues=False)

        # 채움 영역 (가격선 아래 반투명)
        self._fill_base = self._price_plot.plot(pen=None)
        self._price_line = self._price_plot.plot(pen=pg.mkPen("#74b9ff", width=2))
        self._price_fill = pg.FillBetweenItem(
            self._fill_base, self._price_line,
            brush=pg.mkBrush(116, 185, 255, 35),
        )
        self._price_plot.addItem(self._price_fill)

        # MA 라인
        self._ma5_line  = self._price_plot.plot(pen=pg.mkPen("#ffeaa7", width=1.5))
        self._ma20_line = self._price_plot.plot(pen=pg.mkPen("#a29bfe", width=1.5))
        self._ma50_line = self._price_plot.plot(pen=pg.mkPen("#00b894", width=1.5))

        # 현재가 수평 점선
        self._curr_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#f38ba8", width=1, style=Qt.DashLine),
        )
        self._price_plot.addItem(self._curr_line)

        # 범례
        leg = self._price_plot.addLegend(offset=(10, 10))
        leg.addItem(self._price_line, "종가")
        leg.addItem(self._ma5_line,  "MA5")
        leg.addItem(self._ma20_line, "MA20")
        leg.addItem(self._ma50_line, "MA50")

        # ── 거래량 플롯 (하단 30%) ───────────────────────────────────────
        self._volume_plot = self._gw.addPlot(row=1, col=0)
        self._volume_plot.showGrid(x=False, y=True, alpha=0.15)
        self._volume_plot.getAxis("left").setWidth(70)
        self._volume_plot.setLabel("bottom", "분봉 (분)")
        self._volume_plot.setXLink(self._price_plot)

        self._vol_bars = pg.BarGraphItem(x=[], height=[], width=0.7, pen=None)
        self._volume_plot.addItem(self._vol_bars)

        self._gw.ci.layout.setRowStretchFactor(0, 7)
        self._gw.ci.layout.setRowStretchFactor(1, 3)

    @staticmethod
    def _rolling_mean(arr, window: int):
        """numpy 기반 단순이동평균 — O(n) convolution"""
        import numpy as np
        a = np.array(arr, dtype=float)
        result = np.empty(len(a))
        kernel = np.ones(window) / window
        full = np.convolve(a, kernel, mode="full")[:len(a)]
        for i in range(min(window - 1, len(a))):
            result[i] = a[: i + 1].mean()
        result[window - 1:] = full[window - 1:]
        return result

    def update_chart(self, closes: list, volumes: list, code: str, name: str) -> None:
        """1분봉 종가/거래량 리스트로 차트를 갱신한다."""
        self._lbl_code.setText(f"  📈 {name}  ({code})")
        if len(closes) < 2:
            return

        import numpy as np
        x = list(range(len(closes)))
        base_y = [min(closes)] * len(closes)

        self._price_line.setData(x=x, y=closes)
        self._fill_base.setData(x=x, y=base_y)
        self._curr_line.setValue(closes[-1])

        if len(closes) >= 5:
            self._ma5_line.setData(x=x, y=self._rolling_mean(closes, 5))
        if len(closes) >= 20:
            self._ma20_line.setData(x=x, y=self._rolling_mean(closes, 20))
        if len(closes) >= 50:
            self._ma50_line.setData(x=x, y=self._rolling_mean(closes, 50))

        if volumes:
            vols = volumes[:len(closes)]
            avg_vol = float(np.mean(vols)) if vols else 1.0
            brushes = [
                pg.mkBrush("#a6e3a1") if v >= avg_vol else pg.mkBrush("#585b70")
                for v in vols
            ]
            self._vol_bars.setOpts(
                x=x[:len(vols)], height=vols, width=0.7,
                brushes=brushes,
            )


class PortfolioPanel(QWidget):
    """우측 — 보유 종목 현황 (보유중 + 감시중 통합)"""

    tp_changed  = pyqtSignal(float)        # 익절 기준(%) 변경 시
    sl_changed  = pyqtSignal(float)        # 손절 기준(%) 변경 시
    row_clicked = pyqtSignal(str)          # 종목코드 클릭
    manual_sell = pyqtSignal(str, str, int)  # 수동 매도: (code, name, qty)

    # 현재가 다음은 % → 원 순(HTS·스캐너 '당일등락률'과 동일하게 %가 앞)
    _HEADERS = ["종목코드", "종목명", "수량", "평균단가", "현재가", "매수가대비(%)", "손익", "상태", "수동매도"]
    _COL_STATUS = 7   # "상태" 컬럼 인덱스
    _COL_SELL  = 8    # "수동매도" 컬럼 인덱스

    def __init__(self, tp_init: float = 3.0, sl_init: float = -1.0, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        title = QLabel("  💼 보유 현황")
        title.setObjectName("panel_title")
        lay.addWidget(title)

        # ── 예수금 + 익절/손절 설정 한 줄 ──────────────────────────────
        info_row = QWidget()
        info_lay = QHBoxLayout(info_row)
        info_lay.setContentsMargins(8, 2, 8, 2)
        info_lay.setSpacing(6)

        self._lbl_cash = QLabel("예수금: —")
        self._lbl_cash.setObjectName("cash_label")
        info_lay.addWidget(self._lbl_cash)
        info_lay.addStretch()

        lbl_tp = QLabel("익절")
        lbl_tp.setObjectName("risk_label")
        info_lay.addWidget(lbl_tp)
        self._spin_tp = QDoubleSpinBox()
        self._spin_tp.setObjectName("spin_tp")
        self._spin_tp.setRange(0.1, 30.0)
        self._spin_tp.setSingleStep(0.5)
        self._spin_tp.setDecimals(1)
        self._spin_tp.setSuffix(" %")
        self._spin_tp.setValue(tp_init)
        self._spin_tp.setFixedWidth(74)
        self._spin_tp.setToolTip(
            "익절 기준(%) — 매수 평단 대비 순수 등락률, 이 값 이상이면 자동 매도 (수수료·세금 제외)"
        )
        self._spin_tp.valueChanged.connect(self.tp_changed.emit)
        info_lay.addWidget(self._spin_tp)

        lbl_sl = QLabel("손절")
        lbl_sl.setObjectName("risk_label")
        info_lay.addWidget(lbl_sl)
        self._spin_sl = QDoubleSpinBox()
        self._spin_sl.setObjectName("spin_sl")
        self._spin_sl.setRange(-30.0, -0.1)
        self._spin_sl.setSingleStep(0.5)
        self._spin_sl.setDecimals(1)
        self._spin_sl.setSuffix(" %")
        self._spin_sl.setValue(sl_init)
        self._spin_sl.setFixedWidth(74)
        self._spin_sl.setToolTip(
            "손절 기준(%) — 매수 평단 대비 순수 등락률, 이 값 이하이면 자동 매도 (수수료·세금 제외)"
        )
        self._spin_sl.valueChanged.connect(self.sl_changed.emit)
        info_lay.addWidget(self._spin_sl)

        lay.addWidget(info_row)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        # 컬럼별 최적 너비 (코드/명/수량/평균단가/현재가/수익률/손익/상태/수동매도)
        col_widths = [68, 110, 50, 78, 78, 62, 82, 70, 105]
        for i, w in enumerate(col_widths):
            hdr.resizeSection(i, w)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)                   # 종목명
        hdr.setSectionResizeMode(self._COL_STATUS, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(self._COL_SELL,   QHeaderView.Fixed)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.cellClicked.connect(self._on_click)
        lay.addWidget(self._table)

    def _make_item(self, text: str, align_right: bool = False,
                   fg: Optional[QColor] = None) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(
            Qt.AlignVCenter | (Qt.AlignRight if align_right else Qt.AlignLeft)
        )
        if fg:
            item.setForeground(fg)
        return item

    @pyqtSlot(dict)
    def refresh(self, data: dict) -> None:
        cash        = data.get("cash", 0)
        positions   = data.get("positions", {})       # dict[code, Position]
        watch_today = data.get("watch_today", {})     # dict[code, {name, price, signal_type}]

        self._lbl_cash.setText(f"  예수금: {cash:,} 원")

        # ── refresh 전 수동매도 스핀박스 값 보존 (사용자 입력 유지) ──────
        _saved_qty: dict[str, int] = {}
        for row in range(self._table.rowCount()):
            code_item = self._table.item(row, 0)
            w = self._table.cellWidget(row, self._COL_SELL)
            if code_item and w:
                spin = w.findChild(QSpinBox)
                if spin:
                    _saved_qty[code_item.text()] = spin.value()

        # 보유중 + 감시중 전용(미보유) 행 구성
        watch_only = {c: v for c, v in watch_today.items() if c not in positions}
        total_rows = len(positions) + len(watch_only)
        self._table.setRowCount(total_rows)

        r = 0
        # ── 보유 포지션 ───────────────────────────────────────────────
        for pos in positions.values():
            ret_pct = float(pos.price_change_pct_vs_avg)     # 매수 평단 대비 순수 등락률(%)
            pnl = int(pos.pnl)                               # 평가손익(원) = (현재가 - 평균단가) × 수량
            color = QColor("#f38ba8") if pnl < 0 else QColor("#a6e3a1")  # 손익 기준 색상
            status = "감시중" if pos.code in watch_today else "보유중"
            s_color = QColor("#fab387") if status == "감시중" else QColor("#89b4fa")
            values = [
                (pos.code,                  False, None),
                (pos.name,                  False, None),
                (str(pos.qty),              True,  None),
                (f"{pos.avg_price:,}",      True,  None),
                (f"{pos.current_price:,}",  True,  None),
                (f"{ret_pct:+.2f}%",        True,  color),
                (f"{pnl:+,}",               True,  color),
                (status,                    False, s_color),
            ]
            for c, (text, right, fg) in enumerate(values):
                self._table.setItem(r, c, self._make_item(text, right, fg))

            # ── 수동매도 위젯: [스핀박스] [매도] ────────────────────
            cell_w = QWidget()
            cell_lay = QHBoxLayout(cell_w)
            cell_lay.setContentsMargins(2, 1, 2, 1)
            cell_lay.setSpacing(2)

            spin = QSpinBox()
            spin.setRange(1, max(1, pos.qty))
            # 저장된 수량 복원, 없으면 전량
            saved = _saved_qty.get(pos.code, pos.qty)
            spin.setValue(min(saved, pos.qty))
            spin.setFixedWidth(54)
            spin.setToolTip("매도 수량")

            btn = QPushButton("매도")
            btn.setObjectName("manual_sell_btn")
            btn.setFixedWidth(40)
            btn.setToolTip(f"{pos.name} 수동 매도")
            # 클릭 시 시그널 발생 (클로저로 code·name·spin 캡처)
            btn.clicked.connect(
                lambda _checked, c=pos.code, n=pos.name, s=spin:
                    self.manual_sell.emit(c, n, s.value())
            )

            cell_lay.addWidget(spin)
            cell_lay.addWidget(btn)
            self._table.setCellWidget(r, self._COL_SELL, cell_w)
            r += 1

        # ── 감시중 전용 (미보유) ──────────────────────────────────────
        w_color = QColor("#fab387")   # 주황색
        for code, info in watch_only.items():
            price_str = f"{info.get('price', 0):,}" if info.get('price') else "-"
            values = [
                (code,                   False, None),
                (info.get("name", code), False, None),
                ("-",                    True,  None),
                ("-",                    True,  None),
                (price_str,              True,  None),
                ("-",                    True,  None),
                ("-",                    True,  None),
                ("감시중",               False, w_color),
            ]
            for c, (text, right, fg) in enumerate(values):
                self._table.setItem(r, c, self._make_item(text, right, fg))
            # 감시중(미보유) 행에는 수동매도 위젯 없음
            self._table.setCellWidget(r, self._COL_SELL, None)
            r += 1

    def _on_click(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item:
            self.row_clicked.emit(item.text())


class ScanStatusBar(QWidget):
    """스캔 진행 상태바 — opt10030 조회 / 분봉 초기화 / 감시종목 확정"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(24)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(8)

        self._lbl_phase = QLabel("대기 중")
        self._lbl_phase.setObjectName("scan_phase")
        self._lbl_phase.setFixedWidth(130)

        self._bar = QProgressBar()
        self._bar.setFixedHeight(10)
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)

        self._lbl_detail = QLabel("")
        self._lbl_detail.setObjectName("scan_detail")

        lay.addWidget(QLabel("  스캔:"))
        lay.addWidget(self._lbl_phase)
        lay.addWidget(self._bar, stretch=1)
        lay.addWidget(self._lbl_detail)

    def update(self, phase: str, current: int, total: int, detail: str = "") -> None:
        """TR 조회 중 메인 스레드에서 호출 — processEvents로 UI 즉시 갱신"""
        self._lbl_phase.setText(phase)
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(current)
        self._lbl_detail.setText(detail)
        QApplication.processEvents()

    def done(self, msg: str) -> None:
        self._lbl_phase.setText("완료")
        self._bar.setValue(self._bar.maximum())
        self._lbl_detail.setText(msg)
        QApplication.processEvents()

    def reset(self) -> None:
        self._lbl_phase.setText("대기 중")
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._lbl_detail.setText("")


class LogPanel(QWidget):
    """하단 — 시스템 로그"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(180)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        title = QLabel("  📋 시스템 로그")
        title.setObjectName("panel_title")
        lay.addWidget(title)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("Consolas", 9))
        self._log.setObjectName("log_area")
        lay.addWidget(self._log)

    @pyqtSlot(str)
    def append(self, text: str) -> None:
        ts  = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {text}"

        # 색상 분류
        if "체결" in text or "완료" in text:
            color = "#a6e3a1"
        elif "오류" in text or "실패" in text or "경고" in text:
            color = "#f38ba8"
        elif "🚨" in text or "신호" in text:
            color = "#fab387"
        else:
            color = "#cdd6f4"

        self._log.append(f'<span style="color:{color};">{msg}</span>')
        self._log.moveCursor(QTextCursor.MoveOperation.End)


# ---------------------------------------------------------------------------
# ── MainWindow ───────────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    통합 대시보드 메인 윈도우.

    사용 예)
        app = QApplication(sys.argv)
        kiwoom = KiwoomManager()
        win = MainWindow(kiwoom)
        win.show()
        sys.exit(app.exec())
    """

    def __init__(self, kiwoom, parent=None) -> None:
        super().__init__(parent)
        self._kiwoom = kiwoom

        # 당일 감시 종목 누적 {code: {name, price, signal_type}}
        self._today_watch: dict = {}
        # time.monotonic() 기준 — 이 시각 이전에는 _auto_sell_by_pnl 미실행
        self._sl_tp_warmup_end: float = 0.0

        self.setWindowTitle("키움 자동매매 대시보드")
        self.resize(1600, 900)
        self.setStyleSheet(_DARK_QSS)

        self._build_ui()
        self._setup_modules()
        self._setup_timers()

    # -----------------------------------------------------------------------
    # UI 구성
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 상단 헤더
        self.header = HeaderBar()
        root.addWidget(self.header)

        # 구분선
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("h_sep")
        root.addWidget(sep)

        # 메인 영역 — 좌(스캐너 40%) | 우(보유현황+차트 60%)
        h_split = QSplitter(Qt.Horizontal)
        h_split.setHandleWidth(2)

        # 좌: 스캐너 감시 종목
        self.scanner_panel = ScannerPanel()
        h_split.addWidget(self.scanner_panel)

        # 우: 보유현황(위) + 차트(아래) 세로 분할
        right_v = QSplitter(Qt.Vertical)
        right_v.setHandleWidth(2)
        from config import RISK as _RISK
        self.portfolio_panel = PortfolioPanel(
            tp_init=_RISK.get("take_profit_pct", 3.0),
            sl_init=_RISK.get("stop_loss_pct",  -1.0),
        )
        self.chart_panel     = ChartPanel()
        right_v.addWidget(self.portfolio_panel)
        right_v.addWidget(self.chart_panel)
        right_v.setSizes([320, 520])   # 보유현황:차트 ≈ 38:62
        h_split.addWidget(right_v)

        # 4 : 6 비율
        h_split.setSizes([640, 960])

        root.addWidget(h_split, stretch=1)

        # 구분선
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setObjectName("h_sep")
        root.addWidget(sep2)

        # 스캔 상태바
        self.scan_status = ScanStatusBar()
        root.addWidget(self.scan_status)

        # 하단 로그
        self.log_panel = LogPanel()
        root.addWidget(self.log_panel)

    # -----------------------------------------------------------------------
    # 모듈 / 워커 / 시그널 연결
    # -----------------------------------------------------------------------

    def _setup_modules(self) -> None:
        from auth.login_manager import LoginManager
        from order.order_manager import OrderManager

        # ── LoginManager ──────────────────────────────────────────────────
        self.login_mgr = LoginManager(self._kiwoom, parent=self)
        self.login_mgr.login_success.connect(self._on_login_success)
        self.login_mgr.login_failed.connect(
            lambda m: self.log_panel.append(f"⚠ 로그인 실패: {m}")
        )

        # ── 자동 재로그인 콜백 등록 ────────────────────────────────────────
        self._kiwoom.set_auto_login_callback(lambda: self.log_panel.append("✅ 자동 재로그인 성공"))

        # ── OrderManager ──────────────────────────────────────────────────
        from config import STRATEGY
        self.order_mgr = OrderManager(
            self._kiwoom,
            max_positions=STRATEGY.get("max_positions", 5),
            parent=self
        )
        self.order_mgr.order_sent.connect(
            lambda d: self.log_panel.append(
                f"{d['side']} 주문 전송 — {d['name']}({d['code']}) "
                f"{d['qty']}주 {d['price']}원"
            )
        )
        self.order_mgr.order_filled.connect(self._on_order_filled)
        self.order_mgr.order_failed.connect(
            lambda m: self.log_panel.append(f"⚠ 주문 실패: {m}")
        )

        # ── 보유현황 수동 매도 ────────────────────────────────────────────
        self.portfolio_panel.manual_sell.connect(self._on_manual_sell)

        # ── SmartScanner 설정 ─────────────────────────────────────────────
        from config import RISK as _RISK
        from scanner.smart_scanner import SmartScanner, SmartScannerConfig, SnapshotStore
        self._snap_store = SnapshotStore()
        self._scan_cfg   = SmartScannerConfig()
        self._scan_cfg.max_change_pct = float(_RISK.get("max_change_pct", 15.0))
        self._scan_cfg.signal_cooldown_sec = float(
            _RISK.get("signal_cooldown_sec", 45.0)
        )
        # [진단] 샘플 개수 — None 이면 max_positions 와 동일하게 맞춤(혼동 완화)
        _dsn = STRATEGY.get("diagnostic_sample_n")
        if _dsn is not None:
            self._scan_cfg.diagnostic_sample_n = max(1, int(_dsn))
        else:
            self._scan_cfg.diagnostic_sample_n = max(
                1, int(STRATEGY.get("max_positions", 5))
            )
        # 감시 유니버스 상한 — 숫자로 주면 후보·실시간 구독·Worker 표시 행 수가 함께 줄어듦
        _wpm = STRATEGY.get("watch_pool_max")
        if _wpm is not None:
            wpm = max(1, int(_wpm))
            self._scan_cfg.watch_pool_max = wpm
            self._scan_cfg.realtime_sub_max = wpm
            self._scan_cfg.display_top_n = wpm

        # SmartScanner 생성 (실시간 OnReceiveRealData 연결 포함)
        self._smart_scanner = SmartScanner(self._kiwoom, self._scan_cfg)
        self._smart_scanner.store = self._snap_store   # ScannerWorker와 store 공유
        self._smart_scanner.on_signal = self._on_scan_signal_direct

        # 익절/손절 기준값 — config 우선, 이후 화면 SpinBox 변경 시 실시간 반영
        self._auto_tp_pct: float = _RISK.get("take_profit_pct", 3.0)
        self._auto_sl_pct: float = _RISK.get("stop_loss_pct",  -1.0)
        self.portfolio_panel.tp_changed.connect(self._on_tp_changed)
        self.portfolio_panel.sl_changed.connect(self._on_sl_changed)
        self.log_panel.append("[스캐너] SmartScanner 초기화 완료")

        # ── NewsAnalyzer — 백그라운드 뉴스 분석 ──────────────────────────
        import queue as _queue
        from scanner.news_analyzer import NewsAnalyzer
        self._news_queue: _queue.Queue = _queue.Queue()
        self._news_analyzer = NewsAnalyzer(
            on_result=lambda r: self._news_queue.put(r)   # 백그라운드 스레드 → 큐
        )
        self._news_analyzer.start()
        self.log_panel.append("[뉴스] NewsAnalyzer 백그라운드 스레드 시작")

        # ── ScannerWorker → QThread ───────────────────────────────────────
        self._scan_thread  = QThread(self)
        self._scan_worker  = ScannerWorker(self._snap_store, self._scan_cfg, self.order_mgr)
        self._scan_worker.moveToThread(self._scan_thread)

        self._scan_thread.started.connect(self._scan_worker.run)
        # signal_detected → _on_scan_signal (로그) + 자동매매 ON 상태일 때만 주문
        self._scan_worker.signal_detected.connect(self._on_scan_signal)
        self._scan_worker.watch_list_updated.connect(self.scanner_panel.refresh)
        self._scan_worker.log_message.connect(self.log_panel.append)

        # ── PortfolioWorker — 메인 스레드 QTimer 방식 ────────────────────
        self._port_worker = PortfolioWorker(self.order_mgr, parent=self)
        self._port_worker.refresh_done.connect(self._on_portfolio_refresh)
        self._port_worker.log_message.connect(self.log_panel.append)

        # ── Safety Switch → 자동매매 ON/OFF ──────────────────────────────
        self._auto_trading: bool = False   # 기본값: 정지 상태
        self.header.auto_trade_toggled.connect(self._on_auto_trade_toggle)
        self.header.exit_requested.connect(self.close)

        # 버튼 연결 확인 로그
        self.log_panel.append(
            f"[연결] auto_trade_toggled 시그널 연결됨 "
            f"(receiver: _on_auto_trade_toggle)"
        )

        # ── 스캐너 클릭 → 차트 갱신 ─────────────────────────────────────
        self.scanner_panel.row_clicked.connect(self._on_code_selected)

        # ── 보유현황 클릭 → 차트 갱신 ───────────────────────────────────
        self.portfolio_panel.row_clicked.connect(self._on_code_selected)

        # ── 텔레그램 봇 초기화 ──────────────────────────────────────────
        if _TG.get("enabled") and _TG.get("token"):
            try:
                self._tg = TelegramBot(_TG["token"], _TG["chat_id"], parent=self)
                self._tg.cmd_start.connect(lambda: self._on_auto_trade_toggle(True))
                self._tg.cmd_stop.connect(lambda: self._on_auto_trade_toggle(False))
                self._tg.cmd_status.connect(self._on_tg_status_requested)
                self._tg.start()
                self.log_panel.append("[연결] 텔레그램 봇 연결됨")
            except Exception as e:
                logger.warning("[텔레그램] 봇 초기화 실패: %s", e)
                self._tg = None
        else:
            self._tg = None

    def _setup_timers(self) -> None:
        """QTimer — 모두 메인 스레드에서 실행 (Kiwoom OCX 스레드 규칙 준수)"""
        self._selected_code: str = ""

        # 차트 갱신 + 현재가 기반 포트폴리오 갱신 (2초)
        self._chart_timer = QTimer(self)
        self._chart_timer.timeout.connect(self._refresh_chart)
        self._chart_timer.timeout.connect(self._refresh_portfolio_prices)
        self._chart_timer.start(2000)

        # 잔고 동기화 (1분)
        self._balance_timer = QTimer(self)
        self._balance_timer.timeout.connect(self._port_worker.sync)
        self._balance_timer.start(60_000)

        # 시장 시간 스케줄러 (1분마다 체크)
        self._schedule_timer = QTimer(self)
        self._schedule_timer.timeout.connect(self._check_market_time)
        self._schedule_timer.start(60_000)

        # 연결 상태 확인 (15분마다) — 자동 재로그인
        self._connection_timer = QTimer(self)
        self._connection_timer.timeout.connect(self._check_connection)
        self._connection_timer.start(900_000)  # 15분

        # opt10030 주기 스캔 (1분마다) — 메인 스레드에서 호출 (Kiwoom TR은 메인 스레드만 지원)
        # 타임아웃 2초로 설정하여 응답 없으면 빨리 폴백
        self._scan_refresh_timer = QTimer(self)
        self._scan_refresh_timer.timeout.connect(self._run_scanner_scan)
        # 장 시작 전까지는 타이머만 등록, start_after_login 후 가동

        # 뉴스 분석 결과 드레인 (1초마다) — 백그라운드 스레드 결과를 메인 스레드에서 안전하게 처리
        self._news_drain_timer = QTimer(self)
        self._news_drain_timer.timeout.connect(self._drain_news_queue)
        self._news_drain_timer.start(1000)

        # 텔레그램 5분 보고 (5분마다) — 자동매매 ON일 때만 발송
        self._tg_report_timer = QTimer(self)
        self._tg_report_timer.timeout.connect(self._send_tg_status)
        self._tg_report_timer.start(5 * 60 * 1000)

        self._opened_today: bool = False
        self._closed_today: bool = False

    # -----------------------------------------------------------------------
    # 로그인 후 워커 시작
    # -----------------------------------------------------------------------

    def start_after_login(self) -> None:
        """로그인 완료 후 호출"""
        self._kiwoom._account   = self.login_mgr.account
        self.order_mgr._account = self.login_mgr.account

        # [NEW] 포지션 실시간 현재가 갱신 + 동적 감시 중단/재개 콜백 연결
        self._smart_scanner._order_mgr = self.order_mgr
        max_pos = self.order_mgr.max_positions

        def _on_pos_opened(code: str):
            self._smart_scanner.add_position_realtime(code)
            # 포지션이 max에 도달했으면 유니버스 감시 중단
            if len(self.order_mgr.positions) >= max_pos:
                pos_codes = list(self.order_mgr.positions.keys())
                self._smart_scanner.pause_universe_watch(pos_codes)

        def _on_pos_closed(code: str):
            self._smart_scanner.remove_position_realtime(code)
            # on_position_closed는 del positions[code] 이전에 호출되므로 현재 포지션 수 - 1
            remaining = len(self.order_mgr.positions) - 1
            if remaining < max_pos:
                self._smart_scanner.resume_universe_watch()

        self.order_mgr.on_position_opened = _on_pos_opened
        self.order_mgr.on_position_closed = _on_pos_closed
        self.log_panel.append("[실시간] 포지션 현재가 갱신 + 동적 감시 중단/재개 콜백 연결")

        # ScannerWorker 스레드 시작 (실시간 신호 판단)
        self._scan_thread.start()
        self.log_panel.append("[워커] ScannerWorker 스레드 시작")

        # 잔고 1회 즉시 동기화
        self._port_worker.sync()

        # [NEW] 기존 보유 포지션 실시간 등록 (앱 외부에서 매수한 포지션 포함)
        for code in self.order_mgr.positions:
            self._smart_scanner.add_position_realtime(code)

        # [NEW] 잔고 동기화 완료(2~3초) 후 초기 감시 상태 결정
        # — 포지션 풀이면 자동으로 유니버스 감시 중단
        def _init_watch_state():
            if len(self.order_mgr.positions) >= max_pos:
                pos_codes = list(self.order_mgr.positions.keys())
                self._smart_scanner.pause_universe_watch(pos_codes)

        QTimer.singleShot(3000, _init_watch_state)

        # opt10030 첫 스캔을 1초 후 실행 (로그인 직후 여유)
        self.log_panel.append("[스캔] 1초 후 opt10030 초기 스캔 예약...")
        QTimer.singleShot(1000, self._run_scanner_scan)

        # 이후 1분마다 반복 스캔
        self._scan_refresh_timer.start(60_000)
        self.log_panel.append("[스캔] 1분 주기 스캔 타이머 시작 (타임아웃 2초)")

    def closeEvent(self, event) -> None:
        self._connection_timer.stop()
        self._schedule_timer.stop()
        self._balance_timer.stop()
        self._chart_timer.stop()
        self._scan_refresh_timer.stop()
        self._news_drain_timer.stop()
        self._tg_report_timer.stop()
        self._news_analyzer.stop()
        self._scan_worker.stop()
        self._scan_thread.quit()
        self._scan_thread.wait(3000)
        if self._tg:
            # 종료 알림 발송 (타임아웃 2초로 빨리 처리)
            import requests
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self._tg._token}/sendMessage",
                    json={"chat_id": self._tg._chat_id, "text": "🛑 프로그램 종료됨"},
                    timeout=2,
                )
            except Exception:
                pass
            self._tg.stop()
        super().closeEvent(event)

    # -----------------------------------------------------------------------
    # 슬롯 — 이벤트 처리
    # -----------------------------------------------------------------------

    @pyqtSlot(str, str)
    def _on_login_success(self, account: str, mode: str) -> None:
        import time as _time
        from config import RISK as _RISK2

        self.header.set_connected(account, mode)
        self.log_panel.append(f"로그인 성공 — {mode} / 계좌: {account}")
        self._today_watch.clear()           # 로그인 시 당일 감시 목록 초기화
        self._news_analyzer.reset_daily()   # 뉴스 분석 캐시 초기화
        _wu = float(_RISK2.get("sl_tp_warmup_sec", 45.0))
        self._sl_tp_warmup_end = _time.monotonic() + max(0.0, _wu)
        if _wu > 0:
            self.log_panel.append(
                f"[리스크] 로그인 후 {_wu:.0f}초간 자동 손절·익절 보류 (잔고·시세 안정화)"
            )
        # 텔레그램 시작 알림
        if self._tg:
            self._tg.send(f"🚀 프로그램 시작됨\n계좌: {account}\n모드: {mode}")
        self.start_after_login()

    @pyqtSlot(dict)
    def _on_order_filled(self, d: dict) -> None:
        ab = d.get("avg_buy_price")
        if d.get("side") == "매도체결" and ab is not None:
            line = (
                f"✅ {d['side']} — {d['name']}({d['code']}) {d['filled_qty']}주 "
                f"매수가 {ab:,}원 → 매도가 {d['filled_price']:,}원"
            )
        else:
            line = (
                f"✅ {d['side']} — {d['name']}({d['code']}) "
                f"{d['filled_qty']}주 @{d['filled_price']:,}원"
            )
        self.log_panel.append(line)
        # 텔레그램 알림 발송
        if self._tg:
            self._tg.send(line)
        # 포트폴리오 즉시 갱신 트리거
        self._on_portfolio_refresh({
            "cash":      self.order_mgr.cash,
            "positions": dict(self.order_mgr.positions),
        })

    @pyqtSlot()
    def _check_connection(self) -> None:
        """15분마다 연결 상태 확인 — 끊김 감지 시 자동 재로그인"""
        if not self._kiwoom.is_connected():
            self.log_panel.append("⚠️ 연결 끊김 감지 — 자동 재로그인 시도 중...")
            self._kiwoom.auto_reconnect()

    @pyqtSlot()
    def _check_market_time(self) -> None:
        """1분마다 시장 시간 체크 — 09:00 자동 시작, 15:19 자동 청산, 15:20 자동 정지"""
        from datetime import datetime, time
        now = datetime.now().time()

        # 09:00 자동 시작 (평일만)
        if time(9, 0) <= now < time(9, 1) and not self._opened_today:
            if datetime.now().weekday() < 5:  # 월~금
                self.header._btn_auto.setChecked(True)
                self.header._on_auto_clicked(True)
                self._opened_today = True
                self.log_panel.append("📈 시장 개장 — 자동매매 시작")

        # 15:19 자동 청산 (평일만, 장 종료 1분 전)
        elif time(15, 19) <= now < time(15, 20) and not self._closed_today:
            if datetime.now().weekday() < 5:  # 월~금
                self._liquidate_all_positions()

        # 15:20 자동 정지 (평일만)
        elif time(15, 20) <= now < time(15, 21) and not self._closed_today:
            if datetime.now().weekday() < 5:  # 월~금
                self.header._btn_auto.setChecked(False)
                self.header._on_auto_clicked(False)
                self._closed_today = True
                self.log_panel.append("📉 시장 종료 — 자동매매 중지")

        # 자정 이후 플래그 리셋 (다음 날 준비)
        elif now.hour == 0 and now.minute == 0:
            self._opened_today = False
            self._closed_today = False

    def _liquidate_all_positions(self) -> None:
        """오늘 이 앱에서 매수한 수량만 강제 청산 (장 종료 1분 전 15:19). 기존 보유·HTS 매수분은 제외."""
        from datetime import date as _date

        positions = list(self.order_mgr.positions.items())

        if not positions:
            self.log_panel.append("💤 보유 포지션 없음 — 청산 생략")
            self._closed_today = True
            return

        targets = []
        for code, pos in positions:
            q = getattr(pos, "qty_buy_today_app", 0) or 0
            if q <= 0:
                continue
            sell_qty = min(pos.qty, q)
            if sell_qty > 0:
                targets.append((code, pos, sell_qty))

        if not targets:
            self.log_panel.append(
                "💤 오늘 앱 매수분 없음 — 자동청산 생략 (기존 보유·전일 매수 유지)"
            )
            self._closed_today = True
            return

        self.log_panel.append(
            f"🔴 [자동청산 시작] 오늘 앱 매수 {len(targets)}종목만 청산 (기준일 {_date.today().isoformat()})..."
        )

        for code, pos, sell_qty in targets:
            try:
                self.order_mgr.sell(code, pos.name, sell_qty, price=0)
                self.log_panel.append(
                    f"  └─ {pos.name}({code}) {sell_qty}주 시장가 매도 주문 "
                    f"(보유 {pos.qty}주 중 오늘 앱 매수분)"
                )
            except Exception as e:
                self.log_panel.append(
                    f"  ⚠️ {pos.name}({code}) 청산 실패: {e}"
                )

        self._closed_today = True
        self.log_panel.append("🔴 [자동청산 완료] 오늘 앱 매수분 청산 명령 전송")

    @pyqtSlot(bool)
    def _on_auto_trade_toggle(self, enabled: bool) -> None:
        import logging as _log
        _log.getLogger(__name__).info("[자동매매] 토글 수신: enabled=%s", enabled)
        self._auto_trading = enabled
        state = "시작" if enabled else "정지"
        self.log_panel.append(f"{'🟢' if enabled else '🔴'} 자동매매 {state}")
        self.log_panel.append(
            f"[상태] auto_trading={self._auto_trading} "
            f"SnapshotStore={len(self._snap_store)}종목"
        )

    @pyqtSlot(float)
    def _on_tp_changed(self, value: float) -> None:
        self._auto_tp_pct = value
        self.log_panel.append(f"[리스크] 익절 기준 변경 → +{value:.1f}%")

    @pyqtSlot(float)
    def _on_sl_changed(self, value: float) -> None:
        self._auto_sl_pct = value
        self.log_panel.append(f"[리스크] 손절 기준 변경 → {value:.1f}%")

    @pyqtSlot(str, str, int)
    def _on_manual_sell(self, code: str, name: str, qty: int) -> None:
        """보유현황 수동 매도 버튼 처리."""
        pos = self.order_mgr.positions.get(code)
        if pos is None:
            self.log_panel.append(f"⚠ 수동매도 오류 — {name}({code}) 포지션 없음")
            return
        if qty <= 0 or qty > pos.qty:
            self.log_panel.append(
                f"⚠ 수동매도 오류 — 수량 {qty}주 (보유 {pos.qty}주)"
            )
            return
        self.log_panel.append(f"[수동매도] {name}({code}) {qty}주 시장가 요청")
        self.order_mgr.sell(code, name, qty, price=0)

    @pyqtSlot()
    def _run_scanner_scan(self) -> None:
        """
        메인 스레드에서 1분마다 실행 (QTimer).
        opt10030 → 테스타 정배열 + 장동민 시가돌파 필터링 → final_targets.

        주의: Kiwoom API는 메인 스레드에서만 작동.
        타임아웃을 2초로 설정하여 응답 없으면 빨리 폴백.
        """
        import logging as _log
        from config import STRATEGY as _STR
        _logger = _log.getLogger(__name__)
        _logger.info("[_run_scanner_scan] 진입")
        self.log_panel.append("[스캔] opt10030 거래대금 상위 조회 중...")
        self.scan_status.reset()
        try:
            signals = self._smart_scanner.run_periodic_scan(
                on_progress=self.scan_status.update
            )

            # 스캔 완료 후 SnapshotStore 상태 진단 (거래대금 상위 N종 샘플 — 전체 감시와 무관)
            top_df = self._snap_store.top_by_trade_amount(
                max(1, int(self._scan_cfg.diagnostic_sample_n))
            )
            sample_names = []
            for code_s, row_s in top_df.iterrows():
                sample_names.append(
                    f"{row_s.get('name','?')}({code_s}) "
                    f"{int(row_s.get('current_price',0)):,}원"
                )
            sample_str = " / ".join(sample_names) if sample_names else "없음"

            self.log_panel.append(
                f"[스캔] 완료 — Store={len(self._snap_store)}종목 갱신 | "
                f"진단샘플(거래대금상위 {self._scan_cfg.diagnostic_sample_n}종): {sample_str}"
            )
            self.log_panel.append(
                f"[스캔] 유니버스 — 감시·스냅샷 상한 {self._scan_cfg.watch_pool_max}종, "
                f"Worker 신호판단 상위 {self._scan_cfg.display_top_n}종 "
                f"(max_positions={_STR.get('max_positions', 5)} 는 동시 보유 한도)"
            )
            self.scan_status.done(
                f"데이터 갱신 완료 / 전체 {len(self._snap_store)}종목 모니터링"
            )
        except Exception as e:
            self.log_panel.append(f"[스캔 오류] {e}")
            self.scan_status.done(f"오류: {e}")
            _logger.exception("[_run_scanner_scan] 예외")

    def _on_scan_signal_direct(self, sig) -> None:
        """SmartScanner.on_signal 콜백 — 메인 스레드에서 직접 호출됨"""
        self._on_scan_signal(sig)

    @pyqtSlot(object)
    def _on_scan_signal(self, sig) -> None:
        self.log_panel.append(
            f"🚨 [{sig.signal_type}] {sig.name}({sig.code}) "
            f"@{sig.price:,}원  {sig.reason}"
        )
        # 당일 감시 목록에 누적 (포트폴리오 패널 "감시중" 표시용)
        first_signal = len(self._today_watch) == 0
        self._today_watch[sig.code] = {
            "name":        sig.name,
            "price":       sig.price,
            "signal_type": sig.signal_type,
        }
        # 첫 감시 종목 발생 시 자동매매 자동 시작
        if first_signal and not self._auto_trading:
            self.header._btn_auto.setChecked(True)
            self.header._on_auto_clicked(True)
            self.log_panel.append("🟢 감시 종목 발생 — 자동매매 자동 시작")
        # 뉴스 분석 요청 (백그라운드, 즉시 반환)
        self._news_analyzer.analyze(sig.code, sig.name)
        if self._auto_trading:
            self.order_mgr.handle_signal(sig)

    def _drain_news_queue(self) -> None:
        """
        뉴스 분석 결과를 메인 스레드에서 안전하게 처리.

        NewsAnalyzer 백그라운드 스레드가 결과를 Queue에 넣으면
        이 메서드(QTimer 1초 주기)가 꺼내 로그에 표시한다.
        Qt 위젯 접근은 항상 메인 스레드에서만 이뤄진다.
        """
        try:
            while not self._news_queue.empty():
                result = self._news_queue.get_nowait()
                self.log_panel.append(result.summary())
        except Exception:
            pass

    @pyqtSlot(dict)
    def _on_portfolio_refresh(self, data: dict) -> None:
        """watch_today를 주입하고, 헤더(당일 실현손익)/보유현황을 함께 갱신한다."""
        self.header.set_pnl(self.order_mgr.daily_realized_pnl)
        data["watch_today"] = self._today_watch
        self.portfolio_panel.refresh(data)

    @pyqtSlot(str)
    def _on_code_selected(self, code: str) -> None:
        self._selected_code = code
        self._refresh_chart()

    @pyqtSlot()
    def _on_tg_status_requested(self) -> None:
        """텔레그램 /status 명령 수신 시 현재 상태 전송."""
        if not self._tg:
            return
        lines = [
            ("🟢 자동매매 ON" if self._auto_trading else "🔴 자동매매 OFF"),
            f"예수금: {self.order_mgr.cash:,}원",
            f"당일 실현손익: {self.order_mgr.daily_realized_pnl:+,}원",
            "",
        ]
        for pos in self.order_mgr.positions.values():
            lines.append(
                f"  {pos.name} {pos.qty}주 "
                f"매수가대비 {pos.price_change_pct_vs_avg:+.2f}% (평가손익 {pos.pnl:+,}원)"
            )
        if not self.order_mgr.positions:
            lines.append("  (보유 없음)")
        self._tg.send("\n".join(lines))

    def _send_tg_status(self) -> None:
        """5분 주기 텔레그램 자동 보고 — 자동매매 ON일 때만."""
        if self._auto_trading and self._tg:
            self._on_tg_status_requested()

    def _refresh_chart(self) -> None:
        if not self._selected_code:
            return
        snap = self._snap_store.get_snapshot(self._selected_code)
        if snap is None:
            return
        closes  = snap.closes_1min or [snap.current_price]
        volumes = []   # TODO: 분봉 거래량 별도 관리 시 연결
        self.chart_panel.update_chart(
            closes, volumes, snap.code, snap.name
        )

    def _refresh_portfolio_prices(self) -> None:
        """보유 종목 현재가를 실시간 스냅샷 우선으로 갱신하고 패널을 업데이트한다."""
        positions = self.order_mgr.positions
        if not positions:
            return
        try:
            for pos in positions.values():
                # 실시간 체결로 누적되는 SnapshotStore 가격을 우선 사용한다.
                # (GetMasterLastPrice는 장중 실시간성과 정확도가 떨어질 수 있음)
                price = 0
                snap = self._snap_store.get_snapshot(pos.code)
                if snap and snap.current_price > 0:
                    price = snap.current_price
                else:
                    price = self._kiwoom.get_current_price(pos.code)
                if price > 0 and pos.current_price != price:
                    # 현재가가 변경되었을 때만 로그 출력
                    import logging as _lg
                    _lg.getLogger(__name__).debug(
                        "현재가갱신 — %s(%s) price=%d(avg=%d, src=%s)",
                        pos.name, pos.code, price, pos.avg_price,
                        "snapshot" if snap and snap.current_price > 0 else "master_last",
                    )
                    pos.current_price = price
        except Exception:
            return
        self._on_portfolio_refresh({
            "cash":      self.order_mgr.cash,
            "positions": dict(positions),
        })
        self._auto_sell_by_pnl()

    def _auto_sell_by_pnl(self) -> None:
        """
        당일 앱 매수분(qty_buy_today_app)만 익절/손절(설정 %)에 도달하면 해당 수량만 시장가 매도.
        HTS·전일 보유분은 여기서 매도하지 않는다. ScannerWorker에서는 손절/익절을 보지 않는다.
        """
        import time as _time
        if _time.monotonic() < getattr(self, "_sl_tp_warmup_end", 0.0):
            return
        positions = list(self.order_mgr.positions.items())
        for code, pos in positions:
            if self.order_mgr.is_pending(code):
                continue
            qty_today = getattr(pos, "qty_buy_today_app", 0) or 0
            if qty_today <= 0:
                continue
            sell_qty = min(pos.qty, qty_today)
            if sell_qty <= 0:
                continue
            chg = float(pos.price_change_pct_vs_avg)
            hit_take = chg >= self._auto_tp_pct
            hit_stop = chg <= self._auto_sl_pct
            if not (hit_take or hit_stop):
                continue
            reason = "익절" if hit_take else "손절"
            rate_lbl = "등락률" if hit_take else "하락률"
            self.log_panel.append(
                f"📌 [{reason}] {pos.name}({code}) 매수가대비 {rate_lbl} {chg:+.2f}% "
                f"도달 — 당일 매수분 {sell_qty}주만 시장가 매도 (보유 {pos.qty}주)"
            )
            self.order_mgr.sell(code, pos.name, sell_qty, price=0)


# ---------------------------------------------------------------------------
# Deep Dark QSS
# ---------------------------------------------------------------------------

_DARK_QSS = """
/* ─── 베이스 ──────────────────────────────────────────── */
QMainWindow, QWidget {
    background-color: #0d0d14;
    color: #cdd6f4;
    font-family: 'Malgun Gothic';
    font-size: 9pt;
}
QSplitter::handle { background: #1e1e2e; }

/* ─── 헤더 ────────────────────────────────────────────── */
QWidget#header_bar   { background: #1a1a2a; border-bottom: 1px solid #313244; }
QLabel#lbl_title     { color: #89b4fa; }
QFrame#v_divider     { color: #313244; }
QLabel#conn_off      { color: #6c7086; }
QLabel#conn_on       { color: #a6e3a1; }

/* ─── Safety Switch 버튼 ─────────────────────────────── */
QPushButton#btn_auto_off {
    background: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 4px 12px;
}
QPushButton#btn_auto_off:hover { background: #45475a; }
QPushButton#btn_auto_on {
    background: #a6e3a1;
    color: #1e1e2e;
    border: none;
    border-radius: 6px;
    padding: 4px 12px;
    font-weight: bold;
}
QPushButton#btn_auto_on:hover { background: #c3f5be; }

/* ─── 종료 버튼 ───────────────────────────────────────── */
QPushButton#btn_exit {
    background: #f38ba8;
    color: #1e1e2e;
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    font-weight: bold;
}
QPushButton#btn_exit:hover { background: #f5a3b8; }

/* ─── 수동매도 버튼 ────────────────────────────────────── */
QPushButton#manual_sell_btn {
    background: #f38ba8;
    color: #1e1e2e;
    border-radius: 3px;
    font-weight: bold;
    padding: 1px 4px;
}
QPushButton#manual_sell_btn:hover { background: #eb6f92; }
QPushButton#manual_sell_btn:pressed { background: #d05470; }

/* ─── 패널 타이틀 ─────────────────────────────────────── */
QLabel#panel_title {
    background: #13131f;
    color: #89b4fa;
    font-weight: bold;
    padding: 6px 8px;
    border-bottom: 1px solid #313244;
}
QLabel#cash_label  { color: #fab387; }
QLabel#risk_label  { color: #a6adc8; font-size: 8pt; }

/* ─── 익절/손절 SpinBox ───────────────────────────────── */
QDoubleSpinBox#spin_tp, QDoubleSpinBox#spin_sl {
    background: #1e1e2e;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 2px 4px;
    font-size: 8pt;
}
QDoubleSpinBox#spin_tp { color: #a6e3a1; }
QDoubleSpinBox#spin_sl { color: #f38ba8; }
QDoubleSpinBox#spin_tp:focus, QDoubleSpinBox#spin_sl:focus {
    border-color: #89b4fa;
}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    background: #313244; width: 14px; border: none;
}
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background: #45475a;
}

/* ─── 구분선 ──────────────────────────────────────────── */
QFrame#h_sep { color: #1e1e2e; max-height: 1px; }

/* ─── 테이블 ──────────────────────────────────────────── */
QTableWidget {
    background: #0d0d14;
    border: none;
    gridline-color: #1e1e2e;
    selection-background-color: #2a2a3e;
    selection-color: #cdd6f4;
    alternate-background-color: #111120;
}
QHeaderView::section {
    background: #13131f;
    color: #7f849c;
    border: none;
    border-bottom: 1px solid #313244;
    padding: 4px;
    font-weight: bold;
}
QTableWidget::item { padding: 3px 6px; }

/* ─── 로그 ────────────────────────────────────────────── */
QTextEdit#log_area {
    background: #0a0a10;
    border: none;
    border-top: 1px solid #1e1e2e;
    color: #cdd6f4;
    selection-background-color: #2a2a3e;
}

/* ─── 스크롤바 ────────────────────────────────────────── */
QScrollBar:vertical {
    background: #0d0d14; width: 8px; border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #313244; border-radius: 4px; min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def launch(kiwoom) -> None:
    """
    대시보드를 실행한다.

    사용 예)
        from ui.main_window import launch
        launch(kiwoom)
    """
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(kiwoom)
    win.show()

    # 로그인 다이얼로그 — 창이 보인 뒤 실행
    QTimer.singleShot(100, win.login_mgr.show_and_login)

    sys.exit(app.exec())


if __name__ == "__main__":
    import sys, os, logging
    sys.path.insert(0, os.path.dirname(__file__) + "/..")

    # QApplication을 가장 먼저 생성 (pyqtgraph 포함 모든 Qt 코드보다 앞서야 함)
    from PyQt5.QtWidgets import QApplication
    _app = QApplication(sys.argv)

    # 콘솔 로그 출력 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    try:
        from kiwoom_api import KiwoomManager
        kiwoom = KiwoomManager()
    except Exception as e:
        print(f"[WARN] KiwoomManager init failed ({e}) -- Mock mode")
        from kiwoom_api import MockKiwoomManager
        kiwoom = MockKiwoomManager()
    launch(kiwoom)
