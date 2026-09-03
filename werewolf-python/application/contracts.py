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
    """平台返回了重试也没用的发送失败（权限、锚点失效、参数非法）。

    定义在 application 层是为了让投递循环不必反向依赖某个具体平台适配器：
    任何适配器只要抛出它，队列就知道该一次判死而不是退避重试。
    """


class NeedsAnchorSendError(PermanentSendError):
    """这条消息现在发不出去，但同会话下一条真实用户消息可以救回来。

    典型是 QQ 单聊报「无好友关系」、或群聊报「主动消息失败, 无权限」：
    把机器人拉进群、在资料页点添加，都不等于官方接口认可的可推送会话。
    用户再发一条私聊/群消息后，就能用那条 msg_id 做被动回复。
    投递循环应把它挂成 waiting，而不是重试五次变成 failed，也不是一次判死。
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
    # QQ 对同一个人在群和单聊下发两个不同的 openid（member_openid / user_openid），
    # 官方没有换算接口。union_openid 是唯一能把两者对上的字段，平台若下发就带过来，
    # 应用层据此自动建立映射；拿不到时留 None，走 /link 一次性码。
    union_id: str | None = None


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
    # True 表示这条私聊消息现在没有可用的单聊标识（只有群作用域 openid），
    # 直接调接口必然是「无好友关系」。入库时挂 waiting，等玩家 /link 关联成功后
    # 由 link_openid 改写目标并重新入队。不参与 delivery_id 计算：它只是投递
    # 策略，同一条消息不该因为策略变化生成两个不同的投递号。
    requires_anchor: bool = False

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
