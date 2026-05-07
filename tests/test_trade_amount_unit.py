"""거래대금 단위 보정 로직 검증 테스트"""
import pytest
from unittest.mock import MagicMock, patch
from scanner.smart_scanner import SmartScanner, SmartScannerConfig


def calculate_trade_amount_unit(raw_cum_amt: int, price: int, cum_vol: int) -> int:
    """거래대금 단위 보정 로직 (smart_scanner.py 821~839 복제)"""
    if raw_cum_amt <= 0:
        return 0

    est_amt = price * cum_vol
    cand_1k = raw_cum_amt * 1_000
    cand_1m = raw_cum_amt * 1_000_000

    diff_1k = abs(est_amt - cand_1k)
    diff_1m = abs(est_amt - cand_1m)

    real_trade_amt = cand_1m if diff_1m < diff_1k else cand_1k
    return real_trade_amt


class TestTradeAmountUnit:
    """거래대금 단위 보정 테스트"""

    def test_samsung_electronics_morning(self):
        """삼성전자 장초반: 현재가 70,000, 거래량 500만, 거래대금 3,500억"""
        price = 70_000
        cum_vol = 5_000_000
        expected_trade_amt = 350_000_000_000  # 3,500억

        # FID 13에서 천원 단위로 왔다면: 350,000,000 / 1,000 = 350,000
        raw_cum_amt = 350_000_000

        result = calculate_trade_amount_unit(raw_cum_amt, price, cum_vol)
        assert result == expected_trade_amt, f"예상: {expected_trade_amt}, 실제: {result}"
        print(f"삼성전자 아침: {result:,}원 (3,500억원 기대)")

    def test_sk_hynix(self):
        """SK하이닉스: 현재가 200,000, 거래량 800만, 거래대금 1,600억"""
        price = 200_000
        cum_vol = 8_000_000
        expected_trade_amt = 1_600_000_000_000  # 1,600억

        raw_cum_amt = 1_600_000_000
        result = calculate_trade_amount_unit(raw_cum_amt, price, cum_vol)
        assert result == expected_trade_amt, f"예상: {expected_trade_amt}, 실제: {result}"
        print(f"SK하이닉스: {result:,}원 (1,600억원 기대)")

    def test_low_price_stock(self):
        """저가주: 현재가 5,000, 거래량 1,000만, 거래대금 500억"""
        price = 5_000
        cum_vol = 10_000_000
        expected_trade_amt = 500_000_000_000  # 500억

        raw_cum_amt = 500_000_000
        result = calculate_trade_amount_unit(raw_cum_amt, price, cum_vol)
        assert result == expected_trade_amt, f"예상: {expected_trade_amt}, 실제: {result}"
        print(f"저가주: {result:,}원 (500억원 기대)")

    def test_zero_trade_amount(self):
        """거래대금 0"""
        result = calculate_trade_amount_unit(0, 50_000, 1_000_000)
        assert result == 0
        print(f"거래대금 0: {result}")

    def test_negative_raw_amount(self):
        """음수 거래대금"""
        result = calculate_trade_amount_unit(-100, 50_000, 1_000_000)
        assert result == 0
        print(f"음수 거래대금: {result}")


def test_format_trade_amount_korean():
    """거래대금 한글 표기 검증"""
    from scanner.universe import format_trade_amount_korean

    test_cases = [
        (350_000_000_000, "3,500억"),  # 350billion = 3,500 × 100million
        (1_600_000_000_000, "1.6조"),
        (500_000_000_000, "5,000억"),  # 500billion = 5,000 × 100million
        (10_000_000_000, "100억"),  # 10billion = 100 × 100million
        (100_000_000, "1억"),
    ]

    for amount, expected in test_cases:
        result = format_trade_amount_korean(amount)
        print(f"  {amount:,} → {result} (예상: {expected})")
        assert result == expected, f"거래대금 표기 오류: {result} != {expected}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
