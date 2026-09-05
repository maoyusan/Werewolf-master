from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from application.contracts import PlatformEvent, PlatformSession, SessionType


# OneBot 的 CQ 码（[CQ:at,qq=123]、[CQ:image,file=...] 等）。
# 纯文本兜底路径要把它整段剥掉，避免 @ 出现在指令文本里。
_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")


def _value(payload: Any, name: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(name, default)
    return getattr(payload, name, default)


def _parse_time(value: Any) -> datetime:
    """OneBot 的 time 字段是秒级 Unix 时间戳。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if seconds <= 0:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _segments_text(message: Any) -> str:
    """把 OneBot 的消息段数组拼成纯文本。

    只保留 text 段：at 段一律丢弃。NapCat 版不再要求玩家 @Bot 才能触发指令，
    需要指定目标的指令统一用座位号，不依赖 @ 谁。
    """
    if isinstance(message, str):
        return _CQ_CODE.sub(" ", message)
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        data = segment.get("data") or {}
        parts.append(str(data.get("text", "") or ""))
    return "".join(parts)


def message_text(payload: Any) -> str:
    """取出一条 OneBot 消息的可解析文本（array 格式优先，string 格式兜底）。"""
    text = _segments_text(_value(payload, "message"))
    if not text.strip():
        text = _CQ_CODE.sub(" ", str(_value(payload, "raw_message") or ""))
    return text.strip()


def _sender_name(payload: Any) -> str:
    """展示用昵称。

    刻意优先 nickname 而不是群名片：开局后机器人会把参赛者的群名片改成
    「N号」，若此处取 card，玩家名字就会被自己改出来的号码覆盖，
    名单只剩「1号 1号」。nickname 是 QQ 昵称，不受名片改写影响。
    """
    sender = _value(payload, "sender", {}) or {}
    return str(
        _value(sender, "nickname")
        or _value(sender, "card")
        or ""
    )[:64]


def _event_id(payload: Any) -> str:
    message_id = _value(payload, "message_id")
    if message_id in (None, ""):
        raise ValueError("OneBot 消息缺少 message_id")
    return f"napcat:{message_id}"


def normalize_group_message(payload: Any) -> PlatformEvent:
    """群消息 → 平台事件。会话号是群号，用户号是真实 QQ 号。"""
    group_id = _value(payload, "group_id")
    user_id = _value(payload, "user_id")
    if group_id in (None, "") or user_id in (None, ""):
        raise ValueError("OneBot 群消息缺少会话或用户标识")
    sender = _value(payload, "sender", {}) or {}
    role = str(_value(sender, "role") or "") or None
    message_id = _value(payload, "message_id")
    return PlatformEvent(
        event_id=_event_id(payload),
        session=PlatformSession(SessionType.GROUP, str(group_id)),
        user_id=str(user_id),
        display_name=_sender_name(payload),
        text=message_text(payload),
        occurred_at=_parse_time(_value(payload, "time")),
        reply_message_id=str(message_id) if message_id not in (None, "") else None,
        raw=payload,
        is_bot=False,
        group_role=role,
    )


def normalize_private_message(payload: Any) -> PlatformEvent:
    """私聊消息 → 平台事件。会话号与用户号都是真实 QQ 号。"""
    user_id = _value(payload, "user_id")
    if user_id in (None, ""):
        raise ValueError("OneBot 私聊消息缺少用户标识")
    message_id = _value(payload, "message_id")
    return PlatformEvent(
        event_id=_event_id(payload),
        session=PlatformSession(SessionType.C2C, str(user_id)),
        user_id=str(user_id),
        display_name=_sender_name(payload),
        text=message_text(payload),
        occurred_at=_parse_time(_value(payload, "time")),
        reply_message_id=str(message_id) if message_id not in (None, "") else None,
        raw=payload,
        is_bot=False,
    )
