from __future__ import annotations

import asyncio
import logging
import random
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable

from domain.achievements import Achievement, achievement_description, achievement_name
from domain.engine import GameRoomEngine, GameRuleError
from domain.models import (
    DAY_ACTIONS,
    DomainEvent,
    GameMode,
    GamePhase,
    GameRoom,
    ROLE_ACTIONS,
    ROLE_METADATA,
    Role,
    Team,
    is_opaque_display_name,
)
from domain.locale import (
    CATALOG,
    DEFAULT_LANGUAGE,
    format_validation_report,
    get_locale_string,
    resolve_language,
    validate_language_files,
)
from domain.roleinfo import get_about, role_display_name, role_list_pages
from domain.rules import ruleset_v1, try_balance
from infrastructure.db import PostgreSQLStore
from infrastructure.observability import TRACE

from .commands import Command, parse_command
from .contracts import (
    OutboundMessage,
    PermanentSendError,
    PlatformEvent,
    PlatformSession,
    SessionType,
)


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PrivateTarget:
    """一条私聊消息该发到哪。

    NapCat 下私聊标识就是玩家的 QQ 号，跟群里那一份完全一致，
    不存在官方开放平台那种「群作用域 openid 发不了单聊」的问题，
    所以这里既不需要锚点，也不需要 waiting 状态。
    `origin` 只进日志，方便排查目标是怎么定下来的。
    """

    session: PlatformSession
    reply_to: str | None
    event_id: str | None
    origin: str


HELP_TEXT = (
    "【狼人杀指令】\n"
    "— 群里发送 —\n"
    "/startgame：创建房间；本群已有房间且人数已够时，发起人再发一次就直接开局\n"
    "/join：加入当前对局（也可以发「加入」「参加」）\n"
    "/go：本局发起人立刻开局，人数达到下限即可，不需要管理员\n"
    "/leave：入场阶段退出　/cancel：发起人取消本局\n"
    "/status：查看当前进度、还在等谁　/extend 秒数：延长当前阶段\n"
    "/vote 目标：白天投票（官方规则投票后不可改票）　/弃票 或 /abstain：放弃这一票\n"
    "/flee：中途弃权（视规则可能直接判定死亡）\n"
    "— 私聊机器人发送 —\n"
    "夜晚：狼人、查验、守护、访问、转化、模仿、偶像、恋人、连环杀、猎杀教徒、"
    "化学、冻结、纵火、引燃、盗取、挖掘、跳过\n"
    "白天：侦查、撒银、催眠、开枪、揭示市长、和平、捣乱\n"
    "指令后面跟座位号或显示名指定目标；只发指令不带目标时，机器人会分步追问，"
    "用「确认」「重选」「取消」回应即可。\n"
    "/身份 查看自己的身份，/统计 /结算 查看数据与结果。\n"
    "— 其它 —\n"
    "/rolelist 身份列表　/grouplist 群列表　/nextgame 预约下一局　/config 查看本群规则\n"
    "/ping /version /changelog /help"
)

_GROUP_RULE_FIELDS = {
    "mode", "min_players", "max_players", "join_seconds", "night_seconds",
    "day_seconds", "vote_seconds", "random_lynch", "secret_lynch",
    "secret_lynch_show_votes", "secret_lynch_show_voters", "thief_full",
    "allow_arsonist", "burning_overkill", "show_roles_on_death", "show_roles_end",
    "allow_flee", "allow_extend", "max_extend", "random_mode",
    "show_ids", "shuffle_player_list", "allow_nsfw",
    "disabled_roles", "required_roles",
}

# Database/GroupConfig.cs:96-113 —— ConfigGroup 顺序与 hardcodedConfigOptions 的分组。
_CONFIG_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Timers 计时", ("max_extend", "day_seconds", "vote_seconds", "night_seconds", "join_seconds")),
    ("RoleConfig 角色", ("allow_arsonist", "disabled_roles", "required_roles")),
    ("Mechanics 机制", (
        "secret_lynch", "secret_lynch_show_votes", "secret_lynch_show_voters",
        "random_lynch", "random_mode", "show_roles_on_death", "show_roles_end", "thief_full",
    )),
    ("GroupSettings 群设置", (
        "mode", "min_players", "max_players", "allow_extend", "allow_flee",
        "show_ids", "allow_nsfw", "shuffle_player_list", "burning_overkill",
    )),
)

_NIGHT_ACTION_TEXT = {
    "copy": "模仿", "idol": "偶像", "cupid": "恋人", "hunt_cult": "猎杀教徒",
    "hunter_kill": "猎杀", "serial_kill": "连环杀", "chemistry": "化学", "freeze": "冻结",
    "douse": "纵火", "thief": "盗取", "grave": "挖掘",
}
_TARGETED_NIGHT_COMMANDS = frozenset({
    "wolf", "seer", "guard", "visit", "convert", "copy", "idol", "cupid",
    "serial_kill", "hunt_cult", "hunter_kill", "chemistry", "freeze", "douse", "thief", "grave",
})
_TARGETED_DAY_COMMANDS = frozenset({"detect", "shoot"})
_CONFIRM_ONLY_DAY_COMMANDS = frozenset({"silver", "sandman", "mayor", "pacifist", "trouble"})

# 用户需求第四条：出局玩家本局内不再参与游戏。
# 这几条是纯查询/说明，死人照样能用；其余一律拦掉。
_DEAD_ALLOWED_COMMANDS = frozenset({
    "help", "status", "identity", "result", "stats", "ping", "config", "about",
    "rolelist", "version", "changelog", "achv", "myidles",
})

# Attributes/CommandAttribute.cs —— 官方权限分档。
# GroupAdminOnly：Commands/AdminCommands.cs 里带 `GroupAdminOnly = true` 的命令。
_GROUP_ADMIN_COMMANDS = frozenset({"smite", "getidles", "setlink", "remlink"})
# GlobalAdminOnly / DevOnly / LangAdminOnly：Commands/DevCommands.cs 与 AdminCommands.cs。
_DEV_COMMANDS = frozenset({
    "killgame", "skipvote", "maintenance", "getroles", "playtime", "whois", "user",
    "getban", "getbans", "permban", "remban", "notifyban", "notifyspam",
    "preferred", "bangroup", "leavegroup", "resetlink", "addach", "remach",
    "validatelangs", "broadcast", "winchart", "usage", "checkgroups", "clearcount",
    "moveachv", "ohaider", "test", "getcommands", "reloadenglish", "fi",
})
_ADMIN_COMMANDS = _GROUP_ADMIN_COMMANDS | _DEV_COMMANDS

# DevCommands.cs:848-980 —— 官方永封写 SqlDateTime.MaxValue；
# AdminCommands.cs:150 以 365 天为界区分永封与临时封。
_PERMANENT_BAN_EXPIRES = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

# Program.cs:35 —— 维护模式开关落库后的键名。
_MAINT_FLAG = "maintenance"

# Handlers/UpdateHandler.cs:42-60 + Models/SpamDetector.cs —— 官方给每个用户留一份
# 一分钟内的消息记录（SpamDetection 每 2 秒清理超过 1 分钟的条目），
# `/getcommands` 读它、`/clearcount` 清空它。
_MESSAGE_LOG_WINDOW = timedelta(minutes=1)
# Program.cs:205-220 —— MPS 取最近 10 秒的平均值。
_MPS_WINDOW = timedelta(seconds=10)

# DevCommands.cs:576-581 —— `/usage` 采样 10 次，每次间隔 500ms。
_USAGE_SAMPLES = 10
_USAGE_INTERVAL = 0.5


class GameApplication:
    """编排层：把平台事件、领域聚合与持久化出站消息串起来。"""

    def __init__(
        self,
        store: PostgreSQLStore,
        *,
        rules=None,
        engine: GameRoomEngine | None = None,
        message_max_chars: int = 1500,
        max_concurrent_events: int = 100,
        admin_user_ids: Iterable[str] = (),
        dev_user_ids: Iterable[str] = (),
    ):
        self.store = store
        self.rules = rules or ruleset_v1()
        self.chaos_rules = replace(self.rules, name="official-Chaos", mode=GameMode.CHAOS)
        self.admin_user_ids = {str(item) for item in admin_user_ids if str(item).strip()}
        # Attributes/CommandAttribute.cs —— 官方 GlobalAdminOnly / DevOnly / LangAdminOnly
        # 在 QQ 版合并成一档；开发者同时享有群管理员权限（官方 Helpers.cs:186 同理）。
        self.dev_user_ids = {str(item) for item in dev_user_ids if str(item).strip()}
        self.engine = engine or GameRoomEngine()
        self.message_max_chars = max(200, message_max_chars)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._event_limit = asyncio.Semaphore(max(1, max_concurrent_events))
        # Program.cs:251-295 GetFullInfo —— `/fi`、`/runinfo` 用到的进程级计数器。
        self.start_time = datetime.now(timezone.utc)
        self.messages_processed = 0
        self.commands_received = 0
        self.messages_sent = 0
        self.max_games = 0
        self.max_games_time: datetime | None = None
        self._rx_window: list[datetime] = []
        self._tx_window: list[datetime] = []
        # UpdateHandler.UserMessages（Handlers/UpdateHandler.cs:28）。
        self._user_messages: dict[str, list[tuple[datetime, str]]] = {}
        # NapCat 适配器，由 main.py 通过 bind_platform 注入。
        # 只用来做「读群成员昵称 / 改群名片 / 探测机器人是不是群管」这类平台能力，
        # 拿不到（单测、离线跑）时全部静默降级，不影响任何游戏流程。
        self.platform = None
        # 群号 → 本局是否允许改名片。开局时探测一次，避免每次改名都问一遍。
        self._card_permission: dict[str, bool] = {}
        # 群号 → {玩家: 上次写进去的群名片}，只对有变化的人发请求。
        self._card_state: dict[str, dict[str, str]] = {}

    def bind_platform(self, platform) -> None:
        """注入平台适配器（NapCat）。"""
        self.platform = platform

    # ---------------- 平台能力：群名片与昵称 ----------------

    async def _can_manage_cards(self, group_id: str) -> bool:
        """机器人在该群是不是群主/管理员——不是就完全不碰群名片。"""
        platform = self.platform
        if platform is None or not hasattr(platform, "is_group_admin"):
            return False
        key = str(group_id)
        cached = self._card_permission.get(key)
        if cached is not None:
            return cached
        try:
            allowed = bool(await platform.is_group_admin(key))
        except Exception:
            log.exception("探测机器人群权限失败", extra={"room_id": key})
            allowed = False
        self._card_permission[key] = allowed
        return allowed

    async def _set_card(self, group_id: str, user_id: str, card: str) -> None:
        platform = self.platform
        if platform is None or not hasattr(platform, "set_group_card"):
            return
        try:
            await platform.set_group_card(str(group_id), str(user_id), card)
        except Exception:
            log.exception(
                "修改群名片失败", extra={"room_id": str(group_id), "user_id": str(user_id)}
            )

    async def _sync_cards(self, room: GameRoom) -> None:
        """把群名片同步成当前局内状态。

        用户需求第三、四条：机器人是群主/管理员时，群内身份直接以号码为准，
        出局的人名片改成「N号（已出局）」，一眼就能看出谁还在局内。
        对局结束后清空名片，把展示权还给玩家自己。

        只对「和上次不一样」的人发请求，所以每批事件都可以无脑调用一次；
        改名片纯属锦上添花，任何一步失败都只记日志，绝不打断游戏流程。
        """
        group_id = str(room.session_id)
        # 大厅阶段还没分配座位，不碰任何人的名片。
        if room.phase is GamePhase.LOBBY:
            return
        if not await self._can_manage_cards(group_id):
            return

        finished = room.phase in {GamePhase.FINISHED, GamePhase.CANCELLED}
        applied = self._card_state.setdefault(group_id, {})
        for player in room.players:
            if finished:
                card = ""
            elif player.alive:
                card = f"{player.seat}号"
            else:
                card = f"{player.seat}号（已出局）"
            if applied.get(player.user_id) == card:
                continue
            await self._set_card(group_id, player.user_id, card)
            applied[player.user_id] = card
        if finished:
            # 本局记录清掉，下一局重新按新座位号铺一遍。
            self._card_state.pop(group_id, None)
            self._card_permission.pop(group_id, None)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def handle_event(self, event: PlatformEvent) -> list[OutboundMessage]:
        if event.is_bot:
            return []
        self._record_message(event)
        claimed, cached = await self.store.begin_event(event.event_id, event.session.key)
        if not claimed:
            TRACE.record(
                self._room_id(event),
                "指令分发",
                "同一事件号已经处理过，直接返回上次结果（幂等）",
                事件号=event.event_id,
            )
            return cached or []

        lock = await self._lock_for(self._lock_key(event))
        async with self._event_limit, lock:
            try:
                await self._remember_user(event)
                room, messages, action_name = await self._dispatch(event)
                messages = self._split_messages(messages)
                await self.store.commit_result(
                    room=room,
                    event_id=event.event_id,
                    messages=messages,
                    action_key=event.event_id,
                    action_data={"name": action_name, "user_id": event.user_id},
                )
                TRACE.record(
                    self._room_id(event),
                    "指令分发",
                    f"「{action_name}」处理完成，已入队 {len(messages)} 条消息",
                    阶段=room.phase.value if room else "无房间",
                )
                return messages
            except GameRuleError as exc:
                TRACE.record(
                    self._room_id(event),
                    "指令分发",
                    f"操作被规则拒绝：{exc}",
                    用户=event.user_id,
                )
                messages = self._split_messages(
                    [self._reply(event, f"操作未执行：{exc}", room_id=self._room_id(event))]
                )
                await self.store.commit_result(
                    room=None,
                    event_id=event.event_id,
                    messages=messages,
                    action_key=event.event_id,
                    action_data={"name": "rejected", "user_id": event.user_id},
                )
                return messages
            except Exception as exc:
                log.exception(
                    "事件处理失败",
                    extra={
                        "event_id": event.event_id,
                        "session_id": event.session.key,
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                )
                TRACE.record(
                    self._room_id(event),
                    "指令分发",
                    f"处理异常：{type(exc).__name__}: {exc}",
                    事件号=event.event_id,
                )
                messages = self._split_messages(
                    [self._reply(event, "服务暂时无法处理该操作，请稍后再试。", room_id=self._room_id(event))]
                )
                await self.store.commit_result(
                    room=None,
                    event_id=event.event_id,
                    messages=messages,
                    action_key=event.event_id,
                    action_data={"name": "failed", "user_id": event.user_id},
                )
                return messages

    async def _remember_user(self, event: PlatformEvent) -> None:
        """每条消息都把「QQ 号 → 昵称」归档进 players 表。

        对应官方 Helpers.cs:60-78 的 UpdatePlayerName。NapCat 下每条群消息都带
        sender.nickname，所以这一步基本每次都能拿到真名；名单、私聊提示和后台
        因此都能显示「1号 沉潜」而不是光秃秃的座位号。
        归档失败只记日志，绝不能因此挡住玩家的正常指令。
        """
        setter = getattr(self.store, "touch_player", None)
        if setter is None:
            return
        name = (event.display_name or "").strip()
        if is_opaque_display_name(name):
            name = ""
        try:
            await setter(event.user_id, name or None)
        except Exception:
            log.exception("归档玩家昵称失败", extra={"user_id": event.user_id})

    async def _dispatch(self, event: PlatformEvent) -> tuple[GameRoom | None, list[OutboundMessage], str]:
        command = parse_command(event.text)
        TRACE.record(
            self._room_id(event),
            "指令分发",
            f"收到指令「{command.name}」，参数={command.argument or '无'}",
            会话=event.session.key,
            用户=event.user_id,
            原文=(event.text or "")[:80],
            有回复锚点=bool(event.reply_message_id),
        )
        # Handlers/UpdateHandler.cs:330-334 —— 全局封禁/刷屏封禁的用户发出的命令
        # （以 / 或 ! 开头）一律静默丢弃，不给任何回复。
        if command.name != "unknown" and await self._is_banned(event.user_id):
            TRACE.record(
                self._room_id(event), "指令分发", "发送者处于封禁中，按官方规则静默丢弃",
                用户=event.user_id,
            )
            return None, [], "banned"
        if event.session.session_type in {SessionType.C2C, SessionType.DIRECT}:
            room = await self._room_for_event(event)
            if room is not None and command.name in {"cancel", "confirm", "reselect", "unknown"}:
                continued = await self._continue_pending_action(room, event, command)
                if continued is not None:
                    return room, continued, "step_action"
        if command.name in {"help", "start"}:
            return await self._room_for_event(event), [self._reply(event, HELP_TEXT)], "help"
        if command.name == "unknown":
            return await self._room_for_event(event), [self._reply(event, "未识别命令。\n" + HELP_TEXT)], "unknown"

        if command.name == "about":
            return await self._about_command(event, command.argument or "")

        if command.name == "nextgame":
            return await self._next_game(event)

        if command.name in {"create", "create_chaos"}:
            if event.session.session_type != SessionType.GROUP:
                raise GameRuleError("请在群里发送 /startgame")
            # Program.cs:35 + Program.cs:393 —— 维护模式下官方不再放行新对局。
            if await self._maintenance_mode():
                raise GameRuleError("机器人正在维护中，暂时无法开始新的游戏。")
            # Commands/Helpers.cs:92-96 —— CreatedBy == "BAN" 的群不允许再开局。
            group = await self._group_record(event.session.session_id)
            if group and group.get("created_by") == "BAN":
                raise GameRuleError("本群已被封禁，无法开始游戏。")
            existing = await self.store.get_room(event.session.session_id)
            if self._is_active(existing):
                # Commands/Helpers.cs:118-142 —— 本群已有对局时官方不报错：
                # 玩家若在别的群的对局中回 AlreadyInGame，否则只重新展示加入入口。
                await self._ensure_user_can_join_active_room(event.user_id, event.session.session_id)
                # 房主（本局发起人）再次发送 /startgame 等同于 /go：人数够就立刻开局。
                # 用户需求：发起人可以直接用 /startgame 开始游戏，不需要群管理员权限。
                if existing.phase == GamePhase.LOBBY and self._may_start(existing, event):
                    await self._prepare_achievement_state(existing)
                    events = self.engine.force_start(
                        existing, event.user_id, admin=self._is_admin(event)
                    )
                    return (
                        existing,
                        await self._events_to_messages(events, existing, event),
                        "force_start",
                    )
                return existing, self._show_join_prompt(existing, event), "show_join"
            if existing is not None:
                await self.store.delete_room(event.session.session_id)
            await self._ensure_user_can_join_active_room(event.user_id, event.session.session_id)
            selected_rules = self.chaos_rules if command.name == "create_chaos" else self.rules
            getter = getattr(self.store, "get_group_rule_config", None)
            if getter is not None:
                values = await getter(event.session.session_id)
                if values:
                    try:
                        selected_rules = self._apply_group_config(selected_rules, values)
                    except (TypeError, ValueError) as exc:
                        raise GameRuleError(f"本群规则配置无效：{exc}") from exc
            # 建房的人就是本局发起人（房主），后续 /go、/startgame、/cancel 认这个身份。
            room = self.engine.create_room(
                event.session.session_id, selected_rules, host_user_id=event.user_id
            )
            events = [
                DomainEvent(
                    "room_created",
                    # NapCat 下 sender.nickname 基本都有；极少数拿不到时
                    # 退化成「你」，绝不把任何内部标识打进正文。
                    f"房间已创建，发起人是{self._event_public_name(event)}，正在等待玩家加入。",
                )
            ]
            events.append(
                DomainEvent(
                    "lobby_help",
                    f"入场阶段剩余 {room.rules.join_seconds} 秒，请发送 /join 参加，"
                    f"至少需要 {room.rules.min_players} 人。"
                    f"人数够了以后，发起人发送 /startgame 或 /go 就能立刻开局。",
                )
            )
            messages = await self._events_to_messages(events, room, event)
            messages.extend(await self._notify_wait_list(event))
            return room, messages, command.name

        if command.name == "stop_waiting":
            return await self._stop_waiting(event, command.argument)

        if command.name in {"ping", "chatid", "changelog", "runinfo", "rolelist", "grouplist", "version", "setlang", "getlang", "myidles", "achv"}:
            return await self._official_info(event, command.name, command.argument)

        if command.name in _ADMIN_COMMANDS:
            return await self._admin_command(event, command)

        if command.name == "config":
            self._require_group(event)
            values = await self.store.get_group_rule_config(event.session.session_id)
            if command.argument:
                if not self._is_admin(event):
                    raise GameRuleError("只有管理员可以修改本群规则")
                values = self._update_group_config(values, command.argument)
                try:
                    self._apply_group_config(self.rules, values)
                except (TypeError, ValueError) as exc:
                    raise GameRuleError(f"配置不符合官方规则：{exc}") from exc
                await self.store.save_group_rule_config(event.session.session_id, values, event.user_id)
            return await self._room_for_event(event), [self._reply(event, self._config_text(values), room_id=event.session.session_id)], "config"

        if command.name == "stats":
            # GeneralCommands.cs:505-565 GetStats —— 官方在群里给全局/本群/本人三组统计，私聊只给全局与本人。
            return await self._stats_command(event)

        if command.name in {"identity", "result"}:
            room = await self._room_for_query(event)
            if room is None:
                raise GameRuleError("当前会话没有可查询的房间")
            if command.name == "identity":
                self._require_c2c(event)
                player = next((p for p in room.players if p.user_id == event.user_id), None)
                if player is None or player.role is None:
                    raise GameRuleError("你不在当前房间或身份尚未发放")
                return room, [self._reply(event, f"你的身份是【{role_display_name(player.role)}】。", room_id=room.session_id)], "identity"
            self._require_group(event)
            return room, [self._reply(event, self._result_text(room), room_id=room.session_id)], "result"

        room = await self._room_for_event(event)
        if room is None:
            raise GameRuleError("当前会话没有活动房间，请先发送 /startgame")

        # 用户需求第四条：出局玩家本局内不再参与游戏，
        # 所有游戏操作类指令一律拦下，只保留查询类（前面已经返回过了）。
        # /flee 与 /vote 需要进入领域层，分别返回官方 DeadFlee/不能投票 文案；
        # 其它游戏操作仍由统一闸门拦截，避免遗漏存活校验。
        if command.name not in {"flee", "vote", "abstain"}:
            self._reject_if_dead(room, event, command)

        if command.name == "join":
            self._require_group(event)
            await self._ensure_user_can_join_active_room(event.user_id, room.session_id)
            events = self.engine.join(room, event.user_id, event.display_name)
        elif command.name == "leave":
            self._require_group(event)
            events = self.engine.leave(room, event.user_id)
        elif command.name == "cancel":
            self._require_group(event)
            events = self.engine.cancel(room, event.user_id, admin=self._is_admin(event))
        elif command.name in {"start_game", "force_start"}:
            # `/go`、`/startgame`（房间已存在时）—— 本局发起人（房主）自己就能开局，
            # 不需要群管理员权限；管理员只是额外的兜底通道。
            self._require_group(event)
            await self._prepare_achievement_state(room)
            events = self.engine.force_start(room, event.user_id, admin=self._is_admin(event))
        elif command.name == "flee":
            self._require_group(event)
            events = self.engine.flee(room, event.user_id)
        elif command.name == "extend":
            self._require_group(event)
            if command.argument is None:
                raise GameRuleError("用法：/extend 秒数，例如 /extend 30")
            # GameCommands.cs:169 —— int.TryParse 失败时回落到默认 30 秒。
            try:
                seconds = int(command.argument)
            except ValueError:
                seconds = 30
            events = self.engine.extend_time(room, event.user_id, seconds, admin=self._is_admin(event))
        elif command.name == "status":
            self._require_group(event)
            return room, [self._reply(event, self._status_text(room), room_id=room.session_id)], "status"
        elif command.name == "vote":
            self._require_group(event)
            events = self.engine.submit_vote(room, event.user_id, command.argument)
        elif command.name == "abstain":
            self._require_group(event)
            events = self.engine.submit_vote(room, event.user_id, "弃票")
        elif command.name in {
            "wolf", "seer", "guard", "visit", "convert", "copy", "idol", "cupid",
            "serial_kill", "hunt_cult", "hunter_kill", "chemistry", "freeze",
            "douse", "ignite", "thief", "grave",
        }:
            self._require_c2c(event)
            if command.name == "ignite":
                events = self.engine.submit_night_action(room, event.user_id, "纵火", "引燃")
                return room, await self._events_to_messages(events, room, event), command.name
            if command.argument is None:
                return room, self._begin_pending_action(room, event, command.name), "step_action"
            events = self.engine.submit_night_action(
                room, event.user_id,
                _NIGHT_ACTION_TEXT.get(command.name, command.name),
                command.argument,
            )
        elif command.name in {"detect", "silver", "sandman", "shoot", "mayor", "pacifist", "trouble"}:
            self._require_c2c(event)
            if command.name in _TARGETED_DAY_COMMANDS | _CONFIRM_ONLY_DAY_COMMANDS and command.argument is None:
                return room, self._begin_pending_action(room, event, command.name), "step_action"
            action = {
                "detect": "侦查", "silver": "撒银", "sandman": "催眠",
                "mayor": "市长", "pacifist": "和平", "trouble": "捣乱",
            }.get(command.name, command.name)
            events = self.engine.submit_day_action(room, event.user_id, action, command.argument)
        elif command.name == "change_vote":
            raise GameRuleError("官方规则投票后不可改票")
        elif command.name == "skip":
            if room.phase == GamePhase.VOTE:
                events = self.engine.submit_vote(room, event.user_id, "弃票")
            else:
                self._require_c2c(event)
                player = next((p for p in room.players if p.user_id == event.user_id), None)
                action = self._skip_action(player)
                if action is None:
                    raise GameRuleError("当前身份没有可跳过的夜间行动")
                events = self.engine.submit_night_action(room, event.user_id, action, "跳过")
        else:
            raise GameRuleError("当前阶段不支持该命令")
        return room, await self._events_to_messages(events, room, event), command.name

    # ------------------------------------------------------------------
    # 管理 / 开发命令（Commands/AdminCommands.cs、Commands/DevCommands.cs）
    # ------------------------------------------------------------------

    async def _is_banned(self, user_id: str) -> bool:
        """Handlers/UpdateHandler.cs:330-334 —— BanList 只装 `Expires > UtcNow` 的记录。"""
        getter = getattr(self.store, "get_global_ban", None)
        if getter is None:
            return False
        try:
            ban = await getter(user_id)
        except Exception:
            log.exception("读取全局封禁记录失败", extra={"user_id": user_id})
            return False
        if not ban:
            return False
        expires = ban.get("expires")
        return expires is None or expires > datetime.now(timezone.utc)

    async def _maintenance_mode(self) -> bool:
        getter = getattr(self.store, "get_bot_flag", None)
        if getter is None:
            return False
        try:
            return bool(await getter(_MAINT_FLAG))
        except Exception:
            log.exception("读取维护模式开关失败")
            return False

    async def _group_record(self, group_id: str) -> dict | None:
        getter = getattr(self.store, "get_group", None)
        if getter is None:
            return None
        return await getter(group_id)

    def _record_message(self, event: PlatformEvent) -> None:
        """Handlers/UpdateHandler.cs:42-60 AddCount + Bot.MessagesProcessed / CommandsReceived。"""
        now = datetime.now(timezone.utc)
        self.messages_processed += 1
        text = (event.text or "").strip()
        if text[:1] in {"/", "!", "！"}:
            self.commands_received += 1
        log_entries = self._user_messages.setdefault(event.user_id, [])
        log_entries.append((event.occurred_at or now, text))
        # UpdateHandler.SpamDetection()：只保留最近一分钟的记录。
        cutoff = now - _MESSAGE_LOG_WINDOW
        log_entries[:] = [item for item in log_entries if item[0] >= cutoff]
        if not log_entries:
            self._user_messages.pop(event.user_id, None)
        self._rx_window.append(now)
        self._trim_window(self._rx_window, now)

    @staticmethod
    def _trim_window(window: list[datetime], now: datetime) -> None:
        cutoff = now - _MPS_WINDOW
        window[:] = [item for item in window if item >= cutoff]

    async def _run_snapshot(self) -> tuple[int, int]:
        """Program.cs:255-267 —— 当前对局数 / 玩家数，并顺带刷新 MaxGames。"""
        lister = getattr(self.store, "list_active_rooms", None)
        rooms = await lister() if lister is not None else []
        games = len(rooms)
        players = sum(len(room.players) for room in rooms)
        if games > self.max_games:
            self.max_games = games
            self.max_games_time = datetime.now(timezone.utc)
        return games, players

    @staticmethod
    def _format_uptime(delta: timedelta) -> str:
        """C# `TimeSpan.ToString()` 默认格式：`[d.]hh:mm:ss.fffffff`。"""
        total = delta.total_seconds()
        days, rest = divmod(int(total), 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        fraction = int(round((total - int(total)) * 10_000_000))
        base = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if fraction:
            base = f"{base}.{fraction:07d}"
        return f"{days}.{base}" if days else base

    @staticmethod
    def _format_elapsed(delta: timedelta) -> str:
        """AdminCommands.cs:673 的 `mm\\:ss\\.ff`。"""
        total = max(0.0, delta.total_seconds())
        minutes, seconds = divmod(total, 60)
        return f"{int(minutes):02d}:{int(seconds):02d}.{int((seconds % 1) * 100):02d}"

    def _full_info(self, games: int, players: int) -> str:
        uptime = datetime.now(timezone.utc) - self.start_time
        now = datetime.now(timezone.utc)
        self._trim_window(self._rx_window, now)
        self._trim_window(self._tx_window, now)
        mps_in = len(self._rx_window) / _MPS_WINDOW.total_seconds()
        mps_out = len(self._tx_window) / _MPS_WINDOW.total_seconds()
        max_time = (self.max_games_time or self.start_time).strftime("%H:%M:%S")
        # QQ 版没有官方 Control/Node 拆分，节点数恒为 1（本进程自身）。
        return (
            f"运行时长　： {self._format_uptime(uptime)}\n"
            f"节点数　　： 1\n"
            f"在场玩家　： {players}\n"
            f"进行中对局： {games}\n"
            f"收到消息　： {self.messages_processed}\n"
            f"收到指令　： {self.commands_received}\n"
            f"发出消息　： {self.messages_sent}\n"
            f"每秒收　　： {mps_in:g}\n"
            f"每秒发　　： {mps_out:g}\n"
            f"同时最多对局：{self.max_games} 局，出现于 {max_time}"
        )

    def _plain(
        self, event: PlatformEvent, session: PlatformSession, text: str, room_id: str | None = None
    ) -> OutboundMessage:
        """同一条事件的附加回复。

        以前这里会故意丢掉 reply_to / event_id，让第二条起变成主动推送，结果是
        QQ 侧经常直接拦截，用户只看到第一条。现在保留锚点，靠 deliveries.msg_seq
        递增来满足官方「同一 msg_id 回复多条必须换 msg_seq」的要求。
        """
        return OutboundMessage(
            target=session,
            text=text,
            room_id=room_id,
            reply_to=event.reply_message_id if session.key == event.session.key else None,
            source_event_id=event.event_id,
            event_id=event.event_id if session.key == event.session.key else None,
        )

    @staticmethod
    def _single_reply(messages: list[OutboundMessage]) -> list[OutboundMessage]:
        """保留全部被动回复锚点，只给同会话的多条消息编号。

        官方 QQ 接口允许对同一条 msg_id 回复多次，条件是每次带不同的 msg_seq。
        旧实现把第二条起降级成主动推送，等于把「后台已经执行」的结果扔掉，
        正是玩家看不到 /go 反馈的直接原因。
        """
        counters: dict[str, int] = {}
        result: list[OutboundMessage] = []
        for message in messages:
            index = counters.get(message.target.key, 0)
            counters[message.target.key] = index + 1
            result.append(replace(message, sequence=index))
        return result

    @staticmethod
    def _admin_targets(room: GameRoom | None, argument: str) -> list[str]:
        """官方靠 reply / Mention 实体 / 纯数字 ID 指定目标（AdminCommands.cs:26-64）。

        QQ 没有 Mention 实体，这里支持座位号、显示名与用户 ID；解析不到的原样
        当作用户 ID，官方 `SmitePlayer` 对不在场的 ID 同样静默无操作。
        """
        targets: list[str] = []
        for token in argument.replace(",", " ").replace("，", " ").split():
            value = token.lstrip("@").strip()
            if not value:
                continue
            player = None
            if room is not None:
                player = next(
                    (
                        item for item in room.players
                        if value == item.user_id
                        or value.casefold() == item.display_name.casefold()
                        or value.casefold() == item.public_name.casefold()
                        or value.casefold() == item.short_label.casefold()
                    ),
                    None,
                )
                if player is None:
                    seat = GameRoomEngine._parse_seat_token(value)
                    if seat is not None:
                        player = next((item for item in room.players if item.seat == seat), None)
            targets.append(player.user_id if player is not None else value)
        return targets

    @staticmethod
    def _parse_achievement(value: str) -> Achievement:
        """AdminCommands.cs:340 `Enum.TryParse` —— 接受枚举名，本端口另外接受编号与中文名。"""
        text = (value or "").strip()
        if not text:
            raise GameRuleError("请指定成就名称或编号")
        if text.isdigit() and int(text) in Achievement._value2member_map_:
            return Achievement(int(text))
        normalized = text.replace(" ", "_").replace("-", "_").casefold()
        for item in Achievement:
            if item.name.casefold() == normalized or achievement_name(item).casefold() == text.casefold():
                return item
        raise GameRuleError("找不到该成就")

    @staticmethod
    def _ban_remaining_text(expires: datetime, now: datetime) -> str:
        """DevCommands.cs:1596-1607 —— 剩余超过 365 天按永封显示。"""
        remaining = expires - now
        if remaining.days > 365:
            return "本次封禁为永久封禁。"
        hours, seconds = divmod(max(0, remaining.seconds), 3600)
        return (
            f"封禁将在 {max(0, remaining.days)} 天 "
            f"{hours} 小时 {seconds // 60} 分钟后解除。"
        )

    async def _admin_command(
        self, event: PlatformEvent, command: Command
    ) -> tuple[GameRoom | None, list[OutboundMessage], str]:
        if command.name in _DEV_COMMANDS:
            if not self._is_dev(event):
                raise GameRuleError("只有开发者可以使用该命令")
        elif not self._is_admin(event):
            raise GameRuleError("只有群管理员可以使用该命令")
        handler = getattr(self, f"_admin_{command.name}")
        return await handler(event, (command.argument or "").strip())

    async def _admin_smite(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:26-64 Smite —— 对每个目标调用 `game?.SmitePlayer(id)`。"""
        self._require_group(event)
        room = await self._room_for_event(event)
        if room is None:
            return None, [], "smite"
        events: list[DomainEvent] = []
        for user_id in self._admin_targets(room, argument):
            events.extend(self.engine.smite(room, user_id))
        return room, await self._events_to_messages(events, room, event), "smite"

    async def _admin_getidles(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:220-271 GetIdles —— 逐个目标输出 IdleCount + GroupIdleCount。"""
        self._require_group(event)
        room = await self._room_for_event(event)
        targets = self._admin_targets(room, argument)
        if not targets:
            raise GameRuleError("请在命令后附上玩家的座位号、名字或 QQ 号")
        counter = getattr(self.store, "count_idle_kills_24h", None)
        language = await self._user_language(event.user_id)
        lines: list[str] = []
        for user_id in targets:
            # 名单里统一给「座位号 + QQ 昵称」；不在本局的人只给一个中性占位，
            # 绝不把 QQ 号或任何内部标识打到群里。
            label = "（不在本局）"
            if room is not None:
                player = next((item for item in room.players if item.user_id == user_id), None)
                if player is not None:
                    label = player.short_label
            idles = await counter(user_id) if counter is not None else 0
            group_idles = (
                await counter(user_id, event.session.session_id) if counter is not None else 0
            )
            lines.append(
                get_locale_string("IdleCount", language, label, idles)
                + " "
                + get_locale_string("GroupIdleCount", language, group_idles)
            )
        return (
            room,
            [self._reply(event, "\n".join(lines), room_id=event.session.session_id)],
            "getidles",
        )

    async def _admin_setlink(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:287-322 SetLink —— 官方校验 t.me 邀请链接，QQ 版校验 http(s) 链接。"""
        self._require_group(event)
        if not argument:
            raise GameRuleError("请使用 /setlink 加上本群的邀请链接")
        if not argument.casefold().startswith(("http://", "https://")):
            raise GameRuleError("这不是一个有效的群邀请链接。")
        await self.store.save_group_fields(event.session.session_id, group_link=argument)
        return (
            None,
            [self._reply(event, f"链接已设置：{argument}", room_id=event.session.session_id)],
            "setlink",
        )

    async def _admin_remlink(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:273-286 RemLink。"""
        self._require_group(event)
        await self.store.save_group_fields(event.session.session_id, group_link=None)
        return (
            None,
            [self._reply(event, "本群的群链接已移除。", room_id=event.session.session_id)],
            "remlink",
        )

    async def _admin_resetlink(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:1475-1501 ResetLink —— 只清链接，不影响 Preferred。"""
        if not argument:
            raise GameRuleError("用法：/resetlink <群号>，只会重置群链接，不影响 Preferred 状态。")
        group_id = argument.split()[0]
        group = await self._group_record(group_id)
        if group is None:
            raise GameRuleError("找不到该群。")
        await self.store.save_group_fields(group_id, group_link=None)
        name = group.get("name") or group_id
        return (
            None,
            [self._reply(event, f"{name} 的群链接已重置。", room_id=self._room_id(event))],
            "resetlink",
        )

    async def _admin_killgame(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:237-243 KillGame —— `game?.Kill()`，没有对局时静默。"""
        self._require_group(event)
        room = await self._room_for_event(event)
        if room is None:
            return None, [], "killgame"
        events = self.engine.kill_game(room)
        return room, await self._events_to_messages(events, room, event), "killgame"

    async def _admin_skipvote(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:346-352 SkipVote —— `game?.SkipVote()`。"""
        self._require_group(event)
        room = await self._room_for_event(event)
        if room is None:
            return None, [], "skipvote"
        events = self.engine.skip_vote(room)
        return room, await self._events_to_messages(events, room, event), "skipvote"

    async def _admin_maintenance(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:154-160 Maintenance —— 取反后回显当前状态。"""
        value = not await self._maintenance_mode()
        setter = getattr(self.store, "set_bot_flag", None)
        if setter is not None:
            await setter(_MAINT_FLAG, value)
        return (
            None,
            [self._reply(event, f"维护模式：{'已开启' if value else '已关闭'}", room_id=self._room_id(event))],
            "maintenance",
        )

    async def _admin_getroles(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:331-345 GetRoles —— 逐行输出 `玩家: 身份`。"""
        room = (
            await self.store.get_room(argument.split()[0])
            if argument
            else await self._room_for_query(event)
        )
        if room is None:
            raise GameRuleError("找不到该群的对局。")
        lines = [
            f"{player.short_label}: {role_display_name(player.role) if player.role else '未分配'}"
            for player in room.players
        ]
        text = "\n".join(lines) if lines else "该对局没有玩家。"
        return None, [self._reply(event, text, room_id=self._room_id(event))], "getroles"

    async def _admin_playtime(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:315-329 PlayTime —— 指定人数对局的最短/最长/平均时长（分钟）。"""
        try:
            count = int(argument.split()[0])
        except (ValueError, IndexError) as exc:
            raise GameRuleError("用法：/playtime <玩家人数>") from exc
        getter = getattr(self.store, "playtime_stats", None)
        stats = await getter(count) if getter is not None else None
        if not stats:
            raise GameRuleError("没有该人数的对局记录。")
        text = (
            "（单位：分钟）\n"
            f"最短：{stats['minimum']:.2f}\n"
            f"最长：{stats['maximum']:.2f}\n"
            f"平均：{stats['average']:.2f}"
        )
        return None, [self._reply(event, text, room_id=self._room_id(event))], "playtime"

    async def _admin_whois(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:730-740 WhoIs —— 查不到玩家时官方完全静默。"""
        if not argument:
            raise GameRuleError("用法：/whois <QQ号>")
        user_id = argument.split()[0].lstrip("@")
        player = await self.store.get_player(user_id)
        if player is None:
            return None, [], "whois"
        text = f"玩家：{self._admin_target_label(user_id, player)}"
        return None, [self._reply(event, text, room_id=self._room_id(event))], "whois"

    async def _admin_user(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:1568-1610 GetUser —— 玩家档案 + 封禁状态。

        官方还会打印 Donation Level；QQ 版没有 Telegram Payments，捐赠体系不适用。
        """
        if not argument:
            raise GameRuleError("用法：/user <QQ号>")
        user_id = argument.split()[0].lstrip("@")
        player = await self.store.get_player(user_id)
        if player is None:
            raise GameRuleError("找不到该玩家。")
        language = await self._user_language(user_id)
        lines = [
            self._admin_target_label(user_id, player),
            f"QQ 号：{user_id}",
            "------------------",
            f"参与对局：{player['games']} 局",
            f"语言：{language}",
        ]
        if player.get("first_game"):
            lines.append(f"首次参与对局：{player['first_game']:%Y-%m-%d %H:%M:%S}")
        lines.append(f"累计被临时封禁 {player['temp_ban_count']} 次")
        ban = await self.store.get_global_ban(user_id)
        now = datetime.now(timezone.utc)
        if ban and ban["expires"] > now:
            lines.extend([
                "------------------",
                "该玩家当前处于封禁状态",
                f"封禁原因：{ban['reason']}",
                f"封禁时间：{ban['ban_date']:%Y-%m-%d %H:%M:%S}",
                f"操作人：{ban['banned_by'] or '未知'}",
                self._ban_remaining_text(ban["expires"], now),
            ])
        else:
            lines.append("该玩家没有任何封禁记录。")
        return None, [self._reply(event, "\n".join(lines), room_id=self._room_id(event))], "user"

    async def _admin_getban(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:131-160 GetUserStatus。"""
        if not argument:
            raise GameRuleError("用法：/getban <QQ号>")
        user_id = argument.split()[0].lstrip("@")
        ban = await self.store.get_global_ban(user_id)
        now = datetime.now(timezone.utc)
        lines: list[str] = []
        if ban and ban["expires"] > now:
            lines.append(f"封禁原因：{ban['reason']}")
            lines.append(f"操作人：{ban['banned_by'] or '未知'}，时间 {ban['ban_date']:%Y-%m-%d %H:%M:%S}")
            remaining = ban["expires"] - now
            if remaining.days > 365:
                lines.append("永久封禁")
            else:
                hours, seconds = divmod(max(0, remaining.seconds), 3600)
                lines.append(
                    f"剩余 {max(0, remaining.days)} 天 {hours} 小时 "
                    f"{seconds // 60} 分钟"
                )
        else:
            lines.append("该玩家未被封禁。")
        player = await self.store.get_player(user_id)
        if player and player.get("first_seen"):
            lines.append(f"首次出现时间：{player['first_seen']:%Y-%m-%d %H:%M:%S}")
        else:
            lines.append("该玩家从未参与过对局。")
        return None, [self._reply(event, "\n".join(lines), room_id=self._room_id(event))], "getban"

    async def _admin_getbans(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:817-846 GetBans —— 先临时封禁，再单独一条永久封禁。

        官方第一段是内存里的 SpamBanList；QQ 版没有 Telegram 的刷屏封禁通道，
        所有封禁都落在 global_bans 表，故只输出数据库的两段。
        """
        lister = getattr(self.store, "list_global_bans", None)
        bans = await lister() if lister is not None else []
        now = datetime.now(timezone.utc)
        temporary = [item for item in bans if (item["expires"] - now).days <= 365]
        permanent = [item for item in bans if (item["expires"] - now).days > 365]

        def render(records: list[dict]) -> str:
            return "\n".join(
                f"{self._admin_target_label(str(item['user_id']), item)}"
                f"：{item['reason']}\n"
                f"到期时间：{item['expires']:%Y-%m-%d %H:%M:%S}"
                for item in records
            )

        first = "数据库中的临时封禁\n" + (render(temporary) or "（无临时封禁）")
        second = "永久封禁\n" + (render(permanent) or "（无永久封禁）")
        return (
            None,
            [
                self._reply(event, first, room_id=self._room_id(event)),
                self._plain(event, event.session, second, self._room_id(event)),
            ],
            "getbans",
        )

    async def _admin_permban(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:848-980 PermBan —— 建档、踢出当前对局、写入永久封禁。"""
        parts = argument.split(maxsplit=1)
        if not parts:
            raise GameRuleError("用法：/permban <QQ号> <原因>")
        user_id = parts[0].lstrip("@")
        reason = parts[1].strip() if len(parts) > 1 else ""
        await self.store.touch_player(user_id)
        player = await self.store.get_player(user_id)
        await self.store.add_global_ban(
            user_id,
            name=(player or {}).get("name"),
            reason=reason,
            banned_by=(
                event.display_name.strip()
                if not is_opaque_display_name(event.display_name)
                else event.user_id
            ),
            expires=_PERMANENT_BAN_EXPIRES,
        )
        name = self._admin_target_label(user_id, player)
        messages = [
            self._reply(event, f"玩家 {name} 已被永久封禁。", room_id=self._room_id(event))
        ]
        room = None
        if event.session.session_type == SessionType.GROUP:
            room = await self._room_for_event(event)
            if room is not None:
                messages.extend(
                    await self._events_to_messages(self.engine.smite(room, user_id), room, event)
                )
        return room, self._single_reply(messages), "permban"

    async def _admin_remban(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:982-1056 RemBan —— 没有封禁记录时官方静默。"""
        if not argument:
            raise GameRuleError("用法：/remban <QQ号>")
        user_id = argument.split()[0].lstrip("@")
        removed = await self.store.remove_global_ban(user_id)
        if not removed:
            return None, [], "remban"
        return (
            None,
            [self._reply(event, "该玩家的封禁已解除。", room_id=self._room_id(event))],
            "remban",
        )

    async def _admin_notifyban(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:724-729 NotifyBan —— 官方指向 @werewolfbanappeal，QQ 版改为提示申诉。"""
        if not argument:
            raise GameRuleError("用法：/notifyban <QQ号>")
        target = argument.split()[0].lstrip("@")
        text = "你已被封禁，如有异议可以到支持群申诉。"
        return None, [await self._private_message(target, text, event)], "notifyban"

    async def _admin_notifyspam(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:718-723 NotifySpam。"""
        if not argument:
            raise GameRuleError("用法：/notifyspam <QQ号>")
        target = argument.split()[0].lstrip("@")
        return (
            None,
            [await self._private_message(target, "请不要这样刷屏。", event)],
            "notifyspam",
        )

    async def _admin_preferred(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:1289-1328 Preferred —— 官方给 InlineKeyboard 开关，QQ 版直接取反。"""
        if not argument:
            raise GameRuleError("用法：/preferred <群号>")
        group_id = argument.split()[0]
        group = await self._group_record(group_id)
        if group is None:
            raise GameRuleError("找不到该群。")
        # DevCommands.cs:595 —— 官方判定写作 `Preferred != false`，未设置视为启用。
        value = group.get("preferred") is False
        await self.store.save_group_fields(group_id, preferred=value)
        name = group.get("name") or group_id
        return (
            None,
            [self._reply(
                event,
                f"{name} 的推荐群标记已{'开启' if value else '关闭'}。",
                room_id=self._room_id(event),
            )],
            "preferred",
        )

    async def _admin_bangroup(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:68-85 BanGroup —— `CreatedBy = "BAN"` 后退群。

        QQ 机器人无法主动退群，等价做法是标记封禁（Helpers.cs:92-96 之后一律拒绝开局）
        并向该群公告，同时结束该群正在进行的对局。
        """
        group_id = (
            argument.split()[0]
            if argument
            else (
                event.session.session_id
                if event.session.session_type == SessionType.GROUP
                else ""
            )
        )
        if not group_id:
            raise GameRuleError("用法：/bangroup <群号>")
        group = await self._group_record(group_id)
        await self.store.save_group_fields(group_id, created_by="BAN", bot_in_group=False)
        name = (group or {}).get("name") or group_id
        room = await self.store.get_room(group_id)
        messages = [
            self._reply(event, f"{name} 已被封禁。", room_id=self._room_id(event)),
            self._plain(
                event,
                PlatformSession(SessionType.GROUP, group_id),
                "本群已被封禁，机器人不再在此提供游戏服务。",
                group_id,
            ),
        ]
        if self._is_active(room):
            messages.extend(await self._events_to_messages(self.engine.kill_game(room), room, event))
            return room, self._single_reply(messages), "bangroup"
        return None, self._single_reply(messages), "bangroup"

    async def _admin_leavegroup(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:1249-1287 LeaveGroup。"""
        if not argument:
            raise GameRuleError("用法：/leavegroup <群号>")
        group_id = argument.split()[0]
        group = await self._group_record(group_id)
        if group is None:
            raise GameRuleError("找不到该群，请确认群号是否正确。")
        await self.store.save_group_fields(group_id, bot_in_group=False)
        name = group.get("name") or group_id
        return (
            None,
            [
                self._reply(
                    event, f"机器人已退出群 {name}。", room_id=self._room_id(event)
                ),
                self._plain(
                    event,
                    PlatformSession(SessionType.GROUP, group_id),
                    "管理员让我不能再陪各位玩了，先撤啦！",
                    group_id,
                ),
            ],
            "leavegroup",
        )

    async def _admin_addach(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:324-405 AddAchievement —— 解锁后私聊本人并在原会话回执。"""
        parts = argument.split(maxsplit=1)
        if len(parts) < 2:
            raise GameRuleError("用法：/addach <QQ号> <成就名或编号>")
        user_id = parts[0].lstrip("@")
        achievement = self._parse_achievement(parts[1])
        await self.store.touch_player(user_id)
        player = await self.store.get_player(user_id)
        name = self._admin_target_label(user_id, player)
        current = set(await self.store.get_player_achievements(user_id))
        if achievement.value in current:
            text = f"成就「{achievement_name(achievement)}」{name} 早就已经解锁了。"
            return None, [self._reply(event, text, room_id=self._room_id(event))], "addach"
        await self.store.merge_player_achievements(user_id, [achievement.value])
        unlocked = (
            "成就解锁！\n"
            f"{achievement_name(achievement)}\n{achievement_description(achievement)}"
        )
        return (
            None,
            self._single_reply([
                self._reply(
                    event,
                    f"已为 {name} 解锁成就「{achievement_name(achievement)}」。",
                    room_id=self._room_id(event),
                ),
                await self._private_message(user_id, unlocked, event),
            ]),
            "addach",
        )

    async def _admin_remach(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:410-494 RemAchievement。"""
        parts = argument.split(maxsplit=1)
        if len(parts) < 2:
            raise GameRuleError("用法：/remach <QQ号> <成就名或编号>")
        user_id = parts[0].lstrip("@")
        achievement = self._parse_achievement(parts[1])
        player = await self.store.get_player(user_id)
        name = self._admin_target_label(user_id, player)
        current = set(await self.store.get_player_achievements(user_id))
        if achievement.value not in current:
            text = f"{name} 本来就没有解锁成就「{achievement_name(achievement)}」。"
            return None, [self._reply(event, text, room_id=self._room_id(event))], "remach"
        await self.store.remove_player_achievements(user_id, [achievement.value])
        text = f"已移除 {name} 的成就「{achievement_name(achievement)}」。"
        return None, [self._reply(event, text, room_id=self._room_id(event))], "remach"

    async def _admin_validatelangs(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:161-217 + LanguageHelper.ValidateFiles —— 官方选 Base 用
        InlineKeyboard，QQ 版改成 `/validatelangs [Base]`，不带参数即校验全部。"""
        base = argument.strip() or None
        errors = validate_language_files(base)
        detail, summary = format_validation_report(errors, base)
        return (
            None,
            [
                self._reply(event, detail, room_id=self._room_id(event)),
                self._plain(event, event.session, summary, self._room_id(event)),
            ],
            "validatelangs",
        )

    async def _admin_broadcast(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:211-235 Broadcast —— 把文本发给所有正在进行对局的群。"""
        if not argument:
            raise GameRuleError("用法：/broadcast <内容>")
        lister = getattr(self.store, "list_active_rooms", None)
        rooms = await lister() if lister is not None else []
        messages = [
            self._plain(
                event, PlatformSession(SessionType.GROUP, room.session_id), argument, room.session_id
            )
            for room in rooms
        ]
        messages.insert(
            0,
            self._reply(event, f"已广播到 {len(rooms)} 个群。", room_id=self._room_id(event)),
        )
        return None, messages, "broadcast"

    # ------------------------------------------------------------------
    # DevCommands.cs 里的调试命令
    # ------------------------------------------------------------------

    async def _admin_winchart(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:162-166 + Helpers/Charting.cs:21-207 TeamWinChart。

        官方把统计画成 1000x400 的图，但 `Charting.SendImage`（Charting.cs:210-214）
        里的 `SendPhotoAsync` 已被注释掉，真正发出的只有末尾那条
        `\\n{Players}: {Games}` 汇总文本（Charting.cs:206），这里按同一口径复刻。
        区间解析、默认起点、`>= 5` 人过滤、mode 过滤全部与官方一致；
        官方解析不出区间时只提示一句、并不中断，仍会按默认起点继续输出。
        """
        messages: list[OutboundMessage] = []
        start = datetime(2016, 5, 15, tzinfo=timezone.utc)
        mode: str | None = None
        args = (argument or "").split()
        if args:
            try:
                amount: int | None = int(args[0])
            except ValueError:
                amount = None
            if amount is not None and len(args) >= 2:
                now = datetime.now(timezone.utc)
                if args[1] in {"week", "weeks"}:
                    start = now - timedelta(days=amount * 7)
                elif args[1] in {"day", "days"}:
                    start = now - timedelta(days=amount)
                elif args[1] in {"hour", "hours"}:
                    start = now - timedelta(hours=amount)
                else:
                    messages.append(
                        self._reply(
                            event,
                            "时间区间只支持：hour(s) 小时、day(s) 天、week(s) 周。",
                            room_id=self._room_id(event),
                        )
                    )
            if len(args) == 3:
                mode = args[2]
            elif len(args) == 1:
                mode = args[0]
        getter = getattr(self.store, "win_chart_stats", None)
        rows = await getter(start, mode) if getter is not None else []
        summary = "".join(f"\n{row['players']}: {row['games']}" for row in rows)
        if summary:
            messages.append(
                self._plain(event, event.session, summary, self._room_id(event))
                if messages
                else self._reply(event, summary, room_id=self._room_id(event))
            )
        return None, self._single_reply(messages), "winchart"

    async def _admin_test(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:499-555 Test —— 逐月使用量（Usage per month over time）。

        官方渲染成 5000x2000 的图片再发送；QQ 版没有图表栈，按同一份逐月数据
        输出等价文本（月份标签沿用官方的 `M/y` 自定义格式）。
        """
        start = datetime(2016, 4, 1, tzinfo=timezone.utc)
        getter = getattr(self.store, "monthly_player_counts", None)
        rows = await getter(start) if getter is not None else []
        counts = {
            (row["month"].year, row["month"].month): row["players"]
            for row in rows
            if row.get("month") is not None
        }
        today = datetime.now(timezone.utc)
        lines = ["逐月使用量统计"]
        year, month = start.year, start.month
        while (year, month) < (today.year, today.month) or (
            (year, month) == (today.year, today.month) and today.day > 1
        ):
            lines.append(f"{month}/{year % 100}: {counts.get((year, month), 0)}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return (
            None,
            self._single_reply([
                self._reply(event, "请稍候，正在生成统计……", room_id=self._room_id(event)),
                self._plain(event, event.session, "\n".join(lines), self._room_id(event)),
            ]),
            "test",
        )

    async def _admin_usage(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:562-586 Usage —— CPU 平均占用与可用内存。

        官方用 Windows PerformanceCounter 采样 10 次（每次间隔 500ms）后编辑原消息；
        Linux 版改读 `/proc/stat` 与 `/proc/meminfo`，采样次数与间隔保持一致；
        QQ 没有消息编辑接口，占位提示与结果拆成两条。
        """
        samples: list[int] = []
        previous = self._cpu_times()
        for _ in range(_USAGE_SAMPLES):
            await asyncio.sleep(_USAGE_INTERVAL)
            current = self._cpu_times()
            if previous is None or current is None:
                continue
            total_delta = current[0] - previous[0]
            idle_delta = current[1] - previous[1]
            previous = current
            if total_delta > 0:
                samples.append(int((1 - idle_delta / total_delta) * 100))
        cpu_avg = int(sum(samples) / len(samples)) if samples else 0
        ram = self._available_memory_mb()
        return (
            None,
            self._single_reply([
                self._reply(event, "请稍候，正在采集数据……", room_id=self._room_id(event)),
                self._plain(
                    event,
                    event.session,
                    f"CPU 占用：{cpu_avg}%\r\n可用内存：{ram}MB",
                    self._room_id(event),
                ),
            ]),
            "usage",
        )

    async def _admin_checkgroups(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:588-625 CheckGroupList —— 找出群名里同时含“官方”与“狼”变体的群。

        官方用 `Unidecode()` 做音译再小写；端口不额外引入依赖，改用 NFKD 去掉
        组合附加符号后小写，对这两组纯 ASCII 关键字判定结果一致。
        """
        lister = getattr(self.store, "list_linked_groups", None)
        groups = await lister() if lister is not None else []
        ofc_spells = ("official", "offciail", "official", "oficial", "offical")
        wuff_spells = ("wolf", "wuff", "wulf", "lupus")
        matched = []
        for group in groups:
            original = group.get("name") or ""
            folded = self._fold_name(original)
            if any(item in folded for item in ofc_spells) and any(
                item in folded for item in wuff_spells
            ):
                matched.append((original, group.get("group_id"), group.get("group_link")))
        if matched:
            result = "检测到群名同时包含「官方」与「狼」各种拼写变体的群：\n"
            result += "".join(f"\n{name} - {gid} - {link}" for name, gid, link in matched)
        else:
            result = "没有发现群名同时包含「官方」与「狼」变体的群。"
        return (
            None,
            self._single_reply([
                self._reply(event, "请稍候，正在检索……", room_id=self._room_id(event)),
                self._plain(event, event.session, result, self._room_id(event)),
            ]),
            "checkgroups",
        )

    async def _admin_clearcount(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:712-716 ClearCount —— 清空 UserMessages，官方不回消息。"""
        self._user_messages.clear()
        return None, [], "clearcount"

    async def _admin_getcommands(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:741-747 GetCommands —— 打印某用户最近一分钟内发过的命令。

        官方 `UserMessages[target]` 取不到键会抛 KeyNotFoundException，
        由 UpdateHandler 统一回执错误；端口改成一条明确的拒绝信息。
        官方还会 `long.Parse` 用户 ID，这里统一按字符串处理，不做转换。
        """
        target = argument.split()[0].lstrip("@") if argument else ""
        entries = self._user_messages.get(target)
        if not entries:
            raise GameRuleError("没有该用户最近一分钟内的命令记录。")
        reply = "".join(f"\n{text}" for _time, text in entries)
        return None, [self._reply(event, reply, room_id=self._room_id(event))], "getcommands"

    async def _admin_reloadenglish(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:705-710 ReloadEnglish —— 重新载入语言文件，官方不回消息。"""
        CATALOG.reload()
        return None, [], "reloadenglish"

    async def _admin_moveachv(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:114-151 MoveAchv —— 把旧版 Achievements 位图迁到新版 BitArray。

        端口只存新版成就（`player_achievements.achievements int[]`），没有官方
        `Players.Achievements` 那一列旧位图，因此任何玩家都会走官方的
        “没有可迁移的旧记录”分支；命令语法与四条回执文案与官方保持一致。
        """
        user_id = argument.split()[0].lstrip("@") if argument else ""
        if not user_id:
            return (
                None,
                [self._reply(event, "命令格式：/moveachv <QQ号>", room_id=self._room_id(event))],
                "moveachv",
            )
        player = await self.store.get_player(user_id)
        if player is None:
            text = f"数据库里找不到 QQ 号 {user_id} 对应的玩家。"
        else:
            text = f"玩家 {self._admin_target_label(user_id, player)} 没有可迁移的旧成就记录。"
        return None, [self._reply(event, text, room_id=self._room_id(event))], "moveachv"

    async def _admin_ohaider(self, event: PlatformEvent, argument: str):
        """DevCommands.cs:1330-1376 OhAiDer + UpdateHandler.cs:1069-1124 的 `ohai` 回调。

        给所有与目标玩家同局过、且还没解锁 OHAIDER 的玩家补发该成就。
        官方用 Yes/No 内联菜单二次确认，QQ 版没有内联按钮（与 `/validatelangs`
        同样处理），命令直接执行并输出官方确认后的两段文案。
        """
        if event.session.session_type not in {SessionType.C2C, SessionType.DIRECT}:
            return (
                None,
                [self._reply(event, "这条命令只能私聊机器人使用。")],
                "ohaider",
            )
        user_id = argument.split()[0].lstrip("@") if argument else ""
        if not user_id:
            return None, [self._reply(event, "QQ号无效。")], "ohaider"
        player = await self.store.get_player(user_id)
        if player is None:
            return (
                None,
                [self._reply(event, "找不到该QQ号对应的玩家档案。")],
                "ohaider",
            )
        try:
            lister = getattr(self.store, "coplayers_missing_achievement", None)
            targets = await lister(user_id, Achievement.OHAIDER.value) if lister else []
            for target in targets:
                await self.store.merge_player_achievements(target, [Achievement.OHAIDER.value])
        except Exception as exc:  # 官方 catch 后原样回执异常信息
            return (
                None,
                [self._reply(event, f"更新 OHAIDER 成就失败：{exc}")],
                "ohaider",
            )
        text = (
            f"发现 {len(targets)} 名新达成 OHAIDER 条件的玩家。\n"
            f"已为 {len(targets)} 名玩家补发成就。\n处理完成。"
        )
        return None, [self._reply(event, text)], "ohaider"

    async def _admin_fi(self, event: PlatformEvent, argument: str):
        """AdminCommands.cs:671-684 ForceInfo + Program.cs:251-295 GetFullInfo。

        官方先发带 `Time to receive` 的消息，再编辑追加 `Time to reply`；
        QQ 没有消息编辑接口，两段合并成一条发出。官方的每节点明细在 QQ 版
        没有对应物（没有 Control/Node 拆分），Nodes 恒为 1。
        """
        received = datetime.now(timezone.utc)
        games, players = await self._run_snapshot()
        text = "运行信息\n" + self._full_info(games, players)
        text += f"\n收到耗时：{self._format_elapsed(received - event.occurred_at)}"
        text += f"\n回复耗时：{self._format_elapsed(datetime.now(timezone.utc) - received)}"
        return None, [self._reply(event, text, room_id=self._room_id(event))], "fi"

    @staticmethod
    def _event_public_name(event: PlatformEvent) -> str:
        if is_opaque_display_name(event.display_name):
            return "你"
        return event.display_name

    @staticmethod
    def _fold_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(item for item in normalized if not unicodedata.combining(item)).lower()

    @staticmethod
    def _cpu_times() -> tuple[int, int] | None:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as handle:
                line = handle.readline()
        except OSError:
            return None
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        try:
            values = [int(item) for item in parts[1:]]
        except ValueError:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    @staticmethod
    def _available_memory_mb() -> int:
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
        except (OSError, IndexError, ValueError):
            return 0
        return 0

    def _begin_pending_action(
        self, room: GameRoom, event: PlatformEvent, action: str
    ) -> list[OutboundMessage]:
        """把官方的按钮选择流程折叠成可持久化的 QQ 多轮文本对话。"""
        if action not in _TARGETED_NIGHT_COMMANDS | _TARGETED_DAY_COMMANDS | _CONFIRM_ONLY_DAY_COMMANDS:
            raise GameRuleError("这个行动需要直接发送确认或跳过")
        if action in _TARGETED_NIGHT_COMMANDS and room.phase != GamePhase.NIGHT:
            raise GameRuleError("现在不是夜晚行动阶段")
        if action == "mayor":
            if room.phase not in {GamePhase.DAY, GamePhase.VOTE}:
                raise GameRuleError("现在不是市长公开身份的阶段")
        elif action in _TARGETED_DAY_COMMANDS | _CONFIRM_ONLY_DAY_COMMANDS and room.phase != GamePhase.DAY:
            raise GameRuleError("现在不是白天行动阶段")
        player = next((item for item in room.players if item.user_id == event.user_id), None)
        if player is None or not player.alive:
            raise GameRuleError("你不在当前房间或已经出局")
        required = 0 if action in _CONFIRM_ONLY_DAY_COMMANDS else 1
        pending = dict(room.statistics.get("pending_actions") or {})
        pending[event.user_id] = {
            "action": action,
            "targets": [],
            "required": required,
            "phase": room.phase.value,
            "day": room.day,
        }
        room.statistics["pending_actions"] = pending
        room.state_version += 1
        return [self._reply(event, self._target_prompt(room, event.user_id, action, 1, required), room_id=room.session_id)]

    async def _continue_pending_action(
        self, room: GameRoom, event: PlatformEvent, command: Command
    ) -> list[OutboundMessage] | None:
        pending = dict(room.statistics.get("pending_actions") or {})
        state = pending.get(event.user_id)
        if state is None:
            return None
        if state.get("phase") != room.phase.value or state.get("day") != room.day:
            pending.pop(event.user_id, None)
            room.statistics["pending_actions"] = pending
            room.state_version += 1
            return [self._reply(event, "上一项选择已经过期，请按当前阶段重新发送行动指令。", room_id=room.session_id)]
        if command.name == "cancel":
            pending.pop(event.user_id, None)
            room.statistics["pending_actions"] = pending
            room.state_version += 1
            return [self._reply(event, "已取消本次选择。", room_id=room.session_id)]
        action = str(state["action"])
        targets = [str(item) for item in state.get("targets", [])]
        required = int(state.get("required", 1))
        if command.name == "reselect":
            state["targets"] = []
            pending[event.user_id] = state
            room.statistics["pending_actions"] = pending
            room.state_version += 1
            return [self._reply(event, self._target_prompt(room, event.user_id, action, 1, required), room_id=room.session_id)]
        if command.name == "confirm":
            if len(targets) != required:
                return [self._reply(event, self._target_prompt(room, event.user_id, action, len(targets) + 1, required), room_id=room.session_id)]
            pending.pop(event.user_id, None)
            room.statistics["pending_actions"] = pending
            room.state_version += 1
            argument = " ".join(targets)
            if action in _TARGETED_NIGHT_COMMANDS:
                events = self.engine.submit_night_action(
                    room, event.user_id, _NIGHT_ACTION_TEXT.get(action, action), argument
                )
            else:
                events = self.engine.submit_day_action(
                    room, event.user_id, action, "yes" if required == 0 else argument
                )
            return await self._events_to_messages(events, room, event)
        if command.name != "unknown":
            return None
        token = (command.argument or "").strip()
        if not token:
            return [self._reply(event, self._target_prompt(room, event.user_id, action, len(targets) + 1, required), room_id=room.session_id)]
        try:
            target = self.engine._target(
                room, token, actor_id=event.user_id, allow_self=action == "cupid"
            )
        except GameRuleError as exc:
            return [self._reply(event, f"这个座位不能选：{exc}\n" + self._target_prompt(room, event.user_id, action, len(targets) + 1, required), room_id=room.session_id)]
        if target.seat in {int(value) for value in targets}:
            return [self._reply(event, "不能重复选择同一位玩家。", room_id=room.session_id)]
        targets.append(str(target.seat))
        state["targets"] = targets
        pending[event.user_id] = state
        room.statistics["pending_actions"] = pending
        room.state_version += 1
        if len(targets) < required:
            return [self._reply(event, self._target_prompt(room, event.user_id, action, len(targets) + 1, required), room_id=room.session_id)]
        selected = "、".join(f"{seat}号" for seat in targets)
        return [self._reply(event, f"已选择 {selected}。发送“确认”提交，发送“重选”重新选择，或发送“取消”。", room_id=room.session_id)]

    @staticmethod
    def _target_prompt(room: GameRoom, user_id: str, action: str, current: int, total: int) -> str:
        if total == 0:
            return "请确认使用本次能力：发送“确认”使用，或发送“取消”。"
        allow_self = action == "cupid"
        choices = [
            player for player in room.players
            if player.alive and (allow_self or player.user_id != user_id)
        ]
        if room.rules.shuffle_player_list:
            # Werewolf.cs:4960/5036/5107/5124/5322/5432 —— ShufflePlayerList 会打乱菜单选项顺序。
            choices = list(choices)
            random.shuffle(choices)
        return (
            f"请选择第 {current}/{total} 个目标：\n"
            + "\n".join(player.short_label for player in choices)
            + "\n直接回复座位号；可发送“取消”结束本次选择。"
        )

    async def _official_info(self, event: PlatformEvent, name: str, argument: str | None = None):
        if name == "ping":
            text = "pong，机器人在线。"
        elif name == "chatid":
            text = f"当前会话：{event.session.session_id}"
        elif name == "version":
            text = "Werewolf QQ Bot 0.1.0，规则基线 official-ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7。"
        elif name == "changelog":
            text = "本版本已对齐官方身份规则、QQ 私聊行动、群级规则配置和官方辅助指令。"
        elif name == "achv":
            return await self._achievements_command(event)
        elif name == "runinfo":
            # GeneralCommands.cs:68-77 RunInfo。QQ 版没有 Control/Node 拆分，Nodes 恒为 1。
            games, players = await self._run_snapshot()
            uptime = self._format_uptime(datetime.now(timezone.utc) - self.start_time)
            text = (
                "运行信息\n"
                f"运行时长：{uptime}\n"
                "节点数：1\n"
                f"进行中对局：{games}\n"
                f"在场玩家：{players}"
            )
        elif name == "rolelist":
            return await self._role_list(event)
        elif name == "grouplist":
            return await self._group_list(event)
        elif name == "setlang":
            return await self._set_lang(event, argument)
        elif name == "getlang":
            return await self._get_lang(event, argument)
        elif name == "myidles":
            return await self._my_idles(event)
        else:
            text = "你当前没有连续未投票记录。"
        return await self._room_for_event(event), [self._reply(event, text, room_id=self._room_id(event))], name

    async def _prepare_achievement_state(self, room: GameRoom) -> None:
        """开局前载入成就基线：官方 AddAchievement/UpdateAchievements 每次都读数据库里的
        BitArray 与 GamePlayers 计数（Werewolf.cs:6049-6052、5850-5865），
        这里改为一次性注入，让引擎判定与官方一致（已解锁不再重复提示）。"""
        loader = getattr(self.store, "get_player_achievements", None)
        if loader is not None:
            for player in room.players:
                try:
                    player.achievements = list(await loader(player.user_id))
                except Exception:
                    log.exception("读取玩家成就失败", extra={"user_id": player.user_id})
        stats_loader = getattr(self.store, "lifetime_stats", None)
        if stats_loader is not None:
            try:
                room.statistics["lifetime_stats"] = await stats_loader(
                    [player.user_id for player in room.players]
                )
            except Exception:
                log.exception("读取玩家历史战绩失败", extra={"room_id": room.session_id})

    async def _user_language(self, user_id: str) -> str:
        """`UpdateHandler.GetLanguage(id)` —— 玩家个人语言，缺省用 DEFAULT_LANGUAGE。

        官方 Player.Language 存的是语言文件名（不含 .xml）。历史库里可能存着
        旧版占位值（例如 "中文"），这里按 `resolve_language` 反查一次，
        反查不到再退回默认文件，避免把整局提示打成空串。
        """
        getter = getattr(self.store, "get_user_language", None)
        if getter is None:
            return DEFAULT_LANGUAGE
        try:
            value = await getter(user_id)
        except Exception:
            log.exception("读取玩家语言设置失败", extra={"user_id": user_id})
            return DEFAULT_LANGUAGE
        if not value:
            return DEFAULT_LANGUAGE
        if CATALOG.get(value) is not None:
            return value
        found = resolve_language(str(value))
        return found.file_name if found is not None else DEFAULT_LANGUAGE

    async def _sent_private(self, event: PlatformEvent, language: str | None = None):
        """`GetLocaleString("SentPrivate", lang)` —— 群里回执「已通过私聊发送了详细信息」。"""

        language = language or await self._user_language(event.user_id)
        return self._reply(
            event, get_locale_string("SentPrivate", language), room_id=self._room_id(event)
        )

    async def _achievements_command(self, event: PlatformEvent):
        """GeneralCommands.cs:38-44 /achv + InlineCommand.cs:59-74 的成就展示。
        官方靠 inline 面板显示，QQ 没有 inline，因此直接列出已解锁成就。"""
        getter = getattr(self.store, "get_player_achievements", None)
        values = await getter(event.user_id) if getter is not None else []
        unlocked = [Achievement(item) for item in values if item in Achievement._value2member_map_]
        if not unlocked:
            text = f"{GameApplication._event_public_name(event)} 还没有解锁任何成就。"
        else:
            body = "\n".join(
                f"{achievement_name(item)}\n{achievement_description(item)}" for item in unlocked
            )
            text = f"{GameApplication._event_public_name(event)} 已解锁 {len(unlocked)} 个成就：\n{body}"
        messages = [await self._private_message(event.user_id, text, event)]
        if event.session.session_type == SessionType.GROUP:
            messages.append(await self._sent_private(event))
        return await self._room_for_event(event), messages, "achv"

    async def _my_idles(self, event: PlatformEvent):
        """GeneralCommands.cs:566-597 MyIdles —— 统计 24 小时内 Idle 击杀，结果发私聊，群里回 SentPrivate。"""
        counter = getattr(self.store, "count_idle_kills_24h", None)
        in_group = event.session.session_type == SessionType.GROUP
        idles = await counter(event.user_id) if counter is not None else 0
        who = GameApplication._event_public_name(event)
        text = f"{who} 在过去 24 小时内因未投票被处决 {idles} 次。"
        if in_group:
            group_idles = (
                await counter(event.user_id, event.session.session_id) if counter is not None else 0
            )
            text += f" 其中 {group_idles} 次发生在本群。"
        messages = [await self._private_message(event.user_id, text, event)]
        if in_group:
            messages.append(await self._sent_private(event))
        return await self._room_for_event(event), messages, "myidles"

    async def _set_lang(self, event: PlatformEvent, argument: str | None):
        """GeneralCommands.cs:112-185 /setlang —— 本移植锁定简体中文，只回一条说明。

        官方是三级 InlineKeyboard 选语言；本项目按需求「默认且仅使用中文」把
        `domain/locale.LANGUAGE_LOCKED` 置为 True，所有取词都强制走简体中文文件，
        再提供语言切换只会让玩家以为切换成功、实际毫无变化。
        指令本身保留（老玩家仍会输入），但一律回一条明确说明，绝不静默吞掉。
        """
        text = "本机器人当前仅提供简体中文，暂不支持切换语言。"
        return (
            await self._room_for_event(event),
            [self._reply(event, text, room_id=self._room_id(event))],
            "setlang",
        )

    async def _get_lang(self, event: PlatformEvent, argument: str | None):
        """GeneralCommands.cs:435-470 /getlang —— 同 `_set_lang`，锁定简体中文后只回说明。

        官方会把语言 XML 作为文档发出；QQ 机器人接口没有文件附件通道
        （见 `application/contracts.OutboundMessage`），加上本项目已锁定单语言，
        这条命令不再列出语言清单。
        """
        text = "本机器人当前仅提供简体中文，没有其它语言文件可供选择。"
        return (
            await self._room_for_event(event),
            [self._reply(event, text, room_id=self._room_id(event))],
            "getlang",
        )

    async def _about_command(self, event: PlatformEvent, key: str):
        """UpdateHandler.cs:339-362 —— /aboutXxx 前缀路由。

        官方 `GetAbout` 找不到 key 时返回 null，此时 `String.IsNullOrEmpty(reply)`
        为真，整个分支直接 return，不给任何回应（静默）。命中时只私聊发送，
        群里不发 SentPrivate（官方那几行被注释掉了，见 350-354）。
        """
        text = get_about(key, await self._user_language(event.user_id))
        if not text:
            return await self._room_for_event(event), [], "about"
        messages = [await self._private_message(event.user_id, text, event)]
        return await self._room_for_event(event), messages, "about"

    async def _role_list(self, event: PlatformEvent):
        """HelpCommands.cs:87-156 /rolelist —— 分五条私聊发送身份索引。

        官方按 10/10/9/10/3 拆成五条，条间 `Thread.Sleep(300)`；只有第一条之后
        在非私聊会话里回一条 SentPrivate，其余四条静默发送。
        """
        language = await self._user_language(event.user_id)
        messages: list[OutboundMessage] = []
        for index, page in enumerate(role_list_pages(language)):
            messages.append(await self._private_message(event.user_id, page, event))
            if index == 0 and event.session.session_type == SessionType.GROUP:
                messages.append(await self._sent_private(event, language))
        return await self._room_for_event(event), messages, "rolelist"

    async def _group_list(self, event: PlatformEvent):
        """HelpCommands.cs:24-84 /grouplist + UpdateHandler.cs:1303-1370 `groups|` 回调。

        官方流程是「私聊发语言菜单 → 选语言 → 选变体 → 列出该语言最近 21 天内活跃、
        按 LastRefresh 再按 Ranking 倒序的前 10 个群」。QQ 没有 InlineKeyboard，
        这里直接按发起者当前语言的 Base 走到最后一步，等同于官方「只有一个变体时
        直接跳过菜单」的分支（UpdateHandler.cs:1314-1317），标题仍用官方 HereIsList。
        """
        language = await self._user_language(event.user_id)
        lang_file = CATALOG.get(language)
        choice = lang_file.base if lang_file is not None else language
        lister = getattr(self.store, "list_public_groups", None)
        groups = await lister() if lister is not None else []
        if not groups:
            text = "最近 21 天内还没有活跃的狼人杀群。"
        else:
            # 群名拿不到时用群号兜底。NapCat 下 group_id 就是真实群号，
            # 群号本身是公开信息，可以直接展示。
            body = "\n\n".join(
                f"{item.get('name') or ('群' + str(item['group_id']))}"
                f"（近 21 天 {item.get('games', 0)} 局）"
                for item in groups
            )
            text = get_locale_string("HereIsList", language, choice) + "\n\n" + body
        messages = [await self._private_message(event.user_id, text, event)]
        if event.session.session_type == SessionType.GROUP:
            messages.append(await self._sent_private(event, language))
        return await self._room_for_event(event), messages, "grouplist"

    @staticmethod
    def _apply_group_config(base_rules, values: dict[str, object]):
        allowed = {key: value for key, value in values.items() if key in _GROUP_RULE_FIELDS}
        if "mode" in allowed:
            allowed["mode"] = GameMode(allowed["mode"])
        for key in ("disabled_roles", "required_roles"):
            if key in allowed:
                allowed[key] = tuple(allowed[key])
        candidate = replace(base_rules, **allowed)
        candidate.validate()
        return candidate

    @staticmethod
    def _update_group_config(values: dict[str, object], argument: str) -> dict[str, object]:
        if "=" not in argument:
            raise GameRuleError("配置格式应为 /config 项目=值")
        key, raw = (part.strip() for part in argument.split("=", 1))
        if key not in _GROUP_RULE_FIELDS:
            raise GameRuleError("未知群规则项目，请先发送 /config 查看")
        boolean_fields = {
            "random_lynch", "secret_lynch", "secret_lynch_show_votes",
            "secret_lynch_show_voters", "thief_full", "allow_arsonist", "burning_overkill",
            "show_roles_on_death", "allow_flee", "allow_extend", "random_mode",
            "show_ids", "shuffle_player_list", "allow_nsfw",
        }
        if key in boolean_fields:
            if raw.casefold() not in {"true", "false", "1", "0", "yes", "no"}:
                raise GameRuleError("开关只能填写 true 或 false")
            value: object = raw.casefold() in {"true", "1", "yes"}
        elif key in {"disabled_roles", "required_roles"}:
            value = [item.strip() for item in raw.split(",") if item.strip()]
        elif key == "mode":
            try:
                value = GameMode(raw).value
            except ValueError as exc:
                raise GameRuleError("mode 只能是 Normal 或 Chaos") from exc
        elif key == "show_roles_end":
            value = raw.title()
            if value not in {"None", "Living", "All"}:
                raise GameRuleError("show_roles_end 只能是 None、Living 或 All")
        else:
            try:
                value = int(raw)
            except ValueError as exc:
                raise GameRuleError("人数和时间必须填写整数") from exc
        updated = dict(values)
        updated[key] = value
        if key in {"disabled_roles", "max_players"}:
            # UpdateHandler.cs:1851-1912 —— togglerole 后必须 validateroles 才生效；
            # QQ 版没有按钮菜单，改为在写入时直接做等价的 TryBalance 校验。
            disabled = tuple(updated.get("disabled_roles", ()) or ())
            if disabled:
                for item in disabled:
                    try:
                        role = Role(item)
                    except ValueError as exc:
                        raise GameRuleError(f"未知角色：{item}") from exc
                    if not ROLE_METADATA[role].can_be_disabled:
                        raise GameRuleError(f"官方角色不可禁用：{role.value}")
                max_players = int(updated.get("max_players", 35) or 35)
                if not try_balance(disabled, max_players):
                    raise GameRuleError("该禁用组合无法在 5 至上限人数间配平，已拒绝保存")
        return updated

    def _config_text(self, values: dict[str, object]) -> str:
        """UpdateHandler.cs:2169-2249 —— GetConfigMenu/GetConfigSubmenu 的文本等价物。"""
        effective = self._apply_group_config(self.rules, values)

        def show(key: str) -> str:
            current = getattr(effective, key, None)
            if isinstance(current, GameMode):
                current = current.value
            elif isinstance(current, bool):
                current = "开" if current else "关"
            elif isinstance(current, tuple):
                current = "、".join(str(item) for item in current) or "无"
            marker = " *" if key in values else ""
            return f"{key}={current}{marker}"

        lines = ["本群规则（* 表示已被本群覆盖）："]
        for title, keys in _CONFIG_GROUPS:
            lines.append(f"[{title}]")
            lines.append("  " + "，".join(show(key) for key in keys))
        lines.append("发送 /config 项目=值 修改规则（仅管理员）。")
        lines.append("角色禁用示例：/config disabled_roles=Tanner,Fool（保存前会做官方配平校验）。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 定时推进（Program.cs 的 GameTimer 循环）
    # ------------------------------------------------------------------

    # 已完结（sent/dead）的投递记录保留一天，够排查也不会撑爆表。
    _DELIVERY_RETENTION_SECONDS = 86400

    def _timer_event(self, room: GameRoom, event_id: str) -> PlatformEvent:
        """定时器推进用的合成事件。

        NapCat 可以主动发消息，超时播报直接发群里就行，
        不再需要官方开放平台那套「借一条群消息当回复锚点」的把戏。
        """

        return PlatformEvent(
            event_id=event_id,
            session=PlatformSession(SessionType.GROUP, room.session_id),
            user_id="system",
            display_name="system",
            text="",
        )

    async def process_due_rooms(self) -> int:
        processed = 0
        for listed_room in await self.store.list_active_rooms():
            if not self.engine.due(listed_room):
                continue
            lock = await self._lock_for(f"group:{listed_room.session_id}")
            async with self._event_limit, lock:
                room = await self.store.get_room(listed_room.session_id)
                if room is None or not self.engine.due(room):
                    continue
                if self.engine.is_stale(room):
                    event_id = (
                        f"timer-abandon:{room.session_id}:{room.state_version}:{room.phase.value}"
                    )
                    claimed, _ = await self.store.begin_event(event_id, f"timer:{room.session_id}")
                    if not claimed:
                        continue
                    await self._abandon_stale_room(room, event_id)
                    processed += 1
                    continue
                event_id = f"timer:{room.session_id}:{room.state_version}:{room.phase.value}"
                claimed, _ = await self.store.begin_event(event_id, f"timer:{room.session_id}")
                if not claimed:
                    continue
                before_phase = room.phase
                TRACE.record(
                    room.session_id,
                    "定时器",
                    f"阶段「{self._phase_text(before_phase)}」已到期，开始推进",
                    状态版本=room.state_version,
                )
                before_snapshot = room.snapshot()
                try:
                    if room.phase == GamePhase.LOBBY:
                        await self._prepare_achievement_state(room)
                    events = self.engine.on_timeout(room)
                    timer_event = self._timer_event(room, event_id)
                    messages = self._split_messages(await self._events_to_messages(events, room, timer_event))
                    await self.store.commit_result(
                        room=room,
                        event_id=event_id,
                        messages=messages,
                        action_key=event_id,
                        action_data={"name": "timeout", "user_id": "system"},
                    )
                    TRACE.record(
                        room.session_id,
                        "定时器",
                        f"推进完成：{self._phase_text(before_phase)} → {self._phase_text(room.phase)}，"
                        f"产出 {len(messages)} 条消息",
                        状态版本=room.state_version,
                    )
                    if room.phase == GamePhase.CANCELLED:
                        await self.store.delete_room(room.session_id)
                    processed += 1
                except Exception as exc:
                    log.exception("阶段计时推进失败", extra={"room_id": room.session_id})
                    TRACE.record(
                        room.session_id,
                        "定时器",
                        f"推进失败：{type(exc).__name__}: {exc}",
                    )
                    # 引擎或消息转换可能已部分修改房间。恢复推进前快照后明确解散，
                    # 避免该 timer event 被幂等标记为 done 却永远停在过期阶段。
                    room = GameRoom.from_snapshot(before_snapshot)
                    room_events = self.engine.abandon_overdue(room)
                    error = [
                        OutboundMessage(
                            target=PlatformSession(SessionType.GROUP, room.session_id),
                            text="阶段计时处理失败，请管理员检查服务日志。",
                            room_id=room.session_id,
                        )
                    ]
                    error.extend(
                        await self._events_to_messages(
                            room_events, room, self._timer_event(room, event_id)
                        )
                    )
                    await self.store.commit_result(
                        room=room,
                        event_id=event_id,
                        messages=error,
                        action_key=event_id,
                        action_data={"name": "timeout_failed", "user_id": "system"},
                    )
        return processed

    async def _abandon_stale_room(self, room: GameRoom, event_id: str) -> None:
        overdue = self.engine.overdue_seconds(room) or 0
        TRACE.record(
            room.session_id,
            "定时器",
            f"阶段「{self._phase_text(room.phase)}」已超时 {int(overdue)} 秒，自动解散",
            状态版本=room.state_version,
        )
        events = self.engine.abandon_overdue(room)
        timer_event = self._timer_event(room, event_id)
        messages = self._split_messages(await self._events_to_messages(events, room, timer_event))
        await self.store.commit_result(
            room=room,
            event_id=event_id,
            messages=messages,
            action_key=event_id,
            action_data={"name": "timeout_abandon", "user_id": "system"},
        )
        deleter = getattr(self.store, "delete_room", None)
        if deleter is not None:
            await deleter(room.session_id)
        TRACE.record(room.session_id, "定时器", "已因超时自动解散并清理房间")

    async def process_deliveries(self, sender, max_attempts: int) -> int:
        """认领一批待投递消息并发送；单个目标失败不影响整批。

        NapCat 可以主动发消息，不存在官方开放平台那种「缺回复锚点」的挂起态，
        所以失败只分两类：
        - 永久失败（`PermanentSendError`，参数非法、目标不是 QQ 号、被限制）：
          直接判死，不再占用队列也不再计入告警。
        - 其余失败：按指数退避重试，用满次数才落 failed。
        """
        processed = 0
        # 顺手清理早就完结的记录，成本很低但能防止队列无限膨胀。
        await self._sweep_deliveries()
        for pending in await self.store.pending_deliveries():
            record = await self.store.claim_delivery(pending.delivery_id)
            if record is None:
                continue
            try:
                await sender.send_delivery(record)
            except PermanentSendError as exc:
                await self.store.mark_delivery_dead(record.delivery_id, str(exc))
                log.warning(
                    "消息投递被平台永久拒绝，已停止重试",
                    extra={"delivery_id": record.delivery_id, "reason": str(exc)[:120]},
                )
                TRACE.record(
                    record.room_id,
                    "投递",
                    f"平台永久拒绝，不再重试：{str(exc)[:120]}",
                    投递号=record.delivery_id[:12],
                )
            except Exception as exc:
                await self.store.mark_delivery_failure(
                    record.delivery_id, str(exc), max_attempts
                )
                log.warning(
                    "消息投递失败",
                    extra={"delivery_id": record.delivery_id, "reason": str(exc)[:120]},
                )
                TRACE.record(
                    record.room_id,
                    "投递",
                    f"投递失败：{str(exc)[:120]}",
                    投递号=record.delivery_id[:12],
                )
            else:
                await self.store.mark_delivery_sent(record.delivery_id)
            processed += 1
        return processed

    async def _sweep_deliveries(self) -> None:
        """清理早已完结的投递记录；出错只记日志，不影响正常发送。"""
        purge = getattr(self.store, "purge_finished_deliveries", None)
        if purge is not None:
            try:
                await purge(self._DELIVERY_RETENTION_SECONDS)
            except Exception:
                log.exception("清理历史投递失败")

    async def _room_for_event(self, event: PlatformEvent) -> GameRoom | None:
        if event.session.session_type == SessionType.GROUP:
            room = await self.store.get_room(event.session.session_id)
            room = room if self._is_active(room) else None
        else:
            rooms = await self.store.find_active_rooms_for_user(event.user_id)
            if len(rooms) > 1:
                raise GameRuleError("你同时参加了多个房间，请先在对应群里结束其他房间")
            room = rooms[0] if rooms else None
        # 房间快照是入场那一刻存下来的，昵称/QQ 号可能后来才补上，这里统一回填。
        await self._refresh_player_identities(room)
        return room

    async def _room_for_query(self, event: PlatformEvent) -> GameRoom | None:
        if event.session.session_type == SessionType.GROUP:
            room = await self.store.get_room(event.session.session_id)
        else:
            # 私聊查询优先当前活动房间；历史对局不应阻塞正在进行的身份查询。
            active_rooms = await self.store.find_active_rooms_for_user(event.user_id)
            if len(active_rooms) == 1:
                room = active_rooms[0]
            elif active_rooms:
                room = None
            else:
                rooms = await self.store.find_rooms_for_user(event.user_id)
                room = rooms[0] if len(rooms) == 1 else None
        await self._refresh_player_identities(room)
        return room

    async def _ensure_user_can_join_active_room(self, user_id: str, session_id: str) -> None:
        active_rooms = await self.store.find_active_rooms_for_user(user_id)
        if any(room.session_id != session_id for room in active_rooms):
            raise GameRuleError("你已在其他群的活动房间中")

    def _show_join_prompt(self, room: GameRoom, event: PlatformEvent) -> list[OutboundMessage]:
        """Werewolf.cs:1429-1439 ShowJoinButton —— 入场阶段重新展示加入入口。

        官方的 15 秒节流只是为了不刷屏，端口过去直接 `return []`，
        结果玩家重复发 /startgame 时一个字都收不到，看起来就像机器人死了。
        现在节流期间改成回一条精简状态：不再写房间状态、不再刷新播报，
        但一定给发指令的人一条可见回复。
        """
        if room.phase != GamePhase.LOBBY:
            return [
                self._reply(
                    event,
                    f"本群这局已经开始了（当前阶段：{self._phase_text(room.phase)}），"
                    f"发送 /status 查看进度。",
                    room_id=room.session_id,
                )
            ]
        now = datetime.now(timezone.utc)
        needed = max(0, room.rules.min_players - len(room.players))
        host_name = self._host_name(room)
        if needed:
            tail = f"还差 {needed} 人才能开局。"
        else:
            tail = f"人数已经够了，发起人{host_name}发送 /startgame 或 /go 即可开局。"
        last = room.statistics.get("last_join_button_at")
        if last:
            try:
                previous = datetime.fromisoformat(str(last))
            except ValueError:
                previous = None
            if previous is not None and (now - previous).total_seconds() <= 15:
                # 节流：不改房间状态，但仍然给出可见反馈。
                TRACE.record(
                    room.session_id,
                    "指令分发",
                    "入场提示处于 15 秒节流内，改回精简状态",
                    用户=event.user_id,
                )
                return [
                    self._reply(
                        event,
                        f"本群正在等待入场，当前 {len(room.players)} 人。{tail}\n"
                        f"发送 /join 加入，/status 查看详情。",
                        room_id=room.session_id,
                    )
                ]
        room.statistics["last_join_button_at"] = now.isoformat()
        room.state_version += 1
        return [
            self._reply(
                event,
                f"本群已经在等待入场了，请不要重复使用命令，直接发送 /join 加入。\n"
                f"当前 {len(room.players)} 人，至少需要 {room.rules.min_players} 人。{tail}",
                room_id=room.session_id,
            )
        ]

    @staticmethod
    def _host_name(room: GameRoom) -> str:
        """把 host_user_id 换成玩家看得懂的名字，取不到就用中性说法。"""
        if not room.host_user_id:
            return "（本局暂无发起人）"
        player = next((item for item in room.players if item.user_id == room.host_user_id), None)
        return player.public_name if player is not None else "（发起人已离场）"

    @staticmethod
    def _phase_text(phase: GamePhase) -> str:
        """阶段的中文名，用于所有面向玩家的提示。"""
        return {
            GamePhase.LOBBY: "等待入场",
            GamePhase.NIGHT: "夜晚",
            GamePhase.DAY: "白天讨论",
            GamePhase.VOTE: "投票",
            GamePhase.FINISHED: "已结束",
            GamePhase.CANCELLED: "已取消",
        }.get(phase, phase.value)

    async def _next_game(
        self, event: PlatformEvent
    ) -> tuple[GameRoom | None, list[OutboundMessage], str]:
        """GeneralCommands.cs:390-433 NextGame —— 只加入 NotifyGame 等待名单并私聊确认。"""
        self._require_group(event)
        group_id = event.session.session_id
        await self._ensure_user_can_join_active_room(event.user_id, group_id)
        adder = getattr(self.store, "add_wait_list", None)
        added = True
        if adder is not None:
            added = await adder(group_id, event.user_id)
        text = (
            f"你已经在群组 {group_id} 排队了。当下次游戏开始时，我会通知你。"
            if added
            else f"你已经在群组 {group_id} 排队了。"
        )
        text += "\n发送 /stopwaiting 可以取消排队。"
        return None, [await self._private_message(event.user_id, text, event)], "nextgame"

    @staticmethod
    def _admin_target_label(user_id: str, player: dict | None = None) -> str:
        """管理/后台指令里统一的可读身份。

        管理命令是给管理员排查用的，可以带真实 QQ 号；
        但绝不再出现「内部号」这类系统标识 —— NapCat 下压根没有那种东西。
        """
        record = player or {}
        nickname = str(record.get("name") or "").strip()
        if is_opaque_display_name(nickname):
            nickname = ""
        return f"QQ 号：{user_id}｜昵称：{nickname or '（未知昵称）'}"

    async def _refresh_player_identities(self, room: GameRoom | None) -> None:
        """用 players 表里的最新昵称回填房间快照。

        入场那一刻拿到的昵称可能是空的（极少数消息不带 sender.nickname），
        事后玩家再发一条消息就会被 `_remember_user` 归档，这里补回来，
        名单才能稳定显示「1号 沉潜」而不是光秃秃的座位号。
        """
        if room is None or not room.players:
            return
        getter = getattr(self.store, "get_player_profiles", None)
        if getter is None:
            return
        try:
            profiles = await getter([player.user_id for player in room.players])
        except Exception:
            log.exception("回填玩家身份信息失败", extra={"room_id": room.session_id})
            return
        for player in room.players:
            profile = profiles.get(player.user_id)
            if not profile:
                continue
            name = str(profile.get("name") or "").strip()
            # 只在当前展示名不可用时才覆盖，避免把本局用的昵称改掉。
            if name and not is_opaque_display_name(name) and (
                not player.display_name.strip()
                or is_opaque_display_name(player.display_name)
            ):
                player.display_name = name

    async def _stop_waiting(
        self, event: PlatformEvent, argument: str | None
    ) -> tuple[GameRoom | None, list[OutboundMessage], str]:
        """GameCommands.cs:189-218 StopWaiting —— 删除等待名单并把确认发到私聊。"""
        if event.session.session_type == SessionType.GROUP:
            group_id = event.session.session_id
        else:
            group_id = (argument or "").strip()
        if not group_id:
            raise GameRuleError("找不到群组，请在群里发送这个命令，或在命令后附上群号")
        remover = getattr(self.store, "remove_wait_list", None)
        if remover is not None:
            await remover(group_id, event.user_id)
        return (
            None,
            [await self._private_message(event.user_id, f"你已经将 {group_id} 从排队清单中移除了。", event)],
            "stop_waiting",
        )

    async def _notify_wait_list(self, event: PlatformEvent) -> list[OutboundMessage]:
        """Commands/Helpers.cs:150-165 —— 新对局开始后私聊通知排队玩家（开局者除外）。"""
        lister = getattr(self.store, "list_wait_list", None)
        if lister is None:
            return []
        group_id = event.session.session_id
        messages: list[OutboundMessage] = []
        for user_id in await lister(group_id):
            if user_id == event.user_id:
                continue
            messages.append(
                await self._private_message(
                    user_id, f"新游戏即将在群组 {group_id} 开始，请把握时间加入！", event
                )
            )
        clearer = getattr(self.store, "clear_wait_list", None)
        if clearer is not None:
            await clearer(group_id)
        return messages

    async def _private_message(
        self, user_id: str, text: str, source: PlatformEvent
    ) -> OutboundMessage:
        target = await self._private_target(user_id, source)
        return OutboundMessage(
            target=target.session,
            text=text,
            reply_to=target.reply_to,
            source_event_id=source.event_id,
            event_id=target.event_id,
        )

    async def _private_destination(
        self, user_id: str, source: PlatformEvent
    ) -> tuple[PlatformSession, str | None, str | None]:
        """兼容旧签名：只要会话与锚点，不关心是怎么解析出来的。"""
        target = await self._private_target(user_id, source)
        return target.session, target.reply_to, target.event_id

    async def _private_target(self, user_id: str, source: PlatformEvent) -> _PrivateTarget:
        """算出私聊目标会话。

        NapCat 下这件事已经没有难度可言：user_id 就是玩家的真实 QQ 号，群聊里
        看到的那一份和私聊窗口里的那一份完全相同，直接 send_private_msg 即可，
        不需要绑定、不需要映射表，也不存在「无好友关系」那种作用域错配。

        唯一还值得区分的情况是：这条消息本来就是该玩家私聊发来的，
        那就顺手带上 msg_id 走引用回复，观感更好。
        """
        if (
            source.session.session_type in {SessionType.C2C, SessionType.DIRECT}
            and source.user_id == user_id
        ):
            return _PrivateTarget(
                source.session, source.reply_message_id, source.event_id, "当前私聊会话"
            )
        return _PrivateTarget(
            PlatformSession(SessionType.C2C, str(user_id)), None, None, "napcat 私聊"
        )

    @staticmethod
    def _skip_action(player) -> str | None:
        if player is None:
            return None
        # Werewolf.cs:5439 —— HunterKill 菜单自带 Skip 按钮，端口用 /skip 复刻。
        if player.metadata.get("pending_hunt"):
            return "猎杀"
        actions = tuple(action for action in ROLE_ACTIONS.get(player.role, ()) if action not in DAY_ACTIONS)
        return actions[0].value if len(actions) == 1 else None

    @staticmethod
    def _is_active(room: GameRoom | None) -> bool:
        return bool(room and room.phase not in {GamePhase.FINISHED, GamePhase.CANCELLED})

    @staticmethod
    def _lock_key(event: PlatformEvent) -> str:
        if event.session.session_type == SessionType.GROUP:
            return event.session.key
        return f"user:{event.user_id}"

    @staticmethod
    def _room_id(event: PlatformEvent) -> str | None:
        return event.session.session_id if event.session.session_type == SessionType.GROUP else None

    @staticmethod
    def _require_group(event: PlatformEvent) -> None:
        if event.session.session_type != SessionType.GROUP:
            raise GameRuleError("该命令请在群里发送")

    @staticmethod
    def _require_c2c(event: PlatformEvent) -> None:
        if event.session.session_type not in {SessionType.C2C, SessionType.DIRECT}:
            raise GameRuleError("夜间行动请私聊机器人")

    @staticmethod
    def _reject_if_dead(room: GameRoom, event: PlatformEvent, command: Command) -> None:
        """出局玩家在本局内不再参与游戏（用户需求第四条）。

        领域层各个动作本来就会各自校验存活，这里再加一道统一闸门：
        死者发来的任何操作类指令都直接拒掉，不进状态机、不占用行动名额，
        也不会因为某个动作忘了校验而漏出去。查询类指令照常放行。
        """
        if room.phase in {GamePhase.LOBBY, GamePhase.FINISHED, GamePhase.CANCELLED}:
            return
        if command.name in _DEAD_ALLOWED_COMMANDS:
            return
        player = next((item for item in room.players if item.user_id == event.user_id), None)
        if player is None or player.alive:
            return
        raise GameRuleError("你已经出局，本局不能再进行任何操作。")

    def _is_admin(self, event: PlatformEvent) -> bool:
        # OneBot 群消息会带 sender.role；优先使用实时群角色，
        # 静态白名单继续作为私聊、测试和平台未提供角色时的兜底。
        group_admin = (
            event.session.session_type == SessionType.GROUP
            and str(event.group_role or "").casefold() in {"owner", "admin"}
        )
        return group_admin or event.user_id in self.admin_user_ids or event.user_id in self.dev_user_ids

    def _may_start(self, room: GameRoom, event: PlatformEvent) -> bool:
        """判断这条 /startgame 能不能当作「立刻开局」处理。

        本局发起人（房主）本人、或管理员，且人数已达下限时才算数。
        房主已离场（host_user_id 为空）时退化成「任一在场玩家都能开」，
        与 GameRoomEngine._require_host 的判定保持一致，避免两边给出矛盾的结论。
        条件不满足就返回 False，走原来的「重新展示加入入口」，绝不静默失败。
        """

        if len(room.players) < room.rules.min_players:
            return False
        if self._is_admin(event):
            return True
        if room.host_user_id is not None:
            return room.host_user_id == event.user_id
        return any(player.user_id == event.user_id for player in room.players)

    def _is_dev(self, event: PlatformEvent) -> bool:
        return event.user_id in self.dev_user_ids

    async def _events_to_messages(
        self, events: Iterable[DomainEvent], room: GameRoom, source: PlatformEvent
    ) -> list[OutboundMessage]:
        messages: list[OutboundMessage] = []
        # 局内状态一有变化就把群名片同步过去（开局铺号码、出局标已出局、终局清空）。
        await self._sync_cards(room)
        # 同一会话内的批内序号：让「同一条消息回复多条」时每条 delivery_id 不同。
        counters: dict[str, int] = {}
        for domain_event in events:
            # QQ 版投票确认按当前玩法在群里播报；领域事件仍保留官方的私聊目标，便于其他平台复用。
            broadcast_vote = domain_event.kind == "vote_accepted" and room.phase == GamePhase.VOTE
            if domain_event.public or broadcast_vote:
                target = PlatformSession(SessionType.GROUP, room.session_id)
                reply_to = None
                event_id = None
                if target.key == source.session.key:
                    reply_to = source.reply_message_id
                    event_id = source.event_id
            else:
                if not domain_event.target_user_id:
                    log.warning(
                        "私聊事件缺少目标用户，已丢弃", extra={"room_id": room.session_id}
                    )
                    TRACE.record(
                        room.session_id,
                        "消息生成",
                        f"私聊事件「{domain_event.kind}」没有目标用户，已丢弃",
                    )
                    continue
                private = await self._private_target(domain_event.target_user_id, source)
                target = private.session
                reply_to = private.reply_to
                event_id = private.event_id
            index = counters.get(target.key, 0)
            counters[target.key] = index + 1
            messages.append(
                OutboundMessage(
                    target=target,
                    text=domain_event.text,
                    room_id=room.session_id,
                    reply_to=reply_to,
                    source_event_id=source.event_id,
                    event_id=event_id,
                    sequence=index,
                )
            )
        TRACE.record(
            room.session_id,
            "消息生成",
            f"生成 {len(messages)} 条出站消息，阶段={room.phase.value}",
            事件数=len(messages),
        )
        return messages

    @staticmethod
    def _reply(event: PlatformEvent, text: str, *, room_id: str | None = None) -> OutboundMessage:
        return OutboundMessage(
            target=event.session,
            text=text,
            room_id=room_id,
            reply_to=event.reply_message_id,
            source_event_id=event.event_id,
            event_id=event.event_id,
        )

    def _split_messages(self, messages: Iterable[OutboundMessage]) -> list[OutboundMessage]:
        """按长度切片，并给同一会话的每条消息编批内序号。

        批内序号只用来区分 delivery_id，保证「一条来源消息拆成多段」时每段
        都是独立的投递记录；NapCat 可以主动发消息，不需要任何回复锚点。
        """
        result: list[OutboundMessage] = []
        counters: dict[str, int] = {}
        for message in messages:
            chunks = [
                message.text[index : index + self.message_max_chars]
                for index in range(0, max(1, len(message.text)), self.message_max_chars)
            ]
            for chunk in chunks:
                index = counters.get(message.target.key, 0)
                counters[message.target.key] = index + 1
                result.append(
                    OutboundMessage(
                        target=message.target,
                        text=chunk,
                        room_id=message.room_id,
                        reply_to=message.reply_to,
                        source_event_id=message.source_event_id,
                        event_id=message.event_id,
                        sequence=index,
                    )
                )
        # Program.cs:260 messagesTx —— 每条实际投递的消息计一次。
        self.messages_sent += len(result)
        now = datetime.now(timezone.utc)
        self._tx_window.extend([now] * len(result))
        self._trim_window(self._tx_window, now)
        return result

    @staticmethod
    def _roster_players(room: GameRoom) -> list:
        """Werewolf.cs:1374-1397 —— SendPlayerList 的排序规则。

        非 ShufflePlayerList：所有玩家按 TimeDied 升序（死亡者按死亡顺序在前，
        存活者 TimeDied=MaxValue 保持加入顺序）。
        ShufflePlayerList：死亡者按死亡顺序在前，存活者随机排列。
        """
        dead = sorted(
            (p for p in room.players if not p.alive),
            key=lambda p: int(p.metadata.get("death_sequence", 0)),
        )
        alive = [p for p in room.players if p.alive]
        if room.rules.shuffle_player_list:
            alive = list(alive)
            random.shuffle(alive)
        return dead + alive

    @staticmethod
    def _status_text(room: GameRoom) -> str:
        deadline = room.stage_deadline
        remaining = 0
        if deadline:
            remaining = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
        phase_names = {
            GamePhase.LOBBY: "入场",
            GamePhase.NIGHT: "夜晚",
            GamePhase.DAY: "讨论",
            GamePhase.VOTE: "投票",
            GamePhase.FINISHED: "已结束",
            GamePhase.CANCELLED: "已取消",
        }
        roster = "、".join(
            p.short_label for p in GameApplication._roster_players(room)
        )
        if room.phase == GamePhase.LOBBY:
            # Werewolf.cs:1376 —— joining 阶段只播报总人数，且不做随机排序。
            count_line = f"人数：{len(room.players)}/{room.rules.max_players}"
        else:
            count_line = f"人数：{sum(p.alive for p in room.players)}/{len(room.players)}"
        return (
            f"当前阶段：{phase_names[room.phase]}\n"
            f"{count_line}\n"
            f"剩余时间：{remaining} 秒\n"
            f"玩家：{roster or '暂无'}"
        )

    async def _stats_command(self, event: PlatformEvent):
        """StatsController.GroupStats/PlayerStats 的等价文本版本：QQ 版没有统计网站，直接在机器人内输出。"""
        lines: list[str] = []
        room = await self._room_for_query(event)
        if room is not None:
            lines.append(self._stats_text(room))
        if event.session.session_type == SessionType.GROUP:
            group_stats = getattr(self.store, "group_history_stats", None)
            data = await group_stats(event.session.session_id) if group_stats is not None else None
            if data is not None:
                survivor = data.get("best_survivor")
                lines.append(
                    f"本群历史：共进行 {data.get('games', 0)} 局。最佳幸存者："
                    + (f"{survivor[0]}（{survivor[1]}%）" if survivor else "对局数不足")
                )
        player_stats = getattr(self.store, "player_history_stats", None)
        data = await player_stats(event.user_id) if player_stats is not None else None
        if data is None:
            lines.append(f"{GameApplication._event_public_name(event)} 还没有已结算的对局记录。")
        else:
            games = data["games"]
            def pct(value: int) -> int:
                return value * 100 // games if games else 0
            role = data.get("most_common_role")
            killed = data.get("most_killed")
            killed_by = data.get("most_killed_by")
            lines.append(
                f"{GameApplication._event_public_name(event)} 的战绩：共 {games} 局，"
                f"胜 {data['won']} 局（{pct(data['won'])}%），"
                f"负 {data['lost']} 局（{pct(data['lost'])}%），"
                f"存活 {data['survived']} 局（{pct(data['survived'])}%）。\n"
                f"最常担任身份：{role[0] if role else '无'}（{role[1] if role else 0} 次）。\n"
                f"击杀最多：{killed[0] if killed else '无'}（{killed[1] if killed else 0} 次）。\n"
                f"最常被谁击杀：{killed_by[0] if killed_by else '无'}（{killed_by[1] if killed_by else 0} 次）。"
            )
        return room, [self._reply(event, "\n".join(lines), room_id=self._room_id(event))], "stats"

    @staticmethod
    def _stats_text(room: GameRoom) -> str:
        history = len(room.vote_history)
        alive = sum(player.alive for player in room.players)
        return (
            f"统计：第 {room.day} 天，阶段 {room.phase.value}，存活 {alive}/{len(room.players)}。\n"
            f"已结算投票 {history} 次，状态版本 {room.state_version}。"
        )

    @staticmethod
    def _result_text(room: GameRoom) -> str:
        if room.phase != GamePhase.FINISHED:
            return "本局尚未结束。\n" + GameApplication._status_text(room)
        winner = "、".join(team.value for team in room.statistics.get("winner_teams", [])) or (room.winner.value if room.winner else "未知")
        lines = [f"存活人数：{sum(player.alive for player in room.players)} / {len(room.players)}"]
        team_names = {
            Team.VILLAGE: "村民阵营", Team.WOLF: "狼人阵营", Team.CULT: "教会阵营",
            Team.SERIAL_KILLER: "连环杀手", Team.ARSONIST: "纵火者", Team.LOVERS: "恋人",
            Team.TANNER: "坦纳", Team.NO_ONE: "无人",
        }
        if room.rules.show_roles_end == "None":
            lines.extend(player.short_label for player in room.players)
        elif room.rules.show_roles_end == "All":
            lines.extend(
                f"{player.short_label}："
                f"{role_display_name(player.role) if player.role else '未知'}，"
                f"{'存活' if player.alive else '出局'}，"
                f"{team_names.get(player.team, player.team.value if player.team else '未知阵营')}，"
                f"{'胜利' if player.won else '失败'}"
                for player in room.players
            )
        else:
            lines.extend(
                f"{player.short_label}："
                f"{role_display_name(player.role) if player.role else '未知'}，"
                f"{team_names.get(player.team, player.team.value if player.team else '未知阵营')}，"
                f"{'胜利' if player.won else '失败'}"
                for player in room.players if player.alive
            )
        roster = "\n".join(lines)
        return f"本局结算：{winner}\n{roster}"
