"""
signal_evaluator.py — 신호 평가 통합 게이트 (Facade)

거대했던 로직을 evaluators/ 서브 모듈로 분리하고, 
기존 코드와의 호환성을 위해 동일한 인터페이스로 re-export 합니다.
"""

from typing import Optional, Tuple, TYPE_CHECKING
from datetime import datetime, time as dtime

from .evaluators.common import (
    _resolve_time_slot,
    _get_slot_value,
    check_volume_surge,
    check_chejan_strength,
    check_vwap_filter,
    check_indicator_warmup,
    check_bullish_engulfing,
    check_bullish_pin_bar,
    check_disparity_from_ma,
    check_ema20_filter
)
from .evaluators.jdm import check_jdm_entry, check_jdm_open_breakout, _JdmCtx
from .evaluators.breakout import check_breakout, check_breakout_gate
from .evaluators.surge import check_pre_surge, check_opening_surge, check_opening_scalp
from .evaluators.eod import check_eod_entry
from .evaluators.testa import check_testa_alignment
from .evaluators.pullback import check_pullback_entry
from .evaluators.overheat_pullback import check_overheat_pullback_entry

# TYPE_CHECKING용 임포트 (런타임 순환참조 방지)
if TYPE_CHECKING:
    from scanner.models import StockSnapshot
    from scanner.config import SmartScannerConfig

# 기존 인터페이스 유지 (re-export)
__all__ = [
    'check_breakout',
    'check_breakout_gate',
    'check_jdm_entry',
    'check_jdm_open_breakout',
    'check_pre_surge',
    'check_opening_surge',
    'check_opening_scalp',
    'check_eod_entry',
    'check_testa_alignment',
    'check_pullback_entry',
    'check_overheat_pullback_entry',
    'check_volume_surge',
    'check_chejan_strength',
    'check_vwap_filter',
    'check_indicator_warmup',
    'check_bullish_engulfing',
    'check_bullish_pin_bar',
    'check_disparity_from_ma',
    'check_ema20_filter'
]
