import logging
import threading
import json
from pathlib import Path
from datetime import date
from typing import Dict, Any
from PyQt5.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

class AppState(QObject):
    """
    프로그램의 모든 동적 상태를 관리하는 중앙 상태 관리자.
    [Phase 1/2] 세션 영속화 및 스레드 안전성 강화 버전.
    """
    
    # 상태 변경 알림 시그널
    auto_trading_changed = pyqtSignal(bool)
    overnight_mode_changed = pyqtSignal(bool)
    risk_params_changed = pyqtSignal(float, float)
    account_changed = pyqtSignal(str, str)
    market_data_updated = pyqtSignal(float, float, float, float, bool)
    portfolio_updated = pyqtSignal(dict)
    profit_locked_changed = pyqtSignal(bool)
    loss_cut_locked_changed = pyqtSignal(bool)
    pnl_updated = pyqtSignal(float)                # 당일 손익 변경 알림
    log_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._session_path = Path(__file__).parent.parent / "params" / "session_state.json"

        # 내부 상태 저장소
        self._auto_trading = False
        self._overnight_mode = False
        self.kospi_chg: float = 0.0
        self.kosdaq_chg: float = 0.0
        self.index_history: dict[str, list[float]] = {"KOSPI": [], "KOSDAQ": []}
        self.max_history: int = 10  # 최근 10분 지수 저장
        self._tp_pct = 3.0
        self._sl_pct = -2.5
        self._account = "—"
        self._server_mode = "미연결"
        self._is_crash = False
        self._profit_locked = False
        self._loss_cut_locked = False
        self._daily_realized_pnl = 0.0 # [NEW] 당일 실현 손익 중앙 관리

        # 포트폴리오 상태
        self._cash = 0
        self._positions = {}

        # 지수 정보 캐시
        self._market_data = {
            "kp_cur": 0.0, "kp_chg": 0.0,
            "kd_cur": 0.0, "kd_chg": 0.0
        }

        # [Phase 1] 세션 로드
        self.load_session()

    # ── Properties & Setters ───────────────────────────────────────────────

    @property
    def auto_trading(self) -> bool:
        with self._lock: return self._auto_trading

    @auto_trading.setter
    def auto_trading(self, value: bool):
        with self._lock:
            if self._auto_trading != value:
                self._auto_trading = value
                logger.info("[Context] 자동매매 상태 변경 -> %s", "시작" if value else "정지")
                self.save_session()
        self.auto_trading_changed.emit(value)

    @property
    def daily_realized_pnl(self) -> float:
        with self._lock: return self._daily_realized_pnl

    @daily_realized_pnl.setter
    def daily_realized_pnl(self, value: float):
        with self._lock:
            if self._daily_realized_pnl != value:
                self._daily_realized_pnl = value
                self.save_session()
        self.pnl_updated.emit(value)

    @property
    def profit_locked(self) -> bool:
        with self._lock: return self._profit_locked
    
    @profit_locked.setter
    def profit_locked(self, value: bool):
        with self._lock:
            if self._profit_locked != value:
                self._profit_locked = value
                logger.warning("[AppState] 수익 잠금 상태 변경 -> %s", "LOCKED" if value else "UNLOCKED")
                self.save_session()
        self.profit_locked_changed.emit(value)

    @property
    def loss_cut_locked(self) -> bool:
        with self._lock: return self._loss_cut_locked
    
    @loss_cut_locked.setter
    def loss_cut_locked(self, value: bool):
        with self._lock:
            if self._loss_cut_locked != value:
                self._loss_cut_locked = value
                logger.warning("[AppState] 손절 잠금 상태 변경 -> %s", "LOCKED" if value else "UNLOCKED")
                self.save_session()
        self.loss_cut_locked_changed.emit(value)

    @property
    def risk_locked(self) -> bool:
        """하위 호환성용: 수익 또는 손절 잠금 중 하나라도 활성화 시 True"""
        with self._lock:
            return self._profit_locked or self._loss_cut_locked

    @property
    def overnight_mode(self) -> bool:
        with self._lock: return self._overnight_mode

    @overnight_mode.setter
    def overnight_mode(self, value: bool):
        with self._lock:
            if self._overnight_mode != value:
                self._overnight_mode = value
                logger.info("[Context] 야간보유 모드 변경 -> %s", "ON" if value else "OFF")
                self.save_session()
        self.overnight_mode_changed.emit(value)

    # (기타 기존 프로퍼티들은 유지됨 - 생략 방지를 위해 아래에 계속)

    @property
    def tp_pct(self) -> float:
        with self._lock: return self._tp_pct

    @property
    def sl_pct(self) -> float:
        with self._lock: return self._sl_pct

    def set_risk_params(self, tp: float = None, sl: float = None):
        with self._lock:
            changed = False
            if tp is not None and self._tp_pct != tp:
                self._tp_pct = tp
                changed = True
            if sl is not None and self._sl_pct != sl:
                self._sl_pct = sl
                changed = True
            if changed:
                self.save_session()
                tp_copy, sl_copy = self._tp_pct, self._sl_pct

        if changed:
            self.risk_params_changed.emit(tp_copy, sl_copy)

    @property
    def account(self) -> str:
        with self._lock: return self._account

    @property
    def server_mode(self) -> str:
        with self._lock: return self._server_mode

    def set_account(self, account: str, mode: str):
        with self._lock:
            if self._account != account or self._server_mode != mode:
                self._account = account
                self._server_mode = mode
                self.account_changed.emit(self._account, self._server_mode)

    @property
    def is_crash(self) -> bool:
        with self._lock: return self._is_crash

    def update_index(self, kospi: float, kosdaq: float) -> None:
        with self._lock:
            self.kospi_chg = kospi
            self.kosdaq_chg = kosdaq
            # 히스토리 업데이트
            for name, val in [("KOSPI", kospi), ("KOSDAQ", kosdaq)]:
                self.index_history[name].append(val)
                if len(self.index_history[name]) > self.max_history:
                    self.index_history[name].pop(0)

    def update_market_data(self, kp_cur: float, kp_chg: float, kd_cur: float, kd_chg: float, is_crash: bool):
        with self._lock:
            self._market_data.update({"kp_cur": kp_cur, "kp_chg": kp_chg, "kd_cur": kd_cur, "kd_chg": kd_chg})
            self._is_crash = is_crash
        self.market_data_updated.emit(kp_cur, kp_chg, kd_cur, kd_chg, is_crash)

    @property
    def cash(self) -> int:
        with self._lock: return self._cash

    @property
    def positions(self) -> Dict[str, Any]:
        with self._lock: return dict(self._positions)

    def update_portfolio(self, cash: int, positions: dict):
        with self._lock:
            self._cash = cash
            self._positions = dict(positions)
            cash_copy = self._cash
            positions_copy = dict(self._positions)
        self.portfolio_updated.emit({"cash": cash_copy, "positions": positions_copy})

    def append_log(self, msg: str):
        self.log_requested.emit(msg)

    # ── Persistence (Session Management) ───────────────────────────────────

    def save_session(self):
        """현재 핵심 상태를 파일에 기록한다."""
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    "date": str(date.today()),
                    "daily_realized_pnl": self._daily_realized_pnl,
                    "profit_locked": self._profit_locked,
                    "loss_cut_locked": self._loss_cut_locked,
                    "overnight_mode": self._overnight_mode,
                    "auto_trading": self._auto_trading,
                    "tp_pct": self._tp_pct,
                    "sl_pct": self._sl_pct
                }
            with open(self._session_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error("[AppState] 세션 저장 실패: %s", e)

    def load_session(self):
        """파일에서 이전 세션 정보를 복원한다."""
        if not self._session_path.exists():
            return

        try:
            with open(self._session_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # 날짜가 오늘인 경우에만 복원 (자정이 지나면 초기화)
            if data.get("date") == str(date.today()):
                with self._lock:
                    self._daily_realized_pnl = data.get("daily_realized_pnl", 0.0)
                    self._profit_locked = data.get("profit_locked", False)
                    self._loss_cut_locked = data.get("loss_cut_locked", False)
                    self._overnight_mode = data.get("overnight_mode", False)
                    self._auto_trading = data.get("auto_trading", False)
                    self._tp_pct = data.get("tp_pct", 3.0)
                    self._sl_pct = data.get("sl_pct", -2.5)
                logger.info("[AppState] 세션 데이터 복원 완료 (PnL: %s, PL:%s, LC:%s)", 
                            self._daily_realized_pnl, self._profit_locked, self._loss_cut_locked)
            else:
                logger.info("[AppState] 이전 날짜 세션 발견 — 신규 세션 시작")
        except Exception as e:
            logger.error("[AppState] 세션 로드 실패: %s", e)
