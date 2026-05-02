import re

with open('ui/main_window.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Using regex to find and replace the timer setup blocks
code = re.sub(
    r'        # 연결 상태 확인.*?self\._connection_timer\.start\(900_000\).*?# 지수 급락 감지',
    r'''        from app.background_tasks import SystemMonitor
        self.sys_monitor = SystemMonitor(self)
        self.sys_monitor.connection_check_requested.connect(self._check_connection)
        self.sys_monitor.news_drain_requested.connect(self._drain_news_queue)
        self.sys_monitor.cleanup_requested.connect(self._cleanup_memory)
        self.sys_monitor.start()

        # 지수 급락 감지''',
    code,
    flags=re.DOTALL
)

# Remove old news timer
code = re.sub(
    r'        # 뉴스 분석 결과 드레인.*?self\._news_drain_timer\.start\(1000\)\n',
    '',
    code,
    flags=re.DOTALL
)

# Remove old memory cleanup timer
code = re.sub(
    r'        # \[P2\] 메모리 정리.*?self\._memory_cleanup_timer\.start\(60 \* 60 \* 1000\)  # 1시간\n',
    '',
    code,
    flags=re.DOTALL
)

with open('ui/main_window.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Timer replacement done.")
