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

    def process_tick(self, code: str, price: float, cum_volume: int) -> list[MinuteCandle]:
        """
        새로운 틱을 처리하고, 완성된 모든 분봉(공백 포함)을 리스트로 반환한다.
        """
        now = datetime.now()
        cur_min = now.hour * 100 + now.minute
        
        prev_min = self._last_min.get(code, -1)
        completed_candles: list[MinuteCandle] = []

        # 1. 첫 수신이거나 분이 바뀐 경우
        if prev_min != -1 and prev_min != cur_min:
            # [Gap Filling] 마지막 틱 시각과 현재 시각 사이의 빈 분봉들을 메운다.
            last_candle = self._cur_candle.get(code)
            if last_candle:
                # 1) 마지막 진행 중이던 봉 완성
                completed_candles.append(last_candle)
                self._last_cum_vol[code] = last_candle.cum_volume
                
                # 2) 중간에 빈 분(Minute)들 채우기 (09:30 -> 09:33 이라면 931, 932 채움)
                # 시/분 계산을 위해 datetime 객체로 변환하여 순회
                temp_time = datetime(now.year, now.month, now.day, prev_min // 100, prev_min % 100)
                from datetime import timedelta
                
                while True:
                    temp_time += timedelta(minutes=1)
                    temp_min = temp_time.hour * 100 + temp_time.minute
                    if temp_min >= cur_min:
                        break
                    
                    # 거래량 0인 허수 캔들 생성 (직전 종가 유지)
                    gap_candle = MinuteCandle(
                        code=code,
                        time_key=temp_min,
                        open=last_candle.close,
                        high=last_candle.close,
                        low=last_candle.close,
                        close=last_candle.close,
                        volume=0,
                        cum_volume=last_candle.cum_volume
                    )
                    completed_candles.append(gap_candle)

            # 새 분봉 시작
            self._last_min[code] = cur_min
            self._cur_candle[code] = MinuteCandle(
                code=code,
                time_key=cur_min,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=max(0, cum_volume - self._last_cum_vol.get(code, cum_volume)),
                cum_volume=cum_volume
            )
            
        elif prev_min == -1:
            # 최초 수신
            self._last_min[code] = cur_min
            self._cur_candle[code] = MinuteCandle(
                code=code,
                time_key=cur_min,
                open=price, high=price, low=price, close=price,
                volume=0, cum_volume=cum_volume
            )
            self._last_cum_vol[code] = cum_volume
            
        else:
            # 같은 분 내의 틱 업데이트
            candle = self._cur_candle[code]
            candle.high = max(candle.high, price)
            candle.low = min(candle.low, price)
            candle.close = price
            
            # 거래량 업데이트
            last_vol = self._last_cum_vol.get(code, cum_volume)
            candle.volume = max(0, cum_volume - last_vol)
            candle.cum_volume = cum_volume

        return completed_candles

    def set_initial_state(self, code: str, last_min_key: int, last_cum_vol: int):
        """캐시 로드 시 마지막 분봉 상태를 동기화하여 연속성을 보장한다."""
        self._last_min[code] = last_min_key
        self._last_cum_vol[code] = last_cum_vol
        # 현재 진행 중인 봉은 아직 없으므로 다음 틱에서 생성됨

    def get_current_candle(self, code: str) -> Optional[MinuteCandle]:
        """현재 진행 중인(미완성) 분봉을 반환"""
        return self._cur_candle.get(code)

    def cleanup_stale_data(self, active_codes: set[str]) -> None:
        """active_codes에 없는 종목의 상태 데이터를 제거한다."""
        stale_min = set(self._last_min.keys()) - active_codes
        for c in stale_min: self._last_min.pop(c, None)

        stale_cur = set(self._cur_candle.keys()) - active_codes
        for c in stale_cur: self._cur_candle.pop(c, None)

        stale_vol = set(self._last_cum_vol.keys()) - active_codes
        for c in stale_vol: self._last_cum_vol.pop(c, None)
