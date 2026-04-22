"""
logging_config.py — 전용 파일 로거 설정 모듈

사용법:
    from logging_config import order_log, position_log

    order_log.info("[신호수신] ...")
    position_log.info("[포지션생성] ...")

파일 구조:
    logs/order.log    — handle_signal 진입 ~ 체결 전 과정
    logs/position.log — 포지션 생성 / 청산 / ATR trail 이벤트
    logs/kiwoom_tr.log — TR 실패 집계 (Zone 8)

모든 로거는 propagate=True (root 로거 kiwoom_auto.log에도 동시 기록).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _make_logger(name: str, filename: str, level: int = logging.DEBUG) -> logging.Logger:
    """
    RotatingFileHandler 기반 전용 로거를 생성한다.
    이미 핸들러가 설정돼 있으면 재생성하지 않는다 (모듈 재로딩 방어).
    """
    lg = logging.getLogger(name)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers):
        fh = logging.handlers.RotatingFileHandler(
            _LOG_DIR / filename,
            maxBytes=10 * 1024 * 1024,   # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(_FMT)
        lg.addHandler(fh)
    lg.setLevel(level)
    lg.propagate = True   # root logger(kiwoom_auto.log)에도 동시 기록
    return lg


# ── 전용 로거 인스턴스 ──────────────────────────────────────────────────────

order_log = _make_logger(
    "kiwoom.order",
    "order.log",
)
"""주문 흐름 전용: 신호수신 → 필터 → 주문발송 → 체결 전 과정."""

position_log = _make_logger(
    "kiwoom.position",
    "position.log",
)
"""포지션 이벤트 전용: 생성 / peak갱신 / 청산결정 / ATR trail."""

tr_log = _make_logger(
    "kiwoom.tr",
    "kiwoom_tr.log",
)
"""TR 실패 집계 전용: opt10001/opt10030/opt20001 실패 카운터."""
