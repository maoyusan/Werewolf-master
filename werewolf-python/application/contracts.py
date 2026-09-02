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
