"""신호 필터 체인 (Composite 패턴)"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from analysis.feature_engineer import NewsAnalysisResult
    from kiwoom_api import KiwoomAPI
    from order.order_manager import OrderManager
    from scanner.models import ScanSignal
    from scanner.snapshot_store import SnapshotStore
    from scanner.smart_scanner import SmartScannerConfig
    from analysis.risk_manager import RiskManager
    from app.main_window import AppState


logger = logging.getLogger(__name__)


@dataclass
class SignalFilterContext:
    """필터 실행 컨텍스트 (의존성 주입)"""
    order_mgr: OrderManager
    snap_store: SnapshotStore
    trading_cfg: SmartScannerConfig
    risk_mgr: RiskManager
    app_state: Optional[AppState] = None
    kiwoom: Optional[KiwoomAPI] = None
    news_analyzer: Optional["NewsAnalyzer"] = None
    now: Optional[datetime] = None
    opening_entry_times: list[datetime] = field(default_factory=list)
    news_sentiment: str = "NEUTRAL"  # POSITIVE | NEGATIVE | NEUTRAL


class SignalFilter(ABC):
    """신호 필터 인터페이스"""

    @abstractmethod
    def validate(
        self,
        sig: ScanSignal,
        ctx: SignalFilterContext,
    ) -> tuple[bool, str]:
        """
        필터 검증 실행.

        Args:
            sig: 신호 객체
            ctx: 필터 실행 컨텍스트

        Returns:
            (passed: bool, reason: str)
        """
        pass


class OverheatPullbackFilter(SignalFilter):
    """OVERHEAT_PULLBACK 신호 전용 리스크 게이팅 — 당일 리스크 상태 및 섹터 쏠림 최종 점검"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        if sig.signal_type != "OVERHEAT_PULLBACK":
            return True, ""

        # RiskManager 게이팅: 당일 손절컷·이익잠금 활성 시 신규 진입 차단
        if ctx.risk_mgr is not None:
            if getattr(ctx.risk_mgr, "is_loss_cut", False):
                logger.info("[OP거절] %s(%s) 당일 손절컷 활성", sig.name, sig.code)
                return False, f"{sig.code}: OP — 손절컷 활성"
            if getattr(ctx.risk_mgr, "is_profit_lock", False):
                logger.info("[OP거절] %s(%s) 당일 이익잠금 활성", sig.name, sig.code)
                return False, f"{sig.code}: OP — 이익잠금 활성"

        # 섹터 쏠림 게이팅: 동일 섹터 보유 종목이 sector_max_positions 이상이면 차단
        snap = ctx.snap_store.get_snapshot(sig.code) if ctx.snap_store else None
        sector = str(getattr(snap, "sector", "") or "") if snap else ""
        if sector:
            sector_max = int(getattr(ctx.trading_cfg, "sector_max_positions", 2))
            sector_count = sum(
                1 for p in ctx.order_mgr.positions.values()
                if str(getattr(p, "sector", "") or "") == sector
            )
            if sector_count >= sector_max:
                logger.info(
                    "[OP거절] %s(%s) 섹터 쏠림 — %s %d/%d",
                    sig.name, sig.code, sector, sector_count, sector_max
                )
                return False, f"{sig.code}: OP — 섹터 쏠림 ({sector} {sector_count}/{sector_max})"

        logger.info("[OP통과] %s(%s) 눌림목 신호 리스크 게이팅 통과", sig.name, sig.code)
        return True, ""


class OpeningTimeFilter(SignalFilter):
    """개장 1시간(09:00~10:00) 분당 1건 진입 제한"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        now = ctx.now or datetime.now()

        opening_start = datetime.strptime("09:00", "%H:%M").time()
        opening_end = datetime.strptime("10:00", "%H:%M").time()

        if not (opening_start <= now.time() <= opening_end):
            return True, ""

        # 60초 이내 진입 건수 카운트
        one_min_ago = now - timedelta(seconds=60)
        ctx.opening_entry_times = [t for t in ctx.opening_entry_times if t > one_min_ago]

        if len(ctx.opening_entry_times) >= 1:
            logger.debug(
                "[개장1시간거절] %s(%s) — 60초 내 진입 1건 제한 (%d건)",
                sig.name, sig.code, len(ctx.opening_entry_times)
            )
            return False, f"{sig.code}: 개장1시간 진입제한"

        return True, ""


class WeakSignalFilter(SignalFilter):
    """추세 레벨 기반 시간대별 진입 차단

    - OPENING(09:00~09:30): Lv3 차단 — 갭상승 직후 정점 진입 위험
    - 09:30 이후: Lv < 2 차단 — 약세 신호 진입 금지
    """

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        now = ctx.now or datetime.now()

        snap = ctx.snap_store.get_snapshot(sig.code) if ctx.snap_store else None
        trend_lv = int(getattr(snap, "trend_level", 0) or 0) if snap else 0

        opening_start = datetime.strptime("09:00", "%H:%M").time()
        opening_end   = datetime.strptime("09:30", "%H:%M").time()

        # OPENING 구간: Lv3(극강 상승) = 이미 정점 가능성 → 차단
        if opening_start <= now.time() < opening_end:
            if trend_lv == 3:
                logger.info(
                    "[진입거절] %s(%s) OPENING Lv3 차단 — 정점 진입 위험",
                    sig.name, sig.code,
                )
                return False, f"{sig.code}: OPENING Lv3 차단"
            return True, ""

        # 09:30 이후: Lv < 2 차단
        # GAP_PULLBACK/OVERHEAT_PULLBACK은 자체 조건으로 강세 검증하므로 면제
        if now.time() >= opening_end:
            exempt_types = {"GAP_PULLBACK", "OVERHEAT_PULLBACK"}
            if sig.signal_type not in exempt_types and trend_lv < 2:
                logger.info(
                    "[진입거절] %s(%s) 09:30+ 약한신호 차단 — trend_lv=%d (요구: ≥2)",
                    sig.name, sig.code, trend_lv,
                )
                return False, f"{sig.code}: 약한신호 (trend_lv={trend_lv})"

        return True, ""


class EntryStrategyFilter(SignalFilter):
    """EntryStrategy 필터 + Phase1 태깅 및 한도 체크"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        # Phase 태깅: 신호 생성 시각(emitted_at) 09:00~09:30 → Phase1(모닝스캘핑)
        signal_time = getattr(sig, "emitted_at", None) or ctx.now or datetime.now()
        phase1_start = datetime.strptime("09:00", "%H:%M").time()
        phase1_end = datetime.strptime("09:30", "%H:%M").time()

        if phase1_start <= signal_time.time() <= phase1_end:
            sig.entry_phase = 1
            # Phase1 한도 체크
            phase1_max = int(getattr(ctx.trading_cfg, "phase1_max_positions", 3))
            phase1_count = sum(
                1 for p in ctx.order_mgr.positions.values()
                if getattr(p, "entry_phase", 0) == 1
            )
            if phase1_count >= phase1_max:
                logger.debug(
                    "[진입거절] %s(%s) Phase1 한도 — %d/%d",
                    sig.name, sig.code, phase1_count, phase1_max
                )
                return False, f"{sig.code}: Phase1 한도 — {phase1_count}/{phase1_max}"
        else:
            sig.entry_phase = 2

        # EntryStrategy 위임
        if not hasattr(ctx.order_mgr, "_strategy"):
            logger.warning("[EntryStrategyFilter] strategy not found in order_mgr")
            return True, ""

        strategy = getattr(ctx.order_mgr, "_strategy", None)
        if strategy is None:
            return True, ""

        passed, reason = strategy.should_entry(sig, getattr(ctx.order_mgr, "_auto_trading", False))

        if not passed:
            logger.warning(
                "[진입거절] %s(%s) EntryStrategy: %s",
                sig.name, sig.code, reason
            )
            return False, reason

        return True, ""


class AIFilter(SignalFilter):
    """AI 모델 예상승률 필터 (뉴스 감정 가중치 적용)"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        snap = ctx.snap_store.get_snapshot(sig.code) if ctx.snap_store else None
        if not snap:
            return True, ""

        try:
            from analysis.feature_engineer import extract_ml_features
            features = extract_ml_features(sig, snap, ctx.trading_cfg)
        except Exception as e:
            logger.warning("[AIFilter] feature extraction failed for %s(%s): %s", sig.name, sig.code, e)
            return True, ""

        # AI 필터가 없으면 통과
        if not hasattr(ctx.order_mgr, "_ai_filter"):
            return True, ""

        ai_filter = getattr(ctx.order_mgr, "_ai_filter", None)
        if ai_filter is None or not hasattr(ai_filter, "should_enter"):
            return True, ""

        # AI 판정 실행
        ai_thr = float(getattr(ctx.trading_cfg, "ai_threshold", 0.5))

        # 뉴스 감정에 따른 AI 임계값 가중치
        ai_thr_orig = ai_thr
        if ctx.news_sentiment == "POSITIVE":
            ai_thr = ai_thr / 1.15  # 임계값 완화
        elif ctx.news_sentiment == "NEGATIVE":
            ai_thr = min(ai_thr * 1.20, 0.95)  # 임계값 강화 (상한 95%)

        if ai_thr != ai_thr_orig:
            logger.info(
                "[뉴스가중] %s(%s) %s — AI 임계값 %.2f → %.2f",
                sig.name, sig.code, ctx.news_sentiment, ai_thr_orig, ai_thr
            )

        ai_passed, win_rate = ai_filter.should_enter(features, threshold=ai_thr)

        if not ai_passed:
            reject_msg = (
                f"[진입거절] {sig.name}({sig.code}) AI필터 거절 "
                f"(예상승률 {win_rate*100:.1f}% < 기준 {ai_thr*100:.0f}%, 뉴스:{ctx.news_sentiment})"
            )
            logger.warning(reject_msg)
            return False, f"{sig.code}: AI 거절 ({win_rate*100:.0f}%)"

        # 승인 시 로그 (모델 준비 시에만)
        if getattr(ai_filter, "is_ready", False):
            logger.info(
                "[AI승인] %s(%s) 예상승률 {win_rate*100:.1f}% (기준 {ai_thr*100:.0f}%, 뉴스:{ctx.news_sentiment})",
                sig.name, sig.code
            )

        return True, ""


class SignalFilterChain:
    """신호 필터 체인 (Composite 패턴)"""

    def __init__(self):
        # [FIX 2026-06-04 Phase3] 필터 축소: 9개 → 5개
        # 제거: MockSignalFilter (모의투자 아님), InvestorFilter (라깅), NewsFilter (분석 미흡), RSFilter (개별주 우선)
        # 유지: OverheatPullbackFilter, OpeningTimeFilter, WeakSignalFilter, EntryStrategyFilter, AIFilter
        self.filters = [
            OverheatPullbackFilter(),
            OpeningTimeFilter(),
            WeakSignalFilter(),
            EntryStrategyFilter(),
            AIFilter(),
        ]

    def validate(
        self,
        sig: ScanSignal,
        ctx: SignalFilterContext,
    ) -> tuple[bool, str]:
        """
        필터 체인 실행. 첫 실패 지점에서 중단.

        Args:
            sig: 신호 객체
            ctx: 필터 실행 컨텍스트

        Returns:
            (passed: bool, reason: str)
        """
        for filter in self.filters:
            passed, reason = filter.validate(sig, ctx)
            if not passed:
                return False, reason
        return True, ""
