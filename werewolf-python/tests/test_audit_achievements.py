"""独立复刻审计（任务 12）：成就系统。

官方期望值来源（唯一）：提交 ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7。
- `Werewolf Node/Werewolf.cs:6047-6062` AddAchievement（即时解锁，一条私聊，已有则静默）
- `Werewolf Node/Werewolf.cs:5792-5930` UpdateAchievements（结算批量判定，一条汇总私聊）
- `Werewolf Node/Werewolf.cs:5720-5731` CheckLongHaul
- `Werewolf Node/Werewolf.cs:613-618` 不足 20 人时禁用 Inconspicuous
- `Werewolf Control/Commands/GeneralCommands.cs:38-44` /achv
- `Werewolf Control/Models/InlineCommand.cs:59-74` 成就数量与列表展示
- `Database/AchievementsReworked.cs` 枚举值 0-102 与 Display(Name/Description)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from application.contracts import SessionType
from application.service import GameApplication
from domain.achievements import (
    ACHIEVEMENT_INFO,
    Achievement,
    achievement_description,
    achievement_name,
    unlock_text,
)
from domain.engine import GameRoomEngine
from domain.fixtures import FrozenClock
from domain.models import GameMode, GamePhase, GameRoom, KillMethod, Player, Role, Team
from domain.rules import ruleset_official

from tests.test_audit_qq_commands import AuditStore, c2c_event, group_event, texts


def run(coro):
    return asyncio.run(coro)


def _room(roles, *, phase=GamePhase.DAY, rules=None, day=1) -> GameRoom:
    players = [
        Player(f"u{index}", f"P{index}", index, role=role)
        for index, role in enumerate(roles, 1)
    ]
    return GameRoom(
        "audit-achievements",
        rules or ruleset_official(),
        phase=phase,
        players=players,
        day=day,
    )


def _unlocks(events) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for event in events:
        if event.kind == "achievements_unlocked":
            result[event.target_user_id] = list(event.metadata["achievements"])
    return result


# ---------------------------------------------------------------------------
# 枚举定义（Database/AchievementsReworked.cs）
# ---------------------------------------------------------------------------


def test_achievement_enum_covers_official_range() -> None:
    """官方枚举 0-102 连续取值，位下标等于枚举值。"""
    values = sorted(int(item) for item in Achievement)
    assert values == list(range(0, 103))
    assert int(Achievement.NONE) == 0
    assert int(Achievement.WELCOME_TO_HELL) == 1
    assert int(Achievement.AM_I_HALLUCINATING) == 102


def test_every_achievement_has_official_name_and_description() -> None:
    """AchievementsReworked 每项都带 Display(Name/Description)。"""
    for item in Achievement:
        assert item in ACHIEVEMENT_INFO
        assert achievement_name(item)
        assert achievement_description(item)


def test_unlock_text_matches_add_achievement_format() -> None:
    """Werewolf.cs:6060 —— "成就解锁！\\n名称\\n描述"（官方文案的中文版）。"""
    assert unlock_text(Achievement.INTROVERT) == (
        "成就解锁！\n社恐\n参与一局 5 人的游戏"
    )


# ---------------------------------------------------------------------------
# AddAchievement（Werewolf.cs:6047-6062）
# ---------------------------------------------------------------------------


def test_add_achievement_sends_private_message_once() -> None:
    """官方先查 BitArray，已有则 return，不再发第二条私聊。"""
    engine = GameRoomEngine()
    player = Player("u1", "P1", 1, role=Role.VILLAGER)
    first = engine._add_achievement(player, Achievement.FIRST_STONE)
    second = engine._add_achievement(player, Achievement.FIRST_STONE)
    assert len(first) == 1 and second == []
    assert first[0].public is False and first[0].target_user_id == "u1"
    assert first[0].metadata["achievement"] == int(Achievement.FIRST_STONE)
    assert player.achievements == [int(Achievement.FIRST_STONE)]


def test_check_long_haul_grants_only_to_living_non_fled_once() -> None:
    """Werewolf.cs:5720-5731 —— 满一小时后存活且未逃跑者解锁，且只触发一次。"""
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    engine = GameRoomEngine(clock=clock)
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.statistics["game_started_at"] = clock.now().isoformat()
    room.players[1].alive = False
    room.players[2].fled = True

    assert engine._check_long_haul(room) == []  # 未满一小时
    clock.advance(3600)
    granted = engine._check_long_haul(room)
    assert {event.target_user_id for event in granted} == {"u1", "u4", "u5"}
    assert engine._check_long_haul(room) == []  # _longHaulReached


# ---------------------------------------------------------------------------
# UpdateAchievements（Werewolf.cs:5792-5930）
# ---------------------------------------------------------------------------


def test_update_achievements_grants_welcome_to_hell_and_introvert() -> None:
    """5832-5833 WelcomeToHell 无条件；5846-5847 Introvert 需正好 5 人。"""
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    unlocked = _unlocks(GameRoomEngine()._update_achievements(room))
    assert set(unlocked) == {"u1", "u2", "u3", "u4", "u5"}
    for values in unlocked.values():
        assert int(Achievement.WELCOME_TO_HELL) in values
        assert int(Achievement.INTROVERT) in values


def test_update_achievements_skips_fled_players() -> None:
    """5801 —— `Players.Where(x => !x.Fled)`，逃跑者不参与结算。"""
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.players[1].fled = True
    unlocked = _unlocks(GameRoomEngine()._update_achievements(room))
    assert "u2" not in unlocked
    assert room.players[1].achievements == []


def test_update_achievements_does_not_repeat_existing_flags() -> None:
    """5832 —— `!ach2.HasFlag(...)`：已解锁的不进 newAch2，也不出现在通知里。"""
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.players[0].achievements = [int(Achievement.WELCOME_TO_HELL)]
    unlocked = _unlocks(GameRoomEngine()._update_achievements(room))
    assert int(Achievement.WELCOME_TO_HELL) not in unlocked["u1"]
    assert int(Achievement.INTROVERT) in unlocked["u1"]


def test_update_achievements_chaos_and_masochist_and_lifetime_counts() -> None:
    """5834-5835 WelcomeToAsylum；5856-5857 Masochist；5850-5865 GamePlayers 计数。"""
    room = _room(
        [Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.TANNER],
        rules=ruleset_official(mode=GameMode.CHAOS),
    )
    room.players[4].won = True
    # 官方开局即写 GamePlayers，因此本局计入：99 + 1 = 100。
    room.statistics["lifetime_stats"] = {"u1": {"games": 99, "survived": 99}}
    unlocked = _unlocks(GameRoomEngine()._update_achievements(room))
    assert int(Achievement.WELCOME_TO_ASYLUM) in unlocked["u1"]
    assert int(Achievement.MASOCHIST) in unlocked["u5"]
    assert int(Achievement.DEDICATED) in unlocked["u1"]
    assert int(Achievement.SURVIVALIST) in unlocked["u1"]
    assert int(Achievement.DEDICATED) not in unlocked["u2"]


def test_update_achievements_death_village_when_nobody_won() -> None:
    """5904-5905 —— `Players.Count(x => x.Won) == 0` 时全员 DeathVillage。"""
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    unlocked = _unlocks(GameRoomEngine()._update_achievements(room))
    assert all(int(Achievement.DEATH_VILLAGE) in values for values in unlocked.values())


def test_update_achievements_single_aggregated_message_per_player() -> None:
    """5922-5927 —— 每人只发一条“新解锁成就！”汇总私聊。"""
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    events = [
        event
        for event in GameRoomEngine()._update_achievements(room)
        if event.target_user_id == "u1"
    ]
    assert len(events) == 1
    text = events[0].text
    assert text.startswith("新解锁成就！\n")
    assert achievement_name(Achievement.WELCOME_TO_HELL) in text
    assert achievement_description(Achievement.INTROVERT) in text
    assert events[0].public is False


def test_start_disables_inconspicuous_below_twenty_players() -> None:
    """Werewolf.cs:613-618 —— 不足 20 人时开局即置 HasBeenVoted。"""
    room = _room(
        [Role.VILLAGER] * 5,
        phase=GamePhase.LOBBY,
    )
    for player in room.players:
        player.role = None
    GameRoomEngine().start(room, "u1")
    assert all(player.has_been_voted for player in room.players)
    unlocked = _unlocks(GameRoomEngine()._update_achievements(room))
    assert all(int(Achievement.INCONSPICUOUS) not in values for values in unlocked.values())


def test_finish_runs_batch_achievements_after_winner_flags() -> None:
    """Werewolf.cs:4929 —— DoGameEnd 在写完 Won 之后调用 UpdateAchievements。"""
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.TANNER])
    # Werewolf.cs:4667-4690 —— 坦纳需被处决才算胜利。
    tanner = room.players[4]
    tanner.alive = False
    tanner.kill_method = KillMethod.LYNCH.value
    events = GameRoomEngine().finish(room, Team.TANNER)
    unlocked = _unlocks(events)
    assert tanner.won is True
    assert int(Achievement.MASOCHIST) in unlocked["u5"]


# ---------------------------------------------------------------------------
# /achv（GeneralCommands.cs:38-44 + InlineCommand.cs:59-74）
# ---------------------------------------------------------------------------


class AchievementStore(AuditStore):
    """带成就读写的存储替身，对应 Players.NewAchievements。"""

    def __init__(self, rooms=(), achievements=None, stats=None):
        super().__init__(rooms)
        self.achievements = dict(achievements or {})
        self.stats = dict(stats or {})

    async def get_player_achievements(self, user_id):
        return sorted(self.achievements.get(user_id, []))

    async def lifetime_stats(self, user_ids):
        return {key: self.stats[key] for key in user_ids if key in self.stats}


def test_achv_without_unlocks_reports_empty() -> None:
    app = GameApplication(AchievementStore())
    text = texts(run(app.handle_event(c2c_event("/achv", user="u1", name="甲"))))
    assert "还没有解锁任何成就" in text


def test_achv_lists_unlocked_names_and_descriptions_in_private() -> None:
    """InlineCommand.cs:71-72 —— 展示解锁数量；QQ 无 inline，故直接列出名称与描述。"""
    store = AchievementStore(
        achievements={"u1": [int(Achievement.WELCOME_TO_HELL), int(Achievement.INTROVERT)]}
    )
    app = GameApplication(store)
    text = texts(run(app.handle_event(c2c_event("/achv", user="u1", name="甲"))))
    assert "已解锁 2 个成就" in text
    assert achievement_name(Achievement.WELCOME_TO_HELL) in text
    assert achievement_description(Achievement.INTROVERT) in text


def test_achv_in_group_sends_private_and_group_notice() -> None:
    """官方结果只对本人可见；QQ 版沿用 /myidles 的 SentPrivate 模式。"""
    store = AchievementStore(achievements={"u1": [int(Achievement.WELCOME_TO_HELL)]})
    app = GameApplication(store)
    messages = run(app.handle_event(group_event("/achv", user="u1", name="甲")))
    private = [m for m in messages if m.target.session_type != SessionType.GROUP]
    public = [m for m in messages if m.target.session_type == SessionType.GROUP]
    assert len(private) == 1 and len(public) == 1
    assert achievement_name(Achievement.WELCOME_TO_HELL) in private[0].text
    assert "私聊" in public[0].text


# ---------------------------------------------------------------------------
# 开局基线注入（对应官方每次判定都读数据库）
# ---------------------------------------------------------------------------


def test_start_loads_achievement_baseline_and_lifetime_stats() -> None:
    """官方 AddAchievement/UpdateAchievements 每次都读 NewAchievements 与 GamePlayers；
    QQ 版在开局时一次性注入，保证判定结果一致。"""
    room = GameRoom(
        "g1",
        ruleset_official(),
        phase=GamePhase.LOBBY,
        players=[Player(f"u{index}", f"玩家u{index}", index) for index in range(1, 6)],
    )
    store = AchievementStore(
        rooms=[room],
        achievements={"u1": [int(Achievement.WELCOME_TO_HELL)]},
        stats={"u1": {"games": 499, "survived": 10}},
    )
    app = GameApplication(store, admin_user_ids=("admin",))
    run(app.handle_event(group_event("/forcestart", user="admin")))
    run(app.process_due_rooms())
    started = store.rooms["g1"]
    assert started.phase == GamePhase.NIGHT
    player = next(item for item in started.players if item.user_id == "u1")
    assert int(Achievement.WELCOME_TO_HELL) in player.achievements
    assert started.statistics["lifetime_stats"]["u1"]["games"] == 499
