# 📍 프로세스별 코드 위치 상세 가이드

## 1️⃣ 지표 수집 (Data Collection)

### 실시간 데이터 수신

**파일**: [kiwoom_api.py](kiwoom_api.py)

```python
# SetRealReg 등록 (감시 종목 추가)
def set_real_reg(codes: list[str], fid_list: str) -> bool:
    """모니터링할 종목과 FID 리스트 등록"""
    # 실행 위치: main_window.py (시작 시)
    pass

# OnReceiveRealData 콜백 (실시간 틱 수신)
def _on_receive_real_data(self, code, real_type, real_data):
    """
    위치: kiwoom_api.py (이벤트 핸들러)
    
    FID 파싱:
      - FID 10: 현재가
      - FID 11: 전일대비
      - FID 12: 등락률
      - FID 13: 누적거래대금 (천원)
      - FID 15: 당일거래량
      - FID 16: 시가
      - FID 17: 고가
      - FID 18: 저가
    """
    # SnapshotStore 갱신
    self._snap_store.update_price(
        code=code,
        current_price=...,
        volume=...,
        trade_amount=...,
        chejan_strength=...,
        ...
    )
```

**관련 파일**:
- kiwoom_api.py:OnReceiveRealData (이벤트 핸들러)
- snapshot_store.py:update_price() (데이터 저장)

### SnapshotStore 갱신

**파일**: [scanner/snapshot_store.py](scanner/snapshot_store.py)

```python
class SnapshotStore:
    """전 종목 스냅샷 캐시 (pandas DataFrame)"""
    
    def __init__(self):
        self._df = pd.DataFrame(columns=_DF_COLS).set_index("code")
        self._states: dict[str, InternalStockState] = {}
    
    def update_price(self, code, current_price, volume, trade_amount, ...):
        """
        실시간 틱 처리
        
        위치: snapshot_store.py:200+
        호출처: kiwoom_api.py:OnReceiveRealData
        """
        with self._lock:
            # DataFrame 갱신
            self._df.loc[code, 'current_price'] = current_price
            self._df.loc[code, 'volume'] = volume
            
            # InternalStockState 갱신
            st = self._get_or_create_state(code)
            st.current_price = current_price
            st.min_vols = [..., volume]  # 1분 거래량
    
    def update_ohlc(self, code, closes, opens, highs, lows, volumes):
        """
        분봉 데이터 저장
        
        위치: snapshot_store.py:300+
        호출처: smart_scanner.py (분봉 생성 시)
        """
        st = self._states[code]
        st.mins = closes
        st.min_opens = opens
        st.min_highs = highs
        st.min_lows = lows
        st.min_vols = volumes
    
    def get_snapshot(self, code) -> Optional[StockSnapshot]:
        """
        종목 스냅샷 조회 (지표 계산용)
        
        위치: snapshot_store.py:400+
        호출처: smart_scanner.py (신호 판단 시)
        """
        with self._lock:
            st = self._states.get(code)
            if not st: return None
            
            # StockSnapshot 객체 생성
            snap = StockSnapshot(
                code=code,
                name=st.name,
                current_price=st.current_price,
                closes_1min=st.mins,
                volumes_1min=st.min_vols,
                chejan_strength=st.chejan_str,
                trend_level=st.trend_level,
                rs_score=self._rs_scores.get(code, 0),
                ...
            )
            return snap
```

**라인 범위**:
- update_price(): 200~250
- update_ohlc(): 300~350
- get_snapshot(): 400~450

---

## 2️⃣ 지표 계산 (Indicator Calculation)

### IndicatorService

**파일**: [scanner/indicator_service.py](scanner/indicator_service.py)

```python
class IndicatorService:
    """모든 기술지표 계산 (캐시 활용)"""
    
    @staticmethod
    def calc_rsi(closes: list[float], period: int = 14) -> Optional[float]:
        """
        RSI 계산 (상대강도지수)
        
        위치: indicator_service.py:40~44
        호출처: smart_scanner.py, strategies/*.py
        
        반환: 0~100 (높을수록 상승 강세)
        """
        # 내부에서 LRU 캐시 활용 (_calc_rsi_cached)
        pass
    
    @staticmethod
    def calc_ema(closes: list[float], period: int) -> Optional[float]:
        """
        EMA 계산 (지수이동평균)
        
        위치: indicator_service.py:54~57
        호출처: strategy 신호 판단
        """
        pass
    
    @staticmethod
    def calc_ma(closes: list[float], period: int) -> Optional[float]:
        """
        SMA 계산 (단순이동평균)
        
        위치: indicator_service.py:68~70
        호출처: strategy 신호 판단
        """
        pass
    
    @staticmethod
    def calc_bollinger_bands(closes: list[float], period: int = 20, std_mult: float = 2.0):
        """
        볼린저 밴드 계산
        
        위치: indicator_service.py:113~125
        반환: {upper, middle, lower}
        """
        pass
    
    @staticmethod
    def get_trend_status(closes, highs, lows, volumes, **kwargs) -> int:
        """
        추세 강도 판정 (0~3)
        
        위치: indicator_service.py:128~150
        
        반환값:
          0: 약한 상승 또는 정체
          1: 보통 상승
          2: 강한 상승
          3: 매우 강한 상승
        """
        pass
    
    @staticmethod
    def get_ai_features(snap: StockSnapshot, ...) -> dict[str, float]:
        """
        AI 모델용 19개 피처 생성
        
        위치: indicator_service.py:235~435
        호출처: analysis/feature_engineer.py
        
        반환: {f_rsi, f_ema20_gap, f_pct_b, ..., f_hoga_ratio}
        """
        pass

# 호출 예시
rsi = IndicatorService.calc_rsi(snap.closes_1min, 14)
ema10 = IndicatorService.calc_ema(snap.closes_1min, 10)
trend = IndicatorService.get_trend_status(snap.closes_1min, snap.highs_1min, ...)
features = IndicatorService.get_ai_features(snap)
```

**라인 범위**:
- calc_rsi(): 40~44, 22~38 (_calc_rsi_cached)
- calc_ema(): 54~57, 46~52 (_calc_ema_cached)
- calc_ma(): 68~70, 60~65 (_calc_ma_cached)
- calc_bollinger_bands(): 113~125
- get_trend_status(): 128~150
- get_ai_features(): 235~435

---

## 3️⃣ 신호 판단 (Signal Evaluation)

### SmartScanner 신호 판단

**파일**: [scanner/smart_scanner.py](scanner/smart_scanner.py)

```python
class SmartScanner:
    """실시간 신호 생성 (전 종목 감시)"""
    
    def _evaluate_signal(self, code: str) -> Optional[ScanSignal]:
        """
        특정 종목의 신호 판단
        
        위치: smart_scanner.py:700~760 (추정)
        호출처: run_periodic_scan(), 실시간 틱 처리
        
        흐름:
          1. SnapshotStore에서 스냅샷 조회
          2. 지표 계산 (RSI, EMA, 추세 등)
          3. 3가지 전략 신호 판정
          4. 신호 있으면 ScanSignal 반환
        """
        snap = self._snap_store.get_snapshot(code)
        if not snap: return None
        
        # 전략별 신호 판정 (순서대로)
        for strategy_class in [JDMEntryStrategy, BreakoutStrategy, PullbackStrategy]:
            strategy = strategy_class(...)
            sig = strategy.evaluate(snap, self._cfg)
            if sig:
                return sig
        
        return None
```

### 전략별 신호 판정

**파일**: [scanner/strategies/*.py](scanner/strategies/)

#### 1. JDM (장동민) 전략
```python
# scanner/strategies/jdm_entry.py
class JDMEntryStrategy:
    def evaluate(self, snap: StockSnapshot, cfg) -> Optional[ScanSignal]:
        """
        위치: scanner/strategies/jdm_entry.py:evaluate()
        
        신호 판정 기준:
          1. RSI > 50 (상승 시작)
          2. EMA10 > EMA20 (단기 강세)
          3. 거래량 > 20일 평균 (매수 에너지)
          4. 체결강도 > 130% (강한 수급)
          5. 기술적 패턴 (가격 형성 확인)
        """
        pass
```

#### 2. BREAKOUT 전략
```python
# scanner/strategies/breakout.py
class BreakoutStrategy:
    def evaluate(self, snap: StockSnapshot, cfg) -> Optional[ScanSignal]:
        """
        위치: scanner/strategies/breakout.py:evaluate()
        
        신호 판정 기준:
          1. 현재가 > 52주 최고가 또는 250일 최고가
          2. 거래량 서전 (Vol surge)
          3. 모멘텀 확인
        """
        pass
```

#### 3. PULLBACK 전략
```python
# scanner/strategies/pullback.py
class PullbackStrategy:
    def evaluate(self, snap: StockSnapshot, cfg) -> Optional[ScanSignal]:
        """
        위치: scanner/strategies/pullback.py:evaluate()
        
        신호 판정 기준:
          1. 장기 상승 추세 확인 (MA 정배열)
          2. 단기 조정 (RSI < 40)
          3. 지지선 재진입
        """
        pass
```

---

## 4️⃣ 신호 발생 (Signal Emission)

### SmartScanner 신호 발행

**파일**: [scanner/smart_scanner.py](scanner/smart_scanner.py)

```python
class SmartScanner(QObject):
    # PyQt5 신호 정의
    signal_detected = pyqtSignal(object)  # ScanSignal 발행
    
    def _emit(self, sig: ScanSignal) -> None:
        """
        신호 발행 및 기록
        
        위치: smart_scanner.py:763~823
        호출처: _maybe_emit_signal()
        
        절차:
          1. signal_detected.emit(sig) ← PyQt5 신호
          2. DB 저장 (signals 테이블)
          3. 파일 로그 (scanner.log)
          4. 알림 (Notification)
        """
        # 1. PyQt5 신호 발행
        self.signal_detected.emit(sig)  # ← 핵심 연결점
        
        # 2. DB 저장
        try:
            self._db.save_signal(sig)  # threading으로 비동기
        except Exception:
            pass
        
        # 3. 파일 로그
        logger.warning(
            "[신호] %s(%s) %s 신호 발생 (가격: %d원)",
            sig.name, sig.code, sig.signal_type, sig.price
        )
        
        # 4. 알림
        try:
            self._notify(f"신호: {sig.name}({sig.code}) {sig.signal_type}")
        except Exception:
            pass
    
    def _maybe_emit_signal(self, sig: ScanSignal) -> None:
        """
        신호 중복 방지 후 발행
        
        위치: smart_scanner.py:740~760
        
        중복 체크:
          - 이전 5분 내 동일 코드 신호 체크
          - 중복이면 발행 취소
        """
        if self._is_duplicate(sig):
            return
        self._emit(sig)
```

**라인 범위**:
- _emit(): 763~823
- _maybe_emit_signal(): 740~760

**신호 정의**:
- signal_detected = pyqtSignal(object) (라인 초반)

---

## 5️⃣ 신호 라우팅 (Signal Routing)

### SignalManager

**파일**: [ui/signal_manager.py](ui/signal_manager.py)

```python
class SignalManager:
    """신호를 여러 핸들러에 라우팅"""
    
    def _bind_scanner_core(self):
        """
        신호 라우팅 설정
        
        위치: ui/signal_manager.py:134~146
        호출처: __init__()
        
        연결:
          1. TradingController.handle_signal() ← 필터링 + 주문
          2. MainWindow._on_scan_signal() ← UI 로그 + 차트
          3. ScannerPanel.add_signal() ← 신호 목록 표시
        """
        if not ss: return
        
        # 1. 필터링 및 주문 (가장 중요)
        ss.signal_detected.connect(self.tc.handle_signal)  # 라인 139
        
        # 2. UI 로그 및 차트
        ss.signal_detected.connect(self.win._on_scan_signal)  # 라인 140
        
        # 3. 신호 목록 (ScannerPanel)
        ss.signal_detected.connect(self.win.scanner_panel.add_signal)  # 라인 142
```

**라인 범위**:
- _bind_scanner_core(): 134~146

---

## 6️⃣ 신호 필터링 (Signal Filtering)

### TradingController

**파일**: [app/trading_controller.py](app/trading_controller.py)

```python
class TradingController(QObject):
    """신호 필터링 + 주문 실행"""
    
    @pyqtSlot(object)
    def handle_signal(self, sig: ScanSignal) -> bool:
        """
        신호 필터 체인
        
        위치: app/trading_controller.py:202~276
        
        필터 순서:
          1. Phase1 태깅
          2. EntryStrategy 필터 (시간, 거래량)
          3. AI 필터 (ML 모델)
          4. RS 필터 (시장 강도)
          5. OrderManager 진입
        """
        # 1. Phase1 태깅
        if sig.signal_type == "OPENING_SCALP":
            sig.entry_phase = 1
            if ph1_count >= ph1_max:
                self.signal_rejected.emit(...)
                return False
        
        # 2. EntryStrategy 필터
        passed, reason = self._strategy.should_entry(sig, self._auto_trading)
        if not passed:
            self.signal_rejected.emit(...)
            return False
        
        # 3. AI 필터
        snap = self._snap_store.get_snapshot(sig.code)
        if snap:
            features = extract_ml_features(sig, snap, self._scan_cfg)
            ai_passed, win_rate = self._ai_filter.should_enter(features, threshold=0.5)
            if not ai_passed:
                self.signal_rejected.emit(...)
                return False
        
        # 4. RS 필터
        if snap:
            rs_score = snap.rs_score
            if rs_score < rs_threshold:
                self.signal_rejected.emit(...)
                return False
        
        # 5. OrderManager 진입
        self._order_mgr.handle_signal(sig)
        return True
```

**라인 범위**:
- handle_signal(): 202~276
- _strategy.should_entry(): strategy/base.py:should_entry()
- extract_ml_features(): analysis/feature_engineer.py:extract_ml_features()
- _ai_filter.should_enter(): app/ai_filter.py:should_enter()

---

## 7️⃣ 주문 실행 (Order Execution)

### OrderManager

**파일**: [order/order_manager.py](order/order_manager.py)

```python
class OrderManager(QObject):
    """주문 실행 (신호 처리 + 안전장치)"""
    
    def handle_signal(self, signal: ScanSignal) -> None:
        """
        신호 처리 및 주문 실행
        
        위치: order/order_manager.py:570~782
        
        절차:
          1. 신호 로그
          2. 강제 필터 (블랙리스트, 수동 중지)
          3. 신선도 체크 (3초 이상 지연 거절)
          4. 등락률 체크
          5. 섹터 체크
          6. 안전장치 (중복, 피라미딩)
          7. 수량 계산
          8. 수량 조정
          9. buy() 호출
        """
        # 1. 신호 로그
        logger.warning("[주문] %s(%s) 신호 처리 시작", sig.name, sig.code)
        
        # 2. 강제 필터
        if not self._is_buy_allowed(sig.code, sig.name):
            return
        
        # 3. 신선도 체크
        snap = self._snap_store.get_snapshot(sig.code)
        if not snap or snap.updated_at < (now - 3s):
            return
        
        # 4. 등락률 체크
        if snap.change_pct > max_change_pct:
            return
        
        # 5. 섹터 체크
        if same_sector_count >= max_positions:
            return
        
        # 6. 안전장치
        if sig.code in self._pending:
            return
        if not self._can_pyramid(sig.code):
            return
        
        # 7-8. 수량 계산 및 조정
        qty = self._calculate_dynamic_qty(...)
        qty = self._adjust_quantity(qty)
        
        # 9. 매수
        self.buy(sig.code, sig.name, qty, price=0)
    
    def buy(self, code: str, name: str, qty: int, price: int = 0) -> None:
        """
        매수 주문
        
        위치: order/order_manager.py:1064~1073
        호출처: handle_signal()
        """
        self._send(OrderType.BUY, code, name, qty, 0)
    
    def _send(self, order_type, code, name, qty, price) -> None:
        """
        최종 주문 전송
        
        위치: order/order_manager.py:1297~1373
        
        절차:
          1. 호가 단위 보정
          2. OrderExecutor 호출
          3. pending 추가
          4. order_sent 신호 발행
        """
        # 1. 호가 단위 보정
        price = self.align_price_to_hoga(price)
        
        # 2. OrderExecutor 호출
        result = self._order_executor.send(
            order_type, code, name, qty, price
        )
        
        # 3. pending 추가
        self._pending_orders[code] = {
            'order_id': result['order_id'],
            'qty': qty,
            'price': price,
        }
        
        # 4. order_sent 신호
        self.order_sent.emit(...)
```

**라인 범위**:
- handle_signal(): 570~782
- buy(): 1064~1073
- _send(): 1297~1373

---

## 8️⃣ 체결 처리 (Fill Processing)

### OrderManager 체결 콜백

**파일**: [order/order_manager.py](order/order_manager.py)

```python
class OrderManager(QObject):
    
    def _on_chejan_data(self, gubun, item_cnt, fid_list) -> None:
        """
        체결 데이터 처리
        
        위치: order/order_manager.py:1379~1432
        호출처: Kiwoom API (OnReceiveChejanData)
        
        절차:
          1. FID 파싱
          2. 체결 여부 판정
          3. _handle_buy_fill() 호출
          4. Position 생성
        """
        # 1. FID 파싱
        code = fid_list.get(9001)  # 종목코드
        qty = fid_list.get(910)    # 체결 수량
        price = fid_list.get(911)  # 체결 가격
        
        # 2. 체결 여부 판정
        if not self._is_buy_fill(fid_list):
            return
        
        # 3. _handle_buy_fill() 호출
        self._handle_buy_fill(code, qty, price, ...)
    
    def _handle_buy_fill(self, code, qty, price, ...) -> None:
        """
        매수 체결 처리
        
        위치: order/order_manager.py:1450+
        
        절차:
          1. Position 생성
          2. 손절/익절 설정
          3. HealthMonitor 기록
          4. positions 딕셔너리에 추가
        """
        # 1. Position 생성
        pos = Position(
            code=code,
            name=...,
            entry_price=price,
            qty=qty,
            entry_time=now,
        )
        
        # 2. 손절/익절 설정
        pos.stop_loss = price * (1 - stop_loss_pct / 100)
        pos.take_profit = price * (1 + take_profit_pct / 100)
        
        # 3. HealthMonitor 기록
        self._health_monitor.record_trade(...)
        
        # 4. positions에 추가
        self.positions[code] = pos
```

**라인 범위**:
- _on_chejan_data(): 1379~1432
- _handle_buy_fill(): 1450+

---

## 📊 프로세스 흐름 요약

```
[1] 지표 수집
    kiwoom_api.py:OnReceiveRealData
    → snapshot_store.py:update_price()
    → snapshot_store.py:update_ohlc()

    ↓

[2] 지표 계산
    smart_scanner.py:_evaluate_signal()
    → indicator_service.py:calc_rsi(), calc_ema(), ...
    → snapshot_store.py:get_snapshot()

    ↓

[3] 신호 판단
    strategies/jdm_entry.py:evaluate()
    strategies/breakout.py:evaluate()
    strategies/pullback.py:evaluate()

    ↓

[4] 신호 발생
    smart_scanner.py:_emit()
    → signal_detected.emit(sig)
    → DB 저장, 파일 로그, 알림

    ↓

[5] 신호 라우팅
    ui/signal_manager.py:_bind_scanner_core()
    → TradingController.handle_signal()
    → MainWindow._on_scan_signal()
    → ScannerPanel.add_signal()

    ↓

[6] 신호 필터링
    app/trading_controller.py:handle_signal()
    → EntryStrategy.should_entry()
    → AIFilter.should_enter()
    → RS 필터

    ↓

[7] 주문 실행
    order/order_manager.py:handle_signal()
    → order_manager.py:buy()
    → order_manager.py:_send()
    → OrderExecutor.send()

    ↓

[8] 체결 처리
    kiwoom_api.py:OnReceiveChejanData
    → order_manager.py:_on_chejan_data()
    → order_manager.py:_handle_buy_fill()
    → Position 생성
```

---

**작성일**: 2026-05-08  
**상태**: ✅ 완료  
**신뢰도**: 높음
