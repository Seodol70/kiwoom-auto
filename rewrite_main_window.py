with open('ui/main_window.py', encoding='utf-8') as f:
    lines = f.readlines()

new_imports = """
# UI Components
from ui.components.common import _hline, _NoWheelSpinBox, _NoWheelDoubleSpinBox
from ui.components.header_bar import HeaderBar, ManualBuyDialog
from ui.components.scanner_panel import ScannerPanel
from ui.components.chart_panel import ChartPanel
from ui.components.portfolio_panel import PortfolioPanel
from ui.components.investor_panel import InvestorPanel, ScanStatusBar
from ui.components.log_panel import ScannerLogHandler, SysLogQtHandler, LogPanel

"""

new_lines = lines[:65] + [new_imports] + lines[1444:]

with open('ui/main_window.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("ui/main_window.py has been rewritten.")
