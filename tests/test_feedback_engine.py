"""
test_feedback_engine.py — FeedbackEngine 기본 단위 테스트

대상:
- 빈 fills/audit로 run_daily() → 크래시 없이 FeedbackResult 반환
- classify_losses() 손실 분류 케이스
- write_adaptive_params() → 파일 저장 → load_adaptive_params() 왕복
"""

import json
import pytest
from datetime import date, datetime
from pathlib import Path

from analysis.feedback_engine import (
    FeedbackEngine,
    FeedbackResult,
    AuditRecord,
    LossCat,
    ParamAdjustment,
)


# ── run_daily: 빈 데이터 ─────────────────────────────────────────────────

class TestRunDailyEmpty:

    def test_no_crash_with_empty_logs(self, tmp_path):
        """fills/audit 파일 없어도 FeedbackResult 반환"""
        engine = FeedbackEngine(
            log_dir=str(tmp_path),
            adaptive_path=str(tmp_path / "adaptive_params.json"),
        )
        result = engine.run_daily(date(2026, 5, 4))
        assert isinstance(result, FeedbackResult)

    def test_empty_result_fields(self, tmp_path):
        """빈 데이터 → total_trades=0, profitable=False"""
        engine = FeedbackEngine(
            log_dir=str(tmp_path),
            adaptive_path=str(tmp_path / "adaptive_params.json"),
        )
        result = engine.run_daily(date(2026, 5, 4))
        assert result.total_trades == 0
        assert result.profitable is False
        assert isinstance(result.adjustments, list)
        assert isinstance(result.skipped_reasons, list)


# ── classify_losses: 손실 분류 ────────────────────────────────────────────

def _audit(**kwargs) -> AuditRecord:
    defaults = dict(
        trade_date=date(2026, 5, 4),
        code="005930",
        name="삼성전자",
        signal_type="JDM_ENTRY",
        signal_time="09:15:00",
        signal_price=80_000.0,
        chejan_strength_at_signal=150.0,
        change_pct_at_signal=2.0,
        sell_reason="손절",
        return_pct=-1.5,
        realized_pnl=-12_000.0,
        holding_minutes=8.0,
        final_status="FILLED",
    )
    defaults.update(kwargs)
    return AuditRecord(**defaults)


class TestClassifyLosses:

    def _engine(self, tmp_path) -> FeedbackEngine:
        return FeedbackEngine(
            log_dir=str(tmp_path),
            adaptive_path=str(tmp_path / "adaptive_params.json"),
        )

    def test_empty_audits(self, tmp_path):
        """빈 리스트 → 빈 딕셔너리"""
        engine = self._engine(tmp_path)
        result = engine.classify_losses([])
        assert result == {}

    def test_signal_only_excluded(self, tmp_path):
        """SIGNAL_ONLY 거래는 분류에서 제외"""
        engine = self._engine(tmp_path)
        rec = _audit(final_status="SIGNAL_ONLY")
        result = engine.classify_losses([rec])
        assert result == {}

    def test_opening_noise_detected(self, tmp_path):
        """체결강도 > 5000 + 손실 → OPENING_NOISE"""
        engine = self._engine(tmp_path)
        rec = _audit(chejan_strength_at_signal=9_999.0, realized_pnl=-5_000.0)
        result = engine.classify_losses([rec])
        assert LossCat.OPENING_NOISE in result
        assert len(result[LossCat.OPENING_NOISE]) == 1

    def test_high_entry_chg_detected(self, tmp_path):
        """등락률 > 8% + 손실 → HIGH_ENTRY_CHG"""
        engine = self._engine(tmp_path)
        rec = _audit(change_pct_at_signal=9.5, realized_pnl=-8_000.0)
        result = engine.classify_losses([rec])
        assert LossCat.HIGH_ENTRY_CHG in result

    def test_trail_too_tight_detected(self, tmp_path):
        """트레일 청산 + 수익 < 1% → TRAIL_TOO_TIGHT"""
        engine = self._engine(tmp_path)
        rec = _audit(sell_reason="트레일스탑", return_pct=0.5, realized_pnl=3_000.0)
        result = engine.classify_losses([rec])
        assert LossCat.TRAIL_TOO_TIGHT in result

    def test_early_reversal_detected(self, tmp_path):
        """보유 ≤10분 + 손절 + 손실 → EARLY_REVERSAL"""
        engine = self._engine(tmp_path)
        rec = _audit(holding_minutes=7.0, sell_reason="손절", realized_pnl=-5_000.0)
        result = engine.classify_losses([rec])
        assert LossCat.EARLY_REVERSAL in result
        assert LossCat.STOP_LOSS_HIT in result

    def test_stop_loss_hit_detected(self, tmp_path):
        """손절 + 손실 → STOP_LOSS_HIT"""
        engine = self._engine(tmp_path)
        rec = _audit(sell_reason="손절", realized_pnl=-10_000.0, holding_minutes=20.0)
        result = engine.classify_losses([rec])
        assert LossCat.STOP_LOSS_HIT in result

    def test_profit_trade_no_loss_cat(self, tmp_path):
        """수익 거래 + 정상 청산 → 손실 카테고리 없음"""
        engine = self._engine(tmp_path)
        rec = _audit(
            sell_reason="타임컷",
            realized_pnl=15_000.0,
            return_pct=2.5,
            chejan_strength_at_signal=150.0,
            change_pct_at_signal=2.0,
            holding_minutes=22.0,
        )
        result = engine.classify_losses([rec])
        # STOP_LOSS_HIT, OPENING_NOISE, HIGH_ENTRY_CHG, EARLY_REVERSAL 없어야 함
        for cat in (LossCat.STOP_LOSS_HIT, LossCat.OPENING_NOISE,
                    LossCat.HIGH_ENTRY_CHG, LossCat.EARLY_REVERSAL):
            assert cat not in result


# ── write / read 왕복 ────────────────────────────────────────────────────

class TestAdaptiveParamsRoundtrip:

    def test_write_then_read(self, tmp_path):
        """write_adaptive_params → load_adaptive_params 왕복"""
        adaptive_path = tmp_path / "params" / "adaptive_params.json"
        engine = FeedbackEngine(
            log_dir=str(tmp_path),
            adaptive_path=str(adaptive_path),
        )

        adj = ParamAdjustment(
            param="trail_activation_pct",
            old_val=1.0,
            new_val=1.2,
            reason="손절 다발 — 트레일 활성화 완화",
            category=LossCat.TRAIL_TOO_TIGHT,
            confidence=0.8,
        )
        engine.write_adaptive_params(
            approved=[adj],
            existing_params={"trail_activation_pct": 1.0},
            history=[],
            target_date=date(2026, 5, 4),
        )

        params, history = engine.load_adaptive_params()
        assert "trail_activation_pct" in params
        assert float(params["trail_activation_pct"]) == pytest.approx(1.2)
        assert len(history) == 1
        assert history[0]["param"] == "trail_activation_pct"
        assert history[0]["applied"] is True

    def test_empty_write_preserves_existing(self, tmp_path):
        """approved 빈 리스트 → 기존 파라미터 유지"""
        adaptive_path = tmp_path / "params" / "adaptive_params.json"
        engine = FeedbackEngine(
            log_dir=str(tmp_path),
            adaptive_path=str(adaptive_path),
        )

        engine.write_adaptive_params(
            approved=[],
            existing_params={"stop_loss_pct": -1.5, "trail_activation_pct": 1.0},
            history=[],
            target_date=date(2026, 5, 4),
        )

        params, history = engine.load_adaptive_params()
        assert float(params["stop_loss_pct"]) == pytest.approx(-1.5)
        assert len(history) == 0

    def test_file_is_valid_json(self, tmp_path):
        """저장된 파일이 유효한 JSON"""
        adaptive_path = tmp_path / "params" / "adaptive_params.json"
        engine = FeedbackEngine(
            log_dir=str(tmp_path),
            adaptive_path=str(adaptive_path),
        )
        engine.write_adaptive_params(
            approved=[],
            existing_params={"x": 1.0},
            history=[],
            target_date=date(2026, 5, 4),
        )
        content = json.loads(adaptive_path.read_text(encoding="utf-8"))
        assert "params" in content
        assert "history" in content
        assert "last_updated" in content
