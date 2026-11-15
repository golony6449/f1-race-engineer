"""추월 선택지: 앞차와 가까울 때 지금 고를 수 있는 방법을 무전으로 만든다(설계 6.5).

- `advisor.py`: DRS·느린 탈출·타이어 기다림 선택지(모든 트랙).
- `strategy.py`: 추월 지점(`overtaking_spots`)이 있는 서킷에서 공격·방어 계획을
  고르는 규칙 엔진(SCRUM-130). 지점 데이터가 없으면 아무 계획도 만들지 않는다.
"""

from f1re.overtake.advisor import OvertakeAdvisor, OvertakeConfig
from f1re.overtake.strategy import OvertakeStrategy, StrategyConfig, StrategyPlan

__all__ = [
    "OvertakeAdvisor",
    "OvertakeConfig",
    "OvertakeStrategy",
    "StrategyConfig",
    "StrategyPlan",
]
