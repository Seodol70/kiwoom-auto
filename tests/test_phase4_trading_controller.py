"""
test_phase4_trading_controller.py — Phase 4 TradingController 신규 메서드 테스트

4가지 신규 청산 전략:
1. _check_partial_profit - 분할익절
2. _check_breakeven_stop - 본절가 스탑
3. _check_ema20_exit - EMA20 이탈
4. _check_trend_decay - 추세소멸
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from app.trading_controller import TradingController, ExitContext
from order.order_manager import Position
from scanner.smart_scanner import SmartScannerConfig


class TestPhase4PartialProfit:
    """분할익절 (_check_partial_profit) 테스트"""

    def test_partial_profit_disabled(self):
        """분할익절 비활성화 시 skip"""
        mock_order_mgr = MagicMock()
        mock_order_mgr.should_exit_on_trend_decay = MagicMock(return_value=False)
        mock_scan_cfg = SmartScannerConfig()
        mock_scan_cfg.partial_profit_enabled = False  # 비활성
        mock_risk_mgr = MagicMock()

        controller = TradingController(mock_order_mgr, mock_scan_cfg, mock_risk_mgr)

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=82_400,  # +3% (목표값 도달)
        )

        ctx = ExitContext(
            sl_pct=1.5,
            trail_activation=0.57,
            trail_tier1=1.1,
            trail_tier2=2.0,
            trail_tier3=3.0,
            time_cut_min=25,
            partial_profit_pct=3.0,
            atr_trail_enabled=False,
        )

        # 비활성화 상태이므로 False 반환
        result = controller._check_partial_profit(pos, 10, ctx)
        assert result is False

    def test_partial_profit_already_sold(self):
        """이미 분할매도 완료 시 skip"""
        mock_order_mgr = MagicMock()
        mock_scan_cfg = SmartScannerConfig()
        mock_scan_cfg.partial_profit_enabled = True

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=82_400,
        )
        pos.partial_sold = True  # 이미 분할매도 완료

        ctx = ExitContext(
            sl_pct=1.5,
            trail_activation=0.57,
            trail_tier1=1.1,
            trail_tier2=2.0,
            trail_tier3=3.0,
            time_cut_min=25,
            partial_profit_pct=3.0,
            atr_trail_enabled=False,
        )

        result = controller._check_partial_profit(pos, 10, ctx)
        assert result is False

    def test_partial_profit_target_not_reached(self):
        """목표 수익률 미도달 시 skip"""
        mock_order_mgr = MagicMock()
        mock_scan_cfg = SmartScannerConfig()
        mock_scan_cfg.partial_profit_enabled = True

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=81_500,  # +1.875% (목표 3% 미만)
        )

        ctx = ExitContext(
            sl_pct=1.5,
            trail_activation=0.57,
            trail_tier1=1.1,
            trail_tier2=2.0,
            trail_tier3=3.0,
            time_cut_min=25,
            partial_profit_pct=3.0,
            atr_trail_enabled=False,
        )

        result = controller._check_partial_profit(pos, 10, ctx)
        assert result is False  # 목표 미도달


class TestPhase4BreakevenStop:
    """본절가 스탑 (_check_breakeven_stop) 테스트"""

    def test_breakeven_stop_disabled(self):
        """본절가 스탑 비활성화"""
        mock_order_mgr = MagicMock()
        mock_scan_cfg = SmartScannerConfig()
        mock_scan_cfg.breakeven_stop_enabled = False

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=79_000,
        )

        result = controller._check_breakeven_stop(pos, 10)
        assert result is False

    def test_breakeven_stop_before_partial_sell(self):
        """분할매도 전에는 적용 안 함"""
        mock_order_mgr = MagicMock()
        mock_scan_cfg = SmartScannerConfig()
        mock_scan_cfg.breakeven_stop_enabled = True

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=79_000,
        )
        pos.partial_sold = False  # 분할매도 전

        result = controller._check_breakeven_stop(pos, 10)
        assert result is False


class TestPhase4EMA20Exit:
    """EMA20 이탈 청산 (_check_ema20_exit) 테스트"""

    def test_ema20_exit_disabled(self):
        """EMA20 이탈 비활성화"""
        mock_order_mgr = MagicMock()
        mock_scan_cfg = SmartScannerConfig()
        mock_scan_cfg.ema20_exit_enabled = False

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=78_000,  # EMA20 아래
        )

        result = controller._check_ema20_exit(pos, 10)
        assert result is False


class TestPhase4TrendDecay:
    """추세소멸 청산 (_check_trend_decay) 테스트"""

    def test_trend_decay_eod_skip(self):
        """EOD 포지션은 skip"""
        mock_order_mgr = MagicMock()
        mock_scan_cfg = SmartScannerConfig()

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=82_400,
        )
        pos.eod_trade = True  # EOD 포지션

        result = controller._check_trend_decay(pos, 10)
        assert result is False

    def test_trend_decay_loss_skip(self):
        """손실 구간은 skip"""
        mock_order_mgr = MagicMock()
        mock_scan_cfg = SmartScannerConfig()

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=78_000,  # -2.5% (손실)
        )

        result = controller._check_trend_decay(pos, 10)
        assert result is False

    def test_trend_decay_disabled_when_no_signal(self):
        """should_exit_on_trend_decay가 False일 때"""
        mock_order_mgr = MagicMock()
        mock_order_mgr.should_exit_on_trend_decay = MagicMock(return_value=False)
        mock_scan_cfg = SmartScannerConfig()

        controller = TradingController(mock_order_mgr, mock_scan_cfg, MagicMock())

        pos = Position(
            code="005930",
            name="삼성전자",
            qty=10,
            avg_price=80_000,
            current_price=82_400,  # +3% (이익 구간)
        )

        result = controller._check_trend_decay(pos, 10)
        assert result is False  # 추세소멸 신호 없음


class TestExitContext:
    """ExitContext 데이터클래스 테스트"""

    def test_exit_context_creation(self):
        """ExitContext 생성 및 필드 확인"""
        ctx = ExitContext(
            sl_pct=1.5,
            trail_activation=0.57,
            trail_tier1=1.1,
            trail_tier2=2.0,
            trail_tier3=3.0,
            time_cut_min=25,
            partial_profit_pct=3.0,
            atr_trail_enabled=False,
        )

        assert ctx.sl_pct == 1.5
        assert ctx.trail_activation == 0.57
        assert ctx.trail_tier1 == 1.1
        assert ctx.trail_tier2 == 2.0
        assert ctx.trail_tier3 == 3.0
        assert ctx.time_cut_min == 25
        assert ctx.partial_profit_pct == 3.0
        assert ctx.atr_trail_enabled is False

    def test_exit_context_midday_values(self):
        """점심시간대 ExitContext 필드 검증"""
        ctx = ExitContext(
            sl_pct=2.0,
            trail_activation=1.0,
            trail_tier1=1.5,
            trail_tier2=2.5,
            trail_tier3=4.0,
            time_cut_min=15,
            partial_profit_pct=2.5,
            atr_trail_enabled=True,
        )

        assert ctx.sl_pct == 2.0  # 점심 손절 더 큼
        assert ctx.time_cut_min == 15  # 점심 타임컷 더 짧음
