"""
pytest conftest.py — 공용 fixture 정의

모든 테스트 파일에서 자동으로 로드되며,
session-scoped fixture는 전체 테스트 실행 중 한 번만 생성된다.
"""

from __future__ import annotations

import logging

import pytest
from unittest.mock import MagicMock

from PyQt5.QtWidgets import QApplication

# from order.order_manager import OrderManager  # 임시 주석: conftest 로드 에러 방지
# from scanner.smart_scanner import SmartScannerConfig


@pytest.fixture(autouse=True, scope="session")
def _silence_file_loggers():
    """
    [FIX 2026-06-26] order_log/position_log/tr_log(logging_config.py)는 실제
    logs/order.log, logs/position.log 파일에 직접 쓰는 전용 파일 핸들러를 가진
    named logger다. 테스트가 OrderManager._handle_sell_fill() 등 실 동작 메서드를
    직접 호출하면 그 호출이 그대로 운영 로그 파일에 기록된다(남화산업 111710 가짜
    [포지션청산] 로그가 실제 파일에 9회 누적된 사고 — 테스트 종목코드/체결가가
    어제 실거래 사례와 동일해 분석 시 진짜 거래로 오인됨). 세션 시작 시 해당
    로거들의 파일 핸들러를 모두 떼어내 테스트가 운영 로그를 건드릴 수 없게 한다.
    """
    for name in ("kiwoom.order", "kiwoom.position", "kiwoom.tr"):
        logging.getLogger(name).handlers.clear()
    yield


@pytest.fixture(scope="session")
def qapp():
    """
    session-scoped QApplication fixture.

    모든 Qt 테스트가 공유하는 단일 QApplication 인스턴스를 제공한다.
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def mock_kiwoom(qapp):
    """
    Mock KiwoomManager (최소 기능).

    SendOrder 반환값만 구현.
    """
    kiwoom = MagicMock()
    kiwoom._ocx = MagicMock()
    kiwoom._ocx.dynamicCall = MagicMock(return_value=0)  # 주문 성공 (ret=0)
    return kiwoom


# @pytest.fixture
# def mock_order_mgr(mock_kiwoom, qapp):
#     """
#     기본 MockOrderManager fixture.
#
#     - 예수금: 10,000,000원
#     - 포지션: 없음
#     """
#     om = OrderManager(
#         kiwoom=mock_kiwoom,
#         account="1234567890",
#         max_order_amount=1_500_000,
#         max_positions=5,
#         parent=None,
#     )
#     om.cash = 10_000_000
#     return om


# @pytest.fixture
# def mock_scan_cfg():
#     """
#     기본 SmartScannerConfig fixture.
#
#     테스트용 기본값으로 설정된 설정 객체.
#     """
#     cfg = SmartScannerConfig()
#     # 기본값은 scanner/smart_scanner.py 의 SmartScannerConfig 참고
#     return cfg
