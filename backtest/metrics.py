"""
백테스트 성과 지표 계산
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import Trade


def calc_metrics(
    trades: list[Trade],
    equity: pd.Series,
    drawdown: pd.Series,
    initial_capital: float,
    data: pd.DataFrame,
) -> dict:
    """
    거래 결과로부터 주요 성과 지표를 계산한다.

    Returns:
        {
            # 수익 관련
            "total_return_pct"    : float,  총 수익률(%)
            "final_capital"       : float,  최종 자산
            "cagr_pct"            : float,  연평균 수익률(%)

            # 리스크 관련
            "mdd_pct"             : float,  최대 낙폭(%)
            "sharpe_ratio"        : float,  샤프 지수(연간)
            "sortino_ratio"       : float,  소르티노 지수(연간)
            "calmar_ratio"        : float,  칼마 지수

            # 거래 통계
            "total_trades"        : int,
            "win_count"           : int,
            "lose_count"          : int,
            "win_rate_pct"        : float,  승률(%)
            "avg_pnl"             : float,  평균 손익(원)
            "avg_win_pct"         : float,  평균 수익률(%)
            "avg_loss_pct"        : float,  평균 손실률(%)
            "profit_factor"       : float,  프로핏 팩터

            # 벤치마크 대비
            "buy_hold_return_pct" : float,  매수보유 수익률(%)
            "alpha_pct"           : float,  초과 수익률(%)
        }
    """
    final_capital = equity.iloc[-1]
    total_return  = (final_capital - initial_capital) / initial_capital * 100

    # 기간(연)
    days  = (equity.index[-1] - equity.index[0]).days
    years = days / 365.0

    # CAGR
    cagr = ((final_capital / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0.0

    # MDD
    mdd = drawdown.min()

    # 일간 수익률
    daily_ret = equity.pct_change().dropna()

    # 샤프 (무위험 수익률 2% 가정)
    rf_daily  = 0.02 / 252
    excess    = daily_ret - rf_daily
    sharpe    = (excess.mean() / excess.std() * np.sqrt(252)) if excess.std() > 0 else 0.0

    # 소르티노
    neg_ret   = daily_ret[daily_ret < rf_daily] - rf_daily
    sortino   = (excess.mean() / neg_ret.std() * np.sqrt(252)) if len(neg_ret) > 0 and neg_ret.std() > 0 else 0.0

    # 칼마
    calmar = (cagr / abs(mdd)) if mdd != 0 else 0.0

    # 거래 통계
    total_trades = len(trades)
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    win_count  = len(wins)
    lose_count = len(losses)
    win_rate   = (win_count / total_trades * 100) if total_trades > 0 else 0.0

    avg_pnl      = np.mean([t.pnl     for t in trades]) if trades else 0.0
    avg_win_pct  = np.mean([t.pnl_pct for t in wins])   if wins   else 0.0
    avg_loss_pct = np.mean([t.pnl_pct for t in losses]) if losses else 0.0

    gross_profit = sum(t.pnl for t in wins)
    gross_loss   = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    # 매수보유 수익률 (벤치마크)
    bh_return = (data["Close"].iloc[-1] / data["Close"].iloc[0] - 1) * 100
    alpha     = total_return - bh_return

    return {
        "total_return_pct":    round(total_return,  2),
        "final_capital":       round(final_capital, 0),
        "cagr_pct":            round(cagr,          2),
        "mdd_pct":             round(mdd,            2),
        "sharpe_ratio":        round(sharpe,         3),
        "sortino_ratio":       round(sortino,        3),
        "calmar_ratio":        round(calmar,         3),
        "total_trades":        total_trades,
        "win_count":           win_count,
        "lose_count":          lose_count,
        "win_rate_pct":        round(win_rate,       2),
        "avg_pnl":             round(avg_pnl,        0),
        "avg_win_pct":         round(avg_win_pct,    2),
        "avg_loss_pct":        round(avg_loss_pct,   2),
        "profit_factor":       round(profit_factor,  3),
        "buy_hold_return_pct": round(bh_return,      2),
        "alpha_pct":           round(alpha,           2),
    }
