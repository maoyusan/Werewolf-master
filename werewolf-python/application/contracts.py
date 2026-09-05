from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SessionType(str, Enum):
    GROUP = "group"
    C2C = "c2c"
    DIRECT = "direct"
    CHANNEL = "channel"
    OTHER = "other"


class TransientSendError(RuntimeError):
    """平台返回了可以重试的发送失败（限流、超时、网络抖动）。"""


class PermanentSendError(RuntimeError):
    """平台返回了重试也没用的发送失败（被拉黑、参数非法、目标不存在）。

    定义在 application 层是为了让投递循环不必反向依赖某个具体平台适配器：
    任何适配器只要抛出它，队列就知道该一次判死而不是退避重试。
    """


@dataclass(frozen=True)
class PlatformSession:
    session_type: SessionType
    session_id: str

    @property
    def key(self) -> str:
        return f"{self.session_type.value}:{self.session_id}"


@dataclass(frozen=True)
class PlatformEvent:
    event_id: str
    session: PlatformSession
    user_id: str
    display_name: str
    text: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reply_message_id: str | None = None
    raw: Any = None
    is_bot: bool = False
    # NapCat/OneBot 下的群角色：owner / admin / member。私聊事件为 None。
    # 用来判断发起人是不是群主群管，也用来判断机器人自己有没有改名片的权限。
    group_role: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    target: PlatformSession
    text: str
    room_id: str | None = None
    reply_to: str | None = None
    source_event_id: str | None = None
    event_id: str | None = None
    # 同一批次内的序号。仅用于让「同一会话、同样文案」的多条消息拥有不同的
    # delivery_id，避免入库时被 ON CONFLICT DO NOTHING 静默丢弃。
    sequence: int = 0

    @property
    def delivery_id(self) -> str:
        value = "|".join(
            (
                self.target.key,
                self.text,
                self.room_id or "",
                self.reply_to or "",
                self.source_event_id or "",
                self.event_id or "",
                str(self.sequence),
            )
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
