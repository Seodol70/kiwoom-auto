"""
조건검색 연동 — 키움 HTS 조건식을 실시간으로 감시한다.

두 가지 사용 방식:
  A) 조건식 모드  : HTS 에 저장된 조건식 번호로 편입/이탈 종목을 수신
  B) 자체 선정 모드: ConditionSearcher 를 사용하지 않고
                     scanner_main 의 자체 스코어링으로만 watch_list 구성

current_candidates (set[str]) 가 "현재 조건식을 통과한 종목 풀"이다.
scanner_main 은 universe ∩ current_candidates 교집합을 watch_list 로 쓴다.
"""

from __future__ import annotations

import logging
from threading import Event
from typing import Optional

logger = logging.getLogger(__name__)

# 조건검색 조회 유형
COND_ONCE     = 0   # 1회 조회
COND_REALTIME = 1   # 실시간 등록

# 실시간 편입/이탈 구분
ENTER  = "I"
REMOVE = "D"

DEFAULT_SCREEN = "9100"


class ConditionSearcher:
    """
    키움 HTS 조건식 기반 종목 감시기.

    current_candidates: set[str]
        현재 조건식을 통과하고 있는 종목코드 집합.
        OnReceiveRealCondition 콜백이 실시간으로 갱신한다.

    사용 예)
        searcher = ConditionSearcher(kiwoom)
        searcher.load()                        # 조건 목록 로드
        print(searcher.conditions)             # [(0,"급등주"), (1,"거래량상위"), ...]

        searcher.start(0, "급등주")            # 실시간 등록
        # ... 이후 current_candidates 가 자동 갱신됨
        searcher.stop(0, "급등주")             # 해제
    """

    def __init__(self, kiwoom, screen_no: str = DEFAULT_SCREEN) -> None:
        self._kiwoom    = kiwoom
        self._screen_no = screen_no
        self._load_event = Event()

        self.conditions: list[tuple[int, str]] = []  # [(idx, name), ...]
        self.current_candidates: set[str] = set()    # 조건 통과 종목 풀

        self._connect_signals()

    # -----------------------------------------------------------------------
    # 시그널 연결
    # -----------------------------------------------------------------------

    def _connect_signals(self) -> None:
        ocx = self._kiwoom._ocx
        ocx.OnReceiveConditionVer.connect(self._on_condition_ver)
        ocx.OnReceiveTrCondition.connect(self._on_tr_condition)
        ocx.OnReceiveRealCondition.connect(self._on_real_condition)
        logger.debug("조건검색 시그널 연결")

    # -----------------------------------------------------------------------
    # 공개 API
    # -----------------------------------------------------------------------

    def load(self) -> list[tuple[int, str]]:
        """
        HTS 에 저장된 조건 목록을 로드한다.

        Returns:
            [(인덱스, 조건명), ...]  예) [(0, "급등주"), (1, "거래량상위")]
        """
        self._load_event.clear()
        ret = self._kiwoom._ocx.dynamicCall("GetConditionLoad()")
        if ret != 1:
            logger.error("GetConditionLoad 실패")
            return []
        self._load_event.wait(timeout=10)
        return self.conditions

    def start(
        self,
        cond_index: int,
        cond_name:  str,
        realtime:   bool = True,
    ) -> bool:
        """
        조건검색을 시작한다.

        Args:
            cond_index: conditions 에서 얻은 인덱스
            cond_name:  조건명
            realtime:   True → 실시간, False → 1회 조회

        Returns:
            성공 여부
        """
        search_type = COND_REALTIME if realtime else COND_ONCE
        ret = self._kiwoom._ocx.dynamicCall(
            "SendCondition(QString, QString, int, int)",
            [self._screen_no, cond_name, cond_index, search_type],
        )
        ok = ret == 1
        if ok:
            logger.info("조건검색 시작 — [%d] %s (%s)",
                        cond_index, cond_name,
                        "실시간" if realtime else "1회")
        else:
            logger.error("SendCondition 실패 — [%d] %s", cond_index, cond_name)
        return ok

    def stop(self, cond_index: int, cond_name: str) -> None:
        """실시간 조건검색을 해제하고 current_candidates 를 비운다."""
        self._kiwoom._ocx.dynamicCall(
            "SendConditionStop(QString, QString, int)",
            [self._screen_no, cond_name, cond_index],
        )
        self.current_candidates.clear()
        logger.info("조건검색 해제 — [%d] %s", cond_index, cond_name)

    # -----------------------------------------------------------------------
    # 이벤트 콜백
    # -----------------------------------------------------------------------

    def _on_condition_ver(self, ret: int, msg: str) -> None:
        """GetConditionLoad() 완료"""
        if ret != 1:
            logger.error("조건 목록 로드 실패: %s", msg)
            self._load_event.set()
            return

        raw: str = self._kiwoom._ocx.dynamicCall("GetConditionNameList()")
        # 형식: "0^급등주;1^거래량상위;..."
        self.conditions = []
        for item in raw.strip().split(";"):
            if not item:
                continue
            parts = item.split("^")
            if len(parts) == 2:
                self.conditions.append((int(parts[0]), parts[1]))

        logger.info("조건 목록 %d건 로드 완료 — %s",
                    len(self.conditions),
                    [n for _, n in self.conditions])
        self._load_event.set()

    def _on_tr_condition(
        self,
        screen_no:  str,
        code_list:  str,   # "005930;000660;035720;"
        cond_name:  str,
        cond_index: int,
        next_:      int,
    ) -> None:
        """1회 조건검색 결과 수신 — 결과 전체를 current_candidates 에 추가"""
        codes = {c for c in code_list.split(";") if c}
        self.current_candidates |= codes
        logger.info("조건검색 결과 — [%s] %d종목 추가 (누적 %d)",
                    cond_name, len(codes), len(self.current_candidates))

    def _on_real_condition(
        self,
        code:       str,
        event_type: str,   # "I" 편입 / "D" 이탈
        cond_name:  str,
        cond_index: str,
    ) -> None:
        """실시간 조건 편입/이탈"""
        if event_type == ENTER:
            self.current_candidates.add(code)
            logger.debug("편입 — %s [%s] (풀 크기 %d)",
                         code, cond_name, len(self.current_candidates))
        elif event_type == REMOVE:
            self.current_candidates.discard(code)
            logger.debug("이탈 — %s [%s] (풀 크기 %d)",
                         code, cond_name, len(self.current_candidates))
