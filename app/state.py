"""
AppState - 프로그램 전체의 상태를 관리하는 중앙 상태 클래스
스레드-안전 (Thread-Safe) 구현으로 동시 접근 제어
"""

from PyQt5.QtCore import QObject, pyqtSignal
import logging
import threading
from typing import Dict, Any

logger = logging.getLogger(__name__)

class AppState(QObject):
    """
    프로그램의 모든 동적 상태를 관리하는 중앙 상태 관리자.
    Single Source of Truth 역할을 하며, 상태 변경 시 시그널을 통해 UI/엔진에 알림을 보냅니다.
    """
    
    # 상태 변경 알림 시그널
    auto_trading_changed = pyqtSignal(bool)
    overnight_mode_changed = pyqtSignal(bool)
    risk_params_changed = pyqtSignal(float, float) # (tp_pct, sl_pct)
    account_changed = pyqtSignal(str, str)         # (account, server_mode)
    market_data_updated = pyqtSignal(float, float, float, float, bool) # (kp_cur, kp_chg, kd_cur, kd_chg, is_crash)
    portfolio_updated = pyqtSignal(dict)           # {cash: int, positions: dict}
    risk_locked_changed = pyqtSignal(bool)         # 리스크 락 상태 변경
    log_requested = pyqtSignal(str)                # 전역 로그 요청

    def __init__(self):
        super().__init__()

        # 스레드-안전성을 위한 뮤텍스
        self._lock = threading.RLock()

        # 내부 상태 저장소
        self._auto_trading = False
        self._overnight_mode = False
        self._tp_pct = 3.0
        self._sl_pct = -2.5
        self._account = "—"
        self._server_mode = "미연결"
        self._is_crash = False
        self._risk_locked = False

        # 포트폴리오 상태
        self._cash = 0
        self._positions = {}

        # 지수 정보 캐시
        self._market_data = {
            "kp_cur": 0.0, "kp_chg": 0.0,
            "kd_cur": 0.0, "kd_chg": 0.0
        }

    # ── Properties & Setters ───────────────────────────────────────────────

    @property
    def auto_trading(self) -> bool:
        with self._lock:
            return self._auto_trading

    @auto_trading.setter
    def auto_trading(self, value: bool):
        with self._lock:
            if self._auto_trading != value:
                self._auto_trading = value
                logger.info("[Context] 자동매매 상태 변경 -> %s", "시작" if value else "정지")
        self.auto_trading_changed.emit(value)

    @property
    def overnight_mode(self) -> bool:
        with self._lock:
            return self._overnight_mode

    @overnight_mode.setter
    def overnight_mode(self, value: bool):
        with self._lock:
            if self._overnight_mode != value:
                self._overnight_mode = value
                logger.info("[Context] 야간보유 모드 변경 -> %s", "ON" if value else "OFF")
        self.overnight_mode_changed.emit(value)

    @property
    def tp_pct(self) -> float:
        with self._lock:
            return self._tp_pct

    @property
    def sl_pct(self) -> float:
        with self._lock:
            return self._sl_pct

    def set_risk_params(self, tp: float = None, sl: float = None):
        """익절/손절 파라미터를 한 번에 업데이트 (원자적 작업)"""
        with self._lock:
            changed = False
            if tp is not None and self._tp_pct != tp:
                self._tp_pct = tp
                changed = True
            if sl is not None and self._sl_pct != sl:
                self._sl_pct = sl
                changed = True

            if changed:
                logger.info("[Context] 리스크 파라미터 변경: TP=%.1f%%, SL=%.1f%%", self._tp_pct, self._sl_pct)
                tp_copy = self._tp_pct
                sl_copy = self._sl_pct

        if changed:
            self.risk_params_changed.emit(tp_copy, sl_copy)

    @property
    def account(self) -> str:
        with self._lock:
            return self._account

    @property
    def server_mode(self) -> str:
        with self._lock:
            return self._server_mode

    def set_account(self, account: str, mode: str):
        with self._lock:
            if self._account != account or self._server_mode != mode:
                self._account = account
                self._server_mode = mode
                logger.info("[Context] 계정 정보 업데이트: %s (%s)", account, mode)
                acc_copy = self._account
                mode_copy = self._server_mode
            else:
                acc_copy = None
                mode_copy = None

        if acc_copy is not None:
            self.account_changed.emit(acc_copy, mode_copy)

    @property
    def is_crash(self) -> bool:
        with self._lock:
            return self._is_crash

    @property
    def risk_locked(self) -> bool:
        with self._lock:
            return self._risk_locked

    @risk_locked.setter
    def risk_locked(self, value: bool):
        with self._lock:
            if self._risk_locked != value:
                self._risk_locked = value
                logger.warning("[AppState] 리스크 잠금 상태 변경 -> %s", "LOCKED" if value else "UNLOCKED")
        self.risk_locked_changed.emit(value)

    def update_market_data(self, kp_cur: float, kp_chg: float, kd_cur: float, kd_chg: float, is_crash: bool):
        """지수 데이터 업데이트 및 크래시 상태 반영 (원자적 작업)"""
        with self._lock:
            self._market_data.update({
                "kp_cur": kp_cur, "kp_chg": kp_chg,
                "kd_cur": kd_cur, "kd_chg": kd_chg
            })
            self._is_crash = is_crash
        # 신호는 락 밖에서 발행 (대기 시간 최소화)
        self.market_data_updated.emit(kp_cur, kp_chg, kd_cur, kd_chg, is_crash)

    @property
    def cash(self) -> int:
        with self._lock:
            return self._cash

    @property
    def positions(self) -> Dict[str, Any]:
        """포지션 딕셔너리의 복사본 반환 (스레드-안전)"""
        with self._lock:
            return dict(self._positions)

    def update_portfolio(self, cash: int, positions: dict):
        """잔고/포지션 정보 업데이트 (원자적 작업)"""
        with self._lock:
            self._cash = cash
            self._positions = dict(positions)  # 얕은 복사로 외부 수정으로부터 격리
            portfolio_copy = {"cash": self._cash, "positions": dict(self._positions)}
        # 신호는 락 밖에서 발행
        self.portfolio_updated.emit(portfolio_copy)

    def append_log(self, msg: str):
        """UI 패널에 로그 출력을 요청하는 헬퍼"""
        self.log_requested.emit(msg)
