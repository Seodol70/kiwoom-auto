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


class MockSignalFilter(SignalFilter):
    """MagicMock 단위테스트 신호 차단 (실운영 보호)"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        if (sig.code == "000003"
                or "mock" in str(sig.name).lower()
                or "MagicMock" in str(sig.name)):
            logger.warning(
                "[진입거절] %s(%s) 테스트 신호 차단 — 실운영 환경에서 Mock 신호 감지",
                sig.name, sig.code
            )
            return False, f"{sig.code}: 테스트신호차단"
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
    """약한신호(trend_level<2) 차단 — 09:30 이후만 적용"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        now = ctx.now or datetime.now()

        threshold_time = datetime.strptime("09:30", "%H:%M").time()
        if now.time() < threshold_time:
            return True, ""

        # 스냅샷에서 추세 레벨 확인
        snap = ctx.snap_store.get_snapshot(sig.code) if ctx.snap_store else None
        trend_lv = int(getattr(snap, "trend_level", 0) or 0) if snap else 0

        if trend_lv < 2:
            logger.info(
                "[진입거절] %s(%s) 09:30+ 약한신호 차단 — trend_lv=%d (요구: ≥2)",
                sig.name, sig.code, trend_lv
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


class InvestorFilter(SignalFilter):
    """외인/기관 순매매 필터"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        snap = ctx.snap_store.get_snapshot(sig.code) if ctx.snap_store else None
        if not snap:
            return True, ""

        foreign_net = int(getattr(snap, "foreign_net_buy", 0) or 0)
        inst_net = int(getattr(snap, "inst_net_buy", 0) or 0)

        # Phase 1: 둘 다 0이면 데이터 갱신 시도
        if foreign_net == 0 and inst_net == 0:
            if ctx.kiwoom is None:
                logger.debug("[수급갱신스킵] %s — kiwoom API unavailable", sig.code)
                return True, ""

            tr_busy = getattr(ctx.kiwoom, "_tr_busy", False)
            if tr_busy:
                logger.debug("[수급즉시갱신 스킵] %s — TR 사용 중, 기존 데이터로 진행", sig.code)
                return True, ""

            try:
                inv_data = ctx.kiwoom.get_investor_trend(sig.code)
                ctx.snap_store.update_investor(
                    sig.code, inv_data["foreign_net"], inv_data["inst_net"]
                )
                snap = ctx.snap_store.get_snapshot(sig.code)
                if snap:
                    foreign_net = int(getattr(snap, "foreign_net_buy", 0) or 0)
                    inst_net = int(getattr(snap, "inst_net_buy", 0) or 0)
                logger.info(
                    "[수급즉시갱신] %s(%s) 외인=%+d 기관=%+d",
                    sig.name, sig.code, foreign_net, inst_net
                )
            except Exception as e:
                logger.warning(
                    "[수급갱신실패] %s(%s): %s — 기존 데이터로 진행",
                    sig.name, sig.code, e
                )
                return True, ""

        # Phase 2: 외인 && 기관 둘 다 1,000주 이상 순매도면 차단
        INV_NET_SELL_THRESHOLD = -1000
        if foreign_net != 0 or inst_net != 0:
            if foreign_net <= INV_NET_SELL_THRESHOLD and inst_net <= INV_NET_SELL_THRESHOLD:
                reject_msg = (
                    f"[진입거절] {sig.name}({sig.code}) 수급악화 — "
                    f"외인={foreign_net:+,} 기관={inst_net:+,} (둘 다 순매도)"
                )
                logger.info(reject_msg)
                return False, f"{sig.code}: 수급악화 (외인+기관 매도)"

        return True, ""


class NewsFilter(SignalFilter):
    """뉴스 감정(호재/악재) 조회 및 Context에 저장"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        if ctx.news_analyzer is None:
            return True, ""

        try:
            cached = ctx.news_analyzer.get_cached_result(sig.code)
            if cached is not None:
                ctx.news_sentiment = cached.sentiment  # POSITIVE | NEGATIVE | NEUTRAL
                logger.info(
                    "[뉴스감정] %s(%s) %s — %s",
                    sig.name, sig.code, ctx.news_sentiment,
                    cached.headlines[0]["title"][:30] if cached.headlines else "헤드라인 없음"
                )
            else:
                # 아직 분석 안 됨 → 백그라운드 분석 시작
                ctx.news_analyzer.analyze(sig.code, sig.name)
                logger.debug("[뉴스분석요청] %s(%s)", sig.name, sig.code)
        except Exception as e:
            logger.warning("[뉴스감정조회실패] %s(%s): %s", sig.name, sig.code, e)

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


class RSFilter(SignalFilter):
    """상대강도(RS) 필터 — 지수 대비 강도"""

    def validate(self, sig: ScanSignal, ctx: SignalFilterContext) -> tuple[bool, str]:
        snap = ctx.snap_store.get_snapshot(sig.code) if ctx.snap_store else None
        if not snap:
            return True, ""

        rs_score = getattr(snap, "rs_score", 0.0) or 0.0
        rs_thr = float(getattr(ctx.trading_cfg, "rs_threshold", 0.0))

        if rs_score < rs_thr:
            reject_msg = (
                f"[진입거절] {sig.name}({sig.code}) RS필터 거절 "
                f"(RS={rs_score:.2f} < 기준 {rs_thr:.2f})"
            )
            logger.warning(reject_msg)
            return False, f"{sig.code}: RS 필터 거절 ({rs_score:.2f})"

        # 탐색 모드에서 상세 로그
        if getattr(ctx.trading_cfg, "exploration_mode", False):
            logger.debug(
                "[RS필터] %s RS={rs_score:.2f} (기준 {rs_thr:.2f}) → 데이터 수집 통과",
                sig.name
            )

        return True, ""


class SignalFilterChain:
    """신호 필터 체인 (Composite 패턴)"""

    def __init__(self):
        self.filters = [
            OverheatPullbackFilter(),
            MockSignalFilter(),
            OpeningTimeFilter(),
            WeakSignalFilter(),
            EntryStrategyFilter(),
            InvestorFilter(),
            NewsFilter(),
            AIFilter(),
            RSFilter(),
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
