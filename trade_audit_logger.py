"""
TradeAuditLogger — 매매 행위 일별 CSV 감사 로그

신호 발생부터 매도 체결까지를 단일 행(row)으로 기록한다.
분석·피드백 루프(Feedback Loop) 전용 영구 로그.

이벤트 흐름:
  log_signal()        → 신호 발생   (인메모리 row 생성)
  log_buy_order()     → 매수 주문 전송
  log_buy_fill()      → 매수 체결
  log_sell_decision() → 매도 판단  (손절/익절/타임컷/수동 등)
  log_sell_order()    → 매도 주문 전송
  log_sell_fill()     → 매도 체결  → CSV에 완성 row flush
  flush_all()         → 장 마감/종료 시 미완 row 강제 저장

CSV 파일: logs/trade_audit_YYYYMMDD.csv (일별 자동 분리)
"""

import csv
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV 컬럼 정의
# ---------------------------------------------------------------------------

COLUMNS: list[str] = [
    # ── 종목 기본 정보
    "trade_date",               # 날짜 (YYYY-MM-DD)
    "code",                     # 종목코드
    "name",                     # 종목명
    # ── 신호 정보
    "signal_type",              # JDM_ENTRY / BREAKOUT
    "signal_time",              # 신호 판단 시각 (HH:MM:SS)
    "signal_price",             # 신호 당시 현재가
    "signal_reason",            # 통과 이유 (필터명 포함)
    # ── 신호 당시 지표 스냅샷
    "rsi_at_signal",            # RSI(14)
    "ma_short_at_signal",       # 단기 MA (기본 MA7)
    "ma_long_at_signal",        # 장기 MA (기본 MA15)
    "ema_short_at_signal",      # EMA10
    "ema_long_at_signal",       # EMA20
    "chejan_strength_at_signal",# 체결강도 (%)
    "volume_ratio_at_signal",   # 거래량 급증 배수 (직전 10분 평균 대비)
    "change_pct_at_signal",     # 등락률 (%)
    "trade_amount_at_signal",   # 거래대금 (원)
    "kospi_chg_at_signal",      # 신호 당시 코스피 등락률 (%)
    "kosdaq_chg_at_signal",     # 신호 당시 코스닥 등락률 (%)
    "investor_score_at_signal", # 수급 점수 (-1/0/+1)
    # ── 매수 주문
    "buy_order_time",           # 매수 주문 전송 시각
    "buy_order_price",          # 매수 주문가 (0=시장가)
    "buy_order_qty",            # 매수 주문 수량
    # ── 매수 체결
    "buy_fill_time",            # 매수 체결 시각
    "buy_fill_price",           # 매수 체결가
    "buy_fill_qty",             # 매수 체결 수량
    # ── 매도 판단/주문
    "sell_decision_time",       # 매도 판단 시각
    "sell_decision_price",      # 매도 판단 당시 현재가
    "sell_reason",              # 손절/익절/반절익절/EMA20이탈/Time-cut/수동/Day Close 등
    "sell_order_time",          # 매도 주문 전송 시각
    # ── 매도 체결
    "sell_fill_time",           # 매도 체결 시각
    "sell_fill_price",          # 매도 체결가
    "sell_fill_qty",            # 매도 체결 수량
    # ── 결과 계산값
    "avg_buy_price",            # 매수 평단가
    "return_pct",               # 수익률 (%) = (매도가 - 평단) / 평단 × 100
    "realized_pnl",             # 실현손익 (원, 수수료·세금 차감 후)
    "holding_minutes",          # 보유 시간 (분) = 매도체결 - 매수체결
    "final_status",             # SIGNAL_ONLY / ORDERED / BOUGHT / SELL_DECIDED
    "is_warmup",                # 장 초반 지표 워밍업 구간 발생 여부 (1/0)
]


# ---------------------------------------------------------------------------
# TradeAuditLogger
# ---------------------------------------------------------------------------

class TradeAuditLogger:
    """
    일별 CSV 트레이드 감사 로그.

    핵심 설계:
    - _pending_rows: 신호 발생부터 매도 체결까지 인메모리 누적
    - key = "{code}_{signal_time_HHmmss}"  — 당일 동일 종목 복수 매매 지원
    - 매도 체결(log_sell_fill) 시점에 CSV에 완성 행 flush
    - flush_all(): 장 마감·종료 시 미완 행(PARTIAL / SIGNAL_ONLY)도 저장

    스레드 안전:
    - ScannerWorker(QThread)에서 log_signal() 호출
    - 메인 Qt 스레드에서 나머지 log_* 호출
    - threading.Lock 으로 _pending_rows·CSV 쓰기 보호
    """

    def __init__(self, log_dir: str = "logs", db_manager=None) -> None:
        self._log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._lock        = threading.Lock()
        self._pending_rows: dict[str, dict] = {}
        self._today_str   = date.today().isoformat()
        
        # [SQLite] DB 매니저 연결
        if db_manager is None:
            from infra.db_manager import DatabaseManager
            self.db = DatabaseManager()
        else:
            self.db = db_manager

        self._ensure_file()
        
        # [NEW] 배치 기록용 버퍼
        self._write_buffer: list[dict] = []
        self._last_flush_time = datetime.now()
        self._flush_interval_sec = 5.0
        self._stop_event = threading.Event()
        self._flush_thread = threading.Thread(target=self._bg_flush_loop, daemon=True)
        self._flush_thread.start()

    def stop(self) -> None:
        """배치 스레드 중지 및 잔여 데이터 저장."""
        self._stop_event.set()
        self.flush_all()

    def _bg_flush_loop(self) -> None:
        """백그라운드에서 주기적으로 버퍼를 파일에 씀."""
        while not self._stop_event.is_set():
            time_to_wait = max(0.1, self._flush_interval_sec - (datetime.now() - self._last_flush_time).total_seconds())
            if self._stop_event.wait(time_to_wait):
                break
            
            with self._lock:
                if self._write_buffer:
                    self._do_batch_write()

    # ── 파일 관리 ─────────────────────────────────────────────────────────────

    def _csv_path(self, day: Optional[str] = None) -> Path:
        d = day or date.today().strftime("%Y%m%d")
        return Path(self._log_dir) / f"trade_audit_{d}.csv"

    def _ensure_file(self) -> None:
        """오늘 날짜 CSV가 없으면 헤더를 포함해 신규 생성한다."""
        path = self._csv_path()
        if not path.exists():
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=COLUMNS).writeheader()
            logger.info("[TradeAudit] 새 파일 생성: %s", path)

    def _check_date_rollover(self) -> None:
        """자정 이후 날짜가 바뀌면 새 파일을 준비한다 (Lock 내부에서 호출)."""
        today = date.today().isoformat()
        if today != self._today_str:
            self._today_str = today
            self._ensure_file()

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────────────

    def _find_pending_key(self, code: str) -> Optional[str]:
        """
        code에 해당하는 가장 최근 미완 행의 key를 반환한다.
        Lock 내부에서만 호출.
        """
        matched = [k for k in self._pending_rows if k.startswith(f"{code}_")]
        if not matched:
            return None
        return sorted(matched)[-1]   # signal_time suffix(HHmmss) 기준 최신

    def _flush_row(self, key: str) -> None:
        """단일 행을 버퍼에 추가한다. Lock 내부에서만 호출."""
        row = self._pending_rows.get(key)
        if row is None:
            return
            
        self._write_buffer.append(row.copy())
        
        # 버퍼가 너무 크면 즉시 기록 (방어용)
        if len(self._write_buffer) >= 50:
            self._do_batch_write()

    def _do_batch_write(self) -> None:
        """버퍼의 내용을 실제 파일과 DB에 씀. Lock 내부에서 호출 권장."""
        if not self._write_buffer:
            return
            
        try:
            self._check_date_rollover()
            path = self._csv_path()
            rows_to_write = self._write_buffer[:]
            self._write_buffer.clear()
            self._last_flush_time = datetime.now()

            # 1. CSV 저장
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
                for row in rows_to_write:
                    writer.writerow(row)
            
            # 2. SQLite 저장
            if self.db:
                db_batch = []
                for row in rows_to_write:
                    # key 생성 (code + signal_time)
                    code = row.get("code")
                    sig_time = row.get("signal_time", "").replace(":", "")
                    key = f"{code}_{sig_time}"
                    db_data = {k: (None if v == "" else v) for k, v in row.items()}
                    db_batch.append((key, db_data))
                
                self.db.upsert_trades_batch(db_batch)
                
            logger.debug("[TradeAudit] Batch flush 완료 (%d건)", len(rows_to_write))
        except Exception as e:
            logger.error("[TradeAudit] Batch write 오류: %s", e)

    # ── 공개 API ──────────────────────────────────────────────────────────────

    def log_signal(self, sig, snap, cfg=None) -> None:
        """
        신호 발생 시 호출. 인메모리 행을 생성하고 지표값을 스냅샷한다.
        """
        try:
            from analysis.feature_engineer import extract_ml_features
            features = extract_ml_features(sig, snap, cfg)

            now = datetime.now()
            key = f"{sig.code}_{now.strftime('%H%M%S')}"

            row: dict = {col: "" for col in COLUMNS}
            row.update({
                "trade_date":               date.today().isoformat(),
                "code":                     sig.code,
                "name":                     sig.name,
                "signal_type":              getattr(sig, "signal_type", ""),
                "signal_time":              now.strftime("%H:%M:%S"),
                "signal_reason":            getattr(sig, "reason", ""),
                "trade_amount_at_signal":   getattr(snap, "trade_amount", ""),
                "final_status":             "SIGNAL_ONLY",
                "is_warmup":                1 if getattr(sig, "is_warmup", False) else 0,
            })
            # feature_engineer에서 추출된 특성들 업데이트
            row.update(features)

            with self._lock:
                self._pending_rows[key] = row
                # 신호 발생 즉시 CSV에 기록 — 프로그램 비정상 종료 시에도 유실 방지
                # 매수까지 이어지면 매도 체결 시 COMPLETED 행이 별도로 추가됨
                self._flush_row(key)

            logger.debug("[TradeAudit] 신호 기록 — %s", key)

        except Exception:
            logger.exception("[TradeAudit] log_signal 오류")

    def log_buy_order(
        self,
        code:  str,
        qty:   int,
        price: int,
        ts:    Optional[datetime] = None,
    ) -> None:
        """매수 주문 전송 시 호출."""
        try:
            now = (ts or datetime.now()).strftime("%H:%M:%S")
            with self._lock:
                key = self._find_pending_key(code)
                if key is None:
                    return
                self._pending_rows[key].update({
                    "buy_order_time":  now,
                    "buy_order_price": price if price else "시장가",
                    "buy_order_qty":   qty,
                    "final_status":    "ORDERED",
                })
        except Exception:
            logger.exception("[TradeAudit] log_buy_order 오류")

    def log_buy_fill(
        self,
        code:         str,
        filled_qty:   int,
        filled_price: int,
        ts:           Optional[datetime] = None,
    ) -> None:
        """매수 체결 시 호출."""
        try:
            now = (ts or datetime.now()).strftime("%H:%M:%S")
            with self._lock:
                key = self._find_pending_key(code)
                if key is None:
                    return
                self._pending_rows[key].update({
                    "buy_fill_time":  now,
                    "buy_fill_price": filled_price,
                    "buy_fill_qty":   filled_qty,
                    "final_status":   "BOUGHT",
                })
        except Exception:
            logger.exception("[TradeAudit] log_buy_fill 오류")

    def log_sell_decision(
        self,
        code:          str,
        reason:        str,
        current_price: int,
        ts:            Optional[datetime] = None,
    ) -> None:
        """
        매도 판단 시 호출.
        reason 예: "손절 -1.5%", "반절익절 +1.7%", "EMA20이탈", "Time-cut 42분 +0.3%",
                   "수동매도", "Day Close 15:19", "Hard Stop -2.0%"
        """
        try:
            now = (ts or datetime.now()).strftime("%H:%M:%S")
            with self._lock:
                key = self._find_pending_key(code)
                if key is None:
                    return
                self._pending_rows[key].update({
                    "sell_decision_time":  now,
                    "sell_decision_price": current_price,
                    "sell_reason":         reason,
                    "final_status":        "SELL_DECIDED",
                })
        except Exception:
            logger.exception("[TradeAudit] log_sell_decision 오류")

    def log_sell_order(
        self,
        code:  str,
        qty:   int,
        price: int,
        ts:    Optional[datetime] = None,
    ) -> None:
        """매도 주문 전송 시 호출."""
        try:
            now = (ts or datetime.now()).strftime("%H:%M:%S")
            with self._lock:
                key = self._find_pending_key(code)
                if key is None:
                    return
                self._pending_rows[key].update({
                    "sell_order_time": now,
                    "final_status":    "SELL_ORDERED",
                })
        except Exception:
            logger.exception("[TradeAudit] log_sell_order 오류")

    def log_sell_fill(
        self,
        code:          str,
        filled_qty:    int,
        filled_price:  int,
        avg_buy_price: int,
        realized_pnl:  int,
        ts:            Optional[datetime] = None,
    ) -> None:
        """
        매도 체결 시 호출. 완성된 행을 CSV에 flush하고 인메모리에서 제거한다.
        수익률·보유 시간을 자동 계산한다.
        """
        try:
            now_dt = ts or datetime.now()
            now    = now_dt.strftime("%H:%M:%S")

            with self._lock:
                key = self._find_pending_key(code)
                if key is None:
                    return
                row = self._pending_rows[key]

                # 수익률 계산
                ret_pct = ""
                if avg_buy_price and avg_buy_price > 0:
                    ret_pct = f"{(filled_price - avg_buy_price) / avg_buy_price * 100:.2f}"

                # 보유 시간 계산 (매수 체결 시각 기준)
                holding_min = ""
                buy_fill_str = row.get("buy_fill_time", "")
                if buy_fill_str:
                    try:
                        buy_dt     = datetime.fromisoformat(
                            f"{date.today().isoformat()} {buy_fill_str}"
                        )
                        holding_min = f"{(now_dt - buy_dt).total_seconds() / 60:.1f}"
                    except Exception:
                        pass

                row.update({
                    "sell_fill_time":  now,
                    "sell_fill_price": filled_price,
                    "sell_fill_qty":   filled_qty,
                    "avg_buy_price":   avg_buy_price,
                    "return_pct":      ret_pct,
                    "realized_pnl":    realized_pnl,
                    "holding_minutes": holding_min,
                    "final_status":    "COMPLETED",
                })
                self._flush_row(key)
                del self._pending_rows[key]

        except Exception:
            logger.exception("[TradeAudit] log_sell_fill 오류")

    def flush_all(self, status_override: Optional[str] = None) -> None:
        """
        미완 행 전부를 CSV에 저장한다.
        장 마감 또는 프로그램 종료 시(closeEvent) 호출.
        """
        try:
            with self._lock:
                if not self._pending_rows:
                    return

                self._check_date_rollover()
                path = self._csv_path()
                db_batch = []

                # CSV 파일 한 번만 열어서 모두 쓰기
                with open(path, "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
                    
                    for key in list(self._pending_rows.keys()):
                        row        = self._pending_rows[key]
                        cur_status = row.get("final_status", "SIGNAL_ONLY")
                        if status_override:
                            row["final_status"] = status_override
                        elif cur_status in ("ORDERED", "BOUGHT", "SELL_DECIDED", "SELL_ORDERED"):
                            row["final_status"] = "PARTIAL"
                        
                        # 1. CSV 행 추가
                        writer.writerow(row)
                        
                        # 2. DB 배치 목록에 추가
                        if self.db:
                            db_data = {k: (None if v == "" else v) for k, v in row.items()}
                            db_batch.append((key, db_data))
                            
                        del self._pending_rows[key]

                # 3. DB 배치 저장 (한 번의 트랜잭션)
                if db_batch and self.db:
                    self.db.upsert_trades_batch(db_batch)

            logger.info("[TradeAudit] flush_all 완료 (%d건)", len(db_batch) if db_batch else 0)
        except Exception:
            logger.exception("[TradeAudit] flush_all 오류")
