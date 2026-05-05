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
    
    # 호가 잔량
    total_ask_qty: int = 0
    total_bid_qty: int = 0
    
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
    
    # 호가 잔량
    total_ask_qty: int = 0
    total_bid_qty: int = 0

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

    # 추세 상태 (Yosep 신호)
    trend_level: int = 0  # 추세 단계 0~3
    trend_prev_level: int = 0  # 직전 추세 단계

    # 기타 지표
    chejan_strength: float = 100.0  # 체결강도
    chejan_history: list[float] = field(default_factory=list) # 체결강도 히스토리
    rs_score: float = 0.0           # 지수 대비 강도 (Stock% - Index%)
    sl_triggered_at: Optional[datetime] = None # [NEW] 손절가 하회 시작 시각

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

    # 추가 메타 데이터
    values: dict = field(default_factory=dict)  # RSI, EMA 등 추가 정보
    emitted_at: datetime = field(default_factory=datetime.now)
