"""FeedbackWorker — 장 마감 피드백 루프를 별도 스레드에서 실행."""
from __future__ import annotations

import logging
from datetime import date

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


class FeedbackWorker(QObject):
    """FeedbackEngine + LogAnalyzer를 백그라운드 스레드에서 실행하는 워커."""

    finished = pyqtSignal(object)  # FeedbackResult

    @pyqtSlot()
    def run(self) -> None:
        today = date.today()
        try:
            from analysis.feedback_engine import FeedbackEngine
            from analysis.daily_report import DailyReporter

            engine = FeedbackEngine()
            result = engine.run_daily(today)

            audits = engine.parse_audit(today)
            reporter = DailyReporter()
            report_path = reporter.generate(result, audits)
            result.report_path = str(report_path)

        except Exception as e:
            logger.error("[FeedbackWorker] 피드백 오류: %s", e, exc_info=True)
            from analysis.feedback_engine import FeedbackResult
            result = FeedbackResult(
                date=today, total_realized=0, total_trades=0,
                profitable=False, category_hits={}, adjustments=[],
                skipped_reasons=[f"오류 발생: {e}"], applied=False,
            )

        try:
            from analysis.log_analyzer import LogAnalyzer
            analyzer = LogAnalyzer()
            log_result = analyzer.run(today)
            result.telegram_msg = analyzer.format_telegram_report(
                scanner=log_result.scanner,
                trades=log_result.trades,
                feedback_adjustments=result.adjustments,
                feedback_skipped=result.skipped_reasons,
            )
        except Exception as e:
            logger.warning("[FeedbackWorker] LogAnalyzer 오류: %s", e, exc_info=True)

        self.finished.emit(result)
