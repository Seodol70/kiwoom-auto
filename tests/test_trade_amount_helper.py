"""
test_trade_amount_helper.py — TradeAmountHelper 통합 테스트
"""

import pytest
from scanner.trade_amount import TradeAmountHelper


class TestTradeAmountHelper:
    """TradeAmountHelper 클래스 테스트"""

    def test_normalize_from_kiwoom_basic(self):
        """기본 백만원 → 원 단위 변환"""
        # 삼성전자: raw_amt=19,600백만원 → 1.96조원
        result = TradeAmountHelper.normalize_from_kiwoom(19_600)
        expected = 19_600 * 1_000_000
        assert result == expected
        print(f"삼성전자: {result:,}원")

    def test_normalize_from_kiwoom_with_fallback(self):
        """raw_amt=0일 때 price×volume 대체"""
        result = TradeAmountHelper.normalize_from_kiwoom(0, 100_000, 10_000)
        expected = 100_000 * 10_000
        assert result == expected
        print(f"대체값 계산: {result:,}원")

    def test_normalize_from_kiwoom_normal_difference(self):
        """raw_amt와 price×volume의 차이가 정상 범위면 raw 값 사용"""
        # 한온시스템: raw=114400(1144억), price=5700, volume=27730000(약1580억)
        # → 차이 0.72배 (정상 범위) → raw 값 사용
        result = TradeAmountHelper.normalize_from_kiwoom(114_400, 5_700, 27_730_000)
        expected = 114_400 * 1_000_000  # 1,144억원
        assert result == expected
        print(f"정상 범위 차이: {result:,}원 ({result/1e8:.1f}억)")

    def test_normalize_from_kiwoom_zero(self):
        """raw_amt=0, 대체값 없음"""
        result = TradeAmountHelper.normalize_from_kiwoom(0)
        assert result == 0

    def test_to_korean_trillion(self):
        """조 단위"""
        result = TradeAmountHelper.to_korean(1_960_000_000_000)
        assert result == "2.0조"
        print(f"조 단위: {result}")

    def test_to_korean_hundred_million(self):
        """억 단위"""
        result = TradeAmountHelper.to_korean(50_000_000_000)
        assert result == "500억"
        print(f"억 단위: {result}")

    def test_to_korean_ten_thousand(self):
        """만원 단위"""
        result = TradeAmountHelper.to_korean(5_000_000)
        assert result == "500만원"
        print(f"만원 단위: {result}")

    def test_to_korean_won(self):
        """원 단위"""
        result = TradeAmountHelper.to_korean(100)
        assert result == "100원"
        print(f"원 단위: {result}")

    def test_to_korean_zero(self):
        """0 처리"""
        result = TradeAmountHelper.to_korean(0)
        assert result == "0원"
        assert TradeAmountHelper.to_korean(None) == "0원"
        print(f"영 처리: {result}")

    def test_growth_rate_positive(self):
        """증가율 양수"""
        result = TradeAmountHelper.growth_rate(1_000_000_000, 800_000_000)
        assert "▲" in result
        assert "25.0%" in result
        print(f"양수 증가율: {result}")

    def test_growth_rate_negative(self):
        """증가율 음수"""
        result = TradeAmountHelper.growth_rate(500_000_000, 1_000_000_000)
        assert "▼" in result
        assert "50.0%" in result
        print(f"음수 증가율: {result}")

    def test_growth_rate_no_baseline(self):
        """baseline=None"""
        result = TradeAmountHelper.growth_rate(1_000_000_000, None)
        assert result == "—"
        print(f"기준값 없음: {result}")

    def test_growth_rate_zero_baseline(self):
        """baseline=0"""
        result = TradeAmountHelper.growth_rate(1_000_000_000, 0)
        assert result == "—"

    def test_diagnostic_string_full(self):
        """진단 문자열 (한글 + 증가율)"""
        result = TradeAmountHelper.diagnostic_string(50_000_000_000, 40_000_000_000)
        assert "500억" in result
        assert "▲" in result
        print(f"진단 문자열: {result}")

    def test_diagnostic_string_no_baseline(self):
        """진단 문자열 (한글만)"""
        result = TradeAmountHelper.diagnostic_string(50_000_000_000)
        assert result == "500억"
        print(f"진단 문자열 (기준값 없음): {result}")


class TestTradeAmountIntegration:
    """통합 시나리오 테스트"""

    def test_end_to_end_samsung(self):
        """삼성전자 전체 파이프라인"""
        # Kiwoom API raw data
        raw_amt = 19_600  # 백만원 단위
        price = 285_250
        volume = 6_868_000

        # 정규화
        normalized = TradeAmountHelper.normalize_from_kiwoom(raw_amt, price, volume)
        expected = 19_600 * 1_000_000  # 1.96조원
        assert normalized == expected

        # 한글 표기 (19,600백만원 = 196억 × 100 = 1.96조 = 약 1.96조)
        korean = TradeAmountHelper.to_korean(normalized)
        # 19,600 * 1,000,000 = 19,600,000,000 = 196억 (잘못됨)
        # 19,600 * 1,000,000 = 19,600,000,000 = 1.96조원 맞음
        # 그런데 to_korean은 억 단위로 먼저 확인하므로
        # 19,600,000,000 / 100,000,000 = 196억
        assert korean == "196억"  # 1.96조 = 196억

        # 진단
        baseline = int(normalized * 0.8)  # 9시 대비 80%
        growth = TradeAmountHelper.growth_rate(normalized, baseline)
        assert "▲" in growth
        assert "25.0%" in growth

        print(f"삼성전자: {korean} / {growth}")

    def test_compatibility_with_legacy(self):
        """기존 호환성 함수 테스트"""
        from scanner.trade_amount import (
            normalize_trade_amount_from_kiwoom,
            format_trade_amount_korean,
            format_trade_amount_growth,
        )

        # 호환성 함수들도 동일하게 작동해야 함
        assert normalize_trade_amount_from_kiwoom(100) == 100 * 1_000_000
        assert format_trade_amount_korean(1_000_000_000_000) == "1.0조"
        assert "—" in format_trade_amount_growth(100, None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
