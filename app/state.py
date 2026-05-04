"""
AppState - 프로그램 전체의 상태를 관리하는 중앙 상태 클래스
"""

from PyQt5.QtCore import QObject, pyqtSignal
import logging

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
    log_requested = pyqtSignal(str)                # 전역 로그 요청

    def __init__(self):
        super().__init__()
        
        # 내부 상태 저장소
        self._auto_trading = False
        self._overnight_mode = False
        self._tp_pct = 3.0
        self._sl_pct = -2.5
        self._account = "—"
        self._server_mode = "미연결"
        self._is_crash = False
        
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
        return self._auto_trading

    @auto_trading.setter
    def auto_trading(self, value: bool):
        if self._auto_trading != value:
            self._auto_trading = value
            logger.info("[Context] 자동매매 상태 변경 -> %s", "시작" if value else "정지")
            self.auto_trading_changed.emit(value)

    @property
    def overnight_mode(self) -> bool:
        return self._overnight_mode

    @overnight_mode.setter
    def overnight_mode(self, value: bool):
        if self._overnight_mode != value:
            self._overnight_mode = value
            logger.info("[Context] 야간보유 모드 변경 -> %s", "ON" if value else "OFF")
            self.overnight_mode_changed.emit(value)

    @property
    def tp_pct(self) -> float:
        return self._tp_pct

    @property
    def sl_pct(self) -> float:
        return self._sl_pct

    def set_risk_params(self, tp: float = None, sl: float = None):
        """익절/손절 파라미터를 한 번에 업데이트"""
        changed = False
        if tp is not None and self._tp_pct != tp:
            self._tp_pct = tp
            changed = True
        if sl is not None and self._sl_pct != sl:
            self._sl_pct = sl
            changed = True
            
        if changed:
            logger.info("[Context] 리스크 파라미터 변경: TP=%.1f%%, SL=%.1f%%", self._tp_pct, self._sl_pct)
            self.risk_params_changed.emit(self._tp_pct, self._sl_pct)

    @property
    def account(self) -> str:
        return self._account

    @property
    def server_mode(self) -> str:
        return self._server_mode

    def set_account(self, account: str, mode: str):
        if self._account != account or self._server_mode != mode:
            self._account = account
            self._server_mode = mode
            logger.info("[Context] 계정 정보 업데이트: %s (%s)", account, mode)
            self.account_changed.emit(account, mode)

    @property
    def is_crash(self) -> bool:
        return self._is_crash

    def update_market_data(self, kp_cur: float, kp_chg: float, kd_cur: float, kd_chg: float, is_crash: bool):
        """지수 데이터 업데이트 및 크래시 상태 반영"""
        self._market_data.update({
            "kp_cur": kp_cur, "kp_chg": kp_chg,
            "kd_cur": kd_cur, "kd_chg": kd_chg
        })
        self._is_crash = is_crash
        # 지수 정보는 빈번하게 업데이트되므로 로그는 필요한 경우만 남기거나 생략
        self.market_data_updated.emit(kp_cur, kp_chg, kd_cur, kd_chg, is_crash)
        
    @property
    def cash(self) -> int:
        return self._cash
        
    @property
    def positions(self) -> dict:
        return self._positions

    def update_portfolio(self, cash: int, positions: dict):
        """잔고/포지션 정보 업데이트"""
        self._cash = cash
        self._positions = positions
        self.portfolio_updated.emit({"cash": cash, "positions": positions})

    def append_log(self, msg: str):
        """UI 패널에 로그 출력을 요청하는 헬퍼"""
        self.log_requested.emit(msg)
