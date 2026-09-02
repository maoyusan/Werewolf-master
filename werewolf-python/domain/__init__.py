"""领域层：与平台无关的狼人杀核心规则与状态机。"""

from .engine import GameRoomEngine, GameRuleError
from .models import GamePhase, GameRoom, GameRules, Role, Team
from .rules import ruleset_v1

__all__ = [
    "GameRoom",
    "GameRoomEngine",
    "GameRuleError",
    "GamePhase",
    "GameRules",
    "Role",
    "Team",
    "ruleset_v1",
]
