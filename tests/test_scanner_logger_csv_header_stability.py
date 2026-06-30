"""
test_scanner_logger_csv_header_stability.py — ScannerLogger CSV 컬럼 밀림 버그 회귀 테스트

배경(2026-06-30): _do_batch_write()가 배치마다 "그 배치 행들의 키 합집합"으로
fieldnames를 새로 계산했다. 같은 CSV 파일에 서로 다른 전략의 신호(JDM_ENTRY의
li_*/entry_candle_low, MORNING_GOLDENTIME의 mg_*, OVERHEAT_PULLBACK의 op_* 등
ScanSignal.values 키가 전략마다 다름)가 배치마다 다르게 섞이면, 배치별 컬럼
순서가 서로 달라져 DictWriter가 파일에 이미 적힌 헤더와 다른 순서로 값을 써서
컬럼이 밀리는 사고가 실제 운영 로그(scanner_signal_20260630.csv)에서 확인됐다.
파일별 헤더를 최초 1회만 결정해 고정 재사용하도록 수정했고, 이 테스트는 그
고정 동작을 검증한다.
"""
import csv
import threading

import pytest

from scanner.scanner_logger import ScannerLogger


@pytest.fixture(autouse=True)
def isolate_logs_dir(tmp_path, monkeypatch):
    """_do_batch_write()의 Path("logs") 상대경로를 tmp_path로 격리해 운영 로그 오염 방지."""
    monkeypatch.chdir(tmp_path)
    ScannerLogger._write_buffers = {
        "scanner_passed.csv": [],
        "scanner_rejected.csv": [],
        "scanner_signal.csv": [],
    }
    ScannerLogger._file_fieldnames = {}
    ScannerLogger._batch_lock = threading.Lock()
    yield


def test_header_fixed_after_first_batch_survives_new_keys_in_later_batch():
    """첫 배치가 정한 헤더 순서를, 새 키 조합을 가진 두 번째 배치도 그대로 따른다."""
    row1 = {
        "timestamp": "t1", "code": "A", "name": "a", "reason": "r1",
        "f_rsi": 0.1,
        "li_bs": 1, "li_vb": 2, "li_cr": 3, "li_tv": 8,
        "li_leading": 0.5,
        "entry_candle_low": 1000,
    }
    row2 = {
        "timestamp": "t2", "code": "B", "name": "b", "reason": "r2",
        "f_rsi": 0.2,
        "mg_phase": 3, "mg_pullback_pct": -1, "mg_vwap_dist": 0.1,
    }

    with ScannerLogger._batch_lock:
        ScannerLogger._write_buffers["scanner_signal.csv"] = [row1]
        ScannerLogger._do_batch_write()
        ScannerLogger._write_buffers["scanner_signal.csv"] = [row2]
        ScannerLogger._do_batch_write()

    with open("logs/scanner_signal.csv", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        data_rows = list(reader)

    assert len(data_rows) == 2
    for row in data_rows:
        assert len(row) == len(header), "데이터 행의 컬럼 수는 항상 헤더와 같아야 한다"

    # row1의 li_leading 값(0.5)이 정확히 li_leading 컬럼에 들어가야 한다 (밀림 없음)
    li_idx = header.index("li_leading")
    assert data_rows[0][li_idx] == "0.5"

    # row2는 li_leading 키가 없으므로 빈 문자열이어야 한다 (mg_* 값이 끼어들면 안 됨)
    assert data_rows[1][li_idx] == ""

    # row2의 mg_phase는 헤더에 없는 새 키이므로 extrasaction="ignore"로 누락되어야 한다
    assert "mg_phase" not in header


def test_existing_file_header_reused_across_process_restart():
    """프로세스 재시작(클래스 재초기화) 후에도 기존 파일 헤더를 그대로 따른다."""
    row1 = {"timestamp": "t1", "code": "A", "name": "a", "reason": "r1", "li_leading": 0.7}
    with ScannerLogger._batch_lock:
        ScannerLogger._write_buffers["scanner_signal.csv"] = [row1]
        ScannerLogger._do_batch_write()

    # 캐시를 비워 "재시작" 상황 재현 — 디스크에 남은 헤더만으로 복구되어야 함
    ScannerLogger._file_fieldnames = {}

    row2 = {"timestamp": "t2", "code": "B", "name": "b", "reason": "r2", "op_current_level": 1}
    with ScannerLogger._batch_lock:
        ScannerLogger._write_buffers["scanner_signal.csv"] = [row2]
        ScannerLogger._do_batch_write()

    with open("logs/scanner_signal.csv", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        data_rows = list(reader)

    li_idx = header.index("li_leading")
    assert data_rows[0][li_idx] == "0.7"
    assert data_rows[1][li_idx] == ""  # op_current_level이 li_leading 자리로 밀려 들어가면 안 됨
