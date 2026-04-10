# -*- coding: utf-8 -*-
"""
analysis/daily_report.py
─────────────────────────
FeedbackResult + AuditRecord 리스트를 받아 HTML 리포트를 생성한다.
외부 라이브러리 없이 f-string 기반으로 작성.
출력: logs/report_YYYYMMDD.html
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Dict, List

from analysis.feedback_engine import (
    AuditRecord, FeedbackResult, LossCat, SlotStat,
    PEAK_HISTORY_DAYS, PROFIT_LOCK_RATIO,
)


# 카테고리 한글 레이블
_CAT_LABEL = {
    LossCat.OPENING_NOISE:   "장초 이상 체결강도",
    LossCat.HIGH_ENTRY_CHG:  "고등락률 추격 매수",
    LossCat.TRAIL_TOO_TIGHT: "트레일 조기 청산",
    LossCat.EARLY_REVERSAL:  "단기 손절 (≤10분)",
    LossCat.STOP_LOSS_HIT:   "손절 다발",
}

_CAT_COLOR = {
    LossCat.OPENING_NOISE:   "#f38ba8",
    LossCat.HIGH_ENTRY_CHG:  "#fab387",
    LossCat.TRAIL_TOO_TIGHT: "#f9e2af",
    LossCat.EARLY_REVERSAL:  "#cba6f7",
    LossCat.STOP_LOSS_HIT:   "#ff4444",
}


class DailyReporter:

    def __init__(self, output_dir: str = "logs"):
        self.output_dir = Path(output_dir)

    def generate(self, result: FeedbackResult, audits: List[AuditRecord]) -> Path:
        html = self._build_html(result, audits)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"report_{result.date.strftime('%Y%m%d')}.html"
        path.write_text(html, encoding="utf-8")
        return path

    # ── HTML 조립 ─────────────────────────────────────────────────────────────

    def _build_html(self, result: FeedbackResult, audits: List[AuditRecord]) -> str:
        pnl_color = "#a6e3a1" if result.profitable else "#f38ba8"
        status_txt = "수익 — 파라미터 유지" if result.profitable else "손실 — 파라미터 조정 검토"

        sections = [
            self._header_html(result, pnl_color, status_txt),
            self._summary_table_html(audits),
            self._slot_pnl_html(result.slot_stats, result.peak_pnl),
            self._category_heatmap_html(result.category_hits),
            self._param_changes_html(result),
            self._tomorrow_plan_html(result),
            self._footer_html(),
        ]
        body = "\n".join(sections)

        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>트레이딩 피드백 리포트 {result.date}</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background:#1e1e2e; color:#cdd6f4;
          margin:0; padding:24px; }}
  h1   {{ color:#cba6f7; font-size:1.4rem; margin-bottom:4px; }}
  h2   {{ color:#89b4fa; font-size:1.1rem; border-bottom:1px solid #313244;
          padding-bottom:6px; margin-top:28px; }}
  table{{ border-collapse:collapse; width:100%; font-size:0.85rem; margin-top:8px; }}
  th   {{ background:#313244; color:#89dceb; padding:8px 12px; text-align:left; }}
  td   {{ padding:6px 12px; border-bottom:1px solid #2a2a3d; }}
  tr:hover td {{ background:#313244; }}
  .pos {{ color:#a6e3a1; }} .neg {{ color:#f38ba8; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:12px;
            font-size:0.75rem; font-weight:bold; }}
  .card {{ background:#181825; border-radius:8px; padding:16px;
           margin-bottom:16px; }}
  .stat {{ font-size:1.8rem; font-weight:bold; }}
</style>
</head>
<body>
{body}
</body>
</html>"""

    def _header_html(self, r: FeedbackResult, color: str, status: str) -> str:
        pnl = f"{r.total_realized:+,.0f}원"
        return f"""
<h1>📊 일별 피드백 리포트</h1>
<p style="color:#6c7086;">{r.date} | {status}</p>
<div style="display:flex; gap:16px; flex-wrap:wrap;">
  <div class="card" style="flex:1; min-width:160px;">
    <div style="color:#6c7086; font-size:0.8rem;">당일 실현손익</div>
    <div class="stat" style="color:{color};">{pnl}</div>
  </div>
  <div class="card" style="flex:1; min-width:160px;">
    <div style="color:#6c7086; font-size:0.8rem;">분석 거래건수</div>
    <div class="stat">{r.total_trades}건</div>
  </div>
  <div class="card" style="flex:1; min-width:160px;">
    <div style="color:#6c7086; font-size:0.8rem;">파라미터 조정</div>
    <div class="stat" style="color:#f9e2af;">{len(r.adjustments)}개</div>
  </div>
</div>"""

    def _summary_table_html(self, audits: List[AuditRecord]) -> str:
        filled = [a for a in audits if a.final_status in ("FILLED", "COMPLETED")]
        if not filled:
            return "<h2>거래 요약</h2><p style='color:#6c7086;'>체결 거래 없음</p>"

        # 종목별 집계 (code + avg_buy_price 기준)
        from collections import defaultdict
        by_code: Dict[str, AuditRecord] = {}
        for r in filled:
            key = r.code
            if key not in by_code or abs(r.realized_pnl) > abs(by_code[key].realized_pnl):
                by_code[key] = r

        rows_sorted = sorted(by_code.values(), key=lambda x: x.realized_pnl)

        rows_html = ""
        for r in rows_sorted:
            pnl_cls  = "pos" if r.realized_pnl >= 0 else "neg"
            ret_cls  = "pos" if r.return_pct   >= 0 else "neg"
            cat_list = []
            if r.chejan_strength_at_signal > 5000 and r.realized_pnl < 0:
                cat_list.append("장초노이즈")
            if r.change_pct_at_signal > 8.0 and r.realized_pnl < 0:
                cat_list.append("고등락진입")
            if ("트레일스탑" in r.sell_reason) and r.return_pct < 1.0:
                cat_list.append("Trail조기")
            if r.holding_minutes <= 10 and "손절" in r.sell_reason and r.realized_pnl < 0:
                cat_list.append("단기손절")
            cats_html = " ".join(
                f'<span class="badge" style="background:#313244;">{c}</span>'
                for c in cat_list
            )
            sell_short = r.sell_reason[:40] + "…" if len(r.sell_reason) > 40 else r.sell_reason
            rows_html += f"""
  <tr>
    <td>{r.name} <span style="color:#6c7086;">({r.code})</span></td>
    <td>{r.signal_type}</td>
    <td class="{ret_cls}">{r.return_pct:+.2f}%</td>
    <td class="{pnl_cls}">{r.realized_pnl:+,.0f}원</td>
    <td style="color:#6c7086;">{r.holding_minutes:.1f}분</td>
    <td>{sell_short}</td>
    <td>{cats_html}</td>
  </tr>"""

        return f"""
<h2>거래 요약</h2>
<table>
  <thead>
    <tr>
      <th>종목</th><th>신호</th><th>수익률</th><th>실현손익</th>
      <th>보유</th><th>매도사유</th><th>카테고리</th>
    </tr>
  </thead>
  <tbody>{rows_html}
  </tbody>
</table>"""

    def _category_heatmap_html(self, category_hits: Dict[str, int]) -> str:
        if not category_hits:
            return "<h2>손실 원인 분석</h2><p style='color:#6c7086;'>감지된 손실 패턴 없음</p>"

        items = ""
        for cat, label in _CAT_LABEL.items():
            n = category_hits.get(cat, 0)
            if n == 0:
                bg, fg = "#1e1e2e", "#6c7086"
            elif n <= 2:
                bg, fg = "#3d2a1a", "#fab387"
            elif n <= 4:
                bg, fg = "#3d1f1f", "#f38ba8"
            else:
                bg, fg = "#5c1a1a", "#ff4444"
            items += f"""
  <div style="background:{bg}; border-radius:8px; padding:12px 16px;
              display:flex; justify-content:space-between; align-items:center;
              border-left:4px solid {_CAT_COLOR.get(cat,'#888')}; margin-bottom:8px;">
    <span style="color:{fg}; font-weight:{'bold' if n>0 else 'normal'};">{label}</span>
    <span style="font-size:1.4rem; font-weight:bold; color:{fg};">{n}건</span>
  </div>"""

        return f"<h2>손실 원인 분석</h2>{items}"

    def _param_changes_html(self, result: FeedbackResult) -> str:
        sections = ""

        # 적용된 변경사항
        if result.adjustments:
            rows = ""
            for adj in result.adjustments:
                dir_arrow = "▲" if adj.new_val > adj.old_val else "▼"
                dir_color = "#f38ba8" if adj.new_val > adj.old_val else "#a6e3a1"
                rows += f"""
  <tr>
    <td><code style="color:#89dceb;">{adj.param}</code></td>
    <td>{adj.old_val}</td>
    <td style="color:{dir_color}; font-weight:bold;">{dir_arrow} {adj.new_val}</td>
    <td style="color:#6c7086;">{adj.reason}</td>
    <td><span class="badge" style="background:#313244;">{adj.category}</span></td>
  </tr>"""
            sections += f"""
<h2>✅ 적용된 파라미터 변경</h2>
<table>
  <thead><tr><th>파라미터</th><th>이전값</th><th>변경값</th><th>사유</th><th>카테고리</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""
        else:
            sections += "<h2>파라미터 변경</h2><p style='color:#6c7086;'>이번 분석에서 적용된 변경 없음</p>"

        # 스킵된 항목
        if result.skipped_reasons:
            items = "".join(
                f'<li style="color:#6c7086; margin-bottom:4px;">{s}</li>'
                for s in result.skipped_reasons
            )
            sections += f"""
<h2>⏸ 보류된 조정</h2>
<ul style="margin:0; padding-left:20px;">{items}</ul>"""

        return sections

    def _slot_pnl_html(self, slot_stats: List[SlotStat], peak_pnl: float) -> str:
        if not slot_stats:
            return "<h2>시간대별 손익 분석</h2><p style='color:#6c7086;'>데이터 없음</p>"

        # 최대 절댓값 기준으로 바 길이 정규화
        max_abs = max((abs(s.total_pnl) for s in slot_stats if s.count > 0), default=1.0) or 1.0

        rows = ""
        for s in slot_stats:
            if s.count == 0:
                bar_html = "<span style='color:#45475a;'>거래 없음</span>"
                badge    = ""
            else:
                bar_w   = int(abs(s.total_pnl) / max_abs * 200)
                bar_col = "#a6e3a1" if s.total_pnl >= 0 else "#f38ba8"
                bar_html = (
                    f"<div style='display:inline-block; width:{bar_w}px; height:14px; "
                    f"background:{bar_col}; border-radius:3px; vertical-align:middle;'></div>"
                    f" <span style='color:{bar_col}; font-weight:bold;'>{s.total_pnl:+,.0f}원</span>"
                    f" <span style='color:#6c7086; font-size:0.8rem;'>"
                    f"({s.count}건, 승률 {s.win_rate*100:.0f}%)</span>"
                )
                if s.is_danger:
                    badge = "<span class='badge' style='background:#5c1a1a; color:#ff4444;'>⚠️ 위험</span>"
                elif s.is_golden:
                    badge = "<span class='badge' style='background:#1a3a1a; color:#a6e3a1;'>✅ 황금</span>"
                else:
                    badge = ""

            rows += f"""
  <tr>
    <td style="white-space:nowrap; color:#89dceb; font-weight:bold;">{s.slot}</td>
    <td style="color:#6c7086; font-size:0.82rem;">{s.start_time}~{s.end_time}</td>
    <td>{bar_html}</td>
    <td style="text-align:center;">{badge}</td>
  </tr>"""

        peak_html = ""
        if peak_pnl > 0:
            peak_html = (
                f"<p style='color:#f9e2af; margin-top:8px; font-size:0.88rem;'>"
                f"📈 장중 최고 실현손익: <strong style='color:#a6e3a1;'>{peak_pnl:+,.0f}원</strong></p>"
            )

        return f"""
<h2>시간대별 손익 분석</h2>
<table>
  <thead>
    <tr><th>슬롯</th><th>시간대</th><th>손익</th><th>판정</th></tr>
  </thead>
  <tbody>{rows}
  </tbody>
</table>
{peak_html}"""

    def _tomorrow_plan_html(self, result: FeedbackResult) -> str:
        """내일 적용될 설정 예고 섹션."""
        lines = []

        # entry_end_time 변경 예고
        for adj in result.adjustments:
            if adj.param == "entry_end_time":
                total_min = int(round(adj.new_val))
                h, m = divmod(total_min, 60)
                lines.append(
                    f"⏰ 진입 종료 시간: <strong>{h:02d}:{m:02d}</strong> "
                    f"(현재 {int(adj.old_val)//60:02d}:{int(adj.old_val)%60:02d} → 단축)"
                )
            elif adj.param == "entry_start_time":
                total_min = int(round(adj.new_val))
                h, m = divmod(total_min, 60)
                lines.append(
                    f"⏰ 진입 시작 시간: <strong>{h:02d}:{m:02d}</strong> "
                    f"(현재 {int(adj.old_val)//60:02d}:{int(adj.old_val)%60:02d} → 지연)"
                )

        # daily_profit_lock_won 예고
        if result.next_profit_lock > 0:
            lines.append(
                f"🔒 내일 수익 잠금 기준: <strong style='color:#f9e2af;'>"
                f"{result.next_profit_lock:,}원</strong> "
                f"(최근 {PEAK_HISTORY_DAYS}일 장중 최고 평균 × {int(PROFIT_LOCK_RATIO*100)}%)"
            )

        if not lines:
            return (
                "<h2>📅 내일 적용 예정</h2>"
                "<p style='color:#6c7086;'>변경 없음 — 현행 파라미터 유지</p>"
            )

        items = "".join(
            f'<li style="margin-bottom:6px; color:#cdd6f4;">{ln}</li>'
            for ln in lines
        )
        return f"""
<h2>📅 내일 적용 예정</h2>
<ul style="margin:0; padding-left:20px; line-height:1.8;">{items}</ul>"""

    def _footer_html(self) -> str:
        from datetime import datetime
        return f"""
<hr style="border-color:#313244; margin-top:32px;">
<p style="color:#6c7086; font-size:0.75rem; text-align:right;">
  생성: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Kiwoom Auto Feedback Engine
</p>"""
