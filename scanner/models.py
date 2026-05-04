"""
Scanner 도메인 데이터 모델

SnapshotStore, IndicatorService, SignalEvaluator 등 여러 모듈이 사용하는
dataclass를 중앙 정의. 순환 import 방지.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


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
    highs_1min: list[float] = field(default_factory=list)
    lows_1min: list[float] = field(default_factory=list)
    volumes_1min: list[int] = field(default_factory=list)

    # 수급 정보
    foreign_net_buy: int = 0
    inst_net_buy: int = 0
    investor_score: int = 0

    # 추세 상태 (Yosep 신호)
    trend_level: int = 0  # 추세 단계 0~3
    trend_prev_level: int = 0  # 직전 추세 단계

    # 기타 지표
    chejan_strength: float = 100.0  # 체결강도

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
