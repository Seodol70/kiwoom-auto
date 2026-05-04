"""
Snapshot — 실시간 틱 데이터의 분봉(1-minute Candle) 변환 및 관리
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class MinuteCandle:
    """1분봉 데이터 모델"""
    code: str
    time_key: int  # HHMM 형식의 시간키 (예: 930)
    open: float
    high: float
    low: float
    close: float
    volume: int    # 해당 분 내의 순수 거래량 (Delta)
    cum_volume: int # 해당 시점까지의 누적 거래량

class TickToCandleProcessor:
    """틱 데이터를 분봉 데이터로 변환 및 관리하는 클래스"""

    def __init__(self):
        self._last_min: dict[str, int] = {}        # code -> 마지막 기록된 분 (HHMM)
        self._cur_candle: dict[str, MinuteCandle] = {} # code -> 현재 진행 중인 분봉
        self._last_cum_vol: dict[str, int] = {}    # code -> 직전 분 경계의 누적 거래량

    def process_tick(self, code: str, price: float, cum_volume: int) -> Optional[MinuteCandle]:
        """
        새로운 틱을 처리하고, 분(Minute)이 바뀌었을 경우 완성된 이전 분봉을 반환한다.
        """
        now = datetime.now()
        cur_min = now.hour * 100 + now.minute
        
        # 1. 분이 바뀌었는지 확인
        prev_min = self._last_min.get(code, -1)
        completed_candle = None

        if prev_min != cur_min:
            # 분이 바뀌었으면 현재 진행 중인 봉을 완성본으로 간주
            if code in self._cur_candle:
                completed_candle = self._cur_candle[code]
                self._last_cum_vol[code] = completed_candle.cum_volume
            
            # 새 분봉 시작
            self._last_min[code] = cur_min
            self._cur_candle[code] = MinuteCandle(
                code=code,
                time_key=cur_min,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=0, # 첫 틱이므로 일단 0 (혹은 이전 누적과의 차이)
                cum_volume=cum_volume
            )
            
            # 분이 바뀌는 첫 틱의 거래량 계산
            last_vol = self._last_cum_vol.get(code, cum_volume)
            self._cur_candle[code].volume = max(0, cum_volume - last_vol)
            
        else:
            # 같은 분 내의 틱 업데이트
            candle = self._cur_candle[code]
            candle.high = max(candle.high, price)
            candle.low = min(candle.low, price)
            candle.close = price
            
            # 거래량 업데이트
            last_vol = self._last_cum_vol.get(code, candle.cum_volume)
            candle.volume = max(0, cum_volume - last_vol)
            candle.cum_volume = cum_volume

        return completed_candle

    def set_initial_state(self, code: str, last_min_key: int, last_cum_vol: int):
        """캐시 로드 시 마지막 분봉 상태를 동기화하여 연속성을 보장한다."""
        self._last_min[code] = last_min_key
        self._last_cum_vol[code] = last_cum_vol
        # 현재 진행 중인 봉은 아직 없으므로 다음 틱에서 생성됨

    def get_current_candle(self, code: str) -> Optional[MinuteCandle]:
        """현재 진행 중인(미완성) 분봉을 반환"""
        return self._cur_candle.get(code)
