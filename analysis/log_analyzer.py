# -*- coding: utf-8 -*-
"""
analysis/log_analyzer.py
─────────────────────────
장 마감 로그 파싱 → 텔레그램 일일 리포트 포맷터.

FeedbackEngine은 fills/audit CSV 기반 파라미터 조정을 담당하고,
LogAnalyzer는 scanner.log의 필터 거부 패턴과 audit CSV의 매도 이유를
분석해 텔레그램 메시지를 생성한다.

주요 클래스:
  LogAnalyzer.analyze_scanner_log(date) → ScannerLogStats
  LogAnalyzer.analyze_audit(date)       → TradeLogStats
  LogAnalyzer.format_telegram_report(…) → str
  LogAnalyzer.run(date)                 → DailyLogResult
"""
from __future__ import annotations

import csv
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScannerLogStats:
    """scanner.log 파싱 결과"""
    date:             date
    total_fail:       int
    total_pass:       int
    fail_by_step:     Dict[str, int]            # step → count (내림차순)
    pass_by_strategy: Dict[str, int]            # strategy → PASS count
    top_fail_codes:   List[Tuple[str, str, int]]  # (code, name, fail_count) top5


@dataclass
class TradeLogStats:
    """audit CSV 파싱 결과 (체결 거래 한정)"""
    date:            date
    total_trades:    int
    total_realized:  float
    win_count:       int
    sell_reasons:    Dict[str, int]               # category → count
    by_signal_type:  Dict[str, Tuple[int, float]] # signal → (건수, 손익합)
    avg_holding_min: float


@dataclass
class DailyLogResult:
    """LogAnalyzer 통합 결과"""
    date:    date
    scanner: ScannerLogStats
    trades:  TradeLogStats


# ──────────────────────────────────────────────────────────────────────────────
# LogAnalyzer
# ──────────────────────────────────────────────────────────────────────────────

class LogAnalyzer:
    """
    scanner.log + trade_audit_YYYYMMDD.csv 파싱.

    Parameters
    ----------
    log_dir : str
        로그 디렉터리 (기본: "logs")
    """

    def __init__(self, log_dir: str = "logs") -> None:
        self.log_dir = Path(log_dir)

    # ── scanner.log ───────────────────────────────────────────────────────────

    def analyze_scanner_log(self, target_date: date) -> ScannerLogStats:
        """
        scanner.log에서 target_date의 FAIL/PASS 라인을 파싱한다.

        라인 포맷:
          YYYY-MM-DD HH:MM:SS \\t LEVEL \\t PASS|FAIL \\t CODE \\t NAME \\t STEP \\t REASON
        """
        date_prefix = target_date.strftime("%Y-%m-%d")
        fail_by_step:     Counter = Counter()
        pass_by_strategy: Counter = Counter()
        fail_by_code:     Counter = Counter()
        code_name_map: Dict[str, str] = {}

        # 현재 파일 + rotated 파일 (최대 5개)
        log_files = [self.log_dir / "scanner.log"]
        for i in range(1, 6):
            p = self.log_dir / f"scanner.log.{i}"
            if p.exists():
                log_files.append(p)

        total_fail = 0
        total_pass = 0

        for log_path in log_files:
            if not log_path.exists():
                continue
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if not line.startswith(date_prefix):
                            continue
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) < 6:
                            continue
                        # parts[2] = PASS | FAIL
                        pf   = parts[2].strip()
                        code = parts[3].strip() if len(parts) > 3 else ""
                        name = parts[4].strip() if len(parts) > 4 else ""
                        step = parts[5].strip() if len(parts) > 5 else ""

                        if pf in ("FAIL", "NEAR"):   # NEAR = 근사 탈락 (기준 15% 이내)
                            total_fail += 1
                            if step:
                                fail_by_step[step] += 1
                            if code:
                                fail_by_code[code] += 1
                                if name:
                                    code_name_map[code] = name
                        elif pf == "PASS":
                            total_pass += 1
                            if step:
                                pass_by_strategy[step] += 1

            except Exception as exc:
                logger.warning("[LogAnalyzer] scanner.log 파싱 오류 (%s): %s", log_path, exc)

        top_fail_codes = [
            (code, code_name_map.get(code, ""), cnt)
            for code, cnt in fail_by_code.most_common(5)
        ]

        result = ScannerLogStats(
            date=target_date,
            total_fail=total_fail,
            total_pass=total_pass,
            fail_by_step=dict(fail_by_step.most_common()),
            pass_by_strategy=dict(pass_by_strategy),
            top_fail_codes=top_fail_codes,
        )
        logger.info(
            "[LogAnalyzer] scanner.log — FAIL %d건, PASS %d건, 상위스텝: %s",
            total_fail, total_pass,
            dict(list(fail_by_step.most_common(5))),
        )
        return result

    # ── trade_audit CSV ───────────────────────────────────────────────────────

    def analyze_audit(self, target_date: date) -> TradeLogStats:
        """
        trade_audit_YYYYMMDD.csv에서 체결(FILLED/COMPLETED) 거래만 파싱.
        """
        path = self.log_dir / f"trade_audit_{target_date.strftime('%Y%m%d')}.csv"

        if not path.exists():
            logger.warning("[LogAnalyzer] audit 파일 없음: %s", path)
            return TradeLogStats(
                date=target_date, total_trades=0, total_realized=0.0,
                win_count=0, sell_reasons={}, by_signal_type={}, avg_holding_min=0.0,
            )

        total_trades   = 0
        total_realized = 0.0
        win_count      = 0
        sell_reasons:   Counter                     = Counter()
        by_signal_type: Dict[str, List[float]]      = defaultdict(list)
        holding_mins:   List[float]                 = []

        try:
            with open(path, encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    status = row.get("final_status", "")
                    if status not in ("FILLED", "COMPLETED"):
                        continue

                    try:
                        pnl  = float(row.get("realized_pnl",    0) or 0)
                        hold = float(row.get("holding_minutes",  0) or 0)
                        sig  = row.get("signal_type", "UNKNOWN") or "UNKNOWN"
                        reason = row.get("sell_reason", "") or ""
                    except (ValueError, TypeError):
                        continue

                    total_trades   += 1
                    total_realized += pnl
                    if pnl > 0:
                        win_count += 1
                    if hold > 0:
                        holding_mins.append(hold)

                    cat = self._classify_sell_reason(reason)
                    sell_reasons[cat] += 1
                    by_signal_type[sig].append(pnl)

        except Exception as exc:
            logger.warning("[LogAnalyzer] audit 파싱 오류: %s", exc)

        avg_hold = sum(holding_mins) / len(holding_mins) if holding_mins else 0.0

        by_sig_summary: Dict[str, Tuple[int, float]] = {
            sig: (len(pnls), sum(pnls))
            for sig, pnls in by_signal_type.items()
        }

        logger.info(
            "[LogAnalyzer] audit — %d건, 손익 %.0f원, 승률 %d/%d",
            total_trades, total_realized, win_count, total_trades,
        )
        return TradeLogStats(
            date=target_date,
            total_trades=total_trades,
            total_realized=total_realized,
            win_count=win_count,
            sell_reasons=dict(sell_reasons),
            by_signal_type=by_sig_summary,
            avg_holding_min=avg_hold,
        )

    # ── 매도 이유 분류 ────────────────────────────────────────────────────────

    @staticmethod
    def _classify_sell_reason(reason: str) -> str:
        """매도 이유 자유 문자열 → 간결한 카테고리"""
        if not reason:
            return "기타"
        lower = reason.lower()
        if "손절" in reason:
            return "손절"
        if "트레일" in reason or "trail" in lower:
            return "트레일스탑"
        if "추세소멸" in reason:
            return "추세소멸"
        if "익절" in reason or "목표" in reason:
            return "익절"
        if "타임컷" in reason or "time" in lower:
            return "타임컷"
        if "강제청산" in reason or "day close" in lower or "eod" in lower:
            return "강제청산"
        if "ema" in lower or "이탈" in reason:
            return "EMA이탈"
        return "기타"

    # ── 텔레그램 리포트 포맷 ─────────────────────────────────────────────────

    def format_telegram_report(
        self,
        scanner:              ScannerLogStats,
        trades:               TradeLogStats,
        feedback_adjustments: list,   # List[ParamAdjustment]
        feedback_skipped:     list,   # List[str]
    ) -> str:
        """
        scanner 분석 + 거래 통계 + 파라미터 조정 결과를
        텔레그램 전송용 텍스트로 포맷한다.
        """
        d = scanner.date
        lines: List[str] = []

        # ── 헤더
        lines.append(f"📊 [{d}] 장 마감 리포트")
        lines.append("─" * 24)

        # ── 손익 요약
        if trades.total_trades > 0:
            pnl_emoji = "🟢" if trades.total_realized >= 0 else "🔴"
            wr_pct = (
                int(trades.win_count * 100 / trades.total_trades)
                if trades.total_trades else 0
            )
            lines.append(
                f"{pnl_emoji} 손익: {trades.total_realized:+,.0f}원 | "
                f"체결 {trades.total_trades}건 | 승률 {trades.win_count}/{trades.total_trades} ({wr_pct}%)"
            )
            if trades.avg_holding_min > 0:
                lines.append(f"   평균 보유: {trades.avg_holding_min:.1f}분")
        else:
            lines.append("⚪ 금일 체결 없음")
        lines.append("")

        # ── 매도 이유 분포
        if trades.sell_reasons:
            lines.append("📋 매도 이유")
            for reason, cnt in sorted(trades.sell_reasons.items(), key=lambda x: -x[1]):
                lines.append(f"   {reason}: {cnt}건")
            lines.append("")

        # ── 전략별 성과
        if trades.by_signal_type:
            lines.append("📈 전략별 체결")
            for sig, (cnt, pnl) in sorted(
                trades.by_signal_type.items(), key=lambda x: -x[1][0]
            ):
                lines.append(f"   {sig}: {cnt}건  {pnl:+,.0f}원")
            lines.append("")

        # ── 스캐너 필터 분포 (상위 8 스텝)
        if scanner.fail_by_step:
            lines.append(
                f"🔍 스캐너 거부 (총 {scanner.total_fail:,}건)"
            )
            top_steps = list(scanner.fail_by_step.items())[:8]
            for step, cnt in top_steps:
                pct = int(cnt * 100 / scanner.total_fail) if scanner.total_fail else 0
                lines.append(f"   {step}: {cnt:,}건 ({pct}%)")
            lines.append("")

        # ── 파라미터 조정
        lines.append("⚙️ 파라미터 조정")
        if feedback_adjustments:
            for adj in feedback_adjustments:
                arrow = "▲" if adj.new_val > adj.old_val else "▼"
                lines.append(
                    f"   {adj.param}: {adj.old_val} {arrow} {adj.new_val}"
                )
        else:
            lines.append("   (변경 없음)")
        if feedback_skipped:
            lines.append(f"   [보류 {len(feedback_skipped)}건 — 연속 신호 대기]")

        return "\n".join(lines)

    # ── 통합 실행 ─────────────────────────────────────────────────────────────

    def run(self, target_date: Optional[date] = None) -> DailyLogResult:
        """scanner.log + audit CSV 전체 파싱 → DailyLogResult"""
        target_date = target_date or date.today()
        scanner = self.analyze_scanner_log(target_date)
        trades  = self.analyze_audit(target_date)
        return DailyLogResult(date=target_date, scanner=scanner, trades=trades)
