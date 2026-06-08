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
    entry_gap_pct: float = 0.0             # [2026-05-26] 진입 시 갭 상승 % (동적 손절선 산출용)
    signal_price: int = 0                   # [2026-06-01] 신호 발생 시점의 가격 (슬리피지 체크용)


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
    entry_gap_pct:   float = 0.0          # [2026-05-26] 진입 시 갭 상승 % (동적 손절선 산출용)
    # 종가매매(EOD) 플래그
    eod_trade:       bool = False         # True → 당일 15:19 강제청산 제외, 익일 관리
    overnight_held:  bool = False         # True → 익일 장 시작 후 갭 체크 관리 중
    # 매매 단계 태그
    entry_phase:     int  = 0             # 0=미분류, 1=모닝스캘핑(09~10:30), 2=메인전략(10~14:40)
    # 섹터 쏠림 방지
    sector:          str  = ""            # 업종명 (opt10001 응답, 섹터 노출 집계용)
    entry_count:     int  = 1             # 진입 횟수 (피라미딩 추적용)
    sl_triggered_at: Optional[datetime] = None # 손절(SL) 발동 시각

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
        notification_mgr: Optional["NotificationManager"] = None,
    ) -> None:
        super().__init__(parent)
        self._kiwoom          = kiwoom
        self._account         = account
        self.max_order_amount = max_order_amount
        self.max_positions    = max_positions
        self.notif_mgr        = notification_mgr

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
        # 틱 손절(hard stop / 확정손절) 발동 시 strategy.mark_loss_exit() 호출 경로
        self.on_tick_loss_exit: Optional[Callable] = None  # Callable[[Position], None]
        
        self._snap_store = None  # [NEW] 호가 잔량 확인용
        # 외부 주입 객체 — set_state / set_health_monitor / core.py에서 주입
        self.state   = None   # AppState
        self._health = None   # HealthMonitor
        self._audit  = None   # AuditLogger

    def set_snapshot_store(self, store) -> None:
        """호가 잔량 확인을 위해 SnapshotStore 인스턴스를 주입받는다."""
        self._snap_store = store
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

    def set_config(self, cfg):
        """Config 주입 (TradingController에서 호출) — 2026-05-12"""
        self._scan_cfg = cfg
        logger.info("[OrderManager] Config 주입 완료")

    def set_health_monitor(self, monitor):
        """HealthMonitor 주입 (MainWindow에서 호출)"""
        self._health = monitor
        logger.info("[OrderManager] HealthMonitor 주입 완료")

    # -----------------------------------------------------------------------
    # 속성
    # -----------------------------------------------------------------------

    @property
    def available_cash(self) -> int:
        """
        가용 예수금 = self.cash - 미체결 매수 주문 추정 금액.

        [BUG FIX 2026-05-26] 이전엔 self.cash만 반환했음. 그러나 self.cash는 매수 체결
        콜백에서 비로소 차감되므로 주문 접수~체결 사이(수 초~수십 초)에 동일 예수금이
        후속 진입 사이징에 중복 사용되는 버그가 있었음.
        (예: 09:23:00 SFA반도체 사이징=2,137,868원, 09:23:02 케이씨에스 사이징=2,137,868원)
        → 미체결 매수의 예상 금액(잔여수량 × 주문가)을 차감해 진짜 가용액 반환.

        주의: 실전투자 서버는 opw00001 응답에서 '주문가능금액'을 사용하는데, 이 값은
        이미 서버에서 미체결 매수를 차감한 값임. 30초마다 sync_balance가 호출되어 self.cash가
        갱신되므로, 이중 차감 회피를 위해 sync 직후의 미체결만 차감해야 하나, sync 후
        새 매수가 발생할 수 있어 가장 안전한 방식은 다음과 같다:
          - sync_balance에서 마지막으로 캐치된 시각 이후에 접수된 매수만 차감
          - 단순화를 위해 현재는 self.cash 기반으로 미체결을 모두 차감 (보수적)
            → sync 직후 잠시 과소 추정 가능하나, 매수 차단(안전) 방향이라 무해
        """
        pending_amt = 0
        for code, rem_qty in self._app_pending_buys.items():
            info = self._pending_buy_info.get(code, {})
            p = int(info.get("price", 0) or 0)
            if p > 0 and rem_qty > 0:
                # 수수료 약 0.015% 포함해 보수적으로 추정
                pending_amt += int(rem_qty * p * 1.0002)
        return max(0, self.cash - pending_amt)

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
    # 포지션 재구성 헬퍼 (Step 1: 코드 중복 제거, 2026-05-29)
    # -----------------------------------------------------------------------

    def _rebuild_positions_from_holdings(self, holdings: list[dict]) -> dict[str, "Position"]:
        """
        holdings 리스트로부터 Position dict 재구성 (공통 로직 추출).

        sync_balance()와 _sync_with_balance()에서 동일하게 반복되던 Position 생성 로직을
        이 메서드로 통합. 22개 필드 복사 부분 중복 제거.

        Args:
            holdings: key={code, name, qty, avg_price, current_price, ...}인 dict 리스트

        Returns:
            dict[str, Position]: code → Position 매핑
        """
        new_positions: dict[str, Position] = {}
        for h in holdings:
            qty = h.get("qty", 0)
            if qty <= 0:
                # 보유수량 0 or 키 없음 — 불완전 응답 방어
                logger.warning("_rebuild_positions_from_holdings: 보유수량 0 또는 누락 — %s(%s) 스킵",
                               h.get("name", "?"), h.get("code", "?"))
                continue
            code = h["code"]
            avg = h.get("avg_price", 0)
            if avg == 0 and code in self.positions:
                avg = self.positions[code].avg_price
            old = self.positions.get(code)
            qty_today = min(old.qty_buy_today_app, qty) if old else 0

            # 22개 필드 복사 (기존 값 보존)
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
                near_daily_high   = old.near_daily_high   if old else False,
                custom_tp_pct     = old.custom_tp_pct     if old else 0.0,
                eod_trade         = old.eod_trade         if old else False,
                overnight_held    = old.overnight_held    if old else False,
                entry_gap_pct     = old.entry_gap_pct     if old else 0.0,
            )

        return new_positions

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
            server_cash = balance.get("cash", 0) if balance else 0
            balance_available = bool(balance)  # opw00001 응답 여부

            # opw00001 미응답 시에도 opw00018은 진행 (포지션 갱신)
            holdings = self._kiwoom.get_holdings()

            # 홀딩도 응답 없으면 스킵
            if not holdings and not balance:
                logger.debug("sync_balance: opw00001/opw00018 모두 응답 없음 — 동기화 스킵")
                return self.cash

            # opw00018만 응답 있고 opw00001 미응답 시 (현재 상황)
            if not balance_available and holdings:
                logger.warning("sync_balance: opw00001 미응답 (예수금 불명) — opw00018만으로 포지션 갱신")
                # 기존 예수금 유지하고 포지션만 갱신
                server_cash = self.cash  # 기존값 유지
                balance_available = True  # 이후 로직 실행

            # 홀딩 조회도 차단된 경우 — 포지션을 빈 목록으로 덮어쓰지 않음
            if not holdings and self.positions:
                logger.debug("sync_balance: opw00018 응답 없음 — 포지션 동기화 스킵")
                # 예수금만 최신화
                if balance_available:
                    invested = sum(p.avg_price * p.qty for p in self.positions.values())
                    self.cash = max(0, server_cash - invested)
                return self.cash

            # opw00018 모의투자 서버는 매입가 필드를 반환하지 않음 → 기존 메모리값 보존
            self.positions = self._rebuild_positions_from_holdings(holdings)

            # 모의투자 서버는 opw00001 "예수금"을 투자금 차감 없이 반환한다.
            # 실전투자 서버는 이미 차감된 "예수금" 또는 "D+2추정예수금"을 반환하므로 차감하지 않는다.
            invested = sum(p.avg_price * p.qty for p in self.positions.values())
            if balance_available and self._kiwoom.is_mock:
                self.cash = max(0, server_cash - invested)
            elif balance_available:
                self.cash = server_cash
            # else: balance_available=False이면 self.cash는 기존값 유지됨

            status = "정상" if balance_available else "opw00001_미응답"
            logger.info("잔고 동기화 완료(%s) — 예수금 %s원 (서버=%s / 투자=%s) / 보유 %d종목",
                        status, f"{self.cash:,}", f"{server_cash:,}", f"{invested:,}",
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
            # [FIX 2026-06-02] balance 체크를 server_cash 할당 전에 먼저 수행
            # TIMEOUT으로 balance={} 반환 시 server_cash=0으로 덮어쓰기 방지
            if not balance:
                return self.cash
            server_cash = balance.get("cash", 0)
            if server_cash <= 0:
                # 잔고 조회 실패 또는 0원 — 기존값 유지
                logger.warning("_sync_with_balance: server_cash=%d — 기존 예수금 유지", server_cash)
                return self.cash

            holdings = self._kiwoom.get_holdings()
            if not holdings and self.positions:
                invested = sum(p.avg_price * p.qty for p in self.positions.values())
                new_cash = max(0, server_cash - invested)
                # [FIX 2026-06-02] invested > server_cash이면 계산 신뢰 불가 → 기존값 유지
                if new_cash == 0 and self.cash > 0:
                    logger.warning("_sync_with_balance: 예수금 0 계산됨 (server=%d, invested=%d) — 기존값 유지",
                                   server_cash, invested)
                    return self.cash
                self.cash = new_cash
                return self.cash

            # sync_balance()와 동일 — opw00018 응답 기반 포지션 재구성
            self.positions = self._rebuild_positions_from_holdings(holdings)
            if self._kiwoom.is_mock:
                invested = sum(p.avg_price * p.qty for p in self.positions.values())
                new_cash = max(0, server_cash - invested)
                # [FIX 2026-06-02] 0원 오계산 방지 — 기존값이 있으면 유지
                if new_cash == 0 and self.cash > 0:
                    logger.warning("_sync_with_balance: 예수금 0 계산됨 (server=%d, invested=%d) — 기존값 유지",
                                   server_cash, invested)
                else:
                    self.cash = new_cash
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
        # [FIX 2026-05-26] exec_velocity_ratio는 ScanSignal에 없음 → snap_store에서 조회
        _sig_type  = getattr(signal, "signal_type", "?")
        _trend_lv  = int(getattr(signal, "trend_level", 0) or 0)
        _phase_nm  = getattr(signal, "entry_phase", 0)
        _vel_ratio = 0.0
        if self._snap_store:
            _snap_vr = self._snap_store.get_snapshot(code)
            if _snap_vr:
                _vel_ratio = float(getattr(_snap_vr, "exec_velocity_ratio", 0.0) or 0.0)
        order_log.info(
            "[신호수신] %s(%s) 가격=%d 유형=%s trend_lv=%d vel_ratio=%.2f phase=%s",
            name, code, price, _sig_type, _trend_lv, _vel_ratio, _phase_nm,
        )

        # ── 종목 강제 필터(매수 직전) ─────────────────────────────────────
        if not self._is_buy_allowed(code, name):
            return

        # [NEW] 데이터 신선도 체크 (Data Freshness)
        # 네트워크 지연으로 인한 3초 이상 과거 데이터 기반 신호 거절
        snap = self._snap_store.get_snapshot(code) if self._snap_store else None
        if snap and snap.updated_at:
            delay = (datetime.now() - snap.updated_at).total_seconds()
            if delay > 3.0:
                logger.warning("[신호거절] %s(%s) 시세 지연 %.1f초 (3초 초과) — 신선도 미달", name, code, delay)
                return

        _sector = ""
        try:
            _RISK = cfg.RISK
            _mx = float(_RISK.get("max_change_pct", 15.0))

            # [FIX 2026-05-12] opt10001 타임아웃 문제 해결: snap store 캐시 사용
            _info = None
            if snap:
                _info = {
                    "current_price": snap.current_price,
                    "change_pct": snap.change_pct,
                    "sector": getattr(snap, "sector", ""),
                }
                logger.debug("[매수정보] snap store 캐시 사용: %s", code)

            # fallback: opt10001 조회 (캐시 없을 때만)
            if _info is None:
                _info = self._kiwoom.get_stock_info(code)

            if _info is None:
                logger.warning("[매수거절] %s(%s) 실시간 정보 조회 실패 (opt10001)", name, code)
                return

            # [NEW] 주문 가격 호가 단위 보정 (Tick Size Alignment)
            from scanner.universe import align_price_to_hoga, get_hoga_unit
            m_type = getattr(snap, "market_type", "10") if snap else "10"
            
            curr = int(_info.get("current_price", 0))
            if curr > 0:
                price = align_price_to_hoga(curr, m_type, "round") # 현재가 기준 재정렬
            else:
                price = align_price_to_hoga(price, m_type, "round")

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
            # [Phase A 2026-05-19] OPENING 슬롯(09:00~09:30)에서 섹터 없음 허용
            # 이유: Phase A 거래대금 필터(1.2배)가 약한 신호를 이미 필터링함
            now_time = datetime.now().time()
            if datetime.strptime("09:00", "%H:%M").time() <= now_time <= datetime.strptime("09:30", "%H:%M").time():
                msg = f"[OPENING_GATE_ALLOWED] {name}({code}) 갭 상승 신호 진입 (Phase A 거래대금 필터 통과)"
                order_log.warning("[OPENING진입] %s — %s", name, msg)
                logger.warning(msg)
                # 섹터 체크 스킵, 계속 진행
            else:
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
                msg = f"중복 매수 차단 — {name}({code}) 이미 보유 중 (피라미딩 조건 미충족)"
                logger.debug(msg)
                self.order_failed.emit(msg)
                return


        if len(self.positions) + len(self._pending) >= self.max_positions:
            msg = f"최대 보유 종목 수 초과 ({self.max_positions}종목) — {name}({code}) 신호 대기열 등록"
            logger.info(msg)
            self.order_failed.emit(msg)
            self._queued_signal = signal   # 매도 체결 완료 시 자동 실행
            return

        # ── 수량 계산 (Dynamic Sizing) ───────────────────────────────────
        # [GUARD 2026-05-26] price 0 또는 음수면 사이징 진행 불가 — 즉시 거절
        if price <= 0:
            msg = f"매수 거절 — 진입가 비정상 ({price}) — {name}({code})"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return

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
            sl_pct = abs(float(getattr(self._scan_cfg, "jdm_stop_loss_pct", -1.2)))
            stop_price = int(price * (1 - sl_pct / 100.0))
            risk_per_share = max(1, price - stop_price)
            
            qty = risk_amount // risk_per_share
            order_log.info(
                "[사이징:RISK] 총자산=%s원 리스크=%s원(%s%%) 손절가=%s원 -> %d주",
                f"{total_equity:,}", f"{risk_amount:,}", f"{risk_pct}", f"{stop_price:,}", qty
            )

        else:
            # 3. EQUAL (기존 방식): 가용 예수금 / 남은 슬롯
            # [BUG FIX 2026-05-26] self.cash → self.available_cash (미체결 매수 차감)
            # remaining_slots에서 _pending(매수+매도 주문 대기)을 빼므로 슬롯 측에서도 중복 방지
            _cash_avail = self.available_cash
            remaining_slots = self.max_positions - len(self.positions) - len(self._pending)
            remaining_slots = max(remaining_slots, 1)
            budget = _cash_avail // remaining_slots
            qty = budget // price if price > 0 else 0
            order_log.info("[사이징:EQUAL] 가용예수금=%s원 슬롯=%d -> %d주", f"{_cash_avail:,}", remaining_slots, qty)

        # ── 가용 자금 및 주문 한도 체크 ─────────────────────────────────────────
        # 1회 주문 한도(max_order_amount) 적용
        max_qty = self.max_order_amount // price if price > 0 else 0
        if qty > max_qty:
            order_log.info("[사이징] 주문한도 초과 조정: %d -> %d주 (상한 %s원)", qty, max_qty, f"{self.max_order_amount:,}")
            qty = max_qty

        # 가용 예수금 초과 방지 (미체결 매수 차감 후 기준)
        _cash_avail = self.available_cash
        can_buy_qty = _cash_avail // price if price > 0 else 0
        if qty > can_buy_qty:
            order_log.info("[사이징] 예수금 부족 조정: %d -> %d주 (가용 %s원)", qty, can_buy_qty, f"{_cash_avail:,}")
            qty = can_buy_qty

        if qty <= 0:
            msg = f"매수 거절 — 수량 0 (예수금 부족 또는 가격 오류)"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return

        # 진입 메타데이터 임시 보관 → 체결 콜백에서 Position에 반영 (Phase G-1)
        # [BUG FIX 2026-05-26] ScanSignal의 ai_features는 .values dict에 저장됨.
        # 이전엔 getattr(signal, "eod_trade", ...) 형태로 직접 속성 찾았는데 ScanSignal엔
        # 그런 필드가 없어서 항상 기본값(False/0/"")만 반환 → EOD/candle_stop/near_daily_high 등 미작동.
        # → signal.values.get(...) 로 수정. 단 trend_level/trend_prev_level/entry_phase는
        #   smart_scanner._evaluate / trading_controller.handle_signal에서 동적 속성으로 할당되므로 그대로 유지.
        _vals = getattr(signal, "values", None) or {}

        # entry_gap_pct: 갭 상승 % (동적 손절선 산출에 활용)
        # snap에서 prev_close/open_price로 계산, fallback으로 values["gap_pct"] 사용
        _gap_pct = float(_vals.get("gap_pct", 0.0) or 0.0)
        if _gap_pct == 0.0 and self._snap_store:
            _snap_gap = self._snap_store.get_snapshot(code)
            if _snap_gap:
                _pc = float(getattr(_snap_gap, "prev_close", 0) or 0)
                _op = float(getattr(_snap_gap, "open_price", 0) or 0)
                if _pc > 0 and _op > 0:
                    _gap_pct = (_op - _pc) / _pc * 100

        self._pending_meta[code] = PendingOrderMeta(
            candle_stop=int(_vals.get("entry_candle_low", 0) or 0),
            trend_level=int(getattr(signal, "trend_level", 0) or 0),
            trend_prev_level=int(getattr(signal, "trend_prev_level", 0) or 0),
            near_daily_high=bool(_vals.get("near_daily_high", False)),
            custom_tp_pct=float(_vals.get("custom_tp_pct", 0.0) or 0.0),
            sector=_sector,
            eod_trade=bool(_vals.get("eod_trade", False)),
            entry_phase=int(getattr(signal, "entry_phase", 0) or 0),
            entry_gap_pct=_gap_pct,
            signal_price=int(getattr(signal, "price", 0) or 0),  # [방향 C] 슬리피지 체크용
        )

        # ✅ 피라미딩인 경우 수량 조절 (기본 50%)
        is_pyramid = code in self.positions
        if is_pyramid:
            pyramid_ratio = float(getattr(cfg.RISK, "pyramid_order_ratio", 0.5))
            qty = max(1, int(qty * pyramid_ratio))
            order_log.info("[피라미딩사이징] 수량 조절 (ratio %.1f): %d주", pyramid_ratio, qty)

        # ✅ [NEW] 호가 잔량 기반 슬리피지 방지 (Liquidity-Aware Sizing)
        # 매수 수량이 현재 매도 총잔량의 30%를 넘지 않도록 제한
        if self._snap_store:
            snap = self._snap_store.get_snapshot(code)
            if snap and snap.total_ask_qty > 0:
                liquidity_limit = max(1, int(snap.total_ask_qty * 0.3))
                if qty > liquidity_limit:
                    order_log.info("[사이징] 호가잔량 부족 조정: %d -> %d주 (매도잔량 %d의 30%%)", 
                                   qty, liquidity_limit, snap.total_ask_qty)
                    qty = liquidity_limit

        self.buy(code, name, qty, price=0)  # 시장가 매수

    @pyqtSlot(str, int, float, int)
    def _on_price_updated(self, code: str, price: int, pct: float, trend_level: int) -> None:
        """SmartScanner 실시간 가격 갱신 신호 처리 (손절/익절 정확도 개선)."""
        if price <= 0:
            return
        # position_repo 를 통해 포지션 현재가 갱신
        if hasattr(self, "position_repo"):
            self.position_repo.update_price(code, price)
        # ─── [NEW] 실시간 침착한 손절 (3초 유예 로직) ───────────────────────
        if code in self.positions:
            pos = self.positions[code]
            pos.current_price = price
            
            _sl = float(cfg.RISK.get("stop_loss_pct", -1.2))
            _hard = float(cfg.RISK.get("hard_stop_pct", -3.0))
            
            # 1. 하드 스탑 (-3.0% 등) — 즉시 탈출
            # [FIX 2026-05-29] 이미 블랙리스트 등록 = 이미 매도 주문 발행됨 → 중복 차단
            if pos.pnl_pct <= _hard:
                if code in self._stop_loss_today:
                    return
                logger.warning("🚨 [하드스탑] %s(%s) 임계치 돌파 (%.2f%%) — 즉시 매도", pos.name, code, pos.pnl_pct)
                self.mark_stop_loss(code)
                if self.on_tick_loss_exit:
                    self.on_tick_loss_exit(pos)
                self.force_exit(code, pos.name, pos.qty, "하드스탑")
                return

            if code in self._stop_loss_today:
                return  # 이미 손절 처리 중

            # 틱마다 peak_price 갱신 — update_state()는 캔들 주기에서만 호출되므로 여기서 직접 갱신
            if pos.current_price > pos.peak_price:
                pos.peak_price = pos.current_price

            # 2. 트레일 스탑 — Stop Loss보다 먼저 체크 (틱 핸들러에서 check_and_exit_all 보다 빠름)
            if pos.peak_price > 0 and pos.avg_price > 0:
                _scfg = getattr(self, "_scan_cfg", None)
                if _scfg is not None:
                    _activation = float(getattr(_scfg, "trail_activation_pct", 1.0))
                    _peak_chg = (pos.peak_price - pos.avg_price) / pos.avg_price * 100
                    if _peak_chg >= _activation:
                        # Tier 결정
                        _t1_max = float(getattr(_scfg, "trail_tier1_max", 3.0))
                        _t2_max = float(getattr(_scfg, "trail_tier2_max", 8.0))
                        _t1     = float(getattr(_scfg, "trail_pct_tier1",  1.2))
                        _t2     = float(getattr(_scfg, "trail_pct_tier2",  2.0))
                        _t3     = float(getattr(_scfg, "trail_pct_tier3",  3.0))
                        _trend  = int(getattr(pos, "trend_level", 0))
                        _strong = _trend >= int(getattr(_scfg, "strong_trend_hold_level", 3))
                        if _strong:
                            _tp = _t2 if _peak_chg < _t2_max else _t3
                        else:
                            if _peak_chg < _t1_max:
                                # 분할익절 완료 후 잔여 포지션은 Tier2 폭으로 여유 부여
                                _tp = _t2 if getattr(pos, "partial_sold", False) else _t1
                            elif _peak_chg < _t2_max:
                                _tp = _t2
                            else:
                                _tp = _t3
                        _trail_price = int(pos.peak_price * (1 - _tp / 100))
                        if pos.current_price <= _trail_price:
                            logger.info("🎯 [트레일스탑] %s(%s) 고점%s→현재%s (고점대비-%.1f%%) — 즉시 매도",
                                        pos.name, code, f"{pos.peak_price:,}", f"{pos.current_price:,}", _tp)
                            self.force_exit(code, pos.name, pos.qty, "트레일스탑")
                            return

            # 3. 일반 손절 (-1.2% 등) — 3초 유예
            if pos.pnl_pct <= _sl:
                if pos.sl_triggered_at is None:
                    pos.sl_triggered_at = datetime.now()
                    logger.info("⏳ [손절대기] %s(%s) 손절가 하회 (%.2f%%) — 3초 관찰 시작", pos.name, code, pos.pnl_pct)
                else:
                    elapsed = (datetime.now() - pos.sl_triggered_at).total_seconds()
                    if elapsed >= 3.0:
                        logger.warning("📉 [확정손절] %s(%s) 3초간 손절가 하회 — 매도 및 블랙리스트 등록", pos.name, code)
                        self.mark_stop_loss(code)
                        if self.on_tick_loss_exit:
                            self.on_tick_loss_exit(pos)
                        self.force_exit(code, pos.name, pos.qty, "확정손절")
                    else:
                        logger.debug("⏳ [손절대기] %s 관찰 중... (%.1fs)", pos.name, elapsed)
            else:
                # 가격이 다시 회복되면 타이머 초기화 (털기 방지의 핵심)
                if pos.sl_triggered_at is not None:
                    logger.info("✅ [손절취소] %s(%s) 가격 회복 (%.2f%%) — 보유 유지", pos.name, code, pos.pnl_pct)
                    pos.sl_triggered_at = None

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
        Strong(3)에서 Weak(1) 이하로 전환 시 True 반환.
        기존 3→0만 감지하던 조건 완화: 실제로 3→0 즉각 전환은 거의 발생하지 않음.
        3→2→1 단계적 하락 시 1 도달 시점에 청산해 추가 손실 방지.
        """
        pos = self.positions.get(code)
        if pos is None:
            return False
        return int(pos.trend_prev_level) == 3 and int(pos.trend_level) <= 1

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

        [FIX 2026-05-28] 중복 호출 차단 — 이미 등록된 종목이면 즉시 return
        5/28 차백신연구소 811회 등록, 5/22 에코프로 435회 등록 같은 무한루프 방지
        """
        if code in self._stop_loss_today:
            return  # 이미 등록됨 — 로그/연산 모두 스킵
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
            self._queued_signal = None       # 전날 대기 신호 → 익일 오래된 신호로 매수 방지
            self._partial_pending_codes.clear()  # 전날 분할매도 잔존 → 익일 is_partial 오기록 방지
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
        from scanner.universe import is_pure_equity_name

        # 0-1) 손절 락 확인 — RiskManager에서 손절 한도 도달 시 차단
        if self.state and self.state.loss_cut_locked:
            msg = f"매수 차단 — 당일 손절 한도 도달 (손절 락 활성화)"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return False

        # 0-2) 지수 급락 차단 — AppState.is_crash 설정 시 신규 매수 전면 차단
        if self.state and getattr(self.state, "is_crash", False):
            msg = f"매수 차단 — 지수 급락 감지 (시장 위기)"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return False

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

        # 3) 당일 손절 블랙리스트 차단 (익절/수동매도 시에는 재진입 허용)
        if code in self._stop_loss_today:
            msg = f"매수 차단 — {name}({code}) 당일 손절 블랙리스트 종목"
            logger.info(msg)
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
        # 장 운영 시간 외 매도 주문 차단 (RC4058 방지)
        # 장 종료(15:30) 후 재연결 등으로 force_exit가 재호출될 수 있음
        from datetime import time as _dtime
        _now_t = datetime.now().time()
        _MARKET_OPEN  = _dtime(8, 55)
        _MARKET_CLOSE = _dtime(15, 35)
        if not (_MARKET_OPEN <= _now_t <= _MARKET_CLOSE):
            logger.warning(
                "[force_exit] %s(%s) 장 외 시간 매도 보류 — 현재 %s (운영: 08:55~15:35)",
                code, reason, _now_t.strftime("%H:%M:%S"),
            )
            return "0"

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
                from scanner.universe import get_hoga_unit
                tick = get_hoga_unit(cur, getattr(pos, "market_type", "10"))
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
            # [FIX 2026-06-01] 10초→30초 — OPENING 슬롯에서 체결 지연이 수십 초 발생
            # 휴림로봇: 09:18:02 주문→10초 후 취소→09:24:03 뒤늦게 체결→즉시 청산 사례
            if elapsed < 30:
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

    def force_sell(self, code: str) -> str:
        """포지션의 모든 수량을 시장가로 강제 매도한다."""
        pos = self.positions.get(code)
        if pos is None:
            logger.warning("강제 매도 실패 — 포지션 없음: %s", code)
            return ""
        return self.sell(code, pos.name, pos.qty, price=0, order_type="")

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
        """최종 주문 전송 (호가 단위 정렬 보강)."""
        if qty <= 0:
            return "0"

        # [NEW] 최종 가격 보정 (최종 수비수)
        if price > 0:
            from scanner.universe import align_price_to_hoga
            snap = self._snap_store.get_snapshot(code)
            m_type = getattr(snap, "market_type", "10") if snap else "10"
            price = align_price_to_hoga(price, m_type, "round")

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
            # [BUG FIX 2026-05-26] price 추가 저장 — available_cash 계산에 사용
            self._pending_buy_time[code] = datetime.now()
            self._pending_buy_info[code] = {
                "qty": qty, "name": name, "order_no": rq_name, "price": price,
            }
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
        """라우터 역할: FID 파싱 → 타입 판별 → 핸들러 호출"""
        def cj(fid: int) -> str:
            return self._kiwoom._ocx.dynamicCall(
                "GetChejanData(int)", [fid]
            ).strip()

        if gubun != CHEJAN_FILL:
            return
        self._roll_daily_realized_pnl_if_needed()

        code        = cj(9001).lstrip("A")
        name        = self._kiwoom._fix_enc(cj(302))
        _raw_qty    = cj(911)
        _raw_price  = cj(910)
        _cum_qty    = abs(int(_raw_qty   or 0))
        filled_price= abs(int(_raw_price or 0))
        order_no    = cj(9203)
        _prev_cum   = self._order_fill_cumulative.get(order_no, 0)
        filled_qty  = max(0, _cum_qty - _prev_cum)
        self._order_fill_cumulative[order_no] = _cum_qty

        _ot_str = self._kiwoom._fix_enc(cj(905))
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

        # 취소 후 뒤늦은 체결 방어
        if order_type == OrderType.BUY and order_no in self._cancelled_buy_orders:
            self._handle_cancelled_buy(code, name, filled_qty, filled_price, order_no)
            return

        # 분기: 매수 vs 매도
        if order_type == OrderType.BUY:
            self._handle_buy_fill(code, name, filled_qty, filled_price, order_no, _prev_cum)
        else:
            self._handle_sell_fill(code, name, filled_qty, filled_price, order_no)

    def _handle_cancelled_buy(self, code: str, name: str, filled_qty: int, filled_price: int, order_no: str) -> None:
        """취소 후 뒤늦은 체결 방어"""
        self._cancelled_buy_orders.discard(order_no)
        logger.warning(
            "[취소후체결] %s(%s) 취소 주문 %s 이 뒤늦게 체결됨 (%d주 @%d) — 즉시 시장가 청산",
            name, code, order_no, filled_qty, filled_price,
        )
        if code in self.positions:
            pos = self.positions[code]
            total_qty = pos.qty + filled_qty
            pos.avg_price = (pos.avg_price * pos.qty + filled_price * filled_qty) // total_qty
            pos.qty = total_qty
        else:
            self.positions[code] = Position(
                code=code, name=name,
                qty=filled_qty, avg_price=filled_price,
                current_price=filled_price,
            )
        self.sell(code, name, self.positions[code].qty)

    def _handle_buy_fill(self, code: str, name: str, filled_qty: int, filled_price: int, order_no: str, prev_cum: int) -> None:
        """매수 체결 전담"""
        is_app_buy = code in self._app_pending_buys
        
        self._pending_buy_time.pop(code, None)
        self._pending_buy_info.pop(code, None)
        meta = self._pending_meta.pop(code, PendingOrderMeta())
        
        if is_app_buy:
            rem = self._app_pending_buys[code] - filled_qty
            if rem <= 0:
                del self._app_pending_buys[code]
                self._order_fill_cumulative.pop(order_no, None)
                self._pending.discard(code)
            else:
                self._app_pending_buys[code] = rem

        # [2026-06-01] 신호가 대비 체결가 슬리피지 초과 시 즉시 취소 매도
        # [2026-06-02] 기본값 3.0% → 1.5% 강화: LG디스플레이 신호17,080→체결17,380(+1.75%) 손실 패턴
        # 신호가가 0이면 체크 불가 (시장가 신호 등) → 통과
        _signal_price = int(getattr(meta, "signal_price", 0) or 0)
        if _signal_price > 0 and prev_cum == 0:  # 첫 체결에만 체크
            _slippage_pct = abs(filled_price - _signal_price) / _signal_price * 100
            _max_slip = float(getattr(cfg, "max_entry_slippage_pct", 1.5))
            if _slippage_pct >= _max_slip:
                logger.warning(
                    "[슬리피지초과] %s(%s) 신호가=%d → 체결가=%d (%.1f%% ≥ %.1f%%) — 즉시 매도",
                    name, code, _signal_price, filled_price, _slippage_pct, _max_slip
                )
                # 포지션 등록 후 즉시 청산
                if code not in self.positions:
                    self.positions[code] = Position(
                        code=code, name=name,
                        qty=filled_qty, avg_price=filled_price,
                        current_price=filled_price,
                    )
                self.sell(code, name, filled_qty)
                return

        if code in self.positions:
            pos = self.positions[code]
            if prev_cum == 0:
                pos.entry_count = getattr(pos, "entry_count", 1) + 1
                logger.info("🚀 [피라미딩체결] %s(%s) 진입 횟수 증가 -> %d회", name, code, pos.entry_count)

            total_qty = pos.qty + filled_qty
            pos.avg_price = (pos.avg_price * pos.qty + filled_price * filled_qty) // total_qty
            pos.qty = total_qty
            pos.trend_level = int(meta.trend_level)
            pos.trend_prev_level = int(meta.trend_prev_level)
            if is_app_buy:
                pos.qty_buy_today_app += filled_qty
                pos.opened_by_app = True
                if pos.buy_date is None: pos.buy_date = date.today()
        else:
            self.positions[code] = Position(
                code=code, name=name,
                qty=filled_qty, avg_price=filled_price,
                current_price=filled_price,
                # peak_price를 체결가로 초기화 — 0이면 틱 핸들러의 peak_price>0 가드를 통과 못해 trail 영구 비활성
                peak_price=filled_price,
                buy_date=date.today() if is_app_buy else None,
                entry_time=datetime.now() if is_app_buy else None,
                opened_by_app=is_app_buy,
                qty_buy_today_app=filled_qty if is_app_buy else 0,
                candle_stop_price=meta.candle_stop,
                trend_level=int(meta.trend_level),
                trend_prev_level=int(meta.trend_prev_level),
                near_daily_high=meta.near_daily_high,
                custom_tp_pct=meta.custom_tp_pct,
                eod_trade=meta.eod_trade,
                entry_phase=meta.entry_phase,
                sector=meta.sector,
                entry_gap_pct=meta.entry_gap_pct,
            )
            position_log.info(
                "[포지션생성] %s(%s) 체결가=%d 수량=%d 섹터=[%s] trend_lv=%d phase=%d",
                name, code, filled_price, filled_qty,
                meta.sector or "-", int(meta.trend_level), int(meta.entry_phase),
            )

        # 공통 마무리 로직
        buy_amt = filled_qty * filled_price
        fee = int(buy_amt * _FEE)
        self.cash -= (buy_amt + fee)
        self._today_fill_log.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "side": "buy", "code": code, "name": name,
            "qty": filled_qty, "price": filled_price, "amount": buy_amt
        })
        if self._audit: self._audit.log_buy_fill(code, filled_qty, filled_price)
        if self.on_position_opened: self.on_position_opened(code)
        self._finalize_fill(code, name, filled_qty, filled_price, OrderType.BUY)

    def _handle_sell_fill(self, code: str, name: str, filled_qty: int, filled_price: int, order_no: str) -> None:
        """매도 체결 전담"""
        if code not in self.positions:
            return
            
        pos = self.positions[code]
        avg_buy_for_log = pos.avg_price
        sell_amount = filled_price * filled_qty
        buy_amount  = pos.avg_price * filled_qty
        cost = round(sell_amount * (_FEE + _TAX) + buy_amount * _FEE)
        realized = (filled_price - pos.avg_price) * filled_qty - cost
        
        self._today_fill_log.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "side": "sell", "code": code, "name": name,
            "qty": filled_qty, "price": filled_price, "amount": sell_amount, "realized": realized
        })
        
        is_partial_fill = code in self._partial_pending_codes
        self._partial_pending_codes.discard(code)
        self._append_fill_to_file(
            realized=realized, code=code, name=name,
            sell_price=filled_price, avg_price=pos.avg_price, qty=filled_qty,
            is_partial=is_partial_fill,
        )
        if self._audit:
            self._audit.log_sell_fill(code, filled_qty, filled_price, avg_buy_price=pos.avg_price, realized_pnl=realized)
            
        self._recompute_daily_realized_from_ledger()
        sell_from_today = min(filled_qty, pos.qty_buy_today_app)
        pos.qty_buy_today_app -= sell_from_today
        pos.qty -= filled_qty
        remaining_qty = pos.qty

        # cash는 분할/완전 체결 모두에서 갱신해야 하며, 대기 신호 처리 전에 반영해야 함
        self.cash += filled_qty * filled_price

        if pos.qty <= 0:
            _pnl_pct = (filled_price - avg_buy_for_log) / avg_buy_for_log * 100 if avg_buy_for_log else 0
            position_log.info("[포지션청산] %s(%s) 매도가=%d 손익=%+.2f%%", name, code, filled_price, _pnl_pct)
            if self.on_position_closed: self.on_position_closed(code)
            del self.positions[code]
            self._force_sell_issued.discard(code)
            self._pending_sell_retries.pop(code, None)
            if self._queued_signal:
                queued = self._queued_signal
                self._queued_signal = None
                self.handle_signal(queued)
        self._finalize_fill(code, name, filled_qty, filled_price, OrderType.SELL, realized, avg_buy_for_log, remaining_qty=remaining_qty)

    def _finalize_fill(self, code, name, filled_qty, filled_price, order_type, realized=0, avg_buy=None, remaining_qty: int = 0):
        """체결 공통 마무리 (상태 정리, 신호 발행)"""
        self._pending.discard(code)
        self._pending_sell_time.pop(code, None)

        payload = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "side": "매수체결" if order_type == OrderType.BUY else "매도체결",
            "code": code, "name": name, "filled_qty": filled_qty, "filled_price": filled_price,
            "realized_pnl": realized, "remaining_qty": remaining_qty,
        }
        if avg_buy: payload["avg_buy_price"] = avg_buy
        if self.state: self.state.update_portfolio(self.cash, dict(self.positions))

        # [v3.0] 통합 알림 발송
        if self.notif_mgr:
            side_nm = "매수체결" if order_type == OrderType.BUY else "매도체결"
            res_icon = "💰" if (order_type == OrderType.SELL and realized > 0) else ("📉" if realized < 0 else "🔔")
            pnl_str = f" (손익: {realized:+,}원)" if order_type == OrderType.SELL else ""
            self.notif_mgr.info(
                f"{res_icon} {side_nm}",
                f"{name}({code}) {filled_qty}주 @ {filled_price:,}원{pnl_str}",
                telegram=True, sound=True
            )

        self.order_filled.emit(payload)
        
        side_nm = "매수" if order_type == OrderType.BUY else "매도"
        logger.info("체결 — %s %s %d주 @%s원", name, side_nm, filled_qty, f"{filled_price:,}")

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
