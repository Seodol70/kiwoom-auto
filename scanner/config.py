from __future__ import annotations
import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from datetime import time as dtime
from typing import Any, ClassVar, Optional


logger = logging.getLogger(__name__)

# [2026-05-19 DEBUG] 설정 로드 시점 기록
_load_time = datetime.now().isoformat()
logger.info(f"[SmartScannerConfig] 모듈 로드 시작 — {_load_time}")


def _get_cfg(key: str, default: Any) -> Any:
    """ConfigManager 에서 값을 안전하게 가져오는 헬퍼"""
    try:
        from app.config_manager import config_manager
        return config_manager.get(key, default)
    except ImportError:
        return default


class SmartScannerConfig:
    """
    스캐너 설정. config.py RISK/STRATEGY를 단일 진실 소스로 하여 동기화됨.
    ConfigManager.reload_adaptive()는 config.py 값을 SmartScannerConfig에 주입한다.
    """

    # opt10030 최초 수집 목표(연속조회로 최대 ~2회 TR). 이후 ETF·우선주 제거 → watch_pool_max 로 캡.
    collect_raw_top_n:    int   = 100         # 2026-06-05: 300→100 (3페이지→1페이지, 서버 연결 끊김 방지)
    watch_pool_max:       int   = 50          # [2026-05-21] 증분 업데이트 적용 후 50으로 복원
    pre_filter_top_n:     int   = 100         # 하위 호환: collect_raw_top_n 과 동일 사용 권장
    universe_trade_amt_weight: float = 0.2   # 거래대금 순위 가중치 (hybrid universe score)
    universe_vol_ratio_weight: float = 0.2   # 전일 대비 거래량 비율 가중치
    universe_chg_pct_weight:   float = 0.6   # 등락률 가중치
    pre_filter_time:      dtime = dtime(9, 0, 0)
    enabled_strategies:   tuple = ("JDM_ENTRY", "PULLBACK", "GAP_PULLBACK", "EOD")  # [2026-06-02] BREAKOUT 제거
    strategy_order:       tuple = ("JDM_ENTRY", "PULLBACK", "GAP_PULLBACK", "EOD")
    realtime_sub_max:     int   = 50          # [2026-05-21] watch_pool_max 와 일치 (50)
    scan_interval:        float = 60.0  # 2026-04-23: 10→60초 (메인 스레드 블로킹 해소, opt10030 TR 시간 여유)
    tr_delay:             float = 0.25        # TRRequestQueue 최소 간격
    breakout_ratio:       float = 0.010       # 2026-05-11: 1.5%→1.0% (공격적 완화, 진입 기회 확대)
    breakout_volume_mult: float = 0.5         # 2026-05-11: 0.8→0.5 (거래량 필터 거의 제거, 체결강도에만 의존)
    breakout_confirm_minutes:    float = 2.0  # 2026-04-08: 3분 → 2분 (빠른 종목 타이밍 확보)
    # 추세 강도별 관찰 시간 단축 — yosep trend_level 기준 (2026-04-15)
    # trend_level=3(Strong): 즉시 진입 (다음 틱에 gate 확인 후 신호)
    # trend_level=2(Medium): 절반 관찰 (기본 2분의 50%)
    # trend_level=1(Weak): 즉시 진입 — 종목 상승 추세(현재가>EMA, EMA 상승) 확인됨 (2026-04-30)
    # trend_level=0: 기본 breakout_confirm_minutes 유지
    breakout_confirm_minutes_trend3: float = 0.0   # Strong 추세 — 즉시
    breakout_confirm_minutes_trend2: float = 1.0   # Medium 추세 — 1분 관찰
    breakout_confirm_minutes_trend1: float = 0.0   # Weak 추세 — 즉시 (종목 상승 추세 확인됨)
    breakout_cancel_drawdown_pct: float = -0.8  # 2026-04-08: -0.5% → -0.8% (완화된 ratio 노이즈 흡수)
    breakout_pullback_from_high_pct: float = 5.0  # 당일 고점 대비 N% 이상 하락 중이면 BREAKOUT 차단 (2026-05-12: 2.5→5.0, 조정 구간 허용)
    breakout_min_rising_bars: int = 1         # 최근 N개 1분봉이 연속 상승이어야 BREAKOUT 통과 (완화: 2→1)
    jdm_ma_short:         int   = 7          # 최적화됨: 5→7
    jdm_ma_long:          int   = 15         # 최적화됨: 20→15
    jdm_rsi_low:          float = 35.0       # 레거시(다른 로직 참고용). 진입은 jdm_rsi_entry_min 사용
    jdm_rsi_high:         float = 70.0       # RSI 상한 (2026-06-05: 60→70, 급락장 강세 종목 차단 완화)
    jdm_rsi_entry_min:    float = 52.0       # JDM 진입 RSI 하한 — 2026-04-13: 60→52 (상승 시작점 타점)
    jdm_min_ma_spread_abs: int = 30          # [deprecated] MA 이격(원) — 레거시 호환성 유지
    jdm_ma_spread_pct:    float = 0.10       # 2026-05-11: 0.20→0.10 (MA 이격 하한 완화, 골든크로스 직후 진입 용이)
    jdm_ma_spread_max_pct: float = 3.5      # MA 이격 비율(%) 상한 — 2026-04-13: 2.5→3.5 (골든크로스 직후 허용 범위 확장)
    jdm_take_profit_pct:  float = 5.0        # 2026-06-08: 2.5→3.0→5.0% (손익비 개선 — 폭 넓혀 큰 수익 추구)
    jdm_stop_loss_pct:    float = -2.0       # 2026-06-08: -1.2→-1.0→-2.0% (손절 여유 확보 — 즉시청산 방지)
    hard_stop_pct:        float = -3.0       # 2026-06-08: -2.0→-3.0% (손절 -2.0%와 분리 — 급락 시 강제청산)
    # [FIX 2026-05-11] 수급 필터 — FID 13 부정확으로 인해 절대값 기반 필터 제거, 순위 기반만 사용
    min_trade_amount:     int = 0                # [DEPRECATED] FID 13 부정확 → 0으로 고정 (더 이상 사용 안 함)
    min_daily_rank:       int = 100              # ✓ 거래대금 상위 순위만 사용 (범위: rank ≤ 100)
    min_daily_volume:     int = 50_000           # 2026-06-05: 100k→50k (HPSP 고가주 거래량 미달 차단 방지)
    min_daily_volume_opening: int = 20_000       # 2026-05-12: OPENING 슬롯 거래량 극도 완화 (100k→20k, 신호 기회 극대)
    
    # [NEW] 데이터 수집 및 AI 필터 제어 (2026-05-04)
    ai_threshold:         float = 0.30       # AI 예상 승률 통과 기준 (0.3 = 데이터 수집을 위해 공격적 진입)
    rs_threshold:         float = -0.3       # 지수 대비 강도(RS) 통과 기준 (지수보다 조금 약해도 진입 허용)
    exploration_mode:     bool  = False      # 데이터 수집 모드 활성화 여부
    
    markets:              tuple = ("0", "10")
    screen_realtime:      str   = "9200"
    display_top_n:        int   = 50    # [2026-05-21] watch_pool_max 와 일치 (50, 증분 업데이트 적용)
    # [진단] 로그 거래대금 상위 샘플 — 매수 후보 전체와 무관(후보는 watch_pool_max·display_top_n 참고)
    diagnostic_sample_n:  int   = 5
    log_dir:              str   = "logs"
    # 등락률이 이 값 **이상**이면 감시·신호·매수 대상에서 제외 (config.py RISK["max_change_pct"]와 동기화)
    max_change_pct:       float = 22.0               # config.py RISK["max_change_pct"] 와 동일
    # 등락률 하한 — 이 값 **미만**이면 감시 대상 제외 (마이너스 반등 허용 시 -1.5 설정)
    min_change_pct:       float = -1.5               # 기본 -1.5%: 소폭 하락 후 반등 종목 포함
    # ScannerWorker: 동일 종목 재 emit 최소 간격(초). 에지 트리거와 병행 (config.py RISK["signal_cooldown_sec"])
    signal_cooldown_sec:  float = 60.0               # 2026-05-26: 45→60초 (신호 과부하 방지, UI 응답성 개선)
    # [NEW] 4중 필터 — JDM 신호 품질 강화
    entry_start_time:     dtime = dtime(7, 0, 0)    # 진입 허용 시작 — 2026-05-11: 08:00→07:00 (신호 기회 확대)
    entry_end_time:       dtime = dtime(15, 30, 0)  # 진입 허용 종료
    # [08:00 조기 시작] 시간 경계
    pre_market_end:       dtime = dtime(9, 0, 0)   # PRE/OPENING 경계 (시간외 종료)
    # PRE_SURGE 파라미터 (08:00~09:00 시간외 단일가)
    pre_surge_chg_min:    float = 2.0    # PRE 최소 등락률 (%) — 시간외에서 이 이상 오른 종목
    pre_surge_chg_max:    float = 20.0   # PRE 최대 등락률 (%) — 상한
    pre_surge_chejan_min: float = 110.0  # PRE 체결강도 하한 (%)
    pre_surge_chejan_max: float = 700.0  # PRE 체결강도 상한 — 극단 급등(고점) 차단 (900%+ 는 이미 과열)
    pre_surge_rsi_max:    float = 88.0   # PRE RSI 상한 — 과매수 진입 차단 (RSI=100 손실 사례 방지)
    breakout_chejan_max:         float = 1500.0  # BREAKOUT 체결강도 상한 — 2026-05-11: 800→1500 (모의투자 환경 보정, 1518% 이상 차단)
    breakout_chejan_max_morning: float = 1800.0  # MORNING 슬롯 상한 — 2026-05-11: 950→1800
    breakout_chejan_max_opening: float = 2000.0  # 2026-05-12: OPENING 슬롯 극한 완화 (거의 모든 체결강도 허용)
    jdm_chejan_max:         float = 2000.0  # JDM_ENTRY 체결강도 상한 (MORNING 이후) — 2026-05-12: 700→2000 (학습 데이터 수집)
    jdm_chejan_max_opening: float = 2000.0  # OPENING 슬롯 체결강도 상한 — 2026-05-12: 800→2000 (신호 기회 극대)
    breakout_rsi_max:     float = 80.0   # BREAKOUT RSI 상한 — 과매수 진입 차단 (2026-04-15 분석, OPENING은 breakout.py에서 스킵)
    # OPENING_SURGE 파라미터 (09:00~09:16 정규장 초반, 캔들 부족 구간)
    opening_surge_chg_min:    float = 2.0    # 1.0→2.0 (OPENING 최소 등락률 강화, 미약한 신호 차단)
    opening_surge_chejan_min: float = 140.0  # 120→140 (OPENING 체결강도 상향)
    opening_surge_vol_mult:   float = 1.5    # 1.2→1.5 (OPENING 거래량 배수 강화)
    # [2026-06-15] 개장 추세 품질 필터 강화 — OPENING 승률 26% 기반
    opening_momentum_min:     float = 0.25   # 0.10→0.25 (단발 상승 차단, 추세 누적 필요)
    opening_watch_score_min:  float = 0.25   # 0.10→0.25 (관찰 이력 없는 종목 진입 차단)

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
    min_chejan_strength:  float = 110.0             # 체결강도 하한 (%) — 공격적 진입을 위해 110%로 하향
    volume_surge_mult:    float = 1.5               # 분봉 거래량 배수 (직전 5분 평균 대비)
    max_disparity_pct:    float = 5.0               # MA20 이격도 상한 (%)
    # [NEW] OR 전략 + 공격형 필터
    prev_close_min_ratio: float = 0.98              # 조건A: V자반등 최소 비율 (시가 대비 -2% 이내)
    entry_open_surge_max: float = 15.0             # 2026-05-11: 10.0→15.0% (공격적 완화, 시가대비 상승 제한 완화)
    vi_approach_chg_pct:  float = 7.0               # 조건B: VI 직전 등락률 기준 (%)
    volume_1min_surge_mult: float = 1.2             # 최근 1분 거래량 급증 배수 (직전 10분 평균 대비) — 기존 1.5→1.2 완화
    volume_surge_lookback: int = 10                 # 직전 N분 평균 계산 구간
    # [NEW] 스코어링 로직 보너스 기준
    scoring_vol_surge_bonus: float = 2.0            # 이 배수 이상 폭발 시 혜택
    scoring_rank_bonus: int = 10                    # 상위 N위 이내 대장주 시 혜택
    ma_alignment_time:    dtime = dtime(10, 0, 0)  # 2026-05-12: 09:30→10:00 (OPENING 슬롯 연장, 학습 데이터 수집)
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
    #   (market_crash_pct -2.0%보다 여유있는 1단계 차단 — config.py RISK["market_index_block_pct"])
    index_block_pct:   float = -2.0   # 2026-05-13: -2.5→-2.0 (약세 신호 차단, 허위양성 감소)
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
    # 구간 경계: OPENING(09:05~10:00) / MORNING(10:00~11:00) / MIDDAY(11:00~13:00) / AFTERNOON(13:00~14:30)
    # entry_start_time(09:05), ma_alignment_time(10:00), entry_end_time(14:30) (2026-05-12: 학습 데이터 수집 확대)
    slot_morning_end:   dtime = dtime(11, 0, 0)   # MORNING 종료 / MIDDAY 시작
    slot_midday_end:    dtime = dtime(13, 0, 0)   # MIDDAY 종료 / AFTERNOON 시작
    # 구간별 등락률 상한 (%) — ScannerWorker prefilter + 개별 루프에 동시 적용
    max_change_pct_opening:   float = 15.0   # 09:05~10:00 장초반 (2026-06-04: 25→15 고점진입 차단)
    max_change_pct_morning:   float = 20.0   # 10:00~11:00 핵심 오전 (2026-05-12: 15→20 상향)
    max_change_pct_midday:    float = 15.0   # 11:00~13:00 점심 (2026-05-12: 12→15 상향)
    max_change_pct_afternoon: float = 8.0   # 2026-05-13: 12→8 (오후 약세 신호 차단, 손실 감소)
    # 구간별 체결강도 하한 (%) — 2026-06-15: 실적 데이터 기반 재조정 (10:00~11:00 집중)
    min_chejan_strength_opening:   float = 140.0      # 100→140 (OPENING 승률 26% → 강한 매수세만 허용)
    min_chejan_strength_morning:   float = 90.0       # 유지 (10:00~11:00 유일한 흑자 구간)
    min_chejan_strength_midday:    float = 120.0      # 90→120 (MIDDAY 수익 거의 없음 → 기준 강화)
    min_chejan_strength_afternoon: float = 130.0      # 유지
    # 구간별 거래량 급증 배수 (직전 N분 평균 대비)
    volume_surge_mult_opening:   float = 1.5          # 1.2→1.5 (OPENING 거래량 기준 강화)
    volume_surge_mult_morning:   float = 1.5          # 유지
    volume_surge_mult_midday:    float = 1.2          # 유지
    volume_surge_mult_afternoon: float = 1.2          # 유지
    # 구간별 RSI 진입 하한 — 2026-06-15: OPENING·MIDDAY 기준 상향 (데이터 근거)
    jdm_rsi_entry_min_opening:   float = 50.0     # 30→50 (OPENING RSI 무방비 진입 차단)
    jdm_rsi_entry_min_morning:   float = 38.0     # 유지 (흑자 구간 기준 유지)
    jdm_rsi_entry_min_midday:    float = 50.0     # 40→50 (MIDDAY 진입 기준 강화)
    jdm_rsi_entry_min_afternoon: float = 62.0     # 55→62 (오후 강한 모멘텀만 진입)
    # [P2] 구간별 익절 목표 (%) — (레거시, 트레일 스탑으로 대체)
    tp_pct_opening:   float = 2.0
    tp_pct_morning:   float = 2.5
    tp_pct_midday:    float = 3.0
    tp_pct_afternoon: float = 3.5
    # [Trail] 고점 추적 트레일링 스탑 파라미터
    # [2026-06-09] 구조 개편: trail을 "3%+ 구간 수익 잠금"으로 역할 재정의
    #   2% 미만 빠른 반전 → trail 미활성, stop_loss(-2%)가 담당
    #   2%+ 상승 후 반전 → trail 활성, 수익 일부 보호
    #   5%+ 달리는 종목 → tier2(3.0%)로 여유롭게 추적
    trail_activation_pct: float = 3.0   # 2026-06-15: 1.5→3.0 (trail_pct_tier1=2.5%이므로 activation>=3.0이어야 trail_price>매입가 보장)
    trail_pct_tier1:      float = 2.5   # 2026-06-09: 1.2→2.5 (Tier1 폭 대폭 확대 — 고점 2.5% 이내 조정 허용)
    trail_tier1_max:      float = 5.0   # 2026-06-09: 3.0→5.0 (tier2 진입 늦춤 — 5% 수익 전까지 tier1 유지)
    trail_pct_tier2:      float = 3.0   # 2026-06-09: 2.0→3.0 (Tier2 폭 확대 — 5~8% 구간 여유 추적)
    trail_tier2_max:      float = 8.0   # 유지 (tier2/tier3 경계: 8% 수익 도달 시 tier3 진입)
    trail_pct_tier3:      float = 3.0   # 유지 (Tier3 폭: 고점 대비 3% — 큰 수익은 여유있게)
    # [NEW] 체결 가속도(Execution Velocity) 필터 — 10초 체결량 급증 확인
    # [2026-06-02] False→True 재활성화: vel_ratio 0.05 수준 종목(포스코DX, 삼화콘덴서) 손실 반복
    exec_velocity_enabled: bool  = True    # 체결 가속도 필터 활성화
    exec_velocity_mult:    float = 0.3     # 기본 배수 (슬롯별 미설정 시 fallback)
    exec_velocity_mult_opening:   float = 0.5   # OPENING: 2026-06-04 0.1→0.5 (에너지 확인 강화)
    exec_velocity_mult_morning:   float = 0.3   # MORNING: 기본 적용
    exec_velocity_mult_midday:    float = 0.3   # MIDDAY: 기본 적용
    exec_velocity_mult_afternoon: float = 0.5   # AFTERNOON: 에너지 더 필요
    exec_velocity_disabled_opening: bool = False  # OPENING 슬롯도 필터 적용
    # [2026-06-02] 신호가 대비 체결가 슬리피지 상한 (초과 시 즉시 매도)
    max_entry_slippage_pct: float = 1.5  # 1.5% 초과 시 진입 취소 (기존 3.0% → 강화)

    # ── D전략: 호가 압력 필터 ────────────────────────────────────────────────
    # [2026-06-02] 매수2~3호가 물량 / 매도2~3호가 물량 비율로 지지선 강도 판단
    hoga_pressure_enabled: bool  = True   # 호가 압력 필터 활성화
    hoga_pressure_min:     float = 1.3    # 최소 압력비 (매수 > 매도 × 1.3배)
    hoga_pressure_min_opening: float = 1.8  # OPENING 슬롯 강화 (2026-06-04: 1.3→1.8, 강한 매수벽 필수)
    # [2026-06-04 Phase3] 매수1호가 우상향 기울기 최소값 — 음수면 매수세 약화
    bid1_slope_min_opening: float = 0.0   # 0.0: 하락 기울기만 차단 (보합/상승은 허용)
    # 호가 데이터 미수신 시(hoga_ready=False) 필터 스킵 — 데이터 없으면 차단하지 않음

    # ── C전략: 갭 상승 첫 눌림목 진입 ──────────────────────────────────────
    # [2026-06-02] 시가 갭 2~8% 상승 → 첫 음봉 고점 돌파 시 진입
    gap_pullback_enabled:    bool  = True    # 갭 눌림목 전략 활성화
    gap_pullback_min_pct:    float = 2.0     # 갭 상승 최소 % (전일종가 대비)
    gap_pullback_max_pct:    float = 8.0     # 갭 상승 최대 %
    gap_pullback_start:      str   = "9:30"  # 진입 허용 시작 시각 (HH:MM)
    gap_pullback_end:        str   = "10:30" # 진입 허용 종료 시각
    gap_pullback_floor_pct:  float = 1.0     # 시가 대비 하락 허용 범위 (%) — 초과 시 갭 붕괴로 판단
    gap_pullback_vol_surge:  float = 1.5     # 음봉 이후 회복봉 거래량 급증 기준 배수
    gap_pullback_min_trend_level: int = 2    # 2026-06-15: lv0~1 GAP_PULLBACK 손절 다발 → lv2 이상만 허용
    gap_pullback_vel_ratio_min:  float = 1.0  # 2026-06-15: vel<1.0 GAP_PULLBACK 승률 저하 → 1.0 이상만 허용

    # ── A전략: PULLBACK MTF 연동 ─────────────────────────────────────────────
    pullback_mtf_check: bool = True  # PULLBACK 전략에서 5분봉 방향 일치 확인
    # [NEW] ATR 기반 트레일링 스탑
    atr_trail_enabled:        bool  = True   # ATR 트레일 스탑 활성화
    atr_trail_activation_pct: float = 1.5   # ATR 트레일 발동 최소 이익 (%, 기존 trail_activation_pct와 동일)
    atr_trail_multiplier:     float = 1.5   # trail_line = peak_price - multiplier × ATR14
    # [NEW] 섹터 쏠림 방지
    sector_max_positions:     int   = 2     # 동일 업종명 최대 동시 보유 종목 수
    # [NEW] 자금 관리 (Sizing) 파라미터
    position_sizing_mode:     str   = "EQUAL"   # EQUAL, FIXED, RISK
    fixed_order_amount:       int   = 1_500_000 # FIXED 모드 시 1회 주문 금액 (원)
    risk_per_trade_pct:       float = 1.0       # RISK 모드 시 회당 리스크 비율 (총자산의 %)
    # [선행점수] JDM_LEADING 필터 임계값
    # PRIMARY 조건(bs/cr/vb/iv 중 하나 이상) 통과 후 가중합 최소값
    # 0.05는 bs 단독 0.30(score≈0.057)만으로도 통과 → 허위양성 과다
    # 0.15: bs+보조 1개 이상 복합 신호 필수 (단독 primary 차단)
    leading_score_min: float = 0.15   # 2026-06-09: 0.05→0.15 (bs 단독 진입 차단, 복합 신호 필수)

    # [추세추종] JDM 진입 조건 추세 레벨 오버라이드 파라미터 (2026-04-20)
    # [FIX 2026-05-27] 2 → 99로 사실상 무력화.
    # 이전엔 trend_lv≥2에서 캔들 패턴/EMA 이격 완화/RSI 상한 완화가 모두 적용 → 정점 진입 원인.
    # 5/27 빛과전자 +14.7% 이격 진입, 나무기술 +96.5% 이격 진입 등 사고 발생.
    # Lv3여도 캔들 패턴 검증, 일반 이격 상한, 일반 RSI 상한 모두 적용.
    jdm_candle_skip_trend_level:   int   = 99    # 사실상 비활성 (이전: 2)
    jdm_rsi_high_trend:            float = 70.0  # trend_level≥2 시 RSI 상한 (2026-06-04: 80→70)
    jdm_rsi_high_breakout:         float = 72.0  # ATR1.5 돌파 확인 시 RSI 상한 (2026-06-04: 82→72)
    jdm_rsi_high_opening_trend3:   float = 99.0  # 미사용 (코드에서 참조 없음, 보존)
    jdm_rsi_high_with_precursor:   float = 65.0  # 선행 패턴 있을 때 RSI 상한 완화 (2026-06-04 신규)
    jdm_rsi_high_strong_leading:   float = 75.0  # leading≥0.50 강한 선행 시 RSI 상한 (2026-06-05 신규)
    jdm_rsi_high_weak_leading:     float = 75.0  # leading≥0.05 약한 선행 시 RSI 상한 (2026-06-05 신규)
    jdm_rsi_entry_min_trend:       float = 45.0  # trend_level≥2 시 RSI 하한 완화 (슬롯 기준값 → 45)
    ema_disp_max_pct_trend:        float = 7.0   # trend_level≥2 EMA10/EMA20 이격 상한 완화
    price_ema_disp_max_pct_trend:  float = 6.0   # trend_level≥2 현재가/EMA10 이격 상한 완화
    ema20_exit_enabled:            bool  = True   # 2026-05-18: False→True (EMA20 이탈 시 즉시 청산, 추세 변화 감지)
    # [NEW] 보유 시간 상한 (타임컷) — 2026-06-15: 25→35 (15~30분 보유 구간이 흑자 전환점)
    time_cut_minutes:     int   = 35   # 25→35 (데이터: 15분+ 보유 시 승률 55%, 평균 +0.39%)
    # [NEW] 당일 서킷브레이커 — 손절 N회 이후 신규 진입 전면 차단 (0=비활성)
    daily_max_stoplosses: int   = 0    # 예: 5 → 당일 5회 손절 시 매매 중단 (연속 26패 방지)
    # [NEW] 시간대별 청산 파라미터 — 점심시간(MIDDAY 11:00~13:00) 저변동성 구간 대응
    trail_activation_pct_midday: float = 2.5   # 트레일 활성화 기준 완화 (기본 1.5%)
    trail_pct_tier1_midday:      float = 1.2   # 트레일 Tier1 폭 확대 (기본 0.8%)
    trail_pct_tier2_midday:      float = 1.8   # 트레일 Tier2 폭 확대 (기본 1.2%)
    time_cut_minutes_midday:     int   = 40    # 30→40 (MIDDAY 저변동성, 더 기다려야 수익 실현)
    stop_loss_pct_midday:        float = -1.5  # 손절 완화 (기본 -1.2%)
    # [NEW] 시간대별 청산 파라미터 — 장초반(OPENING 09:00~09:30) 과열 구간 대응
    stop_loss_pct_opening:       float = -1.5  # 손절 완화 (기본 -1.2%)
    # [2026-05-26] OPENING 갭 상승 종목 동적 손절 — 갭 크기 비례
    # 진입 시 갭 % 기준으로 손절선을 확대 (갭이 클수록 변동폭이 크므로 손절 여유 필요)
    gap_dynamic_sl_enabled:      bool  = True   # 갭 동적 손절 활성화
    gap_sl_tier1_pct:            float = 5.0    # 갭 2~5% 구간 상한
    gap_sl_tier1_stop:           float = -2.0   # 갭 2~5% → 손절 -2.0%
    gap_sl_tier2_pct:            float = 10.0   # 갭 5~10% 구간 상한
    gap_sl_tier2_stop:           float = -2.5   # 갭 5~10% → 손절 -2.5%
    gap_sl_tier3_stop:           float = -3.0   # 갭 10%+ → 손절 -3.0%
    # 동적 손절 확대에 비례한 익절 목표 상향 (갭 커질수록 목표도 크게)
    gap_tp_tier1_pct:            float = 5.0    # 2026-06-08: 3.5→5.0% (기본 익절과 동일)
    gap_tp_tier2_pct:            float = 6.0    # 2026-06-08: 4.5→6.0% (갭 클수록 목표 높게)
    gap_tp_tier3_pct:            float = 7.0    # 2026-06-08: 5.5→7.0% (갭 10%+ 종목)
    # [NEW] 오후(13:00~14:30) 청산 파라미터 — 변동성 높고 손실 가능성 큼
    time_cut_minutes_afternoon:  int   = 20    # 15→20 (오후도 타임컷 연장, 15분은 너무 짧음)
    stop_loss_pct_afternoon:     float = -1.0  # 2026-05-18: 신규 (오후 손절 강화)
    # [NEW 2026-05-19] 동일 종목 재진입 차단 (손절 후 복구 기간)
    loss_exit_cooldown_minutes:   float = 60.0  # 2026-05-26: 20.0→60.0 (재손절 패턴 차단)
    # [NEW 2026-06-08] 당일 진입 이력 — 청산 사유 무관 재진입 차단 (타임컷/트레일스탑 후 재진입 방지)
    today_entry_cooldown_minutes: float = 90.0  # 6/8 분석: 미래에셋 2회·비보존 3회 반복 손실
    # [NEW 2026-05-19] 최근 상승도 차단 (뒤늦은 진입 방지)
    recent_candle_max_1min_pct:  float = 2.0   # 지난 1분 내 상승도 >= 2% → 진입 거절
    recent_candle_max_5min_pct:  float = 5.0   # 지난 5분 내 상승도 >= 5% → 진입 거절
    # [방향 A 2026-06-01] 체결강도 강할 때 상승도 허용 범위 확대
    surge_chejan_bonus_threshold:    float = 900.0  # 이 이상이면 상승도 허용 범위 확대
    recent_candle_max_1min_pct_strong: float = 3.0  # 체결강도 900%+ 시 1분 상한 (2→3%)
    recent_candle_max_5min_pct_strong: float = 15.0 # 체결강도 900%+ 시 5분 상한 (5→15%)
    # [2026-06-05] 선행점수 높으면 급등 차단 면제 — 지수 역행 급등 종목 포착
    surge_exempt_leading_min:        float = 0.30  # 선행점수 >= 이 값이면 RECENT_SURGE 면제
    # [방향 B 2026-06-01] WARMUP 모드 강화 기준
    jdm_warmup_chejan_min:       float = 920.0  # WARMUP 시 체결강도 최소 (일반 900 → 920)
    jdm_warmup_trade_amount_mult: float = 2.0   # WARMUP 시 거래대금 배수 (OPENING 1.2 → 2.0)
    # [Phase A 2026-05-19] 시가 갭 상승 감지 (갭 리버설 패턴)
    gap_up_min_pct:              float = 2.0   # 전일종가 대비 시가 상승 최소 %
    gap_up_max_pct:              float = 8.0   # 갭 상승 상한 (갭 너무 크면 불안정)
    gap_reversal_enabled:        bool  = True  # 갭 리버설 패턴 활성화
    # [Phase A 2026-05-19] 거래대금 가속도 필터 (원화 기준, 소형주 노이즈 필터)
    trade_amount_surge_enabled:  bool  = True  # 거래대금 급증 필터 활성화
    trade_amount_surge_mult:     float = 2.5   # 2026-05-29: 4.0→2.5 (일봉락+trend_lv 보강으로 다른 안전장치 확보, LG전자+19% 등 87종목 차단 해소)
    # [2026-06-01] PULLBACK KOSDAQ 약세 차단 기준
    # [2026-06-02] -2.0 → -2.5 완화: 오늘 아남전자+10%·네이처셀+13%·나무기술+7%가
    # KOSPI -2.04~-2.49%에도 계속 상승 → -2.0% 기준이 너무 빡빡함
    # [2026-06-02] PULLBACK 지수 필터 비활성화 — 지수와 무관하게 개별 종목 추세로 판단
    # 오늘 KOSPI -2%에도 아남전자+10%·네이처셀+13%·나무기술+7% → 지수 차단이 손해
    pullback_index_filter_enabled: bool  = False  # True이면 지수 약세 시 차단 (기본 비활성)
    pullback_kosdaq_min_pct:       float = -2.5   # 활성화 시 KOSDAQ 하한
    pullback_kospi_min_pct:        float = -2.5   # 활성화 시 KOSPI 하한
    # [2026-06-02 선택B] 진짜 눌림목 조건
    # [2026-06-08] 강화: EMA20 너무 깊이 이탈 차단, RSI 하한 상향, trend_lv 최소 3, 반등 에너지 강화
    pullback_dist_min_pct:   float = -1.5  # 2026-06-08: -3.0→-1.5% (추세 붕괴 수준 이탈 차단)
    pullback_dist_max_pct:   float =  2.0  # EMA20 이격 상한 (유지)
    pullback_rsi_min:        float = 50.0  # 2026-06-08: 45→50 (너무 약한 반등 차단)
    pullback_rsi_max:        float = 72.0  # RSI 상한 (유지)
    pullback_min_trend_lv:   int   = 3     # 2026-06-08: 신규 — trend_lv 최솟값 (기존 코드의 >=2 → >=3)
    pullback_bounce_energy:  float = 1.2   # 2026-06-08: 0.8→1.2 (반등봉 거래량이 눌림 평균의 1.2배 이상)
    # [2026-06-10] 반등 에너지(체결 가속도) 최소 기준 — 6/9 vel<1.0 손실 5건 기반
    pullback_vel_ratio_min:  float = 0.5   # 이 값 미만 vel_ratio면 PULLBACK 차단 (에너지 없는 반등 거절)

    # [Phase 3 2026-05-28] OverheatPullback 전략 파라미터
    # 백테스트 결론: 중소형주 특성상 기본값(50억/2.0배) 대비 대폭 완화 필요
    overheat_ema_period:                int   = 20
    overheat_atr_period:                int   = 14
    overheat_lookback_minutes:          int   = 15    # 10→15 (과열 이력 추적 확대)
    overheat_min_trading_value_5m_avg:  float = 500_000_000   # 50억→5억 (중소형주 대응)
    overheat_volume_surge_mult:         float = 1.2   # 2.0→1.2 (중소형주 거래대금 특성 반영)
    overheat_level_3_threshold:         float = 1.3   # 1.5→1.3 (과열 기준 완화)
    overheat_level_1_min:               float = 0.3
    overheat_level_1_max:               float = 1.0
    # [2026-06-02] AFTERNOON 과열 종목 차단
    # 당일 이미 N% 이상 오른 종목은 오후 진입 차단 (삼성전자 +7% 사례)
    afternoon_already_up_pct: float = 5.0  # 5% 이상 오른 종목은 오후 JDM_ENTRY 차단

    # ── 60분봉 추세 필터 ─────────────────────────────────────────────────────
    # [2026-06-02] 큰 그림(시간봉) 방향과 역행하는 1분봉 신호 차단
    h1_trend_enabled:     bool  = True   # 60분봉 추세 필터 활성화
    h1_trend_filter:      bool  = True   # 60분봉 하락 시 JDM_ENTRY 차단
    h1_min_bars:          int   = 5      # 최소 60분봉 수 (미달 시 필터 스킵)
    h1_skip_tf1_trend_lv: int   = 3      # 1분봉 trend_lv 이 이상이면 60분봉 필터 스킵 (강세 폭발 허용)

    # [NEW] 전략 실험 옵션
    # 활성 전략 목록: "BREAKOUT", "JDM_ENTRY", "PULLBACK", "EOD"
    # [2026-06-02] BREAKOUT 제거 — "이미 오른 것 확인 후 진입" 구조적 후행성
    # BREAKOUT 승률 낮음 + 신호 11,128건/일(47%) 노이즈 → JDM_ENTRY/PULLBACK/GAP_PULLBACK으로 대체
    enabled_strategies: tuple[str, ...] = ("JDM_ENTRY", "GAP_PULLBACK", "PULLBACK", "EOD", "OVERHEAT_PULLBACK", "MORNING_GOLDENTIME")
    # [2026-06-10] PULLBACK 비중 축소: GAP_PULLBACK 우선, PULLBACK 후순위로 이동
    # 근거: PULLBACK 반복 손실(6/1 33건 21%, 6/8 10건 40%) vs JDM 빅 위너 생산
    # [2026-06-15] MORNING_GOLDENTIME 추가: 09:00~09:30 전용 (내부에서 enabled 플래그로 ON/OFF)
    strategy_order: tuple[str, ...] = ("MORNING_GOLDENTIME", "JDM_ENTRY", "GAP_PULLBACK", "PULLBACK", "EOD", "OVERHEAT_PULLBACK")
    # 분당 최대 신호 발행 수 — 동시 다발 진입 방지 (1분에 최대 N종목)
    max_entries_per_minute: int = 2  # 2026-05-07: 1→5→2 (UI 프리징 해결, 신호 제한)
    # ── 요셉 시그널 추세 필터 ────────────────────────────────────────────────
    yosep_trend_enabled: bool = False  # 2026-05-07: True→False (매수신호 시험, trend_level 필터 제거)
    yosep_ema_period: int = 20
    yosep_atr_period: int = 14
    yosep_volume_lookback: int = 20
    yosep_min_trend_level: int = 0            # 2026-04-16: 1→0 (무추세 종목도 MORNING/MIDDAY 허용)
    yosep_min_trend_level_opening: int = 0   # 2026-05-12: 1→0 (OPENING 슬롯 추세 필터 완전 비활성화, 신호 기회 확대)
    yosep_min_trend_level_afternoon: int = 1  # 2026-04-16: 3→1 (약추세 이상으로 완화, 하루 신호 0건 방지)
    yosep_downtrend_block_atr: float = 0.8    # EMA 아래 ATR*N 이상이면 하락 강세로 차단
    yosep_preset: str = "balanced"            # aggressive | balanced | conservative

    # ── 멀티타임프레임(MTF) 추세 필터 ────────────────────────────────────────
    # [2026-06-02] 1분봉만으로 판단하는 후행성 문제 해결: 5분봉 추세와 방향이 일치할 때만 진입
    mtf_enabled: bool = True                  # MTF 필터 활성화
    mtf_min_5min_bars: int = 3                # 5분봉 최소 필요 개수 (미달 시 필터 스킵)
    mtf_block_on_misalign: bool = True        # 1분/5분 방향 불일치 시 진입 차단
    mtf_skip_opening: bool = True             # OPENING 슬롯은 5분봉 부족으로 스킵
    mtf_skip_tf1_trend_lv: int = 3            # 1분봉 trend_lv 이 이상이면 MTF 필터 스킵 (강세 폭발 허용)


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
    investor_filter_enabled: bool  = False  # 수급 필터 비활성화 — opt10059 TR이 메인스레드 30초 블로킹 유발 (15종목×2s)
    investor_refresh_min:    int   = 10     # opt10059 갱신 주기 (분)
    investor_top_n:          int   = 15     # 수급 조회 대상 상위 N종목 (30→15, TR 부하 절반)
    # score +1 종목: 쿨다운 유지 (우선 처리)
    # score -1 종목: 쿨다운 2배 적용 (우선순위 하향, 차단은 아님)


    # ── 분할 익절 ─────────────────────────────────────────────────────────────
    partial_profit_enabled: bool  = True    # 분할 익절 활성화 여부
    partial_profit_pct:     float = 2.5     # 2026-06-10: 1.5→2.5% (분할익절 후 즉시 손절 패턴 방지)
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
    # [2026-06-10] 빅 위너 포착: trend_lv 충족해도 vel_ratio가 이 값 미만이면 타임컷 면제 제외
    # 5/28 이브이첨단소재(3h9m +14%) 같이 vel 높은 종목은 타임컷 없이 장기 보유
    strong_trend_vel_min:     float = 1.5  # vel_ratio 이상이어야 타임컷 면제 적용


    # ── E전략: 오전 골든타임 집중 매매 (09:00~09:30) ─────────────────────────
    # 대시보드 토글로 활성화/비활성화. 기본 비활성 (False).
    morning_goldentime_enabled:        bool  = False   # 오전 골든타임 전략 ON/OFF
    morning_goldentime_cooldown_sec:   float = 180.0   # 종목별 신호 쿨다운 (초)
    morning_goldentime_min_trade_amount: float = 3_000_000_000  # 최소 거래대금 (30억)
    # Phase 2 (09:00~09:10) 시가 돌파 파라미터
    morning_goldentime_p2_open_rise_max: float = 5.0   # 시가 대비 상승 상한 (%) — 과열 차단
    morning_goldentime_p2_hoga_mult:     float = 1.5   # 매수잔량 / 매도잔량 최소 배수
    morning_goldentime_p2_gap_min:       float = 2.0   # 갭 상승 최소 % (우대, 차단 아님)
    morning_goldentime_p2_gap_max:       float = 8.0   # 갭 상승 최대 % (우대 상한)
    morning_goldentime_p2_chejan_min:    float = 110.0  # 체결강도 하한 (%)
    # Phase 3 (09:10~09:30) 눌림목 파라미터
    morning_goldentime_p3_min_trend_lv:  int   = 2     # 최소 추세 레벨 (lv0~1 손절 다발 방지)
    morning_goldentime_p3_pullback_min:  float = -5.0  # 눌림 허용 하한 (%) — 너무 깊으면 추세 붕괴
    morning_goldentime_p3_pullback_max:  float = -0.5  # 눌림 허용 상한 (%) — 눌림 없으면 패스
    morning_goldentime_p3_vol_decay_check: bool = True # 거래량 급감 확인 (눌림 진정성)
    morning_goldentime_p3_vol_decay_max:   float = 0.8 # 최근 2봉/5봉 평균 비율 상한 (이하면 급감 확인)

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
    #   0 이면 비활성. 양수로 지정 (예: 100000 = -10만원 한도).
    #   risk_manager.check()에서 daily_pnl <= -daily_loss_cut_won 으로 비교함.
    daily_loss_cut_won:     int   = 100_000  # 기본 10만원 (발동 기준: 실현손익 -10만원)
    # [NEW] 포트폴리오 리스크 파라미터
    max_portfolio_unrealized_loss_pct: float = 5.0  # 전체 포지션 합산 미실현 손실률 한도 (%)
    consecutive_loss_limit:            int   = 3    # 연속 손절 발생 시 매수 차단 기준 (회)
    cooling_off_minutes:               int   = 30   # 매수 차단 후 대기 시간 (분)


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

    # ── 스레드 안전성 (reload_adaptive ↔ ScannerWorker) ──────────────
    _cfg_lock = threading.RLock()

    @property
    def jdm_rsi_entry_min(self) -> float:
        return _get_cfg("jdm_rsi_entry_min", 52.0)

    # 필요한 다른 필드들도 이와 같이 @property 로 감싸서 ConfigManager 연동 가능
    # (일단은 update_from_file 로직을 ConfigManager 로 위임하는 것에 집중)

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

    def apply_from(self, other: SmartScannerConfig) -> None:
        """다른 SmartScannerConfig 인스턴스로부터 모든 속성을 스레드 안전하게 복사한다.

        reload_adaptive()에서 setattr 루프 대신 이 메서드를 사용하여
        ScannerWorker와의 데이터 레이스를 방지한다.
        """
        with self._cfg_lock:
            for field_name, new_val in vars(other).items():
                if not field_name.startswith("_"):  # private 필드 제외
                    try:
                        setattr(self, field_name, new_val)
                    except Exception as e:
                        logger.warning("[SmartScannerConfig.apply_from] %s 복사 실패: %s", field_name, e)

    def update_from_file(self, adaptive_path: str = "params/adaptive_params.json") -> bool:
        """ConfigManager 를 통해 설정을 갱신한다."""
        try:
            from app.config_manager import config_manager
            config_manager.reload()
            # 현재 인스턴스의 값들도 동기화 (필요시)
            return True
        except Exception as e:
            logger.error("[SmartScannerConfig] 갱신 실패: %s", e)
            return False

    @classmethod
    def from_adaptive(
        cls,
        adaptive_path: str = "params/adaptive_params.json",
        **overrides,
    ) -> "SmartScannerConfig":
        instance = cls(**overrides) if overrides else cls()
        # [FIX 2026-06-02] JSON 값을 인스턴스 속성에 직접 주입
        # update_from_file()은 config_manager.reload()만 했고
        # 실제 인스턴스 setattr이 빠져 있어 adaptive 값이 무시됐음
        try:
            import json as _json
            with open(adaptive_path, "r", encoding="utf-8") as _f:
                _data = _json.load(_f)
            # __annotations__로 일반 속성만 허용 (property 제외)
            _plain_attrs = set(cls.__annotations__.keys()) if hasattr(cls, "__annotations__") else set()
            for _k, _v in _data.get("params", {}).items():
                if _k not in _plain_attrs:
                    continue
                try:
                    _cur = getattr(instance, _k, None)
                    if _cur is not None:
                        setattr(instance, _k, type(_cur)(_v))
                    else:
                        setattr(instance, _k, _v)
                except Exception:
                    try:
                        setattr(instance, _k, _v)
                    except Exception:
                        pass
        except Exception as _e:
            logger.warning("[SmartScannerConfig.from_adaptive] %s 로드 실패: %s", adaptive_path, _e)
        return instance

