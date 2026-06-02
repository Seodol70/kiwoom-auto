"""
test_mtf_trend.py — MTF(멀티타임프레임) 추세 필터 단위 테스트

Qt 없이 실행 가능 (numpy만 필요).
"""

import pytest
from scanner.indicator_service import IndicatorService


class TestBuild5MinCloses:
    """1분봉 → 5분봉 집계 테스트"""

    def test_exact_multiple(self):
        """5의 배수 개수 — 모든 봉 완성"""
        closes = list(range(1, 16))  # 15개 → 5분봉 3개
        c5, v5 = IndicatorService.build_5min_closes(closes)
        assert len(c5) == 3
        # 각 봉의 종가 = 해당 구간 마지막 1분봉 종가
        assert c5[0] == 5
        assert c5[1] == 10
        assert c5[2] == 15

    def test_remainder_ignored(self):
        """나머지 봉은 포함하지 않음 (미완성 봉)"""
        closes = list(range(1, 18))  # 17개 → 완성 봉 3개, 나머지 2개 무시
        c5, _ = IndicatorService.build_5min_closes(closes)
        assert len(c5) == 3

    def test_too_few(self):
        """5개 미만 — 빈 리스트 반환"""
        c5, v5 = IndicatorService.build_5min_closes([100, 101, 102, 103])
        assert c5 == []
        assert v5 == []

    def test_volume_sum(self):
        """5분봉 거래량 = 1분봉 5개 합산"""
        closes = [100] * 10
        vols   = [10, 20, 30, 40, 50,  1, 2, 3, 4, 5]
        _, v5 = IndicatorService.build_5min_closes(closes, vols)
        assert v5[0] == 150   # 10+20+30+40+50
        assert v5[1] == 15    # 1+2+3+4+5

    def test_empty(self):
        """빈 입력"""
        c5, v5 = IndicatorService.build_5min_closes([])
        assert c5 == []


class TestGetMtfTrend:
    """MTF 추세 판정 테스트"""

    def _make_uptrend(self, n=30, start=100, step=1.0):
        """단조 상승 시계열"""
        return [start + i * step for i in range(n)]

    def _make_downtrend(self, n=30, start=130, step=1.0):
        """단조 하락 시계열"""
        return [start - i * step for i in range(n)]

    def test_both_uptrend_aligned(self):
        """1분봉·5분봉 모두 상승 → aligned=True"""
        closes = self._make_uptrend(n=30)
        result = IndicatorService.get_mtf_trend(closes)
        assert result["aligned"] is True
        assert result["tf1_slope"] > 0
        assert result["tf5_slope"] > 0

    def test_both_downtrend_not_aligned(self):
        """1분봉·5분봉 모두 하락 → aligned=False"""
        closes = self._make_downtrend(n=30)
        result = IndicatorService.get_mtf_trend(closes)
        assert result["aligned"] is False

    def test_1min_up_5min_down_not_aligned(self):
        """핵심 시나리오: 5분봉 강한 하락 중 1분봉 짧은 반등 → aligned=False (진입 차단 대상)"""
        # 5분봉 45개치(= 1분봉 45개) 강하게 하락, 마지막 2개만 소폭 반등
        # 5분봉 9개가 하락, 마지막 5분봉 1개만 반등 → 5분봉 EMA는 여전히 하락 기울기
        down_part = self._make_downtrend(n=45, start=200, step=2.0)  # 45봉 강하락
        up_part   = [down_part[-1] + 1, down_part[-1] + 2]           # 2봉 소반등
        closes = down_part + up_part  # 47개 → 5분봉 9개 완성
        result = IndicatorService.get_mtf_trend(closes)
        assert result["tf5_bars"] >= 9, f"5분봉 {result['tf5_bars']}개 — 9개 이상 필요"
        assert result["aligned"] is False, (
            f"5분 강하락 중 1분 소반등은 불일치여야 함 "
            f"tf1={result['tf1_slope']:+.2f} tf5={result['tf5_slope']:+.2f}"
        )

    def test_too_few_bars_no_block(self):
        """1분봉 10개 미만 → tf5_bars=0, aligned는 tf1만으로 판단"""
        closes = self._make_uptrend(n=8)
        result = IndicatorService.get_mtf_trend(closes)
        assert result["tf5_bars"] == 0
        # 데이터 부족이라 aligned 판단은 True 또는 False 상관없지만 예외 없어야 함

    def test_tf5_bars_count(self):
        """5분봉 개수 계산 정확성"""
        closes = self._make_uptrend(n=22)  # 22 // 5 = 4봉
        result = IndicatorService.get_mtf_trend(closes)
        assert result["tf5_bars"] == 4

    def test_slope_direction(self):
        """기울기 부호 검증"""
        up   = self._make_uptrend(n=20)
        down = self._make_downtrend(n=20)
        r_up   = IndicatorService.get_mtf_trend(up)
        r_down = IndicatorService.get_mtf_trend(down)
        assert r_up["tf1_slope"]   > 0
        assert r_down["tf1_slope"] < 0

    def test_with_volumes(self):
        """거래량 포함 시 정상 동작"""
        closes = self._make_uptrend(n=20)
        vols   = [1000] * 20
        result = IndicatorService.get_mtf_trend(closes, volumes_1min=vols)
        assert "aligned" in result

    def test_with_highs_lows(self):
        """highs/lows 포함 시 tf5_trend 계산 정상"""
        closes = self._make_uptrend(n=20)
        highs  = [c + 1 for c in closes]
        lows   = [c - 1 for c in closes]
        result = IndicatorService.get_mtf_trend(closes, highs_1min=highs, lows_1min=lows)
        assert result["tf5_trend"] >= 0

    def test_flat_market(self):
        """횡보 시장 — 기울기 ≈ 0"""
        closes = [100.0] * 20
        result = IndicatorService.get_mtf_trend(closes)
        # 기울기가 매우 작아야 함
        assert abs(result["tf1_slope"]) < 1.0
        assert abs(result["tf5_slope"]) < 1.0
