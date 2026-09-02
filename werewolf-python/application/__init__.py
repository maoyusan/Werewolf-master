"""应用层：与平台无关的游戏服务编排。"""

from .commands import Command, parse_command
from .contracts import OutboundMessage, PlatformEvent, PlatformSession, SessionType
from .service import GameApplication

__all__ = [
    "Command",
    "GameApplication",
    "OutboundMessage",
    "PlatformEvent",
    "PlatformSession",
    "SessionType",
    "parse_command",
]
