"""
NewsAnalyzer — 백그라운드 뉴스 분석 스레드

신호 종목에 대해 네이버 뉴스를 조회하고 긍부정 감성을 분류한다.
메인 스레드를 블로킹하지 않도록 별도 daemon thread에서 동작한다.

흐름:
  1. _on_scan_signal 에서 analyzer.analyze(code, name) 호출 → 즉시 반환
  2. 내부 큐에 적재 → 백그라운드 스레드가 순차 처리
  3. 분석 완료 시 on_result 콜백 호출 (큐 → QTimer 드레인으로 메인 스레드 전달)
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 결과 데이터클래스
# ---------------------------------------------------------------------------

@dataclass
class NewsResult:
    code:        str
    name:        str
    headlines:   List[dict]       # [{"title": str}, ...]
    sentiment:   str              # "POSITIVE" | "NEGATIVE" | "NEUTRAL"
    analyzed_at: datetime = field(default_factory=datetime.now)

    @property
    def sentiment_icon(self) -> str:
        return {"POSITIVE": "📈", "NEGATIVE": "📉", "NEUTRAL": "📊"}.get(
            self.sentiment, "📊"
        )

    def summary(self, max_titles: int = 3) -> str:
        tops = " / ".join(
            h["title"][:25] for h in self.headlines[:max_titles]
        ) if self.headlines else "뉴스 없음"
        return (
            f"{self.sentiment_icon} [뉴스] {self.name}({self.code}) "
            f"{self.sentiment} — {tops}"
        )


# ---------------------------------------------------------------------------
# NewsAnalyzer
# ---------------------------------------------------------------------------

class NewsAnalyzer:
    """
    백그라운드 뉴스 분석기 — ThreadPoolExecutor 병렬 처리.

    단일 스레드 순차 처리 대신 최대 3개 종목을 동시에 조회한다.
    동시 신호 3개가 와도 마지막 종목이 최대 ~8초(timeout) 뒤 완료된다.

    사용 예)
        analyzer = NewsAnalyzer(on_result=lambda r: print(r.summary()))
        analyzer.start()
        analyzer.analyze("005930", "삼성전자")  # 즉시 반환
        # ~수초 후 on_result 콜백 호출 (스레드풀 워커에서)
        analyzer.stop()

    ⚠ on_result 는 스레드풀 워커에서 호출된다.
      Qt UI 갱신이 필요하면 queue.Queue + QTimer 드레인 패턴을 사용할 것.
      (main_window.py 참고)
    """

    _MAX_WORKERS  = 3    # 동시 처리 최대 종목 수
    _QUEUE_MAXSIZE = 10  # 큐 최대 크기 — 넘치면 오래된 요청 버림 (뒷북 방지)

    # 감성 분류 키워드
    _POS = ["상승", "급등", "호재", "수주", "계약", "신고가", "실적", "개선",
            "흑자", "성장", "협약", "투자", "확대", "돌파"]
    _NEG = ["하락", "급락", "손실", "위기", "제재", "조사", "불안", "우려",
            "적자", "감소", "취소", "소송", "벌금", "부실", "하향"]

    def __init__(self, on_result: Optional[Callable[[NewsResult], None]] = None) -> None:
        self._on_result  = on_result
        # maxsize 제한 — 큐가 가득 차면 오래된 요청을 버려 뒷북 방지
        self._queue: queue.Queue = queue.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._running    = False
        self._dispatcher: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._analyzed: set = set()     # 당일 분석 완료 종목 코드
        self._in_flight: Dict[str, Future] = {}  # 현재 처리 중인 종목
        self._lock = threading.Lock()

    # -----------------------------------------------------------------------
    # 공개 API
    # -----------------------------------------------------------------------

    def start(self) -> None:
        """스레드풀 + 디스패처 스레드 시작 (idempotent)"""
        if self._running:
            return
        self._running = True
        self._pool = ThreadPoolExecutor(
            max_workers=self._MAX_WORKERS, thread_name_prefix="NewsWorker"
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="NewsDispatcher"
        )
        self._dispatcher.start()
        logger.info("[NewsAnalyzer] 스레드풀(%d) + 디스패처 시작", self._MAX_WORKERS)

    def stop(self) -> None:
        """정상 종료"""
        self._running = False
        try:
            self._queue.put_nowait(None)  # sentinel
        except queue.Full:
            pass
        if self._pool:
            self._pool.shutdown(wait=False)
        logger.info("[NewsAnalyzer] 정지 요청")

    def analyze(self, code: str, name: str) -> None:
        """
        뉴스 분석 요청 — 즉시 반환, 스레드풀에서 병렬 처리.

        같은 종목은 당일 1회만 분석한다.
        큐가 가득 차면 가장 오래된 요청을 버리고 새 요청을 넣는다 (뒷북 방지).
        """
        with self._lock:
            if code in self._analyzed or code in self._in_flight:
                return
        # 큐가 가득 차면 오래된 항목 1개를 버리고 새 것을 넣음
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self._queue.put_nowait((code, name))
            logger.debug("[NewsAnalyzer] 분석 요청 추가: %s(%s)", name, code)
        except queue.Full:
            pass

    def reset_daily(self) -> None:
        """로그인 시 호출 — 당일 분석 완료 캐시 초기화"""
        with self._lock:
            self._analyzed.clear()
        logger.info("[NewsAnalyzer] 당일 캐시 초기화")

    # -----------------------------------------------------------------------
    # 디스패처 루프 (단일 스레드) — 큐에서 꺼내 스레드풀에 제출
    # -----------------------------------------------------------------------

    def _dispatch_loop(self) -> None:
        """큐에서 요청을 꺼내 스레드풀 워커에 제출"""
        logger.info("[NewsAnalyzer] 디스패처 시작")
        while self._running:
            try:
                item = self._queue.get(timeout=5.0)
            except queue.Empty:
                continue

            if item is None:
                break

            code, name = item
            with self._lock:
                if code in self._analyzed or code in self._in_flight:
                    continue
                future = self._pool.submit(self._worker, code, name)
                self._in_flight[code] = future
                future.add_done_callback(lambda f, c=code: self._on_done(c, f))

        logger.info("[NewsAnalyzer] 디스패처 종료")

    def _worker(self, code: str, name: str) -> Optional["NewsResult"]:
        """스레드풀 워커 — 뉴스 조회 및 분석"""
        return self._fetch_and_analyze(code, name)

    def _on_done(self, code: str, future: "Future") -> None:
        """Future 완료 콜백 — 결과 처리 및 in_flight 정리"""
        with self._lock:
            self._in_flight.pop(code, None)
            self._analyzed.add(code)
        try:
            result = future.result()
            if result and self._on_result:
                self._on_result(result)
        except Exception as e:
            logger.warning("[NewsAnalyzer] %s 처리 실패: %s", code, e)

    # -----------------------------------------------------------------------
    # 뉴스 조회 & 감성 분류
    # -----------------------------------------------------------------------

    def _fetch_and_analyze(self, code: str, name: str) -> Optional[NewsResult]:
        try:
            headlines = self._search_naver_news(name)
            sentiment = self._calc_sentiment(headlines)
            logger.info("[NewsAnalyzer] %s(%s) 뉴스 %d건 — %s",
                        name, code, len(headlines), sentiment)
            return NewsResult(
                code=code, name=name,
                headlines=headlines, sentiment=sentiment,
            )
        except Exception as e:
            logger.warning("[NewsAnalyzer] %s 뉴스 조회 실패: %s", name, e)
            return None

    def _search_naver_news(self, keyword: str, max_titles: int = 5) -> list:
        """
        네이버 뉴스 검색 — 최신순 헤드라인 수집.

        공개 HTML 검색 결과에서 제목을 파싱한다.
        네트워크 접근 불가 환경에서는 빈 리스트를 반환한다.
        """
        query = urllib.parse.quote(keyword)
        url = (
            "https://search.naver.com/search.naver"
            f"?where=news&query={query}&sort=1"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # 뉴스 제목 추출 (news_tit 클래스 기준)
        pattern = r'class="news_tit"[^>]*title="([^"]+)"'
        titles = re.findall(pattern, html)[:max_titles]
        return [{"title": t} for t in titles]

    def _calc_sentiment(self, headlines: list) -> str:
        """긍부정 키워드 카운트 기반 감성 분류"""
        text = " ".join(h.get("title", "") for h in headlines)
        pos = sum(1 for kw in self._POS if kw in text)
        neg = sum(1 for kw in self._NEG if kw in text)
        if pos > neg:
            return "POSITIVE"
        if neg > pos:
            return "NEGATIVE"
        return "NEUTRAL"
