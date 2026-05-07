import time
import threading
import logging
from datetime import datetime
from typing import Optional, Any
from scanner.smart_scanner import format_trade_amount_korean, SmartScannerConfig
from scanner.snapshot_store import SnapshotStore
from scanner.smart_scanner import ScanSignal # [FIX] NameError: ScanSignal
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text


logger = logging.getLogger(__name__)
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
