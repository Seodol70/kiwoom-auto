"""
analyze_performance.py — 신호 성과 분석 스크립트

사용법:
    python scripts/analyze_performance.py
    python scripts/analyze_performance.py --days 7
    python scripts/analyze_performance.py --signal JDM_ENTRY

질문에 답하는 분석:
    1. 체결강도 구간별 승률/평균수익 → "130% 이상이 실제로 유리한가?"
    2. 추세 레벨별 승률 → "trend_level 2~3이 0~1보다 나은가?"
    3. 시간대별 승률 → "09:00~10:00이 오후보다 나은가?"
    4. 신호 타입별 비교 → "JDM vs BREAKOUT 어느 쪽이 낫나?"
    5. 손익비 분포 → "기댓값이 실제로 양수인가?"
"""

import argparse
import sqlite3
import os
from datetime import datetime, timedelta


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "trading.db")


def connect():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB 없음: {DB_PATH}")
        print("  → 프로그램을 한 번 실행하여 data/trading.db를 생성하세요.")
        exit(1)
    return sqlite3.connect(DB_PATH)


def run_query(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return cols, rows


def print_table(title, cols, rows, empty_msg="데이터 없음"):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")
    if not rows:
        print(f"  {empty_msg}")
        return
    # 컬럼 너비 계산
    widths = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(cols)]
    header = "  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))
    print(header)
    print("  " + "-" * (sum(widths) + 2 * len(widths)))
    for row in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def where_clause(days, signal_type):
    """공통 WHERE 절 생성 (완결된 거래만)"""
    conditions = [
        "final_status IN ('COMPLETED', 'SELL_DECIDED')",
        "return_pct IS NOT NULL",
        "return_pct != ''",
        "CAST(return_pct AS REAL) != 0",
    ]
    params = []
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conditions.append("trade_date >= ?")
        params.append(cutoff)
    if signal_type:
        conditions.append("signal_type = ?")
        params.append(signal_type)
    return "WHERE " + " AND ".join(conditions), params


def analyze(days=None, signal_type=None):
    conn = connect()
    w, p = where_clause(days, signal_type)

    # ── 0. 요약 ───────────────────────────────────────────────────────────────
    cols, rows = run_query(conn, f"""
        SELECT
            COUNT(*)                                                      AS 총거래수,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                           THEN 1.0 ELSE 0.0 END) * 100, 1)              AS 승률_pct,
            ROUND(AVG(CAST(return_pct AS REAL)), 2)                       AS 평균수익률_pct,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                           THEN CAST(return_pct AS REAL) END), 2)         AS 평균이익_pct,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) <= 0
                           THEN CAST(return_pct AS REAL) END), 2)         AS 평균손실_pct,
            ROUND(AVG(CAST(holding_minutes AS REAL)), 1)                  AS 평균보유분
        FROM trades {w}
    """, p)
    print_table("전체 요약", cols, rows)

    # ── 1. 체결강도 구간별 성과 ─────────────────────────────────────────────
    cols, rows = run_query(conn, f"""
        SELECT
            CASE
                WHEN CAST(chejan_strength_at_signal AS REAL) < 100  THEN '① <100%  (매도 우세)'
                WHEN CAST(chejan_strength_at_signal AS REAL) < 130  THEN '② 100~130% (균형)'
                WHEN CAST(chejan_strength_at_signal AS REAL) < 200  THEN '③ 130~200% (매수 우세)'
                WHEN CAST(chejan_strength_at_signal AS REAL) < 300  THEN '④ 200~300% (강한 매수)'
                ELSE                                                      '⑤ >300%   (극단 매수)'
            END                                                           AS 체결강도_구간,
            COUNT(*)                                                      AS 건수,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                           THEN 1.0 ELSE 0.0 END) * 100, 1)              AS 승률_pct,
            ROUND(AVG(CAST(return_pct AS REAL)), 2)                       AS 평균수익률_pct
        FROM trades {w}
          AND chejan_strength_at_signal IS NOT NULL
          AND chejan_strength_at_signal != ''
        GROUP BY 체결강도_구간
        ORDER BY MIN(CAST(chejan_strength_at_signal AS REAL))
    """, p)
    print_table("체결강도 구간별 승률", cols, rows,
                "chejan_strength_at_signal 데이터 없음 — 최신 버전으로 기록된 데이터 필요")

    # ── 2. 추세 레벨별 성과 ──────────────────────────────────────────────────
    cols, rows = run_query(conn, f"""
        SELECT
            CASE CAST(trend_level_at_signal AS INTEGER)
                WHEN 0 THEN 'Lv0 횡보'
                WHEN 1 THEN 'Lv1 약세상승'
                WHEN 2 THEN 'Lv2 상승'
                WHEN 3 THEN 'Lv3 강세'
                ELSE '미기록'
            END                                                           AS 추세레벨,
            COUNT(*)                                                      AS 건수,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                           THEN 1.0 ELSE 0.0 END) * 100, 1)              AS 승률_pct,
            ROUND(AVG(CAST(return_pct AS REAL)), 2)                       AS 평균수익률_pct
        FROM trades {w}
        GROUP BY trend_level_at_signal
        ORDER BY trend_level_at_signal
    """, p)
    print_table("추세 레벨별 승률  ← WeakSignalFilter 기준 검증", cols, rows)

    # ── 3. 시간대별 성과 ─────────────────────────────────────────────────────
    cols, rows = run_query(conn, f"""
        SELECT
            CASE
                WHEN signal_time < '09:30' THEN '① 09:00~09:30 (개장)'
                WHEN signal_time < '10:00' THEN '② 09:30~10:00 (오전초)'
                WHEN signal_time < '11:00' THEN '③ 10:00~11:00 (오전)'
                WHEN signal_time < '13:00' THEN '④ 11:00~13:00 (점심)'
                ELSE                            '⑤ 13:00~    (오후)'
            END                                                           AS 시간대,
            COUNT(*)                                                      AS 건수,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                           THEN 1.0 ELSE 0.0 END) * 100, 1)              AS 승률_pct,
            ROUND(AVG(CAST(return_pct AS REAL)), 2)                       AS 평균수익률_pct
        FROM trades {w}
        GROUP BY 시간대
        ORDER BY signal_time
    """, p)
    print_table("시간대별 승률  ← OpeningTimeFilter 효과 검증", cols, rows)

    # ── 4. 신호 타입별 비교 ──────────────────────────────────────────────────
    cols, rows = run_query(conn, f"""
        SELECT
            signal_type                                                   AS 전략,
            COUNT(*)                                                      AS 건수,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                           THEN 1.0 ELSE 0.0 END) * 100, 1)              AS 승률_pct,
            ROUND(AVG(CAST(return_pct AS REAL)), 2)                       AS 평균수익률_pct,
            ROUND(SUM(CAST(realized_pnl AS REAL)) / 1000.0, 0)           AS 누적손익_천원
        FROM trades {w}
        GROUP BY signal_type
        ORDER BY 누적손익_천원 DESC
    """, p)
    print_table("전략별 성과  ← 어떤 전략이 실제로 돈을 버는가", cols, rows)

    # ── 5. 손익비 분포 (기댓값 계산) ────────────────────────────────────────
    cols, rows = run_query(conn, f"""
        WITH stats AS (
            SELECT
                COUNT(*)                                                  AS 총건수,
                AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                         THEN 1.0 ELSE 0.0 END)                          AS 승률,
                ABS(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                             THEN CAST(return_pct AS REAL) END))          AS 평균이익,
                ABS(AVG(CASE WHEN CAST(return_pct AS REAL) <= 0
                             THEN CAST(return_pct AS REAL) END))          AS 평균손실
            FROM trades {w}
        )
        SELECT
            총건수,
            ROUND(승률 * 100, 1)                                          AS 승률_pct,
            ROUND(평균이익, 2)                                             AS 평균이익_pct,
            ROUND(평균손실, 2)                                             AS 평균손실_pct,
            ROUND(평균이익 / NULLIF(평균손실, 0), 2)                      AS 손익비,
            ROUND(승률 * 평균이익 - (1 - 승률) * 평균손실, 3)             AS 기댓값_pct
        FROM stats
    """, p)
    print_table("기댓값 분석  ← 이 숫자가 양수여야 장기 수익", cols, rows)

    # ── 6. 체결강도 × 추세레벨 교차 (최적 조합 탐색) ─────────────────────
    cols, rows = run_query(conn, f"""
        SELECT
            CASE
                WHEN CAST(chejan_strength_at_signal AS REAL) < 130 THEN '체결<130'
                ELSE '체결≥130'
            END                                                           AS 체결강도,
            CASE CAST(trend_level_at_signal AS INTEGER)
                WHEN 0 THEN 'Lv0'
                WHEN 1 THEN 'Lv1'
                WHEN 2 THEN 'Lv2'
                WHEN 3 THEN 'Lv3'
                ELSE '미기록'
            END                                                           AS 추세레벨,
            COUNT(*)                                                      AS 건수,
            ROUND(AVG(CASE WHEN CAST(return_pct AS REAL) > 0
                           THEN 1.0 ELSE 0.0 END) * 100, 1)              AS 승률_pct,
            ROUND(AVG(CAST(return_pct AS REAL)), 2)                       AS 평균수익_pct
        FROM trades {w}
          AND chejan_strength_at_signal IS NOT NULL
          AND chejan_strength_at_signal != ''
          AND trend_level_at_signal IS NOT NULL
        GROUP BY 체결강도, 추세레벨
        HAVING 건수 >= 3
        ORDER BY 승률_pct DESC
    """, p)
    print_table("체결강도 × 추세레벨 조합  ← 진입 조건 최적화 힌트", cols, rows,
                "조합 데이터 부족 (건수 3 이상 필요)")

    conn.close()
    print(f"\n{'─'*60}")
    print("  분석 완료")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="신호 성과 분석")
    parser.add_argument("--days",   type=int,  default=None, help="최근 N일 데이터만 분석 (기본: 전체)")
    parser.add_argument("--signal", type=str,  default=None, help="특정 신호 타입만 (예: JDM_ENTRY)")
    args = parser.parse_args()

    label = []
    if args.days:   label.append(f"최근 {args.days}일")
    if args.signal: label.append(f"신호={args.signal}")
    print(f"\n[신호 성과 분석] {' / '.join(label) if label else '전체 기간'}")

    analyze(days=args.days, signal_type=args.signal)
