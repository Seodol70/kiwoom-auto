"""
SmartScanner — 영웅문 조건검색 없이 파이썬이 직접 전 종목을 감시한다.

개선 포인트
  ① 메모리 최적화  : SnapshotStore — pandas DataFrame 을 1차 캐시로 사용.
                      API 재호출 없이 메모리 내 연산으로 신호를 판단한다.
  ② 구조화 로그    : ScannerLogger — scanner.log 에 선정/탈락 이유를 기록한다.
  ③ 터미널 뷰      : ScannerDisplay — rich 라이브러리로 VS Code 터미널에
                      실시간 감시 테이블과 신호 알림을 출력한다.

3단계 핵심 로직
  [1단계] Pre-Filter  (09:00 1회)
    GetCodeListByMarket → opt10030 → 거래대금 상위 200위 → SnapshotStore 적재
  [2단계] Real-time Scan  (1초 주기)
    PriorityWatchQueue(SetRealReg) → SnapshotStore 갱신 → 신호 판단
  [3단계] Final Signal
    ScanSignal → on_signal 콜백 → 주문 모듈
"""

from __future__ import annotations

import csv
import heapq
import json
import logging
import logging.handlers
import os
import threading
import time
from collections import deque as _Deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, ClassVar, Optional

import pandas as pd

from scanner.universe import _is_ordinary_stock
from PyQt5.QtCore import QTimer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# 로거 설정
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)     # 일반 로거 (콘솔)

class _WinSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    Windows 호환 RotatingFileHandler.

    표준 RotatingFileHandler는 파일 회전 시 os.rename을 사용하는데,
    Windows에서는 다른 프로세스(log_monitor, VS Code 등)가 scanner.log를
    열고 있으면 PermissionError(WinError 32)가 발생한다.

    이 핸들러는 rename 대신 shutil.copy2 + truncate 방식으로 회전해
    파일이 읽기 모드로 열려 있는 상태에서도 안전하게 동작한다.
    """

    def doRollover(self) -> None:
        import shutil

        # 현재 스트림 닫기
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]

        # 백업 파일 순환: .N → .N+1 (backupCount-1 → backupCount, …, 1 → 2)
        # 백업→백업 이동은 해당 파일을 아무도 열지 않으므로 rename OK
        for i in range(self.backupCount - 1, 0, -1):
            sfn = self.rotation_filename(f"{self.baseFilename}.{i}")
            dfn = self.rotation_filename(f"{self.baseFilename}.{i + 1}")
            if os.path.exists(sfn):
                if os.path.exists(dfn):
                    os.remove(dfn)
                os.rename(sfn, dfn)

        # 현재 로그 → .1 : rename 대신 copy + truncate
        dfn = self.rotation_filename(f"{self.baseFilename}.1")
        if os.path.exists(dfn):
            os.remove(dfn)
        if os.path.exists(self.baseFilename):
            shutil.copy2(self.baseFilename, dfn)          # 복사
            with open(self.baseFilename, "w",             # 원본 비우기
                      encoding=self.encoding or "utf-8"):
                pass

        if not self.delay:
            self.stream = self._open()


def _build_scan_logger(log_dir: str = "logs") -> logging.Logger:
    """scanner.log 전용 로거를 생성한다."""
    os.makedirs(log_dir, exist_ok=True)
    scan_log = logging.getLogger("scanner.audit")
    scan_log.setLevel(logging.DEBUG)
    scan_log.propagate = False   # 루트 로거로 전파 금지

    handler = _WinSafeRotatingFileHandler(
        filename=os.path.join(log_dir, "scanner.log"),
        maxBytes=20 * 1024 * 1024,   # 20 MB
        backupCount=10,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s\t%(levelname)s\t%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    scan_log.addHandler(handler)
    return scan_log

scan_log: logging.Logger = _build_scan_logger()


# ---------------------------------------------------------------------------
# 거래대금 표기 (원 → 조·억 한글, 진단용 증가율)
# ---------------------------------------------------------------------------

_JO_WON = 1_000_000_000_000
_EOK_WON = 100_000_000


def format_trade_amount_korean(amount_won: int) -> str:
    """
    거래대금(원)을 읽기 편한 한글 형식으로 표기.

    예시:
    - 487,000,000,000원 → "4,870억" (조 미포함) 또는 "0.487조"
    - 1,234,000,000,000원 → "1.2조 340억" (조 포함 시 소수점)
    - 12,340,000원 → "1,234만원"
    """
    try:
        n = int(amount_won)
    except (TypeError, ValueError):
        return "0원"
    if n <= 0:
        return "0원"

    jo = n // _JO_WON  # 1조 = 1,000,000,000,000
    rem = n % _JO_WON
    eok_int = rem // _EOK_WON  # 1억 = 100,000,000

    parts: list[str] = []

    # 조 단위 표기 (1조 이상)
    if jo > 0:
        if eok_int > 0:
            # 조와 억을 함께 표시 (예: "1.2조 340억")
            jo_decimal = jo + eok_int / 1_0000  # 1조 + n억을 소수점으로
            parts.append(f"{jo_decimal:.1f}조")
        else:
            # 억이 없으면 조만 (예: "1조")
            parts.append(f"{jo}조")
    elif eok_int > 0:
        # 1조 미만이면 억으로 표시 (예: "1,234억")
        parts.append(f"{eok_int:,}억")
    else:
        # 1억 미만이면 만원, 원으로 표시
        man = n // 10_000
        if man > 0:
            return f"{man:,}만원"
        return f"{n:,}원"

    return " ".join(parts)


def format_trade_amount_growth(current: int, baseline: Optional[int]) -> str:
    """거래대금 증가율(%) — baseline 이 없거나 0이면 '—'."""
    if baseline is None or baseline <= 0:
        return "증가율(9시대비) —"
    pct = (current - baseline) / baseline * 100.0
    return (
        f"증가율(9시대비) {pct:+.1f}% "
        f"(기준 {format_trade_amount_korean(baseline)})"
    )


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass
class SmartScannerConfig:
    # opt10030 최초 수집 목표(연속조회로 최대 ~2회 TR). 이후 ETF·우선주 제거 → watch_pool_max 로 캡.
    collect_raw_top_n:    int   = 400         # 2026-04-20: 200→400 (hybrid 스코어 pool 확장)
    watch_pool_max:       int   = 110         # 필터 후 거래대금 상위 유지 (100~120 권장 중앙값)
    pre_filter_top_n:     int   = 400         # 하위 호환: collect_raw_top_n 과 동일 사용 권장
    universe_trade_amt_weight: float = 0.4   # 거래대금 순위 가중치 (hybrid universe score)
    universe_vol_ratio_weight: float = 0.4   # 전일 대비 거래량 비율 가중치
    universe_chg_pct_weight:   float = 0.2   # 등락률 가중치
    pre_filter_time:      dtime = dtime(9, 0, 0)
    realtime_sub_max:     int   = 110         # SetRealReg 감시 상한( watch_pool_max 와 맞춤)
    scan_interval:        float = 60.0  # 2026-04-23: 10→60초 (메인 스레드 블로킹 해소, opt10030 TR 시간 여유)
    tr_delay:             float = 0.25        # TRRequestQueue 최소 간격
    breakout_ratio:       float = 0.015       # 2026-04-15: 3%→2%→1.5% (하루 3% 오르기 어려움, 진입 기회 확대)
    breakout_volume_mult: float = 0.8         # 2026-04-15: 1.0 → 0.8 (체결강도/추세/RSI 필터 강화로 거래량 중요도 낮아짐)
    breakout_confirm_minutes:    float = 2.0  # 2026-04-08: 3분 → 2분 (빠른 종목 타이밍 확보)
    # 추세 강도별 관찰 시간 단축 — yosep trend_level 기준 (2026-04-15)
    # trend_level=3(Strong): 즉시 진입 (다음 틱에 gate 확인 후 신호)
    # trend_level=2(Medium): 절반 관찰 (기본 2분의 50%)
    # trend_level=0~1: 기본 breakout_confirm_minutes 유지
    breakout_confirm_minutes_trend3: float = 0.0   # Strong 추세 — 관찰 시간 0 (즉시)
    breakout_confirm_minutes_trend2: float = 1.0   # Medium 추세 — 1분 관찰
    breakout_cancel_drawdown_pct: float = -0.8  # 2026-04-08: -0.5% → -0.8% (완화된 ratio 노이즈 흡수)
    breakout_pullback_from_high_pct: float = 2.5  # 당일 고점 대비 N% 이상 하락 중이면 BREAKOUT 차단 (완화: 1.5→2.5)
    breakout_min_rising_bars: int = 1         # 최근 N개 1분봉이 연속 상승이어야 BREAKOUT 통과 (완화: 2→1)
    jdm_ma_short:         int   = 7          # 최적화됨: 5→7
    jdm_ma_long:          int   = 15         # 최적화됨: 20→15
    jdm_rsi_low:          float = 35.0       # 레거시(다른 로직 참고용). 진입은 jdm_rsi_entry_min 사용
    jdm_rsi_high:         float = 70.0       # RSI 상한(과열 차단) — 과매수 구간 진입 금지
    jdm_rsi_entry_min:    float = 52.0       # JDM 진입 RSI 하한 — 2026-04-13: 60→52 (상승 시작점 타점)
    jdm_min_ma_spread_abs: int = 30          # [deprecated] MA 이격(원) — 레거시 호환성 유지
    jdm_ma_spread_pct:    float = 0.15       # MA 이격 비율(%) 하한 — 골든크로스 직후 확인 (기존 0.3에서 완화)
    jdm_ma_spread_max_pct: float = 3.5      # MA 이격 비율(%) 상한 — 2026-04-13: 2.5→3.5 (골든크로스 직후 허용 범위 확장)
    jdm_take_profit_pct:  float = 3.0        # 익절 목표 (최적화됨: 4.0%→3.0%)
    jdm_stop_loss_pct:    float = -1.2       # 손절 기준 — config RISK.stop_loss_pct 와 동기화 (2026-04-07)
    # [NEW] 2026-04-03 수급 절대치 필터 — 금액 무관, 순위 100위 내
    min_trade_amount:     int = 0                # 최소 거래대금 (원) — 금액 제한 철폐 (유니버스에서 차단)
    min_daily_rank:       int = 100          # 거래대금 상위 몇 위 이내 (기존 150/50에서 100으로 통일)
    markets:              tuple = ("0", "10")
    screen_realtime:      str   = "9200"
    display_top_n:        int   = 50    # 스캐너 UI 감시 테이블·Worker 상위 표시
    # [진단] 로그 거래대금 상위 샘플 — 매수 후보 전체와 무관(후보는 watch_pool_max·display_top_n 참고)
    diagnostic_sample_n:  int   = 5
    log_dir:              str   = "logs"
    # 등락률이 이 값 **이상**이면 감시·신호·매수 대상에서 제외 (config RISK.max_change_pct 와 동기화)
    max_change_pct:       float = 20.0               # 2026-04-03 상향: 15% → 20% (대장주 포함시키기)
    # ScannerWorker: 동일 종목 재 emit 최소 간격(초). 에지 트리거와 병행 (config RISK.signal_cooldown_sec)
    signal_cooldown_sec:  float = 45.0
    # [NEW] 4중 필터 — JDM 신호 품질 강화
    entry_start_time:     dtime = dtime(8, 0, 0)    # 진입 허용 시작 (08:00 — 시간외 포함 조기 시작)
    entry_end_time:       dtime = dtime(14, 30, 0)  # 진입 허용 종료 (확대: 오후 거래 허용)
    # [08:00 조기 시작] 시간 경계
    pre_market_end:       dtime = dtime(9, 0, 0)   # PRE/OPENING 경계 (시간외 종료)
    # PRE_SURGE 파라미터 (08:00~09:00 시간외 단일가)
    pre_surge_chg_min:    float = 2.0    # PRE 최소 등락률 (%) — 시간외에서 이 이상 오른 종목
    pre_surge_chg_max:    float = 20.0   # PRE 최대 등락률 (%) — 상한
    pre_surge_chejan_min: float = 110.0  # PRE 체결강도 하한 (%)
    pre_surge_chejan_max: float = 700.0  # PRE 체결강도 상한 — 극단 급등(고점) 차단 (900%+ 는 이미 과열)
    pre_surge_rsi_max:    float = 88.0   # PRE RSI 상한 — 과매수 진입 차단 (RSI=100 손실 사례 방지)
    breakout_chejan_max:         float = 800.0  # BREAKOUT 체결강도 상한 — OPENING/기본 슬롯 (2026-04-15 분석)
    breakout_chejan_max_morning: float = 950.0  # MORNING 슬롯 상한 완화 — 갭업 후 과열 정상화 허용 (삼성SDI 패턴)
    jdm_chejan_max:         float = 700.0   # JDM_ENTRY 체결강도 상한 (MORNING 이후) — 극과열 고점 차단
    jdm_chejan_max_opening: float = 1200.0  # OPENING 슬롯 전용 상한 — 장 초반 단일가 직후 900%+ 왜곡 허용
    breakout_rsi_max:     float = 80.0   # BREAKOUT RSI 상한 — 과매수 진입 차단 (2026-04-15 분석)
    # OPENING_SURGE 파라미터 (09:00~09:16 정규장 초반, 캔들 부족 구간)
    opening_surge_chg_min:    float = 1.0    # OPENING 최소 등락률 (%)
    opening_surge_chejan_min: float = 120.0  # OPENING 체결강도 하한 (%)
    opening_surge_vol_mult:   float = 1.2    # OPENING 거래량 배수 (직전 평균 대비)
    # ── Phase 1 모닝 스캘핑 파라미터 (09:00~09:30 진입, 10:30 강제청산) ──────
    phase1_min_candles:       int   = 3      # 진입 전 최소 1분봉 수 (≈09:03 이후)
    phase1_chejan_min:        float = 120.0  # 체결강도 하한 — PRE_SURGE 흐름 지속 확인
    phase1_chejan_max:        float = 700.0  # 체결강도 상한 — 극과열 고점 차단
    phase1_open_rise_max:     float = 8.0   # 시가 대비 상승 상한 (%) — 이미 너무 오른 경우 차단
    phase1_change_pct_max:    float = 15.0  # 전일 대비 등락률 상한 (%) — 급등 과열 차단
    phase1_max_positions:     int   = 3      # Phase 1 최대 동시 보유 포지션 수
    phase1_trail_drop_pct:    float = 1.0   # 10:30 이후 트레일 중 고점 대비 하락 시 청산 (%)
    phase2_entry_start_hour:  int   = 10    # Phase 2(메인전략) 진입 시작 시각 (시)
    phase2_entry_start_min:   int   = 0     # Phase 2(메인전략) 진입 시작 시각 (분)
    entry_open_surge_max_opening: float = 7.0  # OPENING 전용 시가 대비 상승 상한 (기존 3.5% 완화)
    min_chejan_strength:  float = 120.0             # 체결강도 하한 (%) — 2026-04-03 강화: 110→120% (매수세 우위 확실)
    volume_surge_mult:    float = 1.5               # 분봉 거래량 배수 (직전 5분 평균 대비)
    max_disparity_pct:    float = 5.0               # MA20 이격도 상한 (%)
    # [NEW] OR 전략 + 공격형 필터
    prev_close_min_ratio: float = 0.98              # 조건A: V자반등 최소 비율 (시가 대비 -2% 이내)
    entry_open_surge_max: float = 10.0             # 2026-04-16: 4.0→10.0% (오후 active 종목 JDM_SURGE 전차단 — 완화)
    vi_approach_chg_pct:  float = 7.0               # 조건B: VI 직전 등락률 기준 (%)
    volume_1min_surge_mult: float = 1.2             # 최근 1분 거래량 급증 배수 (직전 10분 평균 대비) — 기존 1.5→1.2 완화
    volume_surge_lookback: int = 10                 # 직전 N분 평균 계산 구간
    # [NEW] 스코어링 로직 보너스 기준
    scoring_vol_surge_bonus: float = 2.0            # 이 배수 이상 폭발 시 혜택
    scoring_rank_bonus: int = 10                    # 상위 N위 이내 대장주 시 혜택
    ma_alignment_time:    dtime = dtime(9, 30, 0)  # 이 시각 이후엔 MA 정배열 확인
    # [NEW] 일봉 정배열 + 피봇 R2 설정 (2026-04-03)
    pivot_r2_enabled:     bool = True               # 피봇 R2 돌파 조건 활성화
    daily_alignment_enabled: bool = True            # 일봉 정배열 조건 활성화 (5MA>10MA>20MA)
    daily_ma20_filter_enabled: bool = True          # 일봉 20MA 가격 필터 (현재가 ≥ 20MA 강제)
    daily_ma60_filter_enabled: bool = True          # 일봉 60MA 가격 필터 (현재가 ≥ 60MA — 중기 하락 추세 차단)
    daily_ma20_slope_enabled:  bool = True          # 일봉 20MA 우상향 필터 (추세추종형 — 3일 기울기 양수)
    daily_near_high_threshold_pct: float = 3.0      # 신고가 근처 판정 (25일 최고가 대비 %)
    daily_near_high_tp_pct: float = 5.0             # 신고가 근처 종목 익절 목표 (기본보다 높게)
    daily_candle_refresh_min: int = 15              # 일봉 데이터 갱신 주기(분) — 장 중 거의 불변, 15분으로 완화
    # [NEW] 지수 등락률 기반 진입 차단 (2026-04-07)
    # index_block_pct: 코스피/코스닥 중 하나라도 이 값 이하면 신규 진입 신호 차단
    #   (market_crash_pct -2.0%보다 여유있는 1단계 차단 — config RISK.market_index_block_pct)
    index_block_pct:   float = -1.5   # 신규 진입 완전 차단 기준 (%)
    # [NEW] 공포 장세 구간 (-1% ~ -1.5%): 완전 차단이 아닌 체결강도 기준 상향 (2026-04-08)
    market_fear_pct:       float = -1.0    # 이 값 이하 하락 시 공포 장세로 판단
    market_fear_chejan:    float = 140.0   # 공포 장세 시 체결강도 하한 (기존 슬롯값 대신 적용)
    # 아래 두 값은 MainWindow._check_market_crash()가 주기적으로 갱신하는 상태값
    kospi_chg_pct:     float = 0.0    # 최신 코스피 등락률 (%) — 장 시작 전 0.0
    kosdaq_chg_pct:    float = 0.0    # 최신 코스닥 등락률 (%) — 장 시작 전 0.0
    # [NEW] JDM_ENTRY v2.3 — 슬리피지·EMA 이격도 과열 방지 (2026-04-08)
    slippage_block_pct:      float = 3.0   # 직전 1분봉 종가 대비 현재 1분봉 상승 차단 상한 (%)
    ema_disp_short:          int   = 10    # EMA 이격도 계산 단기 기간
    ema_disp_long:           int   = 20    # EMA 이격도 계산 장기 기간
    ema_disp_max_pct:        float = 4.5   # EMA10/EMA20 이격 상한 (%) — 2026-04-13: 3.0→4.5 (RSI 52 구간 EMA 이격 허용)
    price_ema_disp_max_pct:  float = 4.0   # 현재가/EMA10 이격 상한 (%) — 2026-04-13: 3.0→4.0 (단기 급등 기준 완화)
    # [NEW] 시간대별 매수 조건 차등화 (2026-04-08)
    # 구간 경계: OPENING(09:05~09:30) / MORNING(09:30~11:00) / MIDDAY(11:00~13:00) / AFTERNOON(13:00~14:30)
    # entry_start_time(09:05), ma_alignment_time(09:30), entry_end_time(14:30) 은 기존 파라미터 재활용
    slot_morning_end:   dtime = dtime(11, 0, 0)   # MORNING 종료 / MIDDAY 시작
    slot_midday_end:    dtime = dtime(13, 0, 0)   # MIDDAY 종료 / AFTERNOON 시작
    # 구간별 등락률 상한 (%) — ScannerWorker prefilter + 개별 루프에 동시 적용
    max_change_pct_opening:   float = 20.0   # 09:05~09:30 장초반
    max_change_pct_morning:   float = 15.0   # 09:30~11:00 핵심 오전
    max_change_pct_midday:    float = 12.0   # 11:00~13:00 점심
    max_change_pct_afternoon: float = 10.0   # 13:00~14:30 오후 — 2026-04-13: 8→10% (오후 종목 허용 범위 확장)
    # 구간별 체결강도 하한 (%)
    min_chejan_strength_opening:   float = 110.0
    min_chejan_strength_morning:   float = 120.0
    min_chejan_strength_midday:    float = 130.0
    min_chejan_strength_afternoon: float = 150.0  # 2026-04-14: 130→150% (오후 BREAKOUT 전패 — 강화)
    # 구간별 거래량 급증 배수 (직전 N분 평균 대비)
    volume_surge_mult_opening:   float = 1.2
    volume_surge_mult_morning:   float = 1.5
    volume_surge_mult_midday:    float = 1.2   # 2026-04-16: 2.0→1.2 (오후 거래량 자연 감소 반영)
    volume_surge_mult_afternoon: float = 1.2   # 2026-04-16: 2.0→1.2 (오후 거래량 자연 감소 반영)
    # 구간별 RSI 진입 하한
    jdm_rsi_entry_min_opening:   float = 45.0  # 장초반 빠른 포착 (기존 50->45 완화)
    jdm_rsi_entry_min_morning:   float = 47.0  # 핵심 오전 타점 (기존 52->47 완화)
    jdm_rsi_entry_min_midday:    float = 50.0  # 점심 기준 동기화 (기존 55->50 완화)
    jdm_rsi_entry_min_afternoon: float = 50.0  # 오후 완화 (기존 58->50 완화)
    # [P2] 구간별 익절 목표 (%) — (레거시, 트레일 스탑으로 대체)
    tp_pct_opening:   float = 2.0
    tp_pct_morning:   float = 2.5
    tp_pct_midday:    float = 3.0
    tp_pct_afternoon: float = 3.5
    # [Trail] 고점 추적 트레일링 스탑 파라미터
    trail_activation_pct: float = 1.5   # 트레일 시작 최소 이익(%) — 2026-04-20: 1.0→1.5 (추세주 조기 청산 방지)
    trail_pct_tier1:      float = 0.8   # 수익 < tier1_max 구간 트레일 폭 (%) — 2026-04-20: 1.5→0.8 (발동 후 좁은 폭으로 꽉 물기)
    trail_tier1_max:      float = 2.0   # tier1/tier2 경계 (%) — 2026-04-20: 1.5→2.0
    trail_pct_tier2:      float = 1.2   # 수익 tier1_max ~ tier2_max 구간 — 2026-04-20: 1.5→1.2
    trail_tier2_max:      float = 4.0   # tier2/tier3 경계 (%) — 2026-04-20: 3.0→4.0
    trail_pct_tier3:      float = 2.0   # 수익 tier2_max 이상 구간 (크게 올랐을 때 여유)
    # [NEW] 체결 가속도(Execution Velocity) 필터 — 10초 체결량 급증 확인
    exec_velocity_enabled: bool  = True    # 체결 가속도 필터 활성화 (False면 전 종목 통과)
    exec_velocity_mult:    float = 1.8     # 10초 체결량 ≥ 직전 1분 평균 10초량의 N배 요구 (2026-04-22: 2.5→1.8, 장초반 체결 안정성)
    exec_velocity_disabled_opening: bool = False  # OPENING 슬롯(09:05~09:30)에서 체결 속도 필터 비활성화
    # [NEW] ATR 기반 트레일링 스탑
    atr_trail_enabled:        bool  = True   # ATR 트레일 스탑 활성화
    atr_trail_activation_pct: float = 1.5   # ATR 트레일 발동 최소 이익 (%, 기존 trail_activation_pct와 동일)
    atr_trail_multiplier:     float = 1.5   # trail_line = peak_price - multiplier × ATR14
    # [NEW] 섹터 쏠림 방지
    sector_max_positions:     int   = 2     # 동일 업종명 최대 동시 보유 종목 수
    # [추세추종] JDM 진입 조건 추세 레벨 오버라이드 파라미터 (2026-04-20)
    jdm_candle_skip_trend_level:   int   = 2     # 이 이상 trend_level이면 캔들 반전패턴 스킵 (추세 계속 진행 중)
    jdm_rsi_high_trend:            float = 80.0  # trend_level≥2 시 RSI 상한 완화 (기본 70 → 80)
    jdm_rsi_high_breakout:         float = 82.0  # ATR1.5 돌파 확인 시 RSI 상한 추가 완화 (82)
    jdm_rsi_high_opening_trend3:   float = 83.0  # OPENING + 추세Lv3 RSI 상한 (2026-04-23: 95→83, RSI=92 고점 진입 차단)
    jdm_rsi_entry_min_trend:       float = 45.0  # trend_level≥2 시 RSI 하한 완화 (슬롯 기준값 → 45)
    ema_disp_max_pct_trend:        float = 7.0   # trend_level≥2 EMA10/EMA20 이격 상한 완화
    price_ema_disp_max_pct_trend:  float = 6.0   # trend_level≥2 현재가/EMA10 이격 상한 완화
    ema20_exit_enabled:            bool  = False  # 분봉 EMA20 이탈 시 추세 소멸 청산 (기본 OFF)
    # [NEW] 보유 시간 상한 (타임컷)
    time_cut_minutes:     int   = 25   # 2026-04-13: 40→25 (타임컷 단축 — 추세 꺾인 종목 조기 청산)
    # [NEW] 전략 실험 옵션
    # 활성 전략 목록: "BREAKOUT", "JDM_ENTRY" 중 선택
    enabled_strategies: tuple[str, ...] = ("BREAKOUT", "JDM_ENTRY")
    # 평가/우선순위 (앞선 전략이 먼저 emit되면 같은 분의 후속 전략 평가는 중단)
    strategy_order: tuple[str, ...] = ("BREAKOUT", "JDM_ENTRY")
    # 분당 최대 신호 발행 수 — 동시 다발 진입 방지 (1분에 최대 N종목)
    max_entries_per_minute: int = 1
    # ── 요셉 시그널 추세 필터 ────────────────────────────────────────────────
    yosep_trend_enabled: bool = True
    yosep_ema_period: int = 20
    yosep_atr_period: int = 14
    yosep_volume_lookback: int = 20
    yosep_min_trend_level: int = 0            # 2026-04-16: 1→0 (무추세 종목도 MORNING/MIDDAY 허용)
    yosep_min_trend_level_opening: int = 1   # OPENING(09:00~09:30) 전용 — balanced preset과 동일하게 level 1 유지 (데이터 부족 level0 혼입 방지)
    yosep_min_trend_level_afternoon: int = 1  # 2026-04-16: 3→1 (약추세 이상으로 완화, 하루 신호 0건 방지)
    yosep_downtrend_block_atr: float = 0.8    # EMA 아래 ATR*N 이상이면 하락 강세로 차단
    yosep_preset: str = "balanced"            # aggressive | balanced | conservative

    # ── JDM 골든크로스 추세 오버라이드 ────────────────────────────────────────
    # trend_level ≥ jdm_golden_cross_trend_override 이면 골든크로스 없이
    # MA 정배열(ma_short > ma_long)만으로 JDM 진입 허용.
    # 이미 상승 중인 종목(Lv2+)은 단기MA가 이미 장기MA 위라 골든크로스 미충족이 정상.
    jdm_golden_cross_trend_override: int = 2  # Medium 이상 추세 시 골든크로스 우회 (0=비활성)

    # ── 추세 확인 시 고점 진입 상한 완화 ─────────────────────────────────────
    # JDM_SURGE(시가 대비 상승) / JDM_CHGPCT(전일 대비 등락률) 두 필터에 동시 적용.
    # trend_level ≥ surge_trend_override_level 이면 해당 상한을 surge_trend_max_pct로 교체.
    surge_trend_override_level: int   = 2     # Medium 이상 추세 확인 시 완화 (0=비활성)
    surge_trend_max_pct:        float = 15.0  # 추세 확인 시 허용 상한 (%) — 기본 15%

    # ── 수급 필터 (외국인/기관 순매수, opt10059) ──────────────────────────────
    investor_filter_enabled: bool  = True   # 수급 필터 활성화 여부
    investor_refresh_min:    int   = 10     # opt10059 갱신 주기 (분)
    investor_top_n:          int   = 15     # 수급 조회 대상 상위 N종목 (30→15, TR 부하 절반)
    # score +1 종목: 쿨다운 유지 (우선 처리)
    # score -1 종목: 쿨다운 2배 적용 (우선순위 하향, 차단은 아님)

    # ── 분할 익절 ─────────────────────────────────────────────────────────────
    partial_profit_enabled: bool  = True    # 분할 익절 활성화 여부
    partial_profit_pct:     float = 2.0     # 1차 분할 익절 트리거 수익률 (%)
    partial_sell_ratio:     float = 0.30    # 1차 분할 매도 비율 (30%)

    # ── 종가매매(EOD) 모드 ────────────────────────────────────────────────────
    # overnight_mode_enabled: True 시 14:40~14:55 EOD 진입 신호 활성화,
    #   당일 15:19 강제청산에서 eod_trade 포지션 제외, 익일 09:00 갭 체크 후 관리.
    overnight_mode_enabled:      bool  = False          # 종가매매 모드 활성화
    eod_entry_start:             dtime = dtime(14, 40, 0)  # EOD 진입 시작 시각
    eod_entry_end:               dtime = dtime(14, 55, 0)  # EOD 진입 종료 시각
    eod_near_high_threshold_pct: float = 3.0            # 25일 신고가 근처 판정 (%)
    eod_change_pct_min:          float = 2.0            # 당일 등락률 최소 (%) — 강세 확인
    eod_change_pct_max:          float = 10.0           # 당일 등락률 최대 (%) — 과열 제외
    eod_strength_min:            float = 115.0          # 체결강도 하한 (%)
    eod_min_trend_level:         int   = 2             # 요셉 추세 최소 단계 (2=Medium 이상만 EOD 진입)
    eod_volume_ratio_min:        float = 1.5            # 전일 평균 대비 거래량 배수
    eod_gap_up_exit_pct:         float = 2.0            # 익일 갭 상승 즉시 익절 기준 (%)
    eod_gap_down_exit_pct:       float = -1.5           # 익일 갭 하락 즉시 손절 기준 (%)
    eod_timecut_minutes:         int   = 30             # 익일 09:00 이후 타임컷 (분)
    eod_timecut_min_pct:         float = 1.0            # 익일 타임컷 발동 전 최소 수익률 (%)

    # ── Strong Trend 홀딩 (trend_level=3 진입 포지션 청산 완화) ─────────────────
    # AFTERNOON Strong Trend(level=3) 진입 포지션은 추세가 꺾이지 않는 한 더 길게 홀딩.
    # - 타임컷 면제: 25분 강제청산 제외 (추세소멸/트레일에 위임)
    # - 트레일 tier1 스킵: 고점 대비 1.5% 폭 → tier2(2.5%) 폭으로 시작
    strong_trend_hold_level:  int  = 3     # 이 trend_level 이상이면 홀딩 모드 적용
    strong_trend_timecut_exempt: bool = True  # True → Strong Trend 포지션 타임컷 면제

    # ── 본절가 스탑 (Breakeven Stop) ──────────────────────────────────────────
    # 분할 익절 완료 후 주가가 평단가 이하로 내려오면 잔여 수량 전량 즉시 청산.
    # 효과: 30% 수익을 이미 확보했으므로 전체 트레이드가 절대 마이너스로 끝나지 않음.
    breakeven_stop_enabled: bool  = True    # 본절가 스탑 활성화 여부
    breakeven_stop_buffer_pct: float = 0.0  # 평단가 대비 허용 마진 (%) — 0.0 = 정확히 본전

    # ── 일일 손익 한도 ────────────────────────────────────────────────────────
    # daily_profit_lock_won: 당일 실현손익이 이 금액 이상이면 신규 매수 신호 차단.
    #   0 이면 비활성. 장 마감 후 피드백 엔진이 최근 5일 peak 평균 × 70% 로 자동 조정.
    daily_profit_lock_won:  int   = 50_000   # 기본 5만원 (FeedbackEngine이 다음날 자동 갱신)
    # daily_loss_cut_won: 당일 실현손익이 이 금액 이하이면 전 포지션 강제 청산 + 매수 차단.
    #   0 이면 비활성. 음수로 지정 (예: -100000 = -10만원 한도).
    daily_loss_cut_won:     int   = -100_000 # 기본 -10만원

    # ── Feedback Loop ─────────────────────────────────────────────────────────

    _YOSEP_PRESETS: ClassVar[dict[str, dict[str, float | int | bool]]] = {
        "aggressive": {
            "yosep_trend_enabled": True,
            "yosep_ema_period": 20,
            "yosep_atr_period": 14,
            "yosep_volume_lookback": 14,
            "yosep_min_trend_level": 0,
            "yosep_downtrend_block_atr": 1.2,
        },
        "balanced": {
            "yosep_trend_enabled": True,
            "yosep_ema_period": 20,
            "yosep_atr_period": 14,
            "yosep_volume_lookback": 20,
            "yosep_min_trend_level": 1,
            "yosep_downtrend_block_atr": 0.8,
        },
        "conservative": {
            "yosep_trend_enabled": True,
            "yosep_ema_period": 20,
            "yosep_atr_period": 14,
            "yosep_volume_lookback": 24,
            "yosep_min_trend_level": 2,
            "yosep_downtrend_block_atr": 0.6,
        },
    }

    def apply_yosep_preset(self, preset: str, protected_keys: Optional[set[str]] = None) -> None:
        """
        요셉 시그널 파라미터를 프리셋으로 일괄 적용한다.
        protected_keys에 포함된 키는 덮어쓰지 않는다.
        """
        key = str(preset or "").strip().lower()
        if not key:
            return
        conf = self._YOSEP_PRESETS.get(key)
        if conf is None:
            logger.warning("[YOSEP_PRESET] 알 수 없는 프리셋: %s (기본 balanced 유지)", preset)
            return
        for k, v in conf.items():
            if protected_keys and k in protected_keys:
                continue
            setattr(self, k, v)
        self.yosep_preset = key

    @classmethod
    def from_adaptive(
        cls,
        adaptive_path: str = "config/adaptive_params.json",
        **overrides,
    ) -> "SmartScannerConfig":
        """
        adaptive_params.json 을 읽어 기본값을 덮어쓴 인스턴스를 반환.
        파일이 없거나 파싱 실패 시 기본값 그대로 사용.
        overrides: 런타임 덮어쓰기 (UI SpinBox, config.py RISK 등).

        우선순위: 기본값 < adaptive_params.json < overrides
        """
        import json as _json
        from pathlib import Path as _Path

        instance = cls(**overrides) if overrides else cls()
        path = _Path(adaptive_path)

        if not path.exists():
            return instance

        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)

            from datetime import time as _dtime
            params  = data.get("params", {})
            applied = []
            touched_keys: set[str] = set()
            for key, val in params.items():
                if not hasattr(instance, key):
                    logger.warning("[AdaptiveParams] 알 수 없는 파라미터 무시: %s", key)
                    continue
                orig = getattr(instance, key)
                # tuple/list 필드는 건드리지 않음
                if isinstance(orig, (tuple, list)):
                    continue
                # datetime.time 필드: "HH:MM" 또는 "HH:MM:SS" 문자열 파싱
                if isinstance(orig, _dtime):
                    try:
                        parts = str(val).split(":")
                        h = int(parts[0])
                        m = int(parts[1]) if len(parts) > 1 else 0
                        s = int(parts[2]) if len(parts) > 2 else 0
                        setattr(instance, key, _dtime(h, m, s))
                        applied.append(f"{key}: {orig} → {val}")
                        touched_keys.add(key)
                    except Exception as e:
                        logger.warning("[AdaptiveParams] time 변환 실패 %s=%s: %s", key, val, e)
                    continue
                try:
                    setattr(instance, key, type(orig)(val))
                    applied.append(f"{key}: {orig} → {val}")
                    touched_keys.add(key)
                except (TypeError, ValueError) as e:
                    logger.warning("[AdaptiveParams] 타입 변환 실패 %s=%s: %s", key, val, e)

            # 요셉 프리셋 일괄 적용:
            # - yosep_preset만 지정하면 프리셋값들이 채워짐
            # - 개별 키를 adaptive에서 지정한 경우(protected)는 그대로 유지
            instance.apply_yosep_preset(
                getattr(instance, "yosep_preset", "balanced"),
                protected_keys=touched_keys,
            )

            if applied:
                logger.info(
                    "[AdaptiveParams] %d개 파라미터 로드 (last_updated=%s): %s",
                    len(applied), data.get("last_updated", "?"), ", ".join(applied),
                )
            else:
                logger.debug("[AdaptiveParams] 로드했으나 적용된 파라미터 없음")

        except Exception as e:
            logger.error("[AdaptiveParams] 로드 실패, 기본값 사용: %s", e)

        return instance


# ---------------------------------------------------------------------------
# TRRequestQueue — 키움 API 요청 간격 보장 (최대 4회/초)
# ---------------------------------------------------------------------------

def is_pure_equity_name(name: str) -> bool:
    """
    ETF·ETN·인버스·레버리지·스팩 및 국내 ETF 브랜드명이 들어가면 False.

    스캐너 감시/스냅샷 적재 시 순수 주식만 남기기 위해 사용한다.
    """
    if not name or not str(name).strip():
        return False
    n = str(name).strip()
    upper = n.upper()

    # 강화된 필터링 — ETF/ETN/파생상품 전부 제외
    exclude_kw = (
        # 기본
        "ETF", "ETN", "인버스", "레버리지", "곱버스", "역추적",
        "2X", "3X", "5X", "10X", "스팩", "SPAC", "헷지", "HEDGE",
        # 선물추적, 옵션, 수익증권
        "선물", "옵션", "수익증권", "구조", "파생",
        # ETF 브랜드
        "KODEX", "TIGER", "KBSTAR", "HANAR", "KOSEF", "ARIRANG",
        "TIMEFOLIO", "KINDEX", "ACE", "RISE", "SOL", "FOCUS",
    )
    for kw in exclude_kw:
        if kw in n or kw in upper:
            return False

    return True


def filter_equity_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """opt10030 등에서 받은 행 리스트에서 우선주·비주식(ETF 등)을 제거한다."""
    out: list[dict] = []
    dropped = 0
    for r in rows:
        code = str(r.get("code", "")).lstrip("A").strip()
        if not _is_ordinary_stock(code):
            dropped += 1
            logger.debug("[유니버스필터] 우선주 제외 — %s(%s)", r.get("name", ""), code)
            continue
        nm = r.get("name", "")
        if is_pure_equity_name(str(nm)):
            out.append(r)
        else:
            dropped += 1
            logger.debug(
                "[유니버스필터] 제외 — %s(%s)",
                nm, code,
            )
    if dropped:
        logger.info("[유니버스필터] 우선주·ETF·파생 등 제외 %d건 → 잔여 %d건", dropped, len(out))
    return out, dropped


def apply_watch_pool_cap(rows: list[dict], watch_pool_max: int) -> list[dict]:
    """거래대금 내림차순으로 상위 watch_pool_max 종목만 유지. (레거시 — apply_universe_score_cap 사용 권장)"""
    if not rows:
        return []
    rows = sorted(
        rows,
        key=lambda r: int(r.get("trade_amount", 0) or 0),
        reverse=True,
    )
    return rows[:watch_pool_max]


def _vol_pace_ratio(today_vol: int, prev_volume: int) -> float:
    """
    거래량 페이스 비율 (시간대 편향 보정).

    단순 today_vol/prev_volume 은 장 초반(09:00~09:30)에 항상 극소값이 되어
    vol_ratio 가중치가 무의미해지는 문제가 있다.

    이를 해결하기 위해 '경과시간 대비 기대 거래량'으로 정규화한다:
        pace_ratio = today_vol / (prev_volume × elapsed_ratio)

    여기서 elapsed_ratio = 장 시작 후 경과 분 / 390(총 거래 시간 분).
    최소 5분 보정으로 장 정확히 열리는 순간의 분모 0 방지.

    해석: pace_ratio = 1.0 → 전일과 동일한 속도 / 2.0 → 전일의 2배 속도
    이 값은 시간대에 무관하게 동일한 의미를 가진다.

    Returns
    -------
    pace_ratio : float  (0.0 이면 계산 불가 — 중립 처리)
    """
    if prev_volume <= 0 or today_vol <= 0:
        return 0.0

    _MARKET_OPEN_MIN   = 9 * 60   # 09:00 분 기준
    _TOTAL_TRADING_MIN = 390      # 09:00 ~ 15:30
    _MIN_ELAPSED_MIN   = 5        # 극초반 분모 0 방어

    now = datetime.now().time()   # 모듈 최상위 from datetime import datetime
    now_min = now.hour * 60 + now.minute
    elapsed = max(now_min - _MARKET_OPEN_MIN, _MIN_ELAPSED_MIN)

    # 장외 시간(사전/사후)은 전체 거래 시간 기준으로 클램프
    elapsed = min(elapsed, _TOTAL_TRADING_MIN)
    elapsed_ratio = elapsed / _TOTAL_TRADING_MIN

    return today_vol / (prev_volume * elapsed_ratio)


def apply_universe_score_cap(
    rows: list[dict],
    watch_pool_max: int,
    cfg: "SmartScannerConfig",
    prev_volumes: dict[str, int],
) -> list[dict]:
    """
    거래대금 순위 × 거래량 페이스 × 등락률을 복합 스코어링해 상위 watch_pool_max 종목 반환.

    Hybrid score = trade_amt_score×w1 + vol_pace_score×w2 + chg_pct_score×w3

    vol_pace_score 는 거래량 페이스 비율(_vol_pace_ratio) 기반:
      - pace_ratio  = today_vol / (prev_volume × elapsed_ratio)
      - 장 초반이어도 "같은 시간대 기준 전일 대비 몇 배 속도인지"로 비교
      - min(pace_ratio / 3.0, 1.0)  — 전일 대비 3배 속도=만점
      - 전일 데이터 없으면 0.5 (중립)
    """
    if not rows:
        return []

    n = len(rows)
    w_amt = getattr(cfg, "universe_trade_amt_weight", 0.4)
    w_vol = getattr(cfg, "universe_vol_ratio_weight", 0.4)
    w_chg = getattr(cfg, "universe_chg_pct_weight",   0.2)

    # 거래대금 내림차순 순위 → 정규화 점수
    sorted_by_amt = sorted(rows, key=lambda r: int(r.get("trade_amount", 0) or 0), reverse=True)
    amt_rank: dict[str, float] = {}
    for i, r in enumerate(sorted_by_amt):
        # i=0 (1위) → 1.0, i=n-1 (꼴찌) → 0.0
        amt_rank[r["code"]] = 1.0 - (i / max(n - 1, 1))

    scored: list[tuple[float, dict]] = []
    for r in rows:
        code = r["code"]

        # ① 거래대금 스코어
        s_amt = amt_rank.get(code, 0.5)

        # ② 거래량 페이스 스코어 (시간대 편향 보정)
        pv = prev_volumes.get(code, 0)
        today_vol = int(r.get("volume", 0) or 0)
        pace = _vol_pace_ratio(today_vol, pv)
        if pace > 0:
            r["vol_ratio"]   = round(pace, 4)  # SnapshotStore에 시간 보정 배율 저장
            r["prev_volume"] = pv
            s_vol = min(pace / 3.0, 1.0)       # pace 3배 = 만점
        else:
            r["vol_ratio"]   = 0.0
            r["prev_volume"] = pv
            s_vol = 0.5                         # 전일 데이터 없으면 중립

        # ③ 등락률 스코어
        chg = float(r.get("change_pct", 0) or 0)
        s_chg = min(max(chg / 10.0, 0.0), 1.0)

        score = s_amt * w_amt + s_vol * w_vol + s_chg * w_chg
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    result = [r for _, r in scored[:watch_pool_max]]
    logger.debug(
        "[유니버스스코어] pool %d→%d, top5: %s",
        n, len(result),
        [(r["code"], round(sc, 3)) for sc, r in scored[:5]],
    )
    return result


class TRRequestQueue:
    """
    키움 TR 호출 간격을 중앙에서 관리한다.

    키움 API 제한: 연속 TR 호출 간 최소 0.2초 권장.
    여기서 0.25초로 설정해 여유를 두고, 모든 TR 호출을
    call() 메서드를 통해 실행하면 자동으로 간격이 보장된다.

    기존 time.sleep(tr_delay) 분산 호출을 이 클래스로 대체한다.
    """
    _MIN_INTERVAL = 0.25  # 초

    def __init__(self) -> None:
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def call(self, fn: Callable, *args):
        """fn(*args)를 최소 간격 보장 후 실행하고 결과를 반환한다.

        ⚠️ 대기 구간에 processEvents()를 쓰면 다른 QTimer 콜백이 재귀 발화해
        cascading nested event loop 으로 16초+ 프리징이 발생한다.
        (실측: _load_candles_async → processEvents → scan timer → fetch_opt10030 4페이지
         → get_balance 6초 → get_holdings 2초 총 16초 블로킹)

        따라서 대기는 time.sleep(wait) 으로만 처리한다. 최대 대기는 0.25초이므로
        UI 체감 영향 없음. _comm_rq 내 TRRateLimiter.acquire() 가 별도로
        processEvents를 사용해 그 구간의 UI 응답성을 보장한다.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            wait = max(0.0, self._MIN_INTERVAL - elapsed)
            self._last_call = now + wait
        if wait > 0:
            time.sleep(wait)   # 최대 0.25초 — cascade 방지를 위해 processEvents 사용 금지
        return fn(*args)


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class StockSnapshot:
    code:          str
    name:          str
    current_price: int   = 0
    open_price:    int   = 0
    high_price:    int   = 0
    low_price:     int   = 0
    volume:        int   = 0
    trade_amount:  int   = 0
    prev_close:    int   = 0
    change_pct:    float = 0.0
    closes_1min:   list  = field(default_factory=list)
    opens_1min:    list  = field(default_factory=list)    # 1분봉 시가
    highs_1min:    list  = field(default_factory=list)    # 1분봉 고가
    lows_1min:     list  = field(default_factory=list)    # 1분봉 저가
    chejan_strength: float = 100.0          # [NEW] 체결강도 (FID 20)
    volumes_1min:   list  = field(default_factory=list)   # [NEW] 1분봉 거래량
    daily_closes:  list  = field(default_factory=list)   # [NEW] 일봉 종가 최신순 (최대 120개 — MA60 계산용)
    daily_high_prev: int = 0                # [NEW] 전일 고가 (피봇 R2용)
    daily_low_prev:  int = 0                # [NEW] 전일 저가 (피봇 R2용)
    updated_at:    datetime = field(default_factory=datetime.now)
    # [수급 필터] 외국인/기관 순매수 (10분 주기 opt10059 갱신)
    foreign_net_buy:     int             = 0     # 외국인 당일 순매수 수량 (양수=순매수)
    inst_net_buy:        int             = 0     # 기관 당일 순매수 수량
    investor_score:      int             = 0     # -1(둘다 매도) / 0(중립) / +1(둘다 매수)
    investor_updated_at: Optional[datetime] = None  # 마지막 수급 갱신 시각
    trend_level:         int             = 0     # 요셉 시그널 추세 단계(0~3)
    trend_prev_level:    int             = 0     # 직전 추세 단계(강세 소멸 감시용)
    is_daily_bull:       bool            = False # 일봉 정배열(5MA>10MA>20MA) 캐시 — prefilter 속도용
    rank:                int             = 0     # opt10030 거래대금 순위 (0=미할당)
    rsi:                 float           = 0.0   # RSI14 캐시 (closes_1min 기반, 0=미계산/데이터부족)
                                                 # check_breakout_gate · check_pre_surge 의 과매수 차단에 사용
                                                 # snap.rsi > 0 인 경우에만 필터 적용 (데이터 부족 시 스킵)
    prev_volume:         int             = 0     # 전일 총거래량 (hybrid universe score 계산용)
    vol_ratio:           float           = 0.0   # 유니버스 스코어 거래량 페이스 비율
                                                 # = today_vol / (prev_volume × elapsed_ratio)
                                                 # ※ trade_audit_logger의 vol_ratio(1분봉 급증 배수)와
                                                 #   개념이 다름 — 해당 파일은 snap.volumes_1min을 직접
                                                 #   읽어 로컬 변수로 계산하므로 이 필드를 읽지 않음
    # [NEW] 체결 가속도: 10초 체결량 / 직전 1분 평균 10초 체결량 (0=데이터 부족 → 필터 스킵)
    exec_velocity_ratio: float           = 0.0
    # [NEW] 업종명 (섹터 쏠림 방지용, SnapshotStore._sector_cache에서 제공)
    sector:              str             = ""


@dataclass
class ScanSignal:
    code:             str
    name:             str
    signal_type:      str        # "BREAKOUT" | "JDM_ENTRY"
    price:            int
    reason:           str
    entry_candle_low: int = 0    # 진입 캔들 저가 → 손절가 기준
    trend_level:      int = 0
    trend_prev_level: int = 0
    generated_at:     datetime = field(default_factory=datetime.now)
    # 일봉 맥락 — check_jdm_entry / _build_jdm_signal 에서 채워짐
    near_daily_high:  bool  = False   # True → 25일 신고가 근처 (매물대 없음) → TP 상향
    daily_ma20:       float = 0.0     # 일봉 20MA 값 (로그·감사용)
    # 종가매매(EOD) 플래그 — overnight_mode_enabled 시 14:40~14:55 발생 신호에 설정
    eod_trade:        bool  = False   # True → 당일 청산 제외, 익일 갭 체크 후 관리
    # 매매 단계 — 메인 스레드에서 설정 후 handle_signal 에 전달
    entry_phase:      int   = 0       # 0=미분류, 1=Phase1 모닝스캘핑, 2=Phase2 메인전략
    # 신호 생성 시점의 등락률 — opt10001 실패 시 handle_signal 폴백으로 사용
    change_pct:       float = 0.0


# ---------------------------------------------------------------------------
# ① SnapshotStore — pandas DataFrame 캐시
# ---------------------------------------------------------------------------

_DF_COLS = [
    "code", "name",
    "current_price", "open_price", "high_price", "low_price",
    "volume", "trade_amount", "prev_close", "change_pct",
    "rank", "updated_at",
]


def _df_cell_scalar(val, default=None):
    """
    DataFrame.loc[code] 행에서 컬럼 값이 스칼라가 아니라 Series인 경우(중복 컬럼명 등) 대비.
    truthiness 검사로 Series를 건드리지 않도록 첫 스칼라만 꺼낸다.
    """
    if val is None:
        return default
    if isinstance(val, pd.Series):
        if val.empty:
            return default
        val = val.iloc[0]
    try:
        if pd.isna(val):
            return default
    except TypeError:
        pass
    return val


class SnapshotStore:
    """
    전 종목 스냅샷을 pandas DataFrame 에 보관한다.

    ┌──────────────────────────────────────────────────┐
    │ 왜 DataFrame 인가?                               │
    │  · bulk_update() 1회 호출로 200종목 일괄 적재   │
    │  · df.nlargest() 로 API 재호출 없이 순위 산출   │
    │  · 컬럼 연산 (vectorized) 으로 신호 판단 가능   │
    │  · 백테스트 CSV 로 바로 export 가능             │
    └──────────────────────────────────────────────────┘

    실시간 틱은 update_price() 로 행 단위 갱신한다.
    closes_1min 은 DataFrame 외부에서 dict 로 별도 관리
    (리스트 컬럼은 벡터 연산 불가 → 분리가 더 효율적).
    """

    def __init__(self) -> None:
        self._df   = pd.DataFrame(columns=_DF_COLS).set_index("code")
        self._mins: dict[str, list[float]] = {}   # code → 1분봉 종가
        self._last_min: dict[str, int] = {}        # code → 마지막 기록된 분(minute)
        # [NEW] 분봉 거래량 추적
        self._min_vols:  dict[str, list[int]]   = {}   # code → 1분봉 별 거래량 델타
        self._last_vol:  dict[str, int]         = {}   # code → 직전 분 경계 누적거래량
        # [NEW] 1분봉 OHLC 추적 (캔들 패턴 판단용)
        self._min_opens: dict[str, list[float]] = {}   # code → 1분봉 시가
        self._min_highs: dict[str, list[float]] = {}   # code → 1분봉 고가
        self._min_lows:  dict[str, list[float]] = {}   # code → 1분봉 저가
        self._cur_open:  dict[str, float]       = {}   # code → 현재 분 시가 (첫 틱)
        self._cur_high:  dict[str, float]       = {}   # code → 현재 분 고가 (진행중)
        self._cur_low:   dict[str, float]       = {}   # code → 현재 분 저가 (진행중)
        # [NEW] 체결강도 추적
        self._chejan_str: dict[str, float]      = {}   # code → 체결강도 (FID 20)
        # [NEW] 일봉 캐시 추적 (2026-04-03)
        self._daily_data: dict[str, list[dict]] = {}   # code → 일봉 OHLCV 리스트 (최신순)
        self._daily_updated_at: dict[str, datetime] = {}  # code → 마지막 갱신 시각
        # [수급 필터] opt10059 결과 캐시
        self._inv_foreign: dict[str, int] = {}
        self._inv_inst: dict[str, int] = {}
        self._inv_score: dict[str, int] = {}
        self._inv_updated_at: dict[str, datetime] = {}
        self._trend_level: dict[str, int] = {}         # code → 현재 추세 단계(0~3)
        self._trend_prev_level: dict[str, int] = {}    # code → 직전 추세 단계
        # [NEW] 체결 가속도 — 10초 슬라이딩 윈도우 (ts, cumvol) 튜플 저장
        self._tick_ts_vol: dict[str, _Deque] = {}      # code → deque[(monotonic_ts, cumvol)]
        # [NEW] 업종명 캐시 — opt10001 체결 시 update_sector()로 갱신
        self._sector_cache: dict[str, str] = {}        # code → 업종명
        # _lock은 캐시 로드보다 먼저 초기화해야 함 (_load_1min_cache에서 self._lock 사용)
        self._lock = threading.Lock()
        # 일봉 디스크 캐시 — 재시작해도 당일 데이터 즉시 복구
        self._daily_cache_path: Path = self._get_daily_cache_path()
        self._load_daily_cache()
        # 1분봉 디스크 캐시 — 재시작/신규 편입 종목 즉시 복구
        self._1min_cache_path: Path = self._get_1min_cache_path()
        self._load_1min_cache()

    # ── 일괄 적재 ─────────────────────────────────────────────────────────

    # 숫자형으로 강제 변환할 컬럼
    _NUM_COLS = [
        "current_price", "open_price", "high_price", "low_price",
        "volume", "trade_amount", "prev_close", "change_pct", "rank",
        "prev_volume", "vol_ratio",
    ]

    def bulk_update(self, rows: list[dict]) -> None:
        """
        Pre-Filter 결과(list[dict])를 DataFrame 에 일괄 적재한다.
        기존 행이 있으면 갱신, 없으면 추가한다.
        """
        if not rows:
            logger.warning("[SnapshotStore.bulk_update] rows 빈 리스트 — 적재 스킵")
            return

        rows, _dropped = filter_equity_rows(rows)
        if not rows:
            logger.warning("[SnapshotStore.bulk_update] ETF·파생 제외 후 빈 리스트 — 적재 스킵")
            return

        # [FIX] prev_close=0 복구 — opt10030이 전일종가를 안 보낼 때 change_pct로 역산
        # 수식: prev_close = current_price / (1 + change_pct/100)
        for row in rows:
            prev_close = row.get("prev_close")
            if (prev_close is None or prev_close == 0):
                cp = float(row.get("change_pct") or 0)
                curr = float(row.get("current_price") or 0)
                if curr > 0 and cp != 0:  # 둘 다 유효해야만 역산
                    row["prev_close"] = int(curr / (1.0 + cp / 100.0))
                    logger.debug("[복구] %s: change_pct=%.2f%% curr=%d → prev_close=%d (역산)",
                                 row.get("code"), cp, curr, row["prev_close"])
                elif curr > 0 and cp == 0:  # change_pct=0이면 prev_close=current_price
                    row["prev_close"] = curr
                    logger.debug("[복구] %s: change_pct=0%% → prev_close=%d (동일)",
                                 row.get("code"), curr)

        # 첫 행 진단 로그 (DEBUG로 내림 — 2026-04-03)
        first = rows[0]
        logger.debug("[⚠️ bulk_update] 첫 행 진단 — code=%s name=%s | price=%s open=%s high=%s low=%s | volume=%s trade_amt=%s prev_close=%s chg_pct=%s",
                     first.get("code"), first.get("name"),
                     first.get("current_price"), first.get("open_price"), first.get("high_price"), first.get("low_price"),
                     first.get("volume"), first.get("trade_amount"),
                     first.get("prev_close"), first.get("change_pct"))

        new_df = pd.DataFrame(rows).set_index("code")
        new_df["updated_at"] = datetime.now()
        # 숫자 컬럼 타입 보장
        for col in self._NUM_COLS:
            if col in new_df.columns:
                new_df[col] = pd.to_numeric(new_df[col], errors="coerce").fillna(0)
        with self._lock:
            self._df = new_df.combine_first(self._df)
            for col in self._NUM_COLS:
                if col in self._df.columns:
                    self._df[col] = pd.to_numeric(self._df[col], errors="coerce").fillna(0)
            # [2026-04-03] 중복 인덱스 제거 (같은 code가 여러 줄이면 최신 것만 유지)
            if not self._df.empty and self._df.index.duplicated().any():
                self._df = self._df[~self._df.index.duplicated(keep='last')]
            # 이전 세션에서 남은 ETF 행 제거 (combine_first 로 잔존 가능)
            if not self._df.empty and "name" in self._df.columns:
                keep = self._df["name"].astype(str).map(is_pure_equity_name)
                for c in self._df.index[~keep].tolist():
                    for d in (self._mins, self._min_opens, self._min_highs, self._min_lows,
                              self._min_vols, self._cur_open, self._cur_high, self._cur_low,
                              self._trend_level, self._trend_prev_level,
                              self._inv_foreign, self._inv_inst, self._inv_score, self._inv_updated_at,
                              self._tick_ts_vol, self._sector_cache):
                        d.pop(c, None)
                self._df = self._df[keep]
            for code in new_df.index:
                if code not in self._mins:
                    self._mins[code] = []

        logger.debug("[SnapshotStore.bulk_update] 적재 완료 — df 행수=%d", len(self._df))

    # ── 실시간 틱 갱신 ────────────────────────────────────────────────────

    # 틱 갱신 시 업데이트할 컬럼 — updated_at 제외 (핫패스 슬림화)
    _TICK_COLS = ["current_price", "high_price", "low_price",
                  "open_price", "volume", "trade_amount", "change_pct"]

    def update_price(
        self,
        code:         str,
        current_price: int,
        high_price:   int,
        low_price:    int,
        open_price:   int,
        volume:       int,
        trade_amount: int = None,  # ← None이면 건너뜀 (opt10030 누적값 보존)
        change_pct:   float = None,
    ) -> None:
        """
        실시간 체결 한 틱을 해당 종목 행에 반영한다.

        trade_amount=None이면 opt10030의 누적 거래대금을 보존한다.
        (FID 14는 현재 틱만 포함하므로)
        """
        with self._lock:
            if code not in self._df.index:
                return   # Pre-Filter 에 없는 종목은 무시

            # trade_amount가 None이면 기존 값 유지
            if trade_amount is None:
                trade_amount = self._df.loc[code, "trade_amount"]

            # change_pct가 None이면 기존 값 유지
            if change_pct is None:
                change_pct = self._df.loc[code, "change_pct"]

            self._df.loc[code, self._TICK_COLS] = [
                current_price, high_price, low_price, open_price,
                volume, trade_amount, change_pct,
            ]
            # [NEW] 체결 가속도 — 10초 슬라이딩 윈도우에 (ts, cumvol) 기록
            _ts_now = time.monotonic()
            if code not in self._tick_ts_vol:
                self._tick_ts_vol[code] = _Deque()
            _tq = self._tick_ts_vol[code]
            _tq.append((_ts_now, volume))
            _cutoff_70 = _ts_now - 70.0  # 1분봉 계산에 60초 필요, 여유 10초 확보
            while _tq and _tq[0][0] < _cutoff_70:
                _tq.popleft()
            # 1분봉 누적 — 분(minute)이 바뀔 때만 append (초당 수십 번 실행 최소화)
            cur_min = (datetime.now().hour * 60 +  # noqa: DTZ005
                       datetime.now().minute)
            cp = float(current_price)
            if self._last_min.get(code, -1) != cur_min:
                # 직전 분 완성 캔들 커밋 (처음 진입 시에는 커밋할 이전 분이 없음)
                if code in self._cur_open:
                    def _append120(lst, val):
                        lst.append(val)
                        if len(lst) > 120:
                            lst.pop(0)
                    _append120(self._mins.setdefault(code, []),       cp)
                    _append120(self._min_opens.setdefault(code, []),  self._cur_open[code])
                    _append120(self._min_highs.setdefault(code, []),  self._cur_high[code])
                    _append120(self._min_lows.setdefault(code,  []),  self._cur_low[code])
                    # 분봉 거래량 델타
                    prev_cumvol = self._last_vol.get(code, volume)
                    delta = max(0, volume - prev_cumvol)
                    _append120(self._min_vols.setdefault(code, []), delta)
                    self._last_vol[code] = volume
                # 새 분 시작
                self._last_min[code] = cur_min
                self._cur_open[code] = cp
                self._cur_high[code] = cp
                self._cur_low[code]  = cp
            else:
                # 같은 분 — 고가·저가 갱신
                if code in self._cur_open:
                    if cp > self._cur_high[code]:
                        self._cur_high[code] = cp
                    if cp < self._cur_low[code]:
                        self._cur_low[code] = cp
                else:
                    self._cur_open[code] = cp
                    self._cur_high[code] = cp
                    self._cur_low[code]  = cp

    # ── 조회 ──────────────────────────────────────────────────────────────

    def get_snapshot(self, code: str) -> Optional[StockSnapshot]:
        """단일 종목 스냅샷을 반환한다 (API 호출 없음)."""
        with self._lock:
            if code not in self._df.index:
                return None
            row = self._df.loc[code]

            def safe_int_cell(key: str, default: int = 0) -> int:
                v = _df_cell_scalar(row.get(key, default), None)
                if v is None:
                    return default
                try:
                    iv = int(float(v))
                except (TypeError, ValueError):
                    return default
                return iv if iv != 0 else default

            def safe_float_cell(key: str, default: float = 0.0) -> float:
                v = _df_cell_scalar(row.get(key, default), None)
                if v is None:
                    return default
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return default
                return fv if fv != 0 else default

            nm = _df_cell_scalar(row.get("name", ""), "")
            name_s = str(nm) if nm is not None else ""

            ua_raw = _df_cell_scalar(row.get("updated_at"), None)
            if isinstance(ua_raw, datetime):
                updated_at = ua_raw
            elif ua_raw is not None:
                try:
                    updated_at = pd.Timestamp(ua_raw).to_pydatetime()
                except Exception:
                    updated_at = datetime.now()
            else:
                updated_at = datetime.now()

            # [NEW] 일봉 데이터 추출 (2026-04-03)
            daily_data = self._daily_data.get(code, [])
            daily_closes = [float(c["close"]) for c in daily_data if c.get("close", 0) > 0]
            daily_high_prev = daily_data[0].get("high", 0) if daily_data else 0
            daily_low_prev = daily_data[0].get("low", 0) if daily_data else 0

            # 일봉 정배열 캐시 (5MA>10MA>20MA) — prefilter에서 재계산 없이 바로 사용
            from strategy.jang_dong_min import check_daily_alignment as _cda, calc_rsi as _calc_rsi
            _is_daily_bull = _cda(daily_closes) if len(daily_closes) >= 20 else False

            # RSI14 캐시 — closes_1min 15개 이상일 때만 계산 (부족 시 0.0 → 필터 스킵)
            _closes_now = list(self._mins.get(code, []))
            _rsi_cached = 0.0
            if len(_closes_now) >= 15:
                try:
                    _r = _calc_rsi(_closes_now, 14)
                    if _r is not None and _r > 0:
                        _rsi_cached = float(_r)
                except Exception:
                    pass

            # [NEW] 체결 가속도 비율 계산
            # · 10초 체결량 = 지금 누적 거래량 - 10초 전 누적 거래량
            # · 비교 기준 = 직전 5개 1분봉 평균량 / 6 (60s ÷ 10s = 6구간)
            _exec_vel_ratio = 0.0
            _tq_snap = self._tick_ts_vol.get(code)
            if _tq_snap and len(_tq_snap) >= 2:
                _now_ts = time.monotonic()
                _cutoff_10s = _now_ts - 10.0
                _vol_now = _tq_snap[-1][1]
                _vol_before_10s = _tq_snap[0][1]  # 기본: 데크 최초값
                for _ts_e, _vol_e in _tq_snap:
                    if _ts_e < _cutoff_10s:
                        _vol_before_10s = _vol_e
                _vol_10s = max(0, _vol_now - _vol_before_10s)
                _min_vols_snap = self._min_vols.get(code, [])
                if _min_vols_snap and _vol_10s > 0:
                    _recent_mv = _min_vols_snap[-min(5, len(_min_vols_snap)):]
                    _avg_per_min = sum(_recent_mv) / len(_recent_mv) if _recent_mv else 0
                    _avg_per_10s = _avg_per_min / 6.0
                    if _avg_per_10s > 0:
                        _exec_vel_ratio = _vol_10s / _avg_per_10s
                        logger.debug(
                            "[VEL계산] %s vol_10s=%d avg_per_10s=%.1f ratio=%.2f",
                            code, _vol_10s, _avg_per_10s, _exec_vel_ratio,
                        )

            _sector_snap = self._sector_cache.get(code, "")

            return StockSnapshot(
                code          = code,
                name          = name_s,
                current_price = safe_int_cell("current_price", 0),
                open_price    = safe_int_cell("open_price",    0),
                high_price    = safe_int_cell("high_price",    0),
                low_price     = safe_int_cell("low_price",     0),
                volume        = safe_int_cell("volume",        0),
                trade_amount  = safe_int_cell("trade_amount",  0),
                prev_close    = safe_int_cell("prev_close",    0),
                change_pct    = safe_float_cell("change_pct",  0.0),
                closes_1min   = list(self._mins.get(code, [])),
                opens_1min    = list(self._min_opens.get(code, [])),
                highs_1min    = list(self._min_highs.get(code, [])),
                lows_1min     = list(self._min_lows.get(code,  [])),
                chejan_strength = self._chejan_str.get(code, 100.0),  # [NEW]
                volumes_1min    = list(self._min_vols.get(code, [])),  # [NEW]
                daily_closes  = daily_closes,  # [NEW] 일봉 종가 리스트 (최신순)
                daily_high_prev = daily_high_prev,  # [NEW] 전일 고가
                daily_low_prev  = daily_low_prev,   # [NEW] 전일 저가
                foreign_net_buy = int(self._inv_foreign.get(code, 0)),
                inst_net_buy    = int(self._inv_inst.get(code, 0)),
                investor_score  = int(self._inv_score.get(code, 0)),
                investor_updated_at = self._inv_updated_at.get(code),
                trend_level      = int(self._trend_level.get(code, 0)),
                trend_prev_level = int(self._trend_prev_level.get(code, 0)),
                is_daily_bull    = _is_daily_bull,
                rank             = safe_int_cell("rank", 0),
                rsi              = _rsi_cached,
                updated_at       = updated_at,
                exec_velocity_ratio = _exec_vel_ratio,
                sector              = _sector_snap,
            )

    def update_trend_level(self, code: str, trend_level: int) -> None:
        """요셉 시그널 추세 레벨 갱신(0~3). 직전 단계도 함께 보관."""
        level = int(max(0, min(3, trend_level)))
        with self._lock:
            prev = int(self._trend_level.get(code, 0))
            self._trend_prev_level[code] = prev
            self._trend_level[code] = level

    def update_chejan_strength(self, code: str, strength: float) -> None:
        """[NEW] 체결강도(FID 20) 갱신."""
        if strength > 0:
            with self._lock:
                self._chejan_str[code] = strength

    def update_sector(self, code: str, sector: str) -> None:
        """[NEW] 업종명 캐시 갱신 — handle_signal()에서 opt10001 응답 후 호출."""
        if sector:
            with self._lock:
                self._sector_cache[code] = sector

    def prefilter_candidates(self, max_change_pct: Optional[float] = None) -> list[str]:
        """
        벡터화 사전 필터 — DataFrame 연산으로 Python 루프 전 후보 종목을 추린다.

        조건 (모두 DataFrame 컬럼 연산, O(n) 한 번):
          ① current_price > 0          (가격 유효)
          ② current_price > open_price  (시가 돌파 기본 조건)
          ③ change_pct > 0             (양봉 기조)
          ④ volume > 0                 (거래량 있음)
          ⑤ max_change_pct 지정 시: change_pct < max_change_pct (과열 급등 제외)

        반환값: 조건을 통과한 종목코드 리스트 (MA 검사는 이후 Python 루프에서)

        효과: 50종목 중 보통 5~15개만 남아 Python 루프 비용이 70~90% 감소
        """
        with self._lock:
            if self._df.empty:
                return []
            df = self._df
            ch = df.get("change_pct", pd.Series(0, index=df.index))
            mask = (
                (df["current_price"] > 0) &
                (df["current_price"] > df["open_price"]) &
                (ch > 0) &
                (df["volume"] > 0)
            )
            if max_change_pct is not None:
                mask = mask & (ch < max_change_pct)
            return list(df.index[mask])

    def update_investor(
        self,
        code:        str,
        foreign_net: int,
        inst_net:    int,
    ) -> None:
        """
        외국인/기관 순매수 수량을 StockSnapshot에 갱신한다 (opt10059 10분 주기 호출).

        investor_score 산출:
          +1 : 외국인 AND 기관 모두 순매수 → 수급 우호
           0 : 어느 한쪽만 순매수 또는 둘 다 0
          -1 : 외국인 AND 기관 모두 순매도 → 수급 비우호
        """
        with self._lock:
            if code not in self._df.index:
                return
            self._inv_foreign[code] = int(foreign_net)
            self._inv_inst[code] = int(inst_net)
            if foreign_net > 0 and inst_net > 0:
                score = 1
            elif foreign_net < 0 and inst_net < 0:
                score = -1
            else:
                score = 0
            self._inv_score[code] = score
            self._inv_updated_at[code] = datetime.now()   # noqa: DTZ005

    def get_investor_data(self, code: str) -> tuple[int, int, int]:
        """종목의 수급 데이터를 (foreign_net_buy, inst_net_buy, investor_score) 튜플로 반환.

        get_snapshot() 없이 _inv_* 딕셔너리만 읽으므로 StockSnapshot 객체 생성 비용 없음.
        미조회 종목은 (0, 0, 0) 반환.
        """
        with self._lock:
            return (
                int(self._inv_foreign.get(code, 0)),
                int(self._inv_inst.get(code, 0)),
                int(self._inv_score.get(code, 0)),
            )

    def set_min_candles(self, code: str, closes: list) -> None:
        """opt10080 등으로 가져온 분봉 종가 리스트를 초기값으로 설정한다."""
        with self._lock:
            self._mins[code] = [float(c) for c in closes if c]

    def set_min_candles_ohlc(self, code: str, candles: list[dict]) -> None:
        """분봉 OHLCV 전체를 초기값으로 설정한다 (캔들 패턴 판단용).

        Args:
            candles: [{"open": int, "high": int, "low": int, "close": int, "volume": int}, ...]
                     오래된 것 → 최신 순 (시간순 오름차순)
        """
        with self._lock:
            self._mins[code]       = [float(c["close"])  for c in candles if c.get("close")]
            self._min_opens[code]  = [float(c["open"])   for c in candles if c.get("open")]
            self._min_highs[code]  = [float(c["high"])   for c in candles if c.get("high")]
            self._min_lows[code]   = [float(c["low"])    for c in candles if c.get("low")]
            self._min_vols[code]   = [int(c.get("volume", 0)) for c in candles]  # volume 포함

    # ── 1분봉 디스크 캐시 ────────────────────────────────────────────────────

    @staticmethod
    def _get_1min_cache_path() -> Path:
        today = datetime.now().strftime("%Y%m%d")
        cache_dir = Path(__file__).parent.parent / "cache"
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"1min_{today}.json"

    def _load_1min_cache(self) -> None:
        """당일 1분봉 캐시 파일이 있으면 메모리로 로드한다 (데이터 부족 코드만)."""
        try:
            if not self._1min_cache_path.exists():
                return
            with open(self._1min_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            loaded = 0
            with self._lock:
                for code, ohlcv in data.items():
                    if len(self._mins.get(code, [])) >= 55:
                        continue   # 이미 충분한 데이터 → 스킵
                    closes = ohlcv.get("c", [])
                    opens  = ohlcv.get("o", [])
                    highs  = ohlcv.get("h", [])
                    lows   = ohlcv.get("l", [])
                    vols   = ohlcv.get("v", [])
                    if closes:
                        self._mins[code]       = [float(x) for x in closes]
                        self._min_opens[code]  = [float(x) for x in opens]
                        self._min_highs[code]  = [float(x) for x in highs]
                        self._min_lows[code]   = [float(x) for x in lows]
                        self._min_vols[code]   = [int(x)   for x in vols]
                        loaded += 1
            if loaded:
                logger.info("[1분봉캐시] 로드 완료 — %d종목 (%s)", loaded, self._1min_cache_path.name)
        except Exception as e:
            logger.warning("[1분봉캐시] 로드 실패 — %s", e)

    def save_1min_cache(self) -> None:
        """현재 1분봉 데이터 전체를 디스크에 저장한다 (5분마다 호출)."""
        try:
            with self._lock:
                data = {
                    code: {
                        "c": self._mins.get(code, []),
                        "o": self._min_opens.get(code, []),
                        "h": self._min_highs.get(code, []),
                        "l": self._min_lows.get(code, []),
                        "v": self._min_vols.get(code, []),
                    }
                    for code in self._mins
                    if len(self._mins[code]) >= 10   # 10개 미만 부실 데이터 제외
                }
            with open(self._1min_cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            logger.debug("[1분봉캐시] 저장 완료 — %d종목", len(data))
        except Exception as e:
            logger.warning("[1분봉캐시] 저장 실패 — %s", e)

    def load_1min_for_code(self, code: str) -> int:
        """
        캐시 파일에서 특정 종목의 1분봉을 즉시 로드한다.
        이미 55개 이상이면 스킵. 반환값 = 로드된 캔들 수 (0이면 캐시 없음).
        """
        try:
            if not self._1min_cache_path.exists():
                return 0
            with open(self._1min_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            ohlcv = data.get(code)
            if not ohlcv:
                return 0
            closes = ohlcv.get("c", [])
            if not closes:
                return 0
            with self._lock:
                if len(self._mins.get(code, [])) >= 55:
                    return len(self._mins[code])
                self._mins[code]       = [float(x) for x in closes]
                self._min_opens[code]  = [float(x) for x in ohlcv.get("o", [])]
                self._min_highs[code]  = [float(x) for x in ohlcv.get("h", [])]
                self._min_lows[code]   = [float(x) for x in ohlcv.get("l", [])]
                self._min_vols[code]   = [int(x)   for x in ohlcv.get("v", [])]
            return len(closes)
        except Exception:
            return 0

    # ── 일봉 디스크 캐시 ──────────────────────────────────────────────────────

    @staticmethod
    def _get_daily_cache_path() -> Path:
        """오늘 날짜 기준 일봉 캐시 파일 경로 반환."""
        today = datetime.now().strftime("%Y%m%d")
        cache_dir = Path(__file__).parent.parent / "cache"
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"daily_{today}.json"

    def _load_daily_cache(self) -> None:
        """당일 일봉 캐시 파일이 있으면 메모리로 로드한다."""
        try:
            if not self._daily_cache_path.exists():
                return
            with open(self._daily_cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            self._daily_data = data
            logger.info(
                "[일봉캐시] 로드 완료 — %d종목 (파일: %s)",
                len(data), self._daily_cache_path.name,
            )
        except Exception as e:
            logger.warning("[일봉캐시] 로드 실패 — %s", e)

    def _save_daily_cache(self) -> None:
        """현재 _daily_data 전체를 디스크에 저장한다."""
        try:
            with self._lock:
                snapshot = dict(self._daily_data)
            with open(self._daily_cache_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("[일봉캐시] 저장 실패 — %s", e)

    def set_daily_candles(self, code: str, candles: list[dict]) -> None:
        """
        [NEW] opt10081로 가져온 일봉 OHLCV 데이터를 캐시에 저장한다.

        Args:
            code: 종목코드
            candles: 일봉 캔들 리스트
                   [{"date": "YYYYMMDD", "open": int, "high": int, "low": int, "close": int, "volume": int}, ...]
        """
        with self._lock:
            if candles:
                self._daily_data[code] = candles
                self._daily_updated_at[code] = datetime.now()
        if candles:
            self._save_daily_cache()  # 락 밖에서 디스크 기록

    def top_by_trade_amount(self, n: int = 20) -> pd.DataFrame:
        """
        거래대금 상위 n 종목 DataFrame 반환 (복사본).
        trade_amount 가 모두 0 이면 volume → rank 순으로 fallback.
        [2026-04-03] 중복 인덱스 제거 추가
        """
        with self._lock:
            if self._df.empty:
                return pd.DataFrame()
            # 중복 인덱스 제거 (같은 code가 여러 줄이면 최신 것만 유지)
            df = self._df[~self._df.index.duplicated(keep='last')]

            non_zero_amt = df[df["trade_amount"] > 0]
            if not non_zero_amt.empty:
                return non_zero_amt.nlargest(n, "trade_amount").copy()
            # trade_amount 모두 0인 경우 거래량으로 fallback
            non_zero_vol = df[df["volume"] > 0]
            if not non_zero_vol.empty:
                return non_zero_vol.nlargest(n, "volume").copy()
            # 거래량도 없으면 rank 기준
            if "rank" in df.columns:
                ranked = df.dropna(subset=["rank"])
                if not ranked.empty:
                    return ranked.nsmallest(n, "rank").copy()
            return df.head(n).copy()

    def export_csv(self, path: str = "logs/snapshot.csv") -> None:
        """현재 스냅샷을 CSV 로 내보낸다."""
        with self._lock:
            self._df.reset_index().to_csv(path, index=False, encoding="utf-8-sig")

    def __len__(self) -> int:
        return len(self._df)


# ---------------------------------------------------------------------------
# ② ScannerLogger — scanner.log 구조화 기록
# ---------------------------------------------------------------------------

class ScannerLogger:
    """
    스캐너 판단 근거를 scanner.log + 일별 CSV 에 이중 기록한다.

    scanner.log 라인 형식 (탭 구분):
      PASS   | code | name | filter_step | reason
      FAIL   | code | name | filter_step | reason        ← 탈락 (DEBUG 레벨)
      NEAR   | code | name | filter_step | reason        ← 근사 탈락 (INFO 레벨)
      SIGNAL | code | name | signal_type | price | reason

    일별 CSV: logs/scanner_reject_YYYYMMDD.csv
      time, code, name, filter_step, near_miss, reason
      → 하루치 탈락 이력을 pandas 로 바로 읽어 전략 튜닝에 활용

    FeedbackEngine / LogAnalyzer 는 양쪽 파일을 모두 읽을 수 있음.
    """

    # ── CSV 클래스 변수 (날짜별 자동 교체) ───────────────────────────────────
    _csv_file   = None          # IO[str] | None
    _csv_writer = None          # csv.writer | None
    _csv_date   = None          # date | None
    _csv_lock   = threading.Lock()

    @classmethod
    def _ensure_csv(cls) -> None:
        """오늘 날짜의 CSV 파일이 열려 있는지 확인하고, 필요하면 열거나 교체한다."""
        today = date.today()
        if cls._csv_date == today and cls._csv_file is not None:
            return
        # 기존 파일 닫기
        if cls._csv_file is not None:
            try:
                cls._csv_file.close()
            except Exception:
                pass
        path = Path("logs") / f"scanner_reject_{today.strftime('%Y%m%d')}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        cls._csv_file   = open(path, "a", encoding="utf-8-sig", newline="")
        cls._csv_writer = csv.writer(cls._csv_file)
        if is_new:
            cls._csv_writer.writerow(["time", "code", "name", "filter_step", "near_miss", "reason"])
        cls._csv_date = today

    @classmethod
    def _write_csv(cls, code: str, name: str, step: str,
                   reason: str, near_miss: bool = False) -> None:
        """CSV 에 탈락 한 줄 추가. 예외는 무시(로깅 자체가 주 업무 차단하면 안 됨)."""
        with cls._csv_lock:
            try:
                cls._ensure_csv()
                cls._csv_writer.writerow([
                    datetime.now().strftime("%H:%M:%S"),
                    code, name, step,
                    "Y" if near_miss else "",
                    reason,
                ])
                cls._csv_file.flush()
            except Exception as _e:
                logger.debug("[ScannerLogger] CSV 쓰기 오류: %s", _e)

    # ── 공개 API ──────────────────────────────────────────────────────────────

    @staticmethod
    def passed(code: str, name: str, step: str, reason: str) -> None:
        scan_log.info("PASS\t%s\t%s\t%s\t%s", code, name, step, reason)

    @staticmethod
    def rejected(code: str, name: str, step: str, reason: str) -> None:
        """일반 탈락 — scanner.log(DEBUG) + 일별 CSV 에 기록."""
        scan_log.debug("FAIL\t%s\t%s\t%s\t%s", code, name, step, reason)
        ScannerLogger._write_csv(code, name, step, reason, near_miss=False)

    @staticmethod
    def near_miss(
        code: str, name: str, step: str,
        actual: float, threshold: float,
        reason: str,
        miss_pct_limit: float = 15.0,
    ) -> None:
        """
        기준값에 근접하여 탈락한 경우 별도 기록 (파라미터 튜닝 힌트).

        actual vs threshold 의 차이가 threshold 대비 miss_pct_limit % 이내이면
        [근사] 태그를 붙여 scanner.log INFO 레벨로 기록하고, CSV 에도 near_miss=Y 로 저장.

        예) RSI 87.5 탈락(기준 88.0) → 차이 0.5/88.0 = 0.57% < 15% → 근사 탈락
        """
        diff_pct = abs(actual - threshold) / max(abs(threshold), 1e-9) * 100
        if diff_pct <= miss_pct_limit:
            tagged = f"[근사 {diff_pct:.1f}%] {reason}"
            scan_log.info("NEAR\t%s\t%s\t%s\t%s", code, name, step, tagged)
            ScannerLogger._write_csv(code, name, step, tagged, near_miss=True)
        else:
            # 근사 아님 → 일반 rejected 와 동일
            scan_log.debug("FAIL\t%s\t%s\t%s\t%s", code, name, step, reason)
            ScannerLogger._write_csv(code, name, step, reason, near_miss=False)

    @staticmethod
    def signal(sig: ScanSignal) -> None:
        scan_log.warning(
            "SIGNAL\t%s\t%s\t%s\t%d\t%s",
            sig.code, sig.name, sig.signal_type, sig.price, sig.reason,
        )

    @staticmethod
    def pre_filter_summary(total: int, passed: int, top_n: int) -> None:
        scan_log.info(
            "PRE_FILTER\t전체=%d\t통과=%d\tTop%d 선정",
            total, passed, top_n,
        )

    @staticmethod
    def cycle_summary(n_candidates: int, step_counts: "dict[str, int]") -> None:
        """
        스캔 사이클 종료 시 필터별 탈락 건수 요약.
        step_counts: {filter_step: 탈락_건수} — 호출자가 해당 사이클에서 집계해 넘김.
        """
        if not step_counts:
            return
        top = sorted(step_counts.items(), key=lambda x: -x[1])[:6]
        top_str = "  ".join(f"{s}:{c}" for s, c in top)
        scan_log.info(
            "CYCLE\t후보%d건\t탈락상위→ %s",
            n_candidates, top_str,
        )


# ---------------------------------------------------------------------------
# ③ ScannerDisplay — rich 터미널 뷰
# ---------------------------------------------------------------------------

_CONSOLE = Console()

class ScannerDisplay:
    """
    rich.Live 를 사용해 VS Code 터미널에 실시간 감시 테이블을 출력한다.

    사용 예)
        display = ScannerDisplay(store, cfg)
        display.start()          # 백그라운드 갱신 시작
        display.alert(signal)    # 신호 발생 시 즉시 알림
        display.stop()
    """

    def __init__(self, store: SnapshotStore, cfg: SmartScannerConfig) -> None:
        self._store   = store
        self._cfg     = cfg
        self._live    = Live(console=_CONSOLE, refresh_per_second=1, screen=False)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._live.start()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ScannerDisplay"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._live.stop()

    def alert(self, sig: ScanSignal) -> None:
        """신호 발생 시 터미널에 즉시 강조 출력한다."""
        color = "bright_red" if sig.signal_type == "BREAKOUT" else "bright_green"
        _CONSOLE.print(
            f"\n🚨 [{color}][ {sig.signal_type} ] {sig.name}({sig.code})[/] "
            f"  가격 [bold]{sig.price:,}원[/]  |  {sig.reason}\n",
        )

    # ── 루프 ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            self._live.update(self._build_table())
            time.sleep(1.0)

    def _build_table(self) -> Table:
        top_df = self._store.top_by_trade_amount(self._cfg.display_top_n)

        table = Table(
            title=f"[bold cyan]SmartScanner 감시 현황[/]  "
                  f"{datetime.now().strftime('%H:%M:%S')}  "
                  f"[dim](감시 {len(top_df)}종목)[/]",
            show_lines=False,
            header_style="bold white on dark_blue",
            border_style="dim",
        )
        table.add_column("순위",   justify="right",  width=5)
        table.add_column("종목코드", width=8)
        table.add_column("종목명",  width=12)
        table.add_column("현재가",  justify="right",  width=9)
        table.add_column("등락률",  justify="right",  width=8)
        table.add_column("거래량",  justify="right",  width=10)
        table.add_column("거래대금", justify="right", width=16)
        table.add_column("갱신시각", width=9)

        if top_df.empty:
            table.add_row(*["─"] * 8)
            return table

        for rank, (code, row) in enumerate(top_df.iterrows(), 1):
            # pandas Series에서 값 안전하게 추출 (or 연산자 사용 금지)
            cp = row.get("change_pct", 0)
            change = float(cp) if cp else 0.0
            if change > 0:
                pct_text = Text(f"+{change:.2f}%", style="bright_red")
            elif change < 0:
                pct_text = Text(f"{change:.2f}%",  style="bright_blue")
            else:
                pct_text = Text(f"{change:.2f}%",  style="white")

            p = row.get("current_price", 0)
            v = row.get("volume", 0)
            a = row.get("trade_amount", 0)
            price = int(p) if p else 0
            vol   = int(v) if v else 0
            amt   = int(a) if a else 0
            upd   = row.get("updated_at", datetime.now())
            upd_s = upd.strftime("%H:%M:%S") if isinstance(upd, datetime) else "--:--:--"

            table.add_row(
                str(rank),
                str(code),
                str(row.get("name", "")),
                f"{price:,}",
                pct_text,
                f"{vol:,}",
                format_trade_amount_korean(amt),
                upd_s,
            )

        return table


# ---------------------------------------------------------------------------
# TopVolumeManager — 거래대금 상위 N 종목 관리
# ---------------------------------------------------------------------------

class TopVolumeManager:
    def __init__(self, top_n: int = 200) -> None:
        self.top_n     = top_n
        self._amounts: dict[str, int] = {}
        self._lock     = threading.Lock()

    def clear(self) -> None:
        """이전 스캔에서 쌓인 거래대금 맵을 비운다."""
        with self._lock:
            self._amounts.clear()

    def update(self, code: str, trade_amount: int) -> bool:
        with self._lock:
            self._amounts[code] = trade_amount
            return self._rank(code) <= self.top_n

    def get_top_codes(self, n: Optional[int] = None) -> list[str]:
        n = n or self.top_n
        with self._lock:
            return [c for c, _ in sorted(
                self._amounts.items(), key=lambda x: -x[1]
            )[:n]]

    def _rank(self, code: str) -> int:
        sorted_codes = [c for c, _ in sorted(
            self._amounts.items(), key=lambda x: -x[1]
        )]
        try:
            return sorted_codes.index(code) + 1
        except ValueError:
            return 999999


# ---------------------------------------------------------------------------
# PriorityWatchQueue — SetRealReg 구독 관리
# ---------------------------------------------------------------------------

class PriorityWatchQueue:
    def __init__(self, kiwoom, screen_no: str = "9200", max_subs: int = 100) -> None:
        self._kiwoom   = kiwoom
        self._screen   = screen_no
        self._max_subs = max_subs
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()

    def refresh(self, top_codes: list[str]) -> None:
        with self._lock:
            target    = set(top_codes[: self._max_subs])
            to_add    = target - self._subscribed
            to_remove = self._subscribed - target
            for code in to_remove:
                self._unsub(code)
            if to_add:
                # 여러 종목을 SetRealReg 1회 배치 호출 (50회 → 1회)
                # strCodeList 에 ';' 구분 다종목 지원 (키움 API 공식 지원)
                code_list = ";".join(to_add)
                self._kiwoom._ocx.dynamicCall(
                    "SetRealReg(QString, QString, QString, QString)",
                    [self._screen, code_list, "10;11;12;13;14;16;17;18;20", "1"],  # [NEW] FID 20: 체결강도
                )
                self._subscribed.update(to_add)
                logger.debug("[PriorityWatchQueue] SetRealReg 배치 등록 %d종목", len(to_add))

    def _sub(self, code: str) -> None:
        self._kiwoom._ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            [self._screen, code, "10;11;12;13;14;16;17;18;20", "1"],  # [NEW] FID 20: 체결강도
        )
        self._subscribed.add(code)

    def _unsub(self, code: str) -> None:
        self._kiwoom._ocx.dynamicCall(
            "SetRealRemove(QString, QString)", [self._screen, code]
        )
        self._subscribed.discard(code)

    @property
    def subscribed(self) -> set[str]:
        return self._subscribed.copy()


# ---------------------------------------------------------------------------
# 신호 판단 함수 (순수 함수)
# ---------------------------------------------------------------------------

def check_breakout(
    snap:                    StockSnapshot,
    breakout_ratio:          float = 0.03,  # 2026-04-03 재강화: 1% → 3% (가짜 돌파 방지)
    volume_mult:             float = 1.0,   # 2026-04-03: 1.5 → 1.0 (거래량 완화)
    pullback_from_high_pct:  float = 1.5,   # 당일 고점 대비 N% 이상 하락 시 차단 (0=비활성)
    min_rising_bars:         int   = 2,     # 최근 N개 1분봉 연속 상승 요구 (0=비활성)
) -> Optional[str]:
    if snap.prev_close <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT", "prev_close=0")
        return None

    threshold = snap.prev_close * (1 + breakout_ratio)

    if snap.current_price < threshold:
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"현재가 {snap.current_price:,} < 돌파기준 {threshold:,.0f}",
        )
        return None

    # [RELAXED] 신고가 갱신 requirement 제거 (조건문 2026-04-03)
    # 이유: 당일 11:00 이전 시점에 신고가 도달은 극히 드문 사건.
    #       대신 전일 종가 돌파만으로 신호 판정 — 더 자주 거래 기회 제공
    # (과거) if snap.current_price < snap.high_price: return None

    avg_vol = snap.trade_amount / snap.current_price if snap.current_price else 0
    # 거래대금이 충분하면 거래량 체크, 부족하면 통과 (선택적 필터)
    if snap.trade_amount > 0 and (avg_vol <= 0 or snap.volume < avg_vol * volume_mult):
        ScannerLogger.rejected(
            snap.code, snap.name, "BREAKOUT",
            f"거래량 부족 ({snap.volume:,} < 기준 {avg_vol * volume_mult:,.0f})",
        )
        return None

    # ── ① 당일 고점 대비 하락폭 차단 ─────────────────────────────────────
    # 현재가가 당일 고점에서 pullback_from_high_pct% 이상 내려와 있으면 하락 추세로 판단
    if pullback_from_high_pct > 0 and snap.high_price > 0:
        pullback = (snap.current_price - snap.high_price) / snap.high_price * 100
        if pullback <= -pullback_from_high_pct:
            ScannerLogger.rejected(
                snap.code, snap.name, "BREAKOUT",
                f"고점({snap.high_price:,}) 대비 {pullback:.2f}% 하락 중 "
                f"(차단기준 -{pullback_from_high_pct:.1f}%) — 하락추세",
            )
            return None

    # ── ② 1분봉 연속 상승 확인 ───────────────────────────────────────────
    # 최근 min_rising_bars개 봉이 모두 직전 봉 대비 상승이어야 통과
    closes = snap.closes_1min
    if min_rising_bars > 0 and len(closes) >= min_rising_bars + 1:
        rising = all(
            closes[-(i + 1)] > closes[-(i + 2)]
            for i in range(min_rising_bars)
        )
        if not rising:
            recent = [int(closes[-(i + 1)]) for i in range(min(min_rising_bars + 1, len(closes)))]
            recent_str = " → ".join(f"{p:,}" for p in reversed(recent))
            ScannerLogger.rejected(
                snap.code, snap.name, "BREAKOUT",
                f"1분봉 연속상승 {min_rising_bars}개 미충족 ({recent_str}) — 하락/횡보",
            )
            return None

    # ✅ 모든 조건 통과
    reason = (
        f"전일종가 {snap.prev_close:,} 대비 {breakout_ratio*100:.1f}% 돌파 "
        f"| 현재가 {snap.current_price:,}"
    )
    ScannerLogger.passed(snap.code, snap.name, "BREAKOUT", reason)
    return reason


def check_testa_alignment(
    snap: StockSnapshot,
    max_ma_spread: float = 0.05,   # MA10-MA50 이격도 상한 (5%) — 과열 설거지 방지
) -> Optional[str]:
    """
    테스타 정배열 확인: MA10 > MA20 > MA50 + 이격도 과열 필터.

    조건:
      ① MA10 > MA20 > MA50   (정배열)
      ② (MA10 - MA50) / MA50 ≤ max_ma_spread   (이격 과열 차단)
         → MA10 이 MA50 보다 5% 이상 높으면 이미 급등 종료 구간 (설거지 위험)

    1분봉 종가 50개 이상 필요.
    """
    closes = snap.closes_1min
    if len(closes) < 50:
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"1분봉 데이터 부족 ({len(closes)}/50)",
        )
        return None

    from strategy.jang_dong_min import calc_ma
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma50 = calc_ma(closes, 50)

    if any(v is None for v in [ma10, ma20, ma50]):
        ScannerLogger.rejected(snap.code, snap.name, "TESTA", "MA 계산 실패")
        return None

    if not (ma10 > ma20 > ma50):
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"정배열 미충족 MA10={ma10:.0f} MA20={ma20:.0f} MA50={ma50:.0f}",
        )
        return None

    # 이격도 과열 체크 — (MA10 - MA50) / MA50 > max_ma_spread 이면 탈락
    spread = (ma10 - ma50) / ma50 if ma50 > 0 else 0.0
    if spread > max_ma_spread:
        ScannerLogger.rejected(
            snap.code, snap.name, "TESTA",
            f"MA 이격 과열 {spread:.1%} > {max_ma_spread:.0%} "
            f"(MA10={ma10:.0f} MA50={ma50:.0f}) — 설거지 위험",
        )
        return None

    reason = (
        f"정배열 MA10={ma10:.0f} > MA20={ma20:.0f} > MA50={ma50:.0f} "
        f"이격={spread:.1%}"
    )
    ScannerLogger.passed(snap.code, snap.name, "TESTA", reason)
    return reason


def check_jdm_open_breakout(
    snap: StockSnapshot,
    cfg: SmartScannerConfig,
    min_body_ratio: float = 0.7,   # 양봉 몸통 비율 하한 — 윗꼬리 가짜 돌파 차단
) -> Optional[str]:
    """
    장동민 개선형: OR 3조건 + 양봉 몸통 비율 필터.

    조건 0 (기존): current_price > open_price  (시가 돌파)
    조건 A (V자반등): current_price > prev_close AND current_price >= open_price * prev_close_min_ratio
                    → 어제 가격을 돌파하며 V자 반등, 시가 대비 -2% 이내 제한
    조건 B (VI직전): current_price >= high_price AND change_pct >= vi_approach_chg_pct
                   → 이미 1차 상승 후 고점 재돌파, VI 달려가는 주도주

    세 조건 중 하나라도 통과하면, 양봉 몸통 비율 필터까지 체크 후 통과.
    """
    if snap.open_price <= 0 or snap.current_price <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "JDM_OPEN", "시가/현재가 0")
        return None

    # OR 3조건 검사
    cond0 = snap.current_price > snap.open_price
    cond_a = (snap.current_price > snap.prev_close and
              snap.current_price >= snap.open_price * cfg.prev_close_min_ratio)
    cond_b = (snap.current_price >= snap.high_price and
              snap.change_pct >= cfg.vi_approach_chg_pct)

    condition_met = False
    condition_reason = ""

    if cond0:
        condition_met = True
        condition_reason = "시가돌파"
    elif cond_a:
        condition_met = True
        condition_reason = "V자반등"
    elif cond_b:
        condition_met = True
        condition_reason = "VI직전"

    if not condition_met:
        detail = (
            f"3조건 불만족: "
            f"시가돌파({cond0}) V자반등({cond_a}) VI직전({cond_b}) "
            f"현재={snap.current_price:,} 시가={snap.open_price:,} "
            f"전일={snap.prev_close:,} 고가={snap.high_price:,} 등락={snap.change_pct:.1f}%"
        )
        ScannerLogger.rejected(snap.code, snap.name, "JDM_OPEN", detail)
        return None

    # 양봉 몸통 비율 체크
    candle_range = snap.high_price - snap.low_price
    if candle_range > 0:
        body_ratio = (snap.current_price - snap.open_price) / candle_range
        if body_ratio < min_body_ratio:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_OPEN",
                f"{condition_reason} 통과했으나 몸통 비율 부족 {body_ratio:.0%} < {min_body_ratio:.0%}",
            )
            return None

    breakout_pct = (snap.current_price - snap.open_price) / snap.open_price * 100
    body_ratio_str = (
        f" 몸통={((snap.current_price - snap.open_price) / candle_range):.0%}"
        if candle_range > 0 else ""
    )
    reason = (
        f"{condition_reason} 현재가={snap.current_price:,} > "
        f"시가={snap.open_price:,}(+{breakout_pct:.2f}%){body_ratio_str}"
    )
    ScannerLogger.passed(snap.code, snap.name, "JDM_OPEN", reason)
    return reason


# [NEW] 신규 필터 함수 3개 — JDM 신호 품질 강화 (4중 필터)

def check_volume_surge(
    snap: StockSnapshot,
    surge_mult: float = 1.5,
    lookback: int = 10,
) -> Optional[str]:
    """
    [개선] 직전 N분 평균 거래량 대비 surge_mult 배 이상인지 확인.

    기존: 직전 5분 평균 대비 1.5배
    개선: 직전 lookback분(기본 10분) 평균 대비 surge_mult배(기본 5.0배)
    → 더 강력한 수급 확인 (가짜 신호 필터링 강화)
    """
    vols = snap.volumes_1min
    # 데이터 부족: lookback+1개 필요 (현재 1분 + 과거 lookback분)
    if len(vols) < lookback + 1:
        return None

    # 직전 lookback분 평균
    avg_lookback = sum(vols[-(lookback+1):-1]) / lookback
    if avg_lookback <= 0:
        return None

    cur = vols[-1]
    if cur < avg_lookback * surge_mult:
        ScannerLogger.rejected(
            snap.code, snap.name, "VOL_SURGE",
            f"거래량 {cur:,} / {lookback}분평균 {avg_lookback:,.0f} ({cur/avg_lookback:.1f}배 < {surge_mult}배)"
        )
        return None

    return f"거래량급증{cur:,}주({cur/avg_lookback:.1f}배)"


def check_chejan_strength(
    snap: StockSnapshot,
    min_strength: float = 120.0,
) -> Optional[str]:
    """[NEW] 체결강도 min_strength% 이상 확인 (매수 수급 우위)."""
    if snap.chejan_strength < min_strength:
        ScannerLogger.rejected(snap.code, snap.name, "CHEJAN",
                               f"체결강도 {snap.chejan_strength:.0f}% < {min_strength:.0f}%")
        return None
    return f"체결강도{snap.chejan_strength:.0f}%"


def check_disparity_from_ma(
    snap: StockSnapshot,
    ma_period: int   = 20,
    max_pct: float   = 5.0,
) -> Optional[str]:
    """[NEW] 1분봉 MA(ma_period) 대비 이격도 max_pct% 이내 확인 (과열 차단)."""
    closes = snap.closes_1min
    if len(closes) < ma_period:
        return None   # 데이터 부족 시 bypass (초반 20분간 허용)
    from strategy.jang_dong_min import calc_ma
    ma = calc_ma(closes, ma_period)
    if ma is None or ma <= 0:
        return None
    disp = (snap.current_price - ma) / ma * 100
    if disp > max_pct:
        ScannerLogger.rejected(snap.code, snap.name, "DISPARITY",
                               f"MA{ma_period} 이격도 {disp:.1f}% > {max_pct:.1f}%")
        return None
    return f"MA{ma_period}이격{disp:.1f}%"


def check_ema20_filter(snap: StockSnapshot, period: int = 20) -> Optional[str]:
    """
    EMA20 추세 필터 — 현재가가 20분 EMA 위에 있어야 진입 허용.

    완성된 1분봉 closes_1min 기준으로 EMA20 계산.
    현재가 > EMA20 이면 상승 추세로 판단, 통과.
    """
    closes = snap.closes_1min
    if len(closes) < period:
        ScannerLogger.rejected(snap.code, snap.name, "EMA20",
                               f"데이터 부족 ({len(closes)}/{period})")
        return None
    from strategy.jang_dong_min import calc_ema
    ema20 = calc_ema(closes, period)
    if ema20 is None:
        return None
    if snap.current_price <= ema20:
        ScannerLogger.rejected(
            snap.code, snap.name, "EMA20",
            f"현재가 {snap.current_price:,} ≤ EMA20 {ema20:,.0f} — 하락 추세",
        )
        return None
    return f"EMA20상단(현재가={snap.current_price:,}/EMA20={ema20:,.0f})"


def check_bullish_engulfing(snap: StockSnapshot) -> Optional[str]:
    """
    상승 장악형(Bullish Engulfing) 완성 여부 확인.

    완성된 마지막 두 1분봉 기준:
      ① 직전 봉이 음봉 (open > close)
      ② 현재 봉 시가 ≤ 직전 봉 종가 (갭다운 or 동가 출발)
      ③ 현재 봉 종가 > 직전 봉 시가 (완전 장악)

    Returns:
        패턴 설명 문자열 or None
    """
    c = snap.closes_1min
    o = snap.opens_1min
    if len(c) < 2 or len(o) < 2:
        return None
    prev_o, prev_c = o[-2], c[-2]
    curr_o, curr_c = o[-1], c[-1]
    if prev_c >= prev_o:          # 직전 봉이 양봉이면 패턴 불성립
        return None
    if curr_o <= prev_c and curr_c > prev_o:
        return f"상승장악형(직전음봉:{prev_o:.0f}→{prev_c:.0f} / 현재:{curr_o:.0f}→{curr_c:.0f})"
    return None


def check_bullish_pin_bar(snap: StockSnapshot, min_tail_ratio: float = 0.55) -> Optional[str]:
    """
    강세 핀바(Bullish Pin Bar) 완성 여부 확인.

    완성된 마지막 1분봉 기준:
      ① 하단 꼬리 길이 ≥ 전체 범위의 min_tail_ratio (기본 55%)
      ② 종가 ≥ 봉 중간값 ((고가 + 저가) / 2) — 회복 확인

    Returns:
        패턴 설명 문자열 or None
    """
    c = snap.closes_1min
    h = snap.highs_1min
    l = snap.lows_1min
    o = snap.opens_1min
    if len(c) < 1 or len(h) < 1 or len(l) < 1 or len(o) < 1:
        return None
    curr_c, curr_h, curr_l, curr_o = c[-1], h[-1], l[-1], o[-1]
    total_range = curr_h - curr_l
    if total_range <= 0:
        return None
    body_low    = min(curr_o, curr_c)
    lower_tail  = body_low - curr_l
    mid_price   = (curr_h + curr_l) / 2
    tail_ratio  = lower_tail / total_range
    if tail_ratio >= min_tail_ratio and curr_c >= mid_price:
        return f"강세핀바(하꼬리{tail_ratio*100:.0f}%,저가:{curr_l:.0f})"
    return None


def check_breakout_gate(snap: "StockSnapshot", cfg: SmartScannerConfig) -> Optional[str]:
    """
    BREAKOUT 확인 후 진입 가능 여부를 검증하는 공통 게이트.

    check_jdm_entry 와 동일한 시장 안전 필터를 BREAKOUT 경로에도 적용한다.
      ① 지수 등락률 차단 (index_block_pct)
      ② 진입 허용 시각
      ③ 시간대 슬롯 기반 등락률 상한 (max_change_pct_*)
      ④ 시간대 슬롯 기반 체결강도 하한 (공포 장세 상향 포함)
      ⑤ 손절 블랙리스트는 handle_signal() 에서 처리하므로 여기선 생략

    Returns:
        None   → 진입 거부 (ScannerLogger 에 이유 기록됨)
        reason → 거부 없음 (추가 필터 통과 이유 문자열)
    """
    # ① 지수 등락률 완전 차단
    _block_pct  = getattr(cfg, "index_block_pct", -1.5)
    _kospi_chg  = getattr(cfg, "kospi_chg_pct",   0.0)
    _kosdaq_chg = getattr(cfg, "kosdaq_chg_pct",  0.0)
    if _kospi_chg <= _block_pct:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_INDEX",
            f"코스피 하락 차단 — {_kospi_chg:+.2f}% ≤ {_block_pct:.1f}%")
        return None
    if _kosdaq_chg <= _block_pct:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_INDEX",
            f"코스닥 하락 차단 — {_kosdaq_chg:+.2f}% ≤ {_block_pct:.1f}%")
        return None

    # ② 진입 허용 시각
    now = datetime.now().time()
    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_TIME",
            f"진입 허용 시간 아님 ({cfg.entry_start_time}~{cfg.entry_end_time})")
        return None

    # ③ 시간대 슬롯 기반 등락률 상한
    _slot       = _resolve_time_slot(now, cfg)
    _eff_ch_max = _get_slot_value(_slot, cfg, "max_change_pct", cfg.max_change_pct)
    _snap_chg   = float(getattr(snap, "change_pct", 0) or 0)
    if _snap_chg >= _eff_ch_max:
        ScannerLogger.rejected(snap.code, snap.name, "BREAKOUT_CHGPCT",
            f"[{_slot}] 등락률 {_snap_chg:.2f}% ≥ 구간 상한 {_eff_ch_max:.0f}%")
        return None

    # ④ 시간대 슬롯 기반 체결강도 (공포 장세 상향 포함)
    _eff_chejan = _get_slot_value(_slot, cfg, "min_chejan_strength", cfg.min_chejan_strength)
    _fear_pct    = getattr(cfg, "market_fear_pct",    -1.0)
    _fear_chejan = getattr(cfg, "market_fear_chejan", 140.0)
    if (_kospi_chg <= _fear_pct or _kosdaq_chg <= _fear_pct) and _eff_chejan < _fear_chejan:
        logger.debug("[BREAKOUT_GATE] %s(%s) 공포 장세 → 체결강도 기준 %.0f%% → %.0f%%",
                     snap.name, snap.code, _eff_chejan, _fear_chejan)
        _eff_chejan = _fear_chejan
    if snap.chejan_strength < _eff_chejan:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN",
            actual=snap.chejan_strength, threshold=_eff_chejan,
            reason=f"[{_slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {_eff_chejan:.0f}%",
        )
        return None

    # ⑤ 체결강도 상한 — 극과열 고점 차단
    # MORNING 슬롯: 갭업 후 체결강도 정상화 중인 종목 허용 (950%), 나머지 슬롯: 800%
    if _slot == "MORNING":
        _chejan_max = getattr(cfg, "breakout_chejan_max_morning", 950.0)
    else:
        _chejan_max = getattr(cfg, "breakout_chejan_max", 800.0)
    if snap.chejan_strength >= _chejan_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_CHEJAN_MAX",
            actual=snap.chejan_strength, threshold=_chejan_max,
            reason=f"[{_slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {_chejan_max:.0f}%",
        )
        return None

    # ⑥ RSI 상한 — 과매수 고점 차단 (snap.rsi > 0 인 경우만 적용)
    _rsi_max = getattr(cfg, "breakout_rsi_max", 80.0)
    if snap.rsi > 0 and snap.rsi >= _rsi_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "BREAKOUT_RSI_MAX",
            actual=snap.rsi, threshold=_rsi_max,
            reason=f"[{_slot}] RSI 과매수 차단 — {snap.rsi:.1f} ≥ {_rsi_max:.1f}",
        )
        return None

    return f"[{_slot}] 체결강도 {snap.chejan_strength:.0f}% | 등락률 {_snap_chg:.1f}%"


def _resolve_time_slot(now: "dtime", cfg: SmartScannerConfig) -> str:
    """
    현재 시각을 기준으로 매매 시간 슬롯 문자열을 반환한다.

    Returns:
        "PRE"       — 08:00 ~ 09:00 (시간외 단일가, 캔들 없음)
        "OPENING"   — 09:00 ~ 09:30 (장 초반, MA정배열 미확인 구간)
        "MORNING"   — 09:30 ~ 11:00 (핵심 오전, 표준 기준)
        "MIDDAY"    — 11:00 ~ 13:00 (점심, 중간 강화)
        "AFTERNOON" — 13:00 ~ 14:30 (오후, 고점 차단)
    """
    pre_end = getattr(cfg, "pre_market_end", dtime(9, 0, 0))
    if now < pre_end:
        return "PRE"
    if now < cfg.ma_alignment_time:
        return "OPENING"
    if now < cfg.slot_morning_end:
        return "MORNING"
    if now < cfg.slot_midday_end:
        return "MIDDAY"
    return "AFTERNOON"


def _get_slot_value(slot: str, cfg: SmartScannerConfig, param_base: str, fallback: float) -> float:
    """
    슬롯과 파라미터 기본명으로 구간별 값을 반환한다.

    예) param_base="max_change_pct", slot="AFTERNOON"
        → cfg.max_change_pct_afternoon (없으면 fallback)
    """
    return float(getattr(cfg, f"{param_base}_{slot.lower()}", fallback))


def check_pre_surge(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    PRE_SURGE — 08:00~09:00 시간외 단일가 구간.

    캔들 데이터 없이 등락률·체결강도·거래량만으로 판단한다.
    주문은 09:00 단일가 일괄체결됨을 유의.

    통과 조건:
      ① 지수 차단 없음 (index_block_pct 초과)
      ② pre_surge_chg_min ≤ 등락률 < pre_surge_chg_max
      ③ 체결강도 ≥ pre_surge_chejan_min
      ④ 거래량 > 0
    """
    _block_pct  = getattr(cfg, "index_block_pct",   -1.5)
    _kospi_chg  = getattr(cfg, "kospi_chg_pct",      0.0)
    _kosdaq_chg = getattr(cfg, "kosdaq_chg_pct",     0.0)
    if _kospi_chg <= _block_pct:
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE",
            f"코스피 하락 차단 — {_kospi_chg:+.2f}% ≤ {_block_pct:.1f}%")
        return None
    if _kosdaq_chg <= _block_pct:
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE",
            f"코스닥 하락 차단 — {_kosdaq_chg:+.2f}% ≤ {_block_pct:.1f}%")
        return None

    chg     = float(snap.change_pct or 0)
    chg_min = getattr(cfg, "pre_surge_chg_min",  2.0)
    chg_max = getattr(cfg, "pre_surge_chg_max", 20.0)
    if not (chg_min <= chg < chg_max):
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE",
            f"등락률 범위 미충족 — {chg:+.2f}% (기준 {chg_min:.1f}%~{chg_max:.1f}%)")
        return None

    chejan_min = getattr(cfg, "pre_surge_chejan_min", 110.0)
    if snap.chejan_strength < chejan_min:
        ScannerLogger.near_miss(
            snap.code, snap.name, "PRE_SURGE",
            actual=snap.chejan_strength, threshold=chejan_min,
            reason=f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%",
        )
        return None

    # ⑤ 체결강도 상한 — 이미 극단 과열(고점) 종목 차단
    chejan_max = getattr(cfg, "pre_surge_chejan_max", 700.0)
    if snap.chejan_strength >= chejan_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "PRE_SURGE",
            actual=snap.chejan_strength, threshold=chejan_max,
            reason=f"체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {chejan_max:.0f}%",
        )
        return None

    # ⑥ RSI 상한 — 과매수 구간(고점) 진입 차단 (RSI=0 은 미계산이므로 스킵)
    rsi_max = getattr(cfg, "pre_surge_rsi_max", 88.0)
    if snap.rsi > 0 and snap.rsi >= rsi_max:
        ScannerLogger.near_miss(
            snap.code, snap.name, "PRE_SURGE",
            actual=snap.rsi, threshold=rsi_max,
            reason=f"RSI 과매수 차단 — {snap.rsi:.1f} ≥ {rsi_max:.1f}",
        )
        return None

    if snap.volume <= 0:
        ScannerLogger.rejected(snap.code, snap.name, "PRE_SURGE", "거래량 없음")
        return None

    return (
        f"PRE_SURGE 시간외 등락 {chg:+.2f}% "
        f"/ 체결강도 {snap.chejan_strength:.0f}% "
        f"/ 거래량 {snap.volume:,}"
    )


def check_opening_surge(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    OPENING_SURGE — 09:00~09:16 정규장 초반 (1분봉 < 8개).

    MA/RSI 데이터 부족 구간에서 등락률·체결강도·거래량으로 빠르게 판단한다.
    entry_open_surge_max_opening(기본 7%)으로 고점 진입 방지.

    통과 조건:
      ① 지수 차단 없음
      ② 시가 대비 상승 < entry_open_surge_max_opening
      ③ opening_surge_chg_min ≤ 등락률 < max_change_pct_opening
      ④ 체결강도 ≥ opening_surge_chejan_min
      ⑤ 최근 1분 거래량 ≥ 직전 평균 × opening_surge_vol_mult (데이터 있을 때만)
    """
    _block_pct  = getattr(cfg, "index_block_pct",   -1.5)
    _kospi_chg  = getattr(cfg, "kospi_chg_pct",      0.0)
    _kosdaq_chg = getattr(cfg, "kosdaq_chg_pct",     0.0)
    if _kospi_chg <= _block_pct:
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"코스피 하락 차단 — {_kospi_chg:+.2f}%")
        return None
    if _kosdaq_chg <= _block_pct:
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"코스닥 하락 차단 — {_kosdaq_chg:+.2f}%")
        return None

    # 시가 대비 상승 상한 (OPENING 전용 완화값)
    surge_max = getattr(cfg, "entry_open_surge_max_opening",
                        getattr(cfg, "entry_open_surge_max", 7.0))
    if snap.open_price > 0:
        surge_from_open = (snap.current_price - snap.open_price) / snap.open_price * 100
        if surge_from_open >= surge_max:
            ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
                f"시가 대비 이미 {surge_from_open:.2f}% 상승 ≥ 상한 {surge_max:.1f}%")
            return None

    chg     = float(snap.change_pct or 0)
    chg_min = getattr(cfg, "opening_surge_chg_min", 1.0)
    chg_max = getattr(cfg, "max_change_pct_opening", getattr(cfg, "max_change_pct", 20.0))
    if not (chg_min <= chg < chg_max):
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"등락률 범위 미충족 — {chg:+.2f}% (기준 {chg_min:.1f}%~{chg_max:.1f}%)")
        return None

    chejan_min = getattr(cfg, "opening_surge_chejan_min", 120.0)
    if snap.chejan_strength < chejan_min:
        ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
            f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%")
        return None

    # 거래량 급증 체크 (분봉 데이터 2개 이상일 때만)
    vol_mult = getattr(cfg, "opening_surge_vol_mult", 1.2)
    vols = list(snap.volumes_1min) if snap.volumes_1min else []
    if len(vols) >= 2:
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
        if avg_vol > 0 and vols[-1] < avg_vol * vol_mult:
            ScannerLogger.rejected(snap.code, snap.name, "OPENING_SURGE",
                f"거래량 미달 — {vols[-1]:,} < 평균 {avg_vol:,.0f} × {vol_mult:.1f}")
            return None

    return (
        f"OPENING_SURGE 등락 {chg:+.2f}% "
        f"/ 체결강도 {snap.chejan_strength:.0f}% "
        f"/ 거래량 {snap.volume:,}"
    )


def check_opening_scalp(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    Phase 1 모닝 스캘핑 진입 신호 (09:00~09:30).

    PRE_SURGE 신호가 발생한 종목을 장 시작 후 추적 매수한다.
    MA 데이터가 충분하지 않은 구간이므로 조건을 단순화한다.

    진입 조건:
      1. 1분봉 ≥ phase1_min_candles (기본 3개, ≈09:03 이후)
      2. 현재가 ≥ 시가 (갭업 방향 유지)
      3. 시가 대비 상승 ≤ phase1_open_rise_max (기본 8%, 이미 너무 오른 종목 차단)
      4. 체결강도 phase1_chejan_min ~ phase1_chejan_max 범위
      5. 전일 대비 등락률 ≤ phase1_change_pct_max (기본 15%)
    """
    # ① 1분봉 최소 개수 — 데이터 안정화 대기
    min_candles = int(getattr(cfg, "phase1_min_candles", 3))
    if len(snap.closes_1min) < min_candles:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CANDLES",
            f"1분봉 {len(snap.closes_1min)}개 < 최소 {min_candles}개 — 대기 중")
        return None

    # ② 시가 방향 확인 (현재가 ≥ 시가)
    if snap.open_price > 0 and snap.current_price < snap.open_price:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_DIRECTION",
            f"시가 하방 — 현재가 {snap.current_price:,} < 시가 {snap.open_price:,}")
        return None

    # ③ 시가 대비 상승 상한
    open_rise_max = float(getattr(cfg, "phase1_open_rise_max", 8.0))
    if snap.open_price > 0:
        open_rise = (snap.current_price - snap.open_price) / snap.open_price * 100
        if open_rise > open_rise_max:
            ScannerLogger.rejected(snap.code, snap.name, "SCALP_OPEN_RISE",
                f"시가 대비 {open_rise:.1f}% 상승 > 상한 {open_rise_max:.1f}% — 고점 차단")
            return None
    else:
        open_rise = 0.0

    # ④ 체결강도 범위
    chejan_min = float(getattr(cfg, "phase1_chejan_min", 120.0))
    chejan_max = float(getattr(cfg, "phase1_chejan_max", 700.0))
    if snap.chejan_strength < chejan_min:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHEJAN",
            f"체결강도 미달 — {snap.chejan_strength:.0f}% < {chejan_min:.0f}%")
        return None
    if snap.chejan_strength >= chejan_max:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHEJAN",
            f"체결강도 과열 — {snap.chejan_strength:.0f}% ≥ {chejan_max:.0f}%")
        return None

    # ⑤ 전일 대비 등락률 상한
    chg_max = float(getattr(cfg, "phase1_change_pct_max", 15.0))
    if snap.change_pct > chg_max:
        ScannerLogger.rejected(snap.code, snap.name, "SCALP_CHANGE",
            f"등락률 {snap.change_pct:.1f}% > 상한 {chg_max:.1f}%")
        return None

    reason = (
        f"[SCALP] PRE_SURGE 추적 진입 — 시가 대비 +{open_rise:.1f}%"
        f" | 체결강도 {snap.chejan_strength:.0f}%"
        f" | 등락률 {snap.change_pct:+.1f}%"
        f" | 1분봉 {len(snap.closes_1min)}개"
    )
    ScannerLogger.passed(snap.code, snap.name, "OPENING_SCALP", reason)
    return reason


def check_eod_entry(
    snap: "StockSnapshot",
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    종가매매(EOD) 진입 신호 판단.

    진입 조건:
      1. overnight_mode_enabled = True
      2. 현재 시각이 eod_entry_start ~ eod_entry_end (기본 14:40~14:55)
      3. 일봉 20MA 상방 (현재가 ≥ daily_ma20)
      4. 25일 신고가 근처 (current_price ≥ high_25d × (1 - threshold%))
      5. 일봉 정배열 (MA5 > MA10 > MA20)
      6. 당일 등락률 eod_change_pct_min ~ eod_change_pct_max (기본 +2% ~ +10%)
      7. 체결강도 ≥ eod_strength_min (기본 115%)
      8. 거래량 ≥ 전일 평균 × eod_volume_ratio_min (기본 1.5배)

    Returns:
        신호 이유 문자열 (통과) 또는 None (차단)
    """
    if not getattr(cfg, "overnight_mode_enabled", False):
        return None

    now = datetime.now().time()
    _start = getattr(cfg, "eod_entry_start", dtime(14, 40, 0))
    _end   = getattr(cfg, "eod_entry_end",   dtime(14, 55, 0))
    if not (_start <= now < _end):
        return None

    from strategy.jang_dong_min import get_daily_context, check_daily_alignment

    # ① 일봉 20MA 상방 + 신고가 근처
    _near_thr = float(getattr(cfg, "eod_near_high_threshold_pct", 3.0))
    _dctx = get_daily_context(snap.daily_closes, snap.current_price, _near_thr)

    if not _dctx["above_ma20"] and _dctx["daily_ma20"] > 0:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_MA20",
            f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {_dctx['daily_ma20']:,.0f}",
        )
        return None

    if not _dctx["near_high"]:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_NEAR_HIGH",
            f"25일 신고가 근처 아님 — 현재가 {snap.current_price:,}, "
            f"25일고가 {_dctx['high_25d']:,.0f} (기준 -{_near_thr:.1f}%)",
        )
        return None

    # ② 일봉 정배열
    if not check_daily_alignment(snap.daily_closes):
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_ALIGN",
            f"일봉 정배열 미충족 (5MA > 10MA > 20MA)",
        )
        return None

    # ②-b 분봉 추세 강도 (Medium 이상 — 종가 직전까지 추세 유지 확인)
    _eod_min_trend = int(getattr(cfg, "eod_min_trend_level", 2))
    _trend_lv = int(getattr(snap, "trend_level", 0))
    if _trend_lv < _eod_min_trend:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_TREND",
            f"분봉 추세 미달 — level {_trend_lv} < {_eod_min_trend} (Medium 이상 필요)",
        )
        return None

    # ③ 당일 등락률
    _chg_min = float(getattr(cfg, "eod_change_pct_min", 2.0))
    _chg_max = float(getattr(cfg, "eod_change_pct_max", 10.0))
    chg = snap.change_pct
    if not (_chg_min <= chg <= _chg_max):
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_CHANGE",
            f"등락률 {chg:+.2f}% 범위 밖 (기준 +{_chg_min:.1f}% ~ +{_chg_max:.1f}%)",
        )
        return None

    # ④ 체결강도
    _str_min = float(getattr(cfg, "eod_strength_min", 115.0))
    if snap.chejan_strength < _str_min:
        ScannerLogger.rejected(
            snap.code, snap.name, "EOD_STRENGTH",
            f"체결강도 {snap.chejan_strength:.1f}% < 기준 {_str_min:.0f}%",
        )
        return None

    # ⑤ 거래량 (당일 1분봉 평균 대비 배수 — 최근 10분 기준)
    _vol_ratio = float(getattr(cfg, "eod_volume_ratio_min", 1.5))
    _vols = snap.volumes_1min
    if _vols and len(_vols) >= 10:
        _avg_vol_1min = sum(_vols[-10:]) / 10.0
        _cur_vol_1min = _vols[-1] if _vols else 0
        if _avg_vol_1min > 0 and _cur_vol_1min < _avg_vol_1min * _vol_ratio:
            ScannerLogger.rejected(
                snap.code, snap.name, "EOD_VOLUME",
                f"최근 1분봉 거래량 {_cur_vol_1min:,} < 10분평균 {_avg_vol_1min:,.0f} × {_vol_ratio:.1f}배",
            )
            return None

    reason = (
        f"[EOD] 종가매매 진입 — 등락률 {chg:+.2f}% | 체결강도 {snap.chejan_strength:.1f}% "
        f"| 25일신고가 {_dctx['high_25d']:,.0f}원 근처 | 일봉정배열↑ "
        f"| 20MA {_dctx['daily_ma20']:,.0f}원 상방"
    )
    ScannerLogger.passed(snap.code, snap.name, "EOD_ENTRY", reason)
    return reason


def check_jdm_entry(
    snap: StockSnapshot,
    cfg:  SmartScannerConfig,
) -> Optional[str]:
    """
    JDM_ENTRY 통합 게이트 (ScannerWorker / SmartScanner._evaluate 공통).

    ① 지수 등락률 차단 — 코스피/코스닥 중 하나라도 index_block_pct 이하면 즉시 차단
    ② 진입 허용 시각(entry_start~entry_end) — 오후 저유동 구간 등 배제
    ③ 직전 5분 평균 대비 분봉 거래량 volume_surge_mult 배 이상
    ④ 체결강도 min_chejan_strength% 이상
    ⑤ MA 골든크로스 + 단·장기 이격 jdm_min_ma_spread_abs 원 이상
    ⑥ RSI ∈ [jdm_rsi_entry_min, jdm_rsi_high)
    """
    # [NEW] 2026-04-07 지수 등락률 차단 — 시장 전체 하락 시 신규 진입 금지
    _block_pct   = getattr(cfg, "index_block_pct",  -1.5)
    _kospi_chg   = getattr(cfg, "kospi_chg_pct",     0.0)
    _kosdaq_chg  = getattr(cfg, "kosdaq_chg_pct",    0.0)
    if _kospi_chg <= _block_pct:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_INDEX",
            f"코스피 하락 차단 — {_kospi_chg:+.2f}% ≤ {_block_pct:.1f}%",
        )
        return None
    if _kosdaq_chg <= _block_pct:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_INDEX",
            f"코스닥 하락 차단 — {_kosdaq_chg:+.2f}% ≤ {_block_pct:.1f}%",
        )
        return None

    # [NEW] 2026-04-03 수급 절대치 필터 — 소외주 거르기
    # 조건 A: 거래대금 상위 50위 이내 (rank 우선)
    # 조건 B: OR 누적 거래대금 300억 이상
    if hasattr(cfg, 'min_daily_rank') and cfg.min_daily_rank:
        rank = snap.rank if hasattr(snap, 'rank') else None
        amt = snap.trade_amount if hasattr(snap, 'trade_amount') else 0

        # rank가 있으면 rank 체크, 없으면 거래대금 체크
        if rank is not None and rank > 0 and rank <= cfg.min_daily_rank:
            pass  # 상위 N위 이내면 OK
        elif amt >= cfg.min_trade_amount:
            pass  # 거래대금 300억 이상이면 OK
        else:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_LIQUIDITY",
                f"수급 부족 (rank={rank if rank else 'N/A'}, 거래대금={amt/1e9:.1f}억 < 최소 {cfg.min_trade_amount/1e9:.0f}억)",
            )
            return None

    # [고점 방지] 현재가가 시가 대비 entry_open_surge_max% 이상 이미 올랐으면 진입 차단
    # 단, 추세 레벨 ≥ surge_trend_override_level 이면 surge_trend_max_pct까지 허용
    if snap.open_price > 0:
        surge_from_open = (snap.current_price - snap.open_price) / snap.open_price * 100
        _surge_cap = float(cfg.entry_open_surge_max)
        _surge_override_lvl = int(getattr(cfg, "surge_trend_override_level", 2))
        _surge_trend_max    = float(getattr(cfg, "surge_trend_max_pct", 15.0))
        _snap_trend_lvl     = int(getattr(snap, "trend_level", 0))
        if _surge_override_lvl > 0 and _snap_trend_lvl >= _surge_override_lvl:
            _surge_cap = max(_surge_cap, _surge_trend_max)
        if surge_from_open >= _surge_cap:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_SURGE",
                f"시가 대비 이미 {surge_from_open:.2f}% 상승 — 고점 진입 차단 "
                f"(상한 {_surge_cap:.1f}%, trend={_snap_trend_lvl})",
            )
            return None

    now = datetime.now().time()
    if not (cfg.entry_start_time <= now <= cfg.entry_end_time):
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_TIME",
            f"진입 허용 시간 아님 ({cfg.entry_start_time}~{cfg.entry_end_time})",
        )
        return None

    # [NEW] 시간대 슬롯 resolve — 구간별 기준값 동적 선택 (2026-04-08)
    _slot         = _resolve_time_slot(now, cfg)
    _eff_ch_max   = _get_slot_value(_slot, cfg, "max_change_pct",       cfg.max_change_pct)
    _eff_chejan   = _get_slot_value(_slot, cfg, "min_chejan_strength",   cfg.min_chejan_strength)
    _eff_vol_mult = _get_slot_value(_slot, cfg, "volume_surge_mult",     cfg.volume_1min_surge_mult)
    _eff_rsi_min  = _get_slot_value(_slot, cfg, "jdm_rsi_entry_min",     cfg.jdm_rsi_entry_min)

    # 추세추종 오버라이드 — trend_level ≥ jdm_candle_skip_trend_level 이면 RSI 하한 완화
    _trend_snap_lv = int(getattr(snap, "trend_level", 0))
    _candle_skip_lv = int(getattr(cfg, "jdm_candle_skip_trend_level", 2))
    if _trend_snap_lv >= _candle_skip_lv:
        _rsi_trend_min = float(getattr(cfg, "jdm_rsi_entry_min_trend", 45.0))
        if _rsi_trend_min < _eff_rsi_min:
            _eff_rsi_min = _rsi_trend_min

    # [NEW] 스코어링 시스템 — 대장주/거래량 폭발 시 조건 추가 완화
    _eff_ma_spread = float(getattr(cfg, "jdm_ma_spread_pct", 0.15))
    _scoring_bonus = False
    
    _rank = snap.rank if hasattr(snap, 'rank') else None
    _rank_bonus = int(getattr(cfg, "scoring_rank_bonus", 10))
    if _rank is not None and _rank > 0 and _rank <= _rank_bonus:
        _scoring_bonus = True

    _surge_lookback = int(getattr(cfg, "volume_surge_lookback", 10))
    _vol_bonus_mult = float(getattr(cfg, "scoring_vol_surge_bonus", 2.0))
    if snap.volumes_1min and len(snap.volumes_1min) >= _surge_lookback + 1:
        _avg_vol = sum(snap.volumes_1min[-(_surge_lookback+1):-1]) / _surge_lookback
        _cur_vol = snap.volumes_1min[-1]
        if _avg_vol > 0 and (_cur_vol / _avg_vol) >= _vol_bonus_mult:
            _scoring_bonus = True

    if _scoring_bonus:
        _eff_rsi_min = min(_eff_rsi_min, 40.0)  # 스코어링: RSI 하한 40.0% 로 완화
        _eff_ma_spread = min(_eff_ma_spread, 0.10) # 스코어링: 이격도 0.10% 로 완화

    # ── Safety Filter ① 공포 장세 체결강도 상향 ────────────────────────────
    # 지수가 market_fear_pct(기본 -1%) 이하로 하락 중이면 체결강도 기준을 강화.
    # index_block_pct(-1.5%)까지는 차단하지 않고 조건만 더 까다롭게 적용.
    _fear_pct    = getattr(cfg, "market_fear_pct",    -1.0)
    _fear_chejan = getattr(cfg, "market_fear_chejan", 140.0)
    _is_fear = (_kospi_chg <= _fear_pct) or (_kosdaq_chg <= _fear_pct)
    if _is_fear and _eff_chejan < _fear_chejan:
        _prev_chejan = _eff_chejan
        _eff_chejan  = _fear_chejan
        # 차단은 하지 않고 기준 상향만 — 이후 체결강도 체크에서 판정
        # FEAR 상황 자체는 INFO 로 기록해 튜닝 데이터에 포함
        scan_log.info(
            "FEAR\t%s\t%s\tJDM_FEAR\t공포 장세 감지 (코스피 %+.2f%% / 코스닥 %+.2f%%) "
            "→ 체결강도 기준 %.0f%% → %.0f%% 상향",
            snap.code, snap.name, _kospi_chg, _kosdaq_chg, _prev_chejan, _eff_chejan,
        )

    # 구간별 등락률 상한 체크 (prefilter 이후 2차 보호)
    # 단, 추세 레벨 ≥ surge_trend_override_level 이면 surge_trend_max_pct까지 완화
    _snap_chg = float(getattr(snap, "change_pct", 0) or 0)
    _chg_cap = float(_eff_ch_max)
    _surge_override_lvl2 = int(getattr(cfg, "surge_trend_override_level", 2))
    _surge_trend_max2    = float(getattr(cfg, "surge_trend_max_pct", 15.0))
    _snap_trend_lvl2     = int(getattr(snap, "trend_level", 0))
    if _surge_override_lvl2 > 0 and _snap_trend_lvl2 >= _surge_override_lvl2:
        _chg_cap = max(_chg_cap, _surge_trend_max2)
    if _snap_chg >= _chg_cap:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_CHGPCT",
            f"[{_slot}] 등락률 {_snap_chg:.2f}% ≥ 구간 상한 {_chg_cap:.0f}% (trend={_snap_trend_lvl2})",
        )
        return None

    # PRE 슬롯은 check_pre_surge가 담당 — JDM은 처리하지 않음
    if _slot == "PRE":
        return None

    closes     = snap.closes_1min
    need_long  = cfg.jdm_ma_long + 1    # 16 — 풀 JDM 최소 캔들
    need_short = cfg.jdm_ma_short + 1   # 8  — MA7 라이트 모드 최소 캔들

    # OPENING 슬롯에서 캔들 8개 이상 16개 미만 → MA7 라이트 모드
    _lite_mode = (
        _slot == "OPENING"
        and need_short <= len(closes) < need_long
    )
    need = need_short if _lite_mode else need_long

    if len(closes) < need:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM",
            f"1분봉 데이터 부족 ({len(closes)}/{need}"
            + (" [OPENING_LITE 대기]" if _slot == "OPENING" else "") + ")",
        )
        return None

    # [NEW] 슬리피지 방지 — 직전 1분봉 종가 대비 현재 1분봉 3% 이상 급등 시 진입 유보
    if len(closes) >= 2 and closes[-2] > 0:
        slip_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        _slip_max = getattr(cfg, "slippage_block_pct", 3.0)
        if slip_pct >= _slip_max:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_SLIP",
                f"슬리피지 차단 — 직전 1분봉 대비 {slip_pct:.2f}% 급등 (상한 {_slip_max:.1f}%)",
            )
            return None

    # [우선순위 변경] 거래량 및 체결강도 체크 로직은 MA(정배열/이격도) 평가 이후로 이동되었습니다.

    from strategy.jang_dong_min import (
        calc_atr, calc_ema, calc_ma, calc_pivot_r2, calc_rsi, check_daily_alignment
    )

    # ── 요셉 시그널 추세 필터 ────────────────────────────────────────────────
    if getattr(cfg, "yosep_trend_enabled", True):
        # 슬롯별 최소 추세 레벨 — OPENING은 1분봉 부족으로 level 0이 정상이므로 별도 기준
        if _slot == "AFTERNOON":
            _min_trend = int(getattr(cfg, "yosep_min_trend_level_afternoon", 3))
        elif _slot == "OPENING":
            _min_trend = int(getattr(cfg, "yosep_min_trend_level_opening", 0))
        else:
            _min_trend = int(getattr(cfg, "yosep_min_trend_level", 1))
        _trend_lv = int(getattr(snap, "trend_level", 0))
        if _trend_lv < _min_trend:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_TREND",
                f"요셉 추세 미달 [{_slot}] — level {_trend_lv} < {_min_trend}",
            )
            return None

        closes = list(snap.closes_1min or [])
        highs = list(snap.highs_1min or [])
        lows = list(snap.lows_1min or [])
        _ema_p = int(getattr(cfg, "yosep_ema_period", 20))
        _atr_p = int(getattr(cfg, "yosep_atr_period", 14))
        _down_mult = float(getattr(cfg, "yosep_downtrend_block_atr", 0.8))
        if len(closes) >= _ema_p and len(highs) >= _atr_p + 1 and len(lows) >= _atr_p + 1:
            ema20 = calc_ema(closes, _ema_p)
            atr14 = calc_atr(highs, lows, closes, _atr_p)
            if ema20 is not None and atr14 is not None and atr14 > 0:
                if snap.current_price < (ema20 - atr14 * _down_mult):
                    ScannerLogger.rejected(
                        snap.code, snap.name, "JDM_TREND_DOWN",
                        f"하락 추세 강세 — 현재가 {snap.current_price:,} < EMA{_ema_p} {ema20:,.0f} - ATR{_atr_p}×{_down_mult:.1f}",
                    )
                    return None

    if _lite_mode:
        # ── OPENING 라이트 모드 (09:08~09:16, MA7만 사용) ──────────────────
        # MA15 불가 → MA7 방향성 + 현재가>MA7 로 대체
        ma_s  = calc_ma(closes,      cfg.jdm_ma_short)
        pma_s = calc_ma(closes[:-1], cfg.jdm_ma_short)
        if ma_s is None or pma_s is None:
            return None
        # MA7이 상승 중이고 현재가가 MA7 위에 있어야 함
        if not (ma_s > pma_s and snap.current_price > ma_s):
            ScannerLogger.rejected(snap.code, snap.name, "JDM_LITE",
                f"MA{cfg.jdm_ma_short} 상승 미충족 — "
                f"이전 {pma_s:.0f}→현재 {ma_s:.0f}, 현재가 {snap.current_price:,}")
            return None
        spread_tag = f"MA{cfg.jdm_ma_short}↑ {pma_s:.0f}→{ma_s:.0f}"
        rsi_tag    = ""  # RSI14는 15캔들 이상 필요 — 라이트 모드에서는 스킵
    else:
        # ── 풀 JDM 모드 (캔들 16개 이상) ────────────────────────────────────
        ma_s  = calc_ma(closes,      cfg.jdm_ma_short)
        ma_l  = calc_ma(closes,      cfg.jdm_ma_long)
        rsi   = calc_rsi(closes,     14)
        pma_s = calc_ma(closes[:-1], cfg.jdm_ma_short)
        pma_l = calc_ma(closes[:-1], cfg.jdm_ma_long)

        if any(v is None for v in [ma_s, ma_l, rsi, pma_s, pma_l]):
            return None

        golden = pma_s <= pma_l and ma_s > ma_l
        _gc_override_lvl = int(getattr(cfg, "jdm_golden_cross_trend_override", 2))
        _snap_trend_lv   = int(getattr(snap, "trend_level", 0))
        if not golden:
            # 추세 오버라이드: trend_level ≥ jdm_golden_cross_trend_override AND MA 정배열이면 허용
            # 이미 상승 중인 종목은 단기MA가 장기MA 위에 유지되므로 골든크로스 미발생이 정상
            if _gc_override_lvl > 0 and _snap_trend_lv >= _gc_override_lvl and ma_s > ma_l:
                ScannerLogger.passed(snap.code, snap.name, "JDM_GC_OVERRIDE",
                    f"골든크로스 없지만 추세Lv{_snap_trend_lv}+MA정배열 진입 허용 "
                    f"(직전{pma_s:.0f}/{pma_l:.0f}→현재{ma_s:.0f}/{ma_l:.0f})")
            else:
                ScannerLogger.rejected(snap.code, snap.name, "JDM",
                    f"골든크로스 미충족 (직전MA:{pma_s:.0f}/{pma_l:.0f} → 현재MA:{ma_s:.0f}/{ma_l:.0f})")
                return None

        # 09:30 이후엔 MA 정배열 유지 확인 (추세 오버라이드 종목도 정배열 필요)
        if now >= cfg.ma_alignment_time:
            if not (ma_s > ma_l):
                ScannerLogger.rejected(snap.code, snap.name, "JDM",
                    f"MA 정배열 미충족(09:30+) — MA{cfg.jdm_ma_short}:{ma_s:.0f} ≤ MA{cfg.jdm_ma_long}:{ma_l:.0f}")
                return None

        # MA 이격 체크
        spread_abs = float(ma_s) - float(ma_l)
        spread_pct = (spread_abs / float(ma_l) * 100) if float(ma_l) > 0 else 0
        
        # Whipsaw 방지: MA7이 MA15보다 명확히 위에 있는지(정배열) 명시적 재확인
        if ma_s <= ma_l:
             ScannerLogger.rejected(snap.code, snap.name, "JDM",
                 f"MA 정배열 미충족 (Whipsaw 방지) — MA{cfg.jdm_ma_short}:{ma_s:.0f} <= MA{cfg.jdm_ma_long}:{ma_l:.0f}")
             return None

        if spread_pct < _eff_ma_spread:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM",
                f"MA 이격 부족 ({spread_pct:.2f}% < 최소 {_eff_ma_spread:.2f}%" + (" [Scoring Bonus]" if _scoring_bonus else "") + ")",
            )
            return None
        if spread_pct > float(cfg.jdm_ma_spread_max_pct):
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM",
                f"MA 이격 과열 ({spread_pct:.2f}% > 상한 {cfg.jdm_ma_spread_max_pct:.1f}%)",
            )
            return None
        spread_tag = f"MA{cfg.jdm_ma_short}/{cfg.jdm_ma_long} {ma_s:.0f}/{ma_l:.0f} ({spread_pct:.2f}%)"
        rsi_tag    = f"RSI{rsi:.0f}"

    # ── 거래량 및 체결강도 체크 (MA 평가 후 진행) ────────────────────────────────────
    r_vol = check_volume_surge(snap, _eff_vol_mult, getattr(cfg, "volume_surge_lookback", 10))
    if r_vol is None:
        return None

    # ── [NEW] 체결 가속도(Execution Velocity) 필터 ─────────────────────────────────
    # 10초 체결량 ≥ 직전 1분 평균 10초량의 N배 — '지금 막 불붙은' 종목만 통과
    # exec_velocity_ratio=0은 데이터 부족(장 초반) → 필터 스킵(fail-open)
    # OPENING 슬롯에서 exec_velocity_disabled_opening=True면 필터 비활성화
    _skip_exec_vel = (
        _slot == "OPENING" and
        getattr(cfg, "exec_velocity_disabled_opening", False)
    )
    if getattr(cfg, "exec_velocity_enabled", True) and not _skip_exec_vel:
        _vel_mult = float(getattr(cfg, "exec_velocity_mult", 1.8))
        if snap.exec_velocity_ratio > 0 and snap.exec_velocity_ratio < _vel_mult:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_EXEC_VEL",
                f"[{_slot}] 체결 가속도 미달 — {snap.exec_velocity_ratio:.2f}배 < {_vel_mult:.1f}배 "
                f"(10초 체결 가속 부족, 지지부진 종목 차단)",
            )
            return None
        elif snap.exec_velocity_ratio > 0:
            logger.debug(
                "[EXEC_VEL 통과] %s(%s) ratio=%.2f ≥ %.1f배",
                snap.code, snap.name, snap.exec_velocity_ratio, _vel_mult,
            )
    elif _skip_exec_vel and snap.exec_velocity_ratio > 0:
        logger.debug(
            "[EXEC_VEL SKIPPED] %s(%s) OPENING 슬롯 비활성화 — ratio=%.2f (필터 우회)",
            snap.code, snap.name, snap.exec_velocity_ratio,
        )

    r_chej = check_chejan_strength(snap, _eff_chejan)
    if r_chej is None:
        ScannerLogger.near_miss(
            snap.code, snap.name, "JDM_CHEJAN",
            actual=snap.chejan_strength, threshold=_eff_chejan,
            reason=f"[{_slot}] 체결강도 미달 — {snap.chejan_strength:.0f}% < {_eff_chejan:.0f}%",
        )
        return None

    # OPENING 슬롯은 단일가 직후 체결강도가 900%+ 로 왜곡 — 별도 상한 적용
    if _slot == "OPENING":
        _jdm_chejan_max = float(getattr(cfg, "jdm_chejan_max_opening", 1200.0))
    else:
        _jdm_chejan_max = float(getattr(cfg, "jdm_chejan_max", 700.0))
    if snap.chejan_strength >= _jdm_chejan_max:
        ScannerLogger.rejected(
            snap.code, snap.name, "JDM_CHEJAN_MAX",
            f"[{_slot}] 체결강도 과열 차단 — {snap.chejan_strength:.0f}% ≥ {_jdm_chejan_max:.0f}%",
        )
        return None

    # ── Safety Filter ② EMA10/EMA20 이격 과열 (기존) + 현재가/EMA10 이격 과열 (신규) ──
    _ema_s_period      = getattr(cfg, "ema_disp_short",         10)
    _ema_l_period      = getattr(cfg, "ema_disp_long",          20)
    _ema_disp_max      = getattr(cfg, "ema_disp_max_pct",       3.0)
    _price_ema_disp_max = getattr(cfg, "price_ema_disp_max_pct", 3.0)
    # 추세추종 완화: trend_level ≥ 기준 레벨이면 EMA 이격 상한을 넓혀 추격 차단 해제
    if _trend_snap_lv >= _candle_skip_lv:
        _ema_disp_max       = float(getattr(cfg, "ema_disp_max_pct_trend",       7.0))
        _price_ema_disp_max = float(getattr(cfg, "price_ema_disp_max_pct_trend", 6.0))
    if len(closes) >= _ema_l_period:
        ema_s = calc_ema(closes, _ema_s_period)
        ema_l = calc_ema(closes, _ema_l_period)
        if ema_s is not None and ema_l is not None and ema_l > 0:
            # ② -A: EMA10/EMA20 이격 (추격매수 방지)
            ema_disp_pct = (ema_s - ema_l) / ema_l * 100
            if ema_disp_pct >= _ema_disp_max:
                ScannerLogger.rejected(
                    snap.code, snap.name, "JDM_EMA",
                    f"EMA10/EMA20 이격 과열 — {ema_disp_pct:.2f}% ≥ {_ema_disp_max:.1f}%",
                )
                return None
            # ② -B: 현재가/EMA10 이격 (추격매수 방지) — 단기 급등 포착
            if ema_s > 0:
                price_ema_disp = (snap.current_price - ema_s) / ema_s * 100
                if price_ema_disp >= _price_ema_disp_max:
                    ScannerLogger.rejected(
                        snap.code, snap.name, "JDM_PRICE_EMA",
                        f"현재가/EMA{_ema_s_period} 이격 과열 — {price_ema_disp:.2f}% ≥ {_price_ema_disp_max:.1f}% (현재가 {snap.current_price:,} / EMA{_ema_s_period} {ema_s:,.0f})",
                    )
                    return None

    # RSI 체크 — 라이트 모드(캔들 부족)에서는 스킵
    if not _lite_mode:
        # RSI 상한: trend_level ≥ 기준 레벨이면 80으로 완화 (추세주 70대 RSI 차단 방지)
        _eff_rsi_high = cfg.jdm_rsi_high  # 기본 70
        if _trend_snap_lv >= _candle_skip_lv:
            _eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_trend", 80.0))
            # ATR 돌파 확인: 현재가 > EMA20 + ATR×1.5 이면 추가 완화(82)
            _highs = list(snap.highs_1min or [])
            _lows  = list(snap.lows_1min  or [])
            if len(closes) >= 20 and len(_highs) >= 15 and len(_lows) >= 15:
                _ema20_b = calc_ema(closes, 20)
                _atr14_b = calc_atr(_highs, _lows, closes, 14)
                if (_ema20_b is not None and _atr14_b is not None and _atr14_b > 0
                        and snap.current_price > _ema20_b + _atr14_b * 1.5):
                    _eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_breakout", 82.0))
            # OPENING 슬롯 추세Lv3: 장초반 강세 종목 RSI 상한 95%로 완화
            if _slot == "OPENING" and _trend_snap_lv >= 3:
                _eff_rsi_high = float(getattr(cfg, "jdm_rsi_high_opening_trend3", 95.0))

        rsi_ok = _eff_rsi_min <= rsi < _eff_rsi_high
        if not rsi_ok:
            _rsi_thresh = _eff_rsi_min if rsi < _eff_rsi_min else _eff_rsi_high
            ScannerLogger.near_miss(
                snap.code, snap.name, "JDM_RSI",
                actual=rsi, threshold=_rsi_thresh,
                reason=f"[{_slot}] RSI 범위 초과 — 현재 {rsi:.1f}% (진입허용 {_eff_rsi_min:.0f}~{_eff_rsi_high:.0f}%, trend_lv={_trend_snap_lv})",
            )
            return None

    # [추세추종] 캔들 패턴 확인 — trend_level ≥ 기준 레벨이면 반전 캔들 불필요 (추세 계속 진행 중)
    # 라이트 모드(캔들 부족)에서도 스킵
    if not _lite_mode:
        if _trend_snap_lv >= _candle_skip_lv:
            # 추세주: MA정배열·ATR·EMA 이미 검증됨 → 반전 캔들 패턴 요구 불필요
            candle_reason = f"TREND_SKIP(lv{_trend_snap_lv})"
        else:
            r_engulf = check_bullish_engulfing(snap)
            r_pinbar = check_bullish_pin_bar(snap)
            if r_engulf is None and r_pinbar is None:
                ScannerLogger.rejected(
                    snap.code, snap.name, "JDM_CANDLE",
                    f"캔들 패턴 미충족 (상승장악형·강세핀바 불성립, trend_lv={_trend_snap_lv} < {_candle_skip_lv})",
                )
                return None
            candle_reason = r_engulf or r_pinbar
    else:
        candle_reason = "LITE(캔들패턴스킵)"

    # [NEW] 체결강도 최종 재확인 (슬롯 기반 기준값 적용)
    if snap.chejan_strength < _eff_chejan:
        ScannerLogger.near_miss(
            snap.code, snap.name, "JDM_CHEJAN_FINAL",
            actual=snap.chejan_strength, threshold=_eff_chejan,
            reason=f"[{_slot}] 체결강도 최종 재확인 미충족 — 현재 {snap.chejan_strength:.0f}% < {_eff_chejan:.0f}%",
        )
        return None

    # [NEW] 피봇 R2 돌파 확인 — 라이트 모드에서는 스킵
    if not _lite_mode and cfg.pivot_r2_enabled:
        r2 = calc_pivot_r2(snap.daily_high_prev, snap.daily_low_prev, snap.prev_close)
        if r2 > 0 and snap.current_price < r2:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_PIVOT",
                f"피봇 R2 미돌파 (현재가={snap.current_price:,} < R2={r2:,.0f})",
            )
            return None

    # [NEW] 일봉 정배열 확인 (5일 > 10일 > 20일)
    # 일봉 데이터 20개 미만(로드 전)이면 fail-open — 데이터 없다고 차단하지 않음
    if cfg.daily_alignment_enabled and len(snap.daily_closes) >= 20:
        if not check_daily_alignment(snap.daily_closes):
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_ALIGN",
                f"일봉 정배열 미충족 (5MA > 10MA > 20MA, 데이터={len(snap.daily_closes)}개)",
            )
            return None

    # [NEW] 일봉 20MA 가격 필터 — 현재가가 일봉 20일선 아래면 차단 (가짜 신호 여과)
    from strategy.jang_dong_min import get_daily_context as _get_daily_ctx
    _near_high_thr = float(getattr(cfg, "daily_near_high_threshold_pct", 3.0))
    _daily_ctx = _get_daily_ctx(snap.daily_closes, snap.current_price, _near_high_thr)
    if getattr(cfg, "daily_ma20_filter_enabled", True):
        if not _daily_ctx["above_ma20"] and _daily_ctx["daily_ma20"] > 0:
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_DAILY_MA20",
                f"일봉 20MA 하방 — 현재가 {snap.current_price:,} < 20MA {_daily_ctx['daily_ma20']:,.0f}",
            )
            return None

    # [추세추종] 일봉 20MA 우상향 필터 — 3일 기울기 음수면 하락 추세로 판단, 진입 차단
    if getattr(cfg, "daily_ma20_slope_enabled", True):
        if not _daily_ctx.get("ma20_slope_up", True):
            ScannerLogger.rejected(
                snap.code, snap.name, "JDM_MA20_SLOPE",
                f"일봉 20MA 기울기 하락 — 추세추종 진입 차단 (3일 기울기 음수, 20MA={_daily_ctx['daily_ma20']:,.0f})",
            )
            return None

    mode_tag = "JDM_LITE" if _lite_mode else "JDM"
    # 신고가 근처 정보를 reason에 포함 (ScanSignal 필드는 _build_jdm_signal에서 채움)
    _near_tag = " | 📈신고가근처(TP↑)" if _daily_ctx["near_high"] else ""
    reason = f"[{_slot}][{mode_tag}] {r_vol} | {r_chej} | {spread_tag} | {rsi_tag} | {candle_reason}{_near_tag}"
    ScannerLogger.passed(snap.code, snap.name, mode_tag, reason)
    return reason


# ---------------------------------------------------------------------------
# SmartScanner — 통합 오케스트레이터
# ---------------------------------------------------------------------------

class SmartScanner:
    """
    3단계 스마트 스캐너 (메모리 최적화 + 로그 + 터미널 뷰 통합).

    사용 예)
        scanner = SmartScanner(kiwoom)
        scanner.on_signal = lambda sig: order_module.execute(sig)
        scanner.start()
    """

    def __init__(self, kiwoom, cfg: Optional[SmartScannerConfig] = None) -> None:
        self._kiwoom = kiwoom
        self.cfg     = cfg or SmartScannerConfig()

        # ① DataFrame 캐시
        self.store   = SnapshotStore()

        # TR 요청 큐 — 키움 API 간격 보장
        self._tr_q   = TRRequestQueue()

        # 컴포넌트
        self.top_mgr = TopVolumeManager(
            top_n=max(self.cfg.collect_raw_top_n, self.cfg.watch_pool_max),
        )
        self.watch_q = PriorityWatchQueue(
            kiwoom,
            screen_no=self.cfg.screen_realtime,
            max_subs=self.cfg.realtime_sub_max,
        )

        # ③ 터미널 뷰
        self.display = ScannerDisplay(self.store, self.cfg)

        self._running     = False
        self._prefiltered = False
        self._scan_thread: Optional[threading.Thread] = None
        self._lock        = threading.Lock()

        # 첫 스캔 시 전체 종목 1분봉 일괄 로딩 플래그
        # True가 되면 이후 사이클은 12종목/사이클 제한으로 복귀
        self._initial_candle_load_done: bool = False

        self.on_signal:        Optional[Callable[[ScanSignal], None]] = None
        self.on_index_update:  Optional[Callable[[str, float, float], None]] = None  # (idx_code, current, chg_pct)

        # 포지션 현재가 실시간 업데이트용 (MainWindow에서 주입)
        self._order_mgr = None

        # watch_q.refresh 쓰로틀 — SetRealReg를 매 틱 호출 방지 (30초 간격)
        self._last_watchq_refresh: float = 0.0
        self._WATCHQ_INTERVAL: float = 30.0

        # [NEW] 일봉 데이터 갱신 쓰로틀 (2026-04-03)
        self._last_daily_update: float = 0.0
        self._daily_update_interval_sec: float = self.cfg.daily_candle_refresh_min * 60.0  # 분 → 초
        self._daily_refresh_pending: list = []   # MainWindow QTimer 체인이 소비할 갱신 대기 목록

        # 동적 감시 중단: 포지션 풀(max_positions)시 유니버스 감시를 보유종목만으로 축소
        self._universe_paused: bool = False

        # 캔들 마감 게이팅: 분이 바뀔 때만 _evaluate() 실행 (틱 기반 고점 진입 방지)
        self._eval_min: dict[str, int] = {}
        # WATCH 모드 예비 종목 갱신 주기 (스코어링 기반)
        self._last_reserve_refresh: float = 0.0
        self._RESERVE_INTERVAL: float = 10.0   # 10초마다 예비 top-2 재선정

        # 거래대금 '9시(장시작) 대비' 증가율 — 종목별 당일 최초 관측값(설정: pre_filter_time 이후·양수)을 기준
        self._amt_baseline_date: Optional[date] = None
        self._amt_baseline: dict[str, int] = {}
        # opt10030 직전 성공 결과 캐시 — 실패 시 하드코딩 대체 대신 이전 결과 재사용
        self._last_volume_rows: list[dict] = []
        self._last_volume_updated: float = 0.0  # 마지막 캐시 갱신 시각 (time.monotonic)
        self._opt10030_fetching: bool = False   # opt10030 중복 호출 방지 플래그
        # 전일 거래량 캐시 — hybrid universe score vol_ratio 계산용
        # {code: prev_volume}; 매일 장 마감(15:20) save_prev_volumes() 로 갱신
        self._prev_volumes: dict[str, int] = {}
        self._load_prev_volumes()
        # 동일 종목/신호 중복 emit 방지 (signal_cooldown_sec)
        self._last_signal_ts: dict[tuple[str, str], float] = {}

        self._connect_realtime_signal()

    # ── 전일 거래량 캐시 save/load ────────────────────────────────────────────

    def _prev_volumes_path(self) -> Path:
        return Path("logs") / "prev_volumes.json"

    def _load_prev_volumes(self) -> None:
        """
        logs/prev_volumes.json 에서 전일 거래량 캐시를 로드.
        파일에 저장된 날짜가 오늘이거나 4일 이상 지난 경우 사용하지 않는다.
        """
        # json 은 모듈 최상위에서 이미 임포트됨
        path = self._prev_volumes_path()
        if not path.exists():
            logger.info("[prev_volumes] 캐시 파일 없음 — vol_ratio 중립(0.5)으로 동작")
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            saved_date_str = data.get("date", "")
            saved_date = date.fromisoformat(saved_date_str) if saved_date_str else None
            today = date.today()
            if saved_date is None:
                logger.warning("[prev_volumes] 날짜 정보 없음 — 캐시 무효")
                return
            if saved_date >= today:
                logger.info("[prev_volumes] 저장 날짜(%s)가 오늘 이후 — 무효 (장중 저장본)", saved_date)
                return
            age_days = (today - saved_date).days
            if age_days > 4:
                logger.warning("[prev_volumes] 캐시가 %d일 경과 — 무효 (주말/연휴 등)", age_days)
                return
            volumes: dict = data.get("volumes", {})
            self._prev_volumes = {k: int(v) for k, v in volumes.items() if int(v or 0) > 0}
            logger.info("[prev_volumes] 로드 완료 — %d종목 (%s 기준)", len(self._prev_volumes), saved_date)
        except Exception as e:
            logger.warning("[prev_volumes] 로드 실패: %s", e)

    def save_prev_volumes(self) -> None:
        """
        현재 SnapshotStore의 거래량을 전일 거래량으로 저장 (15:20 강제청산 시 호출).
        저장 형식: {"date": "YYYY-MM-DD", "volumes": {"code": volume, ...}}
        """
        # json 은 모듈 최상위에서 이미 임포트됨
        try:
            with self.store._lock:
                snap_df = self.store._df.copy()
            if snap_df.empty or "volume" not in snap_df.columns:
                logger.warning("[prev_volumes] 스냅샷 데이터 없음 — 저장 스킵")
                return
            volumes = {
                str(code): int(row["volume"])
                for code, row in snap_df.iterrows()
                if int(row.get("volume", 0) or 0) > 0
            }
            path = self._prev_volumes_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": date.today().isoformat(), "volumes": volumes}, f, ensure_ascii=False)
            logger.info("[prev_volumes] 저장 완료 — %d종목 (%s)", len(volumes), date.today())
            self._prev_volumes = volumes  # 메모리 캐시도 갱신
        except Exception as e:
            logger.warning("[prev_volumes] 저장 실패: %s", e)

    def _roll_amt_baseline_date(self) -> None:
        t = date.today()
        if self._amt_baseline_date != t:
            self._amt_baseline_date = t
            self._amt_baseline.clear()

    def _touch_trade_amt_baseline(self, code: str, amt: int) -> None:
        """기준 시각(pre_filter_time) 이후 해당 종목의 최초 양수 거래대금을 당일 기준으로 고정."""
        self._roll_amt_baseline_date()
        if code in self._amt_baseline or amt <= 0:
            return
        if datetime.now().time() < self.cfg.pre_filter_time:
            return
        self._amt_baseline[code] = amt

    def _trade_amount_diag(self, code: str, amt: int) -> str:
        """Pre-Filter 등 로그용: 조·억 표기 + 9시대비 증가율."""
        a = int(amt or 0)
        self._touch_trade_amt_baseline(code, a)
        ta = format_trade_amount_korean(a)
        gr = format_trade_amount_growth(a, self._amt_baseline.get(code))
        return f"거래대금 {ta} · {gr}"

    # -----------------------------------------------------------------------
    # 시작 / 정지
    # -----------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        all_codes = self._fetch_all_codes()
        logger.info("전 종목 %d개 수집", len(all_codes))

        # ③ 터미널 뷰 시작
        self.display.start()

        # 1단계 예약
        # 현재 시각이 09:00~15:20 사이면 즉시 실행, 아니면 내일 09:00 예약
        now = datetime.now().time()
        market_start = self.cfg.pre_filter_time  # 이미 dtime 타입
        market_end = dtime(15, 30, 0)

        if market_start <= now <= market_end:
            logger.info("현재 시각이 장시간(%s~%s) — Pre-Filter 즉시 실행",
                       self.cfg.pre_filter_time, "15:30")
            self._run_pre_filter()
        else:
            secs = self._seconds_until(self.cfg.pre_filter_time)
            t = threading.Timer(secs, self._run_pre_filter)
            t.daemon = True
            t.start()
            logger.info("Pre-Filter %.0f초 후 실행 예약", secs)

        # 2단계 루프
        self._scan_thread = threading.Thread(
            target=self._realtime_loop, daemon=True, name="ScanLoop"
        )
        self._scan_thread.start()

    def stop(self) -> None:
        self._running = False
        self.display.stop()
        self.store.export_csv(os.path.join(self.cfg.log_dir, "snapshot_final.csv"))
        logger.info("SmartScanner 정지 — 스냅샷 저장 완료")

    # -----------------------------------------------------------------------
    # 1단계: Pre-Filter
    # -----------------------------------------------------------------------

    def _run_pre_filter(self) -> None:
        logger.info(
            "▶ [1단계] Pre-Filter 시작 — opt10030 상위 %d종목 수집 → 필터 후 감시 %d종목",
            self.cfg.collect_raw_top_n, self.cfg.watch_pool_max,
        )
        scan_log.info("PRE_FILTER_START\t%s", datetime.now().strftime("%H:%M:%S"))

        rows = self._fetch_top_volume_rows(target=self.cfg.collect_raw_top_n)
        rows, _ = filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        _n0 = len(rows)
        rows = [r for r in rows if float(r.get("change_pct", 0) or 0) < mc]
        if _n0 != len(rows):
            logger.info(
                "  등락률 상한 %.1f%% 미만만 유지 — %d → %d종목",
                mc, _n0, len(rows),
            )
        rows = apply_universe_score_cap(rows, self.cfg.watch_pool_max, self.cfg, self._prev_volumes)
        if not rows:
            logger.warning("  ⚠ Pre-Filter — 필터 후 종목 없음, Pre-Filter 생략")
            return

        logger.info("  📊 감시 후보 %d종목 (순수 주식·hybrid스코어 상위·등락률 < %.1f%%)", len(rows), mc)

        # ① DataFrame 에 일괄 적재
        self.top_mgr.clear()
        self.store.bulk_update(rows)

        for idx, row in enumerate(rows, 1):
            self.top_mgr.update(row["code"], row["trade_amount"])
            change_pct = row.get("change_pct", 0)

            log_msg = (
                f"{self._trade_amount_diag(row['code'], int(row.get('trade_amount') or 0))} / "
                f"등락률 {change_pct:+.2f}% / "
                f"현재가 {row.get('current_price', 0):,}원"
            )

            ScannerLogger.passed(
                row["code"], row.get("name", ""), "PRE_FILTER", log_msg
            )

            if idx % 10 == 0 or idx <= 5:
                logger.info("  ✓ [%3d] %s(%s) %s",
                           idx, row.get("name", "")[:10], row["code"], log_msg)

        top_codes = self.top_mgr.get_top_codes()
        self.watch_q.refresh(top_codes)
        self._prefiltered = True

        ScannerLogger.pre_filter_summary(
            total=len(rows), passed=len(top_codes),
            top_n=self.cfg.watch_pool_max,
        )
        logger.info("▶ [1단계] Pre-Filter 완료 — %d→%d종목 선정", len(rows), len(top_codes))
        for i, code in enumerate(top_codes[:10], 1):
            snap = self.store.get_snapshot(code)
            if snap:
                logger.info("  🎯 [%2d순] %s(%s) %s원", i, snap.name[:10], snap.code, f"{snap.current_price:,}")

    # -----------------------------------------------------------------------
    # 2단계: Real-time Scan 루프
    # -----------------------------------------------------------------------

    def _realtime_loop(self) -> None:
        logger.info("▶ [2단계] Real-time Scan 시작")
        while self._running:
            t0 = time.monotonic()
            if self._prefiltered:
                if self._universe_paused:
                    # ====== WATCH 모드 ======
                    # Tier 1(보유 5개): 현재가 갱신은 _on_receive_real_data + order_manager 처리
                    #   → 여기서는 아무것도 하지 않음 (0.1초 sleep만)
                    # Tier 2(예비 2개): 10초마다 스코어링으로 최신화
                    if t0 - self._last_reserve_refresh >= self._RESERVE_INTERVAL:
                        self._refresh_reserve_codes()
                        self._last_reserve_refresh = t0
                else:
                    # ====== SEARCH 모드 ======
                    # Tier 3 전체(~110개): 매 사이클 _evaluate() 실행
                    for code in list(self.watch_q.subscribed):
                        snap = self.store.get_snapshot(code)
                        if snap:
                            self._evaluate(snap)
            elapsed = time.monotonic() - t0
            # WATCH 모드: 0.1초(초정밀 대기) / SEARCH 모드: 기본 주기(1초)
            interval = 0.1 if self._universe_paused else self.cfg.scan_interval
            time.sleep(max(0.0, interval - elapsed))

    def _evaluate(self, snap: StockSnapshot) -> None:
        # ① 유니버스 감시 중단 — 포지션 풀 시 신규 신호 판단 차단
        if self._universe_paused:
            return

        # ① 캔들 마감 게이팅 — 분이 바뀔 때만 평가 (틱 기반 고점 진입 방지)
        cur_min = datetime.now().minute
        if self._eval_min.get(snap.code, -1) == cur_min:
            return
        self._eval_min[snap.code] = cur_min

        # ② 등락률 상한
        if snap.change_pct >= self.cfg.max_change_pct:
            return

        # ② 시간 필터
        now = datetime.now().time()
        if not (self.cfg.entry_start_time <= now <= self.cfg.entry_end_time):
            return

        # ②-bis 요셉 시그널 추세 단계 갱신 (분 단위 1회)
        if getattr(self.cfg, "yosep_trend_enabled", True):
            from strategy.jang_dong_min import get_trend_status
            trend_level = get_trend_status(
                closes=list(snap.closes_1min or []),
                highs=list(snap.highs_1min or []),
                lows=list(snap.lows_1min or []),
                volumes=list(snap.volumes_1min or []),
                ema_period=int(getattr(self.cfg, "yosep_ema_period", 20)),
                atr_period=int(getattr(self.cfg, "yosep_atr_period", 14)),
                volume_lookback=int(getattr(self.cfg, "yosep_volume_lookback", 20)),
            )
            snap.trend_prev_level = int(getattr(snap, "trend_level", 0))
            snap.trend_level = int(trend_level)
            self.store.update_trend_level(snap.code, trend_level)

        enabled = set(getattr(self.cfg, "enabled_strategies", ("BREAKOUT", "JDM_ENTRY")) or ())
        order = tuple(getattr(self.cfg, "strategy_order", ("BREAKOUT", "JDM_ENTRY")) or ())

        # strategy_order를 따르되 enabled에 없는 항목은 스킵.
        # 모든 전략이 비활성/미설정이면 안전하게 종료.
        for strategy in order:
            if strategy not in enabled:
                continue

            sig: Optional[ScanSignal] = None
            if strategy == "BREAKOUT":
                sig = self._build_breakout_signal(snap)
            elif strategy == "JDM_ENTRY":
                sig = self._build_jdm_signal(snap)
            else:
                logger.debug("[Strategy] 알 수 없는 전략명 스킵 — %s", strategy)
                continue

            if sig is not None:
                sig.trend_level = int(getattr(snap, "trend_level", 0))
                sig.trend_prev_level = int(getattr(snap, "trend_prev_level", 0))
                self._emit(sig)
                # 같은 분 다중 전략 동시 진입 방지: 우선순위 첫 통과 전략만 발행
                return

    def _build_breakout_signal(self, snap: StockSnapshot) -> Optional[ScanSignal]:
        """BREAKOUT 전략 평가 후 통과 시 ScanSignal을 반환한다."""
        # trend_level에 따라 고점 필터 동적 조정 (2026-04-23)
        # 상승 추세 종목은 고점에서 조금 하락해도 진입 허용, 약한 추세는 엄격하게
        trend_level = int(getattr(snap, "trend_level", 0))
        if trend_level >= 2:  # 상승 추세
            pullback_threshold = 5.0   # 고점 대비 5% 하락까지만 차단
        elif trend_level == 1:  # 중간 추세
            pullback_threshold = 3.0   # 고점 대비 3% 하락까지만 차단
        else:  # trend_level == 0 (약한 추세)
            pullback_threshold = self.cfg.breakout_pullback_from_high_pct  # 기본값 2.5%

        r_breakout = check_breakout(
            snap,
            breakout_ratio=self.cfg.breakout_ratio,
            volume_mult=self.cfg.breakout_volume_mult,
            pullback_from_high_pct=pullback_threshold,
            min_rising_bars=self.cfg.breakout_min_rising_bars,
        )
        if not r_breakout:
            return None

        r_gate = check_breakout_gate(snap, self.cfg)
        if not r_gate:
            return None

        reason = " | ".join(r for r in [r_breakout, r_gate] if r)
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        return ScanSignal(
            snap.code, snap.name, "BREAKOUT", snap.current_price, reason,
            entry_candle_low=candle_low,
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
        )

    def _build_jdm_signal(self, snap: StockSnapshot) -> Optional[ScanSignal]:
        """JDM_ENTRY 전략 평가 후 통과 시 ScanSignal을 반환한다."""
        # EMA20 필터 — 현재가가 20분 EMA 위에 있어야 진입 (추세 상승 확인)
        r_ema20 = check_ema20_filter(snap)
        if r_ema20 is None:
            return None

        # MA20 이격도 — 데이터 부족 시 bypass(None은 조인에서 제외)
        r_disp = check_disparity_from_ma(snap, max_pct=self.cfg.max_disparity_pct)

        # JDM 통합 게이트
        r_jdm = check_jdm_entry(snap, self.cfg)
        if r_jdm is None:
            return None

        reason = " | ".join(r for r in [r_ema20, r_disp, r_jdm] if r)
        candle_low = int(snap.lows_1min[-1]) if snap.lows_1min else 0
        # 일봉 맥락 — TP 상향 여부 판단
        from strategy.jang_dong_min import get_daily_context as _gdc
        _dctx = _gdc(snap.daily_closes, snap.current_price,
                     float(getattr(self.cfg, "daily_near_high_threshold_pct", 3.0)))
        return ScanSignal(
            snap.code, snap.name, "JDM_ENTRY", snap.current_price, reason,
            entry_candle_low=candle_low,
            near_daily_high=_dctx["near_high"],
            daily_ma20=_dctx["daily_ma20"],
            change_pct=float(getattr(snap, "change_pct", 0) or 0),
        )

    # -----------------------------------------------------------------------
    # 3단계: Final Signal
    # -----------------------------------------------------------------------

    def _emit(self, sig: ScanSignal) -> None:
        # 동일 종목/신호 재발행 쿨다운
        now_ts = time.monotonic()
        cooldown = float(getattr(self.cfg, "signal_cooldown_sec", 0.0) or 0.0)
        key = (sig.code, sig.signal_type)
        last_ts = self._last_signal_ts.get(key, 0.0)
        if cooldown > 0 and (now_ts - last_ts) < cooldown:
            logger.debug(
                "[SignalCooldown] %s(%s) [%s] 스킵 — %.1fs < %.1fs",
                sig.name, sig.code, sig.signal_type, (now_ts - last_ts), cooldown,
            )
            return
        self._last_signal_ts[key] = now_ts

        # ② 파일 로그
        ScannerLogger.signal(sig)
        logger.warning("🚨 [3단계] %s(%s) [%s] %s", sig.name, sig.code,
                       sig.signal_type, sig.reason)
        # ③ 터미널 알림
        self.display.alert(sig)

        if self.on_signal:
            self.on_signal(sig)

    # -----------------------------------------------------------------------
    # 실시간 데이터 콜백
    # -----------------------------------------------------------------------

    def _connect_realtime_signal(self) -> None:
        self._kiwoom._ocx.OnReceiveRealData.connect(self._on_receive_real_data)
        # 코스피(001)/코스닥(101) 업종지수 실시간 구독 — opt20001 TR 폴링 의존 제거
        try:
            self._kiwoom._ocx.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                ["9050", "001;101", "10;11;12;25", "0"],
            )
            logger.info("[업종지수 실시간] 코스피·코스닥 SetRealReg 등록 완료")
        except Exception as _e:
            logger.warning("[업종지수 실시간] SetRealReg 실패 — opt20001 폴백 유지: %s", _e)

    def _on_receive_real_data(
        self, code: str, real_type: str, real_data: str
    ) -> None:
        # ── 업종지수 실시간 (코스피 001 / 코스닥 101) ─────────────────────────
        if real_type == "업종지수":
            self._handle_index_realtime(code)
            return

        if real_type not in ("주식체결",):
            return

        def fid(n: int) -> str:
            return self._kiwoom._ocx.dynamicCall(
                "GetCommRealData(QString, int)", [code, n]
            )

        try:
            from kiwoom_api import safe_int, safe_float
            price = safe_int(fid(10))
            vol   = safe_int(fid(13))
            # [FIX] FID 14는 "누적거래금액"이 아니라 "현재 틱의 거래금액"
            # → opt10030의 누적 거래대금을 보존하기 위해 실시간 업데이트 제외
            high  = safe_int(fid(17))
            low   = safe_int(fid(18))
            open_ = safe_int(fid(16))
            pct   = safe_float(fid(12))
            strength_raw = safe_float(fid(20))    # [NEW] FID 20: 체결강도
            # FID 20은 일부 상황에서 실제값의 100배로 반환됨 (e.g., 91818 → 918.18%)
            # 10000 이상이면 100으로 나눠서 정규화
            strength = strength_raw / 100.0 if strength_raw >= 10000.0 else strength_raw

            if price <= 0:
                return   # 유효하지 않은 체결 데이터

            # ① DataFrame 갱신 (API 재호출 없음)
            # trade_amount는 opt10030의 누적값을 유지 (FID 14는 현재 틱만 포함)
            self.store.update_price(
                code=code, current_price=price, high_price=high,
                low_price=low, open_price=open_, volume=vol,
                trade_amount=None,  # ← 거래대금은 opt10030 값만 사용
                change_pct=pct,
            )
            snap_now = self.store.get_snapshot(code)
            amt = int(snap_now.trade_amount) if snap_now else 0
            self._touch_trade_amt_baseline(code, amt)
            self.top_mgr.update(code, amt)

            # [NEW] 체결강도 저장 (FID 20)
            if strength > 0:
                self.store.update_chejan_strength(code, strength)

            # [NEW] 포지션 종목 현재가 실시간 반영 (손절/익절 정확도 개선)
            if self._order_mgr and code in self._order_mgr.positions and price > 0:
                self._order_mgr.positions[code].current_price = price
                if snap_now is not None and hasattr(self._order_mgr, "update_position_trend"):
                    self._order_mgr.update_position_trend(code, int(getattr(snap_now, "trend_level", 0)))

            # watch_q.refresh — SetRealReg/Remove를 매 틱 호출하면 API 과부하
            # 30초 간격으로만 구독 목록을 갱신한다 (유니버스 감시 중단 중은 스킵)
            now_t = time.monotonic()
            if not self._universe_paused and now_t - self._last_watchq_refresh >= self._WATCHQ_INTERVAL:
                self.watch_q.refresh(self.top_mgr.get_top_codes())
                self._last_watchq_refresh = now_t

        except Exception as e:
            logger.debug("실시간 파싱 오류 — %s: %s", code, e)

    def _handle_index_realtime(self, idx_code: str) -> None:
        """업종지수 실시간 틱 처리 — TR 호출 없이 헤더 지수를 즉시 갱신한다."""
        try:
            from kiwoom_api import safe_float
            def fid(n: int) -> str:
                return self._kiwoom._ocx.dynamicCall(
                    "GetCommRealData(QString, int)", [idx_code, n]
                )
            raw_cur = safe_float(fid(10))
            # 키움 opt20001과 동일: 10,000 초과 시 ×100 보정 (소수점 2자리 정수화)
            current = raw_cur / 100.0 if raw_cur > 10_000 else raw_cur
            chg_pct = safe_float(fid(12))
            if current <= 0:
                return
            if self.on_index_update:
                self.on_index_update(idx_code, current, chg_pct)
        except Exception as e:
            logger.debug("[업종지수 실시간] %s 파싱 오류: %s", idx_code, e)

    # -----------------------------------------------------------------------
    # 헬퍼
    # -----------------------------------------------------------------------

    def _fetch_all_codes(self) -> list[str]:
        codes = []
        for m in self.cfg.markets:
            raw = self._kiwoom._ocx.dynamicCall(
                "GetCodeListByMarket(QString)", [m]
            )
            codes.extend(c for c in raw.strip().split(";") if c)
        return codes

    def _fetch_top_trade_amount(self, count: int) -> list[dict]:
        """하위 호환용 — _fetch_top_volume_rows 위임"""
        return self._fetch_top_volume_rows(target=min(count, self.cfg.collect_raw_top_n))

    def _fetch_top_volume_rows(
        self,
        target: int = 200,
        on_progress: Optional[Callable] = None,
        retry: int = 2,
    ) -> list[dict]:
        """
        거래대금 상위 조회 — opt10030 (KiwoomManager.fetch_opt10030_top_volume).

        target=400 기준 TR 약 4회(연속조회) + 레이트리미터 각 0.25s → 합계 ~1~2s 수준.

        [2026-04-23] 최적화:
        - 중복 호출 방지: 이미 fetching 중이면 캐시 우선 반환
        - 캐시 우선: 5분 이내 갱신된 캐시는 즉시 반환 (메인 스레드 블로킹 회피)
        """
        now = time.monotonic()
        cache_age = now - self._last_volume_updated

        # ① 이미 fetching 중이면 캐시 우선 (중복 호출 차단)
        if self._opt10030_fetching:
            if self._last_volume_rows:
                logger.info("[opt10030] 진행 중 — 캐시 %d종목 (나이 %.1fs)",
                           len(self._last_volume_rows), cache_age)
                return self._last_volume_rows[:target]
            else:
                logger.warning("[opt10030] 진행 중인데 캐시 없음 — 대기")
                # 캐시가 없으면 fallback까지 기다림 (밑으로 진행)

        # ② 최근 5분 이내 갱신된 캐시 있으면 즉시 반환 (메인 스레드 블로킹 회피)
        if self._last_volume_rows and cache_age < 300.0:  # 5분
            logger.info("[opt10030] 캐시 재사용 (나이 %.1fs, %d종목)", cache_age, len(self._last_volume_rows))
            return self._last_volume_rows[:target]

        # ③ 실제 조회 필요 — 플래그 설정 후 진행
        logger.info("[opt10030] 거래대금 상위 조회 시작 (목표 %d종목, 캐시나이 %.1fs)", target, cache_age)
        if on_progress:
            on_progress("거래대금 상위 조회", 0, target, "opt10030 조회 중...")

        self._opt10030_fetching = True
        try:
            for attempt in range(retry):
                try:
                    if hasattr(self._kiwoom, "fetch_opt10030_top_volume"):
                        rows = self._tr_q.call(self._kiwoom.fetch_opt10030_top_volume, target)
                    else:
                        rows = self._tr_q.call(self._do_fetch_opt10030)
                        rows = rows[:target]
                    logger.info("[opt10030] 응답 %d행 (목표 %d)", len(rows), target)

                    if rows:
                        result = rows[:target]
                        logger.info("[opt10030] 최종 %d종목 확보", len(result))
                        self._last_volume_rows = result
                        self._last_volume_updated = time.monotonic()
                        if on_progress:
                            on_progress("거래대금 상위 조회", len(result), target,
                                        f"{len(result)}종목 확보")
                        return result

                except Exception as e:
                    logger.warning("[opt10030] 조회 실패 (attempt %d): %s", attempt + 1, e)
        finally:
            self._opt10030_fetching = False

        # opt10030 결과 없을 때 — 직전 성공 결과 재사용 (캐시 없을 때만 하드코딩 대체)
        if self._last_volume_rows:
            logger.warning("[opt10030] 실패 — 직전 스캔 결과 %d종목 재사용 (나이 %.1fs)",
                           len(self._last_volume_rows), cache_age)
            return self._last_volume_rows[:target]

        logger.warning("[opt10030] 실제 조회 실패 — 시총 상위 종목으로 대체 (캐시 없음, 최초 실패)")
        fallback = [
            {"code": "005930", "name": "삼성전자",        "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "000660", "name": "SK하이닉스",       "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "207940", "name": "삼성바이오로직스",  "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "005380", "name": "현대차",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "373220", "name": "LG에너지솔루션",   "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "000270", "name": "기아",             "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "035420", "name": "NAVER",            "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "051910", "name": "LG화학",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "006400", "name": "삼성SDI",          "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
            {"code": "035720", "name": "카카오",           "current_price": 0, "trade_amount": 0, "change_pct": 0.0, "prev_close": 0, "open_price": 0, "high_price": 0, "low_price": 0, "volume": 0},
        ]
        logger.info("[opt10030] 대체 종목 %d개 사용", len(fallback))
        return fallback[:target]

    def _do_fetch_opt10030(self) -> list[dict]:
        """opt10030 CommRqData 호출 → rows 반환"""
        logger.debug("[opt10030] CommRqData 호출")
        self._kiwoom._set_input("시장구분",     "0")  # 0=전체
        self._kiwoom._set_input("정렬구분",     "1")  # 1=거래대금 내림차순
        self._kiwoom._set_input("관리종목포함", "0")  # 0=제외
        self._kiwoom._set_input("신용구분",     "0")  # 0=전체
        self._kiwoom._comm_rq("opt10030", "거래대금상위", "9000")
        rows = self._kiwoom._tr_data.get("rows", [])
        logger.debug("[opt10030] 응답 %d행", len(rows))
        return rows

    def _log_store_health(self) -> None:
        """SnapshotStore 상태를 5분마다 한 번 로깅 (Zone 6)."""
        _now = time.monotonic()
        if _now - getattr(self, "_store_health_last", 0.0) < 300.0:
            return
        self._store_health_last = _now

        try:
            with self.store._lock:
                _codes_idx   = list(self.store._df.index)   # DataFrame index = 등록 종목
                _n_codes     = len(_codes_idx)
                _n_mins      = len(self.store._mins)        # 1분봉 데이터 보유 종목 수
                _n_tick_vols = len(getattr(self.store, "_tick_ts_vol", {}))
                _n_sectors   = len(getattr(self.store, "_sector_cache", {}))
                _codes_no_1m = [
                    c for c in _codes_idx
                    if len(self.store._mins.get(c, [])) < 5
                ]
            logger.info(
                "[스토어헬스] 종목=%d 1분봉보유=%d 틱Vel=%d 섹터캐시=%d "
                "1분봉5개미만=%d개%s",
                _n_codes, _n_mins, _n_tick_vols, _n_sectors,
                len(_codes_no_1m),
                f" {_codes_no_1m[:5]}" if _codes_no_1m else "",
            )
        except Exception as _e:
            logger.debug("[스토어헬스] 수집 실패: %s", _e)

    def run_periodic_scan(self, on_progress=None) -> list:
        """
        1분마다 호출하는 전체 스캔 사이클.

        1. opt10030 으로 거래대금 상위 collect_raw_top_n 종목 조회(필요 시 연속조회)
        2. 우선주·ETF 제거 후 거래대금 상위 watch_pool_max 만 스냅샷·감시에 유지
        3. 테스타 정배열 + 장동민 시가돌파 필터링
        4. 통과 종목을 final_targets(ScanSignal 리스트)로 반환

        Args:
            on_progress: 진행 콜백 — on_progress(phase, current, total, detail)
        """
        def _prog(phase, current, total, detail=""):
            if on_progress:
                on_progress(phase, current, total, detail)

        # ① WATCH 모드(포지션 풀)이면 opt10030 호출 자체를 스킵
        if self._universe_paused:
            logger.info("[주기 스캔] WATCH 모드 — opt10030 스캔 스킵 (SetRealReg 감시 중)")
            self._log_store_health()
            return []

        logger.info("=" * 60)
        logger.info("[주기 스캔] 시작 — %s", datetime.now().strftime("%H:%M:%S"))
        self._log_store_health()
        _prog("거래대금 상위 조회", 0, self.cfg.collect_raw_top_n, "opt10030 조회 중...")

        # 연결 확인
        if hasattr(self._kiwoom, 'is_connected') and not self._kiwoom.is_connected():
            logger.warning("[주기 스캔] 연결 끊김 — 스킵")
            return []

        # 1. opt10030 조회(연속조회) → 필터 → 우선주·ETF 제외 → 거래대금 상위 watch_pool_max 유지
        rows = self._fetch_top_volume_rows(
            target=self.cfg.collect_raw_top_n, on_progress=on_progress,
        )
        rows, _ = filter_equity_rows(rows)
        mc = self.cfg.max_change_pct
        _n0 = len(rows)
        rows = [r for r in rows if float(r.get("change_pct", 0) or 0) < mc]
        if _n0 != len(rows):
            logger.info(
                "[주기 스캔] 등락률 상한 %.1f%% 미만만 유지 — %d → %d종목",
                mc, _n0, len(rows),
            )
        rows = apply_universe_score_cap(rows, self.cfg.watch_pool_max, self.cfg, self._prev_volumes)
        if not rows:
            logger.warning("[주기 스캔] 필터 후 종목 없음 — 중단")
            return []

        _prog("거래대금 상위 조회", len(rows), self.cfg.watch_pool_max,
              f"{len(rows)}종목 감시 후보")

        logger.info(
            "[주기 스캔] 감시 후보 %d종목 (수집 %d → 등락 <%.1f%%·hybrid스코어 상위 %d)",
            len(rows), self.cfg.collect_raw_top_n, mc, self.cfg.watch_pool_max,
        )

        # 2. SnapshotStore / TopVolumeManager 갱신
        self.top_mgr.clear()
        logger.debug("[주기 스캔] STEP-A: bulk_update 시작 (%d행)", len(rows))
        self.store.bulk_update(rows)
        logger.debug("[주기 스캔] STEP-B: bulk_update 완료")

        for row in rows:
            _c = row["code"]
            _a = int(row.get("trade_amount") or 0)
            self._touch_trade_amt_baseline(_c, _a)
            self.top_mgr.update(_c, _a)
        logger.debug("[주기 스캔] STEP-C: top_mgr 갱신 완료")

        # 감시·선정용 코드 목록은 SnapshotStore(이번 스캔·유니버스필터 반영)만 사용한다.
        # TopVolumeManager 는 실시간 틱으로 과거 종목이 누적되어 스냅샷과 불일치할 수 있음(예: 99 vs 36).
        _watch_df = self.store.top_by_trade_amount(self.cfg.watch_pool_max)
        top_codes = _watch_df.index.tolist() if not _watch_df.empty else []
        logger.debug(
            "[주기 스캔] STEP-D: top_codes %d개 (스냅샷 기준, 순수 주식만)",
            len(top_codes),
        )

        # STEP-E: SetRealReg 를 이벤트루프 다음 사이클로 위임
        # — dynamicCall 내부에서 Windows 메시지 처리 → OCX 재진입 데드락 방지
        # — 유니버스 감시 중단 중은 스킵
        _reg_codes = top_codes[:self.cfg.realtime_sub_max]
        logger.debug("[주기 스캔] STEP-E: watch_q.refresh 예약 (구독대상=%d)", len(_reg_codes))
        if not self._universe_paused:
            QTimer.singleShot(0, lambda c=_reg_codes: self.watch_q.refresh(c))
        logger.debug("[주기 스캔] STEP-F: watch_q.refresh %s", "스킵(감시중단)" if self._universe_paused else "예약 완료")

        self._prefiltered = True
        logger.debug("[주기 스캔] STEP-G: prefiltered=True")

        # STEP-H: 분봉 초기 로딩 — 데이터 부족 종목을 비동기(QTimer 체인)로 처리
        # ⚠️  메인 스레드에서 TR 을 동기 루프로 호출하면 UI 가 수십 초 얼어붙음.
        #     QTimer.singleShot 체인으로 한 종목씩 분산 처리한다.
        _CANDLE_MIN_BARS = 55   # MA50 에 필요한 최소 분봉 수
        _CANDLE_LOAD_MAX = 6    # 이후 사이클당 최대 예약 종목 수 (12→6, TR 경합 감소)
        codes_need_all = [
            code for code in top_codes
            if len(self.store._mins.get(code, [])) < _CANDLE_MIN_BARS
        ]

        if not self._initial_candle_load_done:
            # 첫 스캔: 제한 없이 전체 종목 일괄 로딩 (장 시작부터 누적된 데이터 확보)
            codes_need = codes_need_all
            if codes_need:
                logger.info(
                    "[주기 스캔] STEP-H: 첫 스캔 — 전체 %d종목 1분봉 일괄 로딩 시작 "
                    "(350ms 간격 체인, 이후 사이클은 %d종목/회로 복귀)",
                    len(codes_need), _CANDLE_LOAD_MAX,
                )
        else:
            # 이후 사이클: 12종목/사이클 제한 유지 (신규 편입 종목만 처리)
            codes_need = codes_need_all[:_CANDLE_LOAD_MAX]

        if codes_need:
            if self._initial_candle_load_done:
                logger.debug(
                    "[주기 스캔] STEP-H: 1분봉 비동기 로딩 예약 (%d종목) — "
                    "350ms 간격으로 순차 처리, UI 블로킹 없음",
                    len(codes_need),
                )
            QTimer.singleShot(500, lambda c=list(codes_need): self._load_candles_async(c, 0))
        else:
            logger.debug("[주기 스캔] STEP-H: 분봉 데이터 충분 — 초기 로딩 스킵")

        # 진단 로그: bulk_update 이후 거래대금 상위 N종 샘플 (N=diagnostic_sample_n)
        _dn = max(1, int(self.cfg.diagnostic_sample_n))
        sample = self.store.top_by_trade_amount(_dn)
        if not sample.empty:
            for code_s, row_s in sample.iterrows():
                _amt = int(row_s.get("trade_amount", 0))
                _ta = format_trade_amount_korean(_amt)
                _gr = format_trade_amount_growth(_amt, self._amt_baseline.get(str(code_s)))
                logger.debug(
                    "[진단] %s(%s) 현재가=%s 거래대금=%s · %s 거래량=%s",
                    row_s.get("name", "?"), code_s,
                    f"{int(row_s.get('current_price', 0)):,}",
                    _ta, _gr,
                    f"{float(row_s.get('volume', 0)):,.0f}",
                )
            logger.debug(
                "[진단] 안내 — 위 %d종은 거래대금 상위 샘플이다. 실제 감시·스냅샷 후보는 최대 %d종, "
                "ScannerWorker 신호 판단은 상위 %d종에서 수행된다.",
                _dn,
                self.cfg.watch_pool_max,
                self.cfg.display_top_n,
            )
        else:
            logger.warning("[진단] top_by_trade_amount 결과 없음 — 파싱 필드명 불일치 가능성")
            # rank 기반 샘플 확인
            with self.store._lock:
                df_sample = self.store._df.head(_dn)
            if not df_sample.empty:
                logger.warning("[진단] DataFrame 직접 샘플: %s", df_sample[["trade_amount","volume","rank"]].to_dict())

        logger.info("[주기 스캔] SnapshotStore 갱신 완료 (%d종목)", len(rows))

        # [일봉 갱신] 5분 주기 — 후보 코드만 계산해 _daily_refresh_pending에 저장.
        # 실제 TR 호출(opt10081)은 MainWindow가 QTimer 체인으로 처리해 메인 스레드 블로킹 방지.
        now = time.time()
        if now - self._last_daily_update >= self._daily_update_interval_sec:
            self._last_daily_update = now
            _eod_chg_min = float(getattr(self.cfg, "eod_change_pct_min", 2.0))
            _eod_chg_max = float(getattr(self.cfg, "eod_change_pct_max", 10.0))
            _daily_refresh_max = 10
            with self.store._lock:
                _df_snap = self.store._df.copy()
            _eod_candidates = [
                c for c in top_codes
                if _eod_chg_min <= float(_df_snap.at[c, "change_pct"]
                                         if c in _df_snap.index else 0.0) <= _eod_chg_max
            ]
            _rest = [c for c in top_codes if c not in set(_eod_candidates)]
            self._daily_refresh_pending = (_eod_candidates + _rest)[:_daily_refresh_max]
            logger.info("[일봉갱신] %d종목 예약 (EOD후보%d+보완%d) — QTimer 체인으로 처리",
                        len(self._daily_refresh_pending),
                        min(len(_eod_candidates), _daily_refresh_max),
                        max(0, len(self._daily_refresh_pending) - len(_eod_candidates)))

        # 3. 신호 판단은 _realtime_loop()의 _evaluate()에서 백그라운드 스레드가 담당.
        #    주기 스캔은 데이터 갱신(opt10030 + SnapshotStore)만 수행하고 종료.
        #    (과거 TESTA+JDM 필터 루프 제거 — 110종목 동기 루프가 메인 스레드를 차단하던 원인)
        logger.info("[주기 스캔] 완료 — 신호 판단은 실시간 워커(_evaluate)에 위임")
        logger.info("=" * 60)

        # 1분봉 캐시 저장 — 5분 주기, 백그라운드 스레드에서 실행 (메인 스레드 I/O 블로킹 방지)
        if now - getattr(self, "_last_1min_cache_save", 0) >= 300:
            self._last_1min_cache_save = now
            import threading as _threading
            _threading.Thread(target=self.store.save_1min_cache, daemon=True).start()

        _prog("감시종목 갱신", len(top_codes), len(top_codes), "데이터 갱신 완료")
        return []

    # -----------------------------------------------------------------------
    # 포지션 실시간 현재가 갱신 (손절/익절 정확도 개선)
    # -----------------------------------------------------------------------

    _SCREEN_POSITION = "9210"   # 포지션 종목 전용 스크린 (watch_q의 9200과 분리)

    def add_position_realtime(self, code: str) -> None:
        """포지션 종목 실시간 현재가 구독 (별도 스크린 9210)"""
        try:
            self._kiwoom._ocx.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                [self._SCREEN_POSITION, code, "10;12", "1"],
            )
            logger.info("[포지션 실시간] 등록 — %s", code)
        except Exception as e:
            logger.warning("[포지션 실시간] 등록 실패 — %s: %s", code, e)

    def remove_position_realtime(self, code: str) -> None:
        """포지션 종목 실시간 구독 해제"""
        try:
            self._kiwoom._ocx.dynamicCall(
                "SetRealRemove(QString, QString)", [self._SCREEN_POSITION, code]
            )
            logger.info("[포지션 실시간] 해제 — %s", code)
        except Exception as e:
            logger.warning("[포지션 실시간] 해제 실패 — %s: %s", code, e)

    def pause_universe_watch(self, position_codes: list[str]) -> None:
        """포지션 풀 — 유니버스 감시를 보유 종목 + 임시 예비 2개로 축소.

        이후 _realtime_loop이 10초마다 _refresh_reserve_codes()로 예비를 스코어 기반 최신화.
        """
        self._universe_paused = True
        self._last_reserve_refresh = 0.0   # 첫 루프에서 즉시 스코어링 갱신 유도
        # 초기 예비: 스코어링 전 임시로 거래대금 상위 2개
        reserve = [c for c in self.top_mgr.get_top_codes() if c not in position_codes][:2]
        self.watch_q.refresh(position_codes + reserve)
        logger.info(
            "[Watch] WATCH 모드 진입 — 보유 %d개 + 임시예비 %d개 구독 (10초 후 스코어링 갱신)",
            len(position_codes), len(reserve),
        )

    def resume_universe_watch(self) -> None:
        """슬롯 생김 — 유니버스 감시 전체 복원."""
        self._universe_paused = False
        top = self.top_mgr.get_top_codes()
        self.watch_q.refresh(top)
        logger.info("[Watch] 유니버스 감시 재개 — 상위 %d종목 구독", len(top))

    # -----------------------------------------------------------------------
    # WATCH 모드 — 예비 종목 스코어링
    # -----------------------------------------------------------------------

    def _score_candidate(self, snap: "StockSnapshot") -> float:
        """예비 종목 점수 계산. 0이면 불합격 (진입 조건 미충족).

        기준:
        - 등락률 > 0, < max_change_pct (상승 중이되 과열 아님)
        - 현재가 > 시가 (시가 돌파 유지)
        - 등락률 점수(0~100) + 체결강도 보너스(0~20) 합산
        """
        if snap.current_price <= 0 or snap.open_price <= 0:
            return 0.0
        if snap.change_pct <= 0:
            return 0.0
        if snap.change_pct >= self.cfg.max_change_pct:
            return 0.0
        if snap.current_price <= snap.open_price:
            return 0.0

        score = min(snap.change_pct, 10.0) * 10.0                          # 등락률 (최대 100점)
        score += min(max(snap.chejan_strength - 100.0, 0.0), 100.0) * 0.2  # 체결강도 보너스 (최대 20점)
        return score

    def _refresh_reserve_codes(self) -> None:
        """WATCH 모드 전용 — _RESERVE_INTERVAL마다 예비 top-2를 실시간 점수로 최신화.

        TR 호출 없이 메모리(top_mgr + SnapshotStore)만 사용.
        상위 30개 후보에서 스코어링 후 가장 좋은 2개를 watch_q에 유지.
        """
        if not self._universe_paused:
            return

        pos_codes: set[str] = set()
        if self._order_mgr:
            pos_codes = set(self._order_mgr.positions.keys())

        if not pos_codes:
            return

        # top_mgr 상위 30개 중 보유 제외 → 스코어링
        candidates = [c for c in self.top_mgr.get_top_codes() if c not in pos_codes][:30]
        scored: list[tuple[float, str]] = []
        for code in candidates:
            snap = self.store.get_snapshot(code)
            if snap is not None:
                s = self._score_candidate(snap)
                if s > 0.0:
                    scored.append((s, code))

        scored.sort(reverse=True)
        new_reserve = [c for _, c in scored[:2]]

        # 현재 구독 중인 예비 목록과 비교 (보유 제외)
        old_reserve = [c for c in self.watch_q.subscribed if c not in pos_codes]

        if set(new_reserve) != set(old_reserve):
            self.watch_q.refresh(list(pos_codes) + new_reserve)
            logger.info(
                "[Watch] 예비 갱신 — %s → %s (점수: %s)",
                old_reserve or "없음",
                new_reserve or "없음",
                [f"{c}:{s:.0f}점" for s, c in scored[:2]],
            )
        else:
            logger.debug("[Watch] 예비 유지 — %s", new_reserve)

    def _load_candles_async(self, codes: list, idx: int) -> None:
        """
        분봉 초기 로딩을 QTimer.singleShot 체인으로 1종목씩 비동기 처리한다.

        메인 스레드에서 동기 루프로 여러 TR 을 연속 호출하면 UI 가 얼어붙는다.
        각 종목을 350ms 간격 체인으로 분산시켜 이벤트 루프가 살아있게 유지한다.
        """
        if idx >= len(codes):
            logger.info("[STEP-H async] 완료 — 총 %d종목 처리", len(codes))
            if not self._initial_candle_load_done:
                self._initial_candle_load_done = True
                logger.info("[STEP-H async] 첫 일괄 로딩 완료 — 이후 사이클 12종목/회 제한 복귀")
                # 캐시 저장 — 백그라운드 스레드 (메인 스레드 I/O 블로킹 방지)
                import threading as _threading
                def _save():
                    try:
                        self.store.save_1min_cache()
                        logger.info("[STEP-H async] 1분봉 캐시 파일 저장 완료")
                    except Exception as _e:
                        logger.warning("[STEP-H async] 1분봉 캐시 저장 실패: %s", _e)
                _threading.Thread(target=_save, daemon=True).start()
            return

        # _tr_busy 중이면 동일 종목을 최대 3회 재시도 후 다음으로 (cascade 방지)
        if getattr(self._kiwoom, "_tr_busy", False):
            retries = getattr(self, "_candle_retry_count", 0)
            if retries < 3:
                self._candle_retry_count = retries + 1
                logger.debug("[STEP-H async] TR 처리 중 — %s 재시도 %d/3", codes[idx], retries + 1)
                QTimer.singleShot(400, lambda: self._load_candles_async(codes, idx))
            else:
                self._candle_retry_count = 0
                logger.debug("[STEP-H async] TR 처리 중 — %s 재시도 초과, 다음으로", codes[idx])
                QTimer.singleShot(350, lambda: self._load_candles_async(codes, idx + 1))
            return
        self._candle_retry_count = 0

        code = codes[idx]

        # ① 파일 캐시 우선 확인 — 있으면 TR 호출 생략 (재시작/신규 편입 즉시 복구)
        cached_n = self.store.load_1min_for_code(code)
        if cached_n >= 55:
            logger.debug("[STEP-H async] %s 캐시에서 %d개 로딩 완료 — TR 스킵", code, cached_n)
            QTimer.singleShot(0, lambda: self._load_candles_async(codes, idx + 1))
            return

        # ② 캐시 없거나 부족 → opt10080 TR 호출 (direct, _tr_q 미사용 — cascade 방지)
        try:
            candles = self._kiwoom.get_min_candles(code, 1, 70)
            ohlc = [c for c in reversed(candles) if c.get("close")]
            if ohlc:
                self.store.set_min_candles_ohlc(code, ohlc)
                logger.debug("[STEP-H async] %s TR 1분봉 OHLC %d개 로딩 완료", code, len(ohlc))
            else:
                logger.debug("[STEP-H async] %s TR 응답 없음 — 스킵", code)
        except Exception as e:
            logger.warning("[STEP-H async] %s 1분봉 로딩 실패: %s", code, e)

        # 다음 종목을 350ms 후 처리 (TR 간격 0.25s + 여유 100ms)
        QTimer.singleShot(350, lambda: self._load_candles_async(codes, idx + 1))

    # _init_min_candles_for_top 제거됨 (2025-03 최적화)
    # SetRealReg 실시간 틱이 SnapshotStore.update_price()에서
    # 분봉을 자동 누적하므로 opt10080 TR 호출 불필요.

    # ── 수급 필터: opt10059 10분 주기 갱신 ────────────────────────────────────

    def trigger_investor_refresh(self) -> None:
        """
        메인 스레드 QTimer에서 호출 — 수급 데이터 갱신 시작점.
        watch pool 상위 investor_top_n 종목을 350ms 체인으로 순차 조회한다.
        (동기 루프 대신 QTimer.singleShot 체인 → UI 블로킹 없음)
        """
        if not self.cfg.investor_filter_enabled:
            return
        top_codes = (
            self.store.top_by_trade_amount(self.cfg.investor_top_n)
            .index.tolist()
        )
        if not top_codes:
            return
        logger.info("[수급갱신] %d종목 opt10059 갱신 시작", len(top_codes))
        QTimer.singleShot(0, lambda: self._refresh_investor_data_async(top_codes, 0))

    def _refresh_investor_data_async(self, codes: list, idx: int, retries: int = 0) -> None:
        """
        opt10059를 QTimer.singleShot 체인으로 1종목씩 비동기 처리한다.
        350ms 간격 → 최대 15종목 × 0.35s ≈ 5.25초 (TR 레이트 리미터 내).

        _tr_busy 시 최대 3회 재시도(800ms 간격), 초과 시 다음 종목으로 이동.
        _tr_q.call() 래퍼 사용 금지 — processEvents 중 스캔 타이머 발화로 인한
        cascading nested event loop 프리징 방지.
        """
        if idx >= len(codes):
            logger.info("[수급갱신] 완료 — %d종목 처리", len(codes))
            return

        # _tr_busy 시 같은 종목 재시도 (3회 한도) → 초과 시 skip
        if getattr(self._kiwoom, "_tr_busy", False):
            if retries < 3:
                logger.debug("[수급갱신] TR 처리 중 — %s 재시도 %d/3", codes[idx], retries + 1)
                QTimer.singleShot(
                    800, lambda: self._refresh_investor_data_async(codes, idx, retries + 1)
                )
            else:
                logger.debug("[수급갱신] TR 처리 중 — %s 재시도 초과, 다음 종목 이동", codes[idx])
                QTimer.singleShot(
                    500, lambda: self._refresh_investor_data_async(codes, idx + 1, 0)
                )
            return

        code = codes[idx]
        try:
            # _tr_q.call() 래퍼 제거 — processEvents cascade 방지
            # _comm_rq 내부의 TRRateLimiter가 rate limiting을 직접 처리함
            data = self._kiwoom.get_investor_trend(code)
            self.store.update_investor(code, data["foreign_net"], data["inst_net"])
            snap = self.store.get_snapshot(code)
            if snap:
                ScannerLogger.passed(
                    code, snap.name, "INVESTOR_REFRESH",
                    f"외국인={data['foreign_net']:+d} 기관={data['inst_net']:+d} "
                    f"score={snap.investor_score:+d}",
                )
        except Exception as e:
            logger.debug("[수급갱신] %s 실패: %s", code, e)

        QTimer.singleShot(350, lambda: self._refresh_investor_data_async(codes, idx + 1, 0))

    @staticmethod
    def _seconds_until(t: dtime) -> float:
        now    = datetime.now()
        target = now.replace(hour=t.hour, minute=t.minute,
                             second=t.second, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(0.0, (target - now).total_seconds())
