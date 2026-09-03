from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from application.contracts import PlatformEvent, PlatformSession, SessionType


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _author_value(message: Any, name: str, default: Any = None) -> Any:
    author = _value(message, "author", {}) or {}
    return _value(author, name, default)


def _union_openid(message: Any) -> str | None:
    """尽力取出 union_openid —— 唯一能把群作用域与单聊作用域对上的字段。

    QQ 群消息只给 member_openid、单聊消息只给 user_openid，两者互不可转换，
    官方也没有换算接口。union_openid 若下发，就能让「群里入场 → 私聊收身份」
    完全无感。当前 qq-botpy 的 GroupMessage/C2CMessage 把 author 包装成只保留
    单个字段的内部对象，取不到时返回 None，由 /link 一次性码兜底。
    """
    for holder in (message, _value(message, "author", {}) or {}, _value(message, "member", {}) or {}):
        value = _value(holder, "union_openid") or _value(holder, "union_user_account")
        if value:
            return str(value)
    return None


def normalize_group_message(message: Any) -> PlatformEvent:
    group_id = _value(message, "group_openid")
    user_id = _author_value(message, "member_openid") or _author_value(message, "id")
    if not group_id or not user_id:
        raise ValueError("QQ 群消息缺少会话或用户标识")
    member = _value(message, "member", {}) or {}
    display_name = (
        _value(member, "nick")
        or _author_value(message, "username")
        or _author_value(message, "nick")
        or _author_value(message, "global_name")
        # 拿不到昵称时留空，而不是回落成 member_openid：display_name 是纯展示字段，
        # 塞进内部标识就会在群播报里漏出 32 位十六进制串。
        or ""
    )
    event_id = str(_value(message, "event_id") or _value(message, "id") or "")
    if not event_id:
        raise ValueError("QQ 群消息缺少事件标识")
    reference = _value(message, "message_reference", {}) or {}
    reply_id = _value(reference, "message_id") or _value(message, "id")
    return PlatformEvent(
        event_id=event_id,
        session=PlatformSession(SessionType.GROUP, str(group_id)),
        user_id=str(user_id),
        display_name=str(display_name)[:64],
        text=str(_value(message, "content", "") or "").strip(),
        occurred_at=_parse_time(_value(message, "timestamp")),
        reply_message_id=str(reply_id) if reply_id else None,
        raw=message,
        is_bot=bool(_author_value(message, "bot", False)),
        union_id=_union_openid(message),
    )


def normalize_channel_message(message: Any) -> PlatformEvent:
    channel_id = _value(message, "channel_id") or _value(message, "guild_id")
    user_id = _author_value(message, "id") or _author_value(message, "user_openid")
    if not channel_id or not user_id:
        raise ValueError("QQ 频道消息缺少会话或用户标识")
    event_id = str(_value(message, "event_id") or _value(message, "id") or "")
    if not event_id:
        raise ValueError("QQ 频道消息缺少事件标识")
    display_name = _author_value(message, "username") or ""
    return PlatformEvent(
        event_id=event_id,
        session=PlatformSession(SessionType.CHANNEL, str(channel_id)),
        user_id=str(user_id),
        display_name=str(display_name)[:64],
        text=str(_value(message, "content", "") or "").strip(),
        occurred_at=_parse_time(_value(message, "timestamp")),
        reply_message_id=str(_value(message, "id") or "") or None,
        raw=message,
        is_bot=bool(_author_value(message, "bot", False)),
    )


def normalize_c2c_message(message: Any) -> PlatformEvent:
    author_id = _author_value(message, "user_openid") or _author_value(message, "id")
    if not author_id:
        raise ValueError("QQ C2C 消息缺少用户标识")
    event_id = str(_value(message, "event_id") or _value(message, "id") or "")
    if not event_id:
        raise ValueError("QQ C2C 消息缺少事件标识")
    display_name = (
        _author_value(message, "username")
        or _author_value(message, "nick")
        or _author_value(message, "global_name")
        or ""
    )
    reference = _value(message, "message_reference", {}) or {}
    reply_id = _value(reference, "message_id") or _value(message, "id")
    return PlatformEvent(
        event_id=event_id,
        session=PlatformSession(SessionType.C2C, str(author_id)),
        user_id=str(author_id),
        display_name=str(display_name)[:64],
        text=str(_value(message, "content", "") or "").strip(),
        occurred_at=_parse_time(_value(message, "timestamp")),
        reply_message_id=str(reply_id) if reply_id else None,
        raw=message,
        is_bot=bool(_author_value(message, "bot", False)),
        union_id=_union_openid(message),
    )


def normalize_direct_message(message: Any) -> PlatformEvent:
    """把 QQ 官方私信（DirectMessage）结构转换成统一的平台事件。"""
    user_id = _author_value(message, "id")
    guild_id = _value(message, "guild_id")
    if not user_id or not guild_id:
        raise ValueError("QQ 私信缺少会话或用户标识")
    event_id = str(_value(message, "event_id") or _value(message, "id") or "")
    if not event_id:
        raise ValueError("QQ 私信缺少事件标识")
    return PlatformEvent(
        event_id=event_id,
        session=PlatformSession(SessionType.DIRECT, str(guild_id)),
        user_id=str(user_id),
        display_name=str(_author_value(message, "username") or "")[:64],
        text=str(_value(message, "content", "") or "").strip(),
        occurred_at=_parse_time(_value(message, "timestamp")),
        reply_message_id=str(_value(message, "id") or "") or None,
        raw=message,
        is_bot=bool(_author_value(message, "bot", False)),
    )
