"""QQ 官方机器人适配器。"""

from .adapter import QQBotAdapter, QQDependencyError
from .events import normalize_c2c_message, normalize_group_message

__all__ = [
    "QQBotAdapter",
    "QQDependencyError",
    "normalize_c2c_message",
    "normalize_group_message",
]
