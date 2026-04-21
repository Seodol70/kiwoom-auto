# -*- coding: utf-8 -*-
"""
analysis/feedback_engine.py
────────────────────────────
장 마감 후 거래 결과를 분석해 SmartScannerConfig 파라미터를 자동 조정하는 엔진.

흐름:
  FeedbackEngine.run_daily(date)
    → parse_fills()  + parse_audit()
    → classify_losses()
    → compute_adjustments()
    → apply_safety_guards()          ← 바운드 / 일일한도 / 연속일 체크
    → write_adaptive_params()        → config/adaptive_params.json

다음 날 시작 시 SmartScannerConfig.from_adaptive() 가 자동 로드.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 시간 슬롯 정의
# ──────────────────────────────────────────────────────────────────────────────

# (슬롯명, 시작 HH:MM, 종료 HH:MM)
TIME_SLOTS: List[Tuple[str, str, str]] = [
    ("PRE",       "08:00", "09:00"),
    ("OPENING",   "09:00", "09:30"),
    ("MORNING",   "09:30", "11:00"),
    ("MIDDAY",    "11:00", "13:00"),
    ("AFTERNOON", "13:00", "14:30"),
]

# 위험 판정 기준
SLOT_DANGER_WIN_RATE    = 0.40   # 승률 40% 미만
SLOT_DANGER_MIN_TRADES  = 2      # 최소 건수 (건수 부족 시 판정 보류)
SLOT_GOLDEN_WIN_RATE    = 0.60   # 황금 구간 승률 60% 이상

# peak_pnl_history 경로 (프로젝트 루트 기준)
PEAK_HISTORY_PATH = "config/peak_pnl_history.json"
PEAK_HISTORY_DAYS = 5            # 최근 N일 평균 peak 사용
PROFIT_LOCK_RATIO = 0.70         # 평균 peak 의 70% 를 lock 으로 설정
PROFIT_LOCK_MIN   = 30_000       # lock 최솟값 (원) — 너무 작으면 비활성

# 시간 파라미터 식별자 (write_adaptive_params 에서 문자열로 직렬화)
TIME_PARAM_NAMES = frozenset({"entry_end_time", "entry_start_time"})

# health_events.jsonl 요약 경고 임계치
TR_FAIL_THRESHOLD_WARN = 10   # 하루 TR 실패 N회 이상이면 Feedback 로그에 경고

# ──────────────────────────────────────────────────────────────────────────────
# 안전장치 상수
# ──────────────────────────────────────────────────────────────────────────────

PARAM_BOUNDS: Dict[str, Tuple[float, float]] = {
    # 기존 파라미터
    "min_chejan_strength":            (100.0,  500.0),
    "opening_surge_chejan_min":       (100.0,  500.0),
    "trail_activation_pct":           (0.2,    3.0),
    "trail_pct_tier1":                (0.5,    4.0),
    "trail_pct_tier2":                (0.8,    5.0),
    "trail_pct_tier3":                (1.0,    6.0),
    "breakout_confirm_minutes":       (1.0,    6.0),
    "entry_open_surge_max_opening":   (3.0,   15.0),
    "jdm_stop_loss_pct":              (-3.0,  -0.5),
    "volume_surge_mult":              (1.0,    5.0),
    "breakout_ratio":                 (0.005,  0.05),
    "max_change_pct":                 (8.0,   30.0),
    "slippage_block_pct":             (1.0,    8.0),
    # 시간 파라미터 (분-단위 float, 08:00=480, 09:00=540, ..., 14:30=870)
    "entry_start_time":               (480.0,  570.0),   # 08:00 ~ 09:30
    "entry_end_time":                 (600.0,  870.0),   # 10:00 ~ 14:30
    # 시간대별 체결강도 하한 (%)
    "min_chejan_strength_opening":    (100.0,  400.0),
    "min_chejan_strength_morning":    (100.0,  400.0),
    "min_chejan_strength_midday":     (100.0,  400.0),
    "min_chejan_strength_afternoon":  (100.0,  400.0),
    # 시간대별 거래량 배수
    "volume_surge_mult_opening":      (1.0,    5.0),
    "volume_surge_mult_morning":      (1.0,    5.0),
    "volume_surge_mult_midday":       (1.0,    5.0),
    "volume_surge_mult_afternoon":    (1.0,    5.0),
    # 일일 손익 한도
    "daily_profit_lock_won":          (10_000.0, 500_000.0),
    "daily_loss_cut_won":             (-500_000.0, -10_000.0),
    # 전략별 전용 파라미터 (Fix ④ — 전략 유형별 분리 조정)
    "pre_surge_chejan_min":           (100.0,  500.0),
    "opening_surge_chejan_min":       (100.0,  500.0),
}

MAX_DAILY_CHANGE_RATIO  = 0.20   # 하루 최대 ±20% 조정
CONSECUTIVE_REQUIRED    = 2      # 최소 2일 연속 같은 방향 신호
MIN_TRADES_FOR_ANALYSIS = 3      # 분석 최소 체결 건수

# ── 완화(Relaxation) 상수 ─────────────────────────────────────────────────────
RELAX_CONSECUTIVE_DAYS  = 2      # 연속 수익 N일 → 파라미터 완화 트리거
RELAX_STEP_RATIO        = 0.10   # 회당 완화폭 — 현재값과 기본값 차이의 10%씩 원점으로
# 완화 대상 파라미터와 기본값 매핑 (조여진 파라미터를 기본값 방향으로 되돌림)
RELAX_DEFAULTS: Dict[str, float] = {
    "min_chejan_strength":            120.0,
    "min_chejan_strength_opening":    110.0,
    "min_chejan_strength_morning":    120.0,
    "min_chejan_strength_midday":     130.0,
    "min_chejan_strength_afternoon":  150.0,
    "volume_surge_mult":              1.5,
    "volume_surge_mult_opening":      1.2,
    "volume_surge_mult_morning":      1.5,
    "volume_surge_mult_midday":       1.2,
    "volume_surge_mult_afternoon":    1.2,
    "trail_activation_pct":           1.0,
    "trail_pct_tier1":                1.5,
    "entry_open_surge_max_opening":   7.0,
    "breakout_confirm_minutes":       2.0,
    "entry_end_time":                 870.0,   # 14:30 in minutes
    "entry_start_time":               480.0,   # 08:00 in minutes
}


# ──────────────────────────────────────────────────────────────────────────────
# 손실 원인 카테고리
# ──────────────────────────────────────────────────────────────────────────────

class LossCat:
    OPENING_NOISE   = "OPENING_NOISE"    # 장초 체결강도 이상값 → 손실
    HIGH_ENTRY_CHG  = "HIGH_ENTRY_CHG"   # 진입 시 등락률 >8% → 손실
    TRAIL_TOO_TIGHT = "TRAIL_TOO_TIGHT"  # Trail 조기 청산 (수익 < 1%)
    EARLY_REVERSAL  = "EARLY_REVERSAL"   # 보유 ≤10분 + 손절
    STOP_LOSS_HIT   = "STOP_LOSS_HIT"   # 손절 다발


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 클래스
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FillRecord:
    ts:          datetime
    code:        str
    name:        str
    sell_price:  int
    avg_price:   int
    qty:         int
    realized:    int   # 원 (수수료 포함)


@dataclass
class AuditRecord:
    """trade_audit CSV 1행 → Python 객체"""
    trade_date:                 date
    code:                       str
    name:                       str
    signal_type:                str      # BREAKOUT / JDM_ENTRY / PRE_SURGE ...
    signal_time:                str
    signal_price:               float
    chejan_strength_at_signal:  float
    change_pct_at_signal:       float
    sell_reason:                str
    return_pct:                 float
    realized_pnl:               float
    holding_minutes:            float
    final_status:               str      # FILLED / SIGNAL_ONLY / COMPLETED
    investor_score_at_signal:   int   = 0   # 기본값 있는 필드는 뒤에


@dataclass
class ParamAdjustment:
    param:      str
    old_val:    float
    new_val:    float
    reason:     str
    category:   str
    confidence: float   # 0~1


@dataclass
class SlotStat:
    """시간 슬롯별 거래 통계 — analyze_time_slots() 결과"""
    slot:       str    # PRE / OPENING / MORNING / MIDDAY / AFTERNOON
    start_time: str    # "HH:MM"
    end_time:   str    # "HH:MM"
    count:      int    # 체결 거래 건수
    win_count:  int    # 수익 거래 건수
    total_pnl:  float  # 누적 실현손익 (원)
    avg_pnl:    float  # 평균 실현손익 (원)
    win_rate:   float  # 승률 (0~1)
    is_danger:  bool   # 위험 구간: win_rate < 40% AND total_pnl < 0 AND count >= 2
    is_golden:  bool   # 황금 구간: win_rate >= 60% AND total_pnl > 0


@dataclass
class FeedbackResult:
    date:            date
    total_realized:  float
    total_trades:    int
    profitable:      bool
    category_hits:   Dict[str, int]
    adjustments:     List[ParamAdjustment]
    skipped_reasons: List[str]
    applied:         bool
    slot_stats:      List[SlotStat]    = field(default_factory=list)
    peak_pnl:        float             = 0.0   # 오늘 장중 최고 실현손익
    next_profit_lock: int              = 0     # 내일 적용될 daily_profit_lock_won
    report_path:     str               = ""
    telegram_msg:    str               = ""    # LogAnalyzer가 생성한 텔레그램 메시지


# ──────────────────────────────────────────────────────────────────────────────
# FeedbackEngine
# ──────────────────────────────────────────────────────────────────────────────

class FeedbackEngine:

    def __init__(
        self,
        log_dir:       str = "logs",
        adaptive_path: str = "config/adaptive_params.json",
    ):
        self.log_dir       = Path(log_dir)
        self.adaptive_path = Path(adaptive_path)

    # ── 파싱 ──────────────────────────────────────────────────────────────────

    def parse_fills(self, target_date: date) -> List[FillRecord]:
        """fills_YYYYMMDD.jsonl → FillRecord 리스트"""
        path = self.log_dir / f"fills_{target_date.strftime('%Y%m%d')}.jsonl"
        if not path.exists():
            logger.warning("[Feedback] fills 파일 없음: %s", path)
            return []

        records: List[FillRecord] = []
        seen_code_max: Dict[str, FillRecord] = {}   # 종목별 최대 qty (중복 partial fill 제거)

        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    rec = FillRecord(
                        ts         = datetime.fromisoformat(d["ts"]),
                        code       = d["code"],
                        name       = d["name"],
                        sell_price = int(d["sell_price"]),
                        avg_price  = int(d["avg_price"]),
                        qty        = int(d["qty"]),
                        realized   = int(d["realized"]),
                    )
                    records.append(rec)
                    # 종목별 최대 qty 체결 기록 유지
                    key = f"{rec.code}_{rec.avg_price}"
                    if key not in seen_code_max or rec.qty > seen_code_max[key].qty:
                        seen_code_max[key] = rec
                except Exception as e:
                    logger.debug("[Feedback] fills 파싱 오류: %s — %s", line[:60], e)

        logger.info("[Feedback] fills 파싱: %d행 → %d거래", len(records), len(seen_code_max))
        return list(seen_code_max.values())

    def parse_audit(self, target_date: date) -> List[AuditRecord]:
        """trade_audit_YYYYMMDD.csv → AuditRecord 리스트"""
        path = self.log_dir / f"trade_audit_{target_date.strftime('%Y%m%d')}.csv"
        if not path.exists():
            logger.warning("[Feedback] audit 파일 없음: %s", path)
            return []

        import csv
        records: List[AuditRecord] = []

        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                def _f(k: str) -> float:
                    v = row.get(k, "")
                    try:
                        return float(v) if v.strip() else 0.0
                    except ValueError:
                        return 0.0

                def _s(k: str) -> str:
                    return str(row.get(k, "") or "")

                try:
                    rec = AuditRecord(
                        trade_date                = date.fromisoformat(_s("trade_date")),
                        code                      = _s("code"),
                        name                      = _s("name"),
                        signal_type               = _s("signal_type"),
                        signal_time               = _s("signal_time"),
                        signal_price              = _f("signal_price"),
                        chejan_strength_at_signal = _f("chejan_strength_at_signal"),
                        change_pct_at_signal      = _f("change_pct_at_signal"),
                        sell_reason               = _s("sell_reason"),
                        return_pct                = _f("return_pct"),
                        realized_pnl              = _f("realized_pnl"),
                        holding_minutes           = _f("holding_minutes"),
                        final_status              = _s("final_status"),
                    )
                    records.append(rec)
                except Exception as e:
                    logger.debug("[Feedback] audit 파싱 오류: %s", e)

        logger.info("[Feedback] audit 파싱: %d행", len(records))
        return records

    # ── 손실 원인 분류 ────────────────────────────────────────────────────────

    def classify_losses(
        self, audits: List[AuditRecord]
    ) -> Dict[str, List[AuditRecord]]:
        """FILLED/COMPLETED 거래를 카테고리별로 분류 (중복 허용)"""
        result: Dict[str, List[AuditRecord]] = defaultdict(list)
        for r in audits:
            if r.final_status not in ("FILLED", "COMPLETED"):
                continue
            for cat in self._categorize(r):
                result[cat].append(r)
        return dict(result)

    def _categorize(self, r: AuditRecord) -> List[str]:
        cats: List[str] = []

        # ① OPENING_NOISE — 체결강도 > 5000% (장 초반 API 이상값) + 손실
        if r.chejan_strength_at_signal > 5_000 and r.realized_pnl < 0:
            cats.append(LossCat.OPENING_NOISE)

        # ② HIGH_ENTRY_CHG — 진입 등락률 > 8% + 손실
        if r.change_pct_at_signal > 8.0 and r.realized_pnl < 0:
            cats.append(LossCat.HIGH_ENTRY_CHG)

        # ③ TRAIL_TOO_TIGHT — 트레일 청산 + 수익 < 1.0%
        if "트레일스탑" in r.sell_reason or "Trail" in r.sell_reason:
            if r.return_pct < 1.0:   # 수익이든 손실이든 1% 미만이면 너무 빠름
                cats.append(LossCat.TRAIL_TOO_TIGHT)

        # ④ EARLY_REVERSAL — 보유 ≤ 10분 + 손절
        if (r.holding_minutes > 0
                and r.holding_minutes <= 10.0
                and "손절" in r.sell_reason
                and r.realized_pnl < 0):
            cats.append(LossCat.EARLY_REVERSAL)

        # ⑤ STOP_LOSS_HIT — 손절 발생 (집계용)
        if "손절" in r.sell_reason and r.realized_pnl < 0:
            cats.append(LossCat.STOP_LOSS_HIT)

        return cats

    def classify_losses_by_strategy(
        self, audits: List[AuditRecord]
    ) -> Dict[str, Dict[str, List[AuditRecord]]]:
        """
        전략 유형(signal_type)별로 손실 원인을 분류한다.

        반환: { signal_type → { LossCat → [AuditRecord] } }
        예: { "BREAKOUT": {"STOP_LOSS_HIT": [...]}, "JDM_ENTRY": {...} }
        """
        by_strategy: Dict[str, Dict[str, List[AuditRecord]]] = defaultdict(lambda: defaultdict(list))
        for r in audits:
            if r.final_status not in ("FILLED", "COMPLETED"):
                continue
            sig = r.signal_type or "UNKNOWN"
            for cat in self._categorize(r):
                by_strategy[sig][cat].append(r)
        return {k: dict(v) for k, v in by_strategy.items()}

    # ── 파라미터 조정값 산출 ──────────────────────────────────────────────────

    def compute_adjustments(
        self,
        category_hits: Dict[str, List[AuditRecord]],
        current_params: Dict[str, float],
        signal_type:    str = "",   # 빈 문자열이면 전략 구분 없이 공통 파라미터 조정
    ) -> List[ParamAdjustment]:
        """
        signal_type 이 지정되면 해당 전략 전용 파라미터를 우선 조정.
        미지정 시 기존 동작(공통 파라미터 조정) 유지.
        """
        adjs: List[ParamAdjustment] = []
        sig = (signal_type or "").upper()

        def cur(key: str, default: float) -> float:
            return current_params.get(key, default)

        # 전략 유형별 파라미터 키 매핑 — Fix ④
        # 각 전략 손실에 대응되는 체결강도/거래량 파라미터를 구분
        # ex) BREAKOUT 손실 → breakout 전용 파라미터 / JDM 손실 → jdm 전용 파라미터
        _sig_chejan_key = {
            "BREAKOUT":  "min_chejan_strength",        # BREAKOUT은 공통 체결강도
            "JDM_ENTRY": "min_chejan_strength",        # JDM도 공통 체결강도 (슬롯별 적용)
            "PRE_SURGE": "pre_surge_chejan_min",       # PRE_SURGE 전용 파라미터
            "OPENING_SCALP": "opening_surge_chejan_min",
        }
        _sig_vol_key = {
            "BREAKOUT":  "volume_surge_mult",
            "JDM_ENTRY": "volume_surge_mult",
            "PRE_SURGE": "volume_surge_mult",
        }
        chejan_key = _sig_chejan_key.get(sig, "min_chejan_strength")
        vol_key    = _sig_vol_key.get(sig, "volume_surge_mult")
        sig_label  = f"[{sig}] " if sig else ""

        # ① OPENING_NOISE → entry_open_surge_max_opening 하향 + chejan 기준 상향
        if hits := category_hits.get(LossCat.OPENING_NOISE):
            n = len(hits)
            old = cur("entry_open_surge_max_opening", 7.0)
            factor = max(0.85, 1.0 - 0.03 * n)   # 건당 3% 하향, 최대 15%
            new = round(old * factor, 1)
            adjs.append(ParamAdjustment(
                param="entry_open_surge_max_opening", old_val=old, new_val=new,
                reason=f"{sig_label}OPENING_NOISE {n}건 — OPENING 슬롯 등락률 상한 하향",
                category=LossCat.OPENING_NOISE, confidence=0.7,
            ))

        # ② HIGH_ENTRY_CHG → entry_open_surge_max_opening 하향 (OPENING 이외)
        if hits := category_hits.get(LossCat.HIGH_ENTRY_CHG):
            avg_chg = sum(r.change_pct_at_signal for r in hits) / len(hits)
            old = cur("entry_open_surge_max_opening", 7.0)
            candidate = round(min(old * 0.90, avg_chg * 0.85), 1)
            new = max(candidate, PARAM_BOUNDS["entry_open_surge_max_opening"][0])
            if new < old:
                adjs.append(ParamAdjustment(
                    param="entry_open_surge_max_opening", old_val=old, new_val=new,
                    reason=f"{sig_label}HIGH_ENTRY_CHG {len(hits)}건, 평균진입등락률 {avg_chg:.1f}%",
                    category=LossCat.HIGH_ENTRY_CHG, confidence=0.75,
                ))

        # ③ TRAIL_TOO_TIGHT → trail_activation_pct + trail_pct_tier1 상향
        if hits := category_hits.get(LossCat.TRAIL_TOO_TIGHT):
            n = len(hits)
            old_act = cur("trail_activation_pct", 0.5)
            new_act = round(old_act * 1.15, 2)
            adjs.append(ParamAdjustment(
                param="trail_activation_pct", old_val=old_act, new_val=new_act,
                reason=f"{sig_label}TRAIL_TOO_TIGHT {n}건 — 활성화 기준 상향",
                category=LossCat.TRAIL_TOO_TIGHT, confidence=0.65,
            ))
            old_t1 = cur("trail_pct_tier1", 1.0)
            new_t1 = round(old_t1 * 1.10, 2)
            adjs.append(ParamAdjustment(
                param="trail_pct_tier1", old_val=old_t1, new_val=new_t1,
                reason=f"{sig_label}TRAIL_TOO_TIGHT {n}건 — tier1 trail 폭 확대",
                category=LossCat.TRAIL_TOO_TIGHT, confidence=0.60,
            ))

        # ④ EARLY_REVERSAL → breakout_confirm_minutes 상향 (BREAKOUT/JDM 전략만)
        if hits := category_hits.get(LossCat.EARLY_REVERSAL):
            if not sig or sig in ("BREAKOUT", "JDM_ENTRY", ""):
                old = cur("breakout_confirm_minutes", 2.0)
                new = round(min(old + 0.5, PARAM_BOUNDS["breakout_confirm_minutes"][1]), 1)
                avg_hold = sum(r.holding_minutes for r in hits) / len(hits)
                adjs.append(ParamAdjustment(
                    param="breakout_confirm_minutes", old_val=old, new_val=new,
                    reason=f"{sig_label}EARLY_REVERSAL {len(hits)}건, 평균보유 {avg_hold:.1f}분 — 관찰시간 연장",
                    category=LossCat.EARLY_REVERSAL, confidence=0.65,
                ))

        # ⑤ STOP_LOSS_HIT ≥ 4건 → 전략별 체결강도 파라미터 상향
        if hits := category_hits.get(LossCat.STOP_LOSS_HIT):
            if len(hits) >= 4:
                lo_bound, hi_bound = PARAM_BOUNDS.get(chejan_key, (100.0, 500.0))
                old = cur(chejan_key, 120.0)
                factor = min(1.12, 1.04 + 0.02 * (len(hits) - 4))
                new = round(min(old * factor, hi_bound), 1)
                adjs.append(ParamAdjustment(
                    param=chejan_key, old_val=old, new_val=new,
                    reason=f"{sig_label}STOP_LOSS_HIT {len(hits)}건 — 진입 체결강도 기준 강화",
                    category=LossCat.STOP_LOSS_HIT, confidence=0.80,
                ))

        # 중복 파라미터가 있으면 가장 보수적인 값(변화가 작은 것)으로 병합
        adjs = self._merge_duplicate_params(adjs)
        return adjs

    def compute_time_slot_adjustments(
        self,
        slot_stats:     List[SlotStat],
        current_params: Dict[str, float],
    ) -> List[ParamAdjustment]:
        """
        시간대별 수익 통계를 분석해 파라미터 조정 제안을 생성한다.

        조정 항목:
        - entry_end_time   : 연속 위험 슬롯이 이어지면 진입 종료 시간 앞당김
        - entry_start_time : OPENING 위험이면 진입 시작 시간 늦춤
        - min_chejan_strength_{slot}: 위험 슬롯 체결강도 기준 상향
        - volume_surge_mult_{slot}  : 위험 슬롯 거래량 배수 상향

        시간 파라미터는 분-단위 float 으로 저장 (08:00=480, 13:00=780 …).
        write_adaptive_params()에서 "HH:MM:SS" 문자열로 변환됨.
        """
        adjs: List[ParamAdjustment] = []

        def _cur(key: str, default: float) -> float:
            return float(current_params.get(key, default))

        def _to_min(hhmm: str) -> float:
            h, m = int(hhmm[:2]), int(hhmm[3:5])
            return float(h * 60 + m)

        slot_map = {s.slot: s for s in slot_stats}

        # ── ① entry_end_time 조정 ──────────────────────────────────────────
        # AFTERNOON 위험 → entry_end_time 13:00 (780분)
        # MIDDAY도 위험 → entry_end_time 11:00 (660분)
        afternoon = slot_map.get("AFTERNOON")
        midday    = slot_map.get("MIDDAY")
        cur_end   = _cur("entry_end_time", _to_min("14:30"))

        if afternoon and afternoon.is_danger:
            target_end = _to_min("13:00") if not (midday and midday.is_danger) else _to_min("11:00")
            if target_end < cur_end:
                adjs.append(ParamAdjustment(
                    param="entry_end_time", old_val=cur_end, new_val=target_end,
                    reason=(
                        f"AFTERNOON 위험구간 (승률 {afternoon.win_rate*100:.0f}%, "
                        f"손익 {afternoon.total_pnl:+,.0f}원, {afternoon.count}건)"
                        + (f" + MIDDAY 위험" if midday and midday.is_danger else "")
                    ),
                    category="TIME_SLOT", confidence=0.75,
                ))

        # ── ② entry_start_time 조정 ───────────────────────────────────────
        # OPENING 위험 → entry_start_time 09:30 (570분)
        opening   = slot_map.get("OPENING")
        cur_start = _cur("entry_start_time", _to_min("08:00"))
        if opening and opening.is_danger and cur_start < _to_min("09:30"):
            adjs.append(ParamAdjustment(
                param="entry_start_time", old_val=cur_start, new_val=_to_min("09:30"),
                reason=(
                    f"OPENING 위험구간 (승률 {opening.win_rate*100:.0f}%, "
                    f"손익 {opening.total_pnl:+,.0f}원, {opening.count}건)"
                ),
                category="TIME_SLOT", confidence=0.70,
            ))

        # ── ③ 슬롯별 체결강도 / 거래량 배수 상향 (위험 슬롯) ──────────────
        slot_key_map = {
            "OPENING":   "opening",
            "MORNING":   "morning",
            "MIDDAY":    "midday",
            "AFTERNOON": "afternoon",
        }
        for s in slot_stats:
            sfx = slot_key_map.get(s.slot)
            if sfx is None or not s.is_danger:
                continue

            # 체결강도 +10%
            cj_key = f"min_chejan_strength_{sfx}"
            old_cj = _cur(cj_key, 120.0)
            new_cj = round(old_cj * 1.10, 1)
            adjs.append(ParamAdjustment(
                param=cj_key, old_val=old_cj, new_val=new_cj,
                reason=(
                    f"{s.slot} 위험구간 — 진입 체결강도 기준 10% 상향 "
                    f"(승률 {s.win_rate*100:.0f}%, {s.count}건)"
                ),
                category="TIME_SLOT", confidence=0.65,
            ))

            # 거래량 배수 +0.3
            vol_key = f"volume_surge_mult_{sfx}"
            old_vol = _cur(vol_key, 1.5)
            new_vol = round(old_vol + 0.3, 1)
            adjs.append(ParamAdjustment(
                param=vol_key, old_val=old_vol, new_val=new_vol,
                reason=(
                    f"{s.slot} 위험구간 — 거래량 배수 기준 상향 "
                    f"(손익 {s.total_pnl:+,.0f}원, {s.count}건)"
                ),
                category="TIME_SLOT", confidence=0.60,
            ))

        return adjs

    def compute_relaxation_adjustments(
        self,
        current_params: Dict[str, float],
        history:        List[Dict],
    ) -> List[ParamAdjustment]:
        """
        연속 수익 N일(RELAX_CONSECUTIVE_DAYS) 이 충족되면
        이전에 조여진 파라미터를 기본값(RELAX_DEFAULTS) 방향으로 RELAX_STEP_RATIO 만큼 완화.

        원리:
          - 현재값이 기본값보다 strict(체결강도↑, 거래량↑, 진입폭↓ 등)하면 완화 제안
          - 현재값이 이미 기본값 이하이면 스킵 (너무 풀어주지 않음)
          - RELAX_STEP_RATIO = 10% → 차이의 10%씩 매일 기본값 쪽으로 수렴
        """
        # 연속 수익일 카운트 — history 에서 applied=True + 손익 > 0 날짜 연속 체크
        # 간단히: 최근 history 에 "RELAX" 방향 신호가 CONSECUTIVE_REQUIRED 일 이상 있는지
        # → 실제로는 audit 기반이지만 history 에 profit_day 마커를 쓰는 게 깔끔함
        # 여기서는 호출부(run_daily)에서 연속 수익일 카운트를 직접 계산해 넘겨줌.
        # (이 메서드는 조건 판단은 호출부에서 하고, 조정값만 생성)

        adjs: List[ParamAdjustment] = []

        def _cur(key: str) -> float:
            return float(current_params.get(key, RELAX_DEFAULTS.get(key, 0.0)))

        for param, default_val in RELAX_DEFAULTS.items():
            cur_val = _cur(param)

            # 현재값이 기본값보다 strict(더 빡빡)한 경우만 완화
            # 체결강도/거래량/confirm_minutes 는 높을수록 strict
            # entry_open_surge/trail 은 낮을수록 strict
            # entry_end_time 은 낮을수록 strict (진입 종료 시간 당겨짐)
            # entry_start_time 은 높을수록 strict (진입 시작 시간 늦춰짐)
            strict_params = {
                "entry_open_surge_max_opening",
                "trail_activation_pct",
                "trail_pct_tier1",
                "entry_end_time",
            }
            relax_params_high = {   # 현재값 > default 이면 strict → 낮춰야 완화
                "min_chejan_strength", "min_chejan_strength_opening",
                "min_chejan_strength_morning", "min_chejan_strength_midday",
                "min_chejan_strength_afternoon",
                "volume_surge_mult", "volume_surge_mult_opening",
                "volume_surge_mult_morning", "volume_surge_mult_midday",
                "volume_surge_mult_afternoon",
                "breakout_confirm_minutes",
                "entry_start_time",
            }

            if param in relax_params_high:
                # 현재값이 기본값보다 높아야 strict → 낮춰서 완화
                if cur_val <= default_val + 1e-6:
                    continue
                gap     = cur_val - default_val
                new_val = round(cur_val - gap * RELAX_STEP_RATIO, 3)
                new_val = max(new_val, default_val)   # 기본값 아래로 내려가지 않음
            else:
                # 현재값이 기본값보다 낮아야 strict → 높여서 완화
                if cur_val >= default_val - 1e-6:
                    continue
                gap     = default_val - cur_val
                new_val = round(cur_val + gap * RELAX_STEP_RATIO, 3)
                new_val = min(new_val, default_val)   # 기본값 위로 올라가지 않음

            if abs(new_val - cur_val) < 1e-6:
                continue

            adjs.append(ParamAdjustment(
                param=param, old_val=cur_val, new_val=new_val,
                reason=f"연속 수익 {RELAX_CONSECUTIVE_DAYS}일 — 파라미터 완화 (기본값 {default_val} 방향, {RELAX_STEP_RATIO*100:.0f}%/일)",
                category="RELAX",
                confidence=0.55,
            ))

        return adjs

    def _count_profitable_streak(self, history: List[Dict]) -> int:
        """
        history 에서 'profit_day' 마커를 읽어 최근 연속 수익일 수를 반환.
        날짜 갭(5달력일 초과)이 발생하면 중단.
        """
        MAX_GAP_DAYS = 5
        profit_entries = sorted(
            [e for e in history if e.get("param") == "_profit_day_"],
            key=lambda x: x["date"],
            reverse=True,
        )
        streak   = 0
        prev_str = None
        for entry in profit_entries:
            cur_str = entry.get("date", "")
            if prev_str is not None:
                try:
                    gap = (
                        date.fromisoformat(prev_str)
                        - date.fromisoformat(cur_str)
                    ).days
                    if gap > MAX_GAP_DAYS:
                        break
                except ValueError:
                    break
            if entry.get("profitable", False):
                streak  += 1
                prev_str = cur_str
            else:
                break
        return streak

    def _merge_duplicate_params(
        self, adjs: List[ParamAdjustment]
    ) -> List[ParamAdjustment]:
        """같은 파라미터 조정이 여러 개 있으면 가장 보수적인 값 하나만 남김"""
        merged: Dict[str, ParamAdjustment] = {}
        for adj in adjs:
            if adj.param not in merged:
                merged[adj.param] = adj
            else:
                prev = merged[adj.param]
                # 변화량이 작은(보수적인) 쪽 선택
                if abs(adj.new_val - adj.old_val) < abs(prev.new_val - prev.old_val):
                    merged[adj.param] = adj
        return list(merged.values())

    # ── 안전장치 ──────────────────────────────────────────────────────────────

    def apply_safety_guards(
        self,
        adjustments:    List[ParamAdjustment],
        current_params: Dict[str, float],
        history:        List[Dict],
    ) -> Tuple[List[ParamAdjustment], List[str]]:
        """
        3단계 안전장치:
        1. 하드 바운드 (PARAM_BOUNDS) — 클램핑
        2. 일일 최대 ±20% 조정폭 — 클램핑
        3. 연속 N일 같은 방향 — 통과 여부 결정
        """
        approved: List[ParamAdjustment] = []
        skipped:  List[str]             = []

        for adj in adjustments:
            # ── 1. 하드 바운드 클램핑
            lo, hi = PARAM_BOUNDS.get(adj.param, (-1e9, 1e9))
            clamped_new = max(lo, min(hi, adj.new_val))
            if clamped_new != adj.new_val:
                skipped.append(
                    f"{adj.param}: {adj.new_val:.3f} → 바운드({lo}~{hi}) 클램핑 → {clamped_new:.3f}"
                )
                adj = replace(adj, new_val=round(clamped_new, 3))

            # 변화가 없으면 스킵
            if abs(adj.new_val - adj.old_val) < 1e-6:
                skipped.append(f"{adj.param}: 변화량 없음 — 스킵")
                continue

            # ── 2. 일일 최대 ±20% 조정폭
            max_delta = abs(adj.old_val) * MAX_DAILY_CHANGE_RATIO
            delta     = adj.new_val - adj.old_val
            if abs(delta) > max_delta:
                clamped_new = adj.old_val + (max_delta if delta > 0 else -max_delta)
                skipped.append(
                    f"{adj.param}: 조정폭 {delta:+.3f} → 20%한도({max_delta:.3f}) 클램핑"
                )
                adj = replace(adj, new_val=round(clamped_new, 3))

            # ── 3. 연속 N일 체크
            direction  = 1 if (adj.new_val > adj.old_val) else -1
            consec     = self._count_consecutive(adj.param, direction, history)
            if consec < CONSECUTIVE_REQUIRED - 1:
                # 오늘이 첫 신호 → history에는 기록되지만 실제 적용 안 함
                skipped.append(
                    f"{adj.param}: 연속 {consec+1}일 신호 (필요 {CONSECUTIVE_REQUIRED}일) — 다음날 적용 예정"
                )
                continue

            approved.append(adj)

        return approved, skipped

    def _count_consecutive(
        self, param: str, direction: int, history: List[Dict]
    ) -> int:
        """
        history(과거 기록)에서 같은 파라미터가 같은 방향으로 조정된 연속 횟수.

        '연속'의 정의: 인접한 두 기록의 날짜 차이가 3 영업일(달력일 기준 5일) 이내.
        날짜 갭이 벌어지면 연속 카운트를 0으로 리셋 — 오래된 신호가 오늘 신호와
        "연속"으로 합산되는 버그 방지.
        """
        MAX_GAP_DAYS = 5   # 달력일 기준 — 주말(2일) + 공휴일(1일) 여유 포함

        # param 기록만 날짜 내림차순 추출
        param_entries = sorted(
            [e for e in history if e.get("param") == param],
            key=lambda x: x["date"],
            reverse=True,
        )

        count = 0
        prev_date_str: Optional[str] = None

        for entry in param_entries:
            cur_date_str = entry.get("date", "")

            # 날짜 갭 체크 — 이전 기록과 5달력일 초과 차이면 연속 중단
            if prev_date_str is not None:
                try:
                    gap = (
                        date.fromisoformat(prev_date_str)
                        - date.fromisoformat(cur_date_str)
                    ).days
                    if gap > MAX_GAP_DAYS:
                        break   # 날짜 갭 → 연속 끊김
                except ValueError:
                    break

            prev_dir = 1 if entry["new_val"] > entry["old_val"] else -1
            if prev_dir == direction:
                count += 1
                prev_date_str = cur_date_str
            else:
                break   # 방향 전환 → 연속 끊김

        return count

    # ── JSON 읽기/쓰기 ────────────────────────────────────────────────────────

    def load_adaptive_params(self) -> Tuple[Dict[str, float], List[Dict]]:
        """(현재 파라미터 dict, 변경 이력 list) 반환. 파일 없으면 ({}, [])"""
        if not self.adaptive_path.exists():
            return {}, []
        try:
            with open(self.adaptive_path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("params", {}), data.get("history", [])
        except Exception as e:
            logger.error("[Feedback] adaptive_params 로드 실패: %s", e)
            return {}, []

    def write_adaptive_params(
        self,
        approved:        List[ParamAdjustment],
        existing_params: Dict[str, float],
        history:         List[Dict],
        target_date:     date,
        pending:         List[ParamAdjustment] | None = None,
    ) -> None:
        """
        approved를 existing_params에 반영하고 JSON 파일로 원자적 저장.
        pending: 이번엔 스킵됐지만 다음날 적용 검토를 위해 history에 기록할 항목.
        """
        def _serialize_param_val(param: str, val: float):
            """시간 파라미터(분-float) → "HH:MM:SS" 문자열로 직렬화."""
            if param in TIME_PARAM_NAMES:
                total_min = int(round(val))
                h, m = divmod(total_min, 60)
                return f"{h:02d}:{m:02d}:00"
            return val

        new_params  = dict(existing_params)
        new_history = list(history)

        for adj in approved:
            new_params[adj.param] = _serialize_param_val(adj.param, adj.new_val)
            new_history.append({
                "date":     target_date.isoformat(),
                "param":    adj.param,
                "old_val":  adj.old_val,
                "new_val":  adj.new_val,
                "reason":   adj.reason,
                "category": adj.category,
                "applied":  True,
            })

        # pending은 direction만 기록 (연속일 카운트용, 값은 변경하지 않음)
        for adj in (pending or []):
            new_history.append({
                "date":     target_date.isoformat(),
                "param":    adj.param,
                "old_val":  adj.old_val,
                "new_val":  adj.new_val,   # 제안값 (미적용)
                "reason":   adj.reason + " [PENDING — 미적용]",
                "category": adj.category,
                "applied":  False,
            })

        payload = {
            "last_updated": target_date.isoformat(),
            "params":       new_params,
            "history":      new_history,
        }

        self.adaptive_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.adaptive_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.adaptive_path)   # 원자적 교체
        logger.info(
            "[Feedback] adaptive_params 저장: %d파라미터 적용, 누적이력 %d건",
            len(approved), len(new_history),
        )

    # ── 롤백 ──────────────────────────────────────────────────────────────────

    def rollback(self, target_date: Optional[date] = None) -> bool:
        """
        target_date 이전 상태로 롤백.
        None 이면 파일 삭제 (완전 초기화 → 기본값 사용).
        """
        if target_date is None:
            if self.adaptive_path.exists():
                self.adaptive_path.unlink()
                logger.info("[Feedback] adaptive_params 전체 초기화 완료")
            return True

        params, history = self.load_adaptive_params()
        filtered = [
            h for h in history
            if date.fromisoformat(h["date"]) < target_date
        ]
        replayed = self._replay_history(filtered)
        # write_adaptive_params에 빈 approved 주면 기존 파람+이력만 씀
        self.write_adaptive_params([], replayed, filtered, target_date)
        logger.info("[Feedback] %s 이전 상태로 롤백 완료", target_date)
        return True

    def _replay_history(self, history: List[Dict]) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for entry in sorted(history, key=lambda x: x["date"]):
            if entry.get("applied", True):
                result[entry["param"]] = entry["new_val"]
        return result

    # ── 시간대 분석 ───────────────────────────────────────────────────────────

    def analyze_time_slots(self, audits: List[AuditRecord]) -> List[SlotStat]:
        """
        거래 내역을 TIME_SLOTS 버킷으로 분류해 슬롯별 수익 통계를 산출한다.
        signal_time (HH:MM:SS 문자열) 기준.
        """
        filled = [a for a in audits if a.final_status in ("FILLED", "COMPLETED")]
        stats: List[SlotStat] = []

        for slot_name, start_str, end_str in TIME_SLOTS:
            sh, sm = int(start_str[:2]), int(start_str[3:5])
            eh, em = int(end_str[:2]),   int(end_str[3:5])
            start_min = sh * 60 + sm
            end_min   = eh * 60 + em

            bucket = []
            for a in filled:
                t = a.signal_time  # "HH:MM:SS" or ""
                if not t:
                    continue
                try:
                    parts = t.split(":")
                    sig_min = int(parts[0]) * 60 + int(parts[1])
                except (ValueError, IndexError):
                    continue
                if start_min <= sig_min < end_min:
                    bucket.append(a)

            count     = len(bucket)
            win_count = sum(1 for a in bucket if a.realized_pnl > 0)
            total_pnl = sum(a.realized_pnl for a in bucket)
            avg_pnl   = total_pnl / count if count > 0 else 0.0
            win_rate  = win_count / count  if count > 0 else 0.0

            is_danger = (
                count >= SLOT_DANGER_MIN_TRADES
                and win_rate < SLOT_DANGER_WIN_RATE
                and total_pnl < 0
            )
            is_golden = (
                count >= SLOT_DANGER_MIN_TRADES
                and win_rate >= SLOT_GOLDEN_WIN_RATE
                and total_pnl > 0
            )

            stats.append(SlotStat(
                slot=slot_name, start_time=start_str, end_time=end_str,
                count=count, win_count=win_count, total_pnl=total_pnl,
                avg_pnl=avg_pnl, win_rate=win_rate,
                is_danger=is_danger, is_golden=is_golden,
            ))

        logger.info(
            "[Feedback] 슬롯 분석: %s",
            " | ".join(
                f"{s.slot}({s.count}건,{s.win_rate*100:.0f}%,{s.total_pnl:+,.0f}원"
                f"{'⚠️' if s.is_danger else '✅' if s.is_golden else ''})"
                for s in stats
            ),
        )
        return stats

    # ── peak P&L 추적 ─────────────────────────────────────────────────────────

    def _analyze_peak_pnl(self, fills: List[FillRecord]) -> float:
        """
        오늘 체결 이력(FillRecord)의 시계열 누적 realized 를 계산,
        장중 최고점(peak)을 반환한다.
        fills 가 없거나 손익이 전부 음수면 0 반환.
        """
        if not fills:
            return 0.0
        sorted_fills = sorted(fills, key=lambda f: f.ts)
        cumulative = 0.0
        peak       = 0.0
        for f in sorted_fills:
            cumulative += f.realized
            if cumulative > peak:
                peak = cumulative
        return peak

    def _peak_history_path(self) -> Path:
        path = Path(PEAK_HISTORY_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_peak_history(self) -> List[Dict]:
        """config/peak_pnl_history.json → list[{date, peak_pnl}]"""
        p = self._peak_history_path()
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[Feedback] peak_history 로드 실패: %s", e)
            return []

    def _save_peak_history(self, target_date: date, peak_pnl: float) -> None:
        """오늘 peak_pnl 을 history 에 추가 (최근 30일분만 보관)."""
        history = self._load_peak_history()
        # 같은 날짜 이미 있으면 갱신
        history = [h for h in history if h.get("date") != target_date.isoformat()]
        history.append({"date": target_date.isoformat(), "peak_pnl": peak_pnl})
        # 최근 30일분만
        history = sorted(history, key=lambda h: h["date"])[-30:]
        p = self._peak_history_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        logger.info("[Feedback] peak_pnl 저장: %s → %.0f원", target_date, peak_pnl)

    def _compute_next_profit_lock(self) -> int:
        """
        최근 PEAK_HISTORY_DAYS 일 평균 peak 의 PROFIT_LOCK_RATIO 배를
        다음날 daily_profit_lock_won 으로 반환.
        데이터 부족 시 0 반환 (= 변경 안 함).
        """
        history = self._load_peak_history()
        recent  = [h["peak_pnl"] for h in history[-PEAK_HISTORY_DAYS:] if h["peak_pnl"] > 0]
        if not recent:
            return 0
        avg_peak = sum(recent) / len(recent)
        lock     = int(avg_peak * PROFIT_LOCK_RATIO)
        if lock < PROFIT_LOCK_MIN:
            logger.debug(
                "[Feedback] 계산된 profit_lock=%d 가 최솟값(%d) 미만 — 변경 안 함",
                lock, PROFIT_LOCK_MIN,
            )
            return 0
        return lock

    # ── 메인 진입점 ───────────────────────────────────────────────────────────

    def run_daily(self, target_date: Optional[date] = None) -> FeedbackResult:
        """
        장 마감 후 호출. FeedbackResult 반환.

        흐름:
          1. fills / audit 파싱
          2. 시간대 분석 + peak_pnl 추적 (수익/손실 무관 항상 실행)
          3. peak_history → 다음날 daily_profit_lock_won 산출
          4-A. 손실 당일: 전략 유형별(Fix ④) 손실 원인 분류 + 파라미터 조정
          4-B. 수익 연속 N일(Fix ①): 파라미터 완화 조정
          5. 시간대 조정 + 손실/완화 조정 합산 → safety guards → write
          6. 수익/손실 당일 마커를 history에 기록 (다음날 연속 카운트용)
        """
        target_date = target_date or date.today()
        logger.info("[Feedback] 분석 시작: %s", target_date)

        # ── 당일 헬스 이벤트 요약 (health_monitor가 기록한 JSONL) ──────────────
        self._log_health_summary()

        fills  = self.parse_fills(target_date)
        audits = self.parse_audit(target_date)

        filled         = [a for a in audits if a.final_status in ("FILLED", "COMPLETED")]
        total_realized = sum(a.realized_pnl for a in filled)
        profitable     = total_realized > 0

        # ── Step 1: 시간대 분석 + peak_pnl (항상 실행) ─────────────────────
        slot_stats = self.analyze_time_slots(audits)
        peak_pnl   = self._analyze_peak_pnl(fills)
        self._save_peak_history(target_date, peak_pnl)
        next_lock  = self._compute_next_profit_lock()

        existing_params, history = self.load_adaptive_params()

        # 시간대 기반 조정 제안 (수익 당일도 실행 — 오전/오후 패턴 보정)
        time_adjs = self.compute_time_slot_adjustments(slot_stats, existing_params)

        # ── Step 2-A: 손실 원인 조정 — 전략 유형별 분리 (Fix ④) ────────────
        loss_adjs:      List[ParamAdjustment] = []
        category_counts: Dict[str, int]       = {}

        if not profitable and len(filled) >= MIN_TRADES_FOR_ANALYSIS:
            # 전체 카테고리 집계 (표시용)
            category_hits_all = self.classify_losses(audits)
            category_counts   = {k: len(v) for k, v in category_hits_all.items()}
            logger.info("[Feedback] 카테고리 분류(전체): %s", category_counts)

            # 전략 유형별 분리 조정 (Fix ④)
            by_strategy = self.classify_losses_by_strategy(audits)
            if by_strategy:
                for sig_type, cat_hits in by_strategy.items():
                    sig_adjs = self.compute_adjustments(cat_hits, existing_params, signal_type=sig_type)
                    loss_adjs.extend(sig_adjs)
                    if sig_adjs:
                        logger.info("[Feedback] [%s] 조정 %d개", sig_type, len(sig_adjs))
            else:
                # 전략 구분 없이 공통 조정 (fallback)
                loss_adjs = self.compute_adjustments(category_hits_all, existing_params)

        elif profitable:
            logger.info("[Feedback] 수익 당일(%.0f원) — 손실 원인 조정 스킵", total_realized)
        else:
            logger.info("[Feedback] 분석 최소 건수 미달 (%d/%d)", len(filled), MIN_TRADES_FOR_ANALYSIS)

        # ── Step 2-B: 수익 연속 N일 → 파라미터 완화 (Fix ①) ─────────────────
        relax_adjs: List[ParamAdjustment] = []
        profit_streak = self._count_profitable_streak(history)
        if profitable:
            profit_streak += 1   # 오늘 포함 카운트

        if profit_streak >= RELAX_CONSECUTIVE_DAYS:
            relax_adjs = self.compute_relaxation_adjustments(existing_params, history)
            if relax_adjs:
                logger.info(
                    "[Feedback] 연속 수익 %d일 — 파라미터 완화 %d개 제안",
                    profit_streak, len(relax_adjs),
                )

        # ── Step 3: 합산 + safety guards ─────────────────────────────────────
        raw_adjs = time_adjs + loss_adjs + relax_adjs
        approved, skipped = self.apply_safety_guards(raw_adjs, existing_params, history)

        # next_lock 이 계산됐으면 params 에 반영
        if next_lock > 0:
            existing_params["daily_profit_lock_won"] = float(next_lock)
            logger.info(
                "[Feedback] daily_profit_lock_won → %d원 (5일 평균 peak %.0f × %.0f%%)",
                next_lock,
                sum(h["peak_pnl"] for h in self._load_peak_history()[-PEAK_HISTORY_DAYS:]) /
                max(1, len(self._load_peak_history()[-PEAK_HISTORY_DAYS:])),
                PROFIT_LOCK_RATIO * 100,
            )

        # 스킵된 항목 중 연속일 부족인 것만 pending history 기록
        pending = [
            adj for adj in raw_adjs
            if adj not in approved
            and not any(adj.param in s and "바운드" in s    for s in skipped)
            and not any(adj.param in s and "변화량" in s    for s in skipped)
        ]

        # ── Step 4: 수익/손실 당일 마커 기록 (연속 카운트용 — Fix ①) ─────────
        # "_profit_day_" 가상 파라미터로 수익 여부를 history 에 남김
        profit_marker = [{
            "date":      target_date.isoformat(),
            "param":     "_profit_day_",
            "old_val":   0.0,
            "new_val":   1.0 if profitable else 0.0,
            "reason":    f"당일 손익 {total_realized:+.0f}원",
            "category":  "MARKER",
            "applied":   False,
            "profitable": profitable,
        }]

        self.write_adaptive_params(approved, existing_params, history, target_date, pending)
        # 마커는 pending과 별도로 history에 append (write 후 재로드해 추가)
        self._append_markers(profit_marker, target_date)

        logger.info(
            "[Feedback] 완료 — 손익 %.0f원 | %d건 | 슬롯조정 %d개 | 손실조정 %d개"
            " | 완화조정 %d개 | 승인 %d개 | 스킵 %d개 | 연속수익 %d일",
            total_realized, len(filled), len(time_adjs), len(loss_adjs),
            len(relax_adjs), len(approved), len(skipped), profit_streak,
        )

        return FeedbackResult(
            date=target_date,
            total_realized=total_realized,
            total_trades=len(filled),
            profitable=profitable,
            category_hits=category_counts,
            adjustments=approved,
            skipped_reasons=skipped,
            applied=bool(approved),
            slot_stats=slot_stats,
            peak_pnl=peak_pnl,
            next_profit_lock=next_lock,
        )

    def _append_markers(self, markers: List[Dict], target_date: date) -> None:
        """profit_day 마커를 adaptive_params.json history 에 추가."""
        try:
            existing_params, history = self.load_adaptive_params()
            # 같은 날 마커가 이미 있으면 교체
            history = [
                h for h in history
                if not (h.get("param") == "_profit_day_"
                        and h.get("date") == target_date.isoformat())
            ]
            history.extend(markers)
            payload = {
                "last_updated": target_date.isoformat(),
                "params":       existing_params,
                "history":      history,
            }
            tmp = self.adaptive_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.adaptive_path)
        except Exception as e:
            logger.warning("[Feedback] 마커 저장 실패: %s", e)

    def _log_health_summary(self) -> None:
        """
        analysis/health_monitor.py 가 기록한 당일 health_events.jsonl 을 읽어
        Feedback 로그에 요약을 남긴다.  파일이 없으면 무시.
        """
        from pathlib import Path as _Path
        health_path = _Path("logs/health_events.jsonl")
        if not health_path.exists():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        events: list = []
        try:
            with health_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        if rec.get("ts", "").startswith(today):
                            events.append(rec)
                    except json.JSONDecodeError:
                        pass
        except OSError as exc:
            logger.debug("[Feedback] health_events 읽기 실패: %s", exc)
            return

        if not events:
            return

        freeze_cnt   = sum(1 for e in events if e.get("type") == "FREEZE")
        tr_fail_cnt  = sum(1 for e in events if e.get("type") == "TR_FAIL")
        relax_cnt    = sum(1 for e in events if e.get("type") == "DROUGHT_RELAX")
        consec_cnt   = sum(1 for e in events if e.get("type") == "CONSECUTIVE_LOSS")
        signal_cnt   = sum(1 for e in events if e.get("type") == "SIGNAL")

        logger.info(
            "[Feedback][HealthSummary] 신호=%d  TR실패=%d  프리징=%d  "
            "가뭄완화=%d  연속손절=%d",
            signal_cnt, tr_fail_cnt, freeze_cnt, relax_cnt, consec_cnt,
        )
        if freeze_cnt:
            logger.warning("[Feedback] 오늘 UI 프리징 %d회 감지됨", freeze_cnt)
        if tr_fail_cnt >= TR_FAIL_THRESHOLD_WARN:
            logger.warning("[Feedback] TR 실패 %d회 — 네트워크 불안정 의심", tr_fail_cnt)
