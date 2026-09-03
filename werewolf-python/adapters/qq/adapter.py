from __future__ import annotations

import asyncio
import logging
from typing import Any

from application.contracts import (
    NeedsAnchorSendError,
    PermanentSendError,
    PlatformEvent,
    SessionType,
    TransientSendError,
)
from domain.models import qq_short_id
from infrastructure.db import DeliveryRecord

from .events import (
    normalize_c2c_message,
    normalize_channel_message,
    normalize_direct_message,
    normalize_group_message,
)


log = logging.getLogger(__name__)

_TRANSIENT_MARKERS = ("429", "rate", "timeout", "temporar", "503", "502", "reset", "connect")

# 缺回复锚点时的官方拒绝。重试无效，但用户再发一条同会话消息就能救回来，
# 所以不能判死，也不能连打五次把观测页堆成 failed。
_NEEDS_ANCHOR_MARKERS = (
    "无好友关系",
    "主动消息",
    "无权限",
    "权限不足",
    "没有权限",
    "已过期",
    "expired",
    "304023",
    "304024",
)

# 重试再多次也不会变的错误。不含上面那些「等下一条用户消息就能发」的情况。
_PERMANENT_MARKERS = (
    "不允许",
    "被限制",
    "forbidden",
    "unauthorized",
    "not allowed",
    "no permission",
    "permission denied",
    # 官方常见的权限/参数类错误码。
    "40003",
    "11244",
    "11264",
    "22009",
    "304003",
    "304004",
)

_GROUP_PANEL_REMARK = "werewolf-python-group-panel"
# 面板条目按使用顺序：大厅 → 局中 → QQ 身份 → 查询 → 杂项。总数 ≤20。
# 全部 only_admin=False，否则普通玩家在 QQ 指令面板里看不到这些指令。
_GROUP_PANEL_ITEMS = (
    {"type": "command", "name": "/startgame", "desc": "开始一局狼人杀", "only_admin": False},
    {"type": "command", "name": "/join", "desc": "加入当前对局", "only_admin": False},
    {"type": "command", "name": "/go", "desc": "发起人立刻开局", "only_admin": False},
    {"type": "command", "name": "/leave", "desc": "退出当前对局", "only_admin": False},
    {"type": "command", "name": "/cancel", "desc": "取消当前房间", "only_admin": False},
    {"type": "command", "name": "/vote", "desc": "白天投票放逐（需跟目标）", "only_admin": False},
    {"type": "command", "name": "/flee", "desc": "中途弃权退出", "only_admin": False},
    {"type": "command", "name": "/extend", "desc": "延长当前阶段（需跟秒数）", "only_admin": False},
    {"type": "command", "name": "/link", "desc": "开通私聊通道（领关联码）", "only_admin": False},
    {"type": "command", "name": "/bindqq", "desc": "登记真实QQ号（需跟号码）", "only_admin": False},
    {"type": "command", "name": "/whoami", "desc": "查看自己的绑定信息", "only_admin": False},
    {"type": "command", "name": "/status", "desc": "查看玩家和进度", "only_admin": False},
    {"type": "command", "name": "/help", "desc": "查看玩法和指令", "only_admin": False},
    {"type": "command", "name": "/rolelist", "desc": "查看身份列表", "only_admin": False},
    {"type": "command", "name": "/stats", "desc": "查看本局数据", "only_admin": False},
    {"type": "command", "name": "/config", "desc": "查看或修改本群规则", "only_admin": False},
    {"type": "command", "name": "/nextgame", "desc": "预约下一局提醒", "only_admin": False},
    {"type": "command", "name": "/ping", "desc": "检查机器人状态", "only_admin": False},
    {"type": "command", "name": "/startchaos", "desc": "混乱模式开局", "only_admin": False},
    {"type": "command", "name": "/version", "desc": "查看机器人版本", "only_admin": False},
)


class QQDependencyError(RuntimeError):
    """官方 QQ SDK 缺失或无法导入时抛出。"""


# 两个发送异常统一定义在 application.contracts，这里保留同名引用，
# 让老代码里的 `from adapters.qq.adapter import TransientSendError` 继续可用。
__all__ = [
    "QQDependencyError",
    "TransientSendError",
    "PermanentSendError",
    "NeedsAnchorSendError",
    "QQBotAdapter",
]


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
        """把归一化后的平台事件交给应用层，并记住私信会话/回复锚点。"""
        if self.store is not None:
            if event.session.session_type == SessionType.DIRECT:
                await self.store.save_direct_session(event.user_id, event.session.session_id)
            saver = getattr(self.store, "save_user_reply_anchor", None)
            if (
                saver is not None
                and event.session.session_type in {SessionType.C2C, SessionType.DIRECT}
                and event.reply_message_id
            ):
                await saver(
                    event.user_id,
                    event.session.session_type.value,
                    event.session.session_id,
                    event.reply_message_id,
                    event.event_id,
                )
        await self.application.handle_event(event)

    def _route(self, method: str, path: str, **parameters: Any) -> Any:
        factory = self._route_factory
        if factory is None:
            from botpy.http import Route

            factory = Route
        return factory(method, path, **parameters)

    async def send_delivery(self, record: DeliveryRecord) -> None:
        """通过对应的官方接口发送一条已入库的文本消息。"""
        payload: dict[str, Any] = {
            "content": record.text,
            "msg_type": 0,
        }
        if record.reply_to:
            payload["msg_id"] = record.reply_to
        # 定时器消息没有 QQ 原始事件号，不能把内部 timer 编号传给 QQ。
        has_event_anchor = bool(record.event_id) and not str(record.event_id).startswith("timer:")
        if has_event_anchor:
            payload["event_id"] = record.event_id
        # msg_seq 只在被动回复（带 msg_id 或 event_id 锚点）时才有意义：官方要求同一个
        # 锚点回复多条消息必须携带递增的 msg_seq，否则第二条起会被判为重复消息直接丢弃。
        # 主动推送没有锚点，不需要也不应该带；qq-botpy 的 post_c2c_message 默认 msg_seq=1
        # 且会把 msg_id=None 塞进 JSON，主动单聊时反而容易被官方判成非法回复。
        if record.reply_to or has_event_anchor:
            payload["msg_seq"] = record.msg_seq

        async with self._send_lock:
            now = asyncio.get_running_loop().time()
            wait_for = self.min_send_interval - (now - self._last_send_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            # 固定记录「发给哪种会话、用的哪个标识、带没带锚点」。排查
            # 「无好友关系」时最需要的就是这几项：单聊失败但 has_msg_id=False
            # 且标识来自群事件，那就是标识拿错，而不是玩家没加机器人。
            context = {
                "target_type": record.target_type.value,
                "target_id": qq_short_id(record.target_id),
                "delivery_id": record.delivery_id[:12],
                "has_msg_id": bool(record.reply_to),
                "has_event_id": has_event_anchor,
                "msg_seq": payload.get("msg_seq"),
                "chars": len(record.text),
                "attempts": record.attempts,
            }
            try:
                response = await self._dispatch_send(record, payload)
            except Exception as exc:
                # 顺序很重要：先判临时错误。「429 too many requests」里也可能带
                # 「not allowed」之类的字样，误判成永久失败就会白丢一条消息。
                if _is_transient(exc):
                    log.warning(
                        "QQ 发送失败（可重试）", extra={**context, "error": str(exc)[:200]}
                    )
                    raise TransientSendError(str(exc)[:200]) from exc
                if _is_needs_anchor(exc):
                    log.warning(
                        "QQ 发送失败（缺少可用会话/锚点，已挂起等待）",
                        extra={
                            **context,
                            "error": str(exc)[:200],
                            "hint": (
                                "单聊报此错时，先确认 target_id 是单聊作用域 openid；"
                                "群作用域 openid 无论玩家是否已添加机器人都会被拒，"
                                "需要玩家执行 /link 完成关联"
                                if record.target_type == SessionType.C2C
                                else "等待同会话下一条用户消息提供 msg_id 锚点"
                            ),
                        },
                    )
                    raise NeedsAnchorSendError(str(exc)[:200]) from exc
                if _is_permanent(exc):
                    log.error(
                        "QQ 发送失败（永久错误，已判死）",
                        extra={**context, "error": str(exc)[:200]},
                    )
                    raise PermanentSendError(str(exc)[:200]) from exc
                log.exception("QQ 发送失败（未归类）", extra=context)
                raise
            log.debug("QQ 发送成功", extra={**context, "response": _brief_response(response)})
            self._last_send_at = asyncio.get_running_loop().time()

    async def _dispatch_send(self, record: DeliveryRecord, payload: dict[str, Any]) -> Any:
        """按会话类型调用官方发送接口。群聊/单聊走原始 HTTP，以便省略空 msg_id。"""
        if record.target_type == SessionType.GROUP:
            route = self._route(
                "POST", "/v2/groups/{group_openid}/messages", group_openid=record.target_id
            )
            return await self.client.api._http.request(route, json=payload)
        if record.target_type == SessionType.C2C:
            route = self._route("POST", "/v2/users/{openid}/messages", openid=record.target_id)
            return await self.client.api._http.request(route, json=payload)
        if record.target_type == SessionType.DIRECT:
            kwargs = {"guild_id": record.target_id, "content": record.text}
            if record.reply_to:
                kwargs["msg_id"] = record.reply_to
            if payload.get("event_id"):
                kwargs["event_id"] = payload["event_id"]
            return await self.client.api.post_dms(**kwargs)
        if record.target_type == SessionType.CHANNEL:
            kwargs = {"channel_id": record.target_id, "content": record.text}
            if record.reply_to:
                kwargs["msg_id"] = record.reply_to
            if payload.get("event_id"):
                kwargs["event_id"] = payload["event_id"]
            return await self.client.api.post_message(**kwargs)
        raise ValueError("未知 QQ 消息目标类型")


def _brief_response(response: Any) -> str:
    """把接口返回压成一行，只留排查用的关键字段，避免刷屏。"""
    if response is None:
        return ""
    if isinstance(response, dict):
        keep = {k: response[k] for k in ("id", "timestamp", "code", "message") if k in response}
        return str(keep or list(response)[:6])[:200]
    return str(response)[:200]


def _is_transient(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _is_needs_anchor(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker.casefold() in text for marker in _NEEDS_ANCHOR_MARKERS)


def _is_permanent(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in _PERMANENT_MARKERS)


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
