from __future__ import annotations

import asyncio
import logging
from typing import Any

from application.contracts import PlatformEvent, SessionType
from infrastructure.db import DeliveryRecord

from .events import (
    normalize_c2c_message,
    normalize_channel_message,
    normalize_direct_message,
    normalize_group_message,
)


log = logging.getLogger(__name__)

_TRANSIENT_MARKERS = ("429", "rate", "timeout", "temporar", "503", "502", "reset", "connect")

_GROUP_PANEL_REMARK = "werewolf-python-group-panel"
# 面板条目顺序按玩家实际使用顺序排列：先开局流程，再查询，最后是设置和杂项。
# 全部 only_admin=False，否则普通玩家在 QQ 指令面板里看不到这些指令。
_GROUP_PANEL_ITEMS = (
    {"type": "command", "name": "/startgame", "desc": "开始一局狼人杀", "only_admin": False},
    {"type": "command", "name": "/join", "desc": "加入当前对局", "only_admin": False},
    {"type": "command", "name": "/go", "desc": "发起人立刻开局", "only_admin": False},
    {"type": "command", "name": "/leave", "desc": "退出当前对局", "only_admin": False},
    {"type": "command", "name": "/cancel", "desc": "取消当前房间", "only_admin": False},
    {"type": "command", "name": "/status", "desc": "查看玩家和进度", "only_admin": False},
    {"type": "command", "name": "/players", "desc": "查看存活玩家列表", "only_admin": False},
    {"type": "command", "name": "/vote", "desc": "白天投票放逐", "only_admin": False},
    {"type": "command", "name": "/help", "desc": "查看玩法和指令", "only_admin": False},
    {"type": "command", "name": "/rolelist", "desc": "查看身份列表", "only_admin": False},
    {"type": "command", "name": "/stats", "desc": "查看本局数据", "only_admin": False},
    {"type": "command", "name": "/config", "desc": "查看或修改本群规则", "only_admin": False},
    {"type": "command", "name": "/nextgame", "desc": "准备下一局", "only_admin": False},
    {"type": "command", "name": "/ping", "desc": "检查机器人状态", "only_admin": False},
    {"type": "command", "name": "/startchaos", "desc": "混乱模式开局", "only_admin": False},
    {"type": "command", "name": "/version", "desc": "查看机器人版本", "only_admin": False},
)


class QQDependencyError(RuntimeError):
    """官方 QQ SDK 缺失或无法导入时抛出。"""


class TransientSendError(RuntimeError):
    """官方接口返回了可重试的发送失败。"""


class QQBotAdapter:
    """对官方 ``qq-botpy`` Gateway/API 客户端的轻量桥接层。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        application: Any,
        *,
        store: Any | None = None,
        reconnect_seconds: float = 2.0,
        max_reconnect_seconds: float = 60.0,
        min_send_interval: float = 0.08,
    ):
        if not app_id or not app_secret:
            raise ValueError("AppID/AppSecret 必须在运行时提供")
        self.app_id = app_id
        self.app_secret = app_secret
        self.application = application
        self.store = store
        self.reconnect_seconds = reconnect_seconds
        self.max_reconnect_seconds = max_reconnect_seconds
        self.min_send_interval = min_send_interval
        self.ready = asyncio.Event()
        self._client: Any = None
        self._route_factory: Any = None
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0
        self._stop = asyncio.Event()

    @property
    def is_ready(self) -> bool:
        return self.ready.is_set()

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("QQ 客户端尚未初始化")
        return self._client

    def build_client(self) -> Any:
        try:
            import botpy
            from botpy.http import Route
        except ImportError as exc:
            raise QQDependencyError("缺少官方 QQ SDK qq-botpy==1.2.1") from exc
        self._route_factory = Route

        adapter = self

        class OfficialClient(botpy.Client):
            async def _bot_login(self, token: Any) -> None:
                await super()._bot_login(token)

                # QQ 的群聊指令菜单会下发 GROUP_MESSAGE_CREATE。qq-botpy 1.2.1
                # 能收到该事件，却没有内置解析器，导致菜单指令被直接丢弃。
                from botpy.message import GroupMessage

                def parse_group_message_create(payload: dict[str, Any]) -> None:
                    message = GroupMessage(
                        self.api, payload.get("id"), payload.get("d", {})
                    )
                    self._connection.state._dispatch("group_message_create", message)

                self._connection.parser["group_message_create"] = parse_group_message_create

            async def on_ready(self) -> None:
                adapter.ready.set()
                robot = getattr(self, "robot", None)
                robot_id = getattr(robot, "id", None) or getattr(robot, "username", None)
                log.info("QQ 网关已就绪", extra={"session_id": str(robot_id or "ready")})
                if adapter.store is not None:
                    await adapter.store.save_gateway_session(
                        shard_id=0,
                        session_id=str(getattr(self, "session_id", "") or robot_id or ""),
                        last_sequence=int(getattr(self, "sequence", 0) or 0),
                    )
                try:
                    await adapter.ensure_group_command_panel()
                except Exception:
                    log.exception("QQ 群指令面板初始化失败，机器人继续启动")

            async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
                log.error("QQ 事件回调执行失败", extra={"reason": str(event_method)[:120]})

            async def on_group_at_message_create(self, message: Any) -> None:
                await adapter.dispatch_raw_message(
                    normalize_group_message, message, source="group_at_message_create"
                )

            async def on_group_message_create(self, message: Any) -> None:
                await adapter.dispatch_raw_message(
                    normalize_group_message, message, source="group_message_create"
                )

            async def on_c2c_message_create(self, message: Any) -> None:
                await adapter.dispatch_raw_message(
                    normalize_c2c_message, message, source="c2c_message_create"
                )

            async def on_direct_message_create(self, message: Any) -> None:
                await adapter.dispatch_raw_message(
                    normalize_direct_message, message, source="direct_message_create"
                )

            async def on_at_message_create(self, message: Any) -> None:
                await adapter.dispatch_raw_message(
                    normalize_channel_message, message, source="at_message_create"
                )

        self._client = OfficialClient(
            intents=botpy.Intents(
                public_messages=True,
                public_guild_messages=True,
                direct_message=True,
            ),
            bot_log=False,
        )
        return self._client

    async def run(self) -> None:
        delay = self.reconnect_seconds
        while not self._stop.is_set():
            try:
                client = self.build_client()
                await client.start(self.app_id, self.app_secret)
                if self._stop.is_set():
                    return
                log.warning("QQ 网关连接已断开，准备重连", extra={"reason": "client_stopped"})
            except QQDependencyError:
                raise
            except Exception:
                log.exception("QQ 网关会话异常结束，准备重连")
            self.ready.clear()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                delay = min(self.max_reconnect_seconds, delay * 2)

    async def close(self) -> None:
        self._stop.set()
        if self._client is not None and not self._client.is_closed():
            await self._client.close()
        self.ready.clear()

    async def ensure_group_command_panel(self) -> None:
        """确保本机器人的官方群指令面板存在，且条目与 ``_GROUP_PANEL_ITEMS`` 一致。

        面板条目变更后必须同步到官方，否则新增的 /go 等指令永远不会出现在
        QQ 的指令面板里。任何一步失败都只记日志，不能阻断机器人启动。
        """
        route_factory = self._route_factory
        if route_factory is None:
            from botpy.http import Route
            route_factory = Route

        api = self.client.api
        listed = await api._http.request(route_factory("GET", "/v2/panels?scope=group&limit=50"))
        records = listed.get("records", []) if isinstance(listed, dict) else []
        found = False
        existing_id: str | None = None
        existing_items: Any = None
        for record in records:
            if not isinstance(record, dict):
                continue
            panel = record.get("panel", {}) or {}
            if panel.get("remark") == _GROUP_PANEL_REMARK:
                found = True
                existing_id = str(record.get("panel_id") or panel.get("panel_id") or "") or None
                existing_items = panel.get("items")
                break

        payload = {
            "scope": "group",
            "target_type": "all",
            "panel": {
                "items": list(_GROUP_PANEL_ITEMS),
                "remark": _GROUP_PANEL_REMARK,
            },
        }

        if not found:
            created = await api._http.request(route_factory("POST", "/v2/panels"), json=payload)
            panel_id = created.get("panel_id", "created") if isinstance(created, dict) else "created"
            log.info("QQ 群指令面板已创建", extra={"session_id": str(panel_id)})
            return

        if _panel_signature(existing_items) == _panel_signature(_GROUP_PANEL_ITEMS):
            log.info("QQ 群指令面板已是最新，无需更新", extra={"session_id": existing_id or "unknown"})
            return

        if existing_id is None:
            # 面板存在但拿不到 panel_id，既不能更新也不能删除；此时再 POST 只会建出
            # 重复面板，因此只告警，等人工在管理端处理。
            log.warning(
                "QQ 群指令面板条目已过期，但接口未返回 panel_id，无法自动更新",
                extra={"reason": "missing_panel_id"},
            )
            return

        # 官方 /v2/panels 是否支持整体更新未在 qq-botpy 1.2.1 中封装，这里先尝试
        # PUT 覆盖；若接口不支持（4xx/405 等）再退化成「先删后建」，保证条目最终一致。
        try:
            await api._http.request(
                route_factory("PUT", f"/v2/panels/{existing_id}"), json=payload
            )
            log.info("QQ 群指令面板已更新", extra={"session_id": existing_id})
            return
        except Exception as exc:
            log.warning(
                "QQ 群指令面板更新接口不可用，改为先删除再重建",
                extra={"session_id": existing_id, "reason": str(exc)[:120]},
            )

        try:
            await api._http.request(route_factory("DELETE", f"/v2/panels/{existing_id}"))
        except Exception as exc:
            log.warning(
                "QQ 群指令面板删除失败，仍尝试直接重建",
                extra={"session_id": existing_id, "reason": str(exc)[:120]},
            )
        created = await api._http.request(route_factory("POST", "/v2/panels"), json=payload)
        panel_id = created.get("panel_id", "created") if isinstance(created, dict) else "created"
        log.info("QQ 群指令面板已重建", extra={"session_id": str(panel_id)})

    async def dispatch_raw_message(
        self, normalizer: Any, message: Any, *, source: str
    ) -> None:
        """把官方 SDK 的原始消息对象归一化后交给应用层，并兜住所有异常。

        这里是 Gateway 回调的最外层：``normalize_*`` 遇到缺字段的畸形事件会抛
        ``ValueError``，应用层也可能抛业务异常。任何一条都不能把 Gateway 循环拖垮，
        但也不能悄悄吞掉——所以全部写中文日志，带上会话类型和能拿到的标识。
        """
        try:
            event = normalizer(message)
        except ValueError as exc:
            # 畸形事件（缺会话/用户/事件标识），跳过即可，不必打完整堆栈。
            log.warning(
                "QQ 事件字段缺失，已跳过该条消息",
                extra={
                    "event_id": _safe_attr(message, "event_id", "id"),
                    "reason": f"{source}: {str(exc)[:120]}",
                },
            )
            return
        except Exception:
            log.exception(
                "QQ 事件解析异常，已跳过该条消息",
                extra={
                    "event_id": _safe_attr(message, "event_id", "id"),
                    "reason": source,
                },
            )
            return

        try:
            await self.handle_message(event)
        except asyncio.CancelledError:
            # 关闭流程发出的取消必须原样向上传播，不能当成业务异常吞掉。
            raise
        except Exception:
            # 业务异常同样不能中断 Gateway，但要留完整堆栈方便定位。
            log.exception(
                "QQ 事件处理失败",
                extra={
                    "event_id": event.event_id,
                    "room_id": event.session.session_id,
                    "reason": f"{source}: {event.session.session_type.value}",
                },
            )

    async def handle_message(self, event: PlatformEvent) -> None:
        """把归一化后的平台事件交给应用层，私信会话先记录映射关系。"""
        if event.session.session_type == SessionType.DIRECT and self.store is not None:
            await self.store.save_direct_session(event.user_id, event.session.session_id)
        await self.application.handle_event(event)

    async def send_delivery(self, record: DeliveryRecord) -> None:
        """通过对应的官方接口发送一条已入库的文本消息。"""
        # 官方群聊/单聊接口支持 msg_seq（qq-botpy 1.2.1 中
        # post_group_message/post_c2c_message 均有该形参，见 botpy/api.py:1390、1436）；
        # 频道 post_message 和私信 post_dms 的签名里没有这个参数，传了会 TypeError。
        supports_msg_seq = False
        if record.target_type == SessionType.GROUP:
            method = self.client.api.post_group_message
            kwargs: dict[str, Any] = {
                "group_openid": record.target_id,
                "content": record.text,
                "msg_type": 0,
            }
            supports_msg_seq = True
        elif record.target_type == SessionType.C2C:
            method = self.client.api.post_c2c_message
            kwargs = {
                "openid": record.target_id,
                "content": record.text,
                "msg_type": 0,
            }
            supports_msg_seq = True
        elif record.target_type == SessionType.DIRECT:
            method = self.client.api.post_dms
            kwargs = {
                "guild_id": record.target_id,
                "content": record.text,
            }
        elif record.target_type == SessionType.CHANNEL:
            method = self.client.api.post_message
            kwargs = {
                "channel_id": record.target_id,
                "content": record.text,
            }
        else:
            raise ValueError("未知 QQ 消息目标类型")
        if record.reply_to:
            kwargs["msg_id"] = record.reply_to
        # 定时器消息没有 QQ 原始事件号，不能把内部 timer 编号传给 QQ。
        has_event_anchor = bool(record.event_id) and not record.event_id.startswith("timer:")
        if has_event_anchor:
            kwargs["event_id"] = record.event_id
        # msg_seq 只在被动回复（带 msg_id 或 event_id 锚点）时才有意义：官方要求同一个
        # 锚点回复多条消息必须携带递增的 msg_seq，否则第二条起会被判为重复消息直接丢弃。
        # 主动推送没有锚点，不需要也不应该带。
        if supports_msg_seq and (record.reply_to or has_event_anchor):
            kwargs["msg_seq"] = record.msg_seq
        async with self._send_lock:
            now = asyncio.get_running_loop().time()
            wait_for = self.min_send_interval - (now - self._last_send_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            try:
                await method(**kwargs)
            except Exception as exc:
                if _is_transient(exc):
                    raise TransientSendError(str(exc)[:200]) from exc
                raise
            self._last_send_at = asyncio.get_running_loop().time()


def _is_transient(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _panel_signature(items: Any) -> tuple[tuple[str, str, str, bool], ...]:
    """把面板条目归一化成可比较的签名，用于判断远端面板是否需要更新。

    只比较真正影响展示的字段（类型、指令名、描述、是否仅管理员可见），
    忽略官方返回里附带的 id、排序号等我们不关心的额外字段。
    """
    if not isinstance(items, (list, tuple)):
        return ()
    signature = []
    for item in items:
        if not isinstance(item, dict):
            return ()
        signature.append(
            (
                str(item.get("type", "command")),
                str(item.get("name", "")),
                str(item.get("desc", "")),
                bool(item.get("only_admin", False)),
            )
        )
    return tuple(signature)


def _safe_attr(message: Any, *names: str) -> str:
    """尽最大努力从原始消息里取一个可用于日志的标识，取不到就返回 unknown。"""
    for name in names:
        try:
            value = message.get(name) if isinstance(message, dict) else getattr(message, name, None)
        except Exception:
            continue
        if value:
            return str(value)[:64]
    return "unknown"
