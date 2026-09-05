from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

import aiohttp

from application.contracts import (
    PermanentSendError,
    PlatformEvent,
    SessionType,
    TransientSendError,
)
from infrastructure.db import DeliveryRecord

from .events import normalize_group_message, normalize_private_message


log = logging.getLogger(__name__)

# 连接/限流类错误，退避重试就能好。
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "rate",
    "limit",
    "频繁",
    "too many",
    "connect",
    "closed",
    "reset",
    "unavailable",
    "尚未就绪",
    "retry",
)

__all__ = [
    "NapCatAdapter",
    "NapCatCallError",
    "TransientSendError",
    "PermanentSendError",
]


class NapCatCallError(RuntimeError):
    """NapCat 返回了 status=failed 的 API 结果。"""

    def __init__(self, action: str, retcode: int, message: str):
        super().__init__(f"{action} 调用失败（retcode={retcode}）：{message}")
        self.action = action
        self.retcode = retcode
        self.message = message


class NapCatAdapter:
    """NapCat（OneBot 11 正向 WebSocket）桥接层。

    与官方开放平台适配器的关键差异：
    * 用户标识是真实 QQ 号，群聊和私聊完全一致，不需要任何绑定握手；
    * 可以主动发消息，不存在「被动回复锚点」这类限制；
    * 额外提供群成员查询与群名片改写能力，供应用层管理座位号与出局标记。
    """

    def __init__(
        self,
        ws_url: str,
        access_token: str,
        application: Any,
        *,
        store: Any | None = None,
        reconnect_seconds: float = 2.0,
        max_reconnect_seconds: float = 60.0,
        min_send_interval: float = 0.3,
        call_timeout: float = 20.0,
    ):
        if not ws_url:
            raise ValueError("NapCat WebSocket 地址必须在运行时提供")
        self.ws_url = ws_url
        self.access_token = access_token or ""
        self.application = application
        self.store = store
        self.reconnect_seconds = reconnect_seconds
        self.max_reconnect_seconds = max_reconnect_seconds
        self.min_send_interval = min_send_interval
        self.call_timeout = call_timeout
        self.ready = asyncio.Event()
        self.self_id: str = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._echo = itertools.count(1)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0
        self._stop = asyncio.Event()
        # 群号 → 机器人在该群是否为群主/管理员。连接期内缓存，重连后清空。
        self._admin_cache: dict[str, bool] = {}

    # ---------------- 生命周期 ----------------

    @property
    def is_ready(self) -> bool:
        return self.ready.is_set()

    async def run(self) -> None:
        delay = self.reconnect_seconds
        while not self._stop.is_set():
            try:
                await self._run_once()
                if self._stop.is_set():
                    return
                log.warning("NapCat 连接已断开，准备重连", extra={"reason": "ws_closed"})
                delay = self.reconnect_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "NapCat 连接异常，准备重连",
                    extra={"reason": str(exc)[:160]},
                )
            self._fail_pending("NapCat 连接已断开")
            self.ready.clear()
            self._admin_cache.clear()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                delay = min(self.max_reconnect_seconds, delay * 2)

    async def _run_once(self) -> None:
        headers: dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                self.ws_url, headers=headers, heartbeat=30, max_msg_size=0
            ) as ws:
                self._ws = ws
                log.info("NapCat 已连接", extra={"session_id": self.ws_url})
                # 握手查询必须放后台：应答要靠下面的读循环收，
                # 在循环开始前 await 它会直接死锁。
                self._spawn(self._after_connect())
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        self._on_payload(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        self._on_payload(message.data.decode("utf-8", "ignore"))
                    elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break
        self._ws = None

    async def _after_connect(self) -> None:
        try:
            info = await self.call("get_login_info")
        except Exception as exc:
            log.warning("NapCat 登录信息查询失败", extra={"reason": str(exc)[:160]})
            # 拿不到账号信息也让机器人跑起来：self_id 会在收到第一条事件时补上。
            self.ready.set()
            return
        self.self_id = str((info or {}).get("user_id") or "")
        nickname = str((info or {}).get("nickname") or "")
        self.ready.set()
        log.info(
            "NapCat 网关已就绪",
            extra={"session_id": self.self_id or "unknown", "reason": nickname[:32]},
        )
        if self.store is not None:
            try:
                await self.store.save_gateway_session(
                    shard_id=0, session_id=self.self_id, last_sequence=0
                )
            except Exception:
                log.exception("NapCat 会话信息落库失败")

    async def close(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
        self._fail_pending("NapCat 适配器已关闭")
        for task in list(self._tasks):
            task.cancel()
        self.ready.clear()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fail_pending(self, reason: str) -> None:
        for echo, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(TransientSendError(reason))
            self._pending.pop(echo, None)

    # ---------------- 收：事件解析与分发 ----------------

    def _on_payload(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("NapCat 下发了非 JSON 数据帧", extra={"reason": str(raw)[:120]})
            return
        if not isinstance(payload, dict):
            return
        echo = payload.get("echo")
        if echo is not None and str(echo) in self._pending:
            self._resolve_call(str(echo), payload)
            return
        self_id = payload.get("self_id")
        if self_id and not self.self_id:
            self.self_id = str(self_id)
        post_type = str(payload.get("post_type") or "")
        if post_type == "meta_event":
            if payload.get("meta_event_type") == "lifecycle":
                self.ready.set()
            return
        if post_type != "message":
            # message_sent（机器人自己发出的回响）、notice、request 一律不进游戏状态机。
            return
        message_type = str(payload.get("message_type") or "")
        if message_type == "group":
            self._spawn(self.dispatch_raw_message(normalize_group_message, payload, source="group"))
        elif message_type == "private":
            self._spawn(
                self.dispatch_raw_message(normalize_private_message, payload, source="private")
            )

    def _resolve_call(self, echo: str, payload: dict[str, Any]) -> None:
        future = self._pending.pop(echo, None)
        if future is None or future.done():
            return
        status = str(payload.get("status") or "")
        try:
            retcode = int(payload.get("retcode", 0) or 0)
        except (TypeError, ValueError):
            retcode = -1
        # OneBot：retcode 0 = 成功，1 = 已提交异步处理，其余为失败。
        if status == "failed" or retcode not in (0, 1):
            future.set_exception(
                NapCatCallError(
                    str(payload.get("action") or echo),
                    retcode,
                    str(payload.get("message") or payload.get("wording") or "未知错误"),
                )
            )
            return
        future.set_result(payload.get("data"))

    async def dispatch_raw_message(self, normalizer: Any, payload: Any, *, source: str) -> None:
        """归一化 OneBot 事件并交给应用层，任何异常都不能拖垮读循环。"""
        try:
            if isinstance(payload, dict) and source == "group":
                payload = dict(payload)
                payload["_bot_id"] = self.self_id
            event = normalizer(payload)
        except ValueError as exc:
            log.warning(
                "NapCat 事件字段缺失，已跳过该条消息",
                extra={"reason": f"{source}: {str(exc)[:120]}"},
            )
            return
        except Exception:
            log.exception("NapCat 事件解析异常，已跳过该条消息", extra={"reason": source})
            return

        if self.self_id and event.user_id == self.self_id:
            # 机器人自己的消息不进状态机，避免自触发。
            return

        try:
            await self.handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "NapCat 事件处理失败",
                extra={
                    "event_id": event.event_id,
                    "room_id": event.session.session_id,
                    "reason": f"{source}: {event.session.session_type.value}",
                },
            )

    async def handle_message(self, event: PlatformEvent) -> None:
        await self.application.handle_event(event)

    # ---------------- 发：投递与 API 调用 ----------------

    async def call(self, action: str, **params: Any) -> Any:
        """调用一次 OneBot API 并等待同 echo 的应答。"""
        ws = self._ws
        if ws is None or ws.closed:
            raise TransientSendError("NapCat 连接尚未就绪")
        echo = f"wolf-{next(self._echo)}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await ws.send_str(
                json.dumps(
                    {"action": action, "params": params, "echo": echo}, ensure_ascii=False
                )
            )
            return await asyncio.wait_for(future, timeout=self.call_timeout)
        except asyncio.TimeoutError as exc:
            raise TransientSendError(f"{action} 等待 NapCat 应答超时") from exc
        finally:
            self._pending.pop(echo, None)

    async def send_delivery(self, record: DeliveryRecord) -> None:
        """把一条已入库的文本消息投递出去。"""
        context = {
            "target_type": record.target_type.value,
            "target_id": record.target_id,
            "delivery_id": record.delivery_id[:12],
            "chars": len(record.text),
            "attempts": record.attempts,
        }
        try:
            numeric = int(str(record.target_id).strip())
        except (TypeError, ValueError) as exc:
            # 官方 openid 时代遗留的队列数据，NapCat 下永远发不出去，直接判死。
            log.error("NapCat 投递目标不是 QQ 号，已判死", extra=context)
            raise PermanentSendError(f"目标标识不是 QQ 号：{record.target_id}") from exc

        async with self._send_lock:
            now = asyncio.get_running_loop().time()
            wait_for = self.min_send_interval - (now - self._last_send_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            try:
                if record.target_type == SessionType.GROUP:
                    await self.call("send_group_msg", group_id=numeric, message=record.text)
                elif record.target_type in (SessionType.C2C, SessionType.DIRECT):
                    # OneBot 11 的 send_msg 支持携带 group_id 的群临时会话。
                    # 群内触发的私聊记录会把来源群号放在 room_id；没有群上下文
                    # 的真正 C2C 消息仍使用标准 send_private_msg。
                    if record.room_id:
                        try:
                            source_group = int(str(record.room_id).strip())
                        except (TypeError, ValueError) as exc:
                            raise PermanentSendError(
                                f"临时私聊来源群号不是 QQ 号：{record.room_id}"
                            ) from exc
                        await self.call(
                            "send_msg",
                            message_type="private",
                            user_id=numeric,
                            group_id=source_group,
                            message=record.text,
                        )
                    else:
                        await self.call("send_private_msg", user_id=numeric, message=record.text)
                else:
                    raise PermanentSendError(f"NapCat 不支持的消息目标类型：{record.target_type}")
            except (TransientSendError, PermanentSendError):
                raise
            except Exception as exc:
                if _is_transient(exc):
                    log.warning("NapCat 发送失败（可重试）", extra={**context, "error": str(exc)[:200]})
                    raise TransientSendError(str(exc)[:200]) from exc
                log.error("NapCat 发送失败（判死）", extra={**context, "error": str(exc)[:200]})
                raise PermanentSendError(str(exc)[:200]) from exc
            self._last_send_at = asyncio.get_running_loop().time()
            log.debug("NapCat 发送成功", extra=context)

    # ---------------- 群管理能力（供应用层调用） ----------------

    async def is_group_admin(self, group_id: str) -> bool:
        """机器人在该群是否为群主/管理员——决定能不能改玩家群名片。"""
        key = str(group_id)
        cached = self._admin_cache.get(key)
        if cached is not None:
            return cached
        if not self.self_id:
            try:
                info = await self.call("get_login_info")
                self.self_id = str((info or {}).get("user_id") or "")
            except Exception as exc:
                log.warning("NapCat 登录信息查询失败", extra={"reason": str(exc)[:120]})
                return False
        try:
            member = await self.call(
                "get_group_member_info",
                group_id=int(key),
                user_id=int(self.self_id),
                no_cache=True,
            )
        except Exception as exc:
            log.warning(
                "NapCat 群管理员权限探测失败",
                extra={"room_id": key, "reason": str(exc)[:120]},
            )
            return False
        role = str((member or {}).get("role") or "member")
        result = role in {"owner", "admin"}
        self._admin_cache[key] = result
        log.info(
            "NapCat 群权限探测完成",
            extra={"room_id": key, "reason": f"role={role}"},
        )
        return result

    async def set_group_card(self, group_id: str, user_id: str, card: str) -> bool:
        """改一个群成员的群名片。失败只记日志，绝不影响游戏流程。"""
        try:
            await self.call(
                "set_group_card",
                group_id=int(str(group_id)),
                user_id=int(str(user_id)),
                card=card,
            )
        except Exception as exc:
            log.warning(
                "NapCat 群名片修改失败",
                extra={"room_id": str(group_id), "reason": str(exc)[:120]},
            )
            return False
        return True

    async def get_group_member_names(self, group_id: str) -> dict[str, str]:
        """取全群成员的展示昵称：QQ 号 → 昵称。

        刻意取 nickname 而不是 card：开局后名片会被改成「N号」，
        用名片当昵称会让名单退化成「1号 1号」。
        """
        try:
            members = await self.call("get_group_member_list", group_id=int(str(group_id)))
        except Exception as exc:
            log.warning(
                "NapCat 群成员列表查询失败",
                extra={"room_id": str(group_id), "reason": str(exc)[:120]},
            )
            return {}
        result: dict[str, str] = {}
        for member in members or []:
            if not isinstance(member, dict):
                continue
            user_id = member.get("user_id")
            if user_id in (None, ""):
                continue
            name = str(member.get("nickname") or member.get("card") or "").strip()
            if name:
                result[str(user_id)] = name[:64]
        return result

    async def get_member_name(self, group_id: str, user_id: str) -> str:
        try:
            member = await self.call(
                "get_group_member_info",
                group_id=int(str(group_id)),
                user_id=int(str(user_id)),
                no_cache=False,
            )
        except Exception:
            return ""
        return str((member or {}).get("nickname") or (member or {}).get("card") or "").strip()[:64]


def _is_transient(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in _TRANSIENT_MARKERS)
