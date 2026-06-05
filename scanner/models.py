"""
Scanner 도메인 데이터 모델

SnapshotStore, IndicatorService, SignalEvaluator 등 여러 모듈이 사용하는
dataclass를 중앙 정의. 순환 import 방지.
"""
from collections import deque as _Deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict


@dataclass
class InternalStockState:
    """SnapshotStore 내부에서 관리하는 종목별 상세 상태 (Mutable)"""
    code: str
    name: str = ""
    
    # 실시간 시세 (고속 캐시)
    current_price: int = 0
    open_price: int = 0
    high_price: int = 0
    low_price: int = 0
    prev_close: int = 0
    volume: int = 0
    trade_amount: int = 0
    cumulative_volume: int = 0  # FID 15 (당일거래량)
    cumulative_amount: int = 0  # FID 13 (누적거래대금, 단위: 천원)
    change_pct: float = 0.0
    market_type: str = "10" # "0": KOSPI, "10": KOSDAQ
    
    # 호가 잔량 (집계)
    total_ask_qty: int = 0
    total_bid_qty: int = 0

    # [2026-06-02] 호가 상세 (1~5호가 가격·수량) — FID 41~49, 51~59
    ask_prices: List[int] = field(default_factory=lambda: [0]*5)  # 매도1~5 가격
    ask_qtys:   List[int] = field(default_factory=lambda: [0]*5)  # 매도1~5 수량
    bid_prices: List[int] = field(default_factory=lambda: [0]*5)  # 매수1~5 가격
    bid_qtys:   List[int] = field(default_factory=lambda: [0]*5)  # 매수1~5 수량
    hoga_updated_at: Optional[datetime] = None
    # [2026-06-04 Phase3] 매수1호가 가격 이력 — 우상향 기울기 감지용 (최근 10틱)
    bid1_history: _Deque = field(default_factory=lambda: _Deque(maxlen=10))
    # [2026-06-04 Phase3-FIX] 매수호가 수량 이력 — 호가 속도(velocity) 계산용 (최근 10스냅)
    bid_qty_sums_history: _Deque = field(default_factory=lambda: _Deque(maxlen=10))
    # [2026-06-05 P1] 매도1호가 수량 이력 — 매도벽 급감 감지용 (최근 10틱)
    ask1_qty_history: _Deque = field(default_factory=lambda: _Deque(maxlen=10))
    # [2026-06-05 P2] 틱 단위 체결량 이력 — 실시간 체결속도 감지용 (최근 20틱)
    tick_vol_history: _Deque = field(default_factory=lambda: _Deque(maxlen=20))

    # 분봉 (1분봉 OHLCV)
    mins: List[float] = field(default_factory=list)
    min_opens: List[float] = field(default_factory=list)
    min_highs: List[float] = field(default_factory=list)
    min_lows: List[float] = field(default_factory=list)
    min_vols: List[int] = field(default_factory=list)
    
    # 수급/기타
    chejan_str: float = 100.0
    inv_foreign: int = 0
    inv_inst: int = 0
    inv_score: int = 0
    inv_updated_at: Optional[datetime] = None
    # 매수 전환 감지용 이력
    inv_foreign_prev: int = 0          # 직전 외인 순매수
    inv_inst_prev: int = 0             # 직전 기관 순매수
    inv_score_prev: int = 0            # 직전 수급 점수 (-1/0/1)
    inv_flip_at: Optional[datetime] = None  # 최근 매수 전환 시각
    
    # 추세/메타
    trend_level: int = 0
    trend_prev_level: int = 0
    sector: str = ""
    chejan_history: _Deque = field(default_factory=lambda: _Deque(maxlen=20)) # 체결강도 이력
    updated_at: datetime = field(default_factory=datetime.now)
    
    # 틱 데이터 (초당 거래 속도 계산용)
    tick_ts_vol: _Deque = field(default_factory=_Deque)
    
    # 일봉 데이터
    daily_data: List[Dict] = field(default_factory=list)
    daily_updated_at: Optional[datetime] = None

    # [2026-06-02] 60분봉 데이터
    h1_closes: List[float] = field(default_factory=list)  # 60분봉 종가 (최신순 역전, 오래된→최신)
    h1_highs:  List[float] = field(default_factory=list)
    h1_lows:   List[float] = field(default_factory=list)
    h1_updated_at: Optional[datetime] = None

    # [NEW] 성능 최적화용 지표 캐시 및 갱신 제어
    rsi_cached: float = 0.0
    exec_vel_cached: float = 0.0
    tick_count: int = 0
    last_calc_ts: float = 0.0

    def update_trend(self, new_level: int):
        self.trend_prev_level = self.trend_level
        self.trend_level = new_level


@dataclass
class StockSnapshot:
    """실시간 종목 스냅샷 — 1초마다 갱신되는 시장 데이터"""

    # 기본 정보
    code: str
    name: str

    # 시세
    current_price: int
    open_price: int
    high_price: int
    low_price: int
    prev_close: int

    # 거래량/거래대금
    volume: int
    trade_amount: int
    change_pct: float
    market_type: str = "10"
    
    # 호가 잔량 (집계)
    total_ask_qty: int = 0
    total_bid_qty: int = 0

    # [2026-06-02] 호가 상세 (1~5호가 가격·수량) — FID 41~49, 51~59
    ask_prices: list[int] = field(default_factory=lambda: [0]*5)  # 매도1~5 가격
    ask_qtys:   list[int] = field(default_factory=lambda: [0]*5)  # 매도1~5 수량
    bid_prices: list[int] = field(default_factory=lambda: [0]*5)  # 매수1~5 가격
    bid_qtys:   list[int] = field(default_factory=lambda: [0]*5)  # 매수1~5 수량
    hoga_updated_at: Optional[datetime] = None
    # [2026-06-04 Phase3] 매수1호가 이력 (최근 10틱) — 우상향 기울기 감지용
    bid1_history: list = field(default_factory=list)
    # [2026-06-04 Phase3-FIX] 매수호가 수량 이력 (최근 10스냅) — 호가 속도 계산용
    bid_qty_sums_history: list = field(default_factory=list)
    # [2026-06-05 P1] 매도1호가 수량 이력 (최근 10틱) — 매도벽 급감 감지용
    ask1_qty_history: list = field(default_factory=list)
    # [2026-06-05 P2] 틱 단위 체결량 이력 (최근 20틱) — 실시간 체결속도 감지용
    tick_vol_history: list = field(default_factory=list)

    @property
    def hoga_pressure(self) -> float:
        """매수호가 압력비 = 매수2~3호가 합계 / 매도2~3호가 합계.
        1.0 초과 → 매수 우위, 미만 → 매도 우위. 데이터 없으면 1.0 반환.
        """
        bid_vol = sum(self.bid_qtys[1:3])  # 매수2+3호가
        ask_vol = sum(self.ask_qtys[1:3])  # 매도2+3호가
        if ask_vol <= 0:
            return 2.0 if bid_vol > 0 else 1.0
        return bid_vol / ask_vol

    @property
    def hoga_ready(self) -> bool:
        """호가 상세 데이터가 수신된 상태인지"""
        return self.hoga_updated_at is not None and any(self.bid_qtys)

    @property
    def bid1_slope(self) -> float:
        """매수1호가 우상향 기울기 — 최근 5틱 이상 있을 때만 계산.
        양수 = 우상향(매수세 강화), 0 = 보합, 음수 = 하락(매수세 약화).
        데이터 부족 시 0.0 반환.
        """
        h = list(self.bid1_history)
        if len(h) < 5:
            return 0.0
        h = h[-5:]  # 최근 5틱
        # 단순 선형 기울기: (마지막 - 첫번째) / 첫번째 * 100 (%)
        if h[0] <= 0:
            return 0.0
        return (h[-1] - h[0]) / h[0] * 100

    # 지표 (계산 결과)
    rsi: Optional[float] = None
    ema10: Optional[float] = None
    ema20: Optional[float] = None
    ma7: Optional[float] = None
    ma15: Optional[float] = None

    # 메타
    updated_at: Optional[datetime] = None
    rank: Optional[int] = None  # 거래대금 상위 순위

    # 일봉 데이터 (외부에서 저장)
    daily_closes: list[float] = field(default_factory=list)
    daily_highs: list[float] = field(default_factory=list)
    daily_lows: list[float] = field(default_factory=list)

    # 1분봉 데이터 (외부에서 저장)
    closes_1min: list[float] = field(default_factory=list)
    opens_1min: list[float] = field(default_factory=list)
    highs_1min: list[float] = field(default_factory=list)
    lows_1min: list[float] = field(default_factory=list)
    volumes_1min: list[int] = field(default_factory=list)

    # 누적 데이터 (True VWAP 계산용)
    cumulative_volume: int = 0  # FID 15 (당일거래량)
    cumulative_amount: int = 0  # FID 13 (누적거래대금, 단위: 천원)

    @property
    def vwap(self) -> Optional[float]:
        """당일 누적 데이터를 기반으로 한 정확한 VWAP (True VWAP)"""
        if self.cumulative_volume > 0:
            return (self.cumulative_amount * 1000.0) / self.cumulative_volume
        return None

    # 수급 정보
    foreign_net_buy: int = 0
    inst_net_buy: int = 0
    investor_score: int = 0
    inv_flip_score: float = 0.0  # 외인+기관 동시 매수 전환 신선도 (0~1, 30분 감쇠)

    # 추세 상태 (Yosep 신호)
    trend_level: int = 0  # 추세 단계 0~3
    trend_prev_level: int = 0  # 직전 추세 단계

    # 기타 지표
    chejan_strength: float = 100.0  # 체결강도
    chejan_history: list[float] = field(default_factory=list) # 체결강도 히스토리
    rs_score: float = 0.0           # 지수 대비 강도 (Stock% - Index%)
    exec_velocity_ratio: float = 0.0 # [NEW] 체결 가속도 (10초 체결량 / 1분 평균 10초량)
    sl_triggered_at: Optional[datetime] = None # [NEW] 손절가 하회 시작 시각

    # [2026-06-02] 60분봉 데이터
    h1_closes: list[float] = field(default_factory=list)
    h1_highs:  list[float] = field(default_factory=list)
    h1_lows:   list[float] = field(default_factory=list)
    h1_trend:  int  = 0     # 60분봉 trend_lv (0~3)
    h1_slope:  float = 0.0  # 60분봉 EMA10 기울기 (양수=상승)
    h1_rsi:    Optional[float] = None  # 60분봉 RSI

    # [2026-06-02] 멀티타임프레임 추세
    mtf_aligned: bool = False       # 1분봉·5분봉 추세 방향 일치 여부
    mtf_tf1_slope: float = 0.0     # 1분봉 EMA10 기울기
    mtf_tf5_slope: float = 0.0     # 5분봉 EMA10 기울기
    mtf_tf1_trend: int = 0         # 1분봉 trend_lv
    mtf_tf5_trend: int = 0         # 5분봉 trend_lv
    mtf_tf5_bars: int = 0          # 사용 가능한 5분봉 수

    @property
    def foreign_net(self) -> int: return self.foreign_net_buy
    @property
    def inst_net(self) -> int: return self.inst_net_buy


@dataclass
class ScanSignal:
    """신호 이벤트 — 스캐너가 발행하는 매수 신호"""

    code: str
    name: str
    signal_type: str  # "breakout", "jdm_entry", "opening_surge", ...
    reason: str  # 신호 발생 사유 (필터 통과 이유)
    price: int  # 신호 발생 시 가격
    is_warmup: bool = False  # [NEW] 워밍업 구간 발생 여부

    # 추가 메타 데이터
    values: dict = field(default_factory=dict)  # RSI, EMA 등 추가 정보
    emitted_at: datetime = field(default_factory=datetime.now)
