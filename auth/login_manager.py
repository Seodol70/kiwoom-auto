"""
LoginManager — 접속 및 인증 모듈

기본값: 모의투자 서버 (안전 우선)
선택:   로그인 다이얼로그에서 실전투자 전환 가능

키움 CommConnect 파라미터
  CommConnect(0) → 실전투자 서버
  CommConnect(1) → 모의투자 서버  ← 기본값
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt5.QtCore import Qt, QObject, QEventLoop, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QCheckBox, QFrame,
)
from PyQt5.QtGui import QFont, QPixmap

logger = logging.getLogger(__name__)

LOGIN_TIMEOUT = 120   # 초 (키움 로그인 창에서 입력 시간 고려)


# ---------------------------------------------------------------------------
# 로그인 다이얼로그
# ---------------------------------------------------------------------------

class LoginDialog(QDialog):
    """
    로그인 전 서버 선택 다이얼로그.

    ┌─────────────────────────────┐
    │  키움 자동매매 시스템        │
    │  ─────────────────────────  │
    │  ☐  실전투자 서버 접속       │
    │     (체크 해제 시 모의투자)  │
    │  ─────────────────────────  │
    │     [  취소  ]  [  접속  ]  │
    └─────────────────────────────┘
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("키움 자동매매 — 서버 선택")
        self.setFixedSize(360, 200)
        self.setStyleSheet(_DIALOG_QSS)
        self._use_real = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 20)

        # 타이틀
        title = QLabel("키움증권 자동매매 시스템")
        title.setFont(QFont("Malgun Gothic", 13, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setObjectName("title")
        layout.addWidget(title)

        # 구분선
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("divider")
        layout.addWidget(line)

        # 서버 선택 체크박스
        self._chk_real = QCheckBox("  실전투자 서버로 접속")
        self._chk_real.setFont(QFont("Malgun Gothic", 10))
        self._chk_real.setChecked(False)      # 기본값: 모의투자
        self._chk_real.toggled.connect(self._on_toggle)
        layout.addWidget(self._chk_real)

        self._lbl_mode = QLabel("● 모의투자 서버 (기본값)")
        self._lbl_mode.setObjectName("mode_mock")
        self._lbl_mode.setFont(QFont("Malgun Gothic", 9))
        layout.addWidget(self._lbl_mode)

        # 버튼 행
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("취소")
        btn_cancel.setObjectName("btn_cancel")
        btn_cancel.clicked.connect(self.reject)

        self._btn_login = QPushButton("접속")
        self._btn_login.setObjectName("btn_login")
        self._btn_login.setDefault(True)
        self._btn_login.clicked.connect(self.accept)

        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self._btn_login)
        layout.addLayout(btn_row)

    def _on_toggle(self, checked: bool) -> None:
        self._use_real = checked
        if checked:
            self._lbl_mode.setText("⚠ 실전투자 서버 — 실제 돈이 사용됩니다!")
            self._lbl_mode.setObjectName("mode_real")
        else:
            self._lbl_mode.setText("● 모의투자 서버 (기본값)")
            self._lbl_mode.setObjectName("mode_mock")
        # QSS 동적 갱신
        self._lbl_mode.style().unpolish(self._lbl_mode)
        self._lbl_mode.style().polish(self._lbl_mode)

    @property
    def use_real(self) -> bool:
        return self._use_real


# ---------------------------------------------------------------------------
# LoginManager
# ---------------------------------------------------------------------------

class LoginManager(QObject):
    """
    로그인 흐름 관리자.

    사용 예)
        mgr = LoginManager(kiwoom, parent=main_window)
        ok = mgr.show_and_login()   # 다이얼로그 → CommConnect → 완료 대기
        if ok:
            print("계좌:", mgr.account)
            print("모드:", mgr.server_mode)
    """

    login_success = pyqtSignal(str, str)   # (account, mode)
    login_failed  = pyqtSignal(str)        # error message

    def __init__(self, kiwoom, parent=None) -> None:
        super().__init__(parent)
        self._kiwoom      = kiwoom
        self._err_code    = -999
        self._loop:       Optional[QEventLoop] = None

        self.account:     str = ""
        self.server_mode: str = ""   # "모의투자" | "실전투자"
        self.use_real:    bool = False

        self._kiwoom._ocx.OnEventConnect.connect(self._on_event_connect)

    # -----------------------------------------------------------------------
    # 공개 API
    # -----------------------------------------------------------------------

    def show_and_login(self) -> bool:
        """
        서버 선택 다이얼로그를 표시하고 CommConnect 를 호출한다.

        Returns:
            True → 로그인 성공
        """
        dlg = LoginDialog()
        if dlg.exec() != QDialog.Accepted:
            logger.info("로그인 취소")
            return False

        self.use_real    = dlg.use_real
        self.server_mode = "실전투자" if self.use_real else "모의투자"
        logger.info("서버 선택: %s", self.server_mode)

        return self._connect()

    def login_mock(self) -> bool:
        """다이얼로그 없이 모의투자로 직접 접속 (테스트용)"""
        self.use_real    = False
        self.server_mode = "모의투자"
        return self._connect()

    def login_real(self) -> bool:
        """다이얼로그 없이 실전투자로 직접 접속"""
        self.use_real    = True
        self.server_mode = "실전투자"
        return self._connect()

    # -----------------------------------------------------------------------
    # 내부 로직
    # -----------------------------------------------------------------------

    def _connect(self) -> bool:
        self._err_code = -999

        # 모의투자: SetLoginInfo로 모의투자 플래그 설정 후 CommConnect
        # 실전투자: 플래그 없이 CommConnect
        if not self.use_real:
            try:
                self._kiwoom._ocx.dynamicCall(
                    "SetLoginInfo(QString, QString)", ["UseSimulInvest", "1"]
                )
                logger.info("모의투자 서버 설정 완료 (UseSimulInvest=1)")
            except Exception:
                # 구버전 API에서 미지원 — 로그인 창에서 직접 모의투자 선택 필요
                logger.info("SetLoginInfo 미지원 버전 — 로그인 창에서 모의투자 서버 선택하세요")
        self._kiwoom._ocx.dynamicCall("CommConnect()")
        logger.info("CommConnect() 호출 — 키움 로그인 창 대기 중")

        # QEventLoop으로 대기 — Qt 이벤트를 처리하면서 OnEventConnect 콜백을 받음
        self._loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._loop.quit)
        timer.start(LOGIN_TIMEOUT * 1000)
        self._loop.exec_()   # 여기서 블로킹하지 않고 Qt 이벤트 처리
        timer.stop()
        self._loop = None

        if self._err_code == -999:
            msg = f"로그인 타임아웃 ({LOGIN_TIMEOUT}초)"
            logger.error(msg)
            self.login_failed.emit(msg)
            return False

        if self._err_code != 0:
            msg = f"로그인 실패 (err={self._err_code})"
            logger.error(msg)
            self.login_failed.emit(msg)
            return False

        # 계좌번호 획득
        raw = self._kiwoom._ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        accounts = [a for a in raw.strip().split(";") if a]
        self.account = accounts[0] if accounts else ""

        logger.info("로그인 성공 — 모드: %s / 계좌: %s",
                    self.server_mode, self.account)
        self.login_success.emit(self.account, self.server_mode)
        return True

    def _on_event_connect(self, err_code: int) -> None:
        self._err_code = err_code
        if self._loop and self._loop.isRunning():
            self._loop.quit()


# ---------------------------------------------------------------------------
# QSS 스타일
# ---------------------------------------------------------------------------

_DIALOG_QSS = """
QDialog {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QLabel {
    color: #cdd6f4;
}
QLabel#title {
    color: #89b4fa;
}
QLabel#mode_mock {
    color: #a6e3a1;
    padding-left: 4px;
}
QLabel#mode_real {
    color: #f38ba8;
    font-weight: bold;
    padding-left: 4px;
}
QFrame#divider {
    color: #45475a;
}
QCheckBox {
    color: #cdd6f4;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #585b70;
    border-radius: 3px;
    background: #313244;
}
QCheckBox::indicator:checked {
    background: #89b4fa;
}
QPushButton {
    border-radius: 6px;
    padding: 8px 20px;
    font-size: 10pt;
    font-family: 'Malgun Gothic';
}
QPushButton#btn_cancel {
    background: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
}
QPushButton#btn_cancel:hover { background: #45475a; }
QPushButton#btn_login {
    background: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
    border: none;
}
QPushButton#btn_login:hover { background: #b4d0ff; }
"""
