"""
키움 자동매매 실시간 로그 감시 대시보드
사용: python log_monitor.py
"""

# Windows UTF-8 인코딩 설정
import os
import sys
if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
import re
import time
from collections import Counter, deque
from datetime import datetime

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

LOG_FILE     = "logs/scanner.log"
KIWOOM_LOG   = "logs/kiwoom_auto.log"
REFRESH_SEC  = 1.5   # 화면 갱신 주기


# ─────────────────────────────────────────────────────────────────────────────
# 상태 저장소
# ─────────────────────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        # 스캔
        self.last_scan_time     = None
        self.scan_count         = 0
        # 신호 판단
        self.last_eval_time     = None
        self.eval_count         = 0
        # STEP-H
        self.last_steph_time    = None
        self.last_steph_count   = 0
        # 신호 통과
        self.pass_signals: deque = deque(maxlen=10)
        self.pass_total         = 0
        # 탈락 통계 (전체 / 최근 2분 창)
        self.fail_all           = Counter()
        self.fail_window        = Counter()
        self.fail_window_ts     = time.monotonic()
        # 오류
        self.errors: deque      = deque(maxlen=8)
        # 보유 / 잔고 (kiwoom_auto.log)
        self.positions          = 0
        self.cash               = 0
        self.fill_log: deque    = deque(maxlen=6)
        # 연결
        self.connected          = None   # True/False/None
        # 파일 오프셋
        self.scanner_pos        = 0
        self.kiwoom_pos         = 0

    def reset_window(self):
        self.fail_window.clear()
        self.fail_window_ts = time.monotonic()


state = State()


# ─────────────────────────────────────────────────────────────────────────────
# 파일 읽기
# ─────────────────────────────────────────────────────────────────────────────

def _tail_new(path: str, pos_attr: str) -> list[str]:
    """파일에서 pos_attr 위치 이후 새 줄만 읽어 반환."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(getattr(state, pos_attr))
            chunk = f.read()
            setattr(state, pos_attr, f.tell())
        return chunk.splitlines() if chunk else []
    except Exception:
        return []


def _seek_end(path: str, pos_attr: str):
    """초기화: 파일 끝부터 읽도록 오프셋 설정."""
    if os.path.exists(path):
        with open(path, "rb") as f:
            f.seek(0, 2)
            setattr(state, pos_attr, f.tell())


# ─────────────────────────────────────────────────────────────────────────────
# scanner.log 파싱
# ─────────────────────────────────────────────────────────────────────────────

_FAIL_LABEL = {
    "BREAKOUT":  "시가 돌파 미충족",
    "VOL_SURGE": "거래량 부족",
    "CHEJAN":    "체결강도 부족",
    "JDM_TIME":  "진입 시간 외",
}

def parse_scanner_line(line: str):
    parts = line.split("\t")
    if len(parts) < 5:
        return

    ts_str = parts[0]          # "2026-04-02 09:36:00"
    level  = parts[1]          # DEBUG / INFO
    tag    = parts[2]          # FAIL / PASS
    code   = parts[3]
    name   = parts[4]
    ftype  = parts[5] if len(parts) > 5 else ""
    detail = parts[6] if len(parts) > 6 else ""

    # ── 신호 판단 실행 감지 ─────────────────────────────────
    if tag in ("FAIL", "PASS"):
        state.last_eval_time = datetime.now()
        state.eval_count += 1

    # ── 탈락 ────────────────────────────────────────────────
    if tag == "FAIL":
        label = _FAIL_LABEL.get(ftype, ftype)
        if ftype == "JDM":
            if "골든크로스" in detail:
                label = "골든크로스 미충족"
            elif "데이터 부족" in detail:
                label = "분봉 데이터 부족"
            elif "RSI" in detail:
                label = "RSI 범위 초과"
            elif "이격" in detail:
                label = "MA 이격 부족"
            else:
                label = "JDM 기타"
        if label:
            state.fail_all[label] += 1
            state.fail_window[label] += 1

    # ── 신호 통과 ────────────────────────────────────────────
    if tag == "PASS":
        state.pass_total += 1
        state.pass_signals.appendleft({
            "ts":     ts_str[11:19],
            "code":   code,
            "name":   name,
            "detail": detail[:60],
        })


# ─────────────────────────────────────────────────────────────────────────────
# kiwoom_auto.log / 콘솔 출력 파싱
# ─────────────────────────────────────────────────────────────────────────────

def parse_kiwoom_line(line: str):
    # 주기 스캔 완료
    if "[주기 스캔] 완료" in line or "[주기 스캔] 시작" in line:
        state.last_scan_time = datetime.now()
        if "완료" in line:
            state.scan_count += 1

    # STEP-H
    m = re.search(r"\[STEP-H async\] 완료 — 총 (\d+)종목", line)
    if m:
        state.last_steph_time  = datetime.now()
        state.last_steph_count = int(m.group(1))

    # 잔고 동기화
    m = re.search(r"예수금 ([\d,]+)원.*보유 (\d+)종목", line)
    if m:
        state.cash      = int(m.group(1).replace(",", ""))
        state.positions = int(m.group(2))

    # 체결
    if "체결 —" in line or "매도체결" in line or "매수체결" in line:
        ts = datetime.now().strftime("%H:%M:%S")
        state.fill_log.appendleft(f"[{ts}] {line.strip()[-80:]}")

    # 오류
    if any(k in line for k in ("ERROR", "Exception", "Traceback", "오류")):
        ts = datetime.now().strftime("%H:%M:%S")
        state.errors.appendleft(f"[{ts}] {line.strip()[:100]}")

    # 연결
    if "로그인 성공" in line:
        state.connected = True
    if "연결 끊김" in line or "재로그인" in line:
        state.connected = False


# ─────────────────────────────────────────────────────────────────────────────
# 대시보드 렌더링
# ─────────────────────────────────────────────────────────────────────────────

def _elapsed(dt) -> str:
    if dt is None:
        return "—"
    s = int((datetime.now() - dt).total_seconds())
    if s < 60:
        return f"{s}초 전"
    return f"{s//60}분 {s%60}초 전"


def _scan_status() -> Text:
    if state.last_scan_time is None:
        return Text("⏳ 대기 중", style="yellow")
    elapsed = (datetime.now() - state.last_scan_time).total_seconds()
    if elapsed < 90:
        return Text(f"✅ 정상  ({int(elapsed)}초 전)", style="green")
    return Text(f"🔴 지연!  ({int(elapsed)}초 경과)", style="bold red")


def _eval_status() -> Text:
    if state.last_eval_time is None:
        return Text("⏳ 아직 시작 안됨", style="yellow")
    elapsed = (datetime.now() - state.last_eval_time).total_seconds()
    if elapsed < 5:
        return Text(f"✅ 실행 중  (누적 {state.eval_count:,}회)", style="green")
    if elapsed < 30:
        return Text(f"⚠️  {int(elapsed)}초간 조용함", style="yellow")
    return Text(f"🔴 {int(elapsed)}초간 멈춤!", style="bold red")


def _conn_status() -> Text:
    if state.connected is None:
        return Text("— 알 수 없음", style="dim")
    return Text("✅ 연결됨", style="green") if state.connected \
        else Text("🔴 연결 끊김!", style="bold red")


def build_dashboard() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )
    layout["body"]["left"].split_column(
        Layout(name="status"),
        Layout(name="fail"),
    )
    layout["body"]["right"].split_column(
        Layout(name="pass"),
        Layout(name="fills"),
        Layout(name="errors"),
    )

    # ── 헤더 ──────────────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    layout["header"].update(Panel(
        Text(f"  📡 키움 자동매매 실시간 감시 대시보드       {now}",
             style="bold cyan"),
        style="cyan",
    ))

    # ── 상태 ──────────────────────────────────────────────────────────────
    tbl = Table(box=None, show_header=False, padding=(0, 1))
    tbl.add_column(width=16)
    tbl.add_column()
    tbl.add_row("API 연결",    _conn_status())
    tbl.add_row("주기 스캔",   _scan_status())
    tbl.add_row("신호 판단",   _eval_status())
    steph_txt = (
        f"✅ {_elapsed(state.last_steph_time)}  ({state.last_steph_count}종목)"
        if state.last_steph_time else "⏳ 대기"
    )
    tbl.add_row("분봉 로딩",   Text(steph_txt, style="green" if state.last_steph_time else "yellow"))
    tbl.add_row("보유 종목",   Text(f"{state.positions}개", style="bold"))
    tbl.add_row("예수금",      Text(f"{state.cash:,}원" if state.cash else "—"))
    tbl.add_row("스캔 횟수",   Text(f"{state.scan_count}회  /  신호통과 {state.pass_total}건"))
    layout["status"].update(Panel(tbl, title="[bold]📊 시스템 상태", border_style="blue"))

    # ── 탈락 통계 ─────────────────────────────────────────────────────────
    # 창 초기화 (2분)
    if time.monotonic() - state.fail_window_ts > 120:
        state.reset_window()

    ftbl = Table(box=None, show_header=True, padding=(0, 1))
    ftbl.add_column("탈락 이유",  style="dim")
    ftbl.add_column("전체",   justify="right", style="red")
    ftbl.add_column("최근2분", justify="right", style="yellow")
    for reason, cnt in state.fail_all.most_common(8):
        w = state.fail_window.get(reason, 0)
        ftbl.add_row(reason, str(cnt), str(w) if w else "—")
    layout["fail"].update(Panel(ftbl, title="[bold]❌ 탈락 이유 통계", border_style="red"))

    # ── 신호 통과 내역 ────────────────────────────────────────────────────
    ptbl = Table(box=None, show_header=True, padding=(0, 1))
    ptbl.add_column("시각",   style="dim", width=8)
    ptbl.add_column("종목",   width=12)
    ptbl.add_column("내용",   style="green")
    for s in list(state.pass_signals)[:6]:
        ptbl.add_row(s["ts"], f"{s['name']}({s['code']})", s["detail"][:45])
    if not state.pass_signals:
        ptbl.add_row("—", "—", "아직 없음")
    layout["pass"].update(Panel(ptbl, title="[bold]🚨 신호 통과 내역", border_style="green"))

    # ── 체결 로그 ─────────────────────────────────────────────────────────
    fill_txt = "\n".join(list(state.fill_log)) if state.fill_log else "체결 없음"
    layout["fills"].update(Panel(
        Text(fill_txt, style="cyan"),
        title="[bold]💰 체결 내역", border_style="cyan",
    ))

    # ── 오류 ──────────────────────────────────────────────────────────────
    err_txt = "\n".join(list(state.errors)) if state.errors else "✅ 오류 없음"
    layout["errors"].update(Panel(
        Text(err_txt, style="red" if state.errors else "green"),
        title="[bold]⚠️  최근 오류", border_style="red" if state.errors else "green",
    ))

    # ── 풋터 ──────────────────────────────────────────────────────────────
    layout["footer"].update(Panel(
        Text("  q  종료   |   scanner.log + kiwoom_auto.log 실시간 감시 중", style="dim"),
        style="dim",
    ))

    return layout


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Windows에서 UTF-8 출력 강제 설정
    import io
    # Rich Console은 기본적으로 UTF-8 지원, file 인자로 UTF-8 스트림 전달
    utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    console = Console(file=utf8_stdout, force_terminal=True)

    # 파일 끝부터 읽기
    _seek_end(LOG_FILE,   "scanner_pos")
    _seek_end(KIWOOM_LOG, "kiwoom_pos")

    # kiwoom_auto.log 가 없으면 콘솔 스트림도 scanner.log 만으로 운영
    has_kiwoom = os.path.exists(KIWOOM_LOG)
    if not has_kiwoom:
        # scanner.log 에서도 스캔/체결 정보를 뽑을 수 있도록 kiwoom 파서를 같이 적용
        pass

    console.print("[bold cyan]📡 키움 자동매매 실시간 감시 시작...[/]")
    console.print(f"  scanner.log   → {os.path.abspath(LOG_FILE)}")
    console.print(f"  kiwoom_auto.log → {os.path.abspath(KIWOOM_LOG) if has_kiwoom else '없음 (scanner.log 단독 사용)'}")
    console.print("  [dim]Ctrl+C 로 종료[/]\n")
    time.sleep(1.0)

    with Live(build_dashboard(), console=console,
              refresh_per_second=1, screen=True) as live:
        try:
            while True:
                # scanner.log 새 줄 처리
                for line in _tail_new(LOG_FILE, "scanner_pos"):
                    parse_scanner_line(line)
                    # scanner.log 에도 주기 스캔 관련 정보 있을 수 있음
                    parse_kiwoom_line(line)

                # kiwoom_auto.log 새 줄 처리 (있을 때만)
                if has_kiwoom:
                    for line in _tail_new(KIWOOM_LOG, "kiwoom_pos"):
                        parse_kiwoom_line(line)

                live.update(build_dashboard())
                time.sleep(REFRESH_SEC)

        except KeyboardInterrupt:
            pass

    console.print("\n[bold cyan]감시 종료.[/]")


if __name__ == "__main__":
    main()
