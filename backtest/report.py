"""
백테스트 결과 리포트 생성 (Plotly)
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest.engine import BacktestResult


# ---------------------------------------------------------------------------
# 성과 지표 출력
# ---------------------------------------------------------------------------

def print_metrics(result: BacktestResult) -> None:
    """콘솔에 성과 지표 요약을 출력한다."""
    m = result.metrics
    divider = "─" * 44

    print(f"\n{'':=<44}")
    print(f"  백테스트 성과 요약")
    print(f"{'':=<44}")

    sections = [
        ("수익", [
            ("총 수익률",        f"{m['total_return_pct']:+.2f} %"),
            ("최종 자산",        f"{m['final_capital']:,.0f} 원"),
            ("CAGR (연복리)",    f"{m['cagr_pct']:+.2f} %"),
            ("매수보유 수익률",   f"{m['buy_hold_return_pct']:+.2f} %"),
            ("초과 수익률(α)",   f"{m['alpha_pct']:+.2f} %"),
        ]),
        ("리스크", [
            ("최대 낙폭(MDD)",   f"{m['mdd_pct']:.2f} %"),
            ("샤프 지수",        f"{m['sharpe_ratio']:.3f}"),
            ("소르티노 지수",    f"{m['sortino_ratio']:.3f}"),
            ("칼마 지수",        f"{m['calmar_ratio']:.3f}"),
        ]),
        ("거래 통계", [
            ("총 거래 수",       f"{m['total_trades']} 회"),
            ("승 / 패",          f"{m['win_count']} / {m['lose_count']}"),
            ("승률",             f"{m['win_rate_pct']:.2f} %"),
            ("평균 손익",        f"{m['avg_pnl']:+,.0f} 원"),
            ("평균 수익률",      f"{m['avg_win_pct']:+.2f} %"),
            ("평균 손실률",      f"{m['avg_loss_pct']:+.2f} %"),
            ("프로핏 팩터",      f"{m['profit_factor']:.3f}"),
        ]),
    ]

    for title, rows in sections:
        print(f"\n  [{title}]")
        print(f"  {divider}")
        for label, value in rows:
            print(f"  {label:<18} {value:>20}")

    print(f"\n{'':=<44}\n")


# ---------------------------------------------------------------------------
# 차트 생성
# ---------------------------------------------------------------------------

def build_chart(
    result: BacktestResult,
    price_data: pd.DataFrame,
    title: str = "백테스트 결과",
) -> go.Figure:
    """
    Plotly 4-패널 차트를 생성한다.
      1행: 가격 캔들 + 매매 시점 마커
      2행: 자산 곡선 vs 매수보유
      3행: 낙폭(Drawdown)
      4행: 개별 거래 손익 막대

    Args:
        result:     BacktestResult
        price_data: OHLCV DataFrame (backtest에 사용한 것과 동일)
        title:      차트 제목

    Returns:
        plotly Figure
    """
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.40, 0.25, 0.20, 0.15],
        vertical_spacing=0.03,
        subplot_titles=("가격 & 매매 시점", "자산 곡선", "낙폭 (%)", "개별 손익 (원)"),
    )

    dates = price_data.index

    # ── 1행: 캔들스틱 ─────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=dates,
        open=price_data["Open"],
        high=price_data["High"],
        low=price_data["Low"],
        close=price_data["Close"],
        name="가격",
        increasing_line_color="#EF553B",
        decreasing_line_color="#636EFA",
        showlegend=False,
    ), row=1, col=1)

    # 매매 시점 마커
    buy_trades  = [(t.entry_date, t.entry_price) for t in result.trades]
    sell_trades = [(t.exit_date,  t.exit_price)  for t in result.trades]

    if buy_trades:
        bd, bp = zip(*buy_trades)
        fig.add_trace(go.Scatter(
            x=list(bd), y=list(bp),
            mode="markers", name="매수",
            marker=dict(symbol="triangle-up", size=12, color="#00CC96"),
        ), row=1, col=1)

    if sell_trades:
        sd, sp = zip(*sell_trades)
        fig.add_trace(go.Scatter(
            x=list(sd), y=list(sp),
            mode="markers", name="매도",
            marker=dict(symbol="triangle-down", size=12, color="#EF553B"),
        ), row=1, col=1)

    # ── 2행: 자산 곡선 ────────────────────────────────────────────
    initial = result.equity_curve.iloc[0]

    fig.add_trace(go.Scatter(
        x=dates, y=result.equity_curve,
        mode="lines", name="전략",
        line=dict(color="#636EFA", width=2),
        fill="tozeroy", fillcolor="rgba(99,110,250,0.08)",
    ), row=2, col=1)

    bh_equity = price_data["Close"] / price_data["Close"].iloc[0] * initial
    fig.add_trace(go.Scatter(
        x=dates, y=bh_equity,
        mode="lines", name="매수보유",
        line=dict(color="#FFA15A", width=1.5, dash="dash"),
    ), row=2, col=1)

    # ── 3행: 낙폭 ────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=dates, y=result.drawdown,
        mode="lines", name="낙폭",
        line=dict(color="#EF553B", width=1),
        fill="tozeroy", fillcolor="rgba(239,85,59,0.15)",
        showlegend=False,
    ), row=3, col=1)

    # ── 4행: 개별 손익 막대 ──────────────────────────────────────
    trade_dates = [t.exit_date for t in result.trades]
    trade_pnls  = [t.pnl       for t in result.trades]
    trade_colors = ["#00CC96" if p > 0 else "#EF553B" for p in trade_pnls]

    fig.add_trace(go.Bar(
        x=trade_dates, y=trade_pnls,
        name="거래 손익",
        marker_color=trade_colors,
        showlegend=False,
    ), row=4, col=1)

    # ── 레이아웃 ─────────────────────────────────────────────────
    m = result.metrics
    subtitle = (
        f"총수익 {m['total_return_pct']:+.1f}%  |  "
        f"MDD {m['mdd_pct']:.1f}%  |  "
        f"샤프 {m['sharpe_ratio']:.2f}  |  "
        f"승률 {m['win_rate_pct']:.1f}%  ({m['total_trades']}회)"
    )

    fig.update_layout(
        title=dict(text=f"{title}<br><sup>{subtitle}</sup>", x=0.5),
        height=900,
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=80, b=40, l=60, r=40),
    )
    fig.update_yaxes(title_text="가격",    row=1, col=1)
    fig.update_yaxes(title_text="자산(원)", row=2, col=1)
    fig.update_yaxes(title_text="낙폭(%)", row=3, col=1)
    fig.update_yaxes(title_text="손익(원)", row=4, col=1)

    return fig


def save_report(
    result: BacktestResult,
    price_data: pd.DataFrame,
    path: str = "backtest_report.html",
    title: str = "백테스트 결과",
) -> None:
    """HTML 리포트 파일로 저장한다."""
    fig = build_chart(result, price_data, title)
    fig.write_html(path, include_plotlyjs="cdn")
    print(f"리포트 저장 완료: {path}")
