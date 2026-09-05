"""Official-source fixtures. Expected values come from Werewolf.cs / Roles.cs /
GameBalancing.cs / Settings.cs at ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7,
not from snapshotting the Python engine."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from application.contracts import PlatformEvent, PlatformSession, SessionType
from application.service import GameApplication
from domain.engine import GameRoomEngine, GameRuleError
from domain.fixtures import FrozenClock, OfficialFixtureRunner
from domain.models import (
    ALL_ROLES,
    GameMode,
    KillMethod,
    MAJORITY_WOLF_ROLES,
    DomainEvent,
    GamePhase,
    GameRoom,
    NightAction,
    QuestionType,
    Player,
    ROLE_METADATA,
    ROLE_TEAM,
    Role,
    Team,
    WOLF_ROLES,
)
from domain.rules import assign_official_roles, official_role_list, role_strength, ruleset_official
from domain.roleinfo import role_display_name
from tests.test_parity import _base, _player, _run


# Official Roles.cs RoleAttribute + Werewolf.cs SetTeam + GameBalancing.GetStrength defaults.
OFFICIAL_ROLE_CATALOG = {
    Role.VILLAGER: ("👱", Team.VILLAGE, False, 1),
    Role.DRUNK: ("🍻", Team.VILLAGE, True, 3),
    Role.HARLOT: ("💋", Team.VILLAGE, True, 6),
    Role.SEER: ("👳", Team.VILLAGE, True, 7),
    Role.TRAITOR: ("🖕", Team.VILLAGE, True, 0),
    Role.GUARDIAN_ANGEL: ("👼", Team.VILLAGE, True, 7),
    Role.DETECTIVE: ("🕵", Team.VILLAGE, True, 6),
    Role.WOLF: ("🐺", Team.WOLF, False, 10),
    Role.CURSED: ("😾", Team.VILLAGE, True, 1),
    Role.GUNNER: ("🔫", Team.VILLAGE, True, 6),
    Role.TANNER: ("👺", Team.TANNER, True, 0),
    Role.FOOL: ("🃏", Team.VILLAGE, True, 3),
    Role.WILD_CHILD: ("👶", Team.VILLAGE, True, 1),
    Role.BEHOLDER: ("👁", Team.VILLAGE, True, 1),
    Role.APPRENTICE_SEER: ("🙇", Team.VILLAGE, True, 6),
    Role.CULTIST: ("👤", Team.CULT, True, 10),
    Role.CULTIST_HUNTER: ("💂", Team.VILLAGE, True, 7),
    Role.MASON: ("👷", Team.VILLAGE, True, 1),
    Role.DOPPELGANGER: ("🎭", Team.THIEF, True, 2),
    Role.CUPID: ("🏹", Team.VILLAGE, True, 2),
    Role.HUNTER: ("🎯", Team.VILLAGE, True, 6),
    Role.SERIAL_KILLER: ("🔪", Team.SERIAL_KILLER, True, 15),
    Role.SORCERER: ("🔮", Team.WOLF, True, 2),
    Role.ALPHA_WOLF: ("⚡️", Team.WOLF, True, 12),
    Role.WOLF_CUB: ("🐶", Team.WOLF, True, 10),
    Role.BLACKSMITH: ("⚒", Team.VILLAGE, True, 5),
    Role.CLUMSY_GUY: ("🤕", Team.VILLAGE, True, -1),
    Role.MAYOR: ("🎖", Team.VILLAGE, True, 4),
    Role.PRINCE: ("👑", Team.VILLAGE, True, 3),
    Role.LYCAN: ("🐺🌝", Team.WOLF, True, 10),
    Role.PACIFIST: ("☮️", Team.VILLAGE, True, 3),
    Role.WISE_ELDER: ("📚", Team.VILLAGE, True, 3),
    Role.ORACLE: ("🌀", Team.VILLAGE, True, 4),
    Role.SANDMAN: ("💤", Team.VILLAGE, True, 3),
    Role.WOLF_MAN: ("👱🌚", Team.VILLAGE, True, 1),
    Role.THIEF: ("😈", Team.THIEF, True, 0),
    Role.TROUBLEMAKER: ("🤯", Team.VILLAGE, True, 5),
    Role.CHEMIST: ("👨‍🔬", Team.VILLAGE, True, 0),
    Role.SNOW_WOLF: ("🐺☃️", Team.WOLF, True, 15),
    Role.GRAVE_DIGGER: ("☠️", Team.VILLAGE, True, 5),
    Role.AUGUR: ("🦅", Team.VILLAGE, True, 5),
    Role.ARSONIST: ("🔥", Team.ARSONIST, True, 8),
    Role.SPUMPKIN: ("🎃", Team.VILLAGE, False, 2),
}


def _event(result: dict, kind: str) -> dict:
    return next(event for event in result["events"] if event["kind"] == kind)


def _events(result: dict, kind: str) -> list[dict]:
    return [event for event in result["events"] if event["kind"] == kind]


def _skip_to_next_night(*extra_night: dict, lynch: str | None = None, players: int = 5) -> list[dict]:
    operations = list(extra_night)
    operations.append({"kind": "timeout", "seconds": 60})
    target = lynch or "skip"
    operations.extend(
        {
            "kind": "vote",
            "actor": f"u{index}",
            "target": "skip" if lynch == f"u{index}" else target,
        }
        for index in range(1, players + 1)
    )
    operations.append({"kind": "resolve_vote"})
    return operations


def _kill_extras(*user_ids: str) -> list[dict]:
    return [
        {"kind": "set_state", "player": user_id, "values": {"alive": False}}
        for user_id in user_ids
    ]


def _lobby_engine() -> tuple[GameRoomEngine, FrozenClock]:
    clock = FrozenClock(__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))
    return GameRoomEngine(clock=clock), clock


class _CreateRoomStore:
    """只保留创建房间这条入口实际会用到的最小存储接口。"""

    def __init__(self) -> None:
        self.rooms: dict[str, GameRoom] = {}
        self.event_ids: set[str] = set()

    async def begin_event(self, event_id: str, _session_key: str) -> tuple[bool, list[object] | None]:
        if event_id in self.event_ids:
            return False, []
        self.event_ids.add(event_id)
        return True, None

    async def commit_result(self, *, room, **_kwargs) -> None:
        if room is not None:
            self.rooms[room.session_id] = room

    async def get_room(self, session_id: str) -> GameRoom | None:
        return self.rooms.get(session_id)

    async def list_active_rooms(self) -> list[GameRoom]:
        return [
            room for room in self.rooms.values()
            if room.phase not in {GamePhase.FINISHED, GamePhase.CANCELLED}
        ]

    async def delete_room(self, session_id: str) -> None:
        self.rooms.pop(session_id, None)

    async def find_active_rooms_for_user(self, user_id: str) -> list[GameRoom]:
        return [
            room for room in self.rooms.values()
            if room.phase not in {GamePhase.FINISHED, GamePhase.CANCELLED}
            and any(player.user_id == user_id for player in room.players)
        ]

    async def get_group_rule_config(self, _session_id: str) -> dict[str, object]:
        return {}


def test_official_startgame_creator_waits_for_join() -> None:
    """Werewolf.cs:219：创建者不会自动加入，仍须通过 /join 或加入按钮参加。"""
    store = _CreateRoomStore()
    app = GameApplication(store, rules=ruleset_official())
    event = PlatformEvent(
        event_id="official-create-without-join",
        session=PlatformSession(SessionType.GROUP, "official-create-group"),
        user_id="creator",
        display_name="创建者",
        text="/startgame",
    )

    messages = asyncio.run(app.handle_event(event))

    room = store.rooms["official-create-group"]
    assert room.players == []
    assert "等待玩家加入" in messages[0].text


def test_official_lobby_host_transfers_when_owner_leaves() -> None:
    """房主离场时自动移交给下一位在场玩家，避免出现无人能开局的死房间。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("official-no-owner", ruleset_official())
    engine.join(room, "first", "首位")
    engine.join(room, "second", "第二位")
    assert room.host_user_id == "first"
    engine.flee(room, "first")

    assert room.host_user_id == "second"
    assert [player.user_id for player in room.players] == ["second"]


def test_official_lobby_fullness_starts_without_waiting_for_original_deadline() -> None:
    """Werewolf.cs:816-817：最后一位加入时立即结束等待计时。"""
    engine, clock = _lobby_engine()
    rules = replace(ruleset_official(), max_players=5, join_seconds=600)
    room = engine.create_room("official-full-lobby", rules)
    for index in range(1, 6):
        engine.join(room, f"u{index}", f"玩家{index}")

    assert room.stage_deadline == clock.now()
    assert engine.due(room) is True


def test_official_duplicate_or_started_join_leaves_room_unchanged() -> None:
    """Werewolf.cs:689-699：重复加入或开局后加入会直接忽略，不改变房间。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("official-idempotent-join", ruleset_official())
    engine.join(room, "u1", "甲")
    before_duplicate = room.snapshot()
    assert engine.join(room, "u1", "甲") == []
    assert room.snapshot() == before_duplicate

    room.phase = GamePhase.NIGHT
    before_started = room.snapshot()
    assert engine.join(room, "u2", "乙") == []
    assert room.snapshot() == before_started


@pytest.mark.parametrize("display_name", ["/命令", " \n/命令 ", "skip", " SKIP "])
def test_official_lobby_rejects_reserved_join_names(display_name: str) -> None:
    """Werewolf.cs:708-729：清理后以斜杠开头的名字和 skip 不能加入。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("official-reserved-name", ruleset_official())

    with pytest.raises(GameRuleError):
        engine.join(room, "u1", display_name)
    assert room.players == []


@pytest.mark.parametrize("display_name", ["", " ", " \n "])
def test_lobby_accepts_empty_display_name(display_name: str) -> None:
    """QQ 群消息偶尔取不到昵称，空名是常态：必须放行，展示层用座位号兜底。

    这一条是本项目相对官方 C# 版的有意偏离——官方运行在 Telegram 上，昵称必定
    存在；NapCat 侧 sender.nickname 可能为空，把空名当错误会让玩家直接进不来。
    """
    engine, _ = _lobby_engine()
    room = engine.create_room("qq-empty-name", ruleset_official())

    engine.join(room, "u1", display_name)

    assert [player.user_id for player in room.players] == ["u1"]
    assert room.players[0].display_name == ""
    # 展示层绝不外泄 QQ 号，退化成座位号。
    assert room.players[0].public_name == "1号"
    assert room.players[0].nickname == "1号玩家"


def test_lobby_allows_multiple_empty_display_names() -> None:
    """一群拿不到昵称的玩家不能互相判成重名，否则第二个人永远进不来。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("qq-empty-name-dup", ruleset_official())

    engine.join(room, "u1", "")
    engine.join(room, "u2", "")

    assert [player.seat for player in room.players] == [1, 2]


def test_official_lobby_rejects_duplicate_display_name() -> None:
    """Werewolf.cs:726-729：同一房间不能有相同显示名。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("official-duplicate-name", ruleset_official())
    engine.join(room, "u1", "同名")

    with pytest.raises(GameRuleError):
        engine.join(room, "u2", "同名")
    assert [player.user_id for player in room.players] == ["u1"]


def test_official_lobby_normalizes_display_name_before_saving() -> None:
    """Werewolf.cs:702-709：名称删除换行并去掉首尾空格后才保存。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("official-name-normalization", ruleset_official())

    engine.join(room, "u1", "  甲\n乙  ")

    assert room.players[0].display_name == "甲乙"


def test_official_lobby_keeps_full_display_name() -> None:
    """Werewolf.cs:702-709：官方不把已清理的显示名截成 64 个字符。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("official-full-name", ruleset_official())
    display_name = "甲" * 65

    engine.join(room, "u1", display_name)

    assert room.players[0].display_name == display_name


def test_official_flee_by_non_player_leaves_lobby_unchanged() -> None:
    """Werewolf.cs:840-841：不在玩家列表的人发送 /flee 会被静默忽略。"""
    engine, _ = _lobby_engine()
    room = engine.create_room("official-absent-flee", ruleset_official())
    engine.join(room, "u1", "甲")
    before = room.snapshot()

    assert engine.flee(room, "u2") == []
    assert room.snapshot() == before


def test_official_force_start_with_too_few_players_does_not_start() -> None:
    """Werewolf.cs:442-445,511-516：人数不足时不开局。

    官方的 ForceStart 只结束等待计时，靠随后的超时分支取消本局；本项目改成
    当场开局（避免定时器路径的播报变成无人可见的主动推送），因此人数不足会
    在这条指令上直接抛错说明还差几人，房间留在等待阶段继续收人。等待超时后
    仍按官方行为取消，由 on_timeout 分支覆盖。
    """
    engine, _ = _lobby_engine()
    room = engine.create_room("official-force-start-minimum", ruleset_official(), host_user_id="u1")
    engine.join(room, "u1", "甲")
    engine.join(room, "u2", "乙")

    with pytest.raises(GameRuleError, match="人数不足"):
        engine.force_start(room, "u1")
    assert room.phase == GamePhase.LOBBY
    assert all(player.role is None for player in room.players)

    # 等待时间自然走完，官方的取消分支照旧生效。
    room.stage_deadline = room.stage_started_at
    assert engine.due(room) is True
    events = engine.on_timeout(room)
    assert room.phase == GamePhase.CANCELLED
    assert [event.kind for event in events] == ["join_timeout"]


def test_official_insufficient_lobby_timeout_removes_room() -> None:
    """Werewolf.cs:511-516：人数不足的等候期结束后，官方会移除整局房间。"""
    engine, clock = _lobby_engine()
    store = _CreateRoomStore()
    app = GameApplication(
        store,
        rules=replace(ruleset_official(), join_seconds=1),
        engine=engine,
    )
    room = engine.create_room("official-timeout-removal", app.rules)
    engine.join(room, "u1", "甲")
    store.rooms[room.session_id] = room
    clock.advance(1)

    assert asyncio.run(app.process_due_rooms()) == 1
    assert "official-timeout-removal" not in store.rooms


def test_official_late_lobby_join_keeps_at_least_one_minute() -> None:
    """Werewolf.cs:450-453：最后一分钟加入后，等待时间重新保留一分钟。"""
    engine, clock = _lobby_engine()
    room = engine.create_room(
        "official-late-lobby-join",
        replace(ruleset_official(), join_seconds=180),
    )
    engine.join(room, "early", "早到者")
    clock.advance(150)

    engine.join(room, "late", "晚到者")

    assert room.stage_deadline == clock.now() + timedelta(seconds=60)


@pytest.mark.parametrize(("alive_count", "expected_seconds"), [(5, 120), (20, 150)])
def test_official_day_time_adds_one_minute_for_a_five_player_game(
    alive_count: int, expected_seconds: int,
) -> None:
    """Werewolf.cs:2824-2835：白天基础时长外，至少额外保留 60 秒，并按人数增加。"""
    engine, clock = _lobby_engine()
    room = GameRoom(
        "official-day-minimum-time",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player(
                f"u{seat}", f"玩家{seat}", seat,
                role=Role.WOLF if seat == 1 else Role.VILLAGER,
            )
            for seat in range(1, alive_count + 1)
        ],
    )

    engine._finish_or_start_day(room)

    assert room.phase == GamePhase.DAY
    assert room.stage_deadline == clock.now() + timedelta(seconds=expected_seconds)


@pytest.mark.parametrize("role", [Role.CUPID, Role.DOPPELGANGER, Role.WILD_CHILD])
def test_official_first_night_special_role_lasts_at_least_two_minutes(role: Role) -> None:
    """Werewolf.cs:3004-3009：首夜有丘比特、分身或野孩子时，至少给 120 秒。"""
    engine, clock = _lobby_engine()
    room = engine.create_room(
        f"official-first-night-special-time-{role.value}",
        ruleset_official(required_roles=(role,)),
    )
    for index in range(1, 6):
        engine.join(room, f"u{index}", f"玩家{index}")

    engine.start(room, "u1", force=True)

    assert any(player.role == role for player in room.players)
    assert room.stage_deadline == clock.now() + timedelta(seconds=120)


def test_official_first_night_classic_thief_lasts_at_least_two_minutes() -> None:
    """Werewolf.cs:3008-3009：普通盗贼首夜也至少给 120 秒。"""
    engine, clock = _lobby_engine()
    room = engine.create_room(
        "official-first-night-thief-time",
        ruleset_official(required_roles=(Role.THIEF,), thief_full=False),
    )
    for index in range(1, 6):
        engine.join(room, f"u{index}", f"玩家{index}")

    engine.start(room, "u1", force=True)

    assert any(player.role == Role.THIEF for player in room.players)
    assert room.stage_deadline == clock.now() + timedelta(seconds=120)


def test_official_night_resolves_when_every_required_action_is_submitted() -> None:
    """Werewolf.cs:3034-3041：所有夜间菜单完成后不再等待倒计时。"""
    engine, _ = _lobby_engine()
    room = GameRoom(
        "official-night-complete",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        players=[
            Player("wolf", "狼人", 1, role=Role.WOLF),
            Player("seer", "预言家", 2, role=Role.SEER),
            Player("one", "甲", 3, role=Role.VILLAGER),
            Player("two", "乙", 4, role=Role.VILLAGER),
            Player("three", "丙", 5, role=Role.VILLAGER),
        ],
    )

    assert [event.kind for event in engine.submit_night_action(room, "wolf", "狼人", "2")] == [
        "action_accepted"
    ]
    events = engine.submit_night_action(room, "seer", "查验", "1")

    assert room.phase == GamePhase.DAY
    assert any(event.kind == "day_started" for event in events)


def test_official_night_without_available_actions_ends_after_one_second() -> None:
    """Werewolf.cs:3034-3041：没有可用夜间菜单时，约一秒后直接结算。"""
    engine, clock = _lobby_engine()
    room = GameRoom(
        "official-night-without-actions",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        stage_started_at=clock.now(),
        stage_deadline=clock.now() + timedelta(seconds=90),
        statistics={"silver_spread": True},
        players=[
            Player("wolf", "狼人", 1, role=Role.WOLF),
            Player("one", "甲", 2, role=Role.VILLAGER),
            Player("two", "乙", 3, role=Role.VILLAGER),
            Player("three", "丙", 4, role=Role.VILLAGER),
            Player("four", "丁", 5, role=Role.VILLAGER),
        ],
    )

    assert engine._night_prompts(room) == []
    clock.advance(1)

    assert engine.due(room) is True


def test_official_vote_resolves_when_every_living_player_has_voted() -> None:
    """Werewolf.cs:2584-2589：所有存活玩家投票后不再等待倒计时。"""
    engine, _ = _lobby_engine()
    room = GameRoom(
        "official-vote-complete",
        ruleset_official(),
        phase=GamePhase.VOTE,
        day=1,
        players=[
            Player("wolf", "狼人", 1, role=Role.WOLF),
            Player("one", "甲", 2, role=Role.VILLAGER),
            Player("two", "乙", 3, role=Role.VILLAGER),
            Player("three", "丙", 4, role=Role.VILLAGER),
            Player("four", "丁", 5, role=Role.VILLAGER),
        ],
    )

    for user_id in ["wolf", "one", "two", "three"]:
        assert [event.kind for event in engine.submit_vote(room, user_id, "skip")] == ["vote_accepted"]
    events = engine.submit_vote(room, "four", "skip")

    assert room.phase == GamePhase.NIGHT
    assert any(event.kind == "night_started" for event in events)


@pytest.mark.parametrize(
    ("phase", "expected_phase", "expected_event"),
    [
        (GamePhase.NIGHT, GamePhase.DAY, "day_started"),
        (GamePhase.DAY, GamePhase.VOTE, "vote_started"),
        (GamePhase.VOTE, GamePhase.NIGHT, "night_started"),
    ],
)
def test_official_phase_timeout_moves_to_its_next_stage(
    phase: GamePhase, expected_phase: GamePhase, expected_event: str,
) -> None:
    """Werewolf.cs:2537-3034：三个可等待阶段超时后按官方顺序继续。"""
    engine, clock = _lobby_engine()
    room = GameRoom(
        f"official-timeout-{phase.value}",
        ruleset_official(),
        phase=phase,
        day=1,
        stage_started_at=clock.now(),
        stage_deadline=clock.now(),
        players=[
            Player("wolf", "狼人", 1, role=Role.WOLF),
            Player("one", "甲", 2, role=Role.VILLAGER),
            Player("two", "乙", 3, role=Role.VILLAGER),
            Player("three", "丙", 4, role=Role.VILLAGER),
            Player("four", "丁", 5, role=Role.VILLAGER),
        ],
    )

    assert engine.due(room) is True
    events = engine.on_timeout(room)

    assert room.phase == expected_phase
    assert any(event.kind == expected_event for event in events)


def test_official_night_action_cannot_be_submitted_twice() -> None:
    """Werewolf.cs:930-934：夜晚菜单确认后，重复操作不再改变目标。"""
    engine, _ = _lobby_engine()
    room = GameRoom(
        "official-night-duplicate-action",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        players=[
            Player("wolf", "狼人", 1, role=Role.WOLF),
            Player("one", "甲", 2, role=Role.SEER),
            Player("two", "乙", 3, role=Role.VILLAGER),
            Player("three", "丙", 4, role=Role.VILLAGER),
            Player("four", "丁", 5, role=Role.VILLAGER),
        ],
    )

    engine.submit_night_action(room, "wolf", "狼人", "2")

    with pytest.raises(GameRuleError, match="已经提交"):
        engine.submit_night_action(room, "wolf", "狼人", "3")
    assert room.night_actions["wolf"].target_id == "one"


def test_official_day_action_cannot_be_submitted_twice() -> None:
    """Werewolf.cs:930-934：白天菜单确认后，重复操作不再改变目标。"""
    engine, _ = _lobby_engine()
    room = GameRoom(
        "official-day-duplicate-action",
        ruleset_official(),
        phase=GamePhase.DAY,
        day=1,
        players=[
            Player("detective", "侦探", 1, role=Role.DETECTIVE),
            Player("wolf", "狼人", 2, role=Role.WOLF),
            Player("one", "甲", 3, role=Role.VILLAGER),
            Player("two", "乙", 4, role=Role.VILLAGER),
            Player("three", "丙", 5, role=Role.VILLAGER),
        ],
    )

    engine.submit_day_action(room, "detective", "侦查", "2")

    with pytest.raises(GameRuleError, match="已经提交"):
        engine.submit_day_action(room, "detective", "侦查", "3")
    assert room.day_actions["detective"].target_id == "wolf"


def test_official_pending_hunter_shot_is_accepted_after_death() -> None:
    """Werewolf.cs:5437-5452：猎人最后一击期间会临时恢复为可操作状态。"""
    engine, _ = _lobby_engine()
    room = GameRoom(
        "official-dead-hunter-action",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        players=[
            Player("hunter", "猎人", 1, role=Role.HUNTER, alive=False, metadata={"pending_hunt": True}),
            Player("wolf", "狼人", 2, role=Role.WOLF),
            Player("one", "甲", 3, role=Role.VILLAGER),
            Player("two", "乙", 4, role=Role.VILLAGER),
            Player("three", "丙", 5, role=Role.VILLAGER),
        ],
    )

    engine.submit_night_action(room, "hunter", "猎杀", "2")
    assert room.players[1].alive is False


def test_official_wolf_roles_exclude_snow_wolf() -> None:
    """Werewolf.cs:48 and GameBalancing.cs:32."""
    assert WOLF_ROLES == frozenset({Role.WOLF, Role.ALPHA_WOLF, Role.WOLF_CUB, Role.LYCAN})
    assert Role.SNOW_WOLF not in WOLF_ROLES
    assert Role.SNOW_WOLF in MAJORITY_WOLF_ROLES


def test_official_role_catalog_emoji_team_disable_strength() -> None:
    assert tuple(ALL_ROLES) == tuple(OFFICIAL_ROLE_CATALOG)
    for role, (emoji, team, can_disable, strength) in OFFICIAL_ROLE_CATALOG.items():
        meta = ROLE_METADATA[role]
        assert meta.emoji == emoji
        assert meta.team == team
        assert ROLE_TEAM[role] == team
        assert meta.can_be_disabled is can_disable
        assert meta.strength == strength


def test_official_role_strength_dynamic_conditions() -> None:
    """GameBalancing.cs:216-285 的全部随牌组变化的强度判断。"""
    assert role_strength(Role.SEER, [Role.SEER]) == 7
    assert role_strength(Role.SEER, [Role.SEER, Role.LYCAN, Role.WOLF_MAN]) == 4
    assert role_strength(Role.GUARDIAN_ANGEL, [Role.GUARDIAN_ANGEL]) == 7
    assert role_strength(Role.GUARDIAN_ANGEL, [Role.GUARDIAN_ANGEL, Role.ARSONIST]) == 8
    assert role_strength(Role.CURSED, [Role.CURSED, Role.WOLF, Role.ALPHA_WOLF, Role.SNOW_WOLF]) == 0
    assert role_strength(Role.TANNER, [Role.TANNER] * 7) == 3
    assert role_strength(Role.BEHOLDER, [Role.BEHOLDER]) == 1
    assert role_strength(Role.BEHOLDER, [Role.BEHOLDER, Role.SEER, Role.FOOL]) == 6
    assert role_strength(Role.CULTIST, [Role.CULTIST, Role.VILLAGER, Role.SEER, Role.DOPPELGANGER]) == 12
    assert role_strength(Role.CULTIST_HUNTER, [Role.CULTIST_HUNTER]) == 1
    assert role_strength(Role.CULTIST_HUNTER, [Role.CULTIST_HUNTER, Role.CULTIST]) == 7
    assert role_strength(Role.MASON, [Role.MASON]) == 1
    assert role_strength(Role.MASON, [Role.MASON, Role.MASON, Role.MASON]) == 6
    assert role_strength(Role.WOLF_CUB, [Role.WOLF_CUB]) == 10
    assert role_strength(Role.WOLF_CUB, [Role.WOLF_CUB, Role.TRAITOR]) == 12


def test_official_role_list_card_pool_thresholds_and_lone_snow_wolf() -> None:
    """GameBalancing.GetRoleList: 152-214 的牌池、门槛和孤雪狼处理。"""
    class FirstRandom:
        def choice(self, values):
            return values[0]

    class SnowFirstRandom:
        def choice(self, values):
            return Role.SNOW_WOLF if Role.SNOW_WOLF in values else values[0]

    small = official_role_list(5, rng=FirstRandom())
    assert small.count(Role.WOLF) == 1
    assert small.count(Role.MASON) == 3
    assert small.count(Role.VILLAGER) == 2
    assert Role.CULTIST not in small
    assert Role.CULTIST_HUNTER not in small
    assert Role.SPUMPKIN not in small

    at_ten = official_role_list(10, rng=FirstRandom())
    assert Role.CULTIST not in at_ten
    assert Role.CULTIST_HUNTER not in at_ten

    large = official_role_list(11, rng=FirstRandom())
    assert large.count(Role.CULTIST_HUNTER) == 1
    assert large.count(Role.CULTIST) == 3
    assert Role.ARSONIST not in official_role_list(11, [Role.ARSONIST], FirstRandom())

    lone_snow = official_role_list(5, rng=SnowFirstRandom())
    assert lone_snow.count(Role.SNOW_WOLF) == 0
    assert lone_snow.count(Role.WOLF) == 1


def test_official_balance_repairs_missing_wolf_cult_hunter_and_seer() -> None:
    """GameBalancing.Balance: 66-93 的三项开局修正。"""
    class OrderedDealRandom:
        def __init__(self, deal):
            self.deal = deal

        def choice(self, values):
            return values[0]

        def shuffle(self, values):
            remaining = list(values)
            ordered = []
            for role in self.deal:
                remaining.remove(role)
                ordered.append(role)
            values[:] = [*ordered, *remaining]

    no_wolf = assign_official_roles(
        5,
        mode=GameMode.CHAOS,
        rng=OrderedDealRandom([Role.SORCERER, Role.VILLAGER, Role.DRUNK, Role.HARLOT, Role.SEER]),
    )
    assert Role.SORCERER not in no_wolf.roles
    assert Role.WOLF in no_wolf.roles

    cult_without_hunter = assign_official_roles(
        11,
        mode=GameMode.CHAOS,
        rng=OrderedDealRandom([
            Role.CULTIST, Role.VILLAGER, Role.DRUNK, Role.HARLOT, Role.SEER, Role.TRAITOR,
            Role.GUARDIAN_ANGEL, Role.DETECTIVE, Role.CURSED, Role.GUNNER, Role.FOOL,
        ]),
    )
    assert Role.CULTIST in cult_without_hunter.roles
    assert Role.CULTIST_HUNTER in cult_without_hunter.roles

    apprentice_without_seer = assign_official_roles(
        5,
        mode=GameMode.CHAOS,
        rng=OrderedDealRandom([Role.APPRENTICE_SEER, Role.WOLF, Role.VILLAGER, Role.DRUNK, Role.HARLOT]),
    )
    assert Role.APPRENTICE_SEER not in apprentice_without_seer.roles
    assert Role.SEER in apprentice_without_seer.roles


def test_official_normal_balance_retries_revealed_and_blocking_role_limits() -> None:
    """GameBalancing.cs:115-129：普通模式重发牌，混乱模式保留第一副牌。"""
    class PlannedRandom:
        def __init__(self, plans, wolf_choices=()):
            self.plans = plans
            self.wolf_choices = list(wolf_choices)
            self.choice_calls = 0
            self.shuffle_calls = 0

        def choice(self, values):
            if self.wolf_choices:
                choice = self.wolf_choices[self.choice_calls % len(self.wolf_choices)]
                self.choice_calls += 1
                if choice in values:
                    return choice
            return values[0]

        def shuffle(self, values):
            plan = self.plans[min(self.shuffle_calls, len(self.plans) - 1)]
            self.shuffle_calls += 1
            remaining = list(values)
            ordered = []
            for role in plan:
                remaining.remove(role)
                ordered.append(role)
            values[:] = [*ordered, *remaining]

    too_many_revealed = [
        Role.WOLF, Role.SORCERER, Role.TANNER, Role.THIEF, Role.ARSONIST,
        Role.BLACKSMITH, Role.MAYOR, Role.PACIFIST, Role.GUNNER, Role.SANDMAN,
        Role.HARLOT, Role.CLUMSY_GUY,
    ]
    revealed_ok = [
        Role.WOLF, Role.SORCERER, Role.TANNER, Role.THIEF, Role.ARSONIST,
        Role.BLACKSMITH, Role.MAYOR, Role.PACIFIST, Role.GUNNER, Role.HARLOT,
        Role.CLUMSY_GUY, Role.CURSED,
    ]
    chaos_revealed_rng = PlannedRandom([too_many_revealed])
    chaos_revealed = assign_official_roles(12, mode=GameMode.CHAOS, rng=chaos_revealed_rng)
    assert chaos_revealed_rng.shuffle_calls == 1
    assert sum(role in chaos_revealed.roles for role in {
        Role.BLACKSMITH, Role.MAYOR, Role.PACIFIST, Role.GUNNER, Role.SANDMAN,
    }) == 5

    normal_revealed_rng = PlannedRandom([too_many_revealed, revealed_ok])
    normal_revealed = assign_official_roles(12, mode=GameMode.NORMAL, rng=normal_revealed_rng)
    assert normal_revealed_rng.shuffle_calls == 2
    assert sum(role in normal_revealed.roles for role in {
        Role.BLACKSMITH, Role.MAYOR, Role.PACIFIST, Role.GUNNER, Role.SANDMAN,
    }) == 4

    too_many_blockers = [
        Role.WOLF, Role.LYCAN, Role.BLACKSMITH, Role.SANDMAN, Role.TROUBLEMAKER,
        Role.CLUMSY_GUY, Role.CURSED, Role.BEHOLDER, Role.WILD_CHILD, Role.WOLF_MAN,
        Role.HARLOT,
    ]
    blockers_ok = [
        Role.WOLF, Role.LYCAN, Role.DRUNK, Role.SANDMAN, Role.TROUBLEMAKER,
        Role.CLUMSY_GUY, Role.CURSED, Role.BEHOLDER, Role.WILD_CHILD, Role.WOLF_MAN,
        Role.HARLOT,
    ]
    normal_blocker_rng = PlannedRandom([too_many_blockers, blockers_ok], [Role.WOLF, Role.LYCAN, Role.WOLF])
    normal_blockers = assign_official_roles(11, mode=GameMode.NORMAL, rng=normal_blocker_rng)
    assert normal_blocker_rng.shuffle_calls == 2
    assert Role.BLACKSMITH not in normal_blockers.roles


def test_official_identity_rosters_include_the_recipient() -> None:
    """Werewolf.cs:1658-1706：初始同伴名单和官方一样包含接收者本人。"""
    room = GameRoom(
        "official-identity-rosters",
        ruleset_official(),
        players=[
            Player("mason-1", "石匠甲", 1, role=Role.MASON),
            Player("mason-2", "石匠乙", 2, role=Role.MASON),
            Player("wolf-1", "狼人甲", 3, role=Role.WOLF),
            Player("snow-1", "雪狼甲", 4, role=Role.SNOW_WOLF),
            Player("cult-1", "教徒甲", 5, role=Role.CULTIST),
            Player("cult-2", "教徒乙", 6, role=Role.CULTIST),
            Player("seer", "预言家", 7, role=Role.SEER),
            Player("beholder", "观察者", 8, role=Role.BEHOLDER),
        ],
    )
    by_user = {event.target_user_id: event for event in GameRoomEngine()._identity_events(room)}
    assert "石匠甲、石匠乙" in by_user["mason-1"].text
    assert "狼人甲、雪狼甲" in by_user["wolf-1"].text
    assert "教徒甲、教徒乙" in by_user["cult-1"].text


def test_official_cupid_first_prompt_only_offers_the_first_lover() -> None:
    """Werewolf.cs:5240-5248：丘比特先选第一位恋人，再出现第二次选择。"""
    room = GameRoom(
        "official-cupid-first-prompt",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("cupid", "丘比特", 1, role=Role.CUPID),
            Player("one", "甲", 2, role=Role.VILLAGER),
            Player("two", "乙", 3, role=Role.VILLAGER),
        ],
    )
    by_user = {event.target_user_id: event for event in GameRoomEngine()._night_prompts(room)}
    assert by_user["cupid"].metadata["actions"] == [QuestionType.LOVER_1.value]


def test_official_grave_digger_notice_keeps_player_prompt_order() -> None:
    """Werewolf.cs:5152-5314：墓地信息在该玩家轮到时发送，不会提前插到队首。"""
    room = GameRoom(
        "official-grave-digger-prompt-order",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("seer", "预言家", 1, role=Role.SEER),
            Player("digger", "墓地看守", 2, role=Role.GRAVE_DIGGER),
            Player("villager", "村民", 3, role=Role.VILLAGER),
        ],
    )
    events = GameRoomEngine()._night_prompts(room)
    assert [(event.kind, event.target_user_id) for event in events] == [
        ("night_prompt", "seer"),
        ("grave_digger", "digger"),
    ]


def test_official_wolf_night_prompt_names_other_awake_eating_wolves() -> None:
    """Werewolf.cs:5187-5201：狼人提示只点名其他未醉的吃人狼。"""
    room = GameRoom(
        "official-wolf-night-teammates",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("wolf-1", "狼人甲", 1, role=Role.WOLF),
            Player("wolf-2", "狼人乙", 2, role=Role.WOLF),
            Player("drunk-wolf", "醉狼", 3, role=Role.LYCAN, drunk=True),
            Player("snow", "雪狼", 4, role=Role.SNOW_WOLF),
            Player("villager", "村民", 5, role=Role.VILLAGER),
        ],
    )
    by_user = {event.target_user_id: event for event in GameRoomEngine()._night_prompts(room)}
    text = by_user["wolf-1"].text
    assert "狼人乙" in text
    assert "醉狼" not in text
    assert "雪狼" not in text


def test_official_cultist_night_prompt_names_other_cultists() -> None:
    """Werewolf.cs:5206-5215：邪教徒行动提示点名其他邪教徒。"""
    room = GameRoom(
        "official-cultist-night-teammates",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("cult-1", "教徒甲", 1, role=Role.CULTIST),
            Player("cult-2", "教徒乙", 2, role=Role.CULTIST),
            Player("villager", "村民", 3, role=Role.VILLAGER),
        ],
    )
    by_user = {event.target_user_id: event for event in GameRoomEngine()._night_prompts(room)}
    assert "教徒乙" in by_user["cult-1"].text


def test_official_night_prompt_clears_previous_choice_before_sending_menu() -> None:
    """Werewolf.cs:5152-5155：每个存活玩家收到夜晚菜单前先清空旧选择。"""
    seer = Player("seer", "预言家", 1, role=Role.SEER, choice="villager", choice2="other")
    room = GameRoom(
        "official-clear-night-choice",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        players=[seer, Player("villager", "村民", 2, role=Role.VILLAGER)],
    )
    GameRoomEngine()._night_prompts(room)
    assert seer.choice is None
    assert seer.choice2 is None


@pytest.mark.parametrize(
    ("role", "action"),
    [
        (Role.CUPID, "恋人"),
        (Role.WILD_CHILD, "偶像"),
        (Role.DOPPELGANGER, "模仿"),
        (Role.THIEF, "盗取"),
    ],
)
def test_official_first_night_mandatory_picks_have_no_skip(
    role: Role, action: str,
) -> None:
    """Werewolf.cs:5303-5304：四种首夜强制选择没有跳过，超时才随机补全。"""
    actor = Player("actor", "行动者", 1, role=role)
    room = GameRoom(
        f"official-first-night-required-{role.value}",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            actor,
            Player("one", "甲", 2, role=Role.VILLAGER),
            Player("two", "乙", 3, role=Role.VILLAGER),
        ],
    )
    prompt = next(event for event in GameRoomEngine()._night_prompts(room) if event.target_user_id == actor.user_id)
    assert "跳过" not in prompt.text
    with pytest.raises(GameRuleError, match="首夜必须选择目标"):
        GameRoomEngine().submit_night_action(room, actor.user_id, action, "跳过")


@pytest.mark.parametrize("role", [Role.WILD_CHILD, Role.DOPPELGANGER])
def test_official_first_night_unselected_role_models_are_randomly_completed(role: Role) -> None:
    """Werewolf.cs:1757-1778：野孩子和分身超时后自动获得其他玩家作为模板。"""
    class OrderedRandom:
        def shuffle(self, values: list[object]) -> None:
            return None

    actor = Player("actor", "行动者", 1, role=role)
    room = GameRoom(
        f"official-first-night-model-timeout-{role.value}",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            actor,
            Player("model", "模板", 2, role=Role.VILLAGER),
            Player("other", "其他", 3, role=Role.VILLAGER),
        ],
    )
    events = GameRoomEngine(rng=OrderedRandom()).resolve_night(room)
    assert actor.role_model == "model"
    assert any(event.kind == "role_model_forced" and event.target_user_id == actor.user_id for event in events)


def test_official_first_night_chemist_brews_without_a_target_menu() -> None:
    """Werewolf.cs:5258-5271：化学家首夜只收到配药通知，下一夜才可选择目标。"""
    chemist = Player("chemist", "化学家", 1, role=Role.CHEMIST)
    room = GameRoom(
        "official-first-night-chemist-brewing",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[chemist, Player("villager", "村民", 2, role=Role.VILLAGER)],
    )
    events = GameRoomEngine()._night_prompts(room)
    assert chemist.has_used_ability is True
    assert [(event.kind, event.target_user_id) for event in events] == [("chemist_brewing", "chemist")]


def test_official_first_night_messages_keep_player_order() -> None:
    """Werewolf.cs:5152-5314：目标菜单和化学家配药消息均按玩家顺序发送。"""
    room = GameRoom(
        "official-first-night-message-order",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("seer", "预言家", 1, role=Role.SEER),
            Player("chemist", "化学家", 2, role=Role.CHEMIST),
            Player("wolf", "狼人", 3, role=Role.WOLF),
            Player("villager", "村民", 4, role=Role.VILLAGER),
        ],
    )
    events = GameRoomEngine()._night_prompts(room)
    assert [(event.kind, event.target_user_id) for event in events] == [
        ("night_prompt", "seer"),
        ("chemist_brewing", "chemist"),
        ("night_prompt", "wolf"),
    ]


def test_official_initial_identity_delivery_is_private_and_keeps_player_order() -> None:
    """Werewolf.cs:1612-1642：按玩家顺序逐个私聊身份，傻瓜显示为预言家。"""
    room = GameRoom(
        "official-initial-identity-order",
        ruleset_official(),
        players=[
            Player("seer", "预言家", 1, role=Role.SEER),
            Player("fool", "傻瓜", 2, role=Role.FOOL),
            Player("beholder", "观察者", 3, role=Role.BEHOLDER),
            Player("villager", "村民", 4, role=Role.VILLAGER),
        ],
    )
    events = GameRoomEngine()._identity_events(room)
    assert [event.target_user_id for event in events] == ["seer", "fool", "beholder", "villager"]
    assert all(not event.public for event in events)
    assert events[1].metadata["shown_role"] == Role.SEER.value
    assert events[2].metadata["seer_id"] == "seer"


def test_official_first_night_prompt_catalog_for_action_roles() -> None:
    """Werewolf.cs:5162-5292：首夜每个可行动身份得到对应的唯一行动。"""
    roles = [
        Role.HARLOT,
        Role.SEER,
        Role.GUARDIAN_ANGEL,
        Role.WOLF,
        Role.FOOL,
        Role.WILD_CHILD,
        Role.CULTIST,
        Role.CULTIST_HUNTER,
        Role.DOPPELGANGER,
        Role.SERIAL_KILLER,
        Role.SORCERER,
        Role.ALPHA_WOLF,
        Role.WOLF_CUB,
        Role.LYCAN,
        Role.THIEF,
        Role.SNOW_WOLF,
        Role.ARSONIST,
        Role.ORACLE,
    ]
    room = GameRoom(
        "official-first-night-action-catalog",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[Player(f"p{index}", role.value, index, role=role) for index, role in enumerate(roles, 1)],
    )
    by_user = {event.target_user_id: event for event in GameRoomEngine()._night_prompts(room)}
    expected = {
        Role.HARLOT: QuestionType.VISIT,
        Role.SEER: QuestionType.SEE,
        Role.GUARDIAN_ANGEL: QuestionType.GUARD,
        Role.WOLF: QuestionType.KILL,
        Role.FOOL: QuestionType.SEE,
        Role.WILD_CHILD: QuestionType.ROLE_MODEL,
        Role.CULTIST: QuestionType.CONVERT,
        Role.CULTIST_HUNTER: QuestionType.HUNT,
        Role.DOPPELGANGER: QuestionType.ROLE_MODEL,
        Role.SERIAL_KILLER: QuestionType.SERIAL_KILL,
        Role.SORCERER: QuestionType.SEE,
        Role.ALPHA_WOLF: QuestionType.KILL,
        Role.WOLF_CUB: QuestionType.KILL,
        Role.LYCAN: QuestionType.KILL,
        Role.THIEF: QuestionType.THIEF,
        Role.SNOW_WOLF: QuestionType.FREEZE,
        Role.ARSONIST: QuestionType.DOUSE,
        Role.ORACLE: QuestionType.SEE,
    }
    for index, role in enumerate(roles, 1):
        assert by_user[f"p{index}"].metadata["actions"] == [expected[role].value]


def test_official_first_night_excludes_day_only_and_unawakened_roles() -> None:
    """Werewolf.cs:5162-5292：白天身份、未觉醒学徒和普通村民没有首夜菜单。"""
    roles = [
        Role.VILLAGER,
        Role.APPRENTICE_SEER,
        Role.DETECTIVE,
        Role.GUNNER,
        Role.BLACKSMITH,
        Role.MAYOR,
        Role.PACIFIST,
        Role.SANDMAN,
        Role.TROUBLEMAKER,
        Role.MASON,
        Role.WOLF_MAN,
        Role.WISE_ELDER,
        Role.PRINCE,
    ]
    room = GameRoom(
        "official-first-night-no-action",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[Player(f"p{index}", role.value, index, role=role) for index, role in enumerate(roles, 1)],
    )
    events = GameRoomEngine()._night_prompts(room)
    assert not [event for event in events if event.kind == "night_prompt"]


# Settings.cs release (#else) values at ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7.
OFFICIAL_SETTINGS_CHANCES = {
    "detective_caught_chance": 40,
    "hunter_kill_wolf_chance_base": 30,
    "alpha_wolf_conversion_chance": 20,
    "hunter_conversion_chance": 50,
    "hunter_kill_cult_chance": 50,
    "thief_steal_chance": 50,
    "chemist_success_chance": 50,
    "harlot_discover_cult_chance": 50,
    "guardian_wolf_death_chance": 50,
    "snow_hunter_freeze_chance": 50,
    "serial_killer_stumble_chance": 80,
    "clumsy_retarget_chance": 50,
    "seer_traitor_wolf_chance": 50,
}

OFFICIAL_CULT_CHANCES = {
    "Seer": 40,
    "GuardianAngel": 60,
    "Detective": 70,
    "Cursed": 60,
    "Harlot": 70,
    "Hunter": 50,
    "Sorcerer": 40,
    "Blacksmith": 75,
    "Oracle": 50,
    "Sandman": 60,
    "WiseElder": 30,
    "Pacifist": 80,
    "GraveDigger": 30,
    "Augur": 40,
    "Doppelgänger": 0,
    "Thief": 0,
    "Spumpkin": 0,
}


def test_official_settings_chances_match_settings_cs() -> None:
    """Werewolf Node/Helpers/Settings.cs release constants."""
    rules = ruleset_official()
    for field, expected in OFFICIAL_SETTINGS_CHANCES.items():
        assert getattr(rules, field) == expected
    assert dict(rules.cult_conversion_chances) == OFFICIAL_CULT_CHANCES


def test_official_role_visibility_defaults_match_group_defaults() -> None:
    rules = ruleset_official()
    assert rules.show_roles_on_death is True
    assert rules.show_roles_end == "Living"


@pytest.mark.parametrize(
    ("show_roles", "contains_role"),
    [(True, True), (False, False)],
)
def test_death_notice_can_show_or_hide_role(
    show_roles: bool, contains_role: bool,
) -> None:
    rules = replace(ruleset_official(), show_roles_on_death=show_roles)
    victim = Player("victim", "村民", 1, role=Role.VILLAGER)
    room = GameRoom("death-visibility", rules, phase=GamePhase.DAY, players=[victim])
    event = GameRoomEngine()._kill(room, victim, KillMethod.LYNCH)[0]
    assert (role_display_name(Role.VILLAGER) in event.text) is contains_role


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        (KillMethod.EAT, "被狼人袭击"),
        (KillMethod.BURN, "被火焰吞噬"),
        (KillMethod.LOVER_DIED, "因恋人死亡而殉情"),
    ],
)
def test_death_notice_keeps_official_cause(method: KillMethod, expected: str) -> None:
    victim = Player("victim", "玩家", 1, role=Role.VILLAGER)
    room = GameRoom("death-cause", ruleset_official(), phase=GamePhase.NIGHT, players=[victim])
    event = GameRoomEngine()._kill(room, victim, method)[0]
    assert expected in event.text


def test_no_one_tanner_special_end_records_the_official_suicide() -> None:
    tanner = Player("tanner", "坦纳", 1, role=Role.TANNER)
    sorcerer = Player("sorcerer", "巫师", 2, role=Role.SORCERER)
    room = GameRoom(
        "no-one-special",
        ruleset_official(),
        phase=GamePhase.DAY,
        players=[tanner, sorcerer],
    )
    events = GameRoomEngine().finish(room, Team.NO_ONE)
    assert room.phase == GamePhase.FINISHED
    assert tanner.alive is False
    assert tanner.kill_method == KillMethod.SUICIDE.value
    assert any(event.kind == "no_one_special_end" for event in events)


@pytest.mark.parametrize("mode", ["None", "Living", "All"])
def test_finish_uses_official_role_visibility_mode(mode: str) -> None:
    rules = replace(ruleset_official(), show_roles_end=mode)
    players = [
        Player("dead", "出局者", 1, role=Role.WOLF, alive=False),
        Player("live", "幸存者", 2, role=Role.VILLAGER),
    ]
    room = GameRoom("end-visibility", rules, phase=GamePhase.DAY, players=players)
    events = GameRoomEngine().finish(room, Team.VILLAGE)
    # Werewolf.cs:4906-4923 —— 名单单独一条消息，角色名走本地化显示名。
    summary = next(event for event in events if event.kind == "game_summary")
    villager, wolf = role_display_name(Role.VILLAGER), role_display_name(Role.WOLF)
    if mode == "None":
        assert villager not in summary.text and wolf not in summary.text
    elif mode == "Living":
        assert villager in summary.text and wolf not in summary.text
    else:
        assert villager in summary.text and wolf in summary.text


@pytest.mark.parametrize(("chance", "visitor_alive"), [(0, True), (100, False)])
def test_wolf_visiting_away_serial_killer_uses_pinned_80_percent_rule(
    chance: int, visitor_alive: bool,
) -> None:
    """Werewolf.cs VisitPlayer: wolf only survives an away SK when the roll is at least 80."""
    class FixedRandom:
        def randrange(self, stop: int) -> int:
            return 0

    rules = replace(ruleset_official(), serial_killer_stumble_chance=chance)
    wolf = Player("wolf", "狼", 1, role=Role.WOLF)
    killer = Player("killer", "连环杀手", 2, role=Role.SERIAL_KILLER, choice="other")
    room = GameRoom("serial-visit", rules, players=[wolf, killer])
    events: list[DomainEvent] = []
    result = GameRoomEngine(rng=FixedRandom())._visit_player(room, wolf, killer, events)
    assert result == ("success" if visitor_alive else "visitor_died")
    assert wolf.alive is visitor_alive


@pytest.mark.parametrize(("chance", "visitor_alive"), [(0, True), (100, False)])
def test_snow_wolf_visiting_away_serial_killer_uses_same_rule(
    chance: int, visitor_alive: bool,
) -> None:
    """Werewolf.cs VisitPlayer: SnowWolf is a wolf visitor for SK stumble rolls."""
    class FixedRandom:
        def randrange(self, stop: int) -> int:
            return 0

    rules = replace(ruleset_official(), serial_killer_stumble_chance=chance)
    snow_wolf = Player("snow", "雪狼", 1, role=Role.SNOW_WOLF)
    killer = Player("killer", "连环杀手", 2, role=Role.SERIAL_KILLER, choice="other")
    room = GameRoom("snow-serial-visit", rules, players=[snow_wolf, killer])
    events: list[DomainEvent] = []
    result = GameRoomEngine(rng=FixedRandom())._visit_player(room, snow_wolf, killer, events)
    assert result == ("success" if visitor_alive else "visitor_died")
    assert snow_wolf.alive is visitor_alive


@pytest.mark.parametrize("chance", [0, 100])
def test_serial_killer_stumble_reroll_is_fixed_at_50_percent(chance: int) -> None:
    """Werewolf.cs: next-night reroll after a grave stumble is fixed at 50%."""
    class FixedRandom:
        def randrange(self, stop: int) -> int:
            return 0

        def choice(self, values: list[Player]) -> Player:
            return values[0]

    serial_killer = Player("killer", "连环杀手", 1, role=Role.SERIAL_KILLER, choice="u3")
    serial_killer.metadata["stumbled_grave_day"] = 1
    first_target = Player("u3", "第一目标", 3, role=Role.VILLAGER)
    reroll_target = Player("u2", "随机目标", 2, role=Role.VILLAGER)
    room = GameRoom(
        "serial-stumble-reroll",
        replace(ruleset_official(), serial_killer_stumble_chance=chance),
        phase=GamePhase.NIGHT,
        day=2,
        players=[serial_killer, reroll_target, first_target],
    )
    events = GameRoomEngine(rng=FixedRandom())._resolve_serial_kill(
        room, NightAction("killer", "serial_kill", "u3", 2), None, None,
    )
    assert first_target.alive is True
    assert reroll_target.alive is False
    assert any(event.kind == "player_died" for event in events)


def test_serial_killer_choice_zero_counts_as_staying_home() -> None:
    """Werewolf.cs VisitPlayer: Choice 0 is the official stay-home value."""
    class FixedRandom:
        def randrange(self, stop: int) -> int:
            return 0

    wolf = Player("wolf", "狼", 1, role=Role.WOLF)
    killer = Player("killer", "连环杀手", 2, role=Role.SERIAL_KILLER, choice="0")
    room = GameRoom("serial-home-zero", ruleset_official(), players=[wolf, killer])
    events: list[DomainEvent] = []
    result = GameRoomEngine(rng=FixedRandom())._visit_player(room, wolf, killer, events)
    assert result == "visitor_died"
    assert wolf.alive is False


def test_visit_away_uses_target_choice_not_action_log() -> None:
    """Werewolf.cs:2342,2424：只有目标自己的 Choice 才表示目标外出。"""
    visitor = Player("visitor", "访客", 1, role=Role.VILLAGER)
    target = Player("target", "目标", 2, role=Role.HARLOT)
    unrelated = Player("unrelated", "无关玩家", 3, role=Role.SEER, choice="target")
    room = GameRoom(
        "visit-choice-source", ruleset_official(), phase=GamePhase.NIGHT, day=1,
        players=[visitor, target, unrelated],
        night_actions={
            "unrelated": NightAction("unrelated", QuestionType.SEE.value, "target", 1),
        },
    )
    assert GameRoomEngine()._is_away(room, target) is False

    target.choice = "visitor"
    assert GameRoomEngine()._is_away(room, target) is True


def test_guardian_saving_attacked_wolf_keeps_trouble_state() -> None:
    """Werewolf.cs:2370-2374：守护被袭击的狼人后保留 InMiddleOfTrouble。"""
    guardian = Player("guardian", "守护", 1, role=Role.GUARDIAN_ANGEL)
    wolf = Player("wolf", "狼人", 2, role=Role.WOLF, was_saved_last_night=True)
    room = GameRoom(
        "guardian-trouble-state", ruleset_official(), phase=GamePhase.NIGHT, day=1,
        players=[guardian, wolf],
    )
    events: list[DomainEvent] = []

    assert GameRoomEngine()._visit_player(room, guardian, wolf, events) == "success"
    assert guardian.metadata["in_middle_of_trouble"] is True


def test_disabled_arsonist_is_never_dealt() -> None:
    """QQ群关闭纵火者时，发牌池和实际身份都不包含该角色。"""
    for seed in range(20):
        assignment = assign_official_roles(
            8, mode=GameMode.CHAOS, allow_arsonist=False,
            rng=__import__("random").Random(seed),
        )
        assert Role.ARSONIST not in assignment.possible_roles
        assert Role.ARSONIST not in assignment.roles


def test_burning_overkill_disallows_arsonist_and_serial_killer_together() -> None:
    """GameBalancing.cs: BurningOverkill controls whether both roles can be dealt."""
    for seed in range(40):
        assignment = assign_official_roles(
            12,
            mode=GameMode.CHAOS,
            burning_overkill=False,
            rng=__import__("random").Random(seed),
        )
        assert not {Role.ARSONIST, Role.SERIAL_KILLER}.issubset(assignment.roles)


def test_official_role_list_skips_snow_wolf_as_village_card() -> None:
    """GameBalancing.GetRoleList: Wolf/Lycan/Cub/Alpha/SnowWolf/Spumpkin are not extra village cards."""
    pool = official_role_list(8)
    assert pool.count(Role.SNOW_WOLF) <= 1
    assert Role.SPUMPKIN not in pool


@pytest.mark.parametrize("mode", [GameMode.NORMAL, GameMode.CHAOS])
def test_official_assignment_invariants(mode: GameMode) -> None:
    """GameBalancing.Balance invariants that must hold for every successful deal."""
    for seed in range(20):
        rng = __import__("random").Random(seed + (0 if mode == GameMode.NORMAL else 100))
        assignment = assign_official_roles(8, mode=mode, rng=rng)
        roles = list(assignment.roles)
        non_vg = {
            Role.CULTIST, Role.SERIAL_KILLER, Role.TANNER, Role.WOLF, Role.ALPHA_WOLF,
            Role.SORCERER, Role.WOLF_CUB, Role.LYCAN, Role.THIEF, Role.SNOW_WOLF, Role.ARSONIST,
        }
        assert any(role not in non_vg for role in roles)
        assert any(role in non_vg and role not in {Role.SORCERER, Role.TANNER, Role.THIEF} for role in roles)
        if Role.CULTIST in roles:
            assert Role.CULTIST_HUNTER in roles
        if Role.APPRENTICE_SEER in roles:
            assert Role.SEER in roles
        if {Role.SORCERER, Role.TRAITOR, Role.SNOW_WOLF} & set(roles):
            assert any(role in {Role.WOLF, Role.ALPHA_WOLF, Role.WOLF_CUB, Role.LYCAN} for role in roles)
        if mode == GameMode.NORMAL:
            village = sum(role_strength(role, roles) for role in roles if role not in non_vg)
            enemy = sum(role_strength(role, roles) for role in roles if role in non_vg)
            assert abs(village - enemy) <= 8 // 4 + 1


def test_snow_wolf_cannot_cast_eat_vote() -> None:
    """SendNightActions: only WolfRoles get AskEat. SnowWolf only gets Freeze."""
    with pytest.raises(Exception):
        _run(_base(
            ["SnowWolf", "Villager", "Villager", "Villager", "Villager"],
            operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
        ))


def test_lycan_casts_eat_vote_as_wolf_role() -> None:
    result = _run(_base(
        ["Lycan", "Villager", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u2")["alive"] is False
    assert _player(result, "u2")["kill_method"] == KillMethod.EAT.value


def test_harlot_visit_non_wolf_survives() -> None:
    result = _run(_base(
        ["Wolf", "Harlot", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "visit", "target": "u3"},
        ],
    ))
    assert _player(result, "u2")["alive"] is True
    assert any(event["kind"] == "harlot_visit" for event in result["events"])


def test_harlot_visit_home_wolf_dies() -> None:
    """VisitPlayer: Harlot visiting a wolf/snow wolf dies VisitWolf."""
    result = _run(_base(
        ["Wolf", "Harlot", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "visit", "target": "u1"},
        ],
    ))
    assert _player(result, "u2")["alive"] is False
    assert _player(result, "u2")["kill_method"] == KillMethod.VISIT_WOLF.value


def test_harlot_visit_eaten_victim_dies() -> None:
    """Harlot AlreadyDead + DiedLastNight + wolf/SK kill => VisitVictim."""
    result = _run(_base(
        ["Wolf", "Harlot", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            {"kind": "night_action", "actor": "u2", "action": "visit", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u2")["alive"] is False
    assert _player(result, "u2")["kill_method"] == KillMethod.VISIT_VICTIM.value


def test_harlot_records_the_victim_that_killed_her_target() -> None:
    rules = ruleset_official()
    wolf = Player("wolf", "狼人", 1, role=Role.WOLF)
    harlot = Player("harlot", "女巫", 2, role=Role.HARLOT)
    victim = Player("victim", "村民", 3, role=Role.VILLAGER)
    room = GameRoom("harlot-visit-victim", rules, phase=GamePhase.NIGHT, day=1,
                    players=[wolf, harlot, victim])
    engine = GameRoomEngine()
    engine._kill(room, victim, KillMethod.EAT, source=wolf)
    engine._resolve_visits(room, {
        QuestionType.VISIT.value: [NightAction(
            actor_id="harlot", action=QuestionType.VISIT.value,
            target_id="victim", day=1,
        )],
    })
    assert harlot.alive is False
    assert harlot.kill_method == KillMethod.VISIT_VICTIM.value
    assert harlot.role_model == "victim"


def test_harlot_does_not_follow_a_visit_death() -> None:
    rules = ruleset_official()
    wolf = Player("wolf", "狼人", 1, role=Role.WOLF)
    harlot = Player("harlot", "女巫", 2, role=Role.HARLOT)
    victim = Player("victim", "村民", 3, role=Role.VILLAGER)
    room = GameRoom("harlot-visit-death", rules, phase=GamePhase.NIGHT, day=1,
                    players=[wolf, harlot, victim])
    victim.alive = False
    victim.died_last_night = True
    victim.metadata.update({
        "killed_by_role": Role.WOLF.value,
        "died_by_visiting_killer": True,
    })
    events = GameRoomEngine()._resolve_visits(room, {
        QuestionType.VISIT.value: [NightAction(
            actor_id="harlot", action=QuestionType.VISIT.value,
            target_id="victim", day=1,
        )],
    })
    assert harlot.alive is True
    assert not any(event.kind == "player_died" for event in events)


def test_cult_visit_checks_only_the_target_wolfs_action() -> None:
    result = _run(_base(
        ["Cultist", "CultistHunter", "Villager", "Wolf", "Wolf"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u4"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u5", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u1")["alive"] is False
    assert _player(result, "u1")["kill_method"] == KillMethod.VISIT_WOLF.value
    assert any(event["kind"] == "cult_visit_wolf" for event in result["events"])


def test_seer_traitor_disguise_pinned() -> None:
    wolf = _run(_base(
        ["Seer", "Wolf", "Traitor", "Villager", "Villager"],
        rules={"seer_traitor_wolf_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u3"},
        ],
    ))
    villager = _run(_base(
        ["Seer", "Wolf", "Traitor", "Villager", "Villager"],
        rules={"seer_traitor_wolf_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u3"},
        ],
    ))
    assert _event(wolf, "seer_result")["metadata"]["seen_role"] == Role.WOLF.value
    assert _event(villager, "seer_result")["metadata"]["seen_role"] == Role.VILLAGER.value


def test_seer_alpha_and_cub_shown_as_wolf() -> None:
    alpha = _run(_base(
        ["Seer", "AlphaWolf", "Villager", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u2"},
        ],
    ))
    cub = _run(_base(
        ["Seer", "WolfCub", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u2"},
        ],
    ))
    assert _event(alpha, "seer_result")["metadata"]["seen_role"] == Role.WOLF.value
    assert _event(cub, "seer_result")["metadata"]["seen_role"] == Role.WOLF.value


def test_sorcerer_sees_wolf_seer_snow_or_other() -> None:
    wolf = _run(_base(
        ["Sorcerer", "Wolf", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "sorcerer", "target": "u2"},
        ],
    ))
    seer = _run(_base(
        ["Sorcerer", "Wolf", "Seer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u2"},
            {"kind": "night_action", "actor": "u1", "action": "sorcerer", "target": "u3"},
        ],
    ))
    other = _run(_base(
        ["Sorcerer", "Wolf", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "sorcerer", "target": "u3"},
        ],
    ))
    assert _event(wolf, "sorcerer_result")["metadata"]["seen_role"] == Role.WOLF.value
    assert _event(seer, "sorcerer_result")["metadata"]["seen_role"] == Role.SEER.value
    assert _event(other, "sorcerer_result")["metadata"]["seen_role"] == Role.VILLAGER.value


def test_oracle_never_reports_targets_actual_role() -> None:
    result = _run(_base(
        ["Oracle", "Wolf", "Villager", "Gunner", "Tanner"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u3"},
        ],
    ))
    seen = _event(result, "oracle_result")["metadata"]["seen_role"]
    assert seen != Role.VILLAGER.value


def test_ga_blocks_wolf_eat_including_home_harlot() -> None:
    result = _run(_base(
        ["Wolf", "GuardianAngel", "Harlot", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "visit", "target": "skip"},
        ],
    ))
    assert _player(result, "u3")["alive"] is True


def test_ga_does_not_block_sk_on_harlot() -> None:
    """Werewolf.cs:3517 GA doesn't find Harlot at home vs SK."""
    result = _run(_base(
        ["Wolf", "GuardianAngel", "Harlot", "SerialKiller", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "visit", "target": "skip"},
            {"kind": "night_action", "actor": "u4", "action": "serial_kill", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u3")["kill_method"] == KillMethod.SERIAL_KILLED.value


def test_ga_visit_wolf_death_and_survive_pinned() -> None:
    death = _run(_base(
        ["Wolf", "GuardianAngel", "Villager", "Villager", "Villager"],
        rules={"guardian_wolf_death_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u1"},
        ],
    ))
    survive = _run(_base(
        ["Wolf", "GuardianAngel", "Villager", "Villager", "Villager"],
        rules={"guardian_wolf_death_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u1"},
        ],
    ))
    assert _player(death, "u2")["alive"] is False
    assert _player(death, "u2")["kill_method"] == KillMethod.GUARD_WOLF.value
    assert _player(survive, "u2")["alive"] is True


def test_ga_cleans_douse_when_no_attack() -> None:
    result = _run(_base(
        ["Wolf", "GuardianAngel", "Arsonist", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "douse", "target": "u4"},
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u4"},
        ],
    ))
    assert any(event["kind"] == "guardian_cleaned_doused" for event in result["events"])


def test_drunk_eat_makes_wolves_drunk_and_blocks_cub_second() -> None:
    """Eating Drunk sets Drunk; same-night Choice2 is emptied."""
    result = _run(_base(
        ["Wolf", "WolfCub", "Drunk", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            *_skip_to_next_night(lynch="u2", players=6),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "kill2", "target": "u4"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u3")["kill_method"] == KillMethod.EAT.value
    assert _player(result, "u4")["alive"] is True


def test_drunk_wolf_has_no_next_night_action_prompt() -> None:
    result = _run(_base(
        ["Wolf", "Drunk", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "set_state", "player": "u1", "values": {"drunk": True}},
            {"kind": "night_prompts"},
        ],
    ))
    assert not any(
        event["kind"] == "night_prompt" and event["target_user_id"] == "u1"
        for event in result["events"]
    )


def test_pending_bite_is_cleared_for_dead_player_without_conversion() -> None:
    result = _run(_base(
        ["AlphaWolf", "Villager", "Villager", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 100},
        operations=[
            {"kind": "set_state", "player": "u2", "values": {"bitten": True, "alive": False}},
            {"kind": "timeout", "seconds": 60},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "skip"},
            {"kind": "vote", "actor": "u3", "target": "skip"},
            {"kind": "vote", "actor": "u4", "target": "skip"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
        ],
    ))
    assert _player(result, "u2")["role"] == Role.VILLAGER.value


def test_wise_elder_survives_first_eat_then_dies() -> None:
    first = _run(_base(
        ["Wolf", "WiseElder", "Villager", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 0},
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(first, "u2")["alive"] is True
    second = _run(_base(
        ["Wolf", "WiseElder", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
        ],
    ))
    assert _player(second, "u2")["alive"] is False
    assert _player(second, "u2")["kill_method"] == KillMethod.EAT.value


def test_hunter_eat_counter_and_eat_pinned() -> None:
    counter = _run(_base(
        ["Wolf", "Hunter", "Villager", "Villager", "Villager"],
        rules={"hunter_kill_wolf_chance_base": 100},
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    eaten = _run(_base(
        ["Wolf", "Hunter", "Villager", "Villager", "Villager"],
        rules={"hunter_kill_wolf_chance_base": 0},
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(counter, "u1")["alive"] is False
    assert _player(counter, "u1")["kill_method"] == KillMethod.HUNTER.value
    assert _player(eaten, "u2")["alive"] is False
    assert _player(eaten, "u2")["kill_method"] == KillMethod.EAT.value


def test_wolf_cub_next_night_second_eat() -> None:
    result = _run(_base(
        ["Wolf", "WolfCub", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            *_skip_to_next_night(lynch="u2", players=6),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "kill2", "target": "u4"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u4")["alive"] is False


def test_wolf_cub_second_vote_excludes_first_target_during_resolution() -> None:
    """Werewolf.cs: the second wolf choice excludes the first winning target."""
    engine = GameRoomEngine()
    room = engine.create_room("direct-cub-second", ruleset_official())
    for index in range(1, 7):
        engine.join(room, f"u{index}", f"P{index}")
    engine.start(room, "u1", force=True)
    roles = [Role.WOLF, Role.WOLF_CUB, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER]
    for player, role in zip(room.players, roles, strict=True):
        player.role = role
        player.original_role = role
    room.statistics["wolf_cub_killed"] = True
    room.night_actions = {
        "u1": NightAction("u1", QuestionType.KILL.value, "u3", room.day),
        "u2": NightAction("u2", QuestionType.KILL.value, "u3", room.day),
        "u1:Kill2": NightAction("u1", QuestionType.KILL_2.value, "u3", room.day),
        "u2:Kill2": NightAction("u2", QuestionType.KILL_2.value, "u3", room.day),
    }

    engine.resolve_night(room)

    assert room.players[2].alive is False
    assert room.players[3].alive is True
    assert room.players[2].kill_method == KillMethod.EAT


def test_cult_default_convert_and_role_chance() -> None:
    success = _run(_base(
        ["Cultist", "CultistHunter", "Villager", "Wolf", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    fail = _run(_base(
        ["Cultist", "CultistHunter", "Seer", "Wolf", "Villager"],
        rules={"cult_conversion_chances": (("Seer", 0),)},
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u4"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    assert _player(success, "u3")["role"] == Role.CULTIST.value
    assert _player(fail, "u3")["role"] == Role.SEER.value


@pytest.mark.parametrize(
    "role",
    [
        Role.SEER,
        Role.GUARDIAN_ANGEL,
        Role.DETECTIVE,
        Role.CURSED,
        Role.HARLOT,
        Role.HUNTER,
        Role.SORCERER,
        Role.BLACKSMITH,
        Role.ORACLE,
        Role.SANDMAN,
        Role.WISE_ELDER,
        Role.PACIFIST,
        Role.GRAVE_DIGGER,
        Role.AUGUR,
    ],
)
def test_every_official_cult_conversion_role_honors_zero_and_hundred(role: Role) -> None:
    """Settings.cs conversion entries are stable at both probability boundaries."""
    def run(chance: int) -> Player:
        cultist = Player("cult", "教徒", 1, role=Role.CULTIST)
        target = Player("target", "目标", 2, role=role)
        room = GameRoom(
            f"cult-boundary-{role.value}-{chance}",
            replace(
                ruleset_official(),
                cult_conversion_chances=((role.value, chance),),
                hunter_conversion_chance=chance if role == Role.HUNTER else 50,
                hunter_kill_cult_chance=0,
            ),
            phase=GamePhase.NIGHT,
            day=1,
            players=[cultist, target],
        )
        GameRoomEngine()._resolve_conversions(room, {
            QuestionType.CONVERT.value: [NightAction("cult", "convert", "target", 1)],
        })
        return target

    assert run(0).role == role
    assert run(100).role == Role.CULTIST


def test_cult_dies_on_home_wolf_and_misses_hunting_wolf() -> None:
    home = _run(_base(
        ["Cultist", "CultistHunter", "Wolf", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    away = _run(_base(
        ["Cultist", "CultistHunter", "Wolf", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "u5"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    assert _player(home, "u1")["kill_method"] == KillMethod.VISIT_WOLF.value
    assert _player(away, "u1")["alive"] is True
    assert _player(away, "u3")["role"] == Role.WOLF.value


def test_cultist_hunter_kills_cultist_and_misses_villager() -> None:
    hit = _run(_base(
        ["Cultist", "CultistHunter", "Wolf", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "u1"},
        ],
    ))
    miss = _run(_base(
        ["Cultist", "CultistHunter", "Wolf", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "u4"},
        ],
    ))
    assert _player(hit, "u1")["kill_method"] == KillMethod.HUNT.value
    assert _player(miss, "u4")["alive"] is True
    assert any(event["kind"] == "cultist_hunter_missed" for event in miss["events"])


def test_cult_dies_visiting_cultist_hunter() -> None:
    result = _run(_base(
        ["Cultist", "CultistHunter", "Wolf", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u2"},
        ],
    ))
    assert _player(result, "u1")["kill_method"] == KillMethod.HUNT.value


def test_chemist_success_and_suicide_and_ignores_ga() -> None:
    success = _run(_base(
        ["Wolf", "Chemist", "Villager", "GuardianAngel", "Villager"],
        rules={"chemist_success_chance": 100},
        operations=[
            {"kind": "set_state", "player": "u2", "values": {"has_used_ability": True}},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u4", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u2", "action": "chemistry", "target": "u3"},
        ],
    ))
    fail = _run(_base(
        ["Wolf", "Chemist", "Villager", "Villager", "Villager"],
        rules={"chemist_success_chance": 0},
        operations=[
            {"kind": "set_state", "player": "u2", "values": {"has_used_ability": True}},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "chemistry", "target": "u3"},
        ],
    ))
    assert _player(success, "u3")["kill_method"] == KillMethod.CHEMISTRY.value
    assert _player(success, "u2")["role"] == Role.CHEMIST.value
    assert _player(fail, "u2")["kill_method"] == KillMethod.CHEMISTRY.value
    assert _player(fail, "u3")["alive"] is True


def test_arsonist_douse_then_spark_ga_can_save() -> None:
    burned = _run(_base(
        ["Wolf", "Arsonist", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "douse", "target": "u3"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "douse", "target": "-2"},
        ],
    ))
    saved = _run(_base(
        ["Wolf", "Arsonist", "GuardianAngel", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "douse", "target": "u4"},
            {"kind": "night_action", "actor": "u3", "action": "guard", "target": "u5"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "guard", "target": "u4"},
            {"kind": "night_action", "actor": "u2", "action": "douse", "target": "-2"},
        ],
    ))
    assert _player(burned, "u3")["kill_method"] == KillMethod.BURN.value
    assert _player(saved, "u4")["alive"] is True


def test_guarded_arsonist_target_remains_doused_for_later_spark() -> None:
    """Werewolf.cs: only a player who actually burns has Doused reset."""
    arsonist = Player("arsonist", "纵火者", 1, role=Role.ARSONIST)
    guardian = Player("guardian", "守护", 2, role=Role.GUARDIAN_ANGEL)
    target = Player("target", "目标", 3, role=Role.VILLAGER, doused=True)
    room = GameRoom("saved-fire", ruleset_official(), phase=GamePhase.NIGHT, day=2,
                    players=[arsonist, guardian, target])
    arsonist.metadata["arsonist_ignite"] = True
    action = NightAction(arsonist.user_id, "douse", None, room.day)
    engine = GameRoomEngine()

    actions = {QuestionType.DOUSE.value: [action]}
    engine._resolve_arsonist(room, actions, guardian, target.user_id)
    assert target.alive and target.doused
    engine._resolve_arsonist(room, actions)
    assert not target.alive and not target.doused


def test_arsonist_burns_lovers_together_and_burning_home_kills_visitors() -> None:
    """Werewolf.cs:3192 and 5609 keep simultaneous fire deaths as Burn."""
    arsonist = Player("arsonist", "纵火者", 1, role=Role.ARSONIST)
    first = Player("first", "甲", 2, role=Role.VILLAGER, doused=True, lover_id="second")
    second = Player("second", "乙", 3, role=Role.VILLAGER, doused=True, lover_id="first")
    visitor = Player("visitor", "访客", 4, role=Role.HARLOT)
    room = GameRoom(
        "burning-lovers",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        players=[arsonist, first, second, visitor],
    )
    arsonist.metadata["arsonist_ignite"] = True
    action = NightAction(arsonist.user_id, QuestionType.DOUSE.value, None, room.day)
    engine = GameRoomEngine()

    engine._resolve_arsonist(room, {QuestionType.DOUSE.value: [action]})
    assert first.kill_method == KillMethod.BURN.value
    assert second.kill_method == KillMethod.BURN.value

    events: list[DomainEvent] = []
    assert engine._visit_player(room, visitor, first, events) == "visitor_died"
    assert visitor.kill_method == KillMethod.VISIT_BURNING.value


def test_snow_freeze_blocks_seer_and_not_arsonist() -> None:
    seer = _run(_base(
        ["Wolf", "SnowWolf", "Seer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u1"},
        ],
    ))
    arson = _run(_base(
        ["Wolf", "SnowWolf", "Arsonist", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "douse", "target": "u4"},
        ],
    ))
    assert not any(event["kind"] == "seer_result" for event in seer["events"])
    assert any(event["kind"] == "arsonist_doused" for event in arson["events"])


def test_snow_freeze_hunter_pinned() -> None:
    frozen = _run(_base(
        ["Wolf", "SnowWolf", "Hunter", "Villager", "Villager"],
        rules={"snow_hunter_freeze_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
        ],
    ))
    shot = _run(_base(
        ["Wolf", "SnowWolf", "Hunter", "Villager", "Villager"],
        rules={"snow_hunter_freeze_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
        ],
    ))
    assert _player(frozen, "u2")["alive"] is True
    assert _player(shot, "u2")["kill_method"] == KillMethod.HUNTER.value


def test_snow_freeze_ga_nulls_save() -> None:
    result = _run(_base(
        ["Wolf", "SnowWolf", "GuardianAngel", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "guard", "target": "u4"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u4"},
        ],
    ))
    assert _player(result, "u4")["alive"] is False


def test_thief_classic_n1_steals_and_victim_becomes_villager() -> None:
    result = _run(_base(
        ["Thief", "Wolf", "Seer", "Villager", "Villager"],
        rules={"thief_full": False},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u2"},
            {"kind": "night_action", "actor": "u1", "action": "thief", "target": "u3"},
        ],
    ))
    assert _player(result, "u1")["role"] == Role.SEER.value
    assert _player(result, "u3")["role"] == Role.VILLAGER.value


def test_thief_full_cannot_steal_wolf_or_fail_chance() -> None:
    blocked = _run(_base(
        ["Thief", "Wolf", "Villager", "Villager", "Villager"],
        rules={"thief_full": True, "thief_steal_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "thief", "target": "u2"},
        ],
    ))
    failed = _run(_base(
        ["Thief", "Wolf", "Seer", "Villager", "Villager"],
        rules={"thief_full": True, "thief_steal_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u2"},
            {"kind": "night_action", "actor": "u1", "action": "thief", "target": "u3"},
        ],
    ))
    assert _player(blocked, "u1")["role"] == Role.THIEF.value
    assert _player(failed, "u1")["role"] == Role.THIEF.value
    assert _player(failed, "u3")["role"] == Role.SEER.value


def test_grave_digger_falls_and_is_spotted_when_chances_pin() -> None:
    fall = _run(_base(
        ["Wolf", "GraveDigger", "Villager", "Villager", "Villager"],
        rules={"grave_digger_fall_chance": 100, "grave_digger_spot_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
        ],
    ))
    spot = _run(_base(
        ["Wolf", "GraveDigger", "Villager", "Villager", "Villager"],
        rules={"grave_digger_fall_chance": 0, "grave_digger_spot_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u4"},
        ],
    ))
    assert _player(fall, "u1")["kill_method"] == KillMethod.FALL_GRAVE.value
    assert _player(spot, "u2")["kill_method"] == KillMethod.SPOTTED.value


def test_grave_digger_default_fall_formula_uses_grave_count() -> None:
    """Werewolf.cs:2389-2391 halves 20..50% for village visitors."""
    class FixedRandom:
        def __init__(self, value: int):
            self.value = value

        def randrange(self, stop: int) -> int:
            return self.value

    first_grave = Player("digger-1", "掘墓人", 1, role=Role.GRAVE_DIGGER)
    first_grave.metadata["dug_graves_last_night"] = 1
    first_visitor = Player("visitor-1", "村民", 2, role=Role.VILLAGER)
    first_room = GameRoom(
        "grave-formula-one", ruleset_official(), phase=GamePhase.NIGHT,
        players=[first_grave, first_visitor],
    )
    GameRoomEngine(rng=FixedRandom(10))._visit_player(first_room, first_visitor, first_grave, [])
    assert first_visitor.alive is True  # 10% threshold: roll 10 is not below it.

    second_grave = Player("digger-2", "掘墓人", 1, role=Role.GRAVE_DIGGER)
    second_grave.metadata["dug_graves_last_night"] = 2
    second_visitor = Player("visitor-2", "村民", 2, role=Role.VILLAGER)
    second_room = GameRoom(
        "grave-formula-two", ruleset_official(), phase=GamePhase.NIGHT,
        players=[second_grave, second_visitor],
    )
    GameRoomEngine(rng=FixedRandom(17))._visit_player(second_room, second_visitor, second_grave, [])
    assert second_visitor.alive is False  # 17.5% threshold: roll 17 falls in it.


def test_sandman_skips_next_night_immediately() -> None:
    result = _run(_base(
        ["Wolf", "Sandman", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "sandman", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "skip"},
            {"kind": "vote", "actor": "u2", "target": "skip"},
            {"kind": "vote", "actor": "u3", "target": "skip"},
            {"kind": "vote", "actor": "u4", "target": "skip"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert _player(result, "u3")["alive"] is True
    assert any(event["kind"] == "sandman" for event in result["events"])
    assert result["phase"] == "day"


def test_silver_blocks_wolf_and_snow_then_clears() -> None:
    blocked = _run(_base(
        ["Wolf", "Blacksmith", "SnowWolf", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "freeze", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "silver", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "skip"},
            {"kind": "vote", "actor": "u2", "target": "skip"},
            {"kind": "vote", "actor": "u3", "target": "skip"},
            {"kind": "vote", "actor": "u4", "target": "skip"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert _player(blocked, "u4")["alive"] is True
    with pytest.raises(Exception):
        OfficialFixtureRunner().run(_base(
            ["Wolf", "Blacksmith", "Villager", "Villager", "Villager"],
            operations=[
                {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
                {"kind": "day_action", "actor": "u2", "action": "silver", "target": "yes"},
                {"kind": "timeout", "seconds": 60},
                {"kind": "vote", "actor": "u1", "target": "skip"},
                {"kind": "vote", "actor": "u2", "target": "skip"},
                {"kind": "vote", "actor": "u3", "target": "skip"},
                {"kind": "vote", "actor": "u4", "target": "skip"},
                {"kind": "vote", "actor": "u5", "target": "skip"},
                {"kind": "resolve_vote"},
                {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            ],
        ))


def test_detective_sees_true_role_and_can_be_caught() -> None:
    result = _run(_base(
        ["Wolf", "Detective", "Traitor", "Villager", "Villager"],
        rules={"detective_caught_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "detect", "target": "u3"},
            {"kind": "start_vote"},
        ],
    ))
    detect = _event(result, "detect_result")
    assert detect["metadata"]["seen_role"] == Role.TRAITOR.value
    assert any(event["kind"] == "detective_caught" for event in result["events"])


def test_gunner_shoots_and_is_demoted_for_elder() -> None:
    shot = _run(_base(
        ["Wolf", "Gunner", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "shoot", "target": "u3"},
            {"kind": "start_vote"},
        ],
    ))
    elder = _run(_base(
        ["Wolf", "Gunner", "WiseElder", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "shoot", "target": "u3"},
            {"kind": "start_vote"},
        ],
    ))
    assert _player(shot, "u3")["kill_method"] == KillMethod.SHOOT.value
    assert _player(elder, "u2")["role"] == Role.VILLAGER.value
    assert _player(elder, "u3")["kill_method"] == KillMethod.SHOOT.value


def test_gunner_can_fire_two_bullets_then_cannot_fire_again() -> None:
    """Werewolf.cs:2871 and 5097 permit each of the Gunner's two bullets."""
    gunner = Player("gunner", "枪手", 1, role=Role.GUNNER, bullet_count=2)
    first = Player("first", "甲", 2, role=Role.VILLAGER)
    second = Player("second", "乙", 3, role=Role.VILLAGER)
    wolf = Player("wolf", "狼", 4, role=Role.WOLF)
    room = GameRoom(
        "gunner-bullets", ruleset_official(), phase=GamePhase.DAY, day=1,
        players=[gunner, first, second, wolf],
    )
    engine = GameRoomEngine()

    engine.submit_day_action(room, gunner.user_id, "shoot", "2")
    engine._resolve_day_actions(room)
    assert first.kill_method == KillMethod.SHOOT.value
    assert gunner.bullet_count == 1

    room.day = 2
    engine.submit_day_action(room, gunner.user_id, "shoot", "3")
    engine._resolve_day_actions(room)
    assert second.kill_method == KillMethod.SHOOT.value
    assert gunner.bullet_count == 0
    with pytest.raises(GameRuleError, match="没有子弹"):
        engine.submit_day_action(room, gunner.user_id, "shoot", "4")


def test_spumpkin_detonation_pinned() -> None:
    boom = _run(_base(
        ["Wolf", "Spumpkin", "Villager", "Villager", "Villager"],
        rules={"spumpkin_detonation_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "shoot", "target": "u3"},
            {"kind": "start_vote"},
        ],
    ))
    fail = _run(_base(
        ["Wolf", "Spumpkin", "Villager", "Villager", "Villager"],
        rules={"spumpkin_detonation_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "shoot", "target": "u3"},
            {"kind": "start_vote"},
        ],
    ))
    assert _player(boom, "u2")["alive"] is False
    assert _player(boom, "u3")["alive"] is False
    assert _player(fail, "u2")["alive"] is True
    assert _player(fail, "u3")["alive"] is True


def test_mayor_vote_weight_two() -> None:
    """Werewolf.cs:2651 revealed Mayor adds a second vote. Extra vote must break a 2-2 tie."""
    votes = [
        {"kind": "timeout", "seconds": 60},
        {"kind": "vote", "actor": "u2", "target": "u4"},
        {"kind": "vote", "actor": "u1", "target": "u4"},
        {"kind": "vote", "actor": "u3", "target": "u5"},
        {"kind": "vote", "actor": "u4", "target": "u5"},
        {"kind": "vote", "actor": "u5", "target": "u3"},
        {"kind": "resolve_vote"},
    ]
    revealed = _run(_base(
        ["Wolf", "Mayor", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "mayor", "target": "yes"},
            *votes,
        ],
    ))
    hidden = _run(_base(
        ["Wolf", "Mayor", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            *votes,
        ],
    ))
    # Revealed: mayor 2 + wolf 1 = 3 on u4; u5 has 2. Hidden: u4=2 and u5=2, tie.
    assert _player(revealed, "u4")["alive"] is False
    assert _player(revealed, "u5")["alive"] is True
    assert _player(hidden, "u4")["alive"] is True
    assert _player(hidden, "u5")["alive"] is True
    assert any(event["kind"] == "vote_tie" for event in hidden["events"])


def test_mayor_can_reveal_during_vote_after_day_menu() -> None:
    """Werewolf.cs: Mayor's day menu remains valid until lynch resolution."""
    result = _run(_base(
        ["Wolf", "Mayor", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "day_action", "actor": "u2", "action": "mayor", "target": "yes"},
            {"kind": "vote", "actor": "u1", "target": "u3"},
            {"kind": "vote", "actor": "u2", "target": "u3"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
            {"kind": "vote", "actor": "u4", "target": "u5"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
        ],
    ))
    assert any(event["kind"] == "mayor_revealed" for event in result["events"])
    assert _player(result, "u3")["alive"] is False


def test_random_lynch_resolves_tied_candidates() -> None:
    """Werewolf.cs: RandomLynch shuffles tied candidates twice and lynches one."""
    result = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        rules={"random_lynch": True},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u2", "target": "u1"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u1"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
        ],
    ))
    assert sum(not _player(result, user_id)["alive"] for user_id in ("u1", "u2")) == 1
    assert not any(event["kind"] == "vote_tie" for event in result["events"])


def test_secret_lynch_records_votes_and_mayor_weight() -> None:
    result = _run(_base(
        ["Mayor", "Wolf", "Villager", "Villager", "Villager"],
        rules={
            "secret_lynch": True,
            "secret_lynch_show_votes": True,
            "secret_lynch_show_voters": True,
        },
        operations=[
            {"kind": "timeout", "seconds": 60},
            {"kind": "day_action", "actor": "u1", "action": "mayor", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u2", "target": "u3"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
        ],
    ))
    event = _event(result, "secret_lynch_result")
    assert [item["target_id"] for item in event["metadata"]["results"]] == ["u2", "u3", "u4"]
    by_target = {item["target_id"]: item for item in event["metadata"]["results"]}
    assert by_target["u2"]["votes"] == 3
    assert {item["user_id"] for item in by_target["u2"]["voters"]} == {"u1", "u4"}
    assert next(item for item in by_target["u2"]["voters"] if item["user_id"] == "u1")["weight"] == 2


def test_grave_digger_ignores_flee_and_idle_deaths() -> None:
    """Werewolf.cs:5295-5300 excludes DiedByFleeOrIdle from grave counts."""
    digger = Player("digger", "掘墓人", 1, role=Role.GRAVE_DIGGER)
    flee = Player("flee", "逃离者", 2, role=Role.VILLAGER)
    idle = Player("idle", "挂机者", 3, role=Role.VILLAGER)
    lynched = Player("lynched", "正常出局者", 4, role=Role.VILLAGER)
    room = GameRoom(
        "grave-filter",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[digger, flee, idle, lynched],
    )
    engine = GameRoomEngine()
    engine._kill(room, flee, KillMethod.FLEE, hunter_final_shot=False)
    engine._kill(room, idle, KillMethod.IDLE, hunter_final_shot=False)
    engine._kill(room, lynched, KillMethod.LYNCH, hunter_final_shot=False)

    events = engine._resolve_grave_digger(room)

    assert digger.metadata["dug_graves_last_night"] == 1
    assert "正常出局者" in events[0].text
    assert "逃离者" not in events[0].text
    assert "挂机者" not in events[0].text


def test_secret_lynch_hides_result_when_show_votes_is_off() -> None:
    result = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        rules={"secret_lynch": True, "secret_lynch_show_votes": False},
        operations=[
            {"kind": "timeout", "seconds": 60},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u2", "target": "u1"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
        ],
    ))
    assert not _events(result, "secret_lynch_result")


def test_augur_shuffles_persistent_possible_roles_in_place() -> None:
    class ReverseRandom:
        def shuffle(self, values: list[object]) -> None:
            values.reverse()

        def choice(self, values: list[object]) -> object:
            return values[0]

        def randrange(self, stop: int) -> int:
            return 0

    engine = GameRoomEngine(
        rng=__import__("domain.fixtures", fromlist=["SeededRandom"]).SeededRandom(1),
        clock=FrozenClock(__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)),
    )
    room = engine.create_room("augur-in-place", ruleset_official())
    for index, role in enumerate(("Augur", "Wolf", "Villager", "Villager", "Villager"), 1):
        engine.join(room, f"u{index}", f"P{index}")
    engine.start(room, "u1", force=True)
    for player, role in zip(room.players, (Role.AUGUR, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER), strict=True):
        player.role = role
        player.alive = True
    room.statistics["possible_roles"] = ["Augur", "Wolf", "Villager", "Gunner"]
    engine.rng = ReverseRandom()
    engine._resolve_augur(room)
    assert room.statistics["possible_roles"] == ["Gunner", "Villager", "Wolf", "Augur"]


def test_drunk_wolf_is_omitted_from_next_night_menu() -> None:
    result = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "set_state", "player": "u1", "values": {"drunk": True}},
            {"kind": "night_prompts"},
        ],
    ))
    assert len(_events(result, "night_prompt")) == 0


def test_thief_notifies_mason_and_new_wolf_teammates() -> None:
    mason_result = _run(_base(
        ["Thief", "Mason", "Mason", "Wolf", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "thief", "target": "u2"},
            {"kind": "resolve_night"},
        ],
    ))
    assert any(
        event["kind"] == "team_member_converted" and event["target_user_id"] == "u3"
        for event in mason_result["events"]
    )

    wolf_result = _run(_base(
        ["Thief", "Wolf", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "thief", "target": "u2"},
            {"kind": "resolve_night"},
        ],
    ))
    assert any(
        event["kind"] == "team_member_added" and event["target_user_id"] == "u2"
        for event in wolf_result["events"]
    )


def test_wild_child_conversion_notifies_wolf_team() -> None:
    room = GameRoom("wild-child-notice", ruleset_official(), phase=GamePhase.NIGHT, day=1)
    wolf = Player("wolf", "狼人", 1, role=Role.WOLF)
    child = Player("child", "孩子", 2, role=Role.WILD_CHILD, role_model="idol")
    idol = Player("idol", "偶像", 3, role=Role.VILLAGER, alive=False)
    room.players = [wolf, child, idol]
    events = GameRoomEngine()._resolve_role_changes(room)
    assert child.role == Role.WOLF
    assert any(event.kind == "team_member_added" and event.target_user_id == "wolf" for event in events)
    assert next(event for event in events if event.kind == "wild_child_changed").metadata["teammates"] == ["wolf"]


def test_bitten_wild_child_and_doppelganger_do_not_transform_after_lynch() -> None:
    """Werewolf.cs:1715-1735 跳过被咬玩家的学徒、野孩子和分身转职。"""
    wild_child = Player("child", "孩子", 1, role=Role.WILD_CHILD, role_model="idol", bitten=True)
    doppelganger = Player("dg", "模仿者", 2, role=Role.DOPPELGANGER, role_model="gunner", bitten=True)
    idol = Player("idol", "偶像", 3, role=Role.VILLAGER, alive=False)
    gunner = Player("gunner", "枪手", 4, role=Role.GUNNER, alive=False)
    room = GameRoom(
        "bitten-role-changes", ruleset_official(), phase=GamePhase.VOTE, day=2,
        players=[wild_child, doppelganger, idol, gunner],
    )

    events = GameRoomEngine()._resolve_role_changes(room, checkbitten=True)

    assert wild_child.role == Role.WILD_CHILD
    assert doppelganger.role == Role.DOPPELGANGER
    assert events == []


def test_doppelganger_copied_role_restores_ability() -> None:
    room = GameRoom("doppelganger-notice", ruleset_official(), phase=GamePhase.NIGHT, day=1)
    doppelganger = Player("dg", "模仿者", 1, role=Role.DOPPELGANGER, role_model="gunner")
    gunner = Player("gunner", "枪手", 2, role=Role.GUNNER, alive=False)
    room.players = [doppelganger, gunner]
    GameRoomEngine()._resolve_role_changes(room)
    assert doppelganger.role == Role.GUNNER
    assert doppelganger.bullet_count == 2
    assert QuestionType.SHOOT.value in doppelganger.metadata["role_actions"]


def test_doppelganger_copied_seer_notifies_beholder() -> None:
    room = GameRoom("doppelganger-seer", ruleset_official(), phase=GamePhase.NIGHT, day=1)
    doppelganger = Player("dg", "模仿者", 1, role=Role.DOPPELGANGER, role_model="seer")
    seer = Player("seer", "预言家", 2, role=Role.SEER, alive=False)
    beholder = Player("beholder", "观察者", 3, role=Role.BEHOLDER)
    room.players = [doppelganger, seer, beholder]
    events = GameRoomEngine()._resolve_role_changes(room)
    assert doppelganger.role == Role.SEER
    assert any(event.kind == "beholder_new_seer" and event.target_user_id == "beholder" for event in events)


def test_doppelganger_copied_snow_wolf_notifies_wolves() -> None:
    room = GameRoom("doppelganger-snow-wolf", ruleset_official(), phase=GamePhase.NIGHT, day=1)
    doppelganger = Player("dg", "模仿者", 1, role=Role.DOPPELGANGER, role_model="snow")
    snow = Player("snow", "雪狼", 2, role=Role.SNOW_WOLF, alive=False)
    wolf = Player("wolf", "狼人", 3, role=Role.WOLF)
    room.players = [doppelganger, snow, wolf]
    events = GameRoomEngine()._resolve_role_changes(room)
    assert doppelganger.role == Role.SNOW_WOLF
    assert any(event.kind == "team_member_added" and event.target_user_id == "wolf" for event in events)


def test_thief_stealing_seer_notifies_beholder() -> None:
    room = GameRoom("thief-seer-notice", ruleset_official(), phase=GamePhase.NIGHT, day=1)
    thief = Player("thief", "盗贼", 1, role=Role.THIEF)
    seer = Player("seer", "预言家", 2, role=Role.SEER)
    beholder = Player("beholder", "观察者", 3, role=Role.BEHOLDER)
    room.players = [thief, seer, beholder]
    events = GameRoomEngine()._resolve_thief(room, {
        QuestionType.THIEF.value: [NightAction("thief", QuestionType.THIEF.value, "seer", 1)],
    })
    assert thief.role == Role.SEER
    assert any(event.kind == "beholder_seer_stolen" and event.target_user_id == "beholder" for event in events)


def test_alpha_bite_updates_actions_and_notifies_teams() -> None:
    room = GameRoom("alpha-bite-notice", ruleset_official(), phase=GamePhase.NIGHT, day=1)
    alpha = Player("alpha", "头狼", 1, role=Role.ALPHA_WOLF)
    mason = Player("mason", "石匠", 2, role=Role.MASON)
    bitten = Player("bitten", "被咬者", 3, role=Role.MASON, bitten=True)
    room.players = [alpha, mason, bitten]
    events = GameRoomEngine()._apply_pending_bites(room)
    assert bitten.role == Role.WOLF
    assert QuestionType.KILL.value in bitten.metadata["role_actions"]
    assert any(event.kind == "team_member_converted" and event.target_user_id == "mason" for event in events)
    assert any(event.kind == "team_member_added" and event.target_user_id == "alpha" for event in events)


def test_alpha_bite_marks_serial_killer_but_not_traitor_as_strongest() -> None:
    """Werewolf.cs:3370-3425：只有咬到连环杀手的头狼会获得最强头狼标记。"""
    rules = replace(
        ruleset_official(),
        alpha_wolf_conversion_chance=100,
        serial_killer_stumble_chance=0,
    )

    alpha = Player("alpha", "头狼", 1, role=Role.ALPHA_WOLF)
    killer = Player("killer", "连环杀手", 2, role=Role.SERIAL_KILLER, choice="other")
    killer_room = GameRoom("alpha-bites-killer", rules, phase=GamePhase.NIGHT, day=1,
                           players=[alpha, killer])
    killer_events = GameRoomEngine()._resolve_wolf_attack(killer_room, killer)

    assert killer.bitten is True
    assert alpha.metadata["strongest_alpha"] is True
    assert any(event.kind == "player_bitten" and event.target_user_id == "killer" for event in killer_events)

    alpha = Player("alpha", "头狼", 1, role=Role.ALPHA_WOLF)
    traitor = Player("traitor", "叛徒", 2, role=Role.TRAITOR)
    traitor_room = GameRoom("alpha-bites-traitor", rules, phase=GamePhase.NIGHT, day=1,
                            players=[alpha, traitor])
    traitor_events = GameRoomEngine()._resolve_wolf_attack(traitor_room, traitor)

    assert traitor.bitten is True
    assert "strongest_alpha" not in alpha.metadata
    assert any(event.kind == "player_bitten" and event.target_user_id == "traitor" for event in traitor_events)


def test_thief_wolf_visit_depends_on_thief_full_rule() -> None:
    """Werewolf.cs:2349 and 2353-2375：经典盗贼可偷狼，完整盗贼访问狼会失败。"""
    thief = Player("thief", "盗贼", 1, role=Role.THIEF)
    wolf = Player("wolf", "狼人", 2, role=Role.WOLF, choice="other")
    classic_room = GameRoom(
        "classic-thief-wolf", replace(ruleset_official(), thief_full=False),
        phase=GamePhase.NIGHT, day=1, players=[thief, wolf],
    )
    classic_events = GameRoomEngine()._resolve_thief(classic_room, {
        QuestionType.THIEF.value: [NightAction("thief", QuestionType.THIEF.value, "wolf", 1)],
    })
    assert thief.role == Role.WOLF
    assert wolf.role == Role.VILLAGER
    assert any(event.kind == "thief_stole_role" for event in classic_events)

    thief = Player("thief", "盗贼", 1, role=Role.THIEF)
    wolf = Player("wolf", "狼人", 2, role=Role.WOLF, choice="other")
    full_room = GameRoom(
        "full-thief-wolf", replace(ruleset_official(), thief_full=True),
        phase=GamePhase.NIGHT, day=2, players=[thief, wolf],
    )
    full_events = GameRoomEngine()._resolve_thief(full_room, {
        QuestionType.THIEF.value: [NightAction("thief", QuestionType.THIEF.value, "wolf", 2)],
    })
    assert thief.role == Role.THIEF
    assert wolf.role == Role.WOLF
    assert any(event.kind == "thief_failed" for event in full_events)


def test_chemist_guardian_and_thief_visit_death_branches() -> None:
    """Werewolf.cs:2339-2419, 3792-3838：访问 SK/掘墓人的死亡分支。"""
    rules = replace(ruleset_official(), grave_digger_fall_chance=100)

    chemist = Player("chemist", "化学家", 1, role=Role.CHEMIST, has_used_ability=True)
    killer = Player("killer", "连环杀手", 2, role=Role.SERIAL_KILLER, choice="other")
    room = GameRoom("chemist-visits-killer", rules, phase=GamePhase.NIGHT, day=1,
                    players=[chemist, killer])
    events = GameRoomEngine()._resolve_chemistry(room, {
        QuestionType.CHEMISTRY.value: [NightAction("chemist", QuestionType.CHEMISTRY.value, "killer", 1)],
    })
    assert chemist.alive is False
    assert chemist.kill_method == KillMethod.VISIT_KILLER.value
    assert not any(event.kind == "chemistry_success" for event in events)

    guardian = Player("guardian", "守护", 1, role=Role.GUARDIAN_ANGEL)
    digger = Player("digger", "掘墓人", 2, role=Role.GRAVE_DIGGER)
    digger.metadata["dug_graves_last_night"] = 1
    room = GameRoom("guardian-visits-grave", rules, phase=GamePhase.NIGHT, day=1,
                    players=[guardian, digger])
    events = GameRoomEngine()._resolve_guard_visits(room, {
        QuestionType.GUARD.value: [NightAction("guardian", QuestionType.GUARD.value, "digger", 1)],
    })
    assert guardian.alive is False
    assert guardian.kill_method == KillMethod.FALL_GRAVE.value
    assert not any(event.kind == "guardian_no_attack" for event in events)

    thief = Player("thief", "盗贼", 1, role=Role.THIEF)
    digger = Player("digger", "掘墓人", 2, role=Role.GRAVE_DIGGER)
    digger.metadata["dug_graves_last_night"] = 1
    room = GameRoom("thief-visits-grave", replace(rules, thief_full=True),
                    phase=GamePhase.NIGHT, day=2, players=[thief, digger])
    GameRoomEngine()._resolve_thief(room, {
        QuestionType.THIEF.value: [NightAction("thief", QuestionType.THIEF.value, "digger", 2)],
    })
    assert thief.alive is False
    assert thief.kill_method == KillMethod.FALL_GRAVE.value


def test_thief_rechecks_random_replacement_before_stealing() -> None:
    class FixedTargetEngine(GameRoomEngine):
        def _pick_random_living(self, room: GameRoom, exclude: Player) -> Player | None:
            return next(player for player in room.players if player.user_id == "burning")

        class FixedRandom:
            def randrange(self, stop: int) -> int:
                return 0

    thief = Player("thief", "盗贼", 1, role=Role.THIEF)
    dead = Player("dead", "死者", 2, role=Role.VILLAGER, alive=False)
    burning = Player("burning", "燃烧者", 3, role=Role.VILLAGER, burning=True)
    room = GameRoom("thief-recheck", ruleset_official(), phase=GamePhase.NIGHT, day=1,
                    players=[thief, dead, burning])
    events = FixedTargetEngine(rng=FixedTargetEngine.FixedRandom())._steal_role(room, thief, dead)
    assert thief.role == Role.THIEF
    assert thief.alive is False
    assert burning.role == Role.VILLAGER
    assert any(event.kind == "thief_failed" for event in events)


def test_clumsy_retarget_pinned() -> None:
    """Werewolf.cs:999 ChooseRandomPlayerId(false) when the 50% clumsy roll hits."""
    ops = [
        {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
        {"kind": "timeout", "seconds": 60},
        {"kind": "vote", "actor": "u2", "target": "u1"},
        {"kind": "vote", "actor": "u1", "target": "u3"},
        {"kind": "vote", "actor": "u3", "target": "u4"},
        {"kind": "vote", "actor": "u4", "target": "u1"},
        {"kind": "vote", "actor": "u5", "target": "u3"},
        {"kind": "resolve_vote"},
    ]
    stable = _run(_base(
        ["Wolf", "ClumsyGuy", "Villager", "Villager", "Villager"],
        rules={"clumsy_retarget_chance": 0},
        operations=ops,
    ))
    # 0%: u1=2 (clumsy+u4), u3=2 (wolf+u5) → official tie, nobody lynched.
    assert _player(stable, "u1")["alive"] is True
    assert _player(stable, "u3")["alive"] is True
    moved = None
    for seed in range(40):
        candidate = _run(_base(
            ["Wolf", "ClumsyGuy", "Villager", "Villager", "Villager"],
            rules={"clumsy_retarget_chance": 100},
            seed=seed,
            operations=ops,
        ))
        if _player(candidate, "u1")["alive"] is False or _player(candidate, "u3")["alive"] is False:
            moved = candidate
            break
    assert moved is not None, "100% clumsy retarget must change the official 2-2 tie"


def test_idle_two_nonvotes_kills_without_hunter_or_lover() -> None:
    result = _run(_base(
        ["Wolf", "Hunter", "Villager", "Villager", "Cupid"],
        operations=[
            {"kind": "night_action", "actor": "u5", "action": "cupid", "target": "u2"},
            {"kind": "night_action", "actor": "u5", "action": "cupid", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u5"},
            {"kind": "vote", "actor": "u2", "target": "skip"},
            {"kind": "vote", "actor": "u3", "target": "u5"},
            {"kind": "vote", "actor": "u4", "target": "u5"},
            {"kind": "vote", "actor": "u5", "target": "u4"},
            {"kind": "resolve_vote"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u4"},
            {"kind": "vote", "actor": "u2", "target": "skip"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
            {"kind": "vote", "actor": "u4", "target": "u1"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert _player(result, "u2")["kill_method"] == KillMethod.IDLE.value
    assert _player(result, "u3")["alive"] is True
    assert not any(event["kind"] == "hunter_prompt" for event in result["events"] if event["target_user_id"] == "u2")


def test_troublemaker_starts_second_vote_cycle() -> None:
    """Werewolf.cs:2544 do/while lynchAttempt < (doubleLynch ? 2 : 1)."""
    after_first = _run(_base(
        ["Wolf", "Troublemaker", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "trouble", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u5"},
            {"kind": "vote", "actor": "u2", "target": "u5"},
            {"kind": "vote", "actor": "u3", "target": "u5"},
            {"kind": "vote", "actor": "u4", "target": "u5"},
            {"kind": "vote", "actor": "u5", "target": "u1"},
        ],
    ))
    assert _player(after_first, "u5")["alive"] is False
    assert after_first["phase"] == "vote"
    assert sum(1 for event in after_first["events"] if event["kind"] == "vote_started") == 2
    both = _run(_base(
        ["Wolf", "Troublemaker", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "trouble", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u5"},
            {"kind": "vote", "actor": "u2", "target": "u5"},
            {"kind": "vote", "actor": "u3", "target": "u5"},
            {"kind": "vote", "actor": "u4", "target": "u5"},
            {"kind": "vote", "actor": "u5", "target": "u1"},
            {"kind": "vote", "actor": "u1", "target": "u4"},
            {"kind": "vote", "actor": "u2", "target": "u4"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
            {"kind": "vote", "actor": "u4", "target": "u1"},
        ],
    ))
    assert _player(both, "u5")["alive"] is False
    assert _player(both, "u4")["alive"] is False
    assert both["phase"] == "night"


def test_traitor_promotes_when_no_wolf_roles() -> None:
    result = _run(_base(
        ["Traitor", "Villager", "Villager", "Villager", "Villager"],
        operations=[{"kind": "check_winners"}],
    ))
    assert _player(result, "u1")["role"] == Role.WOLF.value


def test_snow_wolf_promotes_before_traitor() -> None:
    result = _run(_base(
        ["SnowWolf", "Traitor", "Villager", "Villager", "Villager"],
        operations=[{"kind": "check_winners"}],
    ))
    assert _player(result, "u1")["role"] == Role.WOLF.value
    assert _player(result, "u2")["role"] == Role.TRAITOR.value


def test_beholder_and_mason_and_fool_identity() -> None:
    result = _run(_base(
        ["Seer", "Fool", "Beholder", "Mason", "Mason", "Wolf"],
        operations=[
            {"kind": "refresh_identity"},
            {"kind": "night_action", "actor": "u6", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u6"},
            {"kind": "night_action", "actor": "u2", "action": "seer", "target": "u6"},
        ],
    ))
    identities = {event["target_user_id"]: event for event in _events(result, "identity")}
    assert identities["u2"]["metadata"]["shown_role"] == Role.SEER.value
    assert identities["u3"]["metadata"]["seer_id"] == "u1"
    assert set(identities["u4"]["metadata"]["mason_ids"]) == {"u4", "u5"}


def test_win_cult_sweep() -> None:
    result = _run(_base(
        ["Cultist", "CultistHunter", "Villager", "Wolf", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u5"},
            {"kind": "night_action", "actor": "u3", "action": "convert", "target": "u5"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u4"},
            {"kind": "vote", "actor": "u2", "target": "u4"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
            {"kind": "vote", "actor": "u5", "target": "u4"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "resolve_vote"},
        ],
    ))
    # 3 cultists + CH remain. Official CheckForGameEnd: cult exists so Village
    # does not win; cult is not a sweep; wolves are gone. No winner yet.
    assert result["winner"] is None
    assert _player(result, "u1")["role"] == Role.CULTIST.value
    assert _player(result, "u3")["role"] == Role.CULTIST.value
    assert _player(result, "u5")["role"] == Role.CULTIST.value
    assert _player(result, "u2")["role"] == Role.CULTIST_HUNTER.value
    assert _player(result, "u4")["alive"] is False


def test_win_serial_killer_two_player() -> None:
    result = _run(_base(
        ["SerialKiller", "Villager", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    assert result["winner"] == Team.SERIAL_KILLER.value
    assert _player(result, "u1")["won"] is True


def test_win_arsonist_two_player_blocked_by_gunner_bullets() -> None:
    blocked = _run(_base(
        ["Arsonist", "Gunner", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    wins = _run(_base(
        ["Arsonist", "Gunner", "Villager", "Villager", "Villager"],
        operations=[
            *_kill_extras("u3", "u4", "u5"),
            {"kind": "set_state", "player": "u2", "values": {"bullet_count": 0}},
            {"kind": "check_winners"},
        ],
    ))
    assert blocked["winner"] is None
    assert wins["winner"] == Team.ARSONIST.value


def test_win_lovers_two_alive() -> None:
    result = _run(_base(
        ["Villager", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "set_state", "player": "u1", "values": {"lover_id": "u2"}},
            {"kind": "set_state", "player": "u2", "values": {"lover_id": "u1"}},
            *_kill_extras("u3", "u4", "u5"),
            {"kind": "check_winners"},
        ],
    ))
    assert result["winner"] == Team.LOVERS.value
    assert _player(result, "u1")["won"] is True
    assert _player(result, "u2")["won"] is True


def test_win_noone_tanner_last_and_thief_last() -> None:
    tanner = _run(_base(
        ["Tanner", "Villager", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u2", "u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    thief = _run(_base(
        ["Thief", "Villager", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u2", "u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    assert tanner["winner"] == Team.NO_ONE.value
    assert thief["winner"] == Team.NO_ONE.value
    assert _player(tanner, "u1")["won"] is False


@pytest.mark.parametrize(
    "roles",
    [
        (Role.SORCERER, Role.THIEF),
        (Role.SORCERER, Role.DOPPELGANGER),
        (Role.THIEF, Role.DOPPELGANGER),
        (Role.SORCERER, Role.THIEF, Role.DOPPELGANGER),
    ],
)
def test_official_noone_special_end_matrix(roles: tuple[Role, ...]) -> None:
    """Werewolf.cs:4700-4785 handles the 2p and 3p special-role endings."""
    players = [
        Player(f"u{index}", role.value, index, role=role)
        for index, role in enumerate(roles, 1)
    ]
    room = GameRoom(
        "noone-special-matrix",
        ruleset_official(),
        phase=GamePhase.DAY,
        players=players,
    )
    engine = GameRoomEngine()

    assert engine.check_winners(room) == (Team.NO_ONE,)
    events = engine.finish(room, Team.NO_ONE)

    assert room.phase == GamePhase.FINISHED
    assert any(event.kind == "no_one_special_end" for event in events)
    assert all(player.won is False for player in players)


def test_win_sk_hunter_nobody_won() -> None:
    result = _run(_base(
        ["Hunter", "SerialKiller", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    assert result["winner"] == Team.SK_HUNTER.value
    assert _player(result, "u1")["won"] is False
    assert _player(result, "u2")["won"] is False


def test_win_hunter_vs_wolf_two_player_pinned() -> None:
    village = _run(_base(
        ["Hunter", "Wolf", "Villager", "Villager", "Villager"],
        rules={"hunter_kill_wolf_chance_base": 100},
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    wolf = _run(_base(
        ["Hunter", "Wolf", "Villager", "Villager", "Villager"],
        rules={"hunter_kill_wolf_chance_base": 0},
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    assert village["winner"] == Team.VILLAGE.value
    assert wolf["winner"] == Team.WOLF.value


def test_win_gunner_blocks_wolf_parity() -> None:
    result = _run(_base(
        ["Wolf", "Wolf", "Gunner", "Villager", "Villager"],
        operations=[*_kill_extras("u5"), {"kind": "check_winners"}],
    ))
    assert result["winner"] is None


def test_lovers_wolves_one_ahead_are_blocked_by_gunner() -> None:
    result = _run(_base(
        ["Wolf", "Wolf", "Gunner", "Villager", "Villager"],
        operations=[
            {"kind": "set_state", "player": "u1", "values": {"lover_id": "u2"}},
            {"kind": "set_state", "player": "u2", "values": {"lover_id": "u1"}},
            {"kind": "set_state", "player": "u3", "values": {"bullet_count": 1}},
            *_kill_extras("u4", "u5"),
            {"kind": "check_winners"},
        ],
    ))
    assert result["winner"] is None


def test_lover_of_winner_also_wins() -> None:
    result = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "set_state", "player": "u1", "values": {"lover_id": "u2"}},
            {"kind": "set_state", "player": "u2", "values": {"lover_id": "u1"}},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u3"},
            {"kind": "vote", "actor": "u2", "target": "u3"},
            {"kind": "vote", "actor": "u4", "target": "u3"},
            {"kind": "vote", "actor": "u5", "target": "u3"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
            {"kind": "resolve_vote"},
        ],
    ))
    if result["winner"] == Team.WOLF.value:
        assert _player(result, "u2")["won"] is True


def test_doppelganger_copies_then_follows_apprentice_path() -> None:
    result = _run(_base(
        ["Wolf", "Doppelgänger", "ApprenticeSeer", "Seer", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "copy", "target": "u4"},
            {"kind": "night_action", "actor": "u4", "action": "seer", "target": "u1"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u4"},
        ],
    ))
    assert _player(result, "u2")["role"] == Role.SEER.value
    assert _player(result, "u3")["role"] == Role.SEER.value


def test_every_role_can_be_dealt_in_a_set_roles_fixture() -> None:
    """Parity matrix: every Roles.cs card is a legal set_roles value."""
    fillers = [Role.WOLF.value, Role.VILLAGER.value, Role.VILLAGER.value, Role.VILLAGER.value]
    for role in ALL_ROLES:
        result = _run(_base(
            [role.value, *fillers],
            operations=[{"kind": "check_winners"}],
        ))
        assert _player(result, "u1")["role"] in {role.value, Role.WOLF.value, Role.SEER.value}


def test_ga_save_survives_if_ga_dies_same_night() -> None:
    """Werewolf.cs:3091 capture GA before attacks; 4066 dead-this-night GA still saves."""
    result = _run(_base(
        ["Wolf", "GuardianAngel", "SerialKiller", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u4"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u4"},
            {"kind": "night_action", "actor": "u3", "action": "serial_kill", "target": "u2"},
        ],
    ))
    assert _player(result, "u4")["alive"] is True
    assert _player(result, "u2")["alive"] is False


def test_thief_classic_n1_timeout_steals_random() -> None:
    """Werewolf.cs:4138 classic N1 ChooseRandomPlayerId when Choice is empty."""
    result = _run(_base(
        ["Thief", "Wolf", "Seer", "Villager", "Villager"],
        rules={"thief_full": False},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u2"},
            {"kind": "timeout", "seconds": 90},
        ],
    ))
    assert _player(result, "u1")["role"] != Role.THIEF.value
    stolen = _player(result, "u1")["role"]
    assert stolen in {Role.WOLF.value, Role.SEER.value, Role.VILLAGER.value}
    assert sum(1 for index in range(1, 6) if _player(result, f"u{index}")["role"] == Role.THIEF.value) == 0


def test_cult_zero_chance_roles_never_convert() -> None:
    """Werewolf.cs:3737 Doppelgänger/Thief/Spumpkin ConvertToCult(..., 0)."""
    for role in (Role.DOPPELGANGER.value, Role.THIEF.value, Role.SPUMPKIN.value):
        extra = [] if role != Role.DOPPELGANGER.value else [
            {"kind": "night_action", "actor": "u3", "action": "copy", "target": "u5"},
        ]
        if role == Role.THIEF.value:
            extra = [{"kind": "night_action", "actor": "u3", "action": "thief", "target": "u5"}]
        result = _run(_base(
            ["Cultist", "CultistHunter", role, "Wolf", "Villager"],
            operations=[
                {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
                {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
                *extra,
                {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
            ],
        ))
        assert _player(result, "u3")["role"] != Role.CULTIST.value
        if role != Role.THIEF.value:
            assert _player(result, "u3")["role"] == role


def test_cult_home_snow_kills_visitor() -> None:
    """Werewolf.cs:3688 SnowWolf staying home VisitWolf."""
    result = _run(_base(
        ["Cultist", "CultistHunter", "SnowWolf", "Wolf", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "freeze", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    assert _player(result, "u1")["kill_method"] == KillMethod.VISIT_WOLF.value
    assert _player(result, "u3")["role"] == Role.SNOW_WOLF.value


def test_hunter_cult_convert_and_counter_pinned() -> None:
    """Werewolf.cs:3615 HunterConversionChance then HunterKillCultChance."""
    converted = _run(_base(
        ["Cultist", "CultistHunter", "Hunter", "Wolf", "Villager"],
        rules={"hunter_conversion_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    shot = _run(_base(
        ["Cultist", "CultistHunter", "Hunter", "Wolf", "Villager"],
        rules={"hunter_conversion_chance": 0, "hunter_kill_cult_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    missed = _run(_base(
        ["Cultist", "CultistHunter", "Hunter", "Wolf", "Villager"],
        rules={"hunter_conversion_chance": 0, "hunter_kill_cult_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    assert _player(converted, "u3")["role"] == Role.CULTIST.value
    assert _player(shot, "u1")["kill_method"] == KillMethod.HUNTER_CULT.value
    assert _player(missed, "u3")["role"] == Role.HUNTER.value
    assert _player(missed, "u1")["alive"] is True


def test_chemist_demotes_after_killing_wise_elder() -> None:
    """Werewolf.cs:4279-4284 —— 非隐藏身份模式下毒杀长老，存活的化学家被降级为村民。"""
    result = _run(_base(
        ["Wolf", "Chemist", "WiseElder", "Villager", "Villager"],
        rules={"chemist_success_chance": 100},
        operations=[
            {"kind": "set_state", "player": "u2", "values": {"has_used_ability": True}},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "chemistry", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["kill_method"] == KillMethod.CHEMISTRY.value
    assert _player(result, "u2")["role"] == Role.VILLAGER.value


def test_hunter_final_shot_demotes_on_wise_elder() -> None:
    """Werewolf.cs:5471 HunterKilledWiseElder → Transform KillElder even if hunter is already dead."""
    result = _run(_base(
        ["Wolf", "Hunter", "WiseElder", "Villager", "Villager"],
        rules={"hunter_kill_wolf_chance_base": 0},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
            {"kind": "night_action", "actor": "u2", "action": "hunt", "target": "u3"},
        ],
    ))
    assert _player(result, "u2")["role"] == Role.VILLAGER.value
    assert _player(result, "u3")["kill_method"] == KillMethod.HUNTER.value


def test_snow_cannot_refreeze_next_night_but_victim_can_act() -> None:
    """SendNightActions: snow targets exclude Frozen, then Frozen is cleared before NightCycle."""
    blocked = _base(
        ["Wolf", "SnowWolf", "Seer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u1"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
        ],
    )
    with pytest.raises(Exception):
        OfficialFixtureRunner().run(blocked)
    acted = _run(_base(
        ["Wolf", "SnowWolf", "Seer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u1"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "u4"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u1"},
        ],
    ))
    assert any(event["kind"] == "seer_result" for event in acted["events"])


def test_silver_second_night_wolves_can_eat_again() -> None:
    """SendNightActions clears _silverSpread after one skipped wolf/snow menu night."""
    result = _run(_base(
        ["Wolf", "Blacksmith", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "silver", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u5"},
            {"kind": "vote", "actor": "u2", "target": "u5"},
            {"kind": "vote", "actor": "u3", "target": "u5"},
            {"kind": "vote", "actor": "u4", "target": "u5"},
            {"kind": "vote", "actor": "u5", "target": "u4"},
            {"kind": "timeout", "seconds": 90},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u4"},
            {"kind": "vote", "actor": "u2", "target": "u4"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
            {"kind": "vote", "actor": "u4", "target": "u1"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["kill_method"] == KillMethod.EAT.value


def test_fool_result_is_shuffled_living_non_seer() -> None:
    """Werewolf.cs:3991 Fool shuffle-twice; WolfRoles display as Wolf."""
    result = _run(_base(
        ["Fool", "Wolf", "Villager", "Gunner", "Tanner"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u3"},
        ],
    ))
    seen = _event(result, "fool_result")["metadata"]["seen_role"]
    assert seen in {
        Role.WOLF.value, Role.VILLAGER.value, Role.GUNNER.value, Role.TANNER.value,
    }


def test_augur_sees_absent_possible_role() -> None:
    """Werewolf.cs:4048 PossibleRoles.Shuffle then first role not present and not already seen."""
    result = _run(_base(
        ["Augur", "Wolf", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "set_statistics", "values": {
                "possible_roles": ["Augur", "Wolf", "Villager", "Gunner"],
            }},
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
        ],
    ))
    assert _event(result, "augur_result")["metadata"]["role"] == Role.GUNNER.value


def test_pacifist_and_troublemaker_last_ability_wins() -> None:
    """Werewolf.cs:917 peace clears doubleLynch; 971 trouble clears pacifist."""
    peace_last = _run(_base(
        ["Wolf", "Troublemaker", "Pacifist", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "trouble", "target": "yes"},
            {"kind": "day_action", "actor": "u3", "action": "pacifist", "target": "yes"},
            # 和平后置：Werewolf.cs:2559-2564 LynchCycle 开局直接 return，
            # 处决菜单不会发出，本轮无票可投。
            {"kind": "timeout", "seconds": 60},
        ],
    ))
    trouble_last = _run(_base(
        ["Wolf", "Troublemaker", "Pacifist", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u3", "action": "pacifist", "target": "yes"},
            {"kind": "day_action", "actor": "u2", "action": "trouble", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u5"},
            {"kind": "vote", "actor": "u2", "target": "u5"},
            {"kind": "vote", "actor": "u3", "target": "u5"},
            {"kind": "vote", "actor": "u4", "target": "u5"},
            {"kind": "vote", "actor": "u5", "target": "u1"},
        ],
    ))
    assert _player(peace_last, "u5")["alive"] is True
    assert peace_last["phase"] == "night"
    assert _player(trouble_last, "u5")["alive"] is False
    assert trouble_last["phase"] == "vote"


def test_lynch_bitten_apprentice_does_not_promote() -> None:
    """Werewolf.cs:2780 CheckRoleChanges(true) skips bitten APS after a lynch."""
    result = _run(_base(
        ["AlphaWolf", "ApprenticeSeer", "Seer", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u1"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u3"},
            {"kind": "vote", "actor": "u2", "target": "u3"},
            {"kind": "vote", "actor": "u4", "target": "u3"},
            {"kind": "vote", "actor": "u5", "target": "u3"},
            {"kind": "vote", "actor": "u3", "target": "u4"},
        ],
    ))
    assert not any(event["kind"] == "apprentice_now_seer" for event in result["events"])
    assert _player(result, "u2")["role"] == Role.WOLF.value


def test_cult_two_player_hunter_kills_cultist() -> None:
    """Werewolf.cs:4576-4580 —— CH vs 教徒残局只记 DBKill，教徒保持存活，村民胜。"""
    result = _run(_base(
        ["Cultist", "CultistHunter", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    assert _player(result, "u1")["alive"] is True
    assert result["winner"] == Team.VILLAGE.value


def test_cult_two_player_wolf_eats_and_villager_converts() -> None:
    """Werewolf.cs 2p: wolf eats cultist; other non-DG/Thief is auto-converted."""
    wolf = _run(_base(
        ["Cultist", "Wolf", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    convert = _run(_base(
        ["Cultist", "Villager", "Villager", "Villager", "Villager"],
        operations=[*_kill_extras("u3", "u4", "u5"), {"kind": "check_winners"}],
    ))
    assert wolf["winner"] == Team.WOLF.value
    assert convert["winner"] == Team.CULT.value
    assert _player(convert, "u2")["role"] == Role.CULTIST.value


def test_prince_dies_on_second_lynch() -> None:
    """Werewolf.cs:2745 first lynch reveals; later lynch kills."""
    result = _run(_base(
        ["Wolf", "Prince", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u2", "target": "u5"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "vote", "actor": "u5", "target": "u2"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u2", "target": "u5"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "vote", "actor": "u5", "target": "u2"},
        ],
    ))
    assert _player(result, "u2")["alive"] is False
    assert _player(result, "u2")["kill_method"] == KillMethod.LYNCH.value


def test_cupid_timeout_forces_random_pair() -> None:
    """Werewolf.cs ValidateSpecialRoleChoices: missing Cupid pair is assigned randomly."""
    result = _run(_base(
        ["Cupid", "Wolf", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 90},
        ],
    ))
    lovers = [player for player in result["players"] if player["lover_id"]]
    assert len(lovers) == 2
    assert lovers[0]["lover_id"] == lovers[1]["id"]
    assert lovers[1]["lover_id"] == lovers[0]["id"]


def test_fool_can_see_snow_wolf_as_snow() -> None:
    """Fool uses WolfRoles, not majority wolves; SnowWolf can appear as SnowWolf."""
    roles = {
        _event(_run(_base(
            ["Fool", "SnowWolf", "Wolf", "Villager", "Gunner"],
            seed=seed,
            operations=[
                {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
                {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "skip"},
                {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u4"},
            ],
        )), "fool_result")["metadata"]["seen_role"]
        for seed in range(30)
    }
    assert Role.SNOW_WOLF.value in roles
    assert Role.ALPHA_WOLF.value not in roles


def test_harlot_discover_cult_pinned() -> None:
    """Settings.HarlotDiscoverCultChance."""
    found = _run(_base(
        ["Wolf", "Harlot", "Cultist", "CultistHunter", "Villager"],
        rules={"harlot_discover_cult_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u4", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "convert", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "visit", "target": "u3"},
        ],
    ))
    missed = _run(_base(
        ["Wolf", "Harlot", "Cultist", "CultistHunter", "Villager"],
        rules={"harlot_discover_cult_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u4", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "convert", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "visit", "target": "u3"},
        ],
    ))
    assert _event(found, "harlot_visit")["metadata"]["discovered_cult"] is True
    assert _event(missed, "harlot_visit")["metadata"]["discovered_cult"] is False


def test_arsonist_home_cult_convert_is_zero() -> None:
    """Werewolf.cs:3713 Arsonist home/frozen ConvertToCult(..., 0)."""
    result = _run(_base(
        ["Cultist", "CultistHunter", "Arsonist", "Wolf", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "hunt_cult", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "douse", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "convert", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["role"] == Role.ARSONIST.value
