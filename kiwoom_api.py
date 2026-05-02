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
from datetime import date
from typing import Callable, Optional

try:
    from PyQt5.QAxContainer import QAxWidget
    _OCX_AVAILABLE = True
except Exception:
    _OCX_AVAILABLE = False
    QAxWidget = None  # type: ignore

from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication

from infra.kiwoom_protocol import KiwoomProtocol
from logging_config import tr_log

logger = logging.getLogger(__name__)


class _TrFailCounter:
    """TR 실패를 TR 코드별로 집계하고 일정 횟수 이상 누적 시 tr_log에 경고를 출력한다."""

    _WARN_THRESHOLD = 3   # 연속 N회 실패 시 경고
    _REPORT_EVERY   = 10  # N회마다 집계 로그

    def __init__(self) -> None:
        self._counts: dict[str, int] = collections.defaultdict(int)
        self._total:  dict[str, int] = collections.defaultdict(int)

    def fail(self, tr_code: str, detail: str = "") -> None:
        self._counts[tr_code] += 1
        self._total[tr_code]  += 1
        cnt = self._counts[tr_code]
        tot = self._total[tr_code]
        if cnt >= self._WARN_THRESHOLD:
            tr_log.warning(
                "[TR실패] %s 연속=%d회 누적=%d회%s",
                tr_code, cnt, tot,
                f" — {detail}" if detail else "",
            )
        elif tot % self._REPORT_EVERY == 0:
            tr_log.info("[TR집계] %s 누적실패=%d회", tr_code, tot)

    def ok(self, tr_code: str) -> None:
        """성공 시 연속 실패 카운터 리셋."""
        if self._counts.get(tr_code, 0) > 0:
            tr_log.info(
                "[TR회복] %s 연속실패 %d회 → 응답 정상화",
                tr_code, self._counts[tr_code],
            )
        self._counts[tr_code] = 0


_TR_FAIL = _TrFailCounter()


# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

from order.order_types import OrderType, PriceType


class ReturnCode:
    OK             =  0
    COMM_FAIL      = -100
    LOGIN_FAIL     = -101
    LOGIN_TIMEOUT  = -102

# TR 코드
TR_STOCK_INFO  = "opt10001"   # 주식기본정보요청
TR_ACCOUNT     = "opw00001"   # 예수금상세현황요청
TR_HOLDINGS    = "opw00018"   # 계좌평가잔고내역요청
TR_MIN_CANDLE  = "opt10080"   # 주식분봉차트조회요청
TR_DAILY_CANDLE = "opt10081"  # 주식일봉차트조회요청
TR_DAILY_REALIZED = "opt10074"  # 일자별실현손익요청 (당일 누적 실현손익)
TR_INDEX_INFO     = "opt20001"  # 업종현재가요청 (지수 조회 — 코스피:"001", 코스닥:"101")
TR_INVESTOR       = "opt10059"  # 종목별투자자기관별요청 (외국인/기관 순매수)

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

    acquire()는 메인 스레드에서만 호출 (_comm_rq 전용).
    대기 구간에서 QApplication.processEvents()를 호출해 UI 블로킹을 방지한다.
    호출 전 _tr_busy=True 가 이미 설정되어 있으므로 processEvents 중 재진입 없음.
    """
    MAX_PER_SEC  = 4      # 1초당 최대 호출 횟수 (5→4, 안전 마진)
    MIN_INTERVAL = 0.25   # 연속 호출 최소 간격 (초)

    def __init__(self) -> None:
        self._timestamps: collections.deque = collections.deque()

    @staticmethod
    def _nonblocking_wait(wait_sec: float) -> None:
        """TR 속도 제한 대기. processEvents로 Qt 이벤트(Watchdog ACK 등) 허용.
        _tr_busy + _scan_in_progress 보호가 추가된 이후 cascade 위험 없음.
        (이전 '금지' 주석은 이 보호 추가 전 _load_candles_async 경로 기준이었음)
        최대 대기: MIN_INTERVAL=0.25s 또는 슬라이딩 윈도우=~1s."""
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QEventLoop
        QApplication.processEvents(QEventLoop.AllEvents, max(1, int(wait_sec * 1000)))

    def acquire(self) -> None:
        """TR 호출 전 반드시 호출. 필요 시 비블로킹 대기 후 반환한다."""
        now = time.monotonic()

        # 1. 최소 간격 보장
        if self._timestamps:
            wait = self.MIN_INTERVAL - (now - self._timestamps[-1])
            if wait > 0:
                self._nonblocking_wait(wait)
                now = time.monotonic()

        # 2. 슬라이딩 윈도우(1초) 내 호출 수 확인
        cutoff = now - 1.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.MAX_PER_SEC:
            wait = (self._timestamps[0] + 1.0) - now
            if wait > 0:
                self._nonblocking_wait(wait)
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


def _resolve_prev_close(prev_close: int, current_price: int, change_pct: float) -> int:
    """
    opt10030 전일종가가 0일 때 현재가+등락률로 역산 (2026-04-04).

    Args:
        prev_close: opt10030 전일종가 필드 값
        current_price: opt10030 현재가 필드 값
        change_pct: opt10030 등락률 필드 값

    Returns:
        계산된 또는 원본 전일종가 (int)

    처리 로직:
      1. prev_close > 0 → 원본 그대로 반환
      2. current_price > 0 and change_pct != 0 → current_price / (1 + change_pct/100) 역산
      3. change_pct == 0 (보합) → current_price 사용
      4. 모두 0 → 0 반환
    """
    if prev_close > 0:
        return prev_close
    if current_price > 0 and change_pct != 0.0:
        return max(1, int(current_price / (1.0 + change_pct / 100.0)))
    return current_price if current_price > 0 else 0


# ---------------------------------------------------------------------------
# KiwoomManager
# ---------------------------------------------------------------------------

class KiwoomManager(KiwoomProtocol):
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
        self._tr_busy: bool = False        # 재진입 방지 플래그
        self._tr_current_rq: str = ""      # 현재 처리 중인 rq_name (디버그용)

        self._account_list: list[str] = []
        self._account: str = ""

        # 자동 재로그인 관련
        self._is_connected: bool = False
        self._auto_login_callback: Optional[callable] = None  # 재로그인 성공 시 콜백
        self._tr_prev_next: str = ""  # 연속조회 여부 ("2" = 다음 데이터 있음)

        # TR 레이트 리미터 — 1초 4회 / 0.25초 간격 보장
        self._tr_limiter = TRRateLimiter()

        # OnReceiveMsg 외부 콜백 — OrderManager가 [800033] 등 주문 에러를 처리하기 위해 사용
        self._on_order_msg_cb: Optional[callable] = None

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

    def fetch_opt10030_top_volume(self, max_rows: int = 400) -> list[dict]:
        """
        opt10030 거래대금 상위 종목. 한 페이지(최대 약 100행)를 넘기면 연속조회로 이어 받는다.

        키움: prev_next 콜백이 '2'이면 다음 페이지가 있음 → CommRqData(..., prev_next=2).
        max_rows=400 기준 약 4페이지(TR 4회) 소요. 각 TR 간 0.25s 딜레이 포함.
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

    def get_stock_info(self, code: str) -> Optional[dict]:
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
            또는 조회 실패 시 None
        """
        # [NEW] 재시도 로직 추가 (2026-04-03)
        for retry in range(2):
            self._set_input("종목코드", code)
            self._set_input("수정주가구분", "1")
            self._comm_rq(TR_STOCK_INFO, "stock_info", "0101")

            d = self._tr_data
            if d.get("현재가"):  # 유효한 응답
                current_price = safe_int(d.get("현재가"))
                base_price    = safe_int(d.get("기준가"))   # 전일 종가
                change_pct    = (
                    round((current_price - base_price) / base_price * 100, 2)
                    if base_price else 0.0
                )
                _TR_FAIL.ok("opt10001")
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
                    "sector":        str(d.get("업종명", "")).strip(),
                }

            # 첫 시도 실패 → 0.3초 비블로킹 대기 후 재시도
            if retry == 0:
                self._tr_limiter._nonblocking_wait(0.3)

        # 재시도도 실패
        logger.warning("[opt10001] %s 응답 없음 — 스냅샷 폴백", code)
        _TR_FAIL.fail("opt10001", f"code={code}")
        return None

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
    # 일봉 데이터 조회 (TR: opt10081)
    # -----------------------------------------------------------------------

    def get_daily_candles(self, code: str, count: int = 25) -> list[dict]:
        """
        일봉 캔들 데이터를 조회한다.

        Args:
            code: 종목코드
            count: 최대 일봉 수

        Returns:
            최신순 정렬된 OHLCV 리스트
            [{"date": "YYYYMMDD", "open": int, "high": int,
              "low": int, "close": int, "volume": int}, ...]
        """
        self._set_input("종목코드", code)
        self._set_input("기준일자", "")   # 오늘 기준 최근 N개
        self._set_input("수정주가구분", "1")
        # 타임아웃 1초 — 일봉 체인 10종목 × 최대 1s = 최대 10s (기본 2s × 10 = 20s → watchdog 초과 방지)
        self._comm_rq(TR_DAILY_CANDLE, "daily_candle", "0101", timeout_ms=1_000)

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
        if not self._account:
            logger.warning("get_balance 계좌번호 없음 — 스킵")
            return {}   # sync_balance가 if not balance: 로 스킵
        self._set_input("계좌번호",    self._account)
        self._set_input("비밀번호",    "")
        self._set_input("비밀번호입력매체구분", "00")
        self._set_input("조회구분",    "2")
        # opw00001은 서버 부하 시 4~6초 소요 — 타임아웃 6s→3s (2026-04-27: 스캔 충돌 해소)
        # 3초 이내 응답 안 오면 빠른 포기 → _tr_busy 점유 시간 단축 → scan TR 충돌 최소화
        ok = self._comm_rq(TR_ACCOUNT, "balance", "2000", timeout_ms=3_000)
        if not ok:
            logger.warning("get_balance TR 차단됨 — 잔고 동기화 스킵 (기존값 유지)")
            return {}   # 빈 dict → sync_balance가 if not balance: 로 스킵, 기존 self.cash 유지

        d = self._tr_data
        # 타임아웃으로 _tr_data가 비어 있으면(응답 미도착) 스킵 — 0원으로 덮어쓰기 방지
        if not d.get("예수금"):
            logger.warning("get_balance 응답 없음 또는 예수금 필드 비어 있음 — 스킵 (서버 응답 지연)")
            return {}
        cash = safe_int(d.get("예수금"))

        # 총평가금액·총매입금액은 opw00001 필드명이 서버마다 다를 수 있음
        # → 못 가져오면 holdings에서 직접 계산
        total       = safe_int(d.get("총평가금액"))
        invest      = safe_int(d.get("총매입금액"))
        stock_value = safe_int(d.get("유가잔고평가액") or d.get("주식평가금액"))

        # total/invest가 0이어도 holdings TR 추가 호출 금지 — _tr_busy 연장으로 8s 프리징 유발
        # → 값이 없으면 cash만으로 대체 (다음 30s 갱신 시 정상화됨)
        if total == 0:
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

    def get_today_realized_pnl(self) -> int | None:
        """
        계좌 기준 당일 실현손익(원). opt10074 싱글데이터 '실현손익'.

        TR 실패·미지원(모의 등) 시 None — 호출측에서 기존 로컬 집계만 사용.
        """
        if not getattr(self, "_account", ""):
            return None
        ds = date.today().strftime("%Y%m%d")
        self._set_input("계좌번호", self._account)
        self._set_input("비밀번호", "")
        self._set_input("비밀번호입력매체구분", "00")
        self._set_input("시작일자", ds)
        self._set_input("종료일자", ds)
        ok = self._comm_rq(TR_DAILY_REALIZED, "daily_realized", "2002")
        if not ok:
            logger.warning("opt10074 TR 차단됨 — 당일 실현손익 동기화 생략")
            return None
        d = self._tr_data
        if not d:
            logger.warning("opt10074 응답 없음 — 당일 실현손익 동기화 생략")
            return None
        raw = d.get("실현손익", "")
        if raw is None or (isinstance(raw, str) and not str(raw).strip()):
            logger.debug("opt10074 실현손익 필드 비어 있음 — 0으로 간주")
            return 0
        # 실현손익은 음수 가능 → safe_int(절댓값) 쓰지 않음
        return int(safe_float(raw, 0.0))

    def get_investor_trend(self, code: str) -> dict:
        """
        종목별 당일 외국인/기관 순매수 수량 조회 — opt10059.

        입력: 종목코드, 금융투자구분(1=수량)
        반환: {"foreign_net": int, "inst_net": int}
          - 양수: 순매수, 음수: 순매도
          - 조회 실패 시 {"foreign_net": 0, "inst_net": 0}
        """
        _FALLBACK = {"foreign_net": 0, "inst_net": 0}
        try:
            self._set_input("일자",       "")        # 당일은 빈 문자열
            self._set_input("종목코드",   code)
            self._set_input("금융투자구분", "1")     # 1=수량
            self._comm_rq(TR_INVESTOR, f"investor_{code}", "9500")

            d = self._tr_data
            if not d:
                return _FALLBACK

            # opt10059 멀티데이터: rows 리스트에서 오늘(첫 번째 행) 추출
            rows = d.get("rows", [])
            if not rows:
                return _FALLBACK

            row = rows[0]   # 가장 최신 날짜(오늘)
            foreign_net = safe_int(row.get("외국인순매수", row.get("외국계순매수", "0")))
            # 기관 = 기관계 (투신+보험+은행+기타기관 합산)
            inst_net    = safe_int(row.get("기관계순매수", row.get("기관순매수", "0")))

            logger.debug(
                "[opt10059] %s 외국인=%+d 기관=%+d",
                code, foreign_net, inst_net,
            )
            return {"foreign_net": foreign_net, "inst_net": inst_net}

        except Exception as e:
            logger.debug("[opt10059] %s 조회 실패: %s", code, e)
            return _FALLBACK

    def get_index_info(self, index_code: str) -> Optional[dict]:
        """
        업종(지수) 현재가 조회 — opt20001.

        Args:
            index_code: "001" = 코스피, "101" = 코스닥

        Returns:
            {"index_code": str, "current": float, "base": float, "change_pct": float}
            조회 실패 시 None
        """
        self._set_input("업종코드", index_code)
        ok = self._comm_rq(TR_INDEX_INFO, "index_info", "9300", timeout_ms=1_000)
        if not ok:
            logger.debug("[opt20001] TR 차단 — 지수 조회 스킵 (code=%s)", index_code)
            _TR_FAIL.fail("opt20001", f"TR차단 code={index_code}")
            return None

        d = self._tr_data
        raw_current = d.get("현재가", "")
        raw_base    = d.get("기준가", "")

        logger.debug("[opt20001] 응답 raw — code=%s 현재가=%r 기준가=%r",
                     index_code, raw_current, raw_base)

        # ── 단일 필드로 값이 온 경우 (표준 응답) ─────────────────────────
        if raw_current:
            raw_c = safe_int(raw_current)
            raw_b = safe_int(raw_base)
            # opt20001은 지수를 소수점 2자리 정수화하여 전송할 수 있음
            # 코스피/코스닥 통상 1000~3000 범위. 10000 초과면 ×100 보정
            current = raw_c / 100.0 if raw_c > 10_000 else float(raw_c)
            base    = raw_b / 100.0 if raw_b > 10_000 else float(raw_b)
            change_pct = round((current - base) / base * 100, 2) if base else 0.0
            logger.info("[opt20001] 지수 — code=%s 현재=%.2f 기준=%.2f 등락=%.2f%%",
                        index_code, current, base, change_pct)
            _TR_FAIL.ok("opt20001")
            return {"index_code": index_code, "current": current,
                    "base": base, "change_pct": change_pct}

        # ── rows 형태 응답 — 분봉 데이터에서 현재가/기준가 추출 ────────────
        # rows[0] = 최신 분봉 (오늘), rows 이후 = 전일 이전 데이터 (최신순 정렬)
        rows = d.get("rows", [])
        if not rows:
            logger.warning("[opt20001] 지수 응답 없음 — code=%s", index_code)
            _TR_FAIL.fail("opt20001", f"응답없음 code={index_code}")
            return None

        today_str = datetime.now().strftime("%Y%m%d")
        today_rows = [r for r in rows if str(r.get("time", "")).startswith(today_str)]
        prev_rows  = [r for r in rows if not str(r.get("time", "")).startswith(today_str)]

        if not today_rows:
            logger.warning("[opt20001] 오늘 분봉 없음 — code=%s (rows=%d)", index_code, len(rows))
            return None

        # 최신 분봉 close = 현재가, 전일 가장 최근 close = 기준가
        current_raw = today_rows[0].get("close", 0)
        base_raw    = prev_rows[0].get("close", 0) if prev_rows else 0

        current = float(current_raw) / 100.0 if current_raw > 10_000 else float(current_raw)
        base    = float(base_raw)    / 100.0 if base_raw    > 10_000 else float(base_raw)
        change_pct = round((current - base) / base * 100, 2) if base else 0.0

        logger.info("[opt20001] 지수(rows) — code=%s 현재=%.2f 기준=%.2f 등락=%.2f%%",
                    index_code, current, base, change_pct)
        return {"index_code": index_code, "current": current,
                "base": base, "change_pct": change_pct}

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
        # timeout_ms 명시 — 기본값 2s 유지, Part 3의 2-step 분리 패턴과 함께 동작
        ok = self._comm_rq(TR_HOLDINGS, "holdings", "2001", timeout_ms=2_000)
        if not ok:
            logger.warning("get_holdings TR 차단됨 — 보유잔고 조회 생략")
            return []

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

    def _comm_rq(
        self,
        tr_code:    str,
        rq_name:    str,
        screen_no:  str,
        prev_next:  int = 0,
        timeout_ms: int = 2_000,
    ) -> bool:
        """
        TR 요청 후 응답 이벤트를 QEventLoop으로 대기 (Qt 이벤트 처리 유지).

        호출 전 TRRateLimiter.acquire()로 키움 1초 4회 제한을 자동 준수한다.
        time.sleep(TR_DELAY_SEC) 하드코딩을 제거하고 슬라이딩 윈도우로 대체.

        재진입(reentrant) 방지: QEventLoop.exec_() 중 다른 타이머 콜백이 TR을 다시
        호출하면 self._tr_loop를 덮어써서 교착 상태가 발생한다.
        _tr_busy 플래그로 중첩 호출을 차단한다.

        반환값: True = 요청이 정상 실행됨, False = 재진입 차단 또는 CommRqData 실패
        호출자는 False 반환 시 self._tr_data를 읽으면 안 됨 (stale 데이터 위험).
        """
        # ── 재진입 방지 ──────────────────────────────────────────────────────
        # _tr_busy 확인 먼저 — 차단 시 _tr_data를 건드리지 않음
        # (외부 호출의 exec_() 복귀 직전에 _tr_data를 초기화하면 레이스 발생)
        if self._tr_busy:
            logger.warning(
                "[TR] 재진입 차단 — %s 요청 거부 (현재 '%s' 처리 중)",
                rq_name, self._tr_current_rq,
            )
            return False

        # 새 요청 시작 전에만 초기화
        self._tr_data = {}
        self._tr_prev_next = ""

        # ★ acquire() 전에 busy 플래그 설정 —
        #   _wait()의 QEventLoop 도중 다른 타이머가 재진입하는 것을 차단
        self._tr_busy = True
        self._tr_current_rq = rq_name
        try:
            # 레이트 리미터 — UI 비블로킹 대기 (QEventLoop 사용)
            self._tr_limiter.acquire()
            logger.debug("[CommRqData] tr=%s rq=%s prev_next=%d screen=%s", tr_code, rq_name, prev_next, screen_no)

            ret = self._ocx.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                rq_name, tr_code, prev_next, screen_no,
            )
            if ret != ReturnCode.OK:
                logger.error("CommRqData 실패 — tr=%s ret=%d", tr_code, ret)
                return False
            self._tr_loop = QEventLoop()
            # QTimer를 _tr_loop 자식으로 생성 → Python GC 수집 방지 + Qt 수명 관리
            # timer = QTimer() 로컬 변수는 exec_() 대기 중 GC 수집 위험 → timeout 무효화 가능
            self._tr_timeout_timer = QTimer(self._tr_loop)
            self._tr_timeout_timer.setSingleShot(True)
            self._tr_timeout_timer.timeout.connect(self._tr_loop.quit)
            self._tr_timeout_timer.start(timeout_ms)
            self._tr_loop.exec_()
            self._tr_timeout_timer.stop()
            self._tr_timeout_timer = None
            self._tr_loop = None
        finally:
            self._tr_busy = False
            self._tr_current_rq = ""
        return True

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

    def force_unfreeze(self) -> None:
        """Watchdog freeze 감지 시 daemon thread에서 호출 — 막힌 TR EventLoop 강제 해제.

        QEventLoop.quit()은 Qt 내부적으로 thread-safe (QCoreApplication::postEvent 경유).
        _tr_busy=False 설정은 CPython GIL로 원자적.
        이후 _comm_rq finally 블록이 정상적으로 _tr_busy=False를 재설정함.
        """
        logger.warning(
            "[force_unfreeze] 프리징 복구 시도 — tr_busy=%s, rq='%s'",
            self._tr_busy, self._tr_current_rq,
        )
        # ① 현재 처리 중인 TR EventLoop 강제 종료 (가장 중요)
        tr_loop = self._tr_loop
        if tr_loop is not None:
            try:
                tr_loop.quit()   # thread-safe: Qt가 메인 스레드에 quit 이벤트 포스팅
                logger.warning("[force_unfreeze] _tr_loop.quit() 전송 완료")
            except Exception as e:
                logger.warning("[force_unfreeze] _tr_loop.quit() 실패: %s", e)

        # ② busy 플래그 강제 해제 — _comm_rq finally가 다시 False로 설정하므로 중복 해제 무해
        self._tr_busy = False
        self._tr_current_rq = ""
        logger.warning("[force_unfreeze] _tr_busy 강제 해제 완료")

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

        elif rq_name == "daily_candle":
            self._tr_data = {"rows": self._parse_daily_candle_rows(tr_code, rq_name)}

        elif rq_name == "balance":
            self._tr_data = self._parse_single(tr_code, rq_name, [
                "예수금", "유가잔고평가액", "주식평가금액", "총평가금액", "총매입금액",
            ])

        elif rq_name == "daily_realized":
            # 싱글: 총매수금액, 총매도금액, 실현손익, 매매수수료, 매매세금
            self._tr_data = {}
            for f in (
                "총매수금액",
                "총매도금액",
                "실현손익",
                "매매수수료",
                "매매세금",
            ):
                self._tr_data[f] = self._get_comm_data(tr_code, rq_name, 0, f)

        elif rq_name == "index_info":
            self._tr_data = self._parse_single(tr_code, rq_name, [
                "현재가", "기준가", "대비", "등락률",
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
        if self._on_order_msg_cb:
            try:
                self._on_order_msg_cb(rq_name, msg)
            except Exception as _e:
                logger.debug("[MSG 콜백 오류] %s", _e)

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
                logger.debug("필드 미획득: tr=%s rq=%s field=%s (무시)", tr_code, rq_name, f)
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
        rows.reverse()  # [최신->과거] 를 [과거->최신] 으로 변경 (IndicatorService 표준)
        return rows

    def _parse_daily_candle_rows(self, tr_code: str, rq_name: str) -> list[dict]:
        """opt10081 일봉 차트 데이터 파싱"""
        cnt = self._get_repeat_cnt(tr_code, rq_name)
        rows = []
        for i in range(cnt):
            def g(f, _i=i):
                return self._get_comm_data(tr_code, rq_name, _i, f)
            close_price = safe_int(g("현재가"))
            if close_price <= 0:
                continue
            rows.append({
                "date":   g("일자").strip(),
                "open":   safe_int(g("시가")),
                "high":   safe_int(g("고가")),
                "low":    safe_int(g("저가")),
                "close":  close_price,
                "volume": safe_int(g("거래량")),
            })
        rows.reverse()  # [최신->과거] 를 [과거->최신] 으로 변경 (IndicatorService 표준)
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
            logger.debug("[opt10030] 거래대금 필드 감지: '%s' (raw='%s')",
                        _amt_field, _g0(_amt_field))

        for i in range(cnt):
            def g(f, _i=i):
                return self._get_comm_data(tr_code, rq_name, _i, f)
            code = g("종목코드").strip().lstrip("A")
            if not code:
                logger.debug("[opt10030] 행[%d] 종목코드 없음 — 스킵", i)
                continue

            raw_amt = g(_amt_field)
            # 거래대금 단위 처리: opt10030 모든 거래대금 필드는 '백만 원' 단위
            amt_val = safe_int(raw_amt)
            # opt10030 거래대금: 백만원 → 원으로 변환 (×1,000,000)
            # 예: 487 → 487,000,000원 (4억 8,700만 원)
            # 필드명: "거래대금", "누적거래대금", "거래금액" 등
            if amt_val > 0:
                amt_val *= 1_000_000  # 백만 원 → 원 변환
            if amt_val == 0:
                price_v  = safe_int(g("현재가"))
                volume_v = safe_int(g("거래량"))
                # 거래량 overflow 값(0xFFFFFFFF) 방어
                if volume_v < 2_000_000_000:
                    amt_val = price_v * volume_v

            if i < 5:
                # 거래대금 단위 진단 (천원→원 변환 확인용) — DEBUG 레벨로 유지
                from scanner.smart_scanner import format_trade_amount_korean
                amt_korean = format_trade_amount_korean(amt_val) if amt_val > 0 else "0원"
                logger.debug("[opt10030 진단] 행[%d] %s(%s) 거래대금: raw='%s' → %d원 ≈ %s",
                             i, code, g("종목명").strip(), raw_amt, amt_val, amt_korean)
            rows.append({
                "code":          code,
                "name":          g("종목명").strip(),
                "current_price": safe_int(g("현재가")),
                "open_price":    safe_int(g("시가")),
                "high_price":    safe_int(g("고가")),
                "low_price":     safe_int(g("저가")),
                "volume":        safe_int(g("거래량")) if safe_int(g("거래량")) < 2_000_000_000 else 0,
                "trade_amount":  amt_val,
                "prev_close":    _resolve_prev_close(
                                     safe_int(g("전일종가")),
                                     safe_int(g("현재가")),
                                     safe_float(g("등락률")),
                                 ),
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

    def _comm_rq(self, tr_code: str, rq_name: str, screen_no: str, prev_next: int = 0) -> bool:
        self._tr_prev_next = ""
        self._tr_data: dict = {"rows": []}
        return True

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

    def get_today_realized_pnl(self):
        return None

    def get_index_info(self, _index_code: str) -> Optional[dict]:
        return None

    def get_investor_trend(self, _code: str) -> dict:
        return {"foreign_net": 0, "inst_net": 0}

    def send_order(self, *a, **kw) -> int:
        return -1

    def buy(self, *a, **kw) -> int:
        return -1

    def sell(self, *a, **kw) -> int:
        return -1
