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

from scanner.models import StockSnapshot, InternalStockState
from scanner.universe import is_ordinary_stock, is_pure_equity_name, filter_equity_rows
from scanner.snapshot import TickToCandleProcessor, MinuteCandle

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
    "total_ask_qty", "total_bid_qty",
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
        self._states: dict[str, InternalStockState] = {}
        
        # [Phase 3] 분봉 생성 전담 프로세서
        self._processor = TickToCandleProcessor()
        
        self._lock = threading.Lock()
        self._daily_cache_path: Path = self._get_daily_cache_path()
        self._load_daily_cache()
        self._1min_cache_path: Path = self._get_1min_cache_path()
        self._load_1min_cache()

    def _get_state(self, code: str) -> InternalStockState:
        """종목 코드를 키로 하는 상태 객체를 반환하거나 새로 생성한다."""
        if code not in self._states:
            self._states[code] = InternalStockState(code=code)
        return self._states[code]

    def get_candle_count(self, code: str) -> int:
        """특정 종목의 1분봉 캔들 개수를 반환한다."""
        with self._lock:
            st = self._states.get(code)
            return len(st.mins) if st else 0

    _NUM_COLS = [
        "current_price", "open_price", "high_price", "low_price",
        "volume", "trade_amount", "prev_close", "change_pct", "rank",
        "prev_volume", "vol_ratio", "total_ask_qty", "total_bid_qty",
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
                self._states.pop(c, None)

            for code in new_df.index:
                st = self._get_state(code)
                st.name = str(new_df.at[code, "name"])
                
                # 고속 캐시 및 상태 업데이트 - 0원 방어 로직
                try:
                    new_p = int(new_df.at[code, "current_price"])
                    if new_p > 0:
                        st.current_price = new_p
                    elif st.current_price == 0:
                        logger.debug("[SnapshotStore] %s 초기 시세 0원 감지 (TR 응답 확인 필요)", code)
                except Exception as e:
                    logger.warning("[SnapshotStore] %s 시세 캐시 업데이트 오류: %s", code, e)

                st.change_pct    = float(new_df.at[code, "change_pct"])    if "change_pct"    in new_df.columns else 0.0
                st.trade_amount  = int(new_df.at[code, "trade_amount"])    if "trade_amount"  in new_df.columns else 0
                st.prev_close    = int(new_df.at[code, "prev_close"])      if "prev_close"    in new_df.columns else st.prev_close
                
                # [NEW] 초기 누적 데이터 설정 (VWAP 보정용)
                if st.cumulative_volume == 0: st.cumulative_volume = st.volume
                if st.cumulative_amount == 0: st.cumulative_amount = st.trade_amount
                st.updated_at = datetime.now()

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
        cum_vol:      int = 0,
        cum_amt:      int = 0,
    ) -> None:
        """실시간 체결 한 틱을 해당 종목 상태에 반영한다."""
        with self._lock:
            st = self._get_state(code)
            
            # 1. 시세 업데이트 (0원 방어)
            if current_price > 0:
                st.current_price = current_price
            
            if change_pct is not None:
                st.change_pct = change_pct
            
            # [NEW] 틱 거래량(차분) 계산: 누적 데이터가 들어오면 이전 누적치와 비교
            tick_vol = 0
            if cum_vol > 0:
                if st.cumulative_volume > 0:
                    tick_vol = max(0, cum_vol - st.cumulative_volume)
                else:
                    # 최초 수신 시에는 현재 틱 거래량을 알 수 없으므로 0 혹은 volume(보통 0으로 처리)
                    tick_vol = 0
                st.cumulative_volume = cum_vol
            
            if cum_amt > 0:
                st.cumulative_amount = cum_amt

            # st.volume(누적) 업데이트
            st.volume = cum_vol if cum_vol > 0 else volume
            if trade_amount is not None:
                st.trade_amount = trade_amount
            
            st.updated_at = datetime.now()

            # [NEW] 체결강도 히스토리 업데이트
            if hasattr(st, "chejan_str") and st.chejan_str > 0:
                st.chejan_history.append(st.chejan_str)

            # 2. 틱 속도 계산용 히스토리 갱신 (누적이 아닌 '순수 틱 거래량' 사용)
            _ts_now = time.monotonic()
            st.tick_ts_vol.append((_ts_now, tick_vol))
            _cutoff_70 = _ts_now - 70.0
            while st.tick_ts_vol and st.tick_ts_vol[0][0] < _cutoff_70:
                st.tick_ts_vol.popleft()

            # 3. [Phase 3] 분봉 프로세서 위임 (순수 틱 거래량 사용)
            completed = self._processor.process_tick(code, float(current_price), tick_vol)
            if completed:
                def _append_limit(lst, val, limit=120):
                    lst.append(val)
                    if len(lst) > limit: lst.pop(0)

                _append_limit(st.mins,      completed.close)
                _append_limit(st.min_opens, completed.open)
                _append_limit(st.min_highs, completed.high)
                _append_limit(st.min_lows,  completed.low)
                _append_limit(st.min_vols,  completed.volume)

    def get_snapshot(self, code: str) -> Optional[StockSnapshot]:
        """단일 종목 스냅샷을 반환한다 (API 호출 없음). 락 범위 최소화 버전."""
        with self._lock:
            if code not in self._df.index:
                return None
            row = self._df.loc[code]
            row_copy = {k: _df_cell_scalar(row.get(k), None) for k in row.index}

        def safe_int_cell(key: str, default: int = 0) -> int:
            v = row_copy.get(key, default)
            if v is None: return default
            try:
                iv = int(float(v))
            except (TypeError, ValueError): return default
            return iv if iv != 0 else default

        def safe_float_cell(key: str, default: float = 0.0) -> float:
            v = row_copy.get(key, default)
            if v is None: return default
            try:
                fv = float(v)
            except (TypeError, ValueError): return default
            return fv if fv != 0 else default

        # [Price Logic] 최신가 우선순위: 고속캐시 -> DataFrame -> 분봉 마지막 종가(백업)
        with self._lock:
            st = self._get_state(code)
            curr_p = st.current_price if st.current_price > 0 else safe_int_cell("current_price", 0)
            if curr_p == 0 and st.mins:
                curr_p = int(st.mins[-1])
                logger.debug("[SnapshotStore] %s 현재가 복구 (0원 -> 분봉종가 %d)", code, curr_p)

            # 지표 계산용 복사본
            closes_list = list(st.mins)
            highs_list  = list(st.min_highs)
            lows_list   = list(st.min_lows)
            vols_list   = list(st.min_vols)
            daily_data_copy = list(st.daily_data)
            
            # 수급/추세 데이터 추출
            inv_foreign   = st.inv_foreign
            inv_inst      = st.inv_inst
            inv_score     = st.inv_score
            trend_lv      = st.trend_level
            trend_prev_lv = st.trend_prev_level
            chejan_str    = st.chejan_str
            chejan_hist   = list(st.chejan_history)
            m_type        = getattr(st, "market_type", "10")

        nm = row_copy.get("name", "")
        name_s = str(nm) if nm is not None else ""

        ua_raw = row_copy.get("updated_at", None)
        updated_at = ua_raw if isinstance(ua_raw, datetime) else datetime.now()

        daily_closes = [float(c["close"]) for c in daily_data_copy if c.get("close", 0) > 0]
        daily_high_prev = daily_data_copy[0].get("high", 0) if daily_data_copy else 0
        daily_low_prev = daily_data_copy[0].get("low", 0) if daily_data_copy else 0

        from scanner.indicator_service import IndicatorService
        alignment = IndicatorService.check_daily_alignment(daily_closes, curr_p)
        _rsi_cached = 0.0
        if len(closes_list) >= 15:
            try:
                _r = IndicatorService.calc_rsi(closes_list, 14)
                if _r is not None and _r > 0: _rsi_cached = float(_r)
            except Exception: pass
            
        # [NEW] 체결 가속도(Execution Velocity) 계산 — 10초 체결량 vs 1분 평균
        # ratio=0.0 은 데이터 부족(fail-open)
        _vel_ratio = 0.0
        with self._lock:
            ts_vol = list(st.tick_ts_vol)
            if ts_vol:
                _now = time.monotonic()
                _v10 = sum(v for t, v in ts_vol if t >= _now - 10.0)
                _v60 = sum(v for t, v in ts_vol if t >= _now - 60.0)
                if _v60 > 0:
                    # 1분 평균 10초량 = _v60 / 6
                    _avg10 = _v60 / 6.0
                    _vel_ratio = _v10 / _avg10 if _avg10 > 0 else 0.0

        return StockSnapshot(
            code          = code,
            name          = name_s,
            current_price = curr_p,
            open_price    = safe_int_cell("open_price",    0),
            high_price    = safe_int_cell("high_price",    0),
            low_price     = safe_int_cell("low_price",     0),
            volume        = st.volume if st.volume > 0 else safe_int_cell("volume", 0),
            trade_amount  = st.trade_amount if st.trade_amount > 0 else safe_int_cell("trade_amount", 0),
            prev_close    = safe_int_cell("prev_close",    0),
            change_pct    = st.change_pct if st.change_pct != 0 else safe_float_cell("change_pct",  0.0),
            total_ask_qty = st.total_ask_qty if st.total_ask_qty > 0 else safe_int_cell("total_ask_qty", 0),
            total_bid_qty = st.total_bid_qty if st.total_bid_qty > 0 else safe_int_cell("total_bid_qty", 0),
            closes_1min   = closes_list,
            opens_1min    = list(st.min_opens),
            highs_1min    = highs_list,
            lows_1min     = lows_list,
            volumes_1min  = vols_list,
            daily_closes  = daily_closes,
            daily_highs   = [daily_high_prev],
            daily_lows    = [daily_low_prev],
            foreign_net_buy = inv_foreign,
            inst_net_buy    = inv_inst,
            investor_score  = inv_score,
            trend_level     = trend_lv,
            trend_prev_level = trend_prev_lv,
            chejan_strength  = chejan_str,
            chejan_history   = chejan_hist,
            cumulative_volume = st.cumulative_volume,
            cumulative_amount = st.cumulative_amount,
            market_type      = m_type,
            rank             = safe_int_cell("rank", 0),
            rsi              = _rsi_cached,
            exec_velocity_ratio = _vel_ratio,
            updated_at       = updated_at,
        )

    def update_trend_level(self, code: str, trend_level: int) -> None:
        """요셉 시그널 추세 레벨 갱신(0~3). 직전 단계도 함께 보관."""
        with self._lock:
            st = self._get_state(code)
            st.update_trend(int(max(0, min(3, trend_level))))

    def update_chejan_strength(self, code: str, strength: float) -> None:
        """[NEW] 체결강도(FID 20) 갱신."""
        if strength > 0:
            with self._lock:
                self._get_state(code).chejan_str = strength

    def update_hoga(self, code: str, total_ask: int, total_bid: int) -> None:
        """[NEW] 호가 잔량 갱신."""
        with self._lock:
            st = self._get_state(code)
            st.total_ask_qty = total_ask
            st.total_bid_qty = total_bid
            if code in self._df.index:
                self._df.at[code, "total_ask_qty"] = total_ask
                self._df.at[code, "total_bid_qty"] = total_bid

    def update_sector(self, code: str, sector: str) -> None:
        """[NEW] 업종명 캐시 갱신 — handle_signal()에서 opt10001 응답 후 호출."""
        if sector:
            with self._lock:
                self._get_state(code).sector = sector

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
        """외국인/기관 순매수 수량을 StockSnapshot에 갱신한다."""
        with self._lock:
            st = self._get_state(code)
            st.inv_foreign = int(foreign_net)
            st.inv_inst = int(inst_net)
            
            if foreign_net > 0 and inst_net > 0:
                score = 1
            elif foreign_net < 0 and inst_net < 0:
                score = -1
            else:
                score = 0
            st.inv_score = score
            st.inv_updated_at = datetime.now()

    def get_investor_data(self, code: str) -> tuple[int, int, int]:
        """종목의 수급 데이터를 (foreign_net_buy, inst_net_buy, investor_score) 튜플로 반환."""
        with self._lock:
            st = self._states.get(code)
            if not st: return (0, 0, 0)
            return (st.inv_foreign, st.inv_inst, st.inv_score)

    def set_min_candles(self, code: str, closes: list) -> None:
        """opt10080 등으로 가져온 분봉 종가 리스트를 초기값으로 설정한다."""
        with self._lock:
            self._get_state(code).mins = [float(c) for c in closes if c]

    def set_min_candles_ohlc(self, code: str, candles: list[dict]) -> None:
        """분봉 OHLCV 전체를 초기값으로 설정한다."""
        with self._lock:
            st = self._get_state(code)
            st.mins       = [float(c["close"])  for c in candles if c.get("close")]
            st.min_opens  = [float(c["open"])   for c in candles if c.get("open")]
            st.min_highs  = [float(c["high"])   for c in candles if c.get("high")]
            st.min_lows   = [float(c["low"])    for c in candles if c.get("low")]
            st.min_vols   = [int(c.get("volume", 0)) for c in candles]
            
            # [Phase 3] 프로세서 동기화
            if st.mins:
                last_vol = int(getattr(candles[-1], "cum_volume", 0) or candles[-1].get("volume", 0))
                self._processor.set_initial_state(code, -1, last_vol)

    @staticmethod
    def _get_1min_cache_path() -> Path:
        today = datetime.now().strftime("%Y%m%d")
        cache_dir = Path(__file__).parent.parent / "cache"
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"1min_{today}.json"

    def _load_1min_cache(self) -> None:
        """당일 1분봉 캐시 파일이 있으면 메모리로 로드한다."""
        try:
            if not self._1min_cache_path.exists(): return
            with open(self._1min_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict): return
            loaded = 0
            with self._lock:
                for code, ohlcv in data.items():
                    st = self._get_state(code)
                    if len(st.mins) >= 55: continue
                    
                    st.mins       = [float(x) for x in ohlcv.get("c", [])]
                    st.min_opens  = [float(x) for x in ohlcv.get("o", [])]
                    st.min_highs  = [float(x) for x in ohlcv.get("h", [])]
                    st.min_lows   = [float(x) for x in ohlcv.get("l", [])]
                    st.min_vols   = [int(x)   for x in ohlcv.get("v", [])]
                    if st.mins:
                        loaded += 1
                        self._processor.set_initial_state(code, -1, st.volume)
            if loaded:
                logger.info("[1분봉캐시] 로드 완료 — %d종목 (%s)", loaded, self._1min_cache_path.name)
        except Exception as e:
            logger.warning("[1분봉캐시] 로드 실패 — %s", e)

    def save_1min_cache(self) -> None:
        """현재 1분봉 데이터 전체를 디스크에 저장한다."""
        try:
            with self._lock:
                data = {
                    code: {
                        "c": st.mins, "o": st.min_opens, "h": st.min_highs,
                        "l": st.min_lows, "v": st.min_vols,
                    }
                    for code, st in self._states.items()
                    if len(st.mins) >= 10
                }
            with open(self._1min_cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logger.debug("[1분봉캐시] 저장 완료 — %d종목", len(data))
        except Exception as e:
            logger.warning("[1분봉캐시] 저장 실패 — %s", e)

    def load_1min_for_code(self, code: str) -> int:
        """캐시 파일에서 특정 종목의 1분봉을 즉시 로드한다."""
        try:
            if not self._1min_cache_path.exists(): return 0
            with open(self._1min_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            ohlcv = data.get(code)
            if not ohlcv or not ohlcv.get("c"): return 0
            
            with self._lock:
                st = self._get_state(code)
                if len(st.mins) >= 55: return len(st.mins)
                st.mins       = [float(x) for x in ohlcv.get("c", [])]
                st.min_opens  = [float(x) for x in ohlcv.get("o", [])]
                st.min_highs  = [float(x) for x in ohlcv.get("h", [])]
                st.min_lows   = [float(x) for x in ohlcv.get("l", [])]
                st.min_vols   = [int(x)   for x in ohlcv.get("v", [])]
                return len(st.mins)
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
            if not self._daily_cache_path.exists(): return
            with open(self._daily_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict): return
            with self._lock:
                for code, candles in data.items():
                    self._get_state(code).daily_data = candles
            logger.info("[일봉캐시] 로드 완료 — %d종목", len(data))
        except Exception as e:
            logger.warning("[일봉캐시] 로드 실패 — %s", e)

    def _save_daily_cache(self) -> None:
        """현재 일봉 데이터 전체를 디스크에 저장한다."""
        try:
            with self._lock:
                data = {code: st.daily_data for code, st in self._states.items() if st.daily_data}
            with open(self._daily_cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("[일봉캐시] 저장 실패 — %s", e)

    def set_daily_candles(self, code: str, candles: list[dict]) -> None:
        """opt10081로 가져온 일봉 OHLCV 데이터를 캐시에 저장한다."""
        if not candles: return
        with self._lock:
            st = self._get_state(code)
            st.daily_data = candles
            st.daily_updated_at = datetime.now()
        self._save_daily_cache()

    def _sync_df_prices(self) -> None:
        """고속 캐시(InternalStockState)에 저장된 시세를 DataFrame에 일괄 반영한다."""
        if not self._states: return
        # 주의: 락(lock) 안에서 호출되어야 함
        for code, st in self._states.items():
            if code in self._df.index:
                self._df.at[code, "current_price"] = st.current_price
                self._df.at[code, "volume"]        = st.volume
                self._df.at[code, "change_pct"]    = st.change_pct
                self._df.at[code, "trade_amount"]  = st.trade_amount

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
        """active_codes에 없는 종목 데이터를 메모리에서 제거한다."""
        with self._lock:
            stale = set(self._states.keys()) - active_codes
            if not stale: return 0

            for code in stale:
                self._states.pop(code, None)

            # [Phase 4] 내부 분봉 프로세서 상태 정리
            if hasattr(self, "_processor") and self._processor:
                self._processor.cleanup_stale_data(active_codes)

            stale_in_df = [c for c in stale if c in self._df.index]
            if stale_in_df:
                self._df.drop(index=stale_in_df, inplace=True, errors="ignore")

            logger.info("[SnapshotStore] 메모리 정리 완료 — %d종목 데이터 제거", len(stale))
            return len(stale)

    def export_csv(self, path: str = "logs/snapshot.csv") -> None:
        """현재 스냅샷을 CSV 로 내보낸다."""
        with self._lock:
            self._df.reset_index().to_csv(path, index=False, encoding="utf-8-sig")

    def __len__(self) -> int:
        return len(self._df)
