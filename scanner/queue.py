from __future__ import annotations
import time
import heapq
import logging
import threading
from collections import deque as _Deque


logger = logging.getLogger(__name__)


class TRRequestQueue:
    """
    키움 TR 호출 간격을 중앙에서 관리한다.


    키움 API 제한: 연속 TR 호출 간 최소 0.2초 권장.
    여기서 0.25초로 설정해 여유를 두고, 모든 TR 호출을
    call() 메서드를 통해 실행하면 자동으로 간격이 보장된다.


    기존 time.sleep(tr_delay) 분산 호출을 이 클래스로 대체한다.
    """
    _MIN_INTERVAL = 0.25  # 초


    def __init__(self) -> None:
        self._last_call: float = 0.0
        self._lock = threading.Lock()


    def call(self, fn: Callable, *args):
        """fn(*args)를 최소 간격 보장 후 실행하고 결과를 반환한다.


        _tr_busy + _scan_in_progress 보호가 추가된 이후 cascade 위험 없음.
        processEvents로 Watchdog ACK 발화 허용. 최대 대기: 0.25초.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            wait = max(0.0, self._MIN_INTERVAL - elapsed)
            self._last_call = now + wait
        if wait > 0:
            from PyQt5.QtWidgets import QApplication
            from PyQt5.QtCore import QEventLoop
            QApplication.processEvents(QEventLoop.AllEvents, max(1, int(wait * 1000)))
        return fn(*args)


class PriorityWatchQueue:
    def __init__(self, kiwoom, screen_no: str = "9200", max_subs: int = 100) -> None:
        self._kiwoom   = kiwoom
        self._screen   = screen_no
        self._max_subs = max_subs
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()
        self._pending_add: set[str] = set()
        self._pending_remove: set[str] = set()
        self._worker_thread = None
        self._stop_worker = False


    def refresh(self, top_codes: list[str]) -> None:
        with self._lock:
            target    = set(top_codes[: self._max_subs])
            to_add    = target - self._subscribed
            to_remove = self._subscribed - target

            # SetRealReg/Remove를 비동기로 처리하도록 큐에 추가 (블로킹 방지)
            self._pending_remove.update(to_remove)
            self._pending_add.update(to_add)

        # 워커 스레드 시작 (아직 없으면)
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._stop_worker = False
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()


    def _worker_loop(self) -> None:
        """비동기로 SetRealReg/Remove를 처리하는 워커 스레드"""
        import time as _time
        while not self._stop_worker:
            pending_remove = None
            pending_add = None
            with self._lock:
                if self._pending_remove:
                    pending_remove = list(self._pending_remove)
                    self._pending_remove.clear()
                if self._pending_add:
                    pending_add = list(self._pending_add)
                    self._pending_add.clear()

            # SetRealRemove 처리
            if pending_remove:
                for code in pending_remove:
                    if self._stop_worker:
                        break  # 종료 요청 시 즉시 빠져나옴 (OCX 파괴 후 호출 방지)
                    try:
                        self._unsub_impl(code)
                    except RuntimeError as e:
                        # 'wrapped C/C++ object has been deleted' — Qt 객체 파괴됨
                        # 종료 시점 자연스러운 현상이므로 워커 즉시 종료
                        logger.debug("[PriorityWatchQueue] OCX 파괴 감지 — 워커 종료: %s", code)
                        self._stop_worker = True
                        break
                    except Exception as e:
                        logger.warning("[PriorityWatchQueue] SetRealRemove 실패: %s - %s", code, e)

            # SetRealReg 처리 (배치)
            if pending_add:
                try:
                    self._setrealreg_batch(pending_add)
                    with self._lock:
                        self._subscribed.update(pending_add)
                    logger.debug("[PriorityWatchQueue] SetRealReg 배치 등록 %d종목", len(pending_add))
                except Exception as e:
                    logger.warning("[PriorityWatchQueue] SetRealReg 배치 실패: %s", e)
                    # 실패 시 다시 큐에 추가
                    with self._lock:
                        self._pending_add.update(pending_add)

            _time.sleep(0.05)  # 50ms 간격으로 확인


    def _setrealreg_batch(self, codes: list[str], chunk_size: int = 20) -> None:
        """SetRealReg를 청크 단위로 호출 (너무 많은 종목 한번에 등록 방지)"""
        for i in range(0, len(codes), chunk_size):
            chunk = codes[i:i+chunk_size]
            code_list = ";".join(chunk)
            try:
                self._kiwoom._ocx.dynamicCall(
                    "SetRealReg(QString, QString, QString, QString)",
                    [self._screen, code_list, "10;11;12;13;14;16;17;18;20;121;125", "1"],
                )
                import time as _time
                _time.sleep(0.1)  # 청크 사이 100ms 대기
            except Exception as e:
                logger.warning("[PriorityWatchQueue] SetRealReg 청크 실패 (%d개): %s", len(chunk), e)
                raise


    def _unsub_impl(self, code: str) -> None:
        """SetRealRemove 구현 (워커 스레드에서 사용)"""
        self._kiwoom._ocx.dynamicCall(
            "SetRealRemove(QString, QString)", [self._screen, code]
        )
        with self._lock:
            self._subscribed.discard(code)


    def _sub(self, code: str) -> None:
        """단일 종목 즉시 등록 (보유 종목 진입 시 사용)"""
        try:
            self._kiwoom._ocx.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                [self._screen, code, "10;11;12;13;14;16;17;18;20;121;125", "1"],
            )
            with self._lock:
                self._subscribed.add(code)
        except Exception as e:
            logger.warning("[PriorityWatchQueue] _sub 실패: %s - %s", code, e)


    def _unsub(self, code: str) -> None:
        """단일 종목 해제 (포지션 청산 시 사용) - 비동기 큐에 추가"""
        with self._lock:
            self._pending_remove.add(code)

        # 워커 스레드 시작 (아직 없으면)
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._stop_worker = False
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()


    @property
    def subscribed(self) -> set[str]:
        return self._subscribed.copy()

    def stop(self) -> None:
        """[NEW 2026-05-26] 워커 스레드 정상 종료 — closeEvent에서 호출.

        OCX 파괴 전에 워커를 멈춰 'QAxWidget has been deleted' 에러를 방지한다.
        대기 중인 SetRealReg/Remove는 폐기 (어차피 종료 중이라 의미 없음).
        """
        self._stop_worker = True
        # 남은 큐 비우기 — 어차피 처리해도 OCX 파괴되면 에러
        with self._lock:
            self._pending_add.clear()
            self._pending_remove.clear()
        # 워커 스레드 종료 대기 (최대 1초)
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
