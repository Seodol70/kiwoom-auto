"""
OrderManager — 주문 관리 모듈

역할:
  1. 잔고 동기화  : opw00018 조회로 가용 예수금 확인
  2. 주문 실행    : SendOrder() 시장가 / 지정가
  3. 체결 확인    : OnReceiveChejanData 로 실시간 체결 반영
  4. 안전 장치    : 중복 매수 방지 / 1회 주문 한도 / 최대 보유 종목 수

스레딩:
  SendOrder() 는 반드시 메인 Qt 스레드에서 호출해야 한다.
  ScannerWorker → pyqtSignal → OrderManager.handle_signal (메인 스레드)
  이 흐름을 유지하면 Qt 가 자동으로 스레드 경계를 넘긴다.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Optional

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from app.config_manager import config_manager as cfg
from logging_config import order_log, position_log
from order.position_repository import PositionRepository
from order.order_types import OrderType, PriceType

logger = logging.getLogger(__name__)

_FEE = cfg.COST.get("fee_rate", 0.00015)
_TAX = cfg.COST.get("tax_rate", 0.0023)


# ─────────────────────────────────────────────────────────────────────────────
# 주문 대기 메타데이터 (Phase G-1: 7개 pending dict → 단일 dataclass)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PendingOrderMeta:
    """주문 체결 대기 중 추적하는 종목별 메타데이터."""
    candle_stop: int = 0                    # 진입 캔들 저가 (체결 시 Position에 반영)
    trend_level: int = 0                    # 추세 레벨
    trend_prev_level: int = 0               # 이전 추세 레벨
    near_daily_high: bool = False           # 일봉 고점 근처 여부
    custom_tp_pct: float = 0.0              # 커스텀 익절 %
    sector: str = ""                        # 업종명 (섹터 쏠림 방지용)
    eod_trade: bool = False                 # EOD 거래 플래그
    entry_phase: int = 0                    # 진입 페이즈 (1=모닝스캘핑, 2=메인)


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

# 체결 구분 (OnReceiveChejanData)
CHEJAN_ORDER = "0"    # 주문 접수/확인 (체결 포함, gubun "0")
CHEJAN_FILL  = "0"    # 체결 — Kiwoom gubun "0" 이 주문+체결, "1" 은 잔고변동


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class OrderRecord:
    """주문 1건 기록"""
    order_no:   str
    code:       str
    name:       str
    order_type: int           # 1=매수, 2=매도
    qty:        int
    price:      int           # 0=시장가
    status:     str = "접수"  # 접수 → 체결 → 취소
    filled_qty: int = 0
    filled_price: int = 0
    ordered_at: datetime = field(default_factory=datetime.now)
    filled_at:  Optional[datetime] = None


@dataclass
class Position:
    """보유 포지션"""
    code:       str
    name:       str
    qty:        int
    avg_price:  int
    current_price: int = 0
    # 앱 주문으로 인지한 메타 (opw00018 동기화 시 기존 값 병합)
    buy_date: Optional[date] = None       # 앱 매수로 최초 반영된 날짜(표시·필터 보조)
    entry_time: Optional[datetime] = None # ← 2026-04-03 추가: 정확한 진입 시각 (Time-cut용)
    opened_by_app: bool = False           # 앱 SendOrder 매수로 보유가 생기거나 늘어난 적 있음
    qty_buy_today_app: int = 0            # 오늘 앱에서 매수한 수량 (장마감 자동청산 대상)
    candle_stop_price: int = 0            # 진입 캔들 저가 기반 손절가 (0 = 비활성)
    break_even_done: bool = False         # (레거시 — 미사용)
    half_exited: bool = False             # (레거시 — 미사용, 트레일 스탑으로 대체)
    peak_price: int = 0                   # [Trail] 보유 중 최고가 (트레일 스탑 기준점)
    # [분할 익절] 1차 분할 매도 상태
    partial_sold:     bool = False        # 1차 분할 매도 완료 여부
    qty_partial_sold: int  = 0            # 1차에 매도한 수량
    trend_level: int = 0                  # 요셉 시그널 현재 추세 단계(0~3)
    trend_prev_level: int = 0             # 직전 추세 단계(Strong→No Trend 감시)
    near_daily_high: bool = False         # 진입 시 25일 신고가 근처 → TP 상향 적용
    custom_tp_pct:   float = 0.0          # 종목별 익절 목표 (0 = 전역 설정값 사용)
    # 종가매매(EOD) 플래그
    eod_trade:       bool = False         # True → 당일 15:19 강제청산 제외, 익일 관리
    overnight_held:  bool = False         # True → 익일 장 시작 후 갭 체크 관리 중
    # 매매 단계 태그
    entry_phase:     int  = 0             # 0=미분류, 1=모닝스캘핑(09~10:30), 2=메인전략(10~14:40)
    # 섹터 쏠림 방지
    sector:          str  = ""            # 업종명 (opt10001 응답, 섹터 노출 집계용)
    entry_count:     int  = 1             # 진입 횟수 (피라미딩 추적용)

    @property
    def pnl(self) -> int:
        """평가손익(원): 수수료·세금 차감 후."""
        if not self.current_price or not self.avg_price:
            return 0
        buy_total = self.avg_price * self.qty
        sell_total = self.current_price * self.qty
        fees = buy_total * _FEE + sell_total * _FEE
        tax = sell_total * _TAX
        return int(sell_total - buy_total - fees - tax)

    @property
    def pnl_pct(self) -> float:
        """평가손익률(%): 수수료·세금 차감 전 단순 계산."""
        if not self.avg_price or self.avg_price <= 0:
            return 0.0
        cur = self.current_price if self.current_price > 0 else self.avg_price
        return (cur - self.avg_price) / self.avg_price * 100

    @property
    def return_pct_vs_avg(self) -> float:
        """수수료·세금 차감 후 실질 수익률(%)."""
        cost = self.avg_price * self.qty
        if not cost:
            return 0.0
        return self.pnl / cost * 100.0

    @property
    def price_change_pct_vs_avg(self) -> float:
        """순수 매수평단 대비 등락률(%) — (현재가−평단)/평단×100, 수수료·세금 미반영."""
        if self.avg_price <= 0:
            return 0.0
        cp = self.current_price
        if cp <= 0:
            return 0.0
        return (cp - self.avg_price) / self.avg_price * 100.0

    @property
    def unrealized_cost_minus_value(self) -> int:
        """평균단가×수량 − 현재가×수량 (매입금액 − 평가금액, UI 손익 열)."""
        return self.avg_price * self.qty - self.current_price * self.qty



# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------

class OrderManager(QObject):
    """
    주문 관리자.

    Qt Signal 로 UI 에 이벤트를 전달한다.
    메인 Qt 스레드에서 생성/사용해야 한다.

    사용 예)
        om = OrderManager(kiwoom, account="1234567890")
        scanner.on_signal = om.handle_signal    # 스캐너 콜백 연결
        om.order_filled.connect(ui.on_filled)   # UI 연결
    """

    # ── Qt 시그널 ──────────────────────────────────────────────────────────
    order_sent   = pyqtSignal(dict)   # 주문 전송 완료 → UI 로그
    order_filled = pyqtSignal(dict)   # 체결 확인    → UI 로그 + 포트폴리오 갱신
    order_failed = pyqtSignal(str)    # 주문 실패    → UI 경고

    def __init__(
        self,
        kiwoom,
        account:         str   = "",
        max_order_amount: int  = 1_500_000,    # 1회 최대 주문 금액 (원)
        max_positions:   int   = 5,             # 최대 보유 종목 수
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._kiwoom          = kiwoom
        self._account         = account
        self.max_order_amount = max_order_amount
        self.max_positions    = max_positions

        # OrderExecutor 초기화 (Kiwoom SendOrder 전담)
        from order.order_executor import OrderExecutor
        self._executor = OrderExecutor(kiwoom, account, max_order_amount)

        self.cash:      int = 0                          # 가용 예수금
        self.positions: dict[str, Position] = {}         # 보유 종목
        self.position_repo = PositionRepository(self.positions)  # [NEW Phase 1] positions 캡슐화
        self.orders:    dict[str, OrderRecord] = {}      # 전체 주문 기록
        self._pending:  set[str] = set()                 # 주문 중 종목 (중복 방지)
        self._pending_sell_time: dict[str, datetime] = {}  # 매도 주문 접수 시각
        self._app_pending_buys: dict[str, int] = {}       # code -> 남은 앱 매수 주문 수량 (부분체결 추적)
        self._pending_meta: dict[str, PendingOrderMeta] = {}  # code -> 주문 메타데이터 (Phase G-1)
        self._pnl_date: date = date.today()
        # 당일 실현손익 = 파일에서 복구한 이전 세션 합 + 이번 세션 매도 체결 합
        self.daily_realized_pnl: int = 0
        self._broker_realized_base: int = 0               # 시작 시 파일/opt10074에서 복구한 당일 누적 기준값
        self._today_fill_log: list[dict] = []             # 이번 세션 체결 로그
        self._fills_initialized: bool = False             # 시작 시 1회만 파일 로드하는 플래그

        self._signal_last_time: dict[str, float] = {}     # code → 마지막 신호 시각 (쿨다운용)
        self._queued_signal = None                         # 매도 체결 완료 후 실행 대기 중인 매수 신호

        # [NEW] 미체결 추적 (2026-04-04)
        self._failed_sells: dict[str, dict] = {}  # code → {qty, attempts, last_time}

        # 매도 주문 30초 타임아웃 재시도 횟수 — 2회 이상 시 지정가로 에스컬레이션
        self._pending_sell_retries: dict[str, int] = {}  # code → timeout 횟수

        # [P2] 매수 주문 접수 시각 추적 — 10초 미체결 시 취소+재주문
        self._pending_buy_time: dict[str, datetime] = {}   # code → 접수 시각
        self._pending_buy_info: dict[str, dict] = {}       # code → {qty, name, order_no}

        # [취소 후 뒤늦은 체결 방어] 취소한 매수 주문 order_no 추적
        # 취소 확인 후에도 체결이 오면 즉시 시장가 청산
        self._cancelled_buy_orders: set[str] = set()

        # 분할익절 체결 대기 — 이 코드의 다음 sell 체결은 is_partial=True 로 기록
        self._partial_pending_codes: set[str] = set()

        # FID 911(체결량)은 주문 내 누적 체결량으로 전달됨 → 증분 계산을 위해 이전값 추적
        # {order_no: last_cumulative_qty}
        self._order_fill_cumulative: dict[str, int] = {}

        # [NEW] 당일 손절 블랙리스트 — 손절 체결 종목은 당일 재매수 차단 (2026-04-08)
        # 익절·Time-cut·수동 매도는 포함하지 않음 → 재진입 허용
        self._stop_loss_today: set[str] = set()

        # 강제 매도(Hard Stop / Time-cut / 청산) 발령 중인 종목 추적
        # → 체결 전까지 중복 force_exit 발령 차단 (무한루프 방지)
        self._force_sell_issued: set[str] = set()

        # 포지션 실시간 현재가 구독 콜백 (SmartScanner에서 주입)
        self.on_position_opened: Optional[Callable[[str], None]] = None
        self.on_position_closed: Optional[Callable[[str], None]] = None

        self.state = None  # AppState 주입용
        self._health = None  # HealthMonitor 주입용

        self._connect_chejan()

    def set_account(self, account: str):
        """계좌번호 업데이트 (로그인 성공 시 ApplicationContext에서 호출)"""
        self._account = account
        if self._executor:
            self._executor.set_account(account)
        # [NEW] Kiwoom API 객체 내부 계좌도 강제 업데이트
        if hasattr(self._kiwoom, "_account"):
            self._kiwoom._account = account
        logger.info("[OrderManager] 계좌번호 설정 완료: %s", account)

    def set_state(self, state):
        """AppState 주입 (MainWindow에서 호출)"""
        self.state = state
        # [Phase 1] 세션에서 복구된 손익이 있다면 동기화
        if state.daily_realized_pnl != 0:
            self.daily_realized_pnl = int(state.daily_realized_pnl)
            logger.info("[OrderManager] AppState 세션 손익 복구: %s원", f"{self.daily_realized_pnl:,}")
        logger.info("[OrderManager] AppState 주입 완료")

    def set_health_monitor(self, monitor):
        """HealthMonitor 주입 (MainWindow에서 호출)"""
        self._health = monitor
        logger.info("[OrderManager] HealthMonitor 주입 완료")

    # -----------------------------------------------------------------------
    # 속성
    # -----------------------------------------------------------------------

    @property
    def available_cash(self) -> int:
        """가용 예수금 (TradingController 호환 프로퍼티)."""
        return self.cash

    @property
    def total_equity(self) -> int:
        """총 자산 (예수금 + 보유종목 평가금액)."""
        mv = sum(p.qty * p.current_price for p in self.positions.values() if p.current_price > 0)
        return self.cash + mv

    # -----------------------------------------------------------------------
    # 주문 메시지 콜백 (OnReceiveMsg → kiwoom_api → 여기)
    # -----------------------------------------------------------------------

    def on_order_msg(self, rq_name: str, msg: str) -> None:
        """
        키움 OnReceiveMsg 콜백 — 주문 수신 결과 처리.
        [800033] 매도가능수량 부족: 포지션이 서버에 없음 → 로컬 메모리 정리.
        """
        if "800033" not in msg:
            return

        # rq_name 형식: "매도_XXXXXX"
        if not rq_name.startswith("매도_"):
            return

        code = rq_name[len("매도_"):]
        logger.warning(
            "[800033] %s 매도가능수량 부족 — 포지션 메모리 정리 (서버에 해당 수량 없음)",
            code,
        )

        # 강제 매도 발령 해제 → 재시도 허용하지 않음 (포지션 없으니)
        self._force_sell_issued.discard(code)
        self._pending.discard(code)
        self._pending_sell_time.pop(code, None)
        self._pending_sell_retries.pop(code, None)
        self._failed_sells.pop(code, None)

        # 로컬 포지션 제거 — 서버에 없는 포지션이므로
        pos = self.positions.pop(code, None)
        if pos is not None:
            logger.warning(
                "[800033] %s 포지션 메모리 삭제 — qty=%d (FID911 오버카운트 등 원인)",
                code, pos.qty,
            )
            if self.on_position_closed:
                self.on_position_closed(code)

    # -----------------------------------------------------------------------
    # 잔고 동기화
    # -----------------------------------------------------------------------

    def sync_balance(self) -> int:
        """
        opw00018 조회로 예수금과 보유 포지션을 갱신한다.
        메인 스레드에서 주기적으로 호출한다. (예: QTimer 5분 주기)
        """
        try:
            self._roll_daily_state_if_needed()
            balance = self._kiwoom.get_balance()
            server_cash = balance.get("cash", 0)

            # TR이 차단된 경우(재진입 방지) balance가 빈 dict → 잔고 갱신 스킵
            if not balance:
                import logging as _lg
                _lg.getLogger(__name__).debug("sync_balance: TR 처리 중 — 잔고 동기화 스킵")
                return self.cash

            holdings = self._kiwoom.get_holdings()

            # 홀딩 조회도 차단된 경우 — 포지션을 빈 목록으로 덮어쓰지 않음
            if not holdings and self.positions:
                import logging as _lg
                _lg.getLogger(__name__).debug("sync_balance: TR 처리 중 — 포지션 동기화 스킵")
                # 예수금만 최신화
                invested = sum(p.avg_price * p.qty for p in self.positions.values())
                self.cash = max(0, server_cash - invested)
                return self.cash

            # opw00018 모의투자 서버는 매입가 필드를 반환하지 않음 → 기존 메모리값 보존
            new_positions: dict[str, Position] = {}
            for h in holdings:
                qty = h.get("qty", 0)
                if qty <= 0:
                    # 보유수량 0 or 키 없음 — 불완전 응답 방어 (KeyError 'qty' 재발 방지)
                    logger.warning("sync_balance: 보유수량 0 또는 누락 — %s(%s) 스킵",
                                   h.get("name", "?"), h.get("code", "?"))
                    continue
                code = h["code"]
                avg = h.get("avg_price", 0)
                if avg == 0 and code in self.positions:
                    avg = self.positions[code].avg_price
                old = self.positions.get(code)
                qty_today = min(old.qty_buy_today_app, qty) if old else 0
                new_positions[code] = Position(
                    code              = code,
                    name              = h.get("name", ""),
                    qty               = qty,
                    avg_price         = avg,
                    current_price     = h.get("current_price", 0),
                    buy_date          = old.buy_date if old else None,
                    entry_time        = old.entry_time if old else None,
                    opened_by_app     = old.opened_by_app if old else False,
                    qty_buy_today_app = qty_today,
                    candle_stop_price = old.candle_stop_price if old else 0,
                    break_even_done   = old.break_even_done if old else False,
                    half_exited       = old.half_exited if old else False,
                    peak_price        = old.peak_price        if old else 0,
                    partial_sold      = old.partial_sold      if old else False,
                    qty_partial_sold  = old.qty_partial_sold  if old else 0,
                    trend_level       = old.trend_level       if old else 0,
                    trend_prev_level  = old.trend_prev_level  if old else 0,
                    entry_phase       = old.entry_phase       if old else 0,
                    sector            = old.sector            if old else "",
                )
            self.positions = new_positions

            # 모의투자 서버는 opw00001 "예수금"을 투자금 차감 없이 반환한다.
            # 실전투자 서버는 이미 차감된 "예수금" 또는 "D+2추정예수금"을 반환하므로 차감하지 않는다.
            if self._kiwoom.is_mock:
                invested = sum(p.avg_price * p.qty for p in self.positions.values())
                self.cash = max(0, server_cash - invested)
            else:
                self.cash = server_cash
            logger.info("잔고 동기화 완료 — 예수금 %s원 (서버=%s / 투자=%s) / 보유 %d종목",
                        f"{self.cash:,}", f"{server_cash:,}", f"{invested:,}",
                        len(self.positions))
            
            # [NEW] AppState 직접 업데이트 (Single Source of Truth)
            if self.state:
                self.state.update_portfolio(self.cash, dict(self.positions))

            # [NEW] HealthMonitor 데이터 신선도 갱신
            if self._health:
                self._health.record_portfolio_sync()

            self._sync_daily_realized_from_broker()
        except Exception as e:
            logger.error("잔고 동기화 실패: %s", e)
        return self.cash

    def _sync_with_balance(self, balance: dict) -> int:
        """
        이미 조회된 balance dict로 holdings TR만 실행하여 포지션 갱신.
        PortfolioWorker의 2-step 비동기 잔고 동기화(Part 3)에서 사용.
        balance가 비어 있으면 self.cash 반환 (기존값 유지).
        """
        try:
            server_cash = balance.get("cash", 0)
            if not balance:
                return self.cash

            holdings = self._kiwoom.get_holdings()
            if not holdings and self.positions:
                invested = sum(p.avg_price * p.qty for p in self.positions.values())
                self.cash = max(0, server_cash - invested)
                return self.cash

            # sync_balance()와 동일 — opw00018 응답 기반 포지션 재구성
            new_positions: dict[str, Position] = {}
            for h in holdings:
                qty = h.get("qty", 0)
                if qty <= 0:
                    logger.warning("_sync_with_balance: 보유수량 0 또는 누락 — %s(%s) 스킵",
                                   h.get("name", "?"), h.get("code", "?"))
                    continue
                code = h["code"]
                avg = h.get("avg_price", 0)
                if avg == 0 and code in self.positions:
                    avg = self.positions[code].avg_price
                old = self.positions.get(code)
                qty_today = min(old.qty_buy_today_app, qty) if old else 0
                new_positions[code] = Position(
                    code              = code,
                    name              = h.get("name", ""),
                    qty               = qty,
                    avg_price         = avg,
                    current_price     = h.get("current_price", 0),
                    buy_date          = old.buy_date if old else None,
                    entry_time        = old.entry_time if old else None,
                    opened_by_app     = old.opened_by_app if old else False,
                    qty_buy_today_app = qty_today,
                    candle_stop_price = old.candle_stop_price if old else 0,
                    break_even_done   = old.break_even_done if old else False,
                    half_exited       = old.half_exited if old else False,
                    peak_price        = old.peak_price        if old else 0,
                    partial_sold      = old.partial_sold      if old else False,
                    qty_partial_sold  = old.qty_partial_sold  if old else 0,
                    trend_level       = old.trend_level       if old else 0,
                    trend_prev_level  = old.trend_prev_level  if old else 0,
                    entry_phase       = old.entry_phase       if old else 0,
                    sector            = old.sector            if old else "",
                )
            self.positions = new_positions
            if self._kiwoom.is_mock:
                invested = sum(p.avg_price * p.qty for p in self.positions.values())
                self.cash = max(0, server_cash - invested)
            else:
                self.cash = server_cash
            logger.info("잔고 동기화(2단계) 완료 — 예수금 %s원 / 보유 %d종목",
                        f"{self.cash:,}", len(self.positions))
            
            # [NEW] AppState 직접 업데이트
            if self.state:
                self.state.update_portfolio(self.cash, dict(self.positions))

            # [NEW] HealthMonitor 데이터 신선도 갱신
            if self._health:
                self._health.record_portfolio_sync()

            self._sync_daily_realized_from_broker()
        except Exception as e:
            logger.error("_sync_with_balance 실패: %s", e)
        return self.cash

    # -----------------------------------------------------------------------
    # 자금 회전 헬퍼 (2026-04-03)
    # -----------------------------------------------------------------------

    def _find_worst_position(self) -> Optional[Position]:
        """
        자금 회전 대상 포지션 선택.
        우선순위: 손실 > Time-cut 대상 > 최저 수익률
        """
        candidates = [p for p in self.positions.values() if p.qty > 0]
        if not candidates:
            return None

        now = datetime.now()

        # 1순위: 손실 중인 종목 중 가장 큰 손실
        losses = [p for p in candidates if p.pnl_pct < 0]
        if losses:
            return min(losses, key=lambda p: p.pnl_pct)

        # 2순위: Time-cut 대상 (20분 경과 + 수익 < 0.3%, 2026-04-04 강화)
        for p in candidates:
            if p.entry_time:
                elapsed = (now - p.entry_time).total_seconds() / 60
                if elapsed >= 20 and p.pnl_pct < 0.3:
                    return p

        # 3순위: 가장 낮은 수익률
        return min(candidates, key=lambda p: p.pnl_pct)

    def _find_timecut_position(self) -> Optional[Position]:
        """
        max_positions 초과 시 교체 대상 선택.
        손실 중이거나 Time-cut 조건(20분 + 수익률 < 0.3%)인 포지션만 반환.
        수익 양호 포지션은 None 반환 — 교체 불가.
        """
        candidates = [p for p in self.positions.values() if p.qty > 0]
        if not candidates:
            return None
        now = datetime.now()

        # 손실 중인 종목 중 가장 큰 손실
        losses = [p for p in candidates if p.pnl_pct < 0]
        if losses:
            return min(losses, key=lambda p: p.pnl_pct)

        # Time-cut 대상 (20분 경과 + 수익 < 0.3%)
        for p in candidates:
            if p.entry_time:
                elapsed = (now - p.entry_time).total_seconds() / 60
                if elapsed >= 20 and p.pnl_pct < 0.3:
                    return p

        return None  # 교체 불가 (모두 수익 양호)

    # -----------------------------------------------------------------------
    # 스캐너 신호 수신 → 주문 실행
    # -----------------------------------------------------------------------

    @pyqtSlot(object)
    def handle_signal(self, signal) -> None:
        """
        SmartScanner.on_signal 또는 pyqtSignal 로 호출된다.
        메인 스레드에서 실행이 보장된다.
        """
        code = signal.code
        name = signal.name
        price = signal.price

        # ── [Zone 1] 신호수신 로그 ──────────────────────────────────────────
        _sig_type  = getattr(signal, "signal_type", "?")
        _trend_lv  = int(getattr(signal, "trend_level", 0) or 0)
        _vel_ratio = float(getattr(signal, "exec_velocity_ratio", 0.0) or 0.0)
        _phase_nm  = getattr(signal, "entry_phase", 0)
        order_log.info(
            "[신호수신] %s(%s) 가격=%d 유형=%s trend_lv=%d vel_ratio=%.2f phase=%s",
            name, code, price, _sig_type, _trend_lv, _vel_ratio, _phase_nm,
        )

        # ── 종목 강제 필터(매수 직전) ─────────────────────────────────────
        if not self._is_buy_allowed(code, name):
            return

        _sector = ""
        try:
            _RISK = cfg.RISK
            _mx = float(_RISK.get("max_change_pct", 15.0))
            _info = self._kiwoom.get_stock_info(code)

            # [2026-04-23] opt10001 실패 → 매수 거절 (호가 부족 신호)
            # 원인: opt10001 TR 실패 종목은 이미 호가가 줄어들고 있음
            # 효과: 타임아웃 방지 + 자금 효율화 + 손실 차단
            if _info is None:
                msg = f"매수 거절 — opt10001 실패 (스냅샷/섹터 정보 부족 = 호가 감소 징조)"
                order_log.warning("[opt10001차단] %s(%s) — %s", name, code, msg)
                logger.warning(msg)
                self.order_failed.emit(msg)
                return

            _pct = float(_info.get("change_pct", 0) or 0)
            _sector = str(_info.get("sector", "")).strip()
            logger.debug("[매수 등락률 체크] %s — 현재 등락률: %.2f%% (상한: %.1f%%)", name, _pct, _mx)
            if _pct >= _mx:
                msg = f"매수 차단 — 등락률 {_pct:.1f}% ≥ 상한 {_mx:.1f}% ({name})"
                logger.warning(msg)
                self.order_failed.emit(msg)
                return
        except Exception as _e:
            logger.debug("등락률 사전 확인 실패(무시): %s", _e)

        # ── [Zone 2] 섹터 쏠림 방지 체크 + 섹터 확인 로그 ──────────────────
        if not _sector:
            # OPENING 슬롯(09:00~09:30)에서 섹터 없음 = 시장 불안정 신호 → 거절 (일승 패턴 방지, 2026-04-30)
            now_time = datetime.now().time()
            if datetime.strptime("09:00", "%H:%M").time() <= now_time <= datetime.strptime("09:30", "%H:%M").time():
                msg = "매수 거절 — OPENING 슬롯 + 섹터 정보 없음 (개장 직후 시장 불안정 구간, 2026-04-30)"
                order_log.warning("[섹터차단] %s(%s) — %s", name, code, msg)
                logger.info(msg)
                self.order_failed.emit(msg)
                return
            order_log.warning("[섹터확인] %s(%s) — 섹터 정보 없음 (opt10001 실패 또는 미제공)", name, code)
        if _sector:
            try:
                _RISK_s = cfg.RISK
                _sec_max = int(_RISK_s.get("sector_max_positions", 2))
                _same_cnt = sum(
                    1 for p in self.positions.values()
                    if getattr(p, "sector", "") == _sector
                )
                _sec_dist = {
                    sec: sum(1 for p in self.positions.values() if getattr(p, "sector", "") == sec)
                    for sec in {getattr(p, "sector", "") for p in self.positions.values()} | {_sector}
                    if sec
                }
                order_log.info(
                    "[섹터확인] %s(%s) 섹터=[%s] 동일섹터=%d/%d 전체분포=%s",
                    name, code, _sector, _same_cnt, _sec_max, _sec_dist,
                )
                if _same_cnt >= _sec_max:
                    msg = (f"매수 차단 — 섹터 쏠림 [{_sector}] 이미 {_same_cnt}개 보유 "
                           f"(상한 {_sec_max}개) — {name}({code})")
                    logger.warning(msg)
                    order_log.warning("[섹터차단] %s", msg)
                    self.order_failed.emit(msg)
                    return
            except Exception as _se:
                logger.debug("섹터 쏠림 체크 실패(무시): %s", _se)

        # ── 안전 장치 ────────────────────────────────────────────────────
        if code in self._pending:
            logger.debug("중복 주문 방지 — %s 이미 주문 중", code)
            return

        if code in self.positions:
            # ✅ 피라미딩(추가 진입) 판정
            if self._can_pyramid(code):
                logger.info("🚀 [피라미딩] %s(%s) 추가 진입 조건 충족 (수익률 %.2f%%)", 
                            name, code, self.positions[code].pnl_pct)
                # 추가 진입 허용 (아래 로직 계속 진행)
            else:
                logger.debug("중복 매수 방지 — %s 이미 보유 중 (피라미딩 조건 미충족)", code)
                return

        # [NEW] 당일 손절 블랙리스트 — 손절 종목 당일 재매수 차단 (익절은 허용)
        if code in self._stop_loss_today:
            logger.info("손절 재매수 차단 — %s(%s) 당일 손절 이력", name, code)
            return

        if len(self.positions) + len(self._pending) >= self.max_positions:
            msg = f"최대 보유 종목 수 초과 ({self.max_positions}종목) — 신호 대기열 등록"
            logger.info(msg)
            self._queued_signal = signal   # 매도 체결 완료 시 자동 실행
            return

        # ── 수량 계산 (Dynamic Sizing) ───────────────────────────────────
        mode = getattr(self._scan_cfg, "position_sizing_mode", "EQUAL").upper()
        qty = 0

        if mode == "FIXED":
            # 1. FIXED: 설정된 고정 금액 분할 매수
            budget = int(getattr(self._scan_cfg, "fixed_order_amount", 1_500_000))
            qty = budget // price if price > 0 else 0
            order_log.info("[사이징:FIXED] 목표금액=%s원 -> %d주", f"{budget:,}", qty)

        elif mode == "RISK":
            # 2. RISK: 회당 리스크 한도(예: 총자산 1%) 기반 수량 산출
            # 공식: qty = (총자산 * 리스크%) / (진입가 - 손절가)
            risk_pct = float(getattr(self._scan_cfg, "risk_per_trade_pct", 1.0))
            total_equity = self.total_equity
            risk_amount = int(total_equity * (risk_pct / 100.0))
            
            # 손절가 산출 (기본 손절 % 사용)
            sl_pct = abs(float(getattr(self._scan_cfg, "stop_loss_pct", -1.2)))
            stop_price = int(price * (1 - sl_pct / 100.0))
            risk_per_share = max(1, price - stop_price)
            
            qty = risk_amount // risk_per_share
            order_log.info(
                "[사이징:RISK] 총자산=%s원 리스크=%s원(%s%%) 손절가=%s원 -> %d주",
                f"{total_equity:,}", f"{risk_amount:,}", f"{risk_pct}", f"{stop_price:,}", qty
            )

        else:
            # 3. EQUAL (기존 방식): 예수금 / 남은 슬롯
            remaining_slots = self.max_positions - len(self.positions) - len(self._pending)
            remaining_slots = max(remaining_slots, 1)
            budget = self.cash // remaining_slots
            qty = budget // price if price > 0 else 0
            order_log.info("[사이징:EQUAL] 가용예수금=%s원 슬롯=%d -> %d주", f"{self.cash:,}", remaining_slots, qty)

        # ── 가용 자금 및 주문 한도 체크 ─────────────────────────────────────────
        # 1회 주문 한도(max_order_amount) 적용
        max_qty = self.max_order_amount // price if price > 0 else 0
        if qty > max_qty:
            order_log.info("[사이징] 주문한도 초과 조정: %d -> %d주 (상한 %s원)", qty, max_qty, f"{self.max_order_amount:,}")
            qty = max_qty

        # 가용 예수금(cash) 초과 방지
        can_buy_qty = self.cash // price if price > 0 else 0
        if qty > can_buy_qty:
            order_log.info("[사이징] 예수금 부족 조정: %d -> %d주 (가용 %s원)", qty, can_buy_qty, f"{self.cash:,}")
            qty = can_buy_qty

        if qty <= 0:
            msg = f"매수 거절 — 수량 0 (예수금 부족 또는 가격 오류)"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return

        # 진입 메타데이터 임시 보관 → 체결 콜백에서 Position에 반영 (Phase G-1)
        self._pending_meta[code] = PendingOrderMeta(
            candle_stop=int(getattr(signal, "entry_candle_low", 0) or 0),
            trend_level=int(getattr(signal, "trend_level", 0) or 0),
            trend_prev_level=int(getattr(signal, "trend_prev_level", 0) or 0),
            near_daily_high=bool(getattr(signal, "near_daily_high", False)),
            custom_tp_pct=float(getattr(signal, "custom_tp_pct", 0.0)),
            sector=_sector,
            eod_trade=bool(getattr(signal, "eod_trade", False)),
            entry_phase=int(getattr(signal, "entry_phase", 0)),
        )

        # ✅ 피라미딩인 경우 수량 조절 (기본 50%)
        is_pyramid = code in self.positions
        if is_pyramid:
            pyramid_ratio = float(getattr(cfg.RISK, "pyramid_order_ratio", 0.5))
            qty = max(1, int(qty * pyramid_ratio))
            order_log.info("[피라미딩사이징] 수량 조절 (ratio %.1f): %d주", pyramid_ratio, qty)

        self.buy(code, name, qty, price=0)  # 시장가 매수

    @pyqtSlot(str, int, int)
    def _on_price_updated(self, code: str, price: int, trend_level: int) -> None:
        """SmartScanner 실시간 가격 갱신 신호 처리 (손절/익절 정확도 개선)."""
        if price <= 0:
            return
        # position_repo 를 통해 포지션 현재가 갱신
        if hasattr(self, "position_repo"):
            self.position_repo.update_price(code, price)
        # 폴백: position_repo 없을 경우 직접 갱신
        if code in self.positions:
            self.positions[code].current_price = price
        # 추세 레벨 갱신
        self.update_position_trend(code, trend_level)

    def update_position_trend(self, code: str, trend_level: int) -> None:
        """보유 포지션의 추세 레벨을 갱신한다."""
        pos = self.positions.get(code)
        if pos is None:
            return
        new_lv = int(max(0, min(3, trend_level)))
        if pos.trend_level != new_lv:
            pos.trend_prev_level = int(pos.trend_level)
            pos.trend_level = new_lv

    def should_exit_on_trend_decay(self, code: str) -> bool:
        """
        요셉 시그널 추세 소멸 기반 청산 판정.
        Strong(3) → No Trend(0) 전환 시 True 반환.
        """
        pos = self.positions.get(code)
        if pos is None:
            return False
        return int(pos.trend_prev_level) == 3 and int(pos.trend_level) == 0

    def is_pending(self, code: str) -> bool:
        if code not in self._pending:
            return False
        # 매도 주문이 30초 이상 미체결이면 pending 해제 — 손절 재시도 허용
        sell_time = self._pending_sell_time.get(code)
        if sell_time and (datetime.now() - sell_time).total_seconds() > 30:
            retries = self._pending_sell_retries.get(code, 0) + 1
            self._pending_sell_retries[code] = retries
            logger.warning(
                "[미체결 해제] %s 매도 주문 30초 초과 (재시도 %d회) — pending+force_sell 해제",
                code, retries,
            )
            self._pending.discard(code)
            self._pending_sell_time.pop(code, None)
            # force_exit 데드락 방지: _force_sell_issued도 함께 해제해야 재발령 가능
            self._force_sell_issued.discard(code)
            return False
        return True

    def _can_pyramid(self, code: str) -> bool:
        """
        피라미딩(추가 진입) 가능 여부 확인.
        조건: 수익률 > 1.5% AND 진입 횟수 < 2
        """
        pos = self.positions.get(code)
        if not pos:
            return False
        
        # 설정 로드 (기본값: 수익 1.5% 이상, 최대 2회 진입)
        _RISK = cfg.RISK
        min_profit = float(_RISK.get("pyramid_min_profit_pct", 1.5))
        max_entries = int(_RISK.get("pyramid_max_entries", 2))
        
        profit_ok = pos.pnl_pct >= min_profit
        count_ok = getattr(pos, "entry_count", 1) < max_entries
        
        if profit_ok and count_ok:
            return True
        
        if not profit_ok and count_ok:
            logger.debug("[피라미딩거절] %s 수익률(%.2f%%) < 기준(%.2f%%)", code, pos.pnl_pct, min_profit)
        elif not count_ok:
            logger.debug("[피라미딩거절] %s 진입횟수(%d) >= 최대(%d)", code, getattr(pos, "entry_count", 1), max_entries)
            
        return False

    def mark_stop_loss(self, code: str) -> None:
        """
        해당 종목을 당일 손절 블랙리스트에 등록한다.
        손절·Hard Stop·캔들손절 시 호출. 익절·수동·Day Close는 호출하지 않음.
        """
        self._stop_loss_today.add(code)
        logger.info("[손절 블랙리스트] %s 등록 — 당일 재매수 차단", code)

    def _roll_daily_state_if_needed(self) -> None:
        """날짜가 바뀌면 당일 실현손익·체결 로그·오늘 앱 매수 수량을 초기화한다."""
        today = date.today()
        if self._pnl_date != today:
            self._pnl_date = today
            self.daily_realized_pnl = 0
            self._broker_realized_base = 0
            self._today_fill_log.clear()
            self._fills_initialized = False  # 새 날짜 → 세션 초기화 재허용
            self._stop_loss_today.clear()    # 날짜 변경 시 손절 블랙리스트 초기화
            for p in self.positions.values():
                p.qty_buy_today_app = 0

    # ── 당일 실현손익 — 체결 이력 파일 (append-only, 재시작 복구 지원) ─────────
    # 절대 경로: order_manager.py 위치 기준 → 프로젝트루트/logs/fills_YYYYMMDD.jsonl
    _FILLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"

    def _fills_path(self) -> pathlib.Path:
        return self._FILLS_DIR / f"fills_{date.today().strftime('%Y%m%d')}.jsonl"

    def _append_fill_to_file(self, realized: int, code: str, name: str,
                              sell_price: int, avg_price: int, qty: int,
                              is_partial: bool = False) -> None:
        """매도 체결 1건을 오늘 이력 파일에 추가한다 (append-only)."""
        try:
            self._FILLS_DIR.mkdir(parents=True, exist_ok=True)
            entry = json.dumps({
                "ts":         datetime.now().isoformat(timespec="seconds"),
                "code":       code,
                "name":       name,
                "sell_price": sell_price,
                "avg_price":  avg_price,
                "qty":        qty,
                "realized":   realized,
                "is_partial": is_partial,
            }, ensure_ascii=False)
            with self._fills_path().open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning("체결 이력 파일 저장 실패 — 재시작 후 손익 복구 불가: %s", e)

    def _load_realized_from_fills_file(self) -> int:
        """오늘 이력 파일의 모든 realized 합계를 반환한다."""
        try:
            fpath = self._fills_path()
            if not fpath.exists():
                return 0
            total = 0
            for line in fpath.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    total += int(json.loads(line).get("realized", 0))
            return total
        except Exception as e:
            logger.debug("체결 이력 파일 로드 실패(무시): %s", e)
            return 0

    def _recompute_daily_realized_from_ledger(self) -> None:
        """기준값(_broker_realized_base) + 이번 세션 매도 체결 합."""
        extra = sum(
            int(x.get("realized", 0)) for x in self._today_fill_log if x.get("side") == "sell"
        )
        self.daily_realized_pnl = self._broker_realized_base + extra
        # [Phase 1] AppState 동기화
        if self.state:
            self.state.daily_realized_pnl = float(self.daily_realized_pnl)

    def _sync_daily_realized_from_broker(self) -> None:
        """당일 실현손익 기준값을 세션 시작 시 1회만 초기화한다.

        우선순위: opt10074(실전) → 로컬 체결 이력 파일(모의투자/재시작)
        _fills_initialized 플래그로 이중합산 방지.
        """
        if self._fills_initialized:
            return   # 세션 내 중복 호출 무시

        # ① opt10074 시도 (실전 투자)
        v: Optional[int] = None
        try:
            v = self._kiwoom.get_today_realized_pnl()
        except Exception as e:
            logger.warning("당일 실현손익 TR 조회 예외: %s", e)

        if v is not None and v != 0:
            # opt10074 반환값이 유효한 경우 사용 (양수=이익, 음수=손실 모두 허용)
            self._broker_realized_base = int(v)
            self._today_fill_log.clear()
            self._recompute_daily_realized_from_ledger()
            logger.info("당일 실현손익 opt10074 — %s원", f"{self._broker_realized_base:,}")
        else:
            # v가 None(TR 실패) 또는 0(모의투자 미지원/오늘 거래 없음) → 파일에서 복구
            file_sum = self._load_realized_from_fills_file()
            self._broker_realized_base = file_sum
            self._today_fill_log.clear()
            self._recompute_daily_realized_from_ledger()
            if file_sum != 0:
                logger.info("당일 실현손익 파일 복구 — %s원", f"{file_sum:,}")
            else:
                logger.info("당일 실현손익 0원 — 오늘 매도 체결 없음 (또는 모의투자 미지원)")

        self._fills_initialized = True

    @property
    def today_fill_log(self) -> tuple[dict, ...]:
        """당일 매수/매도 체결 기록(읽기 전용)."""
        return tuple(self._today_fill_log)

    def _roll_daily_realized_pnl_if_needed(self) -> None:
        self._roll_daily_state_if_needed()

    def _is_buy_allowed(self, code: str, name: str) -> bool:
        """매수 직전 강제 차단 (유니버스 필터 + 전역 리스크 필터)."""
        # 0) 전역 리스크 및 지수 급락 체크 (AppState 기반)
        if self.state:
            if self.state.risk_locked:
                msg = f"매수 차단 — 리스크 잠금 상태 (손절 한도 초과 등) ({name})"
                logger.warning(msg)
                self.order_failed.emit(msg)
                return False
            if self.state.is_crash:
                msg = f"매수 차단 — 지수 급락 감지 상태 ({name})"
                logger.warning(msg)
                self.order_failed.emit(msg)
                return False

        from scanner.universe import is_pure_equity_name
        
        # 1) 이름 키워드 차단
        if not is_pure_equity_name(name):
            msg = f"매수 차단 — ETF/ETN/파생 종목 ({name} {code})"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return False

        # 2) 종목 상태 차단
        try:
            state = self._kiwoom._ocx.dynamicCall("GetMasterStockState(QString)", [code]).strip()
        except Exception:
            state = ""
        blocked_words = ("관리", "정지", "투자경고", "투자위험", "투자주의")
        if any(w in state for w in blocked_words):
            msg = f"매수 차단 — 위험 상태 종목 ({name} {code}, 상태={state or '없음'})"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return False

        return True

    # -----------------------------------------------------------------------
    # 매수 / 매도
    # -----------------------------------------------------------------------

    def buy(
        self,
        code:  str,
        name:  str,
        qty:   int,
        price: int = 0,
        order_type: str = "",
    ) -> str:
        """시장가(price=0) 또는 지정가 매수 주문을 전송한다."""
        return self._send(OrderType.BUY, code, name, qty, price, order_type)

    @staticmethod
    def _price_tick(price: int) -> int:
        """KRX 호가 단위 반환 (현재가 기준)."""
        if price < 1_000:      return 1
        if price < 5_000:      return 5
        if price < 10_000:     return 10
        if price < 50_000:     return 50
        if price < 100_000:    return 100
        if price < 500_000:    return 500
        return 1_000

    def force_exit(
        self,
        code: str,
        name: str,
        qty: int,
        reason: str = "Hard Stop",
    ) -> str:
        """
        긴급 탈출(강제 매도) — 손절/청산 시 최우선 순위 (2026-04-04).
        _pending 잠금 상태라도 무시하고 시장가 매도 시도.

        Args:
            code: 종목코드
            name: 종목명
            qty: 수량
            reason: 탈출 사유 (로그용) — "Hard Stop", "Force Close", "Day Close" 등

        Returns:
            주문번호 (실패 시 "0")
        """
        # 이미 강제 매도 발령 중 → 체결 대기 (중복 발령 차단)
        if code in self._force_sell_issued:
            logger.debug("[force_exit] %s 이미 발령 중 — 중복 스킵", code)
            return "0"

        # 포지션 실제 수량 재확인 — [800033] 이후 유령 포지션 방지
        pos = self.positions.get(code)
        if pos is None or pos.qty <= 0:
            logger.info("[force_exit] %s 포지션 없음 — 스킵 (이미 청산됨)", code)
            self._force_sell_issued.discard(code)
            return "0"

        try:
            # 2회 이상 타임아웃 → 지정가(현재가-1틱)로 에스컬레이션 (시장가 미체결 반복 방지)
            retries = self._pending_sell_retries.get(code, 0)
            sell_price = 0  # 기본: 시장가
            if retries >= 2:
                cur = pos.current_price or pos.avg_price
                tick = self._price_tick(cur)
                sell_price = max(1, cur - tick)
                logger.warning(
                    "[force_exit] %s(%s) 지정가 에스컬레이션 — %d원 (%d회 타임아웃)",
                    name, code, sell_price, retries,
                )

            # _pending 체크 없이 바로 주문 시도
            order_id = self._send(OrderType.SELL, code, name, qty, price=sell_price)
            if order_id and order_id != "0":
                logger.warning(
                    "[force_exit] %s(%s) %d주 %s 매도 주문 — 사유: %s (주문번호: %s)",
                    name, code, qty,
                    f"지정가({sell_price}원)" if sell_price else "시장가",
                    reason, order_id,
                )
                # pending에 수동으로 추가 (체결 콜백 대기)
                self._pending.add(code)
                self._force_sell_issued.add(code)
                return order_id
            else:
                logger.error("[force_exit] %s(%s) 주문 실패 — 사유: %s", name, code, reason)
                return "0"
        except Exception as e:
            logger.exception(f"[force_exit] {name}({code}) 예외 발생: {e}")
            return "0"

    def _check_failed_sells(self) -> None:
        """미체결 주문 10초마다 재시도 (손절 대기 중 미체결 대비, 2026-04-04)."""
        from datetime import datetime
        now = datetime.now()

        for code, info in list(self._failed_sells.items()):
            # 포지션이 이미 청산됐으면 재시도 불필요 — 메모리에서 제거
            pos = self.positions.get(code)
            if pos is None or pos.qty <= 0:
                logger.info("[손절 재주문 취소] %s 포지션 없음 — 재시도 목록에서 제거", code)
                del self._failed_sells[code]
                continue

            elapsed = (now - info["last_time"]).total_seconds()

            # 10초 경과 → 재주문
            if elapsed >= 10:
                attempt = info.get("attempts", 0) + 1
                if attempt > 3:
                    # 3회 재시도 이상 실패 → 사용자 알림
                    logger.critical(
                        "[손절 실패 알림] %s 3회 연속 재주문 실패 — 수동 개입 필요",
                        code
                    )
                    del self._failed_sells[code]
                    continue

                sell_qty = pos.qty  # 현재 실제 보유 수량으로 재확인
                logger.info(
                    "[손절 재주문] %s %d주 — %d차 시도",
                    code, sell_qty, attempt
                )
                # 종목명이 없으므로 코드로만 매도 시도
                ret = self._send(OrderType.SELL, code, code, sell_qty, price=0)
                if ret and ret != "0":
                    del self._failed_sells[code]  # 성공 → 제거
                else:
                    info["attempts"] = attempt
                    info["last_time"] = now

    def _check_pending_buys(self) -> None:
        """[P2] 매수 주문 접수 후 10초 경과 미체결 시 취소 후 pending 해제.

        키움 시장가 주문은 보통 즉시 체결되지만, 서킷브레이커·매매정지·OCX 지연
        등으로 pending이 장기 잠금될 수 있다. 10초 초과 시:
          1. SendOrder type=3(취소)로 취소 시도
          2. pending 상태 강제 해제 → 다음 신호 사이클에서 재진입 허용
        """
        from datetime import datetime
        now = datetime.now()

        for code, t in list(self._pending_buy_time.items()):
            elapsed = (now - t).total_seconds()
            if elapsed < 10:
                continue

            info = self._pending_buy_info.get(code, {})
            qty  = info.get("qty", 0)
            name = info.get("name", code)
            order_no = info.get("order_no", "")

            logger.warning(
                "[매수미체결] %s(%s) %d주 — %.0f초 경과, 취소 시도",
                name, code, qty, elapsed
            )

            # 취소 주문 전송 (SendOrder type 3 = 취소)
            if order_no:
                try:
                    rq_cancel = f"취소_{code}"
                    cancel_ret = self._kiwoom._ocx.dynamicCall(
                        "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
                        [rq_cancel, "1001", self._account, 3, code, qty, 0, "00", order_no],
                    )
                    logger.info(
                        "[매수취소] %s(%s) order_no=%s → ret=%s",
                        name, code, order_no, cancel_ret
                    )
                    # 취소 후 뒤늦게 체결이 오면 즉시 청산하도록 order_no 기록
                    self._cancelled_buy_orders.add(order_no)
                except Exception as _e:
                    logger.warning("[매수취소 오류] %s: %s", code, _e)

            # pending 상태 강제 해제
            self._pending.discard(code)
            self._pending_buy_time.pop(code, None)
            self._pending_buy_info.pop(code, None)
            self._pending_meta.pop(code, None)        # 메타데이터 정리 (Phase G-1)
            self._app_pending_buys.pop(code, None)

    def sell(
        self,
        code:  str,
        name:  str,
        qty:   int,
        price: int = 0,
        order_type: str = "",
    ) -> str:
        """시장가 또는 지정가 매도 주문을 전송한다."""
        return self._send(OrderType.SELL, code, name, qty, price, order_type)

    def partial_exit(
        self,
        code:       str,
        name:       str,
        sell_ratio: float = 0.30,
        reason:     str   = "분할익절",
    ) -> int:
        """
        보유 수량의 sell_ratio(기본 30%)를 시장가 1차 분할 매도한다.

        반환: 실제 주문 수량 (0 = 실패 또는 이미 처리됨)

        주의:
        - 이미 partial_sold=True 이면 재호출 무시 (중복 방지)
        - 매도 체결 후 pos.qty는 체결 콜백에서 자동 감소 → trail은 남은 수량에 적용
        """
        from scanner.scanner_logger import ScannerLogger
        pos = self.positions.get(code)
        if pos is None:
            logger.debug("[분할익절] %s 포지션 없음 — 스킵", code)
            return 0
        if pos.partial_sold:
            logger.debug("[분할익절] %s 이미 완료 — 스킵", code)
            return 0
        if pos.qty <= 0:
            return 0

        partial_qty = max(1, int(pos.qty * sell_ratio))
        logger.info(
            "[분할익절] %s(%s) %d주 매도 (보유 %d주의 %.0f%%) — %s",
            name, code, partial_qty, pos.qty, sell_ratio * 100, reason,
        )

        ret = self.sell(code, name, partial_qty)
        if ret:   # 주문 접수 성공 (주문번호 문자열 반환)
            pos.partial_sold     = True
            pos.qty_partial_sold = partial_qty
            self._partial_pending_codes.add(code)
            ScannerLogger.passed(
                code, name, "PARTIAL_EXIT",
                f"{partial_qty}주 매도 ({sell_ratio*100:.0f}%) — {reason}",
            )
            return partial_qty

        logger.warning("[분할익절] %s(%s) 주문 실패", name, code)
        return 0

    def _send(
        self,
        order_type: int,
        code: str,
        name: str,
        qty:  int,
        price: int,
        price_type: str = "",
    ) -> str:
        # OrderExecutor에 위임
        ret, rq_name = self._executor.send(order_type, code, name, qty, price, price_type)

        side = "매수" if order_type == OrderType.BUY else "매도"
        if ret != 0:
            msg = f"{name} {side} 주문 실패 (ret={ret})"
            logger.error(msg)
            self.order_failed.emit(msg)
            # [NEW] 매도 주문 실패 시 미체결 추적 (2026-04-04)
            # 포지션이 없거나 수량이 0이면 추적 불필요 (이미 청산됨)
            if order_type == OrderType.SELL:
                _pos = self.positions.get(code)
                if _pos and _pos.qty > 0:
                    self._failed_sells[code] = {
                        "qty": _pos.qty,  # 실제 보유 수량 기준 (overcounting 방지)
                        "attempts": 0,
                        "last_time": datetime.now(),
                    }
                    logger.warning(
                        "[미체결 추적] %s(%s) %d주 매도 — 10초 후 재주문 시도",
                        name, code, _pos.qty
                    )
                else:
                    logger.info("[미체결 추적 스킵] %s 포지션 없음 — 재시도 불필요", code)
            return ""

        self._pending.add(code)
        if order_type == OrderType.BUY:
            self._app_pending_buys[code] = qty
            # [P2] 매수 접수 시각·정보 기록 — 10초 미체결 감지용
            self._pending_buy_time[code] = datetime.now()
            self._pending_buy_info[code] = {"qty": qty, "name": name, "order_no": rq_name}
            if self._audit:
                self._audit.log_buy_order(code, qty, price)
        else:
            self._pending_sell_time[code] = datetime.now()
            if self._audit:
                self._audit.log_sell_order(code, qty, price)

        rec = OrderRecord(
            order_no=rq_name, code=code, name=name,
            order_type=order_type, qty=qty, price=price,
        )
        self.orders[rq_name] = rec

        payload = {
            "time":  datetime.now().strftime("%H:%M:%S"),
            "side":  side,
            "code":  code,
            "name":  name,
            "qty":   qty,
            "price": price if price else "시장가",
        }
        logger.info("%s %s %s %d주 %s", side, name, code, qty,
                    f"{price:,}원" if price else "시장가")
        self.order_sent.emit(payload)
        return rq_name

    # -----------------------------------------------------------------------
    # 체결 콜백
    # -----------------------------------------------------------------------

    def _connect_chejan(self) -> None:
        self._kiwoom._ocx.OnReceiveChejanData.connect(self._on_chejan_data)

    def _on_chejan_data(self, gubun: str, item_cnt: int, fid_list: str) -> None:
        """
        체결/잔고 이벤트.
        gubun "0" → 주문 접수/체결
        gubun "1" → 잔고 변동
        """
        def cj(fid: int) -> str:
            return self._kiwoom._ocx.dynamicCall(
                "GetChejanData(int)", [fid]
            ).strip()

        if gubun != CHEJAN_FILL:
            return
        self._roll_daily_realized_pnl_if_needed()

        code        = cj(9001).lstrip("A")
        name        = cj(302)
        _raw_qty    = cj(911)
        _raw_price  = cj(910)
        # FID 911 = 주문 내 누적 체결량 (이번 이벤트 단독 수량이 아님)
        _cum_qty    = abs(int(_raw_qty   or 0))
        filled_price= abs(int(_raw_price or 0))
        order_no    = cj(9203)
        # 증분 체결량 = 누적 - 이전 누적
        _prev_cum   = self._order_fill_cumulative.get(order_no, 0)
        filled_qty  = max(0, _cum_qty - _prev_cum)
        self._order_fill_cumulative[order_no] = _cum_qty
        logger.info("체결원시 — %s FID910(체결가)=%r FID911(누적체결량)=%r 이전=%d → 증분=%d price=%d",
                    name, _raw_price, _raw_qty, _prev_cum, filled_qty, filled_price)
        # FID 905 주문구분: "+매수"/"+매도" 문자열로 반환됨 (int 변환 불가)
        _ot_str = cj(905)
        if "매수" in _ot_str:
            order_type = OrderType.BUY
        elif "매도" in _ot_str:
            order_type = OrderType.SELL
        else:
            order_type = 0

        if filled_qty == 0:
            return

        # 주문 레코드 갱신
        rec = self.orders.get(order_no)
        if rec:
            rec.status       = "체결"
            rec.filled_qty   = filled_qty
            rec.filled_price = filled_price
            rec.filled_at    = datetime.now()

        is_app_buy = order_type == OrderType.BUY and code in self._app_pending_buys

        # [취소 후 뒤늦은 체결 방어] 이미 취소한 매수 주문이 뒤늦게 체결된 경우 즉시 청산
        if order_type == OrderType.BUY and order_no in self._cancelled_buy_orders:
            self._cancelled_buy_orders.discard(order_no)
            logger.warning(
                "[취소후체결] %s(%s) 취소 주문 %s 이 뒤늦게 체결됨 (%d주 @%d) — 즉시 시장가 청산",
                name, code, order_no, filled_qty, filled_price,
            )
            # 포지션에 반영한 뒤 즉시 청산
            if code in self.positions:
                _pos = self.positions[code]
                _pos.qty += filled_qty
            else:
                self.positions[code] = Position(
                    code=code, name=name,
                    qty=filled_qty, avg_price=filled_price,
                    current_price=filled_price,
                )
            self.sell(code, name, self.positions[code].qty)
            return

        avg_buy_for_log: Optional[int] = None  # 매도 체결 로그용 (포지션 갱신 전 평단)

        # 포지션 반영
        if order_type == OrderType.BUY:
            # [P2] 매수 체결 → 미체결 추적 해제
            self._pending_buy_time.pop(code, None)
            self._pending_buy_info.pop(code, None)
            # Phase G-1: PendingOrderMeta에서 추출
            meta = self._pending_meta.pop(code, PendingOrderMeta())
            trend_level = meta.trend_level
            trend_prev_level = meta.trend_prev_level
            _near_high = meta.near_daily_high
            _ctp = meta.custom_tp_pct
            _eod_trade = meta.eod_trade
            _entry_phase = meta.entry_phase
            _sector_fill = meta.sector
            candle_stop = meta.candle_stop
            if is_app_buy:
                rem = self._app_pending_buys[code] - filled_qty
                if rem <= 0:
                    del self._app_pending_buys[code]
                    # 주문 완전 체결 → 누적 체결량 추적 정리
                    self._order_fill_cumulative.pop(order_no, None)
                else:
                    self._app_pending_buys[code] = rem

            if code in self.positions:
                pos = self.positions[code]
                
                # ✅ 신규 주문(피라미딩)의 첫 체결인 경우 진입 횟수 증가
                if _prev_cum == 0:
                    pos.entry_count = getattr(pos, "entry_count", 1) + 1
                    logger.info("🚀 [피라미딩체결] %s(%s) 진입 횟수 증가 -> %d회", 
                                name, code, pos.entry_count)

                total_qty   = pos.qty + filled_qty
                pos.avg_price = (pos.avg_price * pos.qty + filled_price * filled_qty) // total_qty
                pos.qty      = total_qty
                # 신규 체결 시 진입 시점 추세를 덮어써 최신 상태로 유지
                pos.trend_level = int(trend_level)
                pos.trend_prev_level = int(trend_prev_level)
                if is_app_buy:
                    pos.qty_buy_today_app += filled_qty
                    pos.opened_by_app = True
                    if pos.buy_date is None:
                        pos.buy_date = date.today()
            else:
                self.positions[code] = Position(
                    code=code, name=name,
                    qty=filled_qty, avg_price=filled_price,
                    current_price=filled_price,
                    buy_date=date.today() if is_app_buy else None,
                    entry_time=datetime.now() if is_app_buy else None,
                    opened_by_app=is_app_buy,
                    qty_buy_today_app=filled_qty if is_app_buy else 0,
                    candle_stop_price=candle_stop,
                    trend_level=int(trend_level),
                    trend_prev_level=int(trend_prev_level),
                    near_daily_high=_near_high,
                    custom_tp_pct=_ctp,
                    eod_trade=_eod_trade,
                    entry_phase=_entry_phase,
                    sector=_sector_fill,
                )
                position_log.info(
                    "[포지션생성] %s(%s) 체결가=%d 수량=%d 섹터=[%s] trend_lv=%d phase=%d",
                    name, code, filled_price, filled_qty,
                    _sector_fill or "-", int(trend_level), int(_entry_phase),
                )
                if _near_high:
                    logger.info("[신고가근처] %s(%s) — 일봉 신고가 근처 진입, TP 상향 적용 (custom_tp=%.1f%%)",
                                name, code, _ctp)
                if _eod_trade:
                    logger.info("[EOD] %s(%s) — 종가매매 진입, 당일 강제청산 제외 / 익일 갭 체크 관리",
                                name, code)
            self.cash -= filled_qty * filled_price
            self._today_fill_log.append({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "side": "buy",
                "code": code,
                "name": name,
                "qty": filled_qty,
                "price": filled_price,
                "amount": filled_qty * filled_price,
            })
            if self._audit:
                self._audit.log_buy_fill(code, filled_qty, filled_price)
            # [NEW] 포지션 실시간 등록
            if self.on_position_opened:
                self.on_position_opened(code)
            realized_for_signal = 0

        elif order_type == OrderType.SELL:
            if code in self.positions:
                pos = self.positions[code]
                avg_buy_for_log = pos.avg_price
                # 실제 순익 = 가격차이 - 매도수수료 - 증권세 - 매수수수료(평단 기준)
                sell_amount = filled_price * filled_qty
                buy_amount  = pos.avg_price * filled_qty
                cost = round(sell_amount * (_FEE + _TAX) + buy_amount * _FEE)
                realized = (filled_price - pos.avg_price) * filled_qty - cost
                self._today_fill_log.append({
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "side": "sell",
                    "code": code,
                    "name": name,
                    "qty": filled_qty,
                    "price": filled_price,
                    "amount": filled_qty * filled_price,
                    "realized": realized,
                })
                realized_for_signal = realized
                # [NEW] 매도 체결을 파일에 append → 재시작 후에도 복구 가능
                _is_partial = code in self._partial_pending_codes
                self._partial_pending_codes.discard(code)
                self._append_fill_to_file(
                    realized=realized, code=code, name=name,
                    sell_price=filled_price, avg_price=pos.avg_price, qty=filled_qty,
                    is_partial=_is_partial,
                )
                if self._audit:
                    self._audit.log_sell_fill(
                        code, filled_qty, filled_price,
                        avg_buy_price=pos.avg_price,
                        realized_pnl=realized,
                    )
                self._recompute_daily_realized_from_ledger()
                sell_from_today = min(filled_qty, pos.qty_buy_today_app)
                pos.qty_buy_today_app -= sell_from_today
                pos.qty -= filled_qty
                if pos.qty <= 0:
                    _pnl_pct = (filled_price - avg_buy_for_log) / avg_buy_for_log * 100 if avg_buy_for_log else 0
                    position_log.info(
                        "[포지션청산] %s(%s) 매도가=%d 평균매입=%d 손익=%+.2f%% 실현손익=%+d",
                        name, code, filled_price, avg_buy_for_log, _pnl_pct, realized,
                    )
                    # [NEW] 포지션 실시간 해제
                    if self.on_position_closed:
                        self.on_position_closed(code)
                    del self.positions[code]
                    # 강제 매도 발령 해제 (체결 완료) + 재시도 카운터 초기화
                    self._force_sell_issued.discard(code)
                    self._pending_sell_retries.pop(code, None)
                    # 매도 체결 완료 → 대기 중인 매수 신호 실행
                    if self._queued_signal is not None:
                        queued = self._queued_signal
                        self._queued_signal = None
                        logger.info("[큐 신호 실행] 매도 완료 후 대기 신호 처리 — %s(%s)",
                                    queued.name, queued.code)
                        self.handle_signal(queued)
            self.cash += filled_qty * filled_price

        self._pending.discard(code)
        self._pending_sell_time.pop(code, None)

        payload = {
            "time":         datetime.now().strftime("%H:%M:%S"),
            "side":         "매수체결" if order_type == OrderType.BUY else "매도체결",
            "code":         code,
            "name":         name,
            "filled_qty":   filled_qty,
            "filled_price": filled_price,
            "realized_pnl": realized_for_signal,
        }
        if avg_buy_for_log is not None:
            payload["avg_buy_price"] = avg_buy_for_log

        # [NEW] AppState 직접 업데이트 (체결 즉시 UI 반영)
        if self.state:
            self.state.update_portfolio(self.cash, dict(self.positions))

        self.order_filled.emit(payload)

        if order_type == OrderType.SELL and avg_buy_for_log is not None:
            logger.info(
                "체결 — %s %s %d주 매수가 %s원 → 매도가 %s원",
                name,
                payload["side"],
                filled_qty,
                f"{avg_buy_for_log:,}",
                f"{filled_price:,}",
            )
        else:
            logger.info(
                "체결 — %s %s %d주 @%s원",
                name,
                payload["side"],
                filled_qty,
                f"{filled_price:,}",
            )

    def cleanup_stale_data(self, active_codes: set[str]) -> int:
        """오래된 내부 상태(신호 시각, 주문 기록 등)를 정리하여 메모리 누수를 방지한다."""
        import time as _time
        now_mono = _time.monotonic()
        cleaned = 0

        # 1. 당일 신호 쿨다운 — 보유 중이 아닌 코드 중 마지막 신호 2시간 초과 항목 제거
        stale_sig = [
            c for c, t in list(self._signal_last_time.items())
            if c not in active_codes and (now_mono - t) > 7200
        ]
        for c in stale_sig:
            self._signal_last_time.pop(c, None)
            cleaned += 1

        # 2. 과거 주문 레코드 — 1000건 초과 시 오래된 것부터 삭제 (당일 체결만 보존)
        if len(self.orders) > 1000:
            sorted_keys = sorted(
                self.orders.keys(),
                key=lambda k: self.orders[k].ordered_at
            )
            to_del = sorted_keys[: len(self.orders) - 500]
            for k in to_del:
                del self.orders[k]
            cleaned += len(to_del)

        return cleaned
