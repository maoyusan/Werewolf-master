"""独立复刻审计：管理员 / 开发者命令。

官方期望值来源（唯一）：
`work/upstream-official/Werewolf for Telegram`。
- 群管理员命令：`Werewolf Control/Commands/AdminCommands.cs`
- 开发者命令：`Werewolf Control/Commands/DevCommands.cs`
- 权限属性：`Werewolf Control/Attributes/CommandAttribute.cs`
- 全局封禁静默丢弃：`Werewolf Control/Handlers/UpdateHandler.cs:330-334`
- 封禁群 / 维护模式：`Werewolf Control/Commands/Helpers.cs:92-96`、`Program.cs:35,393`

QQ 端把官方的 GlobalAdminOnly / DevOnly / LangAdminOnly 合并成 `DEV_USER_IDS` 一档
（QQ 没有 Telegram 的群管理员查询接口），开发者同时具备群管理员权限。
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from adapters.qq.events import normalize_c2c_message, normalize_group_message
from application.contracts import PlatformEvent
from application.service import GameApplication
from domain.achievements import Achievement
from domain.models import GamePhase, GameRoom, Player, Role
from domain.roleinfo import role_display_name
from domain.rules import ruleset_official


_ids = itertools.count(1)
NOW = datetime.now(timezone.utc)


def run(coro):
    return asyncio.run(coro)


class AdminStore:
    """在审计替身上补齐 `/permban`、`/preferred` 等命令需要的读写接口。"""

    def __init__(self, rooms=()):
        self.rooms: dict[str, GameRoom] = {room.session_id: room for room in rooms}
        self.event_cache: dict[str, list] = {}
        self.commits: list[tuple[GameRoom | None, list]] = []
        self.players: dict[str, dict] = {}
        self.bans: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.flags: dict[str, bool] = {}
        self.achievements: dict[str, set[int]] = {}
        self.idles: dict[tuple[str, str | None], int] = {}
        self.playtime: dict[int, dict] = {}
        self.saved_fields: list[tuple[str, dict]] = []

    # --- 事件与房间 ---------------------------------------------------
    async def begin_event(self, event_id, _session_key):
        if event_id in self.event_cache:
            return False, self.event_cache[event_id]
        self.event_cache[event_id] = []
        return True, None

    async def commit_result(self, *, room, event_id, messages, **_kwargs):
        if room is not None:
            self.rooms[room.session_id] = room
        self.event_cache[event_id] = list(messages)
        self.commits.append((room, list(messages)))

    async def get_room(self, session_id):
        return self.rooms.get(session_id)

    async def list_active_rooms(self):
        active = {GamePhase.LOBBY, GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}
        return [room for room in self.rooms.values() if room.phase in active]

    async def delete_room(self, session_id):
        self.rooms.pop(session_id, None)

    async def find_active_rooms_for_user(self, user_id):
        active = {GamePhase.LOBBY, GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}
        return [
            room for room in self.rooms.values()
            if room.phase in active and any(p.user_id == user_id for p in room.players)
        ]

    async def find_rooms_for_user(self, user_id):
        return [
            room for room in self.rooms.values()
            if any(p.user_id == user_id for p in room.players)
        ]

    async def get_direct_session(self, _user_id):
        return None

    async def get_group_rule_config(self, _group_id):
        return {}

    # --- 玩家档案 -----------------------------------------------------
    async def touch_player(self, user_id, name=None):
        record = self.players.setdefault(
            user_id,
            {
                "user_id": user_id, "name": name, "temp_ban_count": 0,
                "first_seen": NOW, "games": 0, "first_game": None,
            },
        )
        if name:
            record["name"] = name

    async def get_player(self, user_id):
        return self.players.get(user_id)

    async def get_player_language(self, _user_id):
        return None

    # --- 封禁 ---------------------------------------------------------
    async def get_global_ban(self, user_id):
        return self.bans.get(user_id)

    async def list_global_bans(self):
        return sorted(self.bans.values(), key=lambda item: (item["expires"], item["user_id"]))

    async def add_global_ban(self, user_id, *, name, reason, banned_by, expires):
        self.bans[user_id] = {
            "user_id": user_id, "name": name, "reason": reason,
            "banned_by": banned_by, "ban_date": NOW, "expires": expires,
        }

    async def remove_global_ban(self, user_id):
        return self.bans.pop(user_id, None) is not None

    # --- 群 -----------------------------------------------------------
    async def get_group(self, group_id):
        return self.groups.get(group_id)

    async def save_group_fields(self, group_id, **fields):
        self.groups.setdefault(group_id, {"group_id": group_id}).update(fields)
        self.saved_fields.append((group_id, fields))

    # --- 其他 ---------------------------------------------------------
    async def get_bot_flag(self, name):
        return self.flags.get(name, False)

    async def set_bot_flag(self, name, value):
        self.flags[name] = value

    async def playtime_stats(self, player_count):
        return self.playtime.get(player_count)

    async def count_idle_kills_24h(self, user_id, group_id=None):
        return self.idles.get((user_id, group_id), 0)

    async def get_player_achievements(self, user_id):
        return sorted(self.achievements.get(user_id, set()))

    async def merge_player_achievements(self, user_id, values):
        self.achievements.setdefault(user_id, set()).update(values)

    async def remove_player_achievements(self, user_id, values):
        self.achievements.setdefault(user_id, set()).difference_update(values)


def make_app(rooms=(), admins=("adm",), devs=("dev",)):
    store = AdminStore(rooms)
    return GameApplication(store, admin_user_ids=admins, dev_user_ids=devs), store


def group_event(content, *, user="dev", name=None, group="g1", event_id=None) -> PlatformEvent:
    return normalize_group_message({
        "id": event_id or f"evt-{next(_ids)}",
        "group_openid": group,
        "content": content,
        "timestamp": "2026-09-01T00:00:00+00:00",
        "author": {"member_openid": user, "username": name or f"玩家{user}"},
    })


def c2c_event(content, *, user="dev", name=None, event_id=None) -> PlatformEvent:
    return normalize_c2c_message({
        "id": event_id or f"evt-{next(_ids)}",
        "content": content,
        "timestamp": "2026-09-01T00:00:00+00:00",
        "author": {"user_openid": user, "username": name or f"玩家{user}"},
    })


def texts(messages) -> str:
    return "\n".join(message.text for message in messages)


def lobby_room(session_id="g1", count=5) -> GameRoom:
    return GameRoom(
        session_id=session_id,
        rules=ruleset_official(),
        phase=GamePhase.LOBBY,
        players=[Player(f"u{i}", f"玩家u{i}", i) for i in range(1, count + 1)],
    )


def day_room(session_id="g1") -> GameRoom:
    roles = [Role.WOLF, Role.SEER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER]
    return GameRoom(
        session_id=session_id,
        rules=ruleset_official(),
        phase=GamePhase.DAY,
        day=1,
        players=[Player(f"u{i}", f"玩家u{i}", i, role=role) for i, role in enumerate(roles, 1)],
    )


# ---------------------------------------------------------------------------
# 权限（Attributes/CommandAttribute.cs）
# ---------------------------------------------------------------------------

def test_every_admin_command_is_routed_and_implemented():
    """命令表、权限表、处理函数三者必须一一对应，避免漏接线。"""
    from application.commands import _ALIASES
    from application.service import _ADMIN_COMMANDS, _DEV_COMMANDS, _GROUP_ADMIN_COMMANDS

    assert not (_DEV_COMMANDS & _GROUP_ADMIN_COMMANDS)
    assert _ADMIN_COMMANDS == _DEV_COMMANDS | _GROUP_ADMIN_COMMANDS
    assert _ADMIN_COMMANDS <= set(_ALIASES.values())
    assert [name for name in sorted(_ADMIN_COMMANDS)
            if not hasattr(GameApplication, f"_admin_{name}")] == []

@pytest.mark.parametrize("command", ["/killgame", "/maintenance", "/permban 1 x", "/broadcast hi"])
def test_dev_commands_reject_plain_group_admin(command):
    """官方 DevOnly / GlobalAdminOnly 不接受普通群管理员。"""
    app, _ = make_app()
    messages = run(app.handle_event(group_event(command, user="adm")))
    assert "只有开发者可以使用该命令" in texts(messages)


@pytest.mark.parametrize("command", ["/smite 1", "/getidles 1", "/setlink https://x", "/remlink"])
def test_group_admin_commands_reject_normal_player(command):
    """官方 GroupAdminOnly：普通玩家不能使用。"""
    app, _ = make_app(rooms=[lobby_room()])
    messages = run(app.handle_event(group_event(command, user="u1")))
    assert "只有群管理员可以使用该命令" in texts(messages)


def test_dev_is_also_group_admin():
    """Helpers.cs:186 —— 开发者天然通过群管理员校验。"""
    app, store = make_app()
    run(app.handle_event(group_event("/setlink https://qq.com/abc", user="dev")))
    assert store.groups["g1"]["group_link"] == "https://qq.com/abc"


# ---------------------------------------------------------------------------
# 全局封禁静默丢弃（UpdateHandler.cs:330-334）
# ---------------------------------------------------------------------------

def test_banned_user_commands_are_silently_dropped():
    app, store = make_app()
    store.bans["u1"] = {
        "user_id": "u1", "name": "玩家u1", "reason": "spam",
        "banned_by": "dev", "ban_date": NOW, "expires": NOW + timedelta(days=30),
    }
    messages = run(app.handle_event(group_event("/startgame", user="u1")))
    assert messages == []
    assert store.rooms == {}


def test_expired_ban_does_not_block_commands():
    """BanList 只装 `Expires > UtcNow` 的记录，过期封禁等同未封禁。"""
    app, store = make_app()
    store.bans["u1"] = {
        "user_id": "u1", "name": "玩家u1", "reason": "spam",
        "banned_by": "dev", "ban_date": NOW, "expires": NOW - timedelta(days=1),
    }
    messages = run(app.handle_event(group_event("/startgame", user="u1")))
    assert "房间已创建" in texts(messages)


# ---------------------------------------------------------------------------
# 维护模式与封禁群（Program.cs:35,393 + Helpers.cs:92-96）
# ---------------------------------------------------------------------------

def test_maintenance_toggle_blocks_new_games():
    app, store = make_app()
    messages = run(app.handle_event(group_event("/maintenance")))
    assert "维护模式：已开启" in texts(messages)
    assert store.flags["maintenance"] is True
    blocked = run(app.handle_event(group_event("/startgame", user="u1")))
    assert "维护中" in texts(blocked)
    run(app.handle_event(group_event("/maintenance")))
    assert store.flags["maintenance"] is False


def test_banned_group_cannot_start_game():
    app, store = make_app()
    store.groups["g1"] = {"group_id": "g1", "created_by": "BAN"}
    messages = run(app.handle_event(group_event("/startgame", user="u1")))
    assert "本群已被封禁" in texts(messages)
    assert store.rooms == {}


def test_bangroup_marks_group_and_kills_running_game():
    """DevCommands.cs:68-85 —— 标记 CreatedBy = "BAN" 并结束对局。"""
    app, store = make_app(rooms=[lobby_room()])
    messages = run(app.handle_event(group_event("/bangroup")))
    assert store.groups["g1"]["created_by"] == "BAN"
    assert store.groups["g1"]["bot_in_group"] is False
    assert "已被封禁" in texts(messages)
    # 同一条群消息可以带多条被动回复（靠 msg_seq 递增），不再降级成主动推送。
    assert [m.sequence for m in messages] == list(range(len(messages)))


# ---------------------------------------------------------------------------
# 对局控制（AdminCommands.cs / DevCommands.cs）
# ---------------------------------------------------------------------------

def test_smite_removes_player_by_seat():
    app, store = make_app(rooms=[day_room()])
    run(app.handle_event(group_event("/smite 3", user="adm")))
    room = store.rooms["g1"]
    assert not next(p for p in room.players if p.seat == 3).alive


def test_smite_without_game_is_silent():
    """官方 `game?.SmitePlayer(...)` —— 没有对局时什么也不发。"""
    app, _ = make_app()
    assert run(app.handle_event(group_event("/smite 3", user="adm"))) == []


def test_smite_unknown_target_is_silent_but_keeps_game():
    app, store = make_app(rooms=[day_room()])
    messages = run(app.handle_event(group_event("/smite 999", user="adm")))
    assert messages == []
    assert all(p.alive for p in store.rooms["g1"].players)


def test_killgame_ends_running_game():
    app, store = make_app(rooms=[lobby_room()])
    messages = run(app.handle_event(group_event("/killgame")))
    assert texts(messages)
    assert store.rooms["g1"].phase in {GamePhase.CANCELLED, GamePhase.FINISHED}


def test_killgame_without_game_is_silent():
    app, _ = make_app()
    assert run(app.handle_event(group_event("/killgame"))) == []


def test_killgame_in_private_rejected():
    """官方 InGroupOnly。"""
    app, _ = make_app()
    assert "该命令请在群里发送" in texts(run(app.handle_event(c2c_event("/killgame"))))


def test_skipvote_ends_current_stage_wait():
    """Werewolf.cs:5414-5418 —— 只把未选择的人置为跳过 / 让当前阶段立刻到点。"""
    app, store = make_app(rooms=[day_room()])
    messages = run(app.handle_event(group_event("/skipvote")))
    assert "已跳过当前阶段的剩余等待" in texts(messages)
    assert store.rooms["g1"].stage_deadline is not None


def test_getroles_lists_every_player_role():
    app, _ = make_app(rooms=[day_room()])
    text = texts(run(app.handle_event(group_event("/getroles"))))
    assert "玩家u1" in text
    assert role_display_name(Role.WOLF) in text and role_display_name(Role.SEER) in text


# ---------------------------------------------------------------------------
# 群链接（AdminCommands.cs:273-322、DevCommands.cs:1475-1501）
# ---------------------------------------------------------------------------

def test_setlink_rejects_non_url():
    app, _ = make_app()
    assert "不是一个有效的群邀请链接" in texts(run(app.handle_event(group_event("/setlink 群号123"))))


def test_remlink_clears_link():
    app, store = make_app()
    run(app.handle_event(group_event("/setlink https://qq.com/a")))
    run(app.handle_event(group_event("/remlink")))
    assert store.groups["g1"]["group_link"] is None


def test_resetlink_requires_existing_group():
    app, _ = make_app()
    assert "找不到该群" in texts(run(app.handle_event(group_event("/resetlink g9"))))


def test_resetlink_keeps_preferred():
    """官方明确只重置链接，不动 Preferred。"""
    app, store = make_app()
    store.groups["g2"] = {"group_id": "g2", "name": "G2", "preferred": True, "group_link": "x"}
    run(app.handle_event(group_event("/resetlink g2")))
    assert store.groups["g2"]["group_link"] is None
    assert store.groups["g2"]["preferred"] is True


def test_preferred_toggles_from_unset_to_disabled():
    """DevCommands.cs:595 判定写作 `Preferred != false`，未设置视为启用，切换后为 False。"""
    app, store = make_app()
    store.groups["g2"] = {"group_id": "g2", "name": "G2", "preferred": None}
    run(app.handle_event(group_event("/preferred g2")))
    assert store.groups["g2"]["preferred"] is False
    run(app.handle_event(group_event("/preferred g2")))
    assert store.groups["g2"]["preferred"] is True


def test_leavegroup_marks_bot_out_and_says_goodbye():
    app, store = make_app()
    store.groups["g2"] = {"group_id": "g2", "name": "G2"}
    messages = run(app.handle_event(group_event("/leavegroup g2")))
    assert store.groups["g2"]["bot_in_group"] is False
    assert any(m.target.session_id == "g2" for m in messages)


# ---------------------------------------------------------------------------
# 封禁管理（DevCommands.cs:817-1056、AdminCommands.cs:131-160）
# ---------------------------------------------------------------------------

def test_permban_writes_permanent_ban_and_smites_in_group():
    app, store = make_app(rooms=[day_room()])
    messages = run(app.handle_event(group_event("/permban u2 作弊")))
    ban = store.bans["u2"]
    assert ban["reason"] == "作弊"
    assert ban["banned_by"] == "玩家dev"
    assert (ban["expires"] - NOW).days > 365
    assert "已被永久封禁" in texts(messages)
    assert not next(p for p in store.rooms["g1"].players if p.user_id == "u2").alive


def test_remban_without_record_is_silent():
    app, _ = make_app()
    assert run(app.handle_event(group_event("/remban u9"))) == []


def test_remban_removes_existing_ban():
    app, store = make_app()
    run(app.handle_event(group_event("/permban u2 x")))
    messages = run(app.handle_event(group_event("/remban u2")))
    assert "封禁已解除" in texts(messages)
    assert "u2" not in store.bans


def test_getban_reports_permanent_ban():
    app, _ = make_app()
    run(app.handle_event(group_event("/permban u2 作弊")))
    text = texts(run(app.handle_event(group_event("/getban u2"))))
    assert "封禁原因：作弊" in text and "永久封禁" in text


def test_getban_for_clean_unknown_player():
    app, _ = make_app()
    text = texts(run(app.handle_event(group_event("/getban u9"))))
    assert "该玩家未被封禁" in text
    assert "从未参与过对局" in text


def test_getbans_splits_temporary_and_permanent():
    app, store = make_app()
    store.bans["u2"] = {
        "user_id": "u2", "name": "A", "reason": "temp", "banned_by": "dev",
        "ban_date": NOW, "expires": NOW + timedelta(days=7),
    }
    run(app.handle_event(group_event("/permban u3 perm")))
    messages = run(app.handle_event(group_event("/getbans")))
    assert len(messages) == 2
    assert "temp" in messages[0].text and "u2" in messages[0].text
    assert "永久封禁" in messages[1].text and "u3" in messages[1].text
    # 两条都挂在同一条群消息上，靠 msg_seq 区分（见 service._single_reply）。
    assert messages[1].event_id == messages[0].event_id
    assert [m.sequence for m in messages] == [0, 1]


def test_user_profile_shows_ban_block():
    app, store = make_app()
    run(app.handle_event(group_event("/permban u2 作弊")))
    store.players["u2"]["games"] = 12
    text = texts(run(app.handle_event(group_event("/user u2"))))
    assert "参与对局：12 局" in text
    assert "该玩家当前处于封禁状态" in text
    assert "本次封禁为永久封禁。" in text


def test_user_unknown_player_rejected():
    app, _ = make_app()
    assert "找不到该玩家" in texts(run(app.handle_event(group_event("/user u9"))))


def test_whois_unknown_is_silent():
    """DevCommands.cs:730-740 —— 查不到时官方不回消息。"""
    app, _ = make_app()
    assert run(app.handle_event(group_event("/whois u9"))) == []


def test_whois_known_player():
    app, store = make_app()
    store.players["u2"] = {
        "user_id": "u2", "name": "阿狼", "temp_ban_count": 0,
        "first_seen": NOW, "games": 3, "first_game": NOW,
    }
    text = texts(run(app.handle_event(group_event("/whois u2"))))
    # 统一身份格式「qq号：xxx｜昵称：yyy」；没绑定真号时给短内部号，
    # 不再把完整 openid 原样回显。
    assert "阿狼" in text and "qq号：" in text and "昵称：" in text


def test_notifyban_and_notifyspam_go_to_private():
    app, _ = make_app()
    ban = run(app.handle_event(group_event("/notifyban u2")))
    spam = run(app.handle_event(group_event("/notifyspam u2")))
    assert ban[0].target.session_id == "u2" and "已被封禁" in ban[0].text
    assert spam[0].target.session_id == "u2" and "刷屏" in spam[0].text


# ---------------------------------------------------------------------------
# 统计与闲置（DevCommands.cs:315-329、AdminCommands.cs:220-271）
# ---------------------------------------------------------------------------

def test_playtime_requires_number():
    app, _ = make_app()
    assert "用法：/playtime" in texts(run(app.handle_event(group_event("/playtime abc"))))


def test_playtime_reports_minutes():
    app, store = make_app()
    store.playtime[7] = {"minimum": 12.5, "maximum": 40.0, "average": 25.25}
    text = texts(run(app.handle_event(group_event("/playtime 7"))))
    assert "（单位：分钟）" in text and "最短：12.50" in text and "平均：25.25" in text


def test_playtime_without_records_rejected():
    app, _ = make_app()
    assert "没有该人数的对局记录" in texts(run(app.handle_event(group_event("/playtime 7"))))


def test_getidles_reports_global_and_group_counts():
    app, store = make_app(rooms=[day_room()])
    store.idles[("u2", None)] = 4
    store.idles[("u2", "g1")] = 2
    text = texts(run(app.handle_event(group_event("/getidles 2", user="adm"))))
    assert "4" in text and "2" in text


def test_getidles_without_target_rejected():
    app, _ = make_app(rooms=[day_room()])
    assert "请在命令后附上" in texts(run(app.handle_event(group_event("/getidles", user="adm"))))


# ---------------------------------------------------------------------------
# 成就（AdminCommands.cs:324-494）
# ---------------------------------------------------------------------------

def test_addach_unlocks_and_notifies_player():
    app, store = make_app()
    messages = run(app.handle_event(group_event(f"/addach u2 {Achievement.EXPLORER.name}")))
    assert Achievement.EXPLORER.value in store.achievements["u2"]
    assert any(m.target.session_id == "u2" for m in messages)
    assert "解锁成就" in texts(messages)


def test_addach_twice_reports_already_unlocked():
    app, _ = make_app()
    name = Achievement.EXPLORER.name
    run(app.handle_event(group_event(f"/addach u2 {name}")))
    text = texts(run(app.handle_event(group_event(f"/addach u2 {name}"))))
    assert "早就已经解锁了" in text


def test_addach_accepts_numeric_id():
    app, store = make_app()
    run(app.handle_event(group_event(f"/addach u2 {Achievement.EXPLORER.value}")))
    assert Achievement.EXPLORER.value in store.achievements["u2"]


def test_addach_unknown_achievement_rejected():
    app, _ = make_app()
    assert "找不到该成就" in texts(run(app.handle_event(group_event("/addach u2 NotAnAch"))))


def test_remach_removes_and_reports_missing():
    app, store = make_app()
    name = Achievement.EXPLORER.name
    assert "本来就没有解锁" in texts(run(app.handle_event(group_event(f"/remach u2 {name}"))))
    run(app.handle_event(group_event(f"/addach u2 {name}")))
    text = texts(run(app.handle_event(group_event(f"/remach u2 {name}"))))
    assert "已移除" in text
    assert Achievement.EXPLORER.value not in store.achievements["u2"]


# ---------------------------------------------------------------------------
# /validatelangs、/broadcast
# ---------------------------------------------------------------------------

def test_validatelangs_returns_detail_and_summary():
    app, _ = make_app()
    messages = run(app.handle_event(c2c_event("/validatelangs")))
    assert len(messages) >= 2
    # 明细与汇总都作为同一条私聊的被动回复返回，靠 msg_seq 递增区分。
    assert messages[-1].event_id == messages[0].event_id
    assert messages[-1].sequence == len(messages) - 1


def test_validatelangs_accepts_base_filter():
    app, _ = make_app()
    messages = run(app.handle_event(c2c_event("/validatelangs English")))
    assert texts(messages)


def test_broadcast_reaches_every_active_game():
    app, _ = make_app(rooms=[lobby_room("g1"), lobby_room("g2")])
    messages = run(app.handle_event(c2c_event("/broadcast 服务器将重启")))
    targets = {m.target.session_id for m in messages}
    assert {"g1", "g2"} <= targets
    assert sum(1 for m in messages if m.text == "服务器将重启") == 2


def test_broadcast_requires_text():
    app, _ = make_app()
    assert "用法：/broadcast" in texts(run(app.handle_event(c2c_event("/broadcast"))))
