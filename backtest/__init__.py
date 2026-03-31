from .engine import BacktestEngine, BacktestResult, Trade
from .metrics import calc_metrics

# plotly 없는 환경에서 parameter_tuning 실행 가능하도록 lazy import
try:
    from .report import build_chart, print_metrics, save_report
except ImportError:
    build_chart = None
    print_metrics = None
    save_report = None

__all__ = [
    "BacktestEngine", "BacktestResult", "Trade",
    "calc_metrics",
    "build_chart", "print_metrics", "save_report",
]
