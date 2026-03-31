"""
KiwoomManager — 키움증권 Open API+ 연동 핵심 클래스

의존: PyQt5 (QApplication + QAxWidget), pywin32
설치: pip install PyQt5 pywin32

키움 Open API+는 32-bit COM 오브젝트이므로
  - Python 32-bit 환경 필수
  - QApplication 이벤트 루프 위에서만 동작
"""

from __future__ import annotations

import collections
import logging
import sys
import threading
import time
from typing import Callable, Optional

try:
    from PyQt5.QAxContainer import QAxWidget
    _OCX_AVAILABLE = True
except Exception:
    _OCX_AVAILABLE = False
    QAxWidget = None  # type: ignore

from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

class ReturnCode:
    OK             =  0
    COMM_FAIL      = -100
    LOGIN_FAIL     = -101
    LOGIN_TIMEOUT  = -102

class OrderType:
    BUY    = 1  # 신규매수
    SELL   = 2  # 신규매도
    BUY_CANCEL  = 3  # 매수취소
    SELL_CANCEL = 4  # 매도취소

class PriceType:
    LIMIT  = "00"  # 지정가
    MARKET = "03"  # 시장가

# TR 코드
TR_STOCK_INFO  = "opt10001"   # 주식기본정보요청
TR_ACCOUNT     = "opw00001"   # 예수금상세현황요청
TR_HOLDINGS    = "opw00018"   # 계좌평가잔고내역요청
TR_MIN_CANDLE  = "opt10080"   # 주식분봉차트조회요청

LOGIN_TIMEOUT_SEC = 30
TR_DELAY_SEC      = 0.25      # TR 연속 조회 제한(초) — 키움 정책 200ms+


# ---------------------------------------------------------------------------
# TR 레이트 리미터 — 1초 5회 제한 (슬라이딩 윈도우)
# ---------------------------------------------------------------------------

class TRRateLimiter:
    """
    키움 TR 요청 레이트 리미터 (슬라이딩 윈도우 방식).

    키움 정책: 1초당 최대 5회, 연속 TR 간 최소 0.2초 간격.
    실제로는 안전 마진을 두어 4회/초, 0.25초 간격으로 제한한다.

    사용 예)
        _lim = TRRateLimiter()
        _lim.acquire()   # 필요 시 sleep 후 반환
        # TR 호출
    """
    MAX_PER_SEC  = 4      # 1초당 최대 호출 횟수 (5→4, 안전 마진)
    MIN_INTERVAL = 0.25   # 연속 호출 최소 간격 (초)

    def __init__(self) -> None:
        self._timestamps: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """TR 호출 전 반드시 호출. 필요 시 sleep 후 반환한다."""
        with self._lock:
            now = time.monotonic()

            # 1. 최소 간격 보장
            if self._timestamps:
                wait = self.MIN_INTERVAL - (now - self._timestamps[-1])
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()

            # 2. 슬라이딩 윈도우(1초) 내 호출 수 확인
            cutoff = now - 1.0
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.MAX_PER_SEC:
                wait = (self._timestamps[0] + 1.0) - now
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
                # 윈도우 재정리
                cutoff = now - 1.0
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()

            self._timestamps.append(time.monotonic())


# ---------------------------------------------------------------------------
# 데이터 안전 변환 헬퍼
# ---------------------------------------------------------------------------

def safe_int(val, default: int = 0) -> int:
    """
    키움 API 문자열 → int 안전 변환.

    처리 케이스:
      " +1,234 "  → 1234
      "-5678"     → 5678  (절댓값)
      "+"         → 0     (부호만)
      ""  / None  → 0
      "1.5"       → 1     (float 경유)
    """
    if val is None:
        return default
    s = str(val).strip().replace(",", "")
    if not s or s in ("+", "-"):
        return default
    try:
        return abs(int(float(s)))
    except (ValueError, TypeError):
        return default


def safe_float(val, default: float = 0.0) -> float:
    """
    키움 API 문자열 → float 안전 변환 (부호는 그대로 유지).

    처리 케이스:
      " +1.23 "  → 1.23
      "-5.67"    → -5.67
      "+"        → 0.0
      ""  / None → 0.0
    """
    if val is None:
        return default
    s = str(val).strip().replace(",", "")
    if not s or s in ("+", "-"):
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# KiwoomManager
# ---------------------------------------------------------------------------

class KiwoomManager:
    """
    키움 Open API+ 래퍼.

    사용 예)
        app = QApplication(sys.argv)
        kiwoom = KiwoomManager()
        kiwoom.login()          # 블로킹 — 로그인 완료까지 대기
        info = kiwoom.get_stock_info("005930")
        app.exec_()
    """

    def __init__(self) -> None:
        import struct
        if struct.calcsize("P") * 8 != 32:
            raise RuntimeError("Kiwoom API requires 32-bit Python (currently 64-bit)")
        if not _OCX_AVAILABLE:
            raise RuntimeError("PyQt5.QAxContainer unavailable")
        self._ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._tr_loop: Optional[QEventLoop] = None
        self._tr_data: dict = {}

        self._account_list: list[str] = []
        self._account: str = ""

        # 자동 재로그인 관련
        self._is_connected: bool = False
        self._auto_login_callback: Optional[callable] = None  # 재로그인 성공 시 콜백
        self._tr_prev_next: str = ""  # 연속조회 여부 ("2" = 다음 데이터 있음)

        # TR 레이트 리미터 — 1초 4회 / 0.25초 간격 보장
        self._tr_limiter = TRRateLimiter()

        self._connect_signals()

    # -----------------------------------------------------------------------
    # 시그널 연결
    # -----------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._ocx.OnEventConnect.connect(self._on_event_connect)
        self._ocx.OnReceiveTrData.connect(self._on_receive_tr_data)
        self._ocx.OnReceiveMsg.connect(self._on_receive_msg)
        self._ocx.OnReceiveChejanData.connect(self._on_receive_chejan_data)

    # -----------------------------------------------------------------------
    # 로그인
    # -----------------------------------------------------------------------

    def login(self) -> bool:
        """
        로그인 창을 띄우고 완료될 때까지 블로킹 대기.
        Returns: 성공 여부
        """
        logger.info("로그인 요청 중...")
        self._ocx.dynamicCall("CommConnect()")
        ok = self._login_event.wait(timeout=LOGIN_TIMEOUT_SEC)

        if not ok:
            logger.error("로그인 타임아웃 (%d초)", LOGIN_TIMEOUT_SEC)
            return False

        self._account_list = self._get_account_list()
        if not self._account_list:
            logger.error("계좌 목록을 가져올 수 없습니다.")
            return False

        self._account = self._account_list[0]
        logger.info("로그인 성공 — 계좌: %s", self._account)
        return True

    def _get_account_list(self) -> list[str]:
        raw: str = self._ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        return [a for a in raw.strip().split(";") if a]

    def get_login_state(self) -> int:
        """1: 로그인, 0: 미로그인"""
        return self._ocx.dynamicCall("GetConnectState()")

    # -----------------------------------------------------------------------
    # opt10030 거래대금 상위 (연속조회 지원)
    # -----------------------------------------------------------------------

    def fetch_opt10030_top_volume(self, max_rows: int = 200) -> list[dict]:
        """
        opt10030 거래대금 상위 종목. 한 페이지(최대 약 100행)를 넘기면 연속조회로 이어 받는다.

        키움: prev_next 콜백이 '2'이면 다음 페이지가 있음 → CommRqData(..., prev_next=2).
        """
        all_rows: list[dict] = []
        prev_next = 0
        page = 0
        while len(all_rows) < max_rows:
            self._set_input("시장구분", "0")
            self._set_input("정렬구분", "1")
            self._set_input("관리종목포함", "0")
            self._set_input("신용구분", "0")
            self._comm_rq("opt10030", "거래대금상위", "9000", prev_next=prev_next)
            chunk = self._tr_data.get("rows", [])
            page += 1
            if not chunk:
                logger.debug("[opt10030] 페이지 %d 응답 없음 — 종료", page)
                break
            all_rows.extend(chunk)
            logger.debug(
                "[opt10030] 페이지 %d 누적 %d행 (prev_next=%s)",
                page, len(all_rows), self._tr_prev_next,
            )
            if len(all_rows) >= max_rows:
                break
            if self._tr_prev_next != "2":
                break
            prev_next = 2
        return all_rows[:max_rows]

    # -----------------------------------------------------------------------
    # 종목 정보 조회 (TR: opt10001)
    # -----------------------------------------------------------------------

    def get_stock_info(self, code: str) -> dict:
        """
        주식 기본 정보를 조회한다.

        Returns:
            {
                "code": str,
                "name": str,
                "current_price": int,
                "open": int, "high": int, "low": int,
                "volume": int,
                "market_cap": int,
            }
        """
        self._set_input("종목코드", code)
        self._comm_rq(TR_STOCK_INFO, "stock_info", "0101")

        d = self._tr_data
        current_price = safe_int(d.get("현재가"))
        base_price    = safe_int(d.get("기준가"))   # 전일 종가
        change_pct    = (
            round((current_price - base_price) / base_price * 100, 2)
            if base_price else 0.0
        )
        return {
            "code":          code,
            "name":          str(d.get("종목명", "")).strip(),
            "current_price": current_price,
            "base_price":    base_price,
            "change_pct":    change_pct,
            "open":          safe_int(d.get("시가")),
            "high":          safe_int(d.get("고가")),
            "low":           safe_int(d.get("저가")),
            "volume":        safe_int(d.get("거래량")),
            "market_cap":    safe_int(d.get("시가총액")),
        }

    def get_current_price(self, code: str) -> int:
        """현재가만 빠르게 조회 (OCX 메모리, TR 호출 없음)"""
        raw: str = self._ocx.dynamicCall(
            "GetMasterLastPrice(QString)", [code]
        )
        return safe_int(raw)

    # -----------------------------------------------------------------------
    # 분봉 데이터 조회 (TR: opt10080)
    # -----------------------------------------------------------------------

    def get_min_candles(self, code: str, tick_unit: int = 3, count: int = 100) -> list[dict]:
        """
        분봉 캔들 데이터를 조회한다.

        Args:
            code: 종목코드
            tick_unit: 분 단위 (1·3·5·10·15·30·45·60)
            count: 최대 캔들 수

        Returns:
            최신순 정렬된 OHLCV 리스트
            [{"time": "HHmm", "open": int, "high": int,
              "low": int, "close": int, "volume": int}, ...]
        """
        self._set_input("종목코드", code)
        self._set_input("틱범위",   str(tick_unit))
        self._set_input("수정주가구분", "1")
        self._comm_rq(TR_MIN_CANDLE, "min_candle", "0101")

        rows: list[dict] = self._tr_data.get("rows", [])
        return rows[:count]

    # -----------------------------------------------------------------------
    # 계좌 정보 조회
    # -----------------------------------------------------------------------

    def get_balance(self) -> dict:
        """
        예수금 및 주식평가금액 조회.

        Returns:
            {
                "cash": int,          예수금
                "stock_value": int,   주식평가금액
                "total": int,         총평가금액
                "pnl": int,           총손익
                "pnl_pct": float,     수익률(%)
            }
        """
        logger.info("get_balance 호출 — 계좌: '%s'", self._account)
        self._set_input("계좌번호",    self._account)
        self._set_input("비밀번호",    "")
        self._set_input("비밀번호입력매체구분", "00")
        self._set_input("조회구분",    "2")
        self._comm_rq(TR_ACCOUNT, "balance", "2000")

        d = self._tr_data
        cash = safe_int(d.get("예수금"))

        # 총평가금액·총매입금액은 opw00001 필드명이 서버마다 다를 수 있음
        # → 못 가져오면 holdings에서 직접 계산
        total       = safe_int(d.get("총평가금액"))
        invest      = safe_int(d.get("총매입금액"))
        stock_value = safe_int(d.get("유가잔고평가액") or d.get("주식평가금액"))

        if total == 0 or invest == 0:
            # holdings TR로 보완
            holdings = self.get_holdings()
            invest = sum(h["avg_price"] * h["qty"] for h in holdings)
            stock_value = sum(h["current_price"] * h["qty"] for h in holdings)
            total = cash + stock_value

        pnl = total - invest - cash  # 주식 평가손익만
        pnl_pct = (pnl / invest * 100) if invest else 0.0

        return {
            "cash":        cash,
            "stock_value": stock_value,
            "total":       total,
            "pnl":         pnl,
            "pnl_pct":     round(pnl_pct, 2),
        }

    def get_holdings(self) -> list[dict]:
        """
        보유 종목 목록 조회.

        Returns:
            [{"code": str, "name": str, "qty": int,
              "avg_price": int, "current_price": int,
              "pnl": int, "pnl_pct": float}, ...]
        """
        self._set_input("계좌번호",    self._account)
        self._set_input("비밀번호",    "")
        self._set_input("비밀번호입력매체구분", "00")
        self._set_input("조회구분",    "1")
        self._comm_rq(TR_HOLDINGS, "holdings", "2001")

        return self._tr_data.get("rows", [])

    # -----------------------------------------------------------------------
    # 주문
    # -----------------------------------------------------------------------

    def send_order(
        self,
        order_type: int,
        code: str,
        qty: int,
        price: int = 0,
        price_type: str = PriceType.MARKET,
        org_order_no: str = "",
    ) -> str:
        """
        주식 주문을 전송한다.

        Args:
            order_type: OrderType.BUY / SELL / BUY_CANCEL / SELL_CANCEL
            code: 종목코드
            qty: 수량
            price: 가격 (시장가=0)
            price_type: PriceType.MARKET / LIMIT
            org_order_no: 원주문번호 (취소/정정 시)

        Returns:
            주문번호 (str) — 실패 시 ""
        """
        rq_name = f"주문_{code}"
        screen_no = "1000"

        ret = self._ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rq_name, screen_no, self._account,
             order_type, code, qty, price, price_type, org_order_no],
        )

        if ret != ReturnCode.OK:
            logger.error("주문 실패 — code=%s ret=%d", code, ret)
            return ""

        logger.info(
            "주문 전송 — %s %s qty=%d price=%d",
            "매수" if order_type == OrderType.BUY else "매도",
            code, qty, price,
        )
        return rq_name  # 체결 콜백에서 실제 주문번호 확인

    def buy(self, code: str, qty: int, price: int = 0) -> str:
        """시장가 매수 (편의 메서드)"""
        return self.send_order(OrderType.BUY, code, qty, price, PriceType.MARKET)

    def sell(self, code: str, qty: int, price: int = 0) -> str:
        """시장가 매도 (편의 메서드)"""
        return self.send_order(OrderType.SELL, code, qty, price, PriceType.MARKET)

    # -----------------------------------------------------------------------
    # 종목 검색 유틸
    # -----------------------------------------------------------------------

    def get_kospi_codes(self) -> list[str]:
        """코스피 전체 종목코드 반환"""
        return self._get_codes_by_market("0")

    def get_kosdaq_codes(self) -> list[str]:
        """코스닥 전체 종목코드 반환"""
        return self._get_codes_by_market("10")

    def _get_codes_by_market(self, market_id: str) -> list[str]:
        raw: str = self._ocx.dynamicCall(
            "GetCodeListByMarket(QString)", [market_id]
        )
        return [c for c in raw.strip().split(";") if c]

    def get_stock_name(self, code: str) -> str:
        return self._ocx.dynamicCall("GetMasterCodeName(QString)", code).strip()

    # -----------------------------------------------------------------------
    # TR 공통 헬퍼
    # -----------------------------------------------------------------------

    def _set_input(self, key: str, value: str) -> None:
        self._ocx.dynamicCall("SetInputValue(QString, QString)", key, value)

    def _comm_rq(self, tr_code: str, rq_name: str, screen_no: str, prev_next: int = 0) -> None:
        """
        TR 요청 후 응답 이벤트를 QEventLoop으로 대기 (Qt 이벤트 처리 유지).

        호출 전 TRRateLimiter.acquire()로 키움 1초 4회 제한을 자동 준수한다.
        time.sleep(TR_DELAY_SEC) 하드코딩을 제거하고 슬라이딩 윈도우로 대체.
        """
        # 레이트 리미터 — 호출 간격 / 초당 횟수 보장
        self._tr_limiter.acquire()

        self._tr_data = {}
        self._tr_prev_next = ""
        logger.debug("[CommRqData] tr=%s rq=%s prev_next=%d screen=%s", tr_code, rq_name, prev_next, screen_no)

        ret = self._ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rq_name, tr_code, prev_next, screen_no,
        )
        if ret != ReturnCode.OK:
            logger.error("CommRqData 실패 — tr=%s ret=%d", tr_code, ret)
            return

        self._tr_loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._tr_loop.quit)
        timer.start(10_000)
        self._tr_loop.exec_()
        timer.stop()
        self._tr_loop = None

    def _get_comm_data(self, tr_code: str, rq_name: str, index: int, field: str) -> str:
        return self._ocx.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            tr_code, rq_name, index, field,
        ).strip()

    def _get_repeat_cnt(self, tr_code: str, rq_name: str) -> int:
        return self._ocx.dynamicCall(
            "GetRepeatCnt(QString, QString)", tr_code, rq_name
        )

    # -----------------------------------------------------------------------
    # 이벤트 콜백
    # -----------------------------------------------------------------------

    def _on_event_connect(self, err_code: int) -> None:
        """로그인 이벤트 처리 — 연결 상태 추적 + 콜백"""
        if err_code == ReturnCode.OK:
            logger.info("OnEventConnect — 로그인 성공")
            self._is_connected = True
            self._account_list = self._get_account_list()
            if self._account_list:
                self._account = self._account_list[0]
            # 재로그인 콜백 수행
            if self._auto_login_callback:
                try:
                    self._auto_login_callback()
                except Exception as e:
                    logger.warning("재로그인 콜백 실패: %s", e)
        else:
            logger.error("OnEventConnect — 로그인 실패 (err=%d)", err_code)
            self._is_connected = False

    def set_auto_login_callback(self, callback: Callable) -> None:
        """재로그인 성공 시 호출할 콜백 등록"""
        self._auto_login_callback = callback

    def is_connected(self) -> bool:
        """현재 연결 상태 반환"""
        return self._is_connected

    def auto_reconnect(self) -> bool:
        """강제 연결 끊김 감지 시 자동 재로그인"""
        if self._is_connected:
            return True

        logger.warning("연결 끊김 감지 — 자동 재로그인 시도")
        self._ocx.dynamicCall("CommConnect()")
        return False

    def _on_receive_tr_data(
        self,
        screen_no: str,
        rq_name: str,
        tr_code: str,
        record_name: str,
        prev_next: str,
        _data_len: int,
        _err_code: str,
        _msg: str,
        _spl_msg: str,
    ) -> None:
        self._tr_prev_next = str(prev_next).strip()
        logger.debug("[TR 수신] rq=%s tr=%s prev_next=%s screen=%s", rq_name, tr_code, self._tr_prev_next, screen_no)

        if rq_name == "stock_info":
            self._tr_data = self._parse_single(tr_code, rq_name, [
                "종목명", "현재가", "기준가", "시가", "고가", "저가", "거래량", "시가총액",
            ])

        elif rq_name == "min_candle":
            self._tr_data = {"rows": self._parse_candle_rows(tr_code, rq_name)}

        elif rq_name == "balance":
            self._tr_data = self._parse_single(tr_code, rq_name, [
                "예수금", "유가잔고평가액", "주식평가금액", "총평가금액", "총매입금액",
            ])

        elif rq_name == "holdings":
            self._tr_data = {"rows": self._parse_holdings_rows(tr_code, rq_name)}

        elif rq_name == "거래대금상위":
            rows = self._parse_top_volume_rows(tr_code, rq_name)
            logger.debug("[opt10030] 파싱 결과: %d행, prev_next=%s", len(rows), self._tr_prev_next)
            self._tr_data = {"rows": rows}

        else:
            logger.debug("[TR 수신] 알 수 없는 rq_name=%s — 처리 스킵", rq_name)

        if self._tr_loop and self._tr_loop.isRunning():
            self._tr_loop.quit()

    def _on_receive_msg(self, _screen: str, rq_name: str, tr_code: str, msg: str) -> None:
        logger.info("[MSG] rq=%s tr=%s → %s", rq_name, tr_code, msg)

    def _on_receive_chejan_data(self, gubun: str, item_cnt: int, fid_list: str) -> None:
        """체결/잔고 이벤트 — 필요 시 확장"""
        logger.debug("체결잔고 수신 — gubun=%s", gubun)

    # -----------------------------------------------------------------------
    # TR 데이터 파서
    # -----------------------------------------------------------------------

    def _parse_single(self, tr_code: str, rq_name: str, fields: list[str]) -> dict:
        result = {}
        for f in fields:
            val = self._get_comm_data(tr_code, rq_name, 0, f)
            result[f] = val
            if not val:
                logger.warning("필드 미획득: tr=%s rq=%s field=%s", tr_code, rq_name, f)
        return result

    def _parse_candle_rows(self, tr_code: str, rq_name: str) -> list[dict]:
        cnt = self._get_repeat_cnt(tr_code, rq_name)
        rows = []
        for i in range(cnt):
            def g(f, _i=i):
                return self._get_comm_data(tr_code, rq_name, _i, f)
            rows.append({
                "time":   g("체결시간").strip(),
                "open":   safe_int(g("시가")),
                "high":   safe_int(g("고가")),
                "low":    safe_int(g("저가")),
                "close":  safe_int(g("현재가")),
                "volume": safe_int(g("거래량")),
            })
        return rows

    # opt10030 거래대금 필드명 후보 — 서버 버전·모의투자 여부에 따라 다를 수 있음
    _OPT10030_AMT_FIELDS = ("거래대금", "누적거래대금", "거래금액", "거래대금(천원)")

    def _parse_top_volume_rows(self, tr_code: str, rq_name: str) -> list[dict]:
        """opt10030 거래량/거래대금 상위 종목 행 파싱"""
        cnt = self._get_repeat_cnt(tr_code, rq_name)
        logger.debug("[opt10030] GetRepeatCnt=%d", cnt)
        rows = []

        # 첫 행에서 거래대금 실제 필드명을 자동 감지
        _amt_field: str = "거래대금"
        if cnt > 0:
            def _g0(f):
                return self._get_comm_data(tr_code, rq_name, 0, f)
            for _cand in self._OPT10030_AMT_FIELDS:
                _val = _g0(_cand).strip()
                if _val and _val != "0":
                    _amt_field = _cand
                    break
            logger.info("[opt10030] 거래대금 필드 감지: '%s' (raw='%s')",
                        _amt_field, _g0(_amt_field))

        for i in range(cnt):
            def g(f, _i=i):
                return self._get_comm_data(tr_code, rq_name, _i, f)
            code = g("종목코드").strip().lstrip("A")
            if not code:
                logger.debug("[opt10030] 행[%d] 종목코드 없음 — 스킵", i)
                continue

            raw_amt = g(_amt_field)
            # 거래대금이 여전히 비어 있으면 현재가×거래량으로 근사
            amt_val = safe_int(raw_amt)
            if amt_val == 0:
                price_v  = safe_int(g("현재가"))
                volume_v = safe_int(g("거래량"))
                # 거래량 overflow 값(0xFFFFFFFF) 방어
                if volume_v < 2_000_000_000:
                    amt_val = price_v * volume_v

            if i < 3:
                logger.info("[opt10030 진단] 행[%d] code=%s 거래대금raw='%s'→%d 거래량raw='%s'",
                            i, code, raw_amt, amt_val, g("거래량"))
            rows.append({
                "code":          code,
                "name":          g("종목명").strip(),
                "current_price": safe_int(g("현재가")),
                "open_price":    safe_int(g("시가")),
                "high_price":    safe_int(g("고가")),
                "low_price":     safe_int(g("저가")),
                "volume":        safe_int(g("거래량")) if safe_int(g("거래량")) < 2_000_000_000 else 0,
                "trade_amount":  amt_val,
                "prev_close":    safe_int(g("전일종가")),
                "change_pct":    safe_float(g("등락률")),
                "rank":          i + 1,
            })
        logger.debug("[opt10030] 유효 행: %d/%d", len(rows), cnt)
        return rows

    def _parse_holdings_rows(self, tr_code: str, rq_name: str) -> list[dict]:
        cnt = self._get_repeat_cnt(tr_code, rq_name)
        rows = []
        for i in range(cnt):
            def g(f, _i=i):
                return self._get_comm_data(tr_code, rq_name, _i, f)
            avg  = safe_int(g("매입가")) or safe_int(g("평균단가")) or safe_int(g("매입단가"))
            curr = safe_int(g("현재가"))
            qty  = safe_int(g("보유수량"))
            pnl  = (curr - avg) * qty
            pnl_pct = (pnl / (avg * qty) * 100) if avg and qty else 0.0
            rows.append({
                "code":          g("종목번호").strip().lstrip("A"),
                "name":          g("종목명").strip(),
                "qty":           qty,
                "avg_price":     avg,
                "current_price": curr,
                "pnl":           pnl,
                "pnl_pct":       round(pnl_pct, 2),
            })
        return rows


# ---------------------------------------------------------------------------
# 실행 예시
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    app = QApplication(sys.argv)
    kiwoom = KiwoomManager()

    if not kiwoom.login():
        sys.exit(1)

    # 종목 정보
    info = kiwoom.get_stock_info("005930")
    print("=== 삼성전자 기본 정보 ===")
    for k, v in info.items():
        print(f"  {k}: {v}")

    # 분봉
    candles = kiwoom.get_min_candles("005930", tick_unit=3, count=5)
    print("\n=== 삼성전자 3분봉 (최근 5개) ===")
    for c in candles:
        print(f"  {c}")

    # 잔고
    balance = kiwoom.get_balance()
    print("\n=== 계좌 잔고 ===")
    for k, v in balance.items():
        print(f"  {k}: {v}")

    app.exec_()


# ---------------------------------------------------------------------------
# MockKiwoomManager — OCX 없이 UI 테스트용
# ---------------------------------------------------------------------------

class _MockOcx:
    """QAxWidget 없이 시그널 연결을 흉내내는 더미 객체"""

    class _Sig:
        def __init__(self):
            self._cbs: list = []
        def connect(self, cb):
            self._cbs.append(cb)
        def disconnect(self, cb=None):
            if cb and cb in self._cbs:
                self._cbs.remove(cb)
        def emit(self, *args):
            for cb in self._cbs:
                try:
                    cb(*args)
                except Exception:
                    pass

    def __init__(self):
        self.OnEventConnect      = _MockOcx._Sig()
        self.OnReceiveTrData     = _MockOcx._Sig()
        self.OnReceiveMsg        = _MockOcx._Sig()
        self.OnReceiveChejanData = _MockOcx._Sig()
        self.OnReceiveRealData   = _MockOcx._Sig()

    def dynamicCall(self, method: str, args=None):
        args = args or []
        if "CommConnect" in method:
            # 즉시 로그인 성공 시뮬레이션 (err_code=0)
            self.OnEventConnect.emit(0)
        elif "GetLoginInfo" in method:
            param = args[0] if args else ""
            if "ACCNO" in param:
                return "MOCK-0000000;"
            if "USER_ID" in param:
                return "mock_user"
            if "GetServerGubun" in param:
                return "1"   # 1 = 모의투자
        return ""


class MockKiwoomManager:
    """
    키움 OCX 없이 UI 레이아웃만 확인할 때 사용하는 스텁.
    실제 매매는 불가하며 모든 API 호출은 빈 값을 반환한다.
    """
    def __init__(self) -> None:
        self._ocx = _MockOcx()
        self._account_list: list[str] = ["MOCK-0000000"]
        self._account: str = "MOCK-0000000"
        self._tr_prev_next: str = ""
        self._is_connected: bool = True

    def login(self) -> bool:
        return True

    def is_connected(self) -> bool:
        return self._is_connected

    def _set_input(self, key: str, value: str) -> None:
        pass

    def _comm_rq(self, tr_code: str, rq_name: str, screen_no: str, prev_next: int = 0) -> None:
        self._tr_prev_next = ""
        self._tr_data: dict = {"rows": []}

    def get_stock_info(self, code: str) -> dict:
        return {"name": "Mock종목", "current_price": 0, "change_pct": 0.0}

    def fetch_opt10030_top_volume(self, max_rows: int = 200) -> list:
        """OCX 없음 — 빈 리스트 (UI 테스트용)"""
        return []

    def get_current_price(self, code: str) -> int:
        return 0

    def get_min_candles(self, code: str, tick_unit: int = 1, count: int = 60) -> list:
        return []

    def get_balance(self) -> dict:
        return {"cash": 0, "total_eval": 0, "total_buy": 0, "pnl": 0, "pnl_pct": 0.0}

    def get_holdings(self) -> list:
        return []

    def send_order(self, *a, **kw) -> int:
        return -1

    def buy(self, *a, **kw) -> int:
        return -1

    def sell(self, *a, **kw) -> int:
        return -1
