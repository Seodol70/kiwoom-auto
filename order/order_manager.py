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

from config import COST as _COST

logger = logging.getLogger(__name__)

_FEE = _COST.get("fee_rate", 0.00015)
_TAX = _COST.get("tax_rate", 0.0023)


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

class OrderType:
    BUY         = 1
    SELL        = 2
    BUY_CANCEL  = 3
    SELL_CANCEL = 4

class PriceType:
    LIMIT  = "00"
    MARKET = "03"

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
    opened_by_app: bool = False           # 앱 SendOrder 매수로 보유가 생기거나 늘어난 적 있음
    qty_buy_today_app: int = 0            # 오늘 앱에서 매수한 수량 (장마감 자동청산 대상)

    @property
    def pnl(self) -> int:
        """평가손익(원): 수수료·세금 차감 후."""
        gross    = (self.current_price - self.avg_price) * self.qty
        buy_fee  = int(self.avg_price    * self.qty * _FEE)              # 매수 수수료
        sell_fee = int(self.current_price * self.qty * (_FEE + _TAX))    # 매도 수수료+세금
        return gross - buy_fee - sell_fee

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

    @property
    def pnl_pct(self) -> float:
        """손절·익절·표시용 순수 등락률 — price_change_pct_vs_avg 와 동일."""
        return self.price_change_pct_vs_avg


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

        self.cash:      int = 0                          # 가용 예수금
        self.positions: dict[str, Position] = {}         # 보유 종목
        self.orders:    dict[str, OrderRecord] = {}      # 전체 주문 기록
        self._pending:  set[str] = set()                 # 주문 중 종목 (중복 방지)
        self._app_pending_buys: dict[str, int] = {}       # code -> 남은 앱 매수 주문 수량 (부분체결 추적)
        self._pnl_date: date = date.today()
        # 당일 실현손익 = 파일에서 복구한 이전 세션 합 + 이번 세션 매도 체결 합
        self.daily_realized_pnl: int = 0
        self._broker_realized_base: int = 0               # 시작 시 파일/opt10074에서 복구한 당일 누적 기준값
        self._today_fill_log: list[dict] = []             # 이번 세션 체결 로그
        self._fills_initialized: bool = False             # 시작 시 1회만 파일 로드하는 플래그

        # 포지션 실시간 현재가 구독 콜백 (SmartScanner에서 주입)
        self.on_position_opened: Optional[Callable[[str], None]] = None
        self.on_position_closed: Optional[Callable[[str], None]] = None

        self._connect_chejan()

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

            holdings = self._kiwoom.get_holdings()
            # opw00018 모의투자 서버는 매입가 필드를 반환하지 않음 → 기존 메모리값 보존
            new_positions: dict[str, Position] = {}
            for h in holdings:
                code = h["code"]
                avg = h["avg_price"]
                if avg == 0 and code in self.positions:
                    avg = self.positions[code].avg_price
                old = self.positions.get(code)
                qty_today = min(old.qty_buy_today_app, h["qty"]) if old else 0
                new_positions[code] = Position(
                    code          = code,
                    name          = h["name"],
                    qty           = h["qty"],
                    avg_price     = avg,
                    current_price = h["current_price"],
                    buy_date      = old.buy_date if old else None,
                    opened_by_app = old.opened_by_app if old else False,
                    qty_buy_today_app = qty_today,
                )
            self.positions = new_positions

            # 모의투자 서버는 opw00001 "예수금"을 투자금 차감 없이 반환한다.
            # 보유 종목 매입금액 합계를 차감해 실제 가용 예수금을 추정한다.
            invested = sum(p.avg_price * p.qty for p in self.positions.values())
            self.cash = max(0, server_cash - invested)
            logger.info("잔고 동기화 완료 — 예수금 %s원 (서버=%s / 투자=%s) / 보유 %d종목",
                        f"{self.cash:,}", f"{server_cash:,}", f"{invested:,}",
                        len(self.positions))
            self._sync_daily_realized_from_broker()
        except Exception as e:
            logger.error("잔고 동기화 실패: %s", e)
        return self.cash

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

        # ── 종목 강제 필터(매수 직전) ─────────────────────────────────────
        if not self._is_buy_allowed(code, name):
            return

        try:
            from config import RISK as _RISK
            _mx = float(_RISK.get("max_change_pct", 15.0))
            _info = self._kiwoom.get_stock_info(code)
            _pct = float(_info.get("change_pct", 0) or 0)
            logger.debug("[매수 등락률 체크] %s — 현재 등락률: %.2f%% (상한: %.1f%%)", name, _pct, _mx)
            if _pct >= _mx:
                msg = f"매수 차단 — 등락률 {_pct:.1f}% ≥ 상한 {_mx:.1f}% ({name})"
                logger.warning(msg)
                self.order_failed.emit(msg)
                return
        except Exception as _e:
            logger.debug("등락률 사전 확인 실패(무시): %s", _e)

        # ── 안전 장치 ────────────────────────────────────────────────────
        if code in self._pending:
            logger.debug("중복 주문 방지 — %s 이미 주문 중", code)
            return

        if code in self.positions:
            logger.debug("중복 매수 방지 — %s 이미 보유 중", code)
            return

        if len(self.positions) + len(self._pending) >= self.max_positions:
            msg = f"최대 보유 종목 수 초과 ({self.max_positions}종목)"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return

        # ── 수량 계산 — 100% 현금 운용 (예수금 전액 소진) ──────────────────
        # 남은 슬롯 수 기준으로 예수금을 균등 분배
        remaining_slots = self.max_positions - len(self.positions) - len(self._pending)
        remaining_slots = max(remaining_slots, 1)  # 0 나누기 방지
        budget = self.cash // remaining_slots
        qty = budget // price if price > 0 else 0

        # ── 가용 예수금 부족 체크 ─────────────────────────────────────────
        # 최소 1주 매수 불가능하면 주문 미실행
        if qty <= 0:
            msg = f"가용 예수금 부족 — 최소 1주 매수 불가 (예수금 {self.cash:,} / 주가 {price:,})"
            logger.warning(msg)
            self.order_failed.emit(msg)
            return

        self.buy(code, name, qty, price=0)  # 시장가 매수

    def is_pending(self, code: str) -> bool:
        return code in self._pending

    def _roll_daily_state_if_needed(self) -> None:
        """날짜가 바뀌면 당일 실현손익·체결 로그·오늘 앱 매수 수량을 초기화한다."""
        today = date.today()
        if self._pnl_date != today:
            self._pnl_date = today
            self.daily_realized_pnl = 0
            self._broker_realized_base = 0
            self._today_fill_log.clear()
            self._fills_initialized = False  # 새 날짜 → 세션 초기화 재허용
            for p in self.positions.values():
                p.qty_buy_today_app = 0

    # ── 당일 실현손익 — 체결 이력 파일 (append-only, 재시작 복구 지원) ─────────
    # 절대 경로: order_manager.py 위치 기준 → 프로젝트루트/logs/fills_YYYYMMDD.jsonl
    _FILLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"

    def _fills_path(self) -> pathlib.Path:
        return self._FILLS_DIR / f"fills_{date.today().strftime('%Y%m%d')}.jsonl"

    def _append_fill_to_file(self, realized: int, code: str, name: str,
                              sell_price: int, avg_price: int, qty: int) -> None:
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
        """ETF/ETN/관리·정지·투자경고 종목을 매수 직전에 강제 차단한다."""
        # 1) 이름 키워드 차단 (scanner.smart_scanner.is_pure_equity_name 과 동일)
        nm_orig = (name or "").strip()
        nm = nm_orig.upper()
        exclude_kw = (
            "ETF", "ETN", "인버스", "레버리지", "곱버스", "역추적",
            "2X", "3X", "5X", "10X", "스팩", "SPAC", "헷지", "HEDGE",
            "선물", "옵션", "수익증권", "구조", "파생",
            "KODEX", "TIGER", "KBSTAR", "HANAR", "KOSEF", "ARIRANG",
            "TIMEFOLIO", "KINDEX", "ACE", "RISE", "SOL", "FOCUS",
        )
        if any(kw in nm_orig or kw in nm for kw in exclude_kw):
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
    ) -> str:
        """시장가(price=0) 또는 지정가 매수 주문을 전송한다."""
        # sync_balance() 는 주문 직후 호출하지 않음:
        # 모의투자 서버가 즉시 0원 반환해 예수금을 잘못 덮어쓰는 문제 방지.
        # 정확한 잔고는 OnReceiveChejanData 콜백 + 주기 sync_balance(5분)에서 반영.
        return self._send(OrderType.BUY, code, name, qty, price)

    def sell(
        self,
        code:  str,
        name:  str,
        qty:   int,
        price: int = 0,
    ) -> str:
        """시장가 또는 지정가 매도 주문을 전송한다."""
        return self._send(OrderType.SELL, code, name, qty, price)

    def _send(
        self,
        order_type: int,
        code: str,
        name: str,
        qty:  int,
        price: int,
    ) -> str:
        price_type = PriceType.MARKET if price == 0 else PriceType.LIMIT
        rq_name    = f"{'매수' if order_type == OrderType.BUY else '매도'}_{code}"

        ret = self._kiwoom._ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rq_name, "1001", self._account,
             order_type, code, qty, price, price_type, ""],
        )

        side = "매수" if order_type == OrderType.BUY else "매도"
        if ret != 0:
            msg = f"{name} {side} 주문 실패 (ret={ret})"
            logger.error(msg)
            self.order_failed.emit(msg)
            return ""

        self._pending.add(code)
        if order_type == OrderType.BUY:
            self._app_pending_buys[code] = qty

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
        filled_qty  = abs(int(_raw_qty   or 0))
        filled_price= abs(int(_raw_price or 0))
        order_no    = cj(9203)
        logger.info("체결원시 — %s FID910(체결가)=%r FID911(체결량)=%r → price=%d qty=%d",
                    name, _raw_price, _raw_qty, filled_price, filled_qty)
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

        avg_buy_for_log: Optional[int] = None  # 매도 체결 로그용 (포지션 갱신 전 평단)

        # 포지션 반영
        if order_type == OrderType.BUY:
            if is_app_buy:
                rem = self._app_pending_buys[code] - filled_qty
                if rem <= 0:
                    del self._app_pending_buys[code]
                else:
                    self._app_pending_buys[code] = rem

            if code in self.positions:
                pos = self.positions[code]
                total_qty   = pos.qty + filled_qty
                pos.avg_price = (pos.avg_price * pos.qty + filled_price * filled_qty) // total_qty
                pos.qty      = total_qty
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
                    opened_by_app=is_app_buy,
                    qty_buy_today_app=filled_qty if is_app_buy else 0,
                )
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
            # [NEW] 포지션 실시간 등록
            if self.on_position_opened:
                self.on_position_opened(code)

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
                # [NEW] 매도 체결을 파일에 append → 재시작 후에도 복구 가능
                self._append_fill_to_file(
                    realized=realized, code=code, name=name,
                    sell_price=filled_price, avg_price=pos.avg_price, qty=filled_qty,
                )
                self._recompute_daily_realized_from_ledger()
                sell_from_today = min(filled_qty, pos.qty_buy_today_app)
                pos.qty_buy_today_app -= sell_from_today
                pos.qty -= filled_qty
                if pos.qty <= 0:
                    # [NEW] 포지션 실시간 해제
                    if self.on_position_closed:
                        self.on_position_closed(code)
                    del self.positions[code]
            self.cash += filled_qty * filled_price

        self._pending.discard(code)

        payload = {
            "time":         datetime.now().strftime("%H:%M:%S"),
            "side":         "매수체결" if order_type == OrderType.BUY else "매도체결",
            "code":         code,
            "name":         name,
            "filled_qty":   filled_qty,
            "filled_price": filled_price,
        }
        if avg_buy_for_log is not None:
            payload["avg_buy_price"] = avg_buy_for_log

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
        self.order_filled.emit(payload)
