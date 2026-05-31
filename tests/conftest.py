"""
pytest conftest.py — 공용 fixture 정의

모든 테스트 파일에서 자동으로 로드되며,
session-scoped fixture는 전체 테스트 실행 중 한 번만 생성된다.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from PyQt5.QtWidgets import QApplication

# from order.order_manager import OrderManager  # 임시 주석: conftest 로드 에러 방지
# from scanner.smart_scanner import SmartScannerConfig


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
