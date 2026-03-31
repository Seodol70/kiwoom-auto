"""
백테스트 엔진

사용 예)
    from backtest.engine import BacktestEngine
    from strategy.jang_dong_min import StrategyConfig, generate_signal

    def my_strategy(closes, idx):
        cfg = StrategyConfig()
        from strategy.jang_dong_min import StrategyState
        return generate_signal(closes[:idx+1], cfg, StrategyState(), datetime.now())

    engine = BacktestEngine(initial_capital=10_000_000)
    result = engine.run(df, my_strategy)
    print(result.metrics)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """단일 매매 기록"""
    entry_date: pd.Timestamp
    exit_date:  pd.Timestamp
    entry_price: float
    exit_price:  float
    qty:         int
    side:        str          # "LONG"
    pnl:         float        # 수수료 차감 후 손익 (원)
    pnl_pct:     float        # 수익률 (%)
    exit_reason: str          # "SIGNAL" | "STOP_LOSS" | "TAKE_PROFIT" | "FORCE_CLOSE"


@dataclass
class BacktestResult:
    """백테스트 전체 결과"""
    trades:       list[Trade]
    equity_curve: pd.Series        # 날짜별 자산 가치
    drawdown:     pd.Series        # 날짜별 낙폭 (%)
    metrics:      dict
    signals:      pd.Series        # 날짜별 매매 신호


# ---------------------------------------------------------------------------
# 백테스트 엔진
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    단일 종목 롱 온리 백테스트 엔진.

    Args:
        initial_capital: 초기 자본 (원)
        fee_rate:        편도 수수료율 (기본 0.015% — 키움 기준)
        tax_rate:        매도 시 거래세율 (기본 0.18% — 코스피 기준)
        slippage:        슬리피지 — 체결가 보정 비율 (기본 0.05%)
        stop_loss_pct:   손절 기준 (%) — 음수 입력. None 이면 비활성
        take_profit_pct: 익절 기준 (%) — 양수 입력. None 이면 비활성
    """

    def __init__(
        self,
        initial_capital: float = 10_000_000,
        fee_rate: float = 0.00015,
        tax_rate: float = 0.0018,
        slippage: float = 0.0005,
        stop_loss_pct: Optional[float] = -2.0,
        take_profit_pct: Optional[float] = 3.0,
    ) -> None:
        self.initial_capital = initial_capital
        self.fee_rate        = fee_rate
        self.tax_rate        = tax_rate
        self.slippage        = slippage
        self.stop_loss_pct   = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    # -----------------------------------------------------------------------
    # 실행
    # -----------------------------------------------------------------------

    def run(
        self,
        data: pd.DataFrame,
        strategy_fn: Callable[[list[float], int], str],
    ) -> BacktestResult:
        """
        백테스트를 실행한다.

        Args:
            data: OHLCV DataFrame.
                  필수 컬럼: Open, High, Low, Close, Volume
                  인덱스: DatetimeIndex
            strategy_fn: 전략 함수.
                  (closes: list[float], current_idx: int) -> "BUY" | "SELL" | "HOLD"

        Returns:
            BacktestResult
        """
        self._validate(data)
        closes = data["Close"].tolist()

        capital   = self.initial_capital
        position  = 0          # 보유 수량
        entry_px  = 0.0        # 매수 단가
        entry_dt  = None

        trades:       list[Trade]  = []
        equity:       list[float]  = []
        signals_out:  list[str]    = []

        for i, (dt, row) in enumerate(data.iterrows()):
            signal = strategy_fn(closes, i)

            # ── 손절 / 익절 자동 처리 ──
            if position > 0:
                cur_pnl_pct = (row["Close"] - entry_px) / entry_px * 100
                if self.stop_loss_pct and cur_pnl_pct <= self.stop_loss_pct:
                    signal = "_STOP_LOSS"
                elif self.take_profit_pct and cur_pnl_pct >= self.take_profit_pct:
                    signal = "_TAKE_PROFIT"

            # ── 매수 ──
            if signal == "BUY" and position == 0:
                buy_px  = row["Close"] * (1 + self.slippage)
                fee     = buy_px * self.fee_rate
                qty     = int(capital // (buy_px + fee))
                if qty > 0:
                    capital  -= qty * (buy_px + fee)
                    position  = qty
                    entry_px  = buy_px
                    entry_dt  = dt

            # ── 매도 (신호 / 손절 / 익절) ──
            elif signal in ("SELL", "_STOP_LOSS", "_TAKE_PROFIT") and position > 0:
                sell_px    = row["Close"] * (1 - self.slippage)
                fee        = sell_px * self.fee_rate
                tax        = sell_px * self.tax_rate
                proceeds   = position * (sell_px - fee - tax)
                pnl        = proceeds - position * entry_px
                pnl_pct    = pnl / (position * entry_px) * 100
                capital   += proceeds

                reason_map = {
                    "SELL":          "SIGNAL",
                    "_STOP_LOSS":    "STOP_LOSS",
                    "_TAKE_PROFIT":  "TAKE_PROFIT",
                }
                trades.append(Trade(
                    entry_date=entry_dt, exit_date=dt,
                    entry_price=entry_px, exit_price=sell_px,
                    qty=position, side="LONG",
                    pnl=pnl, pnl_pct=round(pnl_pct, 4),
                    exit_reason=reason_map.get(signal, "SIGNAL"),
                ))
                position = 0
                entry_px = 0.0

            # ── 자산 평가 ──
            stock_val = position * row["Close"]
            equity.append(capital + stock_val)
            signals_out.append(signal if signal in ("BUY", "SELL") else "HOLD")

        # 마지막 날 강제 청산
        if position > 0:
            last_row   = data.iloc[-1]
            sell_px    = last_row["Close"] * (1 - self.slippage)
            fee        = sell_px * self.fee_rate
            tax        = sell_px * self.tax_rate
            proceeds   = position * (sell_px - fee - tax)
            pnl        = proceeds - position * entry_px
            pnl_pct    = pnl / (position * entry_px) * 100
            capital   += proceeds
            equity[-1] = capital
            trades.append(Trade(
                entry_date=entry_dt, exit_date=data.index[-1],
                entry_price=entry_px, exit_price=sell_px,
                qty=position, side="LONG",
                pnl=pnl, pnl_pct=round(pnl_pct, 4),
                exit_reason="FORCE_CLOSE",
            ))

        equity_s  = pd.Series(equity, index=data.index, name="equity")
        dd_s      = self._calc_drawdown(equity_s)
        signals_s = pd.Series(signals_out, index=data.index, name="signal")

        from backtest.metrics import calc_metrics
        metrics = calc_metrics(
            trades=trades,
            equity=equity_s,
            drawdown=dd_s,
            initial_capital=self.initial_capital,
            data=data,
        )

        return BacktestResult(
            trades=trades,
            equity_curve=equity_s,
            drawdown=dd_s,
            metrics=metrics,
            signals=signals_s,
        )

    # -----------------------------------------------------------------------
    # 내부 유틸
    # -----------------------------------------------------------------------

    @staticmethod
    def _validate(data: pd.DataFrame) -> None:
        required = {"Open", "High", "Low", "Close", "Volume"}
        missing  = required - set(data.columns)
        if missing:
            raise ValueError(f"OHLCV 컬럼 누락: {missing}")
        if not isinstance(data.index, pd.DatetimeIndex):
            raise TypeError("DataFrame 인덱스는 DatetimeIndex 이어야 합니다.")

    @staticmethod
    def _calc_drawdown(equity: pd.Series) -> pd.Series:
        peak = equity.cummax()
        return ((equity - peak) / peak * 100).rename("drawdown")
