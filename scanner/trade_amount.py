"""
trade_amount.py — 거래대금 변환 및 포맷팅 통합 모듈

거래대금 관련 모든 계산/변환을 이 모듈에서 관리.
- 원본 데이터(백만원) → 원 단위 정규화
- 원 단위 → 한글 표기 (조·억·만원)
- 거래대금 증가율 계산
- 진단 로그 포맷팅
"""

from typing import Optional


class TradeAmountHelper:
    """거래대금 단위 변환 및 포맷팅 통합 클래스"""

    # 단위 상수
    UNIT_WON = 1
    UNIT_MILLION_WON = 1_000_000
    UNIT_EOK_WON = 100_000_000  # 억원
    UNIT_TRILLION_WON = 1_000_000_000_000  # 조원

    @staticmethod
    def normalize_from_kiwoom(
        raw_amt: int,
        fallback_price: int = 0,
        fallback_volume: int = 0
    ) -> int:
        """
        Kiwoom API 거래대금(백만원 단위) → 원 단위로 정규화.

        Args:
            raw_amt: Kiwoom API의 거래대금 (백만원 단위, FID 13/14 등)
            fallback_price: raw_amt ≤ 0일 때 대체값 (현재가)
            fallback_volume: raw_amt ≤ 0일 때 대체값 (거래량)

        Returns:
            거래대금 (원 단위)

        Examples:
            >>> TradeAmountHelper.normalize_from_kiwoom(19600, 0, 0)
            19600000000

            >>> TradeAmountHelper.normalize_from_kiwoom(0, 285250, 6868000)
            1959227000000  # 285250 × 6868000
        """
        if raw_amt <= 0:
            if fallback_price > 0 and fallback_volume > 0:
                return fallback_price * fallback_volume
            return 0

        # 키움 API 거래대금: 백만원 단위 → 원 단위
        calculated = raw_amt * TradeAmountHelper.UNIT_MILLION_WON

        # 대체값이 있으면 비교 검증
        # FID 13이 부정확하면 현가 × 거래량 사용
        if fallback_price > 0 and fallback_volume > 0:
            fallback_amount = fallback_price * fallback_volume
            # 비율이 100배 이상 (또는 1/100 이하) 차이나면 대체값 사용
            # (정상 범위는 0.8~1.2배 정도이지만, 시스템 오류는 그 이상)
            ratio = calculated / fallback_amount if fallback_amount > 0 else 0
            if ratio > 100 or ratio < 0.01:
                return fallback_amount

        return calculated

    @staticmethod
    def to_korean(amount_won: int) -> str:
        """
        원 단위 거래대금을 한글 표기로 변환 (조, 억, 만원, 원).

        Args:
            amount_won: 거래대금 (원 단위)

        Returns:
            한글 표기 문자열

        Examples:
            >>> TradeAmountHelper.to_korean(1960000000000)
            '2.0조'

            >>> TradeAmountHelper.to_korean(50000000000)
            '500억'

            >>> TradeAmountHelper.to_korean(5000000)
            '500만원'

            >>> TradeAmountHelper.to_korean(100)
            '100원'
        """
        n = int(amount_won or 0)
        if n <= 0:
            return "0원"

        if n >= TradeAmountHelper.UNIT_TRILLION_WON:
            return f"{n / TradeAmountHelper.UNIT_TRILLION_WON:.1f}조"
        if n >= TradeAmountHelper.UNIT_EOK_WON:
            return f"{n // TradeAmountHelper.UNIT_EOK_WON:,}억"
        if n >= 10_000:
            return f"{n // 10_000:,}만원"
        return f"{n:,}원"

    @staticmethod
    def growth_rate(current: int, baseline: Optional[int]) -> str:
        """
        거래대금 증가율 계산 (9시 기준값 대비).

        Args:
            current: 현재 거래대금 (원 단위)
            baseline: 기준 거래대금 (원 단위), None이면 '-'

        Returns:
            증가율 문자열 ("▲ 15.3%", "▼ 5.2%", "→ 0.0%", "—")

        Examples:
            >>> TradeAmountHelper.growth_rate(1000000000, 800000000)
            '▲ 25.0%'

            >>> TradeAmountHelper.growth_rate(500000000, 1000000000)
            '▼ 50.0%'

            >>> TradeAmountHelper.growth_rate(1000000000, None)
            '—'
        """
        if baseline is None or baseline <= 0:
            return "—"

        if current <= 0:
            return "0% (거래대금 없음)"

        growth_pct = (current - baseline) / baseline * 100

        if growth_pct > 0:
            arrow = "▲"
        elif growth_pct < 0:
            arrow = "▼"
        else:
            arrow = "→"

        return f"{arrow} {abs(growth_pct):.1f}%"

    @staticmethod
    def diagnostic_string(amount_won: int, baseline: Optional[int] = None) -> str:
        """
        진단용 통합 포맷 (한글 표기 + 증가율).

        Args:
            amount_won: 거래대금 (원 단위)
            baseline: 기준값 (9시 기준값 등), None이면 증가율 표시 안 함

        Returns:
            진단 문자열 ("50억 / ▲ 25.0%")

        Examples:
            >>> TradeAmountHelper.diagnostic_string(50000000000, 40000000000)
            '500억 / ▲ 25.0%'

            >>> TradeAmountHelper.diagnostic_string(50000000000)
            '500억'
        """
        korean = TradeAmountHelper.to_korean(amount_won)

        if baseline is None:
            return korean

        growth = TradeAmountHelper.growth_rate(amount_won, baseline)
        return f"{korean} / {growth}"


# ─── 하위 호환용 글로벌 함수들 ──────────────────────────────────────

def normalize_trade_amount_from_kiwoom(
    raw_amt: int,
    fallback_price: int = 0,
    fallback_volume: int = 0
) -> int:
    """[호환성] Kiwoom API 거래대금 정규화 (TradeAmountHelper 위임)"""
    return TradeAmountHelper.normalize_from_kiwoom(raw_amt, fallback_price, fallback_volume)


def format_trade_amount_korean(amount_won: int) -> str:
    """[호환성] 거래대금 한글 포맷팅 (TradeAmountHelper 위임)"""
    return TradeAmountHelper.to_korean(amount_won)


def format_trade_amount_growth(current: int, baseline: Optional[int]) -> str:
    """[호환성] 거래대금 증가율 (TradeAmountHelper 위임)"""
    return TradeAmountHelper.growth_rate(current, baseline)
