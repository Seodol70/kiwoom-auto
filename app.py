"""
키움 자동매매 모니터링 대시보드 v2
실행: streamlit run app.py
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from data_provider import DataProvider
from strategy.jang_dong_min import StrategyConfig, calc_ma, calc_rsi, calc_bollinger_bands

# ---------------------------------------------------------------------------
# 페이지 설정
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="키움 자동매매",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# 세션 상태 초기화
# ---------------------------------------------------------------------------

if "signal_log"      not in st.session_state:
    st.session_state.signal_log = []       # 오늘 누적 신호 리스트

if "strategy_active" not in st.session_state:
    st.session_state.strategy_active = True

if "selected_code"   not in st.session_state:
    st.session_state.selected_code = "005930"

if "refresh_sec"     not in st.session_state:
    st.session_state.refresh_sec = 30

# ---------------------------------------------------------------------------
# 자동 갱신 (streamlit-autorefresh)
# ---------------------------------------------------------------------------

st_autorefresh(
    interval=st.session_state.refresh_sec * 1000,
    key="dashboard_refresh",
)

# ---------------------------------------------------------------------------
# 데이터 프로바이더
# ---------------------------------------------------------------------------

provider = DataProvider()   # kiwoom=None → Mock 모드

# ---------------------------------------------------------------------------
# 신호 누적 (갱신마다 신호 1건 추가)
# ---------------------------------------------------------------------------

if st.session_state.strategy_active:
    new_sig = provider.get_new_signal(st.session_state.selected_code)
    st.session_state.signal_log.append(new_sig)

# 당일 09:00 이전 신호 제거
today_open = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
st.session_state.signal_log = [
    s for s in st.session_state.signal_log
    if s["시각"] >= today_open.strftime("%H:%M:%S")
]

signal_df = pd.DataFrame(st.session_state.signal_log) if st.session_state.signal_log else pd.DataFrame()

# ---------------------------------------------------------------------------
# 데이터 로드
# ---------------------------------------------------------------------------

balance      = provider.get_balance()
holdings_df  = provider.get_holdings()
prices_df    = provider.get_price_history(st.session_state.selected_code)
stock_list   = provider.get_stock_list()

# ---------------------------------------------------------------------------
# 헤더
# ---------------------------------------------------------------------------

status_color = "🟢" if st.session_state.strategy_active else "🔴"
status_text  = "전략 실행 중" if st.session_state.strategy_active else "일시정지"

col_title, col_status, col_time = st.columns([3, 1, 1])
with col_title:
    st.title("📈 키움 자동매매 대시보드")
with col_status:
    st.metric("전략 상태", f"{status_color} {status_text}")
with col_time:
    st.metric("마지막 갱신", datetime.now().strftime("%H:%M:%S"))

st.divider()

# ---------------------------------------------------------------------------
# 섹션 1: 계좌 요약
# ---------------------------------------------------------------------------

st.subheader("💰 계좌 요약")

# 오늘 실현손익 집계
realized_pnl   = 0
realized_count = 0
if not signal_df.empty:
    sell_log = signal_df[signal_df["신호"] == "SELL"]
    realized_count = len(sell_log)

m1, m2, m3, m4, m5 = st.columns(5)

delta_color = "normal" if balance["pnl_pct"] >= 0 else "inverse"
m1.metric("총평가금액",    f"{balance['total']:,.0f} 원",
          delta=f"{balance['pnl_pct']:+.2f}%")
m2.metric("예수금",        f"{balance['cash']:,.0f} 원")
m3.metric("주식평가금액",  f"{balance['stock_value']:,.0f} 원")
m4.metric("총손익",        f"{balance['pnl']:+,.0f} 원",
          delta=f"{balance['pnl_pct']:+.2f}%")
m5.metric("오늘 매도 횟수", f"{realized_count} 회")

st.divider()

# ---------------------------------------------------------------------------
# 섹션 2: 보유 종목
# ---------------------------------------------------------------------------

st.subheader("📋 보유 종목")

tbl_col, pie_col = st.columns([1.8, 1.2])

with tbl_col:
    if holdings_df.empty:
        st.info("보유 종목이 없습니다.")
    else:
        def color_pnl(val):
            if isinstance(val, (int, float)):
                return "color: #c0392b" if val < 0 else "color: #27ae60" if val > 0 else ""
            return ""

        styled = (
            holdings_df.style
            .applymap(color_pnl, subset=["평가손익", "수익률(%)"])
            .format({
                "매입가":   "{:,.0f}",
                "현재가":   "{:,.0f}",
                "평가손익": "{:+,.0f}",
                "수익률(%)": "{:+.2f}%",
            })
            .set_properties(**{"text-align": "right"},
                             subset=["보유수량", "매입가", "현재가", "평가손익", "수익률(%)"])
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

with pie_col:
    if not holdings_df.empty:
        vals = holdings_df["현재가"] * holdings_df["보유수량"]
        fig_pie = go.Figure(go.Pie(
            labels=holdings_df["종목명"],
            values=vals,
            hole=0.5,
            textinfo="label+percent",
            textfont_size=13,
            marker=dict(colors=["#636EFA", "#EF553B", "#00CC96", "#FFA15A", "#AB63FA"]),
        ))
        fig_pie.update_layout(
            title=dict(text="보유 비중", x=0.5),
            showlegend=False,
            margin=dict(t=50, b=10, l=10, r=10),
            height=270,
        )
        st.plotly_chart(fig_pie, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# 섹션 3: 가격 차트
# ---------------------------------------------------------------------------

selected_name = stock_list.get(st.session_state.selected_code, st.session_state.selected_code)
st.subheader(f"📊 {selected_name} ({st.session_state.selected_code}) · 3분봉")

closes = prices_df["close"].tolist()
cfg    = StrategyConfig()

ma5_s  = prices_df["close"].rolling(5).mean()
ma20_s = prices_df["close"].rolling(20).mean()
bb     = calc_bollinger_bands(closes, cfg.bb_period, cfg.bb_std)
bb_upper = prices_df["close"].rolling(cfg.bb_period).apply(
    lambda x: x.mean() + cfg.bb_std * x.std(), raw=True)
bb_lower = prices_df["close"].rolling(cfg.bb_period).apply(
    lambda x: x.mean() - cfg.bb_std * x.std(), raw=True)

# 4패널 (캔들 / 볼밴 / RSI / 거래량)
fig = make_subplots(
    rows=3, cols=1,
    shared_xaxes=True,
    row_heights=[0.60, 0.20, 0.20],
    vertical_spacing=0.02,
)

# ── 캔들 ──
fig.add_trace(go.Candlestick(
    x=prices_df["time"],
    open=prices_df["open"], high=prices_df["high"],
    low=prices_df["low"],   close=prices_df["close"],
    name="캔들",
    increasing_line_color="#EF553B",
    decreasing_line_color="#636EFA",
    showlegend=False,
), row=1, col=1)

# ── 이동평균 ──
fig.add_trace(go.Scatter(
    x=prices_df["time"], y=ma5_s,
    mode="lines", name="MA5",
    line=dict(color="#FFA15A", width=1.5),
), row=1, col=1)
fig.add_trace(go.Scatter(
    x=prices_df["time"], y=ma20_s,
    mode="lines", name="MA20",
    line=dict(color="#AB63FA", width=1.5),
), row=1, col=1)

# ── 볼린저 밴드 ──
fig.add_trace(go.Scatter(
    x=prices_df["time"], y=bb_upper,
    mode="lines", name="BB Upper",
    line=dict(color="rgba(100,149,237,0.4)", width=1, dash="dot"),
    showlegend=False,
), row=1, col=1)
fig.add_trace(go.Scatter(
    x=prices_df["time"], y=bb_lower,
    mode="lines", name="BB Lower",
    fill="tonexty",
    fillcolor="rgba(100,149,237,0.06)",
    line=dict(color="rgba(100,149,237,0.4)", width=1, dash="dot"),
    showlegend=False,
), row=1, col=1)

# ── 신호 마커 ──
if not signal_df.empty:
    buys  = signal_df[signal_df["신호"] == "BUY"]
    sells = signal_df[signal_df["신호"] == "SELL"]
    if not buys.empty:
        fig.add_trace(go.Scatter(
            x=buys["시각"], y=buys["가격"],
            mode="markers", name="BUY",
            marker=dict(symbol="triangle-up", size=13, color="#00CC96"),
        ), row=1, col=1)
    if not sells.empty:
        fig.add_trace(go.Scatter(
            x=sells["시각"], y=sells["가격"],
            mode="markers", name="SELL",
            marker=dict(symbol="triangle-down", size=13, color="#EF553B"),
        ), row=1, col=1)

# ── RSI ──
rsi_vals = []
for i in range(len(closes)):
    r = calc_rsi(closes[:i+1], cfg.rsi_period)
    rsi_vals.append(r)
rsi_series = pd.Series(rsi_vals)

fig.add_hrect(y0=70, y1=100, fillcolor="rgba(239,85,59,0.08)",
              line_width=0, row=2, col=1)
fig.add_hrect(y0=0,  y1=30,  fillcolor="rgba(0,204,150,0.08)",
              line_width=0, row=2, col=1)
fig.add_hline(y=70, line_dash="dot", line_color="rgba(239,85,59,0.5)",
              row=2, col=1)
fig.add_hline(y=30, line_dash="dot", line_color="rgba(0,204,150,0.5)",
              row=2, col=1)
fig.add_trace(go.Scatter(
    x=prices_df["time"], y=rsi_series,
    mode="lines", name="RSI",
    line=dict(color="#FFA15A", width=1.8),
    showlegend=False,
), row=2, col=1)

# ── 거래량 ──
vol_colors = [
    "#EF553B" if c >= o else "#636EFA"
    for c, o in zip(prices_df["close"], prices_df["open"])
]
fig.add_trace(go.Bar(
    x=prices_df["time"], y=prices_df["volume"],
    name="거래량",
    marker_color=vol_colors,
    opacity=0.7,
    showlegend=False,
), row=3, col=1)

fig.update_layout(
    xaxis_rangeslider_visible=False,
    height=560,
    margin=dict(t=10, b=10, l=50, r=20),
    legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
    plot_bgcolor="#0e1117",
    paper_bgcolor="#0e1117",
    font=dict(color="#fafafa"),
)
fig.update_yaxes(gridcolor="rgba(255,255,255,0.06)")
fig.update_xaxes(gridcolor="rgba(255,255,255,0.06)")
fig.update_yaxes(title_text="가격",   row=1, col=1)
fig.update_yaxes(title_text="RSI",    row=2, col=1, range=[0, 100])
fig.update_yaxes(title_text="거래량", row=3, col=1)

st.plotly_chart(fig, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# 섹션 4: 매매 신호 로그
# ---------------------------------------------------------------------------

st.subheader("🔔 매매 신호 로그")

if signal_df.empty:
    st.info("아직 수집된 신호가 없습니다. 전략 실행 중이면 갱신 주기마다 추가됩니다.")
else:
    log_col, rsi_col = st.columns([1.6, 1.4])

    with log_col:
        badge = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}
        display = signal_df.copy()
        display["신호"] = display["신호"].map(lambda s: f"{badge.get(s,'')} {s}")
        display = display.sort_values("시각", ascending=False)
        st.dataframe(
            display[["시각", "종목명", "신호", "가격", "MA5", "MA20", "RSI"]],
            use_container_width=True,
            hide_index=True,
            height=280,
        )

        # 신호 분포 요약
        cnt = signal_df["신호"].value_counts()
        s1, s2, s3 = st.columns(3)
        s1.metric("🟢 BUY",  cnt.get("BUY",  0))
        s2.metric("🔴 SELL", cnt.get("SELL", 0))
        s3.metric("⚪ HOLD", cnt.get("HOLD", 0))

    with rsi_col:
        fig_rsi = go.Figure()
        fig_rsi.add_hrect(y0=70, y1=100, fillcolor="rgba(239,85,59,0.10)", line_width=0)
        fig_rsi.add_hrect(y0=0,  y1=30,  fillcolor="rgba(0,204,150,0.10)", line_width=0)
        fig_rsi.add_hline(y=70, line_dash="dot", line_color="#EF553B",
                          annotation_text="과매수", annotation_position="top right")
        fig_rsi.add_hline(y=30, line_dash="dot", line_color="#00CC96",
                          annotation_text="과매도", annotation_position="bottom right")
        fig_rsi.add_trace(go.Scatter(
            x=signal_df["시각"], y=signal_df["RSI"],
            mode="lines+markers",
            line=dict(color="#FFA15A", width=2),
            marker=dict(size=6),
            name="RSI",
        ))
        # BUY/SELL 지점 강조
        for _, row in signal_df.iterrows():
            if row["신호"] in ("BUY", "SELL"):
                color = "#00CC96" if row["신호"] == "BUY" else "#EF553B"
                fig_rsi.add_vline(x=row["시각"], line_dash="dot",
                                  line_color=color, opacity=0.5)
        fig_rsi.update_layout(
            title="RSI 추이",
            yaxis=dict(range=[0, 100]),
            height=320,
            margin=dict(t=40, b=10, l=40, r=10),
            showlegend=False,
            plot_bgcolor="#0e1117",
            paper_bgcolor="#0e1117",
            font=dict(color="#fafafa"),
        )
        fig_rsi.update_yaxes(gridcolor="rgba(255,255,255,0.06)")
        fig_rsi.update_xaxes(gridcolor="rgba(255,255,255,0.06)")
        st.plotly_chart(fig_rsi, use_container_width=True)

# ---------------------------------------------------------------------------
# 사이드바
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ 설정")

    # 종목 선택 — 실제로 차트·신호에 반영됨
    options = [f"{c}  {n}" for c, n in stock_list.items()]
    default_idx = list(stock_list.keys()).index(st.session_state.selected_code)
    chosen = st.selectbox("모니터링 종목", options, index=default_idx)
    new_code = chosen.split()[0]
    if new_code != st.session_state.selected_code:
        st.session_state.selected_code = new_code
        st.session_state.signal_log = []   # 종목 변경 시 로그 초기화
        st.rerun()

    # 갱신 주기
    new_refresh = st.slider("자동 갱신 주기 (초)", 10, 300,
                            st.session_state.refresh_sec, step=10)
    if new_refresh != st.session_state.refresh_sec:
        st.session_state.refresh_sec = new_refresh
        st.rerun()
    st.caption(f"현재: {st.session_state.refresh_sec}초마다 갱신")

    st.divider()

    # 전략 ON/OFF
    st.header("🎛️ 전략 제어")
    if st.session_state.strategy_active:
        if st.button("⏸ 전략 일시정지", use_container_width=True):
            st.session_state.strategy_active = False
            st.rerun()
    else:
        if st.button("▶ 전략 재개", use_container_width=True, type="primary"):
            st.session_state.strategy_active = True
            st.rerun()

    if st.button("🗑 신호 로그 초기화", use_container_width=True):
        st.session_state.signal_log = []
        st.rerun()

    st.divider()

    if st.button("🛑 전 종목 즉시 청산", use_container_width=True, type="primary"):
        st.error("청산 주문 전송됨 (TODO: kiwoom_api 연동)")

    st.divider()
    st.caption(f"종목: {selected_name} ({st.session_state.selected_code})")
    st.caption(f"신호 누적: {len(st.session_state.signal_log)}건")
    st.caption("© 2026 키움 자동매매 시스템")
