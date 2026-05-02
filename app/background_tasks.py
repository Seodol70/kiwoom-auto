import logging
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)

class SystemMonitor(QObject):
    """
    백그라운드 스케줄러 (메인 스레드의 QTimer 기반).
    메모리 정리, 주기적인 상태 핑, 뉴스 큐 드레인 등 UI와 무관한 시스템 작업을 전담합니다.
    """
    connection_check_requested = pyqtSignal()
    news_drain_requested = pyqtSignal()
    cleanup_requested = pyqtSignal()
    health_check_requested = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 연결 상태 확인 (15분)
        self._conn_timer = QTimer(self)
        self._conn_timer.timeout.connect(self.connection_check_requested.emit)
        
        # 뉴스 큐 드레인 (1초)
        self._news_timer = QTimer(self)
        self._news_timer.timeout.connect(self.news_drain_requested.emit)
        
        # 메모리 정리 (1시간)
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.timeout.connect(self.cleanup_requested.emit)

        # 헬스 체크 및 자가 치유 (5분)
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self.health_check_requested.emit)
        
    def start(self):
        """타이머 동작 시작"""
        self._conn_timer.start(900_000)
        self._news_timer.start(1000)
        self._cleanup_timer.start(3_600_000)
        self._health_timer.start(300_000)

    def stop(self):
        """모든 타이머 중지"""
        self._conn_timer.stop()
        self._news_timer.stop()
        self._cleanup_timer.stop()
        self._health_timer.stop()
