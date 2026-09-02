"""独立复刻审计（任务 8.1-8.6）：QQ 文字指令入口对照官方群命令与回调。

官方期望值来源（唯一）：
`work/upstream-official/Werewolf for Telegram`，提交 ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7。
- 群命令：`Werewolf Control/Commands/GameCommands.cs`
- 命令分发与权限：`Werewolf Control/Handlers/UpdateHandler.cs:336-443`
- 节点行为：`Werewolf Node/Werewolf.cs`（AddPlayer/RemovePlayer/ExtendTime/ForceStart/HandleReply/SendLynchMenu）

所有测试用固定的伪 QQ 事件（normalize_* + 手工 PlatformEvent），不连接真实 QQ。
不一致处按官方行为断言并标 `xfail(strict=True, reason="Q-DIFF-XX")`。
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

from adapters.qq.events import normalize_c2c_message, normalize_group_message
from application.commands import parse_command
from application.contracts import PlatformEvent, SessionType
from application.service import GameApplication
from domain.models import GameMode, GamePhase, GameRoom, Player, Role
from domain.roleinfo import role_display_name
from domain.rules import ruleset_official


_ids = itertools.count(1)


def run(coro):
    return asyncio.run(coro)


class AuditStore:
    """最小可信存储替身：保留房间对象引用，记录事件去重与提交。"""

    def __init__(self, rooms=()):  # noqa: D401
        self.rooms: dict[str, GameRoom] = {room.session_id: room for room in rooms}
        self.event_cache: dict[str, list] = {}
        self.commits: list[tuple[GameRoom | None, list]] = []

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


def make_app(rooms=(), admins=()):
    store = AuditStore(rooms)
    return GameApplication(store, admin_user_ids=admins), store


def group_event(content, *, user="u1", name=None, group="g1", event_id=None) -> PlatformEvent:
    return normalize_group_message({
        "id": event_id or f"evt-{next(_ids)}",
        "group_openid": group,
        "content": content,
        "timestamp": "2026-09-01T00:00:00+00:00",
        "author": {"member_openid": user, "username": name or f"玩家{user}"},
    })


def c2c_event(content, *, user="u1", name=None, event_id=None) -> PlatformEvent:
    return normalize_c2c_message({
        "id": event_id or f"evt-{next(_ids)}",
        "content": content,
        "timestamp": "2026-09-01T00:00:00+00:00",
        "author": {"user_openid": user, "username": name or f"玩家{user}"},
    })


def texts(messages) -> str:
    return "\n".join(message.text for message in messages)


def lobby_room(session_id="g1", count=5, **rule_kwargs) -> GameRoom:
    rules = ruleset_official(**rule_kwargs)
    room = GameRoom(
        session_id=session_id,
        rules=rules,
        phase=GamePhase.LOBBY,
        players=[Player(f"u{i}", f"玩家u{i}", i) for i in range(1, count + 1)],
    )
    return room


def phase_room(phase, roles, session_id="g1", day=1, **rule_kwargs) -> GameRoom:
    rules = ruleset_official(**rule_kwargs)
    players = [
        Player(f"u{i}", f"玩家u{i}", i, role=role)
        for i, role in enumerate(roles, start=1)
    ]
    return GameRoom(session_id=session_id, rules=rules, phase=phase, day=day, players=players)


# ---------------------------------------------------------------------------
# /startgame、/startchaos（GameCommands.cs:20-42 + Commands/Helpers.cs:41-175）
# ---------------------------------------------------------------------------

def test_startgame_creates_room_with_join_guidance():
    """官方 Node.StartGame 建局并展示加入入口；QQ 文字版应引导 /join。"""
    app, store = make_app()
    messages = run(app.handle_event(group_event("/startgame")))
    text = texts(messages)
    assert "房间已创建" in text
    assert "/join" in text
    room = store.rooms["g1"]
    assert room.phase == GamePhase.LOBBY
    assert room.rules.mode == GameMode.NORMAL


def test_startgame_duplicate_event_id_replays_cached_result():
    """QQ 重推同一事件号时必须幂等（官方 Telegram 不会重复触发命令）。"""
    app, store = make_app()
    event = group_event("/startgame", event_id="fixed-start-1")
    first = run(app.handle_event(event))
    second = run(app.handle_event(event))
    assert [m.text for m in second] == [m.text for m in first]
    assert len(store.commits) == 1
    assert len(store.rooms) == 1


def test_startgame_in_private_rejected_and_guides_group_slash_command():
    """官方 StartFromGroup/GroupCommandOnly：私聊不能建局。"""
    app, _ = make_app()
    messages = run(app.handle_event(c2c_event("/startgame")))
    assert "请在群里发送 /startgame" in texts(messages)


def test_startchaos_creates_chaos_mode_room():
    """GameCommands.cs:32-42 /startchaos 使用 Chaos 模式。"""
    app, store = make_app()
    messages = run(app.handle_event(group_event("/startchaos")))
    assert "房间已创建" in texts(messages)
    assert store.rooms["g1"].rules.mode == GameMode.CHAOS


def test_startgame_during_lobby_reprompts_join_instead_of_error():
    app, _ = make_app()
    run(app.handle_event(group_event("/startgame", user="u1")))
    messages = run(app.handle_event(group_event("/startgame", user="u2")))
    text = texts(messages)
    assert "操作未执行" not in text
    assert "/join" in text


def test_startgame_when_user_active_in_other_group_rejected():
    """Commands/Helpers.cs:126-135 AlreadyInGame。"""
    other = lobby_room(session_id="g-other", count=3)
    app, _ = make_app(rooms=[other])
    messages = run(app.handle_event(group_event("/startgame", user="u1", group="g1")))
    assert "你已在其他群的活动房间中" in texts(messages)


# ---------------------------------------------------------------------------
# /join（GameCommands.cs:44-74 + Werewolf.cs:685-824 AddPlayer）
# ---------------------------------------------------------------------------

def test_join_without_room_guides_startgame():
    """官方 NoGame 文案引导开局；QQ 必须给出可用的 /startgame。"""
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/join")))
    assert "/startgame" in texts(messages)


def test_join_success_then_repeat_join_is_silent():
    """AddPlayer: 已加入者再次加入被静默忽略（Werewolf.cs:695-699）。"""
    app, store = make_app()
    run(app.handle_event(group_event("/startgame", user="u1")))
    joined = run(app.handle_event(group_event("/join", user="u2", name="甲")))
    assert "加入游戏" in texts(joined)
    assert any(p.user_id == "u2" for p in store.rooms["g1"].players)
    again = run(app.handle_event(group_event("/join", user="u2", name="甲")))
    assert again == []
    assert sum(p.user_id == "u2" for p in store.rooms["g1"].players) == 1


def test_join_with_bot_mention_and_extra_spaces_still_joins():
    """QQ 群 @机器人 时 content 带 <@!openid> 与多余空格，解析必须命中同一流程。"""
    app, store = make_app()
    run(app.handle_event(group_event("/startgame", user="u1")))
    messages = run(app.handle_event(
        group_event("<@!bot-openid-1>   /join   ", user="u2", name="乙")
    ))
    assert "加入游戏" in texts(messages)
    assert any(p.user_id == "u2" for p in store.rooms["g1"].players)


def test_join_during_running_game_is_silent():
    """AddPlayer: !IsJoining 时直接 return（Werewolf.cs:689-693）。"""
    room = phase_room(GamePhase.NIGHT, [Role.SEER, Role.WOLF, Role.VILLAGER])
    app, store = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/join", user="u9", name="丁")))
    assert messages == []
    assert all(p.user_id != "u9" for p in store.rooms["g1"].players)


def test_join_in_private_rejected():
    """GameCommands.cs:50-55 JoinFromGroup：私聊 /join 被拒绝。"""
    room = lobby_room()
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(c2c_event("/join", user="u1")))
    assert "该命令请在群里发送" in texts(messages)


def test_join_when_room_full_rejected():
    """AddPlayer: PlayerLimitReached（Werewolf.cs:731-735）。
    官方拒绝的硬上限是 Settings.MaxPlayers=35；群配置上限只提前结束等待。"""
    room = lobby_room(count=35)
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/join", user="u99", name="满员")))
    assert "房间人数已满" in texts(messages)


# ---------------------------------------------------------------------------
# /forcestart（GameCommands.cs:76-102 + Werewolf.cs:2517 ForceStart）
# ---------------------------------------------------------------------------

def test_forcestart_without_room_guides_startgame():
    app, _ = make_app(admins=("admin",))
    messages = run(app.handle_event(group_event("/forcestart", user="admin")))
    assert "/startgame" in texts(messages)


def test_forcestart_host_without_admin_starts_immediately():
    """本局发起人（房主）不需要管理员权限，/go 当场开局并当场回消息。

    官方走 GroupAdminOnly（UpdateHandler.cs:433-440），本项目按产品需求放宽为
    「发起人自己就能开」；同时不再只把 deadline 拉到当前时刻交给定时器，
    避免开局播报因缺少原始消息号退化成主动推送而被平台拦截。
    """
    room = lobby_room(count=5)
    room.host_user_id = "u1"
    app, store = make_app(rooms=[room], admins=("admin",))
    messages = run(app.handle_event(group_event("/forcestart", user="u1")))
    text = texts(messages)
    assert "游戏开始" in text
    assert store.rooms["g1"].phase == GamePhase.NIGHT
    # 开局播报必须挂在这条 /go 群消息上，玩家侧才看得见。
    assert messages[0].source_event_id is not None


def test_forcestart_non_host_non_admin_rejected():
    """既不是发起人也不是管理员时明确拒绝，并说清谁能开。"""
    room = lobby_room(count=5)
    room.host_user_id = "u1"
    app, store = make_app(rooms=[room], admins=("admin",))
    messages = run(app.handle_event(group_event("/forcestart", user="u2")))
    assert "只有本局发起人" in texts(messages)
    assert store.rooms["g1"].phase == GamePhase.LOBBY


def test_forcestart_admin_starts_immediately_without_timer():
    """管理员是兜底通道：即便不是发起人也能当场开局。"""
    room = lobby_room(count=5)
    room.host_user_id = "u1"
    app, store = make_app(rooms=[room], admins=("admin",))
    messages = run(app.handle_event(group_event("/forcestart", user="admin")))
    assert "游戏开始" in texts(messages)
    assert store.rooms["g1"].phase == GamePhase.NIGHT
    # 已经开局，定时器循环不应再重复处理这间房。
    assert run(app.process_due_rooms()) == 0


def test_forcestart_with_too_few_players_reports_shortage():
    """官方等待超时后人数不足直接取消；这里是主动开局，改为当场说明差几人，
    房间保留在等待阶段，玩家可以继续 /join。"""
    room = lobby_room(count=3)
    room.host_user_id = "u1"
    app, store = make_app(rooms=[room], admins=("admin",))
    messages = run(app.handle_event(group_event("/forcestart", user="u1")))
    text = texts(messages)
    assert "人数不足" in text and "5" in text
    assert store.rooms["g1"].phase == GamePhase.LOBBY


# ---------------------------------------------------------------------------
# /players（GameCommands.cs:104-119）
# ---------------------------------------------------------------------------

def test_players_reports_roster_in_group():
    room = lobby_room(count=5)
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/players", user="u1")))
    text = texts(messages)
    assert "当前阶段" in text
    assert "玩家u1" in text


def test_players_without_room_guides_startgame():
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/players")))
    assert "/startgame" in texts(messages)


# ---------------------------------------------------------------------------
# /flee（GameCommands.cs:121-151 + Werewolf.cs:829-874 RemovePlayer）
# ---------------------------------------------------------------------------

def test_flee_lobby_removes_player_and_reseats():
    room = lobby_room(count=5)
    app, store = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/flee", user="u2")))
    # Flee 是官方随机三选一的文案（Werewolf.cs:848），只断言玩家名与剩余人数播报。
    assert "玩家u2" in texts(messages)
    assert "还有 4 人。" in texts(messages)
    seats = [(p.user_id, p.seat) for p in store.rooms["g1"].players]
    assert ("u2", 2) not in dict(seats).items()
    assert [seat for _, seat in seats] == [1, 2, 3, 4]


def test_flee_when_not_seated_is_silent():
    """官方：本群有局但玩家不在局中时无任何输出（GameCommands.cs:135-146）。"""
    room = lobby_room(count=5)
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/flee", user="u9")))
    assert messages == []


def test_flee_without_room_guides_startgame():
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/flee", user="u1")))
    assert "/startgame" in texts(messages)


def test_flee_running_game_kills_player():
    room = phase_room(GamePhase.NIGHT, [Role.SEER, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    app, store = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/flee", user="u3")))
    assert texts(messages).splitlines()[0].startswith("玩家u3")
    fled = next(p for p in store.rooms["g1"].players if p.user_id == "u3")
    assert not fled.alive and fled.metadata.get("died_by_flee_or_idle")


def test_flee_dead_player_rejected():
    """RemovePlayer: DeadFlee（Werewolf.cs:842-846）。"""
    room = phase_room(GamePhase.NIGHT, [Role.SEER, Role.WOLF, Role.VILLAGER])
    room.players[2].alive = False
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/flee", user="u3")))
    assert "死人怎么会逃跑呢" in texts(messages)


def test_flee_lobby_with_flee_disabled_still_removes_player():
    room = lobby_room(count=5, allow_flee=False)
    app, store = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/flee", user="u2")))
    assert all(p.user_id != "u2" for p in store.rooms["g1"].players)
    assert messages != []


def test_flee_disabled_running_game_announces_flee_disabled():
    room = phase_room(
        GamePhase.NIGHT,
        [Role.SEER, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        allow_flee=False,
    )
    app, store = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/flee", user="u3")))
    assert messages != []  # 官方会公告 FleeDisabled
    assert next(p for p in store.rooms["g1"].players if p.user_id == "u3").alive


# ---------------------------------------------------------------------------
# /extend（GameCommands.cs:153-187 + Werewolf.cs:2521-2536 ExtendTime）
# ---------------------------------------------------------------------------

def test_extend_success_clamped_then_repeat_rejected():
    """官方按 MaxExtend 截断；同一玩家只可延长一次（CantExtend）。"""
    room = lobby_room(count=5, allow_extend=True, max_extend=60)
    app, _ = make_app(rooms=[room])
    first = run(app.handle_event(group_event("/extend 999", user="u1")))
    assert "延长 60 秒" in texts(first)
    second = run(app.handle_event(group_event("/extend 10", user="u1")))
    assert "已经延长过一次" in texts(second)


def test_extend_by_non_player_rejected():
    """GameCommands.cs:165-166 NotPlaying。"""
    room = lobby_room(count=5, allow_extend=True)
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/extend 30", user="u9")))
    assert "你不在当前房间" in texts(messages)


def test_extend_negative_by_player_requires_admin():
    """GameCommands.cs:170-171 GroupAdminOnly：负数缩短仅管理员。"""
    room = lobby_room(count=5, allow_extend=True)
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/extend -10", user="u1")))
    assert "只有管理员" in texts(messages)


def test_extend_without_allow_extend_flag_rejected_for_players():
    """GameCommands.cs:176-183：群未开启 AllowExtend 时普通玩家不可延长。"""
    room = lobby_room(count=5, allow_extend=False)
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/extend 30", user="u1")))
    assert "本群未开启玩家延长等待时间" in texts(messages)


def test_extend_non_numeric_argument_defaults_to_30_seconds():
    room = lobby_room(count=5, allow_extend=True)
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/extend abc", user="u1")))
    assert "30 秒" in texts(messages)


def test_extend_by_admin_not_seated_is_noop():
    room = lobby_room(count=5, allow_extend=False)
    from datetime import datetime, timedelta, timezone

    deadline = datetime.now(timezone.utc) + timedelta(seconds=90)
    room.stage_deadline = deadline
    app, store = make_app(rooms=[room], admins=("admin",))
    messages = run(app.handle_event(group_event("/extend 30", user="admin")))
    assert messages == []
    assert store.rooms["g1"].stage_deadline == deadline


# ---------------------------------------------------------------------------
# /nextgame、/stopwaiting（GeneralCommands.cs:390-433、GameCommands.cs:189-218）
# ---------------------------------------------------------------------------

def test_nextgame_subscribes_wait_list_instead_of_creating_room():
    app, store = make_app()
    run(app.handle_event(group_event("/nextgame", user="u1")))
    assert "g1" not in store.rooms  # 官方不会因 /nextgame 创建对局


def test_stopwaiting_confirms_in_private_chat():
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/stopwaiting", user="u1")))
    assert messages, "官方会发送 DeletedFromWaitList 确认"
    assert all(
        m.target.session_type in {SessionType.C2C, SessionType.DIRECT}
        and m.target.session_id == "u1"
        for m in messages
    )


# ---------------------------------------------------------------------------
# 白天投票 /vote、/abstain（Werewolf.cs:4950-4966 SendLynchMenu + HandleReply）
# ---------------------------------------------------------------------------

def vote_room(**rule_kwargs):
    return phase_room(
        GamePhase.VOTE,
        [Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.WOLF, Role.SEER],
        **rule_kwargs,
    )


def test_vote_success_and_no_revote():
    """官方投票后 CurrentQuestion 清空，重复点击被忽略、票不改变（HandleReply:930-933）。"""
    room = vote_room()
    app, store = make_app(rooms=[room])
    first = run(app.handle_event(group_event("/vote 2", user="u1")))
    assert "投票已记录" in texts(first)
    target_id = store.rooms["g1"].votes["u1"]
    second = run(app.handle_event(group_event("/vote 3", user="u1")))
    assert "已经投过票" in texts(second)
    assert store.rooms["g1"].votes["u1"] == target_id  # 票未被改动，与官方一致


def test_vote_invalid_seat_prompts_seat_usage():
    room = vote_room()
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/vote 99", user="u1")))
    assert "找不到目标座位" in texts(messages)


def test_vote_without_target_does_not_consume_the_vote():
    room = vote_room()
    app, store = make_app(rooms=[room])
    run(app.handle_event(group_event("/vote", user="u1")))
    # 官方期望：没有选择目标不算投票，玩家仍可继续投票。
    messages = run(app.handle_event(group_event("/vote 2", user="u1")))
    assert "投票已记录" in texts(messages)
    assert store.rooms["g1"].votes["u1"] == "u2"


def test_vote_self_rejected_like_official_menu():
    """SendLynchMenu 的候选列表排除自己（x.Id != player.Id）。"""
    room = vote_room()
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/vote 1", user="u1")))
    assert "不能选择自己" in texts(messages)


def test_vote_by_dead_player_rejected():
    """官方只给存活玩家发投票菜单。"""
    room = vote_room()
    room.players[0].alive = False
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/vote 2", user="u1")))
    assert "出局玩家不能投票" in texts(messages)


def test_vote_wrong_phase_rejected():
    room = phase_room(GamePhase.NIGHT, [Role.SEER, Role.WOLF, Role.VILLAGER])
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/vote 2", user="u3")))
    assert "现在不是投票阶段" in texts(messages)


def test_abstain_and_skip_record_non_vote():
    """QQ 文字化弃票入口（官方处决无弃票按钮，不投票按闲置计；等价性已在总表 P-008 裁定）。

    注意：中文“弃票/跳过”与 `/vote abstain` 可用；纯 `/abstain` 不在别名表内，
    会退回“未识别命令 + 指令帮助”（见下一个测试），属提示层差异，记入展示差异。
    """
    room = vote_room()
    app, store = make_app(rooms=[room])
    first = run(app.handle_event(group_event("弃票", user="u1")))
    assert "投票已记录" in texts(first)
    assert store.rooms["g1"].votes["u1"] is None
    second = run(app.handle_event(group_event("跳过", user="u2")))
    assert "投票已记录" in texts(second)
    assert store.rooms["g1"].votes["u2"] is None
    third = run(app.handle_event(group_event("/vote abstain", user="u3")))
    assert "投票已记录" in texts(third)
    assert store.rooms["g1"].votes["u3"] is None


def test_bare_abstain_slash_command_falls_back_to_help():
    """记录现状：`/abstain` 未映射，回未识别命令帮助（引导可用斜杠指令，不算玩法差异）。"""
    room = vote_room()
    app, store = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/abstain", user="u1")))
    text = texts(messages)
    assert "未识别命令" in text and "/startgame" in text
    assert "u1" not in store.rooms["g1"].votes


def test_change_vote_rejected_like_official():
    """官方无改票回调；Python 明确拒绝改票。"""
    room = vote_room()
    app, _ = make_app(rooms=[room])
    run(app.handle_event(group_event("/vote 2", user="u1")))
    messages = run(app.handle_event(group_event("改票 3", user="u1")))
    assert "不可改票" in texts(messages)


# ---------------------------------------------------------------------------
# 夜间私聊行动（Werewolf.cs HandleReply / SendMenu 的文字化入口）
# ---------------------------------------------------------------------------

def night_room(**rule_kwargs):
    return phase_room(
        GamePhase.NIGHT,
        [Role.SEER, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        **rule_kwargs,
    )


def test_night_seer_private_action_success_and_repeat_rejected():
    """官方回调选择后 CurrentQuestion 清空、再点无效且不改结果。"""
    room = night_room()
    app, store = make_app(rooms=[room])
    first = run(app.handle_event(c2c_event("/查验 3", user="u1")))
    assert "行动已记录" in texts(first)
    assert store.rooms["g1"].night_actions["u1"].target_id == "u3"
    second = run(app.handle_event(c2c_event("/查验 4", user="u1")))
    assert "该夜间行动已经提交" in texts(second)
    assert store.rooms["g1"].night_actions["u1"].target_id == "u3"  # 结果未变


def test_night_action_in_group_redirected_to_private():
    """官方夜间行动只在 PM 菜单中出现，群里不可提交。"""
    room = night_room()
    app, store = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/查验 3", user="u1")))
    assert "私聊" in texts(messages)
    assert "u1" not in store.rooms["g1"].night_actions


def test_night_action_wrong_phase_rejected():
    room = phase_room(GamePhase.DAY, [Role.SEER, Role.WOLF, Role.VILLAGER])
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(c2c_event("/查验 2", user="u1")))
    assert "现在不是夜晚行动阶段" in texts(messages)


def test_night_action_without_room_rejected():
    app, _ = make_app()
    messages = run(app.handle_event(c2c_event("/查验 2", user="u1")))
    assert "/startgame" in texts(messages)


def test_night_action_without_target_opens_text_menu_and_confirms():
    """官方按钮菜单的文字化等价流程：列目标 → 回座位号 → 确认。"""
    room = night_room()
    app, store = make_app(rooms=[room])
    prompt = run(app.handle_event(c2c_event("/查验", user="u1")))
    assert "请选择第 1/1 个目标" in texts(prompt)
    picked = run(app.handle_event(c2c_event("3", user="u1")))
    assert "已选择 3号" in texts(picked)
    confirmed = run(app.handle_event(c2c_event("确认", user="u1")))
    assert "行动已记录" in texts(confirmed)
    assert store.rooms["g1"].night_actions["u1"].target_id == "u3"


def test_night_menu_rejects_invalid_seat_and_reprompts():
    """官方按钮不存在非法目标；文字入口必须拦截非法座位并重新给出列表。"""
    room = night_room()
    app, store = make_app(rooms=[room])
    run(app.handle_event(c2c_event("/查验", user="u1")))
    messages = run(app.handle_event(c2c_event("99", user="u1")))
    text = texts(messages)
    assert "这个座位不能选" in text
    assert "请选择第 1/1 个目标" in text
    assert "u1" not in store.rooms["g1"].night_actions


def test_wolf_cannot_target_wolf_via_command():
    """HandleReply/SendMenu：狼人菜单不含狼人目标。"""
    room = phase_room(GamePhase.NIGHT, [Role.WOLF, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.SEER])
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(c2c_event("/狼人 2", user="u1")))
    assert "狼人不能袭击狼人" in texts(messages)


# ---------------------------------------------------------------------------
# 白天私聊能力（HandleReply: SpreadSilver/Sandman/Mayor/Shoot 等的文字化入口）
# ---------------------------------------------------------------------------

def test_day_shoot_success_and_repeat_rejected():
    room = phase_room(GamePhase.DAY, [Role.GUNNER, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.SEER])
    room.players[0].bullet_count = 2
    app, store = make_app(rooms=[room])
    first = run(app.handle_event(c2c_event("/开枪 2", user="u1")))
    assert "开枪目标已记录" in texts(first)
    second = run(app.handle_event(c2c_event("/开枪 3", user="u1")))
    assert "该白天行动已经提交" in texts(second)
    assert store.rooms["g1"].day_actions["u1"].target_id == "u2"


def test_day_ability_in_group_redirected_to_private():
    room = phase_room(GamePhase.DAY, [Role.GUNNER, Role.WOLF, Role.VILLAGER])
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(group_event("/开枪 2", user="u1")))
    assert "私聊" in texts(messages)


def test_mayor_confirm_only_flow_via_text():
    """官方市长 reveal 按钮 → 文字“确认”。"""
    room = phase_room(GamePhase.DAY, [Role.MAYOR, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.SEER])
    app, store = make_app(rooms=[room])
    prompt = run(app.handle_event(c2c_event("/mayor", user="u1")))
    assert "发送“确认”使用" in texts(prompt)
    confirmed = run(app.handle_event(c2c_event("确认", user="u1")))
    assert "公开市长身份" in texts(confirmed)
    mayor = next(p for p in store.rooms["g1"].players if p.user_id == "u1")
    assert mayor.has_revealed and mayor.vote_weight == 2


# ---------------------------------------------------------------------------
# 未识别输入与身份查询
# ---------------------------------------------------------------------------

def test_unknown_group_text_returns_command_help():
    """QQ @机器人 的乱输入必须引导到可用斜杠指令（官方 Telegram 直接忽略非命令）。"""
    app, _ = make_app()
    messages = run(app.handle_event(group_event("随便说点什么", user="u1")))
    text = texts(messages)
    assert "未识别命令" in text
    assert "/startgame" in text


def test_mention_only_message_returns_help():
    app, _ = make_app()
    messages = run(app.handle_event(group_event("<@!bot-openid-1>", user="u1")))
    assert "/startgame" in texts(messages)


def test_identity_query_in_private_returns_role():
    room = night_room()
    app, _ = make_app(rooms=[room])
    messages = run(app.handle_event(c2c_event("/身份", user="u1")))
    # 官方私聊身份提示用本地化角色名（如「👳 先知」），不是枚举英文值。
    assert f"你的身份是【{role_display_name(Role.SEER)}】" in texts(messages)


def test_parse_command_tolerates_repeated_spaces_and_mentions():
    assert parse_command("<@!bot-1>   /join").name == "join"
    assert parse_command("  /vote    3  ").argument == "3"
    assert parse_command("/extend   45").argument == "45"
    assert parse_command("<@!bot-1> <@!bot-2> /players").name == "status"


# ---------------------------------------------------------------------------
# /myidles、/stats（GeneralCommands.cs:505-597 + StatsController）
# ---------------------------------------------------------------------------

class HistoryStore(AuditStore):
    """带跨局历史查询的存储替身，对应 werewolf.sql 存储过程与 StatsController。"""

    def __init__(self, rooms=(), idles=(0, 0), player=None, group=None):
        super().__init__(rooms)
        self.idles = idles
        self.player = player
        self.group = group

    async def count_idle_kills_24h(self, _user_id, group_id=None):
        return self.idles[1] if group_id is not None else self.idles[0]

    async def player_history_stats(self, _user_id):
        return self.player

    async def group_history_stats(self, _group_id):
        return self.group


def test_myidles_in_group_sends_pm_and_group_notice():
    """GeneralCommands.cs:566-597：结果发私聊，群里只回 SentPrivate；群内追加 GroupIdleCount。"""
    store = HistoryStore(idles=(5, 2))
    app = GameApplication(store)
    messages = run(app.handle_event(group_event("/myidles", user="u1", name="甲")))
    private = [m for m in messages if m.target.session_type != SessionType.GROUP]
    public = [m for m in messages if m.target.session_type == SessionType.GROUP]
    assert len(private) == 1 and len(public) == 1
    assert "5 次" in private[0].text and "其中 2 次发生在本群" in private[0].text
    assert "私聊" in public[0].text


def test_myidles_in_private_has_no_group_count():
    """私聊触发时官方不查询 GetGroupIdleKills24Hours，也不发 SentPrivate。"""
    store = HistoryStore(idles=(3, 9))
    app = GameApplication(store)
    messages = run(app.handle_event(c2c_event("/myidles", user="u1", name="甲")))
    assert len(messages) == 1
    assert "3 次" in messages[0].text
    assert "本群" not in messages[0].text


def test_stats_in_group_reports_group_and_player_history():
    """StatsController.GroupStats/PlayerStats：群内同时给本群与本人历史统计。"""
    store = HistoryStore(
        group={"games": 42, "best_survivor": ("乙", 73)},
        player={
            "games": 10, "won": 6, "lost": 4, "survived": 5,
            "most_common_role": ("Villager", 4),
            "most_killed": ("丙", 3),
            "most_killed_by": ("丁", 2),
        },
    )
    app = GameApplication(store)
    text = texts(run(app.handle_event(group_event("/stats", user="u1", name="甲"))))
    assert "共进行 42 局" in text and "乙（73%）" in text
    assert "共 10 局" in text and "胜 6 局（60%）" in text and "存活 5 局（50%）" in text
    assert "Villager" in text and "丙" in text and "丁" in text


def test_stats_without_history_is_allowed_in_private():
    """官方 /stats 在私聊同样可用（只是不给群统计），无战绩时不报错。"""
    store = HistoryStore(player=None, group=None)
    app = GameApplication(store)
    text = texts(run(app.handle_event(c2c_event("/stats", user="u1", name="甲"))))
    assert "还没有已结算的对局记录" in text
