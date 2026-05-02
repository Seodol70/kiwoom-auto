from __future__ import annotations
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class StatusReporter:
    """계좌 및 감시 상태를 종합하여 보고용 메시지를 생성한다."""

    def __init__(self, order_mgr, snap_store, today_watch: dict):
        self.om = order_mgr
        self.store = snap_store
        self.today_watch = today_watch

    def generate_report(self) -> str:
        """현재 계좌 수익, 보유 현황, 감시 현황을 텍스트로 요약."""
        now = datetime.now()
        lines = [f"📊 [정기보고] {now:%H:%M:%S}"]
        
        # 1. 자산 현황
        pnl = self.om.daily_realized_pnl
        pnl_str = f"{pnl:+,.0f}원"
        lines.append(f"💰 당일 실현손익: {pnl_str}")
        lines.append(f"💵 가용 예수금: {self.om.cash:,}원")
        
        # 2. 보유 현황
        positions = self.om.positions
        if not positions:
            lines.append("📂 현재 보유 종목 없음")
        else:
            lines.append(f"📂 보유 중 ({len(positions)}종목):")
            for code, pos in positions.items():
                rt = pos.return_pct_vs_avg
                lines.append(f"  - {pos.name}({code}): {rt:+.2f}% ({pos.qty}주)")
        
        # 3. 감시 현황
        watch_cnt = len(self.today_watch)
        mon_cnt = len(self.store)
        lines.append(f"🔍 감시 현황: 포착 {watch_cnt}종목 / 모니터링 {mon_cnt}종목")
        
        return "\n".join(lines)
