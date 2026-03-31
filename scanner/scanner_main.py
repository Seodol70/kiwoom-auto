"""
스캐너 메인 — 유니버스 + 조건검색(선택) → watch_list 관리

두 가지 선정 모드를 모두 지원한다.

  ┌─────────────────────────────────────────────────────┐
  │  MODE A : 조건식 모드                                │
  │  HTS 조건식 번호를 넘기면 조건 편입 종목만 감시      │
  │  watch_list = universe ∩ condition_candidates        │
  ├─────────────────────────────────────────────────────┤
  │  MODE B : 자체 선정 모드                             │
  │  조건식 없이 자체 스코어링으로 종목 선정             │
  │  watch_list = universe 내 등락률·거래대금 상위 종목  │
  └─────────────────────────────────────────────────────┘

1분마다 watch_list 종목의 현재가·등락률·거래대금을 갱신한다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from scanner.condition_search import ConditionSearcher
from scanner.universe import get_filtered_universe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass
class ScannerConfig:
    # 공통
    update_interval:  int   = 60       # watch_list 갱신 주기 (초)
    min_change_pct:   float = 3.0      # 자체 선정: 등락률 최소 (%)
    max_change_pct:   float = 15.0     # 자체 선정: 등락률 상한 (%) — 급등 과열 종목 제외
    min_trade_amt:    int   = 5_000_000_000   # 유니버스 거래대금 기준 (50억)
    top_n_self:       int   = 20       # 자체 선정 시 최대 감시 종목 수

    # 조건식 모드 (USE_CONDITION = True 일 때만 활성)
    use_condition:    bool  = False    # True → 조건식 모드
    cond_index:       int   = 0        # HTS 조건식 번호
    cond_name:        str   = ""       # HTS 조건식 이름
    cond_screen_no:   str   = "9100"


# ---------------------------------------------------------------------------
# watch_list 아이템
# ---------------------------------------------------------------------------

@dataclass
class WatchItem:
    code:         str
    name:         str
    current_price: int   = 0
    change_pct:   float  = 0.0
    volume:       int    = 0
    trade_amount: int    = 0
    source:       str    = "SELF"       # "CONDITION" | "SELF"
    added_at:     datetime = field(default_factory=datetime.now)
    updated_at:   datetime = field(default_factory=datetime.now)

    def update(self, price: int, change_pct: float, volume: int) -> None:
        self.current_price = price
        self.change_pct    = change_pct
        self.volume        = volume
        self.trade_amount  = price * volume
        self.updated_at    = datetime.now()


# ---------------------------------------------------------------------------
# ScannerMain
# ---------------------------------------------------------------------------

class ScannerMain:
    """
    스캐너 통합 관리자.

    사용 예 — 조건식 모드)
        cfg = ScannerConfig(use_condition=True, cond_index=0, cond_name="급등주")
        scanner = ScannerMain(kiwoom, cfg)
        scanner.on_watch_updated = lambda wl: strategy.refresh(wl)
        scanner.start()

    사용 예 — 자체 선정 모드)
        cfg = ScannerConfig(use_condition=False, min_change_pct=3.0, top_n_self=20)
        scanner = ScannerMain(kiwoom, cfg)
        scanner.start()
    """

    def __init__(self, kiwoom, cfg: Optional[ScannerConfig] = None) -> None:
        self._kiwoom  = kiwoom
        self.cfg      = cfg or ScannerConfig()

        self.universe:    set[str]             = set()
        self.watch_list:  dict[str, WatchItem] = {}   # code → WatchItem

        self._searcher: Optional[ConditionSearcher] = None
        self._timer:    Optional[threading.Timer]   = None
        self._running   = False

        # 외부 콜백 — watch_list 갱신될 때마다 호출
        self.on_watch_updated: Optional[Callable[[dict[str, WatchItem]], None]] = None

    # -----------------------------------------------------------------------
    # 시작 / 정지
    # -----------------------------------------------------------------------

    def start(self) -> None:
        """스캐너를 시작한다. 유니버스 구성 후 주기적 갱신을 예약한다."""
        if self._running:
            logger.warning("스캐너가 이미 실행 중입니다.")
            return

        logger.info("스캐너 시작 — 모드: %s",
                    "조건식" if self.cfg.use_condition else "자체 선정")

        # ① 유니버스 구성
        self.universe = get_filtered_universe(
            self._kiwoom,
            min_trade_amt=self.cfg.min_trade_amt,
        )

        # ② 조건식 모드 초기화
        if self.cfg.use_condition:
            self._searcher = ConditionSearcher(
                self._kiwoom,
                screen_no=self.cfg.cond_screen_no,
            )
            self._searcher.load()
            self._searcher.start(self.cfg.cond_index, self.cfg.cond_name)

        self._running = True
        self._schedule_update()
        logger.info("스캐너 시작 완료 — 유니버스 %d종목", len(self.universe))

    def stop(self) -> None:
        """스캐너를 정지한다."""
        self._running = False
        if self._timer:
            self._timer.cancel()
        if self._searcher:
            self._searcher.stop(self.cfg.cond_index, self.cfg.cond_name)
        logger.info("스캐너 정지")

    # -----------------------------------------------------------------------
    # 주기적 갱신 루프
    # -----------------------------------------------------------------------

    def _schedule_update(self) -> None:
        if not self._running:
            return
        self._update_watch_list()
        self._timer = threading.Timer(self.cfg.update_interval, self._schedule_update)
        self._timer.daemon = True
        self._timer.start()

    def _update_watch_list(self) -> None:
        """watch_list 대상 선정 + 현재가 갱신을 한 번 수행한다."""
        logger.info("watch_list 갱신 시작 — %s",
                    datetime.now().strftime("%H:%M:%S"))

        # ─ 대상 코드 결정 ──────────────────────────────────────────────
        if self.cfg.use_condition and self._searcher:
            # MODE A: 유니버스 ∩ 조건식 편입 종목
            target_codes = self.universe & self._searcher.current_candidates
            source = "CONDITION"
        else:
            # MODE B: 유니버스 내 자체 스코어링
            target_codes = self._self_select()
            source = "SELF"

        # ─ 현재가·등락률 갱신 ──────────────────────────────────────────
        updated: dict[str, WatchItem] = {}
        for code in target_codes:
            item = self._fetch_watch_item(code, source)
            if item:
                updated[code] = item

        self.watch_list = updated
        logger.info("watch_list 갱신 완료 — %d종목", len(self.watch_list))

        if self.on_watch_updated:
            self.on_watch_updated(self.watch_list)

    # -----------------------------------------------------------------------
    # MODE B: 자체 종목 선정
    # -----------------------------------------------------------------------

    def _self_select(self) -> set[str]:
        """
        유니버스에서 등락률 기준으로 상위 top_n_self 종목을 고른다.

        TR 호출이 많으므로 현재가 조회는 빠른 GetMasterLastPrice 를 사용하고
        등락률은 get_stock_info 로 별도 확인한다.
        """
        scored: list[tuple[float, str]] = []   # (change_pct, code)

        for code in self.universe:
            try:
                info = self._kiwoom.get_stock_info(code)
                change_pct = info.get("change_pct", 0.0)
                if change_pct < self.cfg.min_change_pct:
                    continue
                if change_pct > self.cfg.max_change_pct:
                    logger.debug("과열 제외 — %s 등락률 %.1f%% > 상한 %.1f%%",
                                 code, change_pct, self.cfg.max_change_pct)
                    continue
                trade_amt = info["current_price"] * info["volume"]
                scored.append((trade_amt, code))
            except Exception as e:
                logger.debug("스코어링 실패 — %s: %s", code, e)

        # 거래대금 내림차순 상위 top_n 선택
        scored.sort(reverse=True)
        selected = {code for _, code in scored[: self.cfg.top_n_self]}
        logger.info("자체 선정 — 등락률 %.1f%%~%.1f%% 통과 %d종목 → 상위 %d 선택",
                    self.cfg.min_change_pct, self.cfg.max_change_pct,
                    len(scored), len(selected))
        return selected

    # -----------------------------------------------------------------------
    # WatchItem 생성 헬퍼
    # -----------------------------------------------------------------------

    def _fetch_watch_item(self, code: str, source: str) -> Optional[WatchItem]:
        """키움 API 로 현재가·등락률을 조회해 WatchItem 을 생성/갱신한다."""
        try:
            info = self._kiwoom.get_stock_info(code)

            # 기존 아이템이 있으면 update, 없으면 새로 생성
            if code in self.watch_list:
                item = self.watch_list[code]
            else:
                item = WatchItem(
                    code   = code,
                    name   = info.get("name", ""),
                    source = source,
                )

            item.update(
                price      = info.get("current_price", 0),
                change_pct = info.get("change_pct",    0.0),
                volume     = info.get("volume",         0),
            )
            return item

        except Exception as e:
            logger.warning("WatchItem 생성 실패 — %s: %s", code, e)
            return None

    # -----------------------------------------------------------------------
    # 조회 인터페이스
    # -----------------------------------------------------------------------

    def get_watch_list(self) -> list[WatchItem]:
        """현재 watch_list 를 거래대금 내림차순으로 반환한다."""
        return sorted(
            self.watch_list.values(),
            key=lambda x: x.trade_amount,
            reverse=True,
        )

    def get_condition_pool(self) -> set[str]:
        """
        조건식 모드일 때 현재 조건식 편입 종목 풀을 반환한다.
        자체 선정 모드에서는 빈 set 반환.
        """
        if self._searcher:
            return self._searcher.current_candidates.copy()
        return set()

    def summary(self) -> dict:
        """현재 상태 요약"""
        return {
            "mode":         "조건식" if self.cfg.use_condition else "자체 선정",
            "universe_cnt": len(self.universe),
            "condition_pool_cnt": len(self.get_condition_pool()),
            "watch_list_cnt": len(self.watch_list),
            "updated_at":   datetime.now().strftime("%H:%M:%S"),
        }
