"""
overheat_pullback_backtest.py — Phase 2: 로그 데이터 기반 백테스팅 헬퍼

역할:
  1. 과거 거래 로그에서 급등 종목의 1분봉 데이터 추출
  2. OverheatPullbackEvaluator 평가
  3. 타점 정확도(Hit Rate, Win Rate) 분석
  4. 파라미터 튜닝 제안

사용 예시:
  ```python
  backtester = OverheatPullbackBacktester()
  backtester.analyze_historical_trade(
      code='005930',
      start_date='2026-05-13',
      end_date='2026-05-20',
  )
  backtester.print_summary_report()
  ```
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, date as Date
import json
import os
from scanner.evaluators.overheat_pullback import OverheatPullbackEvaluator


class OverheatPullbackBacktester:
    """
    로그 데이터 기반 백테스팅 및 파라미터 최적화.

    Phase 2 로드맵:
      1. 거래 로그에서 수익 종목(+10% 이상) 자동 필터
      2. 각 종목의 1분봉 데이터로 OverheatPullback 신호 발생 검증
      3. Hit Rate (신호 정확도) / Win Rate (수익률) 분석
      4. 최적 파라미터 추천 (threshold, volume_mult, lookback_period 등)
    """

    def __init__(self, evaluator: Optional[OverheatPullbackEvaluator] = None):
        """
        Args:
            evaluator: OverheatPullbackEvaluator (None이면 기본값 생성)
        """
        self.evaluator = evaluator or OverheatPullbackEvaluator()
        self.test_results: List[Dict[str, Any]] = []

    def simulate_single_trade(
        self,
        code: str,
        name: str,
        candle_history: List[Dict[str, Any]],
        entry_price: float,
        peak_price: float,
        daily_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        단일 거래에 대한 OverheatPullback 시뮬레이션.

        Args:
            code: 종목코드
            name: 종목명
            candle_history: 1분봉 데이터
            entry_price: 실제 진입 가격
            peak_price: 거래 중 기록한 최고가
            daily_info: 일봉 정보

        Returns:
            {
                'code': '005930',
                'name': '삼성전자',
                'signal_matched': bool,
                'entry_price': 70000,
                'peak_price': 71000,
                'profit_pct': 1.4,
                'evaluator_result': {...}
            }
        """
        # OverheatPullback 평가
        eval_result = self.evaluator.evaluate(
            candle_history=candle_history,
            daily_info=daily_info,
            code=code,
            name=name,
        )

        # 실제 수익률 계산
        profit_pct = (peak_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

        return {
            'code': code,
            'name': name,
            'signal_matched': eval_result['is_buy_signal'],
            'signal_reason': eval_result['reason'],
            'entry_price': round(entry_price, 0),
            'peak_price': round(peak_price, 0),
            'profit_pct': round(profit_pct, 2),
            'evaluator_result': eval_result,
        }

    def analyze_historical_trade(
        self,
        code: str,
        name: str,
        trade_date: str,
        entry_price: float,
        peak_price: float,
        candle_history: List[Dict[str, Any]],
        daily_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        과거 거래 1건을 분석하고 결과를 저장.

        Args:
            code: 종목코드
            name: 종목명
            trade_date: 거래일 (YYYY-MM-DD)
            entry_price: 진입가
            peak_price: 최고가
            candle_history: 거래 당일 1분봉 데이터
            daily_info: 당일 일봉 정보

        Returns:
            시뮬레이션 결과
        """
        result = self.simulate_single_trade(
            code=code,
            name=name,
            candle_history=candle_history,
            entry_price=entry_price,
            peak_price=peak_price,
            daily_info=daily_info,
        )
        result['trade_date'] = trade_date
        self.test_results.append(result)
        return result

    def calculate_hit_rate(self) -> float:
        """
        신호 적중률 계산 (실제 수익 거래 중 OverheatPullback 신호 발생 비율).

        Returns:
            float: 0.0 ~ 1.0 (예: 0.65 = 65%)
        """
        if not self.test_results:
            return 0.0

        profitable_trades = [r for r in self.test_results if r['profit_pct'] > 0]
        if not profitable_trades:
            return 0.0

        signal_matched = sum(1 for r in profitable_trades if r['signal_matched'])
        return signal_matched / len(profitable_trades) if profitable_trades else 0.0

    def calculate_win_rate(self) -> float:
        """
        승률 계산 (OverheatPullback 신호 발생 거래 중 수익 거래 비율).

        Returns:
            float: 0.0 ~ 1.0
        """
        if not self.test_results:
            return 0.0

        signaled_trades = [r for r in self.test_results if r['signal_matched']]
        if not signaled_trades:
            return 0.0

        winning = sum(1 for r in signaled_trades if r['profit_pct'] > 0)
        return winning / len(signaled_trades)

    def calculate_average_profit(self) -> float:
        """
        OverheatPullback 신호 발생 거래의 평균 수익률.

        Returns:
            float: 평균 수익률 (%)
        """
        signaled_trades = [r for r in self.test_results if r['signal_matched']]
        if not signaled_trades:
            return 0.0

        return sum(r['profit_pct'] for r in signaled_trades) / len(signaled_trades)

    def print_summary_report(self) -> None:
        """
        분석 결과 요약 보고서 출력.
        """
        total_trades = len(self.test_results)
        profitable_trades = sum(1 for r in self.test_results if r['profit_pct'] > 0)
        signaled_trades = sum(1 for r in self.test_results if r['signal_matched'])

        hit_rate = self.calculate_hit_rate()
        win_rate = self.calculate_win_rate()
        avg_profit = self.calculate_average_profit()

        print("=" * 70)
        print("[ Phase 2: OverheatPullback 백테스팅 리포트 ]")
        print("=" * 70)
        print(f"총 거래 건수: {total_trades}")
        print(f"수익 거래: {profitable_trades}건 ({profitable_trades/total_trades*100:.1f}%)")
        print(f"신호 발생: {signaled_trades}건 ({signaled_trades/total_trades*100:.1f}%)")
        print()
        print("[성과 지표]")
        print(f"  • Hit Rate (수익 거래 중 신호 발생): {hit_rate*100:.1f}%")
        print(f"  • Win Rate (신호 거래 중 수익): {win_rate*100:.1f}%")
        print(f"  • Avg Profit (신호 거래 평균): {avg_profit:+.2f}%")
        print()
        print("[상세 분석]")
        for result in self.test_results[:10]:  # 최근 10건
            status = "✓" if result['signal_matched'] else "✗"
            profit_symbol = "+" if result['profit_pct'] > 0 else ""
            print(f"  {status} {result['code']} {result['name']:10s} | "
                  f"수익 {profit_symbol}{result['profit_pct']:6.2f}% | "
                  f"신호: {result['signal_reason']}")
        print("=" * 70)

    def suggest_parameter_tuning(self) -> Dict[str, Any]:
        """
        백테스트 결과 기반 파라미터 튜닝 제안.

        Returns:
            {
                'hit_rate': 0.65,
                'recommendations': [
                    {'param': 'level_3_threshold', 'current': 1.5, 'suggested': 1.3},
                    ...
                ]
            }
        """
        hit_rate = self.calculate_hit_rate()
        win_rate = self.calculate_win_rate()

        recommendations = []

        # Hit Rate 기반 제안
        if hit_rate < 0.50:
            recommendations.append({
                'param': 'level_3_threshold',
                'current': 1.5,
                'suggested': 1.3,
                'reason': f'Hit Rate {hit_rate*100:.1f}% 낮음. 과열 기준을 완화하면 더 많은 신호 포착 가능',
            })

        if win_rate < 0.40:
            recommendations.append({
                'param': 'volume_surge_mult',
                'current': 2.0,
                'suggested': 1.8,
                'reason': f'Win Rate {win_rate*100:.1f}% 낮음. 거래대금 필터를 완화하여 더 많은 기회 포착',
            })

        if win_rate > 0.70:
            recommendations.append({
                'param': 'lookback_minutes',
                'current': 10,
                'suggested': 15,
                'reason': f'Win Rate {win_rate*100:.1f}% 높음. 더 긴 과열 이력 확인으로 신뢰도 추가 강화',
            })

        return {
            'hit_rate': round(hit_rate, 3),
            'win_rate': round(win_rate, 3),
            'avg_profit': round(self.calculate_average_profit(), 2),
            'recommendations': recommendations,
        }

    def export_results_to_json(self, filepath: str) -> None:
        """
        테스트 결과를 JSON으로 저장 (후속 분석용).

        Args:
            filepath: 저장 경로 (예: 'backtest_results_20260513.json')
        """
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({
                'test_timestamp': datetime.now().isoformat(),
                'total_tests': len(self.test_results),
                'hit_rate': self.calculate_hit_rate(),
                'win_rate': self.calculate_win_rate(),
                'avg_profit': self.calculate_average_profit(),
                'results': self.test_results,
            }, f, indent=2, ensure_ascii=False)

        print(f"✓ 결과 저장: {filepath}")


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3 준비: 실전 배포 시 신호 모니터링 클래스
# ──────────────────────────────────────────────────────────────────────────────

class OverheatPullbackMonitor:
    """
    실전 배포 시 OverheatPullback 신호를 모니터링하고 대시보드에 표시.

    Phase 3: 자동매매 바인딩 전 수동 매수 유도 및 검증.
    """

    def __init__(self):
        """신호 히스토리 및 모니터링 상태 초기화."""
        self.signal_history: List[Dict[str, Any]] = []
        self.monitored_codes: set = set()

    def record_signal(
        self,
        code: str,
        name: str,
        signal_result: Dict[str, Any],
        current_price: float,
    ) -> None:
        """
        신호 발생 시 기록 (대시보드 표시용).

        Args:
            code: 종목코드
            name: 종목명
            signal_result: OverheatPullbackEvaluator 반환값
            current_price: 신호 발생 시점의 종가
        """
        if signal_result['is_buy_signal']:
            self.signal_history.append({
                'timestamp': datetime.now().isoformat(),
                'code': code,
                'name': name,
                'price': round(current_price, 0),
                'reason': signal_result['reason'],
                'debug': signal_result.get('debug_info', {}),
            })
            self.monitored_codes.add(code)

    def get_pending_signals(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        대기 중인 신호 반환 (최근 N개).

        Args:
            limit: 반환할 최대 신호 건수

        Returns:
            최근 신호 리스트
        """
        return self.signal_history[-limit:]

    def export_for_dashboard(self) -> Dict[str, Any]:
        """
        대시보드 표시용 데이터 포맷 반환.

        Returns:
            {
                'total_signals': 10,
                'active_codes': ['005930', '000660', ...],
                'recent_signals': [...]
            }
        """
        return {
            'total_signals': len(self.signal_history),
            'active_codes': list(self.monitored_codes),
            'recent_signals': self.get_pending_signals(5),
        }
