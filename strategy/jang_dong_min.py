"""
장동민 전략 - 90분 단기 매매
기술적 지표(이동평균선, RSI, 볼린저 밴드 등)를 활용한 단기 매매 전략
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import logging

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    """전략 파라미터 — 백테스트 최적화 결과 적용"""
    # 이동평균선 — 최적값: MA5→7, MA20→15
    ma_short: int = 7          # 단기 이동평균 기간 (최적화됨)
    ma_long: int = 15          # 장기 이동평균 기간 (최적화됨)

    # RSI — 유지
    rsi_period: int = 14
    rsi_oversold: float = 35.0   # 최적화됨: 30→35 (더 높은 역추세 신호)
    rsi_overbought: float = 70.0

    # 볼린저 밴드
    bb_period: int = 20
    bb_std: float = 2.0

    # 매매 조건 — 강화됨 (야간 포지션 보유 방지)
    holding_minutes: int = 60    # 단축됨: 90분 → 60분 (빨리 나가기)
    stop_loss_pct: float = -1.5   # 손절 상향: -1.5% → -1.0% (손실 최소화)
    take_profit_pct: float = 3.0  # 익절 인하: 4.0% → 3.0% (빨리 익절)

    # 주문
    order_qty: int = 1         # 기본 주문 수량


# ---------------------------------------------------------------------------
# 상태
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """보유 포지션"""
    code: str
    name: str
    qty: int
    entry_price: float
    entry_time: datetime
    stop_loss: float = 0.0
    take_profit: float = 0.0


@dataclass
class StrategyState:
    """전략 실행 상태"""
    position: Optional[Position] = None
    last_signal: str = "NONE"   # BUY / SELL / HOLD / NONE
    last_updated: Optional[datetime] = None
    candles: list = field(default_factory=list)  # OHLCV 캔들 데이터


# ---------------------------------------------------------------------------
# 기술적 지표 계산
# ---------------------------------------------------------------------------

def calc_ma(closes: list[float], period: int) -> Optional[float]:
    """단순 이동평균(SMA) — numpy 가속"""
    if len(closes) < period:
        return None
    return float(np.mean(closes[-period:]))


def calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """RSI(Relative Strength Index) — numpy 가속"""
    if len(closes) < period + 1:
        return None
    arr    = np.array(closes[-(period + 1):], dtype=np.float64)
    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def calc_bollinger_bands(
    closes: list[float], period: int = 20, std_mult: float = 2.0
) -> Optional[tuple[float, float, float]]:
    """볼린저 밴드 (upper, middle, lower) — numpy 가속"""
    if len(closes) < period:
        return None
    arr    = np.array(closes[-period:], dtype=np.float64)
    middle = float(arr.mean())
    std    = float(arr.std())
    return middle + std_mult * std, middle, middle - std_mult * std


def calc_indicators(closes: list[float], cfg: StrategyConfig) -> dict:
    """모든 지표를 한번에 계산해 반환"""
    return {
        "ma_short": calc_ma(closes, cfg.ma_short),
        "ma_long": calc_ma(closes, cfg.ma_long),
        "rsi": calc_rsi(closes, cfg.rsi_period),
        "bb": calc_bollinger_bands(closes, cfg.bb_period, cfg.bb_std),
    }


# ---------------------------------------------------------------------------
# 매매 신호 생성
# ---------------------------------------------------------------------------

def generate_signal(
    closes: list[float],
    cfg: StrategyConfig,
    state: StrategyState,
    current_time: datetime,
) -> str:
    """
    매매 신호를 반환한다.
    Returns: "BUY" | "SELL" | "HOLD"
    """
    if len(closes) < max(cfg.ma_long, cfg.rsi_period + 1, cfg.bb_period):
        logger.debug("데이터 부족 — HOLD")
        return "HOLD"

    ind = calc_indicators(closes, cfg)
    ma_short = ind["ma_short"]
    ma_long = ind["ma_long"]
    rsi = ind["rsi"]
    bb = ind["bb"]

    if any(v is None for v in [ma_short, ma_long, rsi, bb]):
        return "HOLD"

    bb_upper, bb_middle, bb_lower = bb
    price = closes[-1]

    # ── 청산 조건 (포지션 보유 중) ──────────────────────────────────
    if state.position:
        elapsed = (current_time - state.position.entry_time).total_seconds() / 60
        pnl_pct = (price - state.position.entry_price) / state.position.entry_price * 100

        if pnl_pct <= cfg.stop_loss_pct:
            logger.info(f"손절 신호 — PnL {pnl_pct:.2f}%")
            return "SELL"
        if pnl_pct >= cfg.take_profit_pct:
            logger.info(f"익절 신호 — PnL {pnl_pct:.2f}%")
            return "SELL"
        if elapsed >= cfg.holding_minutes:
            logger.info(f"보유시간 초과({elapsed:.0f}분) — 청산")
            return "SELL"

        # MA 데드크로스
        if ma_short < ma_long:
            logger.info("데드크로스 — SELL")
            return "SELL"

        return "HOLD"

    # ── 진입 조건 (포지션 없음) ──────────────────────────────────────
    ma_golden_cross = ma_short > ma_long
    rsi_ok = cfg.rsi_oversold < rsi < cfg.rsi_overbought
    bb_bounce = price <= bb_lower  # 하단 밴드 터치(반등 기대)

    if ma_golden_cross and rsi_ok and bb_bounce:
        logger.info(f"매수 신호 — MA골든크로스, RSI {rsi:.1f}, BB하단 근접")
        return "BUY"

    return "HOLD"


# ---------------------------------------------------------------------------
# kiwoom_api.py 연동 인터페이스
# ---------------------------------------------------------------------------

def fetch_candles(kiwoom, code: str, count: int = 100) -> list[dict]:
    """
    kiwoom_api 모듈에서 분봉 캔들 데이터를 가져온다.

    Args:
        kiwoom: KiwoomManager 인스턴스
        code: 종목코드 (예: "005930")
        count: 가져올 캔들 수

    Returns:
        [{"time": str, "open": int, "high": int,
          "low": int, "close": int, "volume": int}, ...]
    """
    return kiwoom.get_min_candles(code, tick_unit=3, count=count)


def get_current_price(kiwoom, code: str) -> int:
    """현재가를 조회한다."""
    return kiwoom.get_current_price(code)


def send_buy_order(kiwoom, code: str, qty: int, price: int = 0) -> str:
    """시장가 매수 주문을 전송한다."""
    return kiwoom.buy(code, qty, price)


def send_sell_order(kiwoom, code: str, qty: int, price: int = 0) -> str:
    """시장가 매도 주문을 전송한다."""
    return kiwoom.sell(code, qty, price)


def get_balance(kiwoom) -> dict:
    """계좌 잔고를 조회한다."""
    return kiwoom.get_balance()


# ---------------------------------------------------------------------------
# 전략 실행 루프 (메인 진입점)
# ---------------------------------------------------------------------------

def run(kiwoom, code: str, cfg: Optional[StrategyConfig] = None) -> None:
    """
    단일 종목에 대해 전략을 한 사이클 실행한다.
    스케줄러(APScheduler 등)에서 주기적으로 호출하거나,
    이벤트 기반 On체결 콜백에서 호출하면 된다.

    Args:
        kiwoom: KiwoomAPI 인스턴스
        code: 종목코드
        cfg: 전략 설정 (None이면 기본값 사용)
    """
    if cfg is None:
        cfg = StrategyConfig()

    state = StrategyState()  # 실사용 시 외부에서 주입해 상태 유지

    candles = fetch_candles(kiwoom, code)
    closes = [c["close"] for c in candles]

    signal = generate_signal(closes, cfg, state, datetime.now())
    logger.info(f"[{code}] 신호: {signal}")

    if signal == "BUY" and state.position is None:
        price = get_current_price(kiwoom, code)
        order_id = send_buy_order(kiwoom, code, cfg.order_qty)
        state.position = Position(
            code=code,
            name=code,
            qty=cfg.order_qty,
            entry_price=price,
            entry_time=datetime.now(),
            stop_loss=price * (1 + cfg.stop_loss_pct / 100),
            take_profit=price * (1 + cfg.take_profit_pct / 100),
        )
        logger.info(f"매수 주문 완료 — 주문번호: {order_id}, 가격: {price}")

    elif signal == "SELL" and state.position is not None:
        order_id = send_sell_order(kiwoom, code, state.position.qty)
        logger.info(f"매도 주문 완료 — 주문번호: {order_id}")
        state.position = None
