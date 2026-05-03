"""
SnapshotStore — pandas DataFrame 기반 종목 스냅샷 캐시

전 종목 시세를 메모리의 pandas DataFrame에 보관하고,
실시간 틱과 분봉 데이터를 별도 dict로 관리한다.

[Phase 2] smart_scanner.py에서 분리
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque as _Deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.models import StockSnapshot
from scanner.universe import is_ordinary_stock, is_pure_equity_name, filter_equity_rows

logger = logging.getLogger(__name__)





def _df_cell_scalar(val, default=None):
    """
    DataFrame.loc[code] 행에서 컬럼 값이 스칼라가 아니라 Series인 경우(중복 컬럼명 등) 대비.
    truthiness 검사로 Series를 건드리지 않도록 첫 스칼라만 꺼낸다.
    """
    if val is None:
        return default
    if isinstance(val, pd.Series):
        if val.empty:
            return default
        val = val.iloc[0]
    try:
        if pd.isna(val):
            return default
    except TypeError:
        pass
    return val


_DF_COLS = [
    "code", "name",
    "current_price", "open_price", "high_price", "low_price",
    "volume", "trade_amount", "prev_close", "change_pct",
    "rank", "updated_at",
]


class SnapshotStore:
    """
    전 종목 스냅샷을 pandas DataFrame 에 보관한다.

    ┌──────────────────────────────────────────────────┐
    │ 왜 DataFrame 인가?                               │
    │  · bulk_update() 1회 호출로 200종목 일괄 적재   │
    │  · df.nlargest() 로 API 재호출 없이 순위 산출   │
    │  · 컬럼 연산 (vectorized) 으로 신호 판단 가능   │
    │  · 백테스트 CSV 로 바로 export 가능             │
    └──────────────────────────────────────────────────┘

    실시간 틱은 update_price() 로 행 단위 갱신한다.
    closes_1min 은 DataFrame 외부에서 dict 로 별도 관리
    (리스트 컬럼은 벡터 연산 불가 → 분리가 더 효율적).
    """

    def __init__(self) -> None:
        self._df   = pd.DataFrame(columns=_DF_COLS).set_index("code")
        self._mins: dict[str, list[float]] = {}   # code → 1분봉 종가
        self._last_min: dict[str, int] = {}        # code → 마지막 기록된 분(minute)
        self._min_vols:  dict[str, list[int]]   = {}   # code → 1분봉 별 거래량 델타
        self._last_vol:  dict[str, int]         = {}   # code → 직전 분 경계 누적거래량
        self._min_opens: dict[str, list[float]] = {}   # code → 1분봉 시가
        self._min_highs: dict[str, list[float]] = {}   # code → 1분봉 고가
        self._min_lows:  dict[str, list[float]] = {}   # code → 1분봉 저가
        self._cur_open:  dict[str, float]       = {}   # code → 현재 분 시가 (첫 틱)
        self._cur_high:  dict[str, float]       = {}   # code → 현재 분 고가 (진행중)
        self._cur_low:   dict[str, float]       = {}   # code → 현재 분 저가 (진행중)
        self._chejan_str: dict[str, float]      = {}   # code → 체결강도 (FID 20)
        self._daily_data: dict[str, list[dict]] = {}   # code → 일봉 OHLCV 리스트 (최신순)
        self._daily_updated_at: dict[str, datetime] = {}  # code → 마지막 갱신 시각
        self._inv_foreign: dict[str, int] = {}
        self._inv_inst: dict[str, int] = {}
        self._inv_score: dict[str, int] = {}
        self._inv_updated_at: dict[str, datetime] = {}
        self._trend_level: dict[str, int] = {}         # code → 현재 추세 단계(0~3)
        self._trend_prev_level: dict[str, int] = {}    # code → 직전 추세 단계
        self._tick_ts_vol: dict[str, _Deque] = {}      # code → deque[(monotonic_ts, cumvol)]
        self._sector_cache: dict[str, str] = {}        # code → 업종명
        self._prices: dict[str, int] = {}              # code → 현재가 (고속 캐시)
        self._chg_pcts: dict[str, float] = {}          # code → 등락률 (고속 캐시)
        self._vols: dict[str, int] = {}                # code → 누적 거래량
        self._amt: dict[str, int] = {}                 # code → 누적 거래대금
        self._lock = threading.Lock()
        self._daily_cache_path: Path = self._get_daily_cache_path()
        self._load_daily_cache()
        self._1min_cache_path: Path = self._get_1min_cache_path()
        self._load_1min_cache()

    _NUM_COLS = [
        "current_price", "open_price", "high_price", "low_price",
        "volume", "trade_amount", "prev_close", "change_pct", "rank",
        "prev_volume", "vol_ratio",
    ]

    def bulk_update(self, rows: list[dict]) -> None:
        """
        Pre-Filter 결과(list[dict])를 DataFrame 에 일괄 적재한다.
        기존 행이 있으면 갱신, 없으면 추가한다.
        """
        if not rows:
            logger.warning("[SnapshotStore.bulk_update] rows 빈 리스트 — 적재 스킵")
            return

        rows, _dropped = filter_equity_rows(rows)
        if not rows:
            logger.warning("[SnapshotStore.bulk_update] ETF·파생 제외 후 빈 리스트 — 적재 스킵")
            return

        for row in rows:
            prev_close = row.get("prev_close")
            if (prev_close is None or prev_close == 0):
                cp = float(row.get("change_pct") or 0)
                curr = float(row.get("current_price") or 0)
                if curr > 0 and cp != 0:
                    row["prev_close"] = int(curr / (1.0 + cp / 100.0))
                    logger.debug("[복구] %s: change_pct=%.2f%% curr=%d → prev_close=%d (역산)",
                                 row.get("code"), cp, curr, row["prev_close"])
                elif curr > 0 and cp == 0:
                    row["prev_close"] = curr
                    logger.debug("[복구] %s: change_pct=0%% → prev_close=%d (동일)",
                                 row.get("code"), curr)

        first = rows[0]
        logger.debug("[⚠️ bulk_update] 첫 행 진단 — code=%s name=%s | price=%s open=%s high=%s low=%s | volume=%s trade_amt=%s prev_close=%s chg_pct=%s",
                     first.get("code"), first.get("name"),
                     first.get("current_price"), first.get("open_price"), first.get("high_price"), first.get("low_price"),
                     first.get("volume"), first.get("trade_amount"),
                     first.get("prev_close"), first.get("change_pct"))

        new_df = pd.DataFrame(rows).set_index("code")
        new_df["updated_at"] = datetime.now()
        for col in self._NUM_COLS:
            if col in new_df.columns:
                new_df[col] = pd.to_numeric(new_df[col], errors="coerce").fillna(0)

        if not new_df.empty and new_df.index.duplicated().any():
            new_df = new_df[~new_df.index.duplicated(keep='last')]

        codes_to_remove = []
        if not new_df.empty and "name" in new_df.columns:
            keep = new_df["name"].astype(str).map(is_pure_equity_name)
            codes_to_remove = new_df.index[~keep].tolist()
            new_df = new_df[keep]

        with self._lock:
            self._df = new_df.combine_first(self._df)

            if not self._df.empty and "name" in self._df.columns:
                keep = self._df["name"].astype(str).map(is_pure_equity_name)
                codes_to_remove.extend(self._df.index[~keep].tolist())
                if codes_to_remove:
                    codes_to_remove = list(set(codes_to_remove))
                    self._df = self._df[keep]

            for c in codes_to_remove:
                for d in (self._mins, self._min_opens, self._min_highs, self._min_lows,
                          self._min_vols, self._cur_open, self._cur_high, self._cur_low,
                          self._trend_level, self._trend_prev_level,
                          self._inv_foreign, self._inv_inst, self._inv_score, self._inv_updated_at,
                          self._tick_ts_vol, self._sector_cache):
                    d.pop(c, None)

            for code in new_df.index:
                if code not in self._mins:
                    self._mins[code] = []
                # 고속 캐시 초기화
                self._prices[code] = int(new_df.at[code, "current_price"])
                self._chg_pcts[code] = float(new_df.at[code, "change_pct"])
                self._vols[code] = int(new_df.at[code, "volume"])
                self._amt[code] = int(new_df.at[code, "trade_amount"])

        logger.debug("[SnapshotStore.bulk_update] 적재 완료 — df 행수=%d", len(self._df))

    _TICK_COLS = ["current_price", "high_price", "low_price",
                  "open_price", "volume", "trade_amount", "change_pct"]

    def update_price(
        self,
        code:         str,
        current_price: int,
        high_price:   int,
        low_price:    int,
        open_price:   int,
        volume:       int,
        trade_amount: int = None,
        change_pct:   float = None,
    ) -> None:
        """
        실시간 체결 한 틱을 해당 종목 행에 반영한다.

        trade_amount=None이면 opt10030의 누적 거래대금을 보존한다.
        (FID 14는 현재 틱만 포함하므로)
        """
        with self._lock:
            # 1. 고속 dict 캐시 업데이트 (DataFrame 수정보다 훨씬 빠름)
            self._prices[code] = current_price
            self._chg_pcts[code] = change_pct if change_pct is not None else self._chg_pcts.get(code, 0.0)
            self._vols[code] = volume
            if trade_amount is not None:
                self._amt[code] = trade_amount

            # 2. DataFrame 갱신 지연 (필요할 때만 _sync_df_prices() 호출)
            # 여기서는 틱 데이터 보관용 deque와 분봉 로직만 수행
            _ts_now = time.monotonic()
            if code not in self._tick_ts_vol:
                self._tick_ts_vol[code] = _Deque()
            _tq = self._tick_ts_vol[code]
            _tq.append((_ts_now, volume))
            _cutoff_70 = _ts_now - 70.0
            while _tq and _tq[0][0] < _cutoff_70:
                _tq.popleft()
            cur_min = (datetime.now().hour * 60 +
                       datetime.now().minute)
            cp = float(current_price)
            if self._last_min.get(code, -1) != cur_min:
                if code in self._cur_open:
                    def _append120(lst, val):
                        lst.append(val)
                        if len(lst) > 120:
                            lst.pop(0)
                    _append120(self._mins.setdefault(code, []),       cp)
                    _append120(self._min_opens.setdefault(code, []),  self._cur_open[code])
                    _append120(self._min_highs.setdefault(code, []),  self._cur_high[code])
                    _append120(self._min_lows.setdefault(code,  []),  self._cur_low[code])
                    prev_cumvol = self._last_vol.get(code, volume)
                    delta = max(0, volume - prev_cumvol)
                    _append120(self._min_vols.setdefault(code, []), delta)
                    self._last_vol[code] = volume
                self._last_min[code] = cur_min
                self._cur_open[code] = cp
                self._cur_high[code] = cp
                self._cur_low[code]  = cp
            else:
                if code in self._cur_open:
                    if cp > self._cur_high[code]:
                        self._cur_high[code] = cp
                    if cp < self._cur_low[code]:
                        self._cur_low[code] = cp
                else:
                    self._cur_open[code] = cp
                    self._cur_high[code] = cp
                    self._cur_low[code]  = cp

    def get_snapshot(self, code: str) -> Optional[StockSnapshot]:
        """단일 종목 스냅샷을 반환한다 (API 호출 없음). 락 범위 최소화 버전."""
        with self._lock:
            if code not in self._df.index:
                return None
            row = self._df.loc[code]

            row_copy = {k: _df_cell_scalar(row.get(k), None) for k in row.index}
            closes_list = list(self._mins.get(code, []))
            opens_list = list(self._min_opens.get(code, []))
            highs_list = list(self._min_highs.get(code, []))
            lows_list = list(self._min_lows.get(code, []))
            vols_list = list(self._min_vols.get(code, []))
            daily_data_copy = list(self._daily_data.get(code, []))
            chejan_str = self._chejan_str.get(code, 100.0)
            sector = self._sector_cache.get(code, "")
            trend_lv = int(self._trend_level.get(code, 0))
            trend_prev_lv = int(self._trend_prev_level.get(code, 0))
            tick_data = dict(self._tick_ts_vol.get(code, {}))
            inv_foreign = int(self._inv_foreign.get(code, 0))
            inv_inst = int(self._inv_inst.get(code, 0))
            inv_score = int(self._inv_score.get(code, 0))
            inv_updated = self._inv_updated_at.get(code)
            
            # [Optimization] DataFrame 대신 고속 dict에서 최신가 가져오기
            current_price_cached = self._prices.get(code, 0)
            volume_cached = self._vols.get(code, 0)
            amt_cached = self._amt.get(code, 0)
            chg_pct_cached = self._chg_pcts.get(code, 0.0)

        def safe_int_cell(key: str, default: int = 0) -> int:
            v = row_copy.get(key, default)
            if v is None:
                return default
            try:
                iv = int(float(v))
            except (TypeError, ValueError):
                return default
            return iv if iv != 0 else default

        def safe_float_cell(key: str, default: float = 0.0) -> float:
            v = row_copy.get(key, default)
            if v is None:
                return default
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return default
            return fv if fv != 0 else default

        nm = row_copy.get("name", "")
        name_s = str(nm) if nm is not None else ""

        ua_raw = row_copy.get("updated_at", None)
        if isinstance(ua_raw, datetime):
            updated_at = ua_raw
        elif ua_raw is not None:
            try:
                updated_at = pd.Timestamp(ua_raw).to_pydatetime()
            except Exception:
                updated_at = datetime.now()
        else:
            updated_at = datetime.now()

        daily_closes = [float(c["close"]) for c in daily_data_copy if c.get("close", 0) > 0]
        daily_high_prev = daily_data_copy[0].get("high", 0) if daily_data_copy else 0
        daily_low_prev = daily_data_copy[0].get("low", 0) if daily_data_copy else 0

        current_price = safe_int_cell("current_price", 0)

        from scanner.indicator_service import IndicatorService
        alignment = IndicatorService.check_daily_alignment(daily_closes, current_price)
        _is_daily_bull = alignment.get("is_aligned", False) if alignment else False

        _rsi_cached = 0.0
        if len(closes_list) >= 15:
            try:
                _r = IndicatorService.calc_rsi(closes_list, 14)
                if _r is not None and _r > 0:
                    _rsi_cached = float(_r)
            except Exception:
                pass

        _exec_vel_ratio = 0.0
        if tick_data and len(tick_data) >= 2:
            _now_ts = time.monotonic()
            _cutoff_10s = _now_ts - 10.0
            _tick_list = list(tick_data.items())
            _vol_now = _tick_list[-1][1]
            _vol_before_10s = _tick_list[0][1]
            for _ts_e, _vol_e in _tick_list:
                if _ts_e < _cutoff_10s:
                    _vol_before_10s = _vol_e
            _vol_10s = max(0, _vol_now - _vol_before_10s)
            if vols_list and _vol_10s > 0:
                _recent_mv = vols_list[-min(5, len(vols_list)):]
                _avg_per_min = sum(_recent_mv) / len(_recent_mv) if _recent_mv else 0
                _avg_per_10s = _avg_per_min / 6.0
                if _avg_per_10s > 0:
                    _exec_vel_ratio = _vol_10s / _avg_per_10s

        return StockSnapshot(
            code          = code,
            name          = name_s,
            current_price = current_price_cached if current_price_cached > 0 else safe_int_cell("current_price", 0),
            open_price    = safe_int_cell("open_price",    0),
            high_price    = safe_int_cell("high_price",    0),
            low_price     = safe_int_cell("low_price",     0),
            volume        = volume_cached if volume_cached > 0 else safe_int_cell("volume", 0),
            trade_amount  = amt_cached if amt_cached > 0 else safe_int_cell("trade_amount", 0),
            prev_close    = safe_int_cell("prev_close",    0),
            change_pct    = chg_pct_cached if chg_pct_cached != 0 else safe_float_cell("change_pct",  0.0),
            closes_1min   = closes_list,
            highs_1min    = highs_list,
            lows_1min     = lows_list,
            volumes_1min  = vols_list,
            daily_closes  = daily_closes,
            daily_highs   = [daily_high_prev],
            daily_lows    = [daily_low_prev],
            foreign_net_buy = inv_foreign,
            inst_net_buy    = inv_inst,
            rank             = safe_int_cell("rank", 0),
            rsi              = _rsi_cached,
            updated_at       = updated_at,
        )

    def update_trend_level(self, code: str, trend_level: int) -> None:
        """요셉 시그널 추세 레벨 갱신(0~3). 직전 단계도 함께 보관."""
        level = int(max(0, min(3, trend_level)))
        with self._lock:
            prev = int(self._trend_level.get(code, 0))
            self._trend_prev_level[code] = prev
            self._trend_level[code] = level

    def update_chejan_strength(self, code: str, strength: float) -> None:
        """[NEW] 체결강도(FID 20) 갱신."""
        if strength > 0:
            with self._lock:
                self._chejan_str[code] = strength

    def update_sector(self, code: str, sector: str) -> None:
        """[NEW] 업종명 캐시 갱신 — handle_signal()에서 opt10001 응답 후 호출."""
        if sector:
            with self._lock:
                self._sector_cache[code] = sector

    def prefilter_candidates(self, max_change_pct: Optional[float] = None) -> list[str]:
        """
        벡터화 사전 필터 — DataFrame 연산으로 Python 루프 전 후보 종목을 추린다.

        조건 (모두 DataFrame 컬럼 연산, O(n) 한 번):
          ① current_price > 0          (가격 유효)
          ② current_price > open_price  (시가 돌파 기본 조건)
          ③ change_pct > 0             (양봉 기조)
          ④ volume > 0                 (거래량 있음)
          ⑤ max_change_pct 지정 시: change_pct < max_change_pct (과열 급등 제외)

        반환값: 조건을 통과한 종목코드 리스트 (MA 검사는 이후 Python 루프에서)

        효과: 50종목 중 보통 5~15개만 남아 Python 루프 비용이 70~90% 감소
        """
        with self._lock:
            if self._df.empty:
                return []
            
            # 조회 전 시세 동기화
            self._sync_df_prices()
            
            df = self._df
            ch = df.get("change_pct", pd.Series(0, index=df.index))
            mask = (
                (df["current_price"] > 0) &
                (df["current_price"] > df["open_price"]) &
                (ch > 0) &
                (df["volume"] > 0)
            )
            if max_change_pct is not None:
                mask = mask & (ch < max_change_pct)
            return list(df.index[mask])

    def update_investor(
        self,
        code:        str,
        foreign_net: int,
        inst_net:    int,
    ) -> None:
        """
        외국인/기관 순매수 수량을 StockSnapshot에 갱신한다 (opt10059 10분 주기 호출).

        investor_score 산출:
          +1 : 외국인 AND 기관 모두 순매수 → 수급 우호
           0 : 어느 한쪽만 순매수 또는 둘 다 0
          -1 : 외국인 AND 기관 모두 순매도 → 수급 비우호
        """
        with self._lock:
            if code not in self._df.index:
                return
            self._inv_foreign[code] = int(foreign_net)
            self._inv_inst[code] = int(inst_net)
            if foreign_net > 0 and inst_net > 0:
                score = 1
            elif foreign_net < 0 and inst_net < 0:
                score = -1
            else:
                score = 0
            self._inv_score[code] = score
            self._inv_updated_at[code] = datetime.now()

    def get_investor_data(self, code: str) -> tuple[int, int, int]:
        """종목의 수급 데이터를 (foreign_net_buy, inst_net_buy, investor_score) 튜플로 반환.

        get_snapshot() 없이 _inv_* 딕셔너리만 읽으므로 StockSnapshot 객체 생성 비용 없음.
        미조회 종목은 (0, 0, 0) 반환.
        """
        with self._lock:
            return (
                int(self._inv_foreign.get(code, 0)),
                int(self._inv_inst.get(code, 0)),
                int(self._inv_score.get(code, 0)),
            )

    def set_min_candles(self, code: str, closes: list) -> None:
        """opt10080 등으로 가져온 분봉 종가 리스트를 초기값으로 설정한다."""
        with self._lock:
            self._mins[code] = [float(c) for c in closes if c]

    def set_min_candles_ohlc(self, code: str, candles: list[dict]) -> None:
        """분봉 OHLCV 전체를 초기값으로 설정한다 (캔들 패턴 판단용).

        Args:
            candles: [{"open": int, "high": int, "low": int, "close": int, "volume": int}, ...]
                     오래된 것 → 최신 순 (시간순 오름차순)
        """
        with self._lock:
            self._mins[code]       = [float(c["close"])  for c in candles if c.get("close")]
            self._min_opens[code]  = [float(c["open"])   for c in candles if c.get("open")]
            self._min_highs[code]  = [float(c["high"])   for c in candles if c.get("high")]
            self._min_lows[code]   = [float(c["low"])    for c in candles if c.get("low")]
            self._min_vols[code]   = [int(c.get("volume", 0)) for c in candles]

    @staticmethod
    def _get_1min_cache_path() -> Path:
        today = datetime.now().strftime("%Y%m%d")
        cache_dir = Path(__file__).parent.parent / "cache"
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"1min_{today}.json"

    def _load_1min_cache(self) -> None:
        """당일 1분봉 캐시 파일이 있으면 메모리로 로드한다 (데이터 부족 코드만)."""
        try:
            if not self._1min_cache_path.exists():
                return
            with open(self._1min_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            loaded = 0
            with self._lock:
                for code, ohlcv in data.items():
                    if len(self._mins.get(code, [])) >= 55:
                        continue
                    closes = ohlcv.get("c", [])
                    opens  = ohlcv.get("o", [])
                    highs  = ohlcv.get("h", [])
                    lows   = ohlcv.get("l", [])
                    vols   = ohlcv.get("v", [])
                    if closes:
                        self._mins[code]       = [float(x) for x in closes]
                        self._min_opens[code]  = [float(x) for x in opens]
                        self._min_highs[code]  = [float(x) for x in highs]
                        self._min_lows[code]   = [float(x) for x in lows]
                        self._min_vols[code]   = [int(x)   for x in vols]
                        loaded += 1
            if loaded:
                logger.info("[1분봉캐시] 로드 완료 — %d종목 (%s)", loaded, self._1min_cache_path.name)
        except Exception as e:
            logger.warning("[1분봉캐시] 로드 실패 — %s", e)

    def save_1min_cache(self) -> None:
        """현재 1분봉 데이터 전체를 디스크에 저장한다 (5분마다 호출)."""
        try:
            with self._lock:
                data = {
                    code: {
                        "c": self._mins.get(code, []),
                        "o": self._min_opens.get(code, []),
                        "h": self._min_highs.get(code, []),
                        "l": self._min_lows.get(code, []),
                        "v": self._min_vols.get(code, []),
                    }
                    for code in self._mins
                    if len(self._mins[code]) >= 10
                }
            with open(self._1min_cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logger.debug("[1분봉캐시] 저장 완료 — %d종목", len(data))
        except Exception as e:
            logger.warning("[1분봉캐시] 저장 실패 — %s", e)

    def load_1min_for_code(self, code: str) -> int:
        """
        캐시 파일에서 특정 종목의 1분봉을 즉시 로드한다.
        이미 55개 이상이면 스킵. 반환값 = 로드된 캔들 수 (0이면 캐시 없음).
        """
        try:
            if not self._1min_cache_path.exists():
                return 0
            with open(self._1min_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            ohlcv = data.get(code)
            if not ohlcv:
                return 0
            closes = ohlcv.get("c", [])
            if not closes:
                return 0
            with self._lock:
                if len(self._mins.get(code, [])) >= 55:
                    return len(self._mins[code])
                self._mins[code]       = [float(x) for x in closes]
                self._min_opens[code]  = [float(x) for x in ohlcv.get("o", [])]
                self._min_highs[code]  = [float(x) for x in ohlcv.get("h", [])]
                self._min_lows[code]   = [float(x) for x in ohlcv.get("l", [])]
                self._min_vols[code]   = [int(x)   for x in ohlcv.get("v", [])]
            return len(closes)
        except Exception:
            return 0

    @staticmethod
    def _get_daily_cache_path() -> Path:
        """오늘 날짜 기준 일봉 캐시 파일 경로 반환."""
        today = datetime.now().strftime("%Y%m%d")
        cache_dir = Path(__file__).parent.parent / "cache"
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"daily_{today}.json"

    def _load_daily_cache(self) -> None:
        """당일 일봉 캐시 파일이 있으면 메모리로 로드한다."""
        try:
            if not self._daily_cache_path.exists():
                return
            with open(self._daily_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            self._daily_data = data
            logger.info(
                "[일봉캐시] 로드 완료 — %d종목 (파일: %s)",
                len(data), self._daily_cache_path.name,
            )
        except Exception as e:
            logger.warning("[일봉캐시] 로드 실패 — %s", e)

    def _save_daily_cache(self) -> None:
        """현재 _daily_data 전체를 디스크에 저장한다."""
        try:
            with self._lock:
                snapshot = dict(self._daily_data)
            with open(self._daily_cache_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("[일봉캐시] 저장 실패 — %s", e)

    def set_daily_candles(self, code: str, candles: list[dict]) -> None:
        """
        [NEW] opt10081로 가져온 일봉 OHLCV 데이터를 캐시에 저장한다.

        Args:
            code: 종목코드
            candles: 일봉 캔들 리스트
                   [{"date": "YYYYMMDD", "open": int, "high": int, "low": int, "close": int, "volume": int}, ...]
        """
        with self._lock:
            if candles:
                self._daily_data[code] = candles
                self._daily_updated_at[code] = datetime.now()
        if candles:
            self._save_daily_cache()

    def _sync_df_prices(self) -> None:
        """고속 캐시(dict)에 저장된 시세를 DataFrame에 일괄 반영한다."""
        if not self._prices:
            return
        # 주의: 락(lock) 안에서 호출되어야 함
        for code, price in self._prices.items():
            if code in self._df.index:
                self._df.at[code, "current_price"] = price
                self._df.at[code, "volume"] = self._vols.get(code, 0)
                if code in self._chg_pcts:
                    self._df.at[code, "change_pct"] = self._chg_pcts[code]
                if code in self._amt:
                    self._df.at[code, "trade_amount"] = self._amt[code]

    def top_by_trade_amount(self, n: int = 20) -> pd.DataFrame:
        """
        거래대금 상위 n 종목 DataFrame 반환 (복사본).
        trade_amount 가 모두 0 이면 volume → rank 순으로 fallback.
        [2026-04-03] 중복 인덱스 제거 추가
        """
        with self._lock:
            if self._df.empty:
                return pd.DataFrame()
            
            # 조회 전 시세 동기화
            self._sync_df_prices()
            
            df = self._df[~self._df.index.duplicated(keep='last')]

            non_zero_amt = df[df["trade_amount"] > 0]
            if not non_zero_amt.empty:
                return non_zero_amt.nlargest(n, "trade_amount").copy()
            non_zero_vol = df[df["volume"] > 0]
            if not non_zero_vol.empty:
                return non_zero_vol.nlargest(n, "volume").copy()
            if "rank" in df.columns:
                ranked = df.dropna(subset=["rank"])
                if not ranked.empty:
                    return ranked.nsmallest(n, "rank").copy()
            return df.head(n).copy()

    def cleanup_stale_data(self, active_codes: set[str]) -> int:
        """active_codes에 없는 종목 데이터를 메모리에서 제거한다. 제거된 종목 수 반환.

        일봉 데이터(_daily_data)는 재조회 비용이 크므로 유지한다.
        """
        with self._lock:
            stale = set(self._prices.keys()) - active_codes
            if not stale:
                return 0

            realtime_dicts = [
                self._prices, self._chg_pcts, self._vols, self._amt,
                self._mins, self._last_min, self._min_vols, self._last_vol,
                self._min_opens, self._min_highs, self._min_lows,
                self._cur_open, self._cur_high, self._cur_low,
                self._chejan_str,
                self._inv_foreign, self._inv_inst, self._inv_score, self._inv_updated_at,
                self._trend_level, self._trend_prev_level,
                self._tick_ts_vol, self._sector_cache,
            ]
            for code in stale:
                for d in realtime_dicts:
                    d.pop(code, None)

            stale_in_df = [c for c in stale if c in self._df.index]
            if stale_in_df:
                self._df.drop(index=stale_in_df, inplace=True, errors="ignore")

            logger.debug("[SnapshotStore] 메모리 정리 — %d종목 제거 (active=%d)", len(stale), len(active_codes))
            return len(stale)

    def export_csv(self, path: str = "logs/snapshot.csv") -> None:
        """현재 스냅샷을 CSV 로 내보낸다."""
        with self._lock:
            self._df.reset_index().to_csv(path, index=False, encoding="utf-8-sig")

    def __len__(self) -> int:
        return len(self._df)
