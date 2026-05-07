import re
import os

path = r'd:\prj\kiwoom-auto\scanner\smart_scanner.py'
with open(path, 'r', encoding='utf-8', errors='ignore') as f:
    content = f.read()

# 정규표현식으로 깨진 구간 찾기
# ro\s+if all_codes: 로 시작해서 e\) 로 끝나는 구간
pattern = r'ro\s+if all_codes:.*?e\)'
replacement = """if all_codes:
                # [OPTIMIZED] 전 종목(2,500+)에 대해 루프를 도는 것은 느리고 이름 누락 시 필터링에서 탈락함.
                # 1. 전일 거래대금 기준으로 먼저 정렬하여 상위 후보군(예: 400개) 선정
                sorted_codes = sorted(
                    all_codes, 
                    key=lambda c: self.universe_mgr._prev_volumes.get(c, 0), 
                    reverse=True
                )[:self.cfg.collect_raw_top_n]

                fallback_rows = []
                for code in sorted_codes:
                    pv = self.universe_mgr._prev_volumes.get(code, 0)
                    if pv <= 0: continue
                    
                    # 상위 후보군에 대해서만 최소한의 정보 확보
                    name = self.store.get_name(code)
                    if not name:
                        # 스토어에 없으면 OCX에서 직접 가져옴 (상위 N개에 대해서만 수행하므로 빠름)
                        name = self._kiwoom.get_stock_name(code)
                    
                    # 현재가 복구 시도
                    snap = self.store.get_snapshot(code)
                    cur_p = snap.current_price if snap and snap.current_price > 0 else 0
                    if cur_p == 0:
                        self.store.load_1min_for_code(code)
                        st = self.store.get_internal_state(code)
                        if st and st.mins:
                            cur_p = int(st.mins[-1])
                    
                    # [NEW] 등락률 복구 (0.0으로 대체하지 않고 전일종가 대비 계산)
                    master_p = self._kiwoom.get_master_last_price(code)
                    chg_pct = 0.0
                    if cur_p > 0 and master_p > 0:
                        chg_pct = round((cur_p - master_p) / master_p * 100, 2)
                    
                    fallback_rows.append({
                        "code": code,
                        "name": name,
                        "trade_amount": pv * 1000,
                        "volume": pv,
                        "current_price": cur_p,
                        "change_pct": chg_pct,
                        "prev_close": master_p
                    })
                
                if fallback_rows:
                    logger.info("[주기 스캔] 전일 캐시 기반 %d종목 폴백 유니버스 생성 완료 (이름/등락률 복구 포함)", len(fallback_rows))
                    return fallback_rows
        except Exception as e:
            logger.warning("[주기 스캔] 예외 발생: %s", e)"""

new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

with open(path, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Fix completed.")
