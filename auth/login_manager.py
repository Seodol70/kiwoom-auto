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
    QPushButton, QCheckBox, QFrame, QListWidget, QListWidgetItem,
    QAbstractItemView,
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

        self._account_cache_file = "config/last_account.txt"

        self._kiwoom._ocx.OnEventConnect.connect(self._on_event_connect)

    def _save_last_account(self) -> None:
        try:
            import os, json
            os.makedirs("config", exist_ok=True)
            with open(self._account_cache_file, "w", encoding="utf-8") as f:
                json.dump({"account": self.account, "use_real": self.use_real}, f)
        except Exception as e:
            logger.debug("계좌 정보 저장 실패: %s", e)

    def _load_last_account(self) -> dict:
        import os, json
        if os.path.exists(self._account_cache_file):
            try:
                with open(self._account_cache_file, "r", encoding="utf-8") as f:
                    data = f.read().strip()
                    if not data.startswith("{"):
                        return {"account": data, "use_real": False}
                    return json.loads(data)
            except Exception:
                return {}
        return {}

    # -----------------------------------------------------------------------
    # 공개 API
    # -----------------------------------------------------------------------

    def show_and_login(self) -> bool:
        """
        저장된 캐시가 있으면 다이얼로그 없이 바로 접속하고,
        없으면 서버 선택 다이얼로그를 표시한 뒤 CommConnect를 호출한다.

        Returns:
            True → 로그인 성공
        """
        cache = self._load_last_account()
        if cache and "account" in cache:
            self.account = cache["account"]
            self.use_real = cache.get("use_real", False)
            self.server_mode = "실전투자" if self.use_real else "모의투자"
            logger.info("캐시된 설정으로 다이얼로그 생략 접속: %s 모드", self.server_mode)
            
            # 접속 성공 시 바로 True 반환
            if self._connect(preferred_account=self.account):
                return True
            
            # 자동 접속 실패(기간 만료, 비번 오류 등) 시 캐시 삭제 후 선택창으로 폴백
            import os
            if os.path.exists(self._account_cache_file):
                os.remove(self._account_cache_file)
            logger.warning("캐시로 자동 접속 실패! 서버 선택 창으로 넘어갑니다.")

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

    def reconnect_silent(self) -> bool:
        """자동 재연결 — 다이얼로그 없이 이전 계좌로 재접속.

        키움이 연결을 강제로 끊었을 때(야간 점검, 장 종료 후 등) 호출.
        이전에 선택했던 self.account가 있으면 다이얼로그 없이 자동 선택.
        """
        logger.warning("[자동재연결] 시도 — 이전 계좌: '%s' 모드: %s",
                        self.account, self.server_mode)
        return self._connect(preferred_account=self.account)

    def _connect(self, preferred_account: str = "") -> bool:
        """CommConnect → OnEventConnect 대기 → 계좌 선택.

        preferred_account: 재연결 시 이전 계좌번호. 지정되면 다이얼로그 없이 자동 선택.
        """
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

        if len(accounts) == 0:
            logger.error("계좌 목록 없음")
            return False
        elif len(accounts) == 1:
            self.account = accounts[0]
            self._save_last_account()
        elif preferred_account and preferred_account in accounts:
            # 자동 재연결 (실시간 끊김 시): 메모리의 이전 계좌를 자동 선택
            self.account = preferred_account
            logger.info("계좌 자동 선택 완료: %s (전체 %d개)", self.account, len(accounts))
        else:
            # 파일에 저장된 이전 선택 계좌가 있는지 확인
            last_acc_data = self._load_last_account()
            last_acc = last_acc_data.get("account", "")
            if last_acc and last_acc in accounts:
                self.account = last_acc
                logger.info("이전 접속 계좌 자동 선택 (재시작): %s", self.account)
            else:
                # 최초 로그인 또는 저장된 계좌가 유효하지 않을 때: 다이얼로그에서 선택
                dlg = AccountSelectDialog(accounts)
                dlg.exec()
                self.account = dlg.selected_account
                self._save_last_account()
                logger.info("계좌 선택 완료: %s (전체 %d개)", self.account, len(accounts))

        logger.info("로그인 성공 — 모드: %s / 계좌: %s",
                    self.server_mode, self.account)
        self.login_success.emit(self.account, self.server_mode)
        return True

    def _on_event_connect(self, err_code: int) -> None:
        self._err_code = err_code
        if self._loop and self._loop.isRunning():
            self._loop.quit()


# ---------------------------------------------------------------------------
# 계좌 선택 다이얼로그
# ---------------------------------------------------------------------------

class AccountSelectDialog(QDialog):
    """
    로그인 후 계좌가 여러 개인 경우 사용할 계좌를 선택하는 다이얼로그.

    ┌──────────────────────────────────┐
    │  사용할 계좌를 선택하세요         │
    │  ──────────────────────────────  │
    │  ○ 1234567890  (모의투자)        │
    │  ○ 9876543210  (모의투자)        │
    │  ──────────────────────────────  │
    │           [  확인  ]             │
    └──────────────────────────────────┘
    """

    def __init__(self, accounts: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("계좌 선택")
        self.setFixedSize(360, 240)
        self.setStyleSheet(_DIALOG_QSS)
        self._selected: str = accounts[0] if accounts else ""
        self._build_ui(accounts)

    def _build_ui(self, accounts: list[str]) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel("사용할 계좌를 선택하세요")
        title.setFont(QFont("Malgun Gothic", 11, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setObjectName("title")
        layout.addWidget(title)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("divider")
        layout.addWidget(line)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setFont(QFont("Malgun Gothic", 10))
        self._list.setStyleSheet(
            "QListWidget { background:#313244; border:1px solid #45475a; border-radius:4px; color:#cdd6f4; }"
            "QListWidget::item:selected { background:#89b4fa; color:#1e1e2e; }"
            "QListWidget::item { padding:6px 8px; }"
        )
        for acc in accounts:
            self._list.addItem(QListWidgetItem(acc))
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setObjectName("divider")
        layout.addWidget(line2)

        btn_ok = QPushButton("확인")
        btn_ok.setObjectName("btn_login")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._on_ok)
        layout.addWidget(btn_ok)

    def _on_ok(self) -> None:
        item = self._list.currentItem()
        if item:
            self._selected = item.text()
        self.accept()

    def _on_double_click(self, item: QListWidgetItem) -> None:
        self._selected = item.text()
        self.accept()

    @property
    def selected_account(self) -> str:
        return self._selected


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
