"""
선행지표 효과 분석 도구 — B안

scanner_signal.csv (진입 시점 선행지표) +
position.log (청산 수익률) 를 매칭하여
각 선행지표별 승률/평균수익을 출력한다.

사용:
  python tools/analyze_leading.py
  python tools/analyze_leading.py --days 7
  python tools/analyze_leading.py --date 2026-06-06
"""
from __future__ import annotations
import csv
import re
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict


LOG_DIR = Path("logs")
POSITION_LOG = LOG_DIR / "position.log"
RESULT_CSV = LOG_DIR / "trade_result.csv"


def get_signal_csv(target_date: datetime | None = None) -> Path:
    """날짜별 scanner_signal CSV 경로 반환 (없으면 통합 파일 fallback)."""
    d = target_date or datetime.now()
    dated = LOG_DIR / f"scanner_signal_{d.strftime('%Y%m%d')}.csv"
    if dated.exists():
        return dated
    return LOG_DIR / "scanner_signal.csv"

# 선행지표 컬럼 정의
LEADING_COLS = ["li_bs", "li_vb", "li_cr", "li_ca", "li_hp", "li_hv", "li_leading"]
LEADING_LABELS = {
    "li_bs":      "매수1호가기울기",
    "li_vb":      "거래량폭발",
    "li_cr":      "체결강도반등",
    "li_ca":      "체결강도가속",
    "li_hp":      "호가압력",
    "li_hv":      "호가속도",
    "li_leading": "선행점수합산",
}

# 분석 기준 구간 (지표값 >= 임계값이면 "강한 신호"로 분류)
THRESHOLDS = {
    "li_bs":      0.30,
    "li_vb":      0.40,
    "li_cr":      0.25,
    "li_ca":      0.20,
    "li_hp":      0.50,
    "li_hv":      0.30,
    "li_leading": 0.10,
}


def parse_position_log(since: datetime | None = None) -> dict[str, list[dict]]:
    """
    position.log 파싱 → {code: [{"entry_time", "exit_time", "pnl_pct"}, ...]}
    """
    trades: dict[str, list[dict]] = defaultdict(list)
    pending: dict[str, dict] = {}  # code → entry 임시 보관

    entry_pat = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[포지션생성\].*?\((\w+)\).*체결가=(\d+)"
    )
    exit_pat = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[포지션청산\].*?\((\w+)\).*손익=([+-]?\d+\.?\d*%?)"
    )

    try:
        lines = POSITION_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        print(f"[경고] {POSITION_LOG} 없음")
        return trades

    for line in lines:
        m = entry_pat.search(line)
        if m:
            ts_str, code, price = m.group(1), m.group(2), m.group(3)
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            if since and ts < since:
                continue
            pending[code] = {"entry_time": ts, "entry_price": int(price)}
            continue

        m = exit_pat.search(line)
        if m:
            ts_str, code, pnl_str = m.group(1), m.group(2), m.group(3)
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            pnl = float(pnl_str.replace("%", ""))
            if code in pending:
                entry = pending.pop(code)
                trades[code].append({
                    "entry_time": entry["entry_time"],
                    "exit_time":  ts,
                    "pnl_pct":    pnl,
                    "name":       code,
                })

    return trades


def parse_signal_csv(since: datetime | None = None) -> list[dict]:
    """scanner_signal.csv 파싱 → 선행지표 컬럼이 있는 행만 반환"""
    rows = []
    signal_csv = get_signal_csv(since)
    try:
        with open(signal_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not any(col in row for col in LEADING_COLS):
                    continue  # 선행지표 기록 이전 구버전 행 스킵
                ts_str = row.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
                if since and ts < since:
                    continue
                rows.append(row)
    except FileNotFoundError:
        print(f"[경고] {signal_csv} 없음")
    return rows


def match_signal_to_trade(signals: list[dict], trades: dict[str, list[dict]]) -> list[dict]:
    """
    신호 시각과 진입 시각이 5분 이내인 것을 매칭.
    하나의 신호에 가장 가까운 청산 결과 1개만 연결.
    """
    results = []
    for sig in signals:
        code = sig.get("code", "")
        ts_str = sig.get("timestamp", "")
        try:
            sig_ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue

        best = None
        best_gap = timedelta(minutes=5)

        for trade in trades.get(code, []):
            gap = abs(trade["entry_time"] - sig_ts)
            if gap < best_gap:
                best_gap = gap
                best = trade

        row = {
            "date":      sig_ts.strftime("%Y-%m-%d"),
            "time":      sig_ts.strftime("%H:%M:%S"),
            "code":      code,
            "name":      sig.get("name", ""),
            "pnl_pct":   best["pnl_pct"] if best else None,
            "matched":   best is not None,
        }
        for col in LEADING_COLS:
            try:
                row[col] = float(sig.get(col, 0) or 0)
            except (ValueError, TypeError):
                row[col] = 0.0
        results.append(row)

    return results


def save_result_csv(results: list[dict]) -> None:
    if not results:
        return
    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(f"[저장] {RESULT_CSV} ({len(results)}건)")


def analyze(results: list[dict]) -> None:
    matched = [r for r in results if r["matched"]]
    if not matched:
        print("[분석] 매칭된 거래 없음 - 데이터 부족")
        return

    total = len(matched)
    wins  = sum(1 for r in matched if r["pnl_pct"] > 0)
    avg   = sum(r["pnl_pct"] for r in matched) / total

    print(f"\n{'='*60}")
    print(f"  선행지표 효과 분석  ({matched[0]['date']} ~ {matched[-1]['date']})")
    print(f"{'='*60}")
    print(f"  전체: {total}건  승률: {wins/total*100:.1f}%  평균수익: {avg:+.2f}%")
    print(f"{'-'*60}")

    for col in LEADING_COLS:
        label = LEADING_LABELS[col]
        thr   = THRESHOLDS[col]

        strong = [r for r in matched if r[col] >= thr]
        weak   = [r for r in matched if r[col] <  thr]

        def stats(group):
            if not group:
                return "0건 (데이터 없음)"
            n = len(group)
            w = sum(1 for r in group if r["pnl_pct"] > 0)
            a = sum(r["pnl_pct"] for r in group) / n
            return f"{n:3d}건  승률 {w/n*100:5.1f}%  평균 {a:+.2f}%"

        print(f"\n  [{label}] (기준: ≥ {thr})")
        print(f"    강한신호 (≥{thr}): {stats(strong)}")
        print(f"    약한신호 (<{thr}): {stats(weak)}")

    # 조합 분석: bs + vb 동시 강한 경우
    print(f"\n{'-'*60}")
    print("  [조합 분석]")
    combos = [
        ("bs+vb 둘 다 강함",  lambda r: r["li_bs"] >= THRESHOLDS["li_bs"] and r["li_vb"] >= THRESHOLDS["li_vb"]),
        ("bs만 강함",         lambda r: r["li_bs"] >= THRESHOLDS["li_bs"] and r["li_vb"] <  THRESHOLDS["li_vb"]),
        ("vb만 강함",         lambda r: r["li_bs"] <  THRESHOLDS["li_bs"] and r["li_vb"] >= THRESHOLDS["li_vb"]),
        ("cr+bs 둘 다 강함",  lambda r: r["li_cr"] >= THRESHOLDS["li_cr"] and r["li_bs"] >= THRESHOLDS["li_bs"]),
        ("선행점수 0 (전부 약함)", lambda r: r["li_leading"] < 0.05),
    ]
    for label, cond in combos:
        group = [r for r in matched if cond(r)]
        if not group:
            continue
        n = len(group)
        w = sum(1 for r in group if r["pnl_pct"] > 0)
        a = sum(r["pnl_pct"] for r in group) / n
        print(f"    {label:25s}: {n:3d}건  승률 {w/n*100:5.1f}%  평균 {a:+.2f}%")

    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="선행지표 효과 분석")
    parser.add_argument("--days",  type=int,   default=None, help="최근 N일 분석 (기본: 전체)")
    parser.add_argument("--date",  type=str,   default=None, help="특정 날짜 분석 (YYYY-MM-DD)")
    parser.add_argument("--save",  action="store_true",      help="결과를 trade_result.csv로 저장")
    args = parser.parse_args()

    since = None
    if args.date:
        since = datetime.strptime(args.date, "%Y-%m-%d")
    elif args.days:
        since = datetime.now() - timedelta(days=args.days)

    print("[1] position.log 파싱...")
    trades = parse_position_log(since)
    print(f"    청산 완료: {sum(len(v) for v in trades.values())}건")

    print("[2] scanner_signal.csv 파싱...")
    signals = parse_signal_csv(since)
    print(f"    신호 (선행지표 포함): {len(signals)}건")

    print("[3] 신호-청산 매칭...")
    results = match_signal_to_trade(signals, trades)
    matched_count = sum(1 for r in results if r["matched"])
    print(f"    매칭 성공: {matched_count}/{len(results)}건")

    if args.save:
        save_result_csv(results)

    analyze(results)


if __name__ == "__main__":
    main()
