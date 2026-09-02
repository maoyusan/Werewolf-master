"""独立复核审计（任务 3.2-3.5 / 7.1-7.5）：阶段总流程、轮次与倒计时、
指令状态矩阵、群设置进入规则流程、快照与恢复安全性。

期望值全部取自官方源码 ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7：
`Werewolf Node/Werewolf.cs`、`Werewolf Node/Helpers/Settings.cs`、
`Werewolf Node/Models/IPlayer.cs`、`Werewolf Control/Commands/GameCommands.cs`、
`Database/GroupConfig.cs`。发现的差异按官方行为断言并标记 strict xfail。
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from application.contracts import PlatformEvent, PlatformSession, SessionType
from application.service import GameApplication
from domain.engine import GameRoomEngine, GameRuleError
from domain.fixtures import FrozenClock
from domain.models import (
    GameMode,
    GamePhase,
    GameRoom,
    NightAction,
    Player,
    QuestionType,
    Role,
    Team,
)
from domain.rules import ruleset_official


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))


def _engine(seed: int | None = None) -> tuple[GameRoomEngine, FrozenClock]:
    clock = _clock()
    rng = random.Random(seed) if seed is not None else None
    return GameRoomEngine(rng=rng, clock=clock), clock


def _room(
    roles: list[Role],
    clock: FrozenClock,
    *,
    phase: GamePhase = GamePhase.NIGHT,
    day: int = 1,
    rules=None,
    deadline_seconds: int = 90,
) -> GameRoom:
    room = GameRoom(
        "audit-flow",
        rules or ruleset_official(),
        phase=phase,
        day=day,
        stage_started_at=clock.now(),
        stage_deadline=clock.now() + timedelta(seconds=deadline_seconds),
    )
    room.players = [
        Player(f"u{index}", f"玩家{index}", index, role=role)
        for index, role in enumerate(roles, 1)
    ]
    return room


class _FakeStore:
    """服务层测试所需的最小持久化接口（房间保存在内存字典中）。"""

    def __init__(self, group_config: dict[str, object] | None = None) -> None:
        self.rooms: dict[str, GameRoom] = {}
        self.event_ids: set[str] = set()
        self.group_config = dict(group_config or {})
        self.saved_config: dict[str, object] | None = None

    async def begin_event(self, event_id: str, _key: str):
        if event_id in self.event_ids:
            return False, []
        self.event_ids.add(event_id)
        return True, None

    async def commit_result(self, *, room, **_kwargs) -> None:
        if room is not None:
            self.rooms[room.session_id] = room

    async def get_room(self, session_id: str):
        return self.rooms.get(session_id)

    async def delete_room(self, session_id: str) -> None:
        self.rooms.pop(session_id, None)

    async def list_active_rooms(self):
        return [
            room for room in self.rooms.values()
            if room.phase not in {GamePhase.FINISHED, GamePhase.CANCELLED}
        ]

    async def find_active_rooms_for_user(self, user_id: str):
        return [
            room for room in await self.list_active_rooms()
            if any(p.user_id == user_id and p.alive for p in room.players)
        ]

    async def find_rooms_for_user(self, user_id: str):
        return [
            room for room in self.rooms.values()
            if any(p.user_id == user_id for p in room.players)
        ]

    async def get_group_rule_config(self, _group_id: str):
        return dict(self.group_config)

    async def save_group_rule_config(self, _group_id: str, values, _updated_by):
        self.saved_config = dict(values)
        self.group_config = dict(values)

    async def get_direct_session(self, _user_id: str):
        return None


def _group_event(event_id: str, text: str, user_id: str = "creator") -> PlatformEvent:
    return PlatformEvent(
        event_id=event_id,
        session=PlatformSession(SessionType.GROUP, "audit-group"),
        user_id=user_id,
        display_name=f"名{user_id}",
        text=text,
    )


def _c2c_event(event_id: str, text: str, user_id: str) -> PlatformEvent:
    return PlatformEvent(
        event_id=event_id,
        session=PlatformSession(SessionType.C2C, user_id),
        user_id=user_id,
        display_name=f"名{user_id}",
        text=text,
    )


# ---------------------------------------------------------------------------
# 3.2 / 3.4 阶段总流程：完整多轮推进
# ---------------------------------------------------------------------------


def test_audit_flow_full_game_night_day_vote_night_to_wolf_win() -> None:
    """Werewolf.cs:624-640,2824-2835,3003,2571：主循环夜→昼→处决顺序、
    每阶段进入条件/倒计时、轮次计数与结束条件的完整两天推进。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.SEER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.NIGHT,
        day=1,
        deadline_seconds=90,
    )

    # 夜晚不允许投票（官方 LynchCycle 只在 Time==Lynch 发菜单，Werewolf.cs:2540,4950）。
    with pytest.raises(GameRuleError, match="不是投票阶段"):
        engine.submit_vote(room, "u3", "1")

    engine.submit_night_action(room, "u1", "狼人", "3")
    events = engine.submit_night_action(room, "u2", "查验", "1")

    # 全部夜间行动提交后立即结算（Werewolf.cs:3034-3041）并进入白天。
    assert room.phase == GamePhase.DAY
    assert room.day == 1
    assert not room.players[2].alive  # u3 被吃
    assert any(event.kind == "day_started" for event in events)
    # 白天时长 = DayTime(60) + max((4//5-1)*30, 60) = 120（Werewolf.cs:2824-2835）。
    assert room.stage_deadline == clock.now() + timedelta(seconds=120)

    # 白天不允许夜间行动（官方夜间菜单只在 NightCycle 发出）；
    # 已提交过的狼人重复发送则命中官方“菜单已确认”分支（Werewolf.cs:930-934）。
    with pytest.raises(GameRuleError, match="不是夜晚行动阶段"):
        engine.submit_night_action(room, "u4", "狼人", "2")
    with pytest.raises(GameRuleError, match="已经提交"):
        engine.submit_night_action(room, "u1", "狼人", "2")

    clock.advance(120)
    assert engine.due(room) is True
    events = engine.on_timeout(room)
    assert room.phase == GamePhase.VOTE
    assert any(event.kind == "vote_started" for event in events)
    # 处决阶段时长 = LynchTime 默认 90（Werewolf.cs:2567-2571）。
    assert room.stage_deadline == clock.now() + timedelta(seconds=90)

    engine.submit_vote(room, "u1", "2")
    engine.submit_vote(room, "u2", "1")
    engine.submit_vote(room, "u4", "2")
    events = engine.submit_vote(room, "u5", "2")

    # 存活玩家全部投票后立即结算（Werewolf.cs:2606-2609），u2 被处决，进入第 2 夜。
    assert not room.players[1].alive
    assert room.phase == GamePhase.NIGHT
    assert room.day == 2
    assert any(event.kind == "night_started" for event in events)
    # 第 2 夜恢复基础 90 秒（首夜 120 秒特例仅 GameDay==1，Werewolf.cs:3003-3010）。
    assert room.stage_deadline == clock.now() + timedelta(seconds=90)
    assert len(room.vote_history) == 1 and room.vote_history[0]["day"] == 1

    events = engine.submit_night_action(room, "u1", "狼人", "4")

    # 狼人达到半数，游戏结束（CheckForGameEnd，Werewolf.cs:4477+）。
    assert room.phase == GamePhase.FINISHED
    assert room.winner == Team.WOLF
    assert room.stage_deadline is None
    assert any(event.kind == "game_finished" for event in events)
    assert room.players[0].won is True


def test_audit_flow_first_night_cupid_then_second_night_base_duration() -> None:
    """Werewolf.cs:3004-3010：首夜有丘比特时 120 秒；第 2 夜即便丘比特仍存活，
    也恢复为 NightTime 基础时长。空投票轮走 NoLynchVotes 分支（Werewolf.cs:2790-2793）。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.CUPID, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.NIGHT,
        day=1,
        deadline_seconds=120,
    )

    engine.submit_night_action(room, "u2", "恋人", "3")
    engine.submit_night_action(room, "u2", "恋人", "4")
    events = engine.submit_night_action(room, "u1", "狼人", "跳过")

    assert room.phase == GamePhase.DAY
    assert room.players[2].lover_id == "u4"
    assert room.players[3].lover_id == "u3"

    clock.advance(int((room.stage_deadline - clock.now()).total_seconds()))
    engine.on_timeout(room)
    assert room.phase == GamePhase.VOTE

    for user_id in ["u1", "u2", "u3", "u4"]:
        engine.submit_vote(room, user_id, "弃票")
    events = engine.submit_vote(room, "u5", "弃票")

    # 无任何有效票 → NoLynchVotes，无人出局（Werewolf.cs:2694-2697,2790-2793）。
    assert any(event.kind == "vote_empty" for event in events)
    assert all(player.alive for player in room.players)
    assert all(player.non_vote_count == 1 for player in room.players)

    # 第 2 夜时长恢复 90 秒，不再套用首夜 120 秒特例。
    assert room.phase == GamePhase.NIGHT and room.day == 2
    assert room.stage_deadline == clock.now() + timedelta(seconds=90)


def test_audit_flow_dead_players_cannot_act_or_vote_or_be_voted() -> None:
    """Werewolf.cs:891-896,4955-4959：死亡玩家不再收到菜单，按钮被忽略；
    处决菜单只列出存活玩家。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.SEER, Role.DETECTIVE, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.NIGHT,
        day=2,
    )
    room.players[1].alive = False  # 预言家已死
    room.players[5].alive = False  # u6 已死

    with pytest.raises(GameRuleError, match="出局玩家不能行动"):
        engine.submit_night_action(room, "u2", "查验", "1")

    room.phase = GamePhase.DAY
    with pytest.raises(GameRuleError, match="出局玩家不能执行白天行动"):
        engine.submit_day_action(room, "u2", "侦查", "1")

    room.phase = GamePhase.VOTE
    with pytest.raises(GameRuleError, match="出局玩家不能投票"):
        engine.submit_vote(room, "u2", "1")
    with pytest.raises(GameRuleError, match="已经出局"):
        engine.submit_vote(room, "u4", "6")


def test_audit_flow_lynch_vote_cannot_target_self() -> None:
    """Werewolf.cs:4959：处决菜单的候选列表排除自己（x.Id != player.Id）。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.VOTE,
    )

    with pytest.raises(GameRuleError, match="不能选择自己"):
        engine.submit_vote(room, "u2", "2")
    assert "u2" not in room.votes


def test_audit_flow_day_actions_resolve_gunner_before_detective() -> None:
    """Werewolf.cs:2871-2968：白天结束后先结算枪手，再结算侦探；
    已被击杀的侦探不再得到侦查结果（detect 分支要求 !IsDead）。"""
    engine, clock = _engine()
    room = _room(
        [Role.GUNNER, Role.DETECTIVE, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
        day=1,
    )

    engine.submit_day_action(room, "u2", "侦查", "3")
    engine.submit_day_action(room, "u1", "开枪", "2")

    clock.advance(90)
    events = engine.on_timeout(room)

    assert not room.players[1].alive
    assert room.players[0].bullet_count == 1
    assert not any(event.kind == "detect_result" for event in events)
    assert room.phase == GamePhase.VOTE


def test_audit_flow_allow_flee_off_keeps_player_alive() -> None:
    """Werewolf.cs:835-839：AllowFlee 关闭时开局后 /flee 无效（只公告 FleeDisabled），玩家保持存活。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
        rules=ruleset_official(allow_flee=False),
    )

    events = engine.flee(room, "u2")
    assert [event.kind for event in events] == ["flee_disabled"]
    assert events[0].public is True
    assert room.players[1].alive is True


# ---------------------------------------------------------------------------
# 等待阶段 /extend（官方 GameCommands.Extend + Werewolf.ExtendTime）
# ---------------------------------------------------------------------------


def test_audit_flow_extend_once_clamp_negative_and_disabled() -> None:
    """GameCommands.cs:169-183 与 Werewolf.cs:2524-2529：延长秒数按 MaxExtend
    截断、负数仅管理员、未开启 AllowExtend 时普通玩家被拒、每人限一次。"""
    engine, clock = _engine()
    rules = ruleset_official(allow_extend=True, max_extend=60)
    room = engine.create_room("audit-extend", rules)
    engine.join(room, "u1", "甲")
    engine.join(room, "u2", "乙")
    base = room.stage_deadline

    engine.extend_time(room, "u1", 999)
    assert room.stage_deadline == base + timedelta(seconds=60)

    with pytest.raises(GameRuleError, match="已经延长过一次"):
        engine.extend_time(room, "u1", 30)

    with pytest.raises(GameRuleError, match="只有管理员可以缩短"):
        engine.extend_time(room, "u2", -30)

    disabled = engine.create_room("audit-extend-off", ruleset_official(allow_extend=False))
    engine.join(disabled, "u1", "甲")
    with pytest.raises(GameRuleError, match="未开启"):
        engine.extend_time(disabled, "u1", 30)


# ---------------------------------------------------------------------------
# 指令状态矩阵（服务层）
# ---------------------------------------------------------------------------


def test_audit_flow_service_command_matrix_across_states() -> None:
    """Werewolf.cs:689-693,882,833-839 与 service.py：无房间/等待/夜晚各状态下
    群指令的接受与拒绝结果，以及计时器驱动的真实开局。"""
    engine, clock = _engine(seed=11)
    store = _FakeStore()
    app = GameApplication(store, rules=ruleset_official(join_seconds=10), engine=engine)

    run = lambda event: asyncio.run(app.handle_event(event))

    # 无房间：加入 / 投票 / 状态查询全部得到明确拒绝。
    assert "没有活动房间" in run(_group_event("m1", "/join", "u1"))[0].text
    assert "没有活动房间" in run(_group_event("m2", "投票 1", "u1"))[0].text

    # 创建房间后重复创建不报错，官方只重新展示加入入口（Commands/Helpers.cs:118-142）。
    assert "等待玩家加入" in run(_group_event("m3", "/startgame", "u0"))[0].text
    assert "/join" in run(_group_event("m4", "/startgame", "u0"))[0].text

    for index in range(1, 6):
        text = run(_group_event(f"j{index}", "/join", f"u{index}"))[0].text
        assert "加入游戏" in text

    # 等待阶段不能投票、不能提交夜间行动。
    assert "不是投票阶段" in run(_group_event("m5", "投票 1", "u1"))[0].text

    # 计时器到点后按人数条件真正开局（Werewolf.cs:504-624）。
    clock.advance(10)
    assert asyncio.run(app.process_due_rooms()) == 1
    room = store.rooms["audit-group"]
    assert room.phase == GamePhase.NIGHT and room.day == 1
    assert all(player.role is not None for player in room.players)

    # 开局后：leave / cancel（等待期指令）与 extend 全部被拒，房间保持不变。
    assert "开始后不能退出" in run(_group_event("m6", "/leave", "u1"))[0].text
    assert "等待入场时" in run(_group_event("m7", "/extend 30", "u1"))[0].text
    assert "不是投票阶段" in run(_group_event("m8", "投票 2", "u1"))[0].text
    # 进行中的对局里再发 /startgame：官方 ShowJoinButton 因 !IsJoining 静默返回，
    # 本项目改为明确告知当前阶段——不允许出现「指令被吞掉、玩家完全无感知」的情况。
    reply = run(_group_event("m9", "/startgame", "u1"))
    assert "已经开始" in reply[0].text and "/status" in reply[0].text
    assert store.rooms["audit-group"].phase == GamePhase.NIGHT

    # 群聊里发夜间行动会被要求私聊（官方夜间行动只在 PM 菜单中）。
    assert "私聊" in run(_group_event("m10", "狼人 2", "u1"))[0].text

    # 结束后：房间视为不活跃，可重新 /startgame（官方 Program.RemoveGame 后 NoGame）。
    room.phase = GamePhase.FINISHED
    store.rooms["audit-group"] = room
    text = run(_group_event("m11", "/startgame", "u0"))[0].text
    assert "等待玩家加入" in text
    assert store.rooms["audit-group"].phase == GamePhase.LOBBY


def test_audit_flow_group_config_values_enter_new_room_rules() -> None:
    """Werewolf.cs:204-212,2567,2829,3003：群设置（时长/模式/开关/禁用角色）
    在开局时读入并决定规则流程。"""
    store = _FakeStore(group_config={
        "night_seconds": 45,
        "day_seconds": 10,
        "vote_seconds": 20,
        "secret_lynch": True,
        "show_roles_on_death": False,
        "thief_full": True,
        "disabled_roles": [Role.ARSONIST.value, Role.TANNER.value],
        "mode": GameMode.CHAOS.value,
    })
    app = GameApplication(store, rules=ruleset_official())

    asyncio.run(app.handle_event(_group_event("cfg1", "/startgame", "u0")))
    rules = store.rooms["audit-group"].rules

    assert rules.night_seconds == 45
    assert rules.day_seconds == 10
    assert rules.vote_seconds == 20
    assert rules.secret_lynch is True
    assert rules.show_roles_on_death is False
    assert rules.thief_full is True
    assert set(rules.disabled_roles) == {Role.ARSONIST.value, Role.TANNER.value}
    assert rules.mode == GameMode.CHAOS


def test_audit_flow_invalid_group_config_is_rejected() -> None:
    """Group.cs / GroupConfig.cs 只允许既有配置项与合法取值；
    Python 侧必须拒绝未知项、非法布尔、非法模式与破坏官方下限的数值。"""
    with pytest.raises(GameRuleError, match="未知群规则项目"):
        GameApplication._update_group_config({}, "not_a_field=1")
    with pytest.raises(GameRuleError, match="true 或 false"):
        GameApplication._update_group_config({}, "secret_lynch=maybe")
    with pytest.raises(GameRuleError, match="Normal 或 Chaos"):
        GameApplication._update_group_config({}, "mode=Wild")
    with pytest.raises(GameRuleError, match="None、Living 或 All"):
        GameApplication._update_group_config({}, "show_roles_end=Some")
    values = GameApplication._update_group_config({}, "min_players=0")
    with pytest.raises(ValueError):
        GameApplication._apply_group_config(ruleset_official(), values)
    values = GameApplication._update_group_config({}, "night_seconds=-5")
    with pytest.raises(ValueError):
        GameApplication._apply_group_config(ruleset_official(), values)


def test_audit_flow_default_rules_match_settings_cs() -> None:
    """Settings.cs:71-149：MinPlayers=5、MaxPlayers=35、TimeDay=60、TimeNight=90、
    TimeLynch=90、GameJoinTime=180、RandomLynch=false。"""
    rules = ruleset_official()
    assert rules.min_players == 5
    assert rules.max_players == 35
    assert rules.day_seconds == 60
    assert rules.night_seconds == 90
    assert rules.vote_seconds == 90
    assert rules.join_seconds == 180
    assert rules.random_lynch is False
    assert rules.mode == GameMode.NORMAL


# ---------------------------------------------------------------------------
# 7.2-7.5 快照、恢复与损坏数据
# ---------------------------------------------------------------------------


def test_audit_flow_snapshot_roundtrip_preserves_every_player_field() -> None:
    """IPlayer.cs:16-164：官方对局内玩家状态字段在 Python 快照中全量保存；
    round-trip 后逐字段一致。"""
    clock = _clock()
    room = _room(
        [Role.WOLF, Role.GUNNER, Role.HARLOT, Role.MAYOR, Role.CULTIST],
        clock,
        phase=GamePhase.VOTE,
        day=3,
    )
    sample = room.players[1]
    sample.original_role = Role.VILLAGER
    sample.alive = False
    sample.lover_id = "u3"
    sample.role_model = "u1"
    sample.changed_roles_count = 2
    sample.has_used_ability = True
    sample.won = True
    sample.fled = True
    sample.frozen = True
    sample.bullet_count = 1
    sample.drunk = True
    sample.day_cult = 2
    sample.killed_by = "u1"
    sample.kill_method = "Eat"
    sample.choice = "u3"
    sample.choice2 = "u4"
    sample.non_vote_count = 1
    sample.has_been_voted = True
    sample.was_saved_last_night = True
    sample.bitten = True
    sample.doused = True
    sample.burning = True
    sample.died_last_night = True
    sample.vote_weight = 2
    sample.votes_received = 3
    sample.has_revealed = True
    sample.is_frozen = True
    sample.action_day = 3
    sample.metadata = {"pending_hunt": True, "death_sequence": 2}
    room.votes = {"u1": "u2", "u3": None}
    room.vote_round = 2
    room.night_actions["u1"] = NightAction("u1", QuestionType.KILL.value, "u2", 3, second_target_id="u3")
    room.day_actions["u2"] = NightAction("u2", QuestionType.SHOOT.value, "u5", 3)
    room.statistics = {"wolf_cub_killed": True, "death_sequence": 2}
    room.settled_boundaries = ["night-3"]
    room.state_version = 17
    room.winners = ("u2",)
    room.winner = Team.WOLF

    restored = GameRoom.from_snapshot(room.snapshot())

    for field in dataclasses.fields(Player):
        assert getattr(restored.players[1], field.name) == getattr(sample, field.name), field.name
    assert restored.snapshot() == room.snapshot()
    assert restored.phase == GamePhase.VOTE and restored.day == 3
    assert restored.votes == {"u1": "u2", "u3": None}
    assert restored.night_actions["u1"].second_target_id == "u3"
    assert restored.day_actions["u2"].target_id == "u5"
    assert restored.stage_deadline == room.stage_deadline


@pytest.mark.parametrize("phase", [GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE])
def test_audit_flow_snapshot_resume_matches_original_progression(phase: GamePhase) -> None:
    """7.4：夜晚/白天/投票任一阶段中断后，从快照恢复的房间在超时结算时
    与未中断的房间产生完全一致的事件与状态。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=phase,
        day=2,
    )
    if phase == GamePhase.VOTE:
        room.votes = {"u2": "u1", "u3": "u1", "u4": "u1", "u5": "u1", "u1": "u2"}
    restored = GameRoom.from_snapshot(room.snapshot())

    clock.advance(90)
    resumed_engine = GameRoomEngine(clock=clock)
    # 官方取串时用全局 `Program.R` 在多个 <value> 里随机挑一条
    # （Werewolf.cs:396-409，例如 NoAttack 有三条文案），两次独立运行本就不保证同文案。
    # 这里对两次结算固定同一随机种子，只比较流程与状态的等价性。
    random.seed(20240501)
    original_events = engine.on_timeout(room)
    random.seed(20240501)
    restored_events = resumed_engine.on_timeout(restored)

    assert [event.kind for event in restored_events] == [event.kind for event in original_events]
    assert [event.text for event in restored_events] == [event.text for event in original_events]
    assert restored.phase == room.phase
    assert restored.day == room.day
    assert restored.state_version == room.state_version
    assert [player.alive for player in restored.players] == [player.alive for player in room.players]


def test_audit_flow_corrupt_or_missing_snapshot_fails_safely() -> None:
    """7.5：缺失或损坏的保存数据不会被静默接受——快照解析抛错，
    服务层把异常转化为固定的失败回复而不崩溃。"""
    from infrastructure.db import _snapshot_value

    with pytest.raises(KeyError):
        GameRoom.from_snapshot({})
    with pytest.raises(ValueError):
        _snapshot_value("[1, 2, 3]")

    clock = _clock()
    good = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER], clock)
    broken = good.snapshot()
    broken["phase"] = "banquet"
    with pytest.raises(ValueError):
        GameRoom.from_snapshot(broken)

    class _BrokenStore(_FakeStore):
        async def get_room(self, session_id: str):
            raise ValueError("房间数据格式不正确")

    app = GameApplication(_BrokenStore(), rules=ruleset_official())
    messages = asyncio.run(app.handle_event(_group_event("broken1", "/join", "u1")))
    assert "服务暂时无法处理该操作" in messages[0].text


def test_audit_flow_timer_skips_room_deleted_between_list_and_load() -> None:
    """7.4：计时器扫描到的房间若在处理前被删除，应安全跳过而非报错。"""
    engine, clock = _engine()

    class _VanishingStore(_FakeStore):
        async def get_room(self, session_id: str):
            return None

    vanishing = _VanishingStore()
    room = engine.create_room("audit-group", ruleset_official(join_seconds=1))
    engine.join(room, "u1", "甲")
    vanishing.rooms[room.session_id] = room
    clock.advance(1)
    app = GameApplication(vanishing, rules=ruleset_official(), engine=engine)
    # list_active_rooms 仍返回房间，但 get_room 已经取不到 → 处理数为 0，且不抛错。
    assert asyncio.run(app.process_due_rooms()) == 0


# ---------------------------------------------------------------------------
# 差异（按官方行为断言，strict xfail）
# ---------------------------------------------------------------------------


def test_audit_flow_flee_does_not_kill_lover() -> None:
    """Werewolf.cs:849-861,5602-5607：开局后逃跑只标记本人死亡，
    不调用恋人殉情等后续连锁。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.SEER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
    )
    room.players[1].lover_id = "u3"
    room.players[2].lover_id = "u2"

    engine.flee(room, "u2")

    assert room.players[1].alive is False
    assert room.players[2].alive is True  # 官方：恋人不殉情


def test_audit_flow_fleeing_wolf_cub_does_not_grant_second_kill() -> None:
    """Werewolf.cs:5602-5626：KillPlayer 对 Idle/Flee 提前 return，
    狼崽逃跑不会给狼群下一夜的双杀。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.WOLF_CUB, Role.VILLAGER, Role.SEER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
    )

    engine.flee(room, "u2")

    assert room.statistics.get("wolf_cub_killed") is None


def test_audit_flow_pacifist_peace_works_during_lynch() -> None:
    """Werewolf.cs:913-926：和平按钮在游戏运行中的任何时刻有效；
    LynchCycle 每秒检查 _pacifistUsed 并立即结束处决。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.VOTE,
    )

    events = engine.submit_day_action(room, "u2", "和平", "yes")

    assert any(event.kind == "pacifist" for event in events)
    # LynchCycle 立即 return：公告 PacifistNoLynchNow 并直接进入下一夜。
    assert any(event.kind == "pacifist_vote" for event in events)
    assert room.phase is GamePhase.NIGHT
    assert room.statistics.get("pacifist_used") is None
    assert room.vote_history[-1]["eliminated"] == []


def test_audit_flow_mayor_can_reveal_at_night() -> None:
    """Werewolf.cs:899-911：Mayor reveal 分支在 CurrentQuestion 校验之前，
    只要求存活与未用过能力，夜晚按下同样生效。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.MAYOR, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.NIGHT,
        day=2,
    )

    events = engine.submit_day_action(room, "u2", "市长", None)

    assert any(event.kind == "mayor_revealed" for event in events)
    assert room.players[1].vote_weight == 2


def test_audit_flow_double_lynch_second_round_has_no_idle_penalty() -> None:
    """Werewolf.cs:2661-2684：NonVote 递增仅发生在 lynchAttempt < 2 时；
    捣乱者触发的第二轮处决全员沉默也不会累计未投票。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.WOLF, Role.TROUBLEMAKER, Role.SEER,
         Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
        day=1,
    )
    engine.submit_day_action(room, "u3", "捣乱", "yes")

    clock.advance(120)
    engine.on_timeout(room)
    assert room.phase == GamePhase.VOTE

    engine.submit_vote(room, "u1", "4")
    for user_id in ["u2", "u3", "u4", "u5", "u6"]:
        engine.submit_vote(room, user_id, "1")
    events = engine.submit_vote(room, "u7", "1")

    # 第一轮处决 u1 后开启第二轮投票。
    assert not room.players[0].alive
    assert room.phase == GamePhase.VOTE
    assert any(event.kind == "vote_started" for event in events)

    # 第二轮全员沉默 → 官方不累计 NonVote，也无人被挂机处死。
    clock.advance(90)
    engine.on_timeout(room)
    living = [player for player in room.players if player.user_id != "u1"]
    assert all(player.alive for player in living)
    assert all(player.non_vote_count == 0 for player in living)


def test_audit_flow_join_beyond_group_max_is_still_accepted() -> None:
    """Werewolf.cs:731-735：AddPlayer 只有 Players.Count >= 35 才拒绝；
    达到群配置 MaxPlayers 仅触发 KillTimer，随后的抢先加入仍被接受。"""
    engine, clock = _engine()
    room = engine.create_room("audit-groupmax", replace(ruleset_official(), max_players=5))
    for index in range(1, 6):
        engine.join(room, f"u{index}", f"玩家{index}")

    events = engine.join(room, "u6", "玩家6")

    assert len(room.players) == 6
    assert any(event.kind == "player_joined" for event in events)


def test_audit_flow_total_waiting_time_capped_at_max_join_time() -> None:
    """Werewolf.cs:479：i = Max(i - secondsToAdd, GameJoinTime - MaxJoinTime)，
    从开局起等待总时长不会超过 300 秒。"""
    engine, clock = _engine()
    room = engine.create_room("audit-maxjoin", ruleset_official(allow_extend=True, max_extend=60))
    for index in range(1, 7):
        engine.join(room, f"u{index}", f"玩家{index}")
    for index in range(1, 6):
        engine.extend_time(room, f"u{index}", 60)

    total = (room.stage_deadline - room.stage_started_at).total_seconds()
    assert total <= 300


def test_audit_flow_extend_by_absent_admin_is_ignored() -> None:
    """Werewolf.cs:2521-2535：ExtendTime 先按用户 Id 查玩家，
    查不到就什么也不做，即使请求来自管理员。"""
    engine, clock = _engine()
    room = engine.create_room("audit-absent-admin", ruleset_official())
    engine.join(room, "u1", "甲")
    before = room.stage_deadline

    engine.extend_time(room, "outsider-admin", 30, admin=True)

    assert room.stage_deadline == before


def test_audit_flow_random_mode_group_setting_exists() -> None:
    """GroupConfig.cs:24 与 Werewolf.cs:177-200：randommode 是可编辑群设置，
    开启后每局随机决定模式、ThiefFull、SecretLynch 等机制。"""
    values = GameApplication._update_group_config({}, "random_mode=true")
    assert values["random_mode"] is True
