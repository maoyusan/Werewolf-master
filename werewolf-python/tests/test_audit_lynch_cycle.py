"""独立复刻审计（任务 3）：GameTimer 主循环、DayCycle 与 LynchCycle 的阶段边界。

官方期望值来源（唯一）：
`work/upstream-official/Werewolf for Telegram/Werewolf Node/Werewolf.cs`
（commit ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7）
- GameTimer 主循环（GameDay++ → CheckRoleChanges → CheckLongHaul → NightCycle
  → DayCycle → LynchCycle）：614-635
- DayCycle（timeToAdd 公式、白天结束时保留 Mayor/Pacifist 菜单）：2815-2972
- LynchCycle（和平立即 return、lynchAttempt<2 才累计 NonVote、双重处决）：2537-2812
- HandleReply 的“随时可用”能力（Mayor/Pacifist/Troublemaker 互相覆盖）：878-1000
"""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from domain.engine import GameRoomEngine, GameRuleError
from domain.fixtures import FrozenClock
from domain.models import GamePhase, GameRoom, Player, Role
from domain.rules import ruleset_official


def _engine(seed: int = 7) -> tuple[GameRoomEngine, FrozenClock]:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    return GameRoomEngine(rng=random.Random(seed), clock=clock), clock


def _room(
    roles: list[Role],
    clock: FrozenClock,
    *,
    phase: GamePhase = GamePhase.NIGHT,
    day: int = 1,
    rules=None,
) -> GameRoom:
    room = GameRoom(
        "audit-lynch",
        rules or ruleset_official(),
        phase=phase,
        day=day,
        stage_started_at=clock.now(),
        stage_deadline=clock.now() + timedelta(seconds=90),
    )
    room.players = [
        Player(f"u{index}", f"玩家{index}", index, role=role)
        for index, role in enumerate(roles, 1)
    ]
    return room


def kinds(events) -> list[str]:
    return [event.kind for event in events]


# ---------------------------------------------------------------------------
# DayCycle —— timeToAdd 公式（Werewolf.cs:2822）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "alive,expected_extra",
    [(5, 60), (10, 60), (15, 60), (20, 90), (25, 120)],
)
def test_day_length_matches_official_time_to_add(alive: int, expected_extra: int) -> None:
    """`Math.Max(((Players.Count(x => !x.IsDead) / 5) - 1) * 30, 60)`。"""
    engine, clock = _engine()
    room = _room([Role.WOLF] + [Role.VILLAGER] * (alive - 1), clock)

    engine._finish_or_start_day(room)

    assert room.phase is GamePhase.DAY
    span = (room.stage_deadline - room.stage_started_at).total_seconds()
    assert span == room.rules.day_seconds + expected_extra


# ---------------------------------------------------------------------------
# GameTimer 主循环顺序（Werewolf.cs:614-635、637）
# ---------------------------------------------------------------------------

def test_check_long_haul_runs_before_each_of_the_three_cycles() -> None:
    """CheckLongHaul 在 NightCycle/DayCycle/LynchCycle 之前各调用一次。"""
    engine, clock = _engine()
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER], clock)
    original = engine._check_long_haul
    calls: list[int] = []

    def counting(target_room: GameRoom):
        calls.append(target_room.day)
        return original(target_room)

    engine._check_long_haul = counting  # type: ignore[method-assign]

    engine._finish_or_start_day(room)   # → DayCycle 之前
    engine.start_vote(room)             # → LynchCycle 之前
    engine.resolve_vote(room)           # → 下一轮 NightCycle 之前

    assert len(calls) == 3


def test_game_day_increments_only_after_the_lynch() -> None:
    """GameDay++ 位于 while 循环开头：夜→昼→处决共用同一个 GameDay。"""
    engine, clock = _engine()
    room = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER], clock)

    engine._finish_or_start_day(room)
    assert room.day == 1
    engine.start_vote(room)
    assert room.day == 1
    engine.resolve_vote(room)
    assert room.day == 2
    assert room.phase is GamePhase.NIGHT
    # LynchCycle 结束后 lynchAttempt 是局部变量，下一轮重新从 1 开始。
    assert "lynch_attempt" not in room.statistics


def test_double_lynch_second_round_keeps_the_same_game_day() -> None:
    """Werewolf.cs:2543-2811 —— do/while 的第二次处决仍在同一个 GameDay 内。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.TROUBLEMAKER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
    )
    engine.submit_day_action(room, "u2", "捣乱", "yes")

    engine.start_vote(room)
    events = engine.resolve_vote(room)

    assert "vote_started" in kinds(events)
    assert room.phase is GamePhase.VOTE
    assert room.day == 1
    assert room.statistics["lynch_attempt"] == 2
    assert "double_lynch" not in room.statistics


# ---------------------------------------------------------------------------
# DayCycle 结束时保留的菜单（Werewolf.cs:2841-2851）
# ---------------------------------------------------------------------------

def test_mayor_and_pacifist_menus_survive_into_the_lynch() -> None:
    """白天结束时除 QuestionType.Mayor / Pacifist 外的菜单都改为 TimesUp。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.MAYOR, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.VOTE,
    )

    events = engine.submit_day_action(room, "u2", "市长", None)

    assert "mayor_revealed" in kinds(events)
    assert room.players[1].vote_weight == 2


def test_troublemaker_menu_does_not_survive_into_the_lynch() -> None:
    """Troublemaker 菜单在白天结束时被改为 TimesUp，处决阶段无法再按。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.TROUBLEMAKER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.VOTE,
    )

    with pytest.raises(GameRuleError):
        engine.submit_day_action(room, "u2", "捣乱", "yes")


# ---------------------------------------------------------------------------
# LynchCycle 的和平立即中止（Werewolf.cs:2559-2564、2574-2604）
# ---------------------------------------------------------------------------

def test_pacifist_pressed_in_daytime_skips_the_lynch_entirely() -> None:
    """2559-2564：LynchCycle 开局检测到 _pacifistUsed 就 return，处决菜单不发出。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
    )
    engine.submit_day_action(room, "u2", "和平", "yes")

    events = engine.start_vote(room)

    assert "vote_started" not in kinds(events)
    assert "pacifist_vote" in kinds(events)
    assert room.phase is GamePhase.NIGHT
    assert room.day == 2
    assert room.statistics.get("pacifist_used") is None


def test_pacifist_pressed_in_daytime_cancels_the_double_lynch() -> None:
    """Werewolf.cs:917 —— peace 覆盖 trouble，第二轮处决同样不会发生。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.TROUBLEMAKER, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
    )
    engine.submit_day_action(room, "u2", "捣乱", "yes")
    engine.submit_day_action(room, "u3", "和平", "yes")

    events = engine.start_vote(room)

    assert "vote_started" not in kinds(events)
    assert room.phase is GamePhase.NIGHT
    assert "double_lynch" not in room.statistics


def test_pacifist_pressed_during_the_lynch_aborts_it_immediately() -> None:
    """2574-2604：倒计时里按下和平立刻公告并 return，不必等投票时间走完。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.VOTE,
    )
    engine.submit_vote(room, "u1", "u5")
    engine.submit_vote(room, "u3", "u5")

    events = engine.submit_day_action(room, "u2", "和平", "yes")

    assert kinds(events)[:2] == ["pacifist", "pacifist_vote"]
    assert room.phase is GamePhase.NIGHT
    assert room.day == 2
    assert all(player.alive for player in room.players)


def test_pacifist_abort_does_not_tally_votes() -> None:
    """官方在计票循环之前就 return：票数、市长计数、秘密投票播报全部不产生。"""
    rules = replace(
        ruleset_official(), secret_lynch=True, secret_lynch_show_votes=True
    )
    engine, clock = _engine()
    room = _room(
        [Role.MAYOR, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER, Role.WOLF],
        clock,
        phase=GamePhase.VOTE,
        rules=rules,
    )
    engine.submit_day_action(room, "u1", "市长", None)
    engine.submit_vote(room, "u1", "u4")
    engine.submit_vote(room, "u3", "u4")

    events = engine.submit_day_action(room, "u2", "和平", "yes")

    assert "secret_lynch_result" not in kinds(events)
    assert all(player.votes_received == 0 for player in room.players)
    assert room.players[0].mayor_lynch_after_reveal_count == 0
    assert room.vote_history[-1]["counts"] == {}
    assert room.vote_history[-1]["eliminated"] == []


def test_pacifist_abort_does_not_accumulate_non_votes() -> None:
    """LynchCycle 在 NonVote 结算之前 return，本轮沉默不计入闲置。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.VOTE,
    )
    for player in room.players:
        player.non_vote_count = 1

    engine.submit_day_action(room, "u2", "和平", "yes")

    assert [player.non_vote_count for player in room.players] == [1, 1, 1, 1, 1]


def test_pacifist_ability_is_single_use_across_phases() -> None:
    """HasUsedAbility 一旦置位，下一轮处决无法再次触发和平。"""
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
    )
    engine.submit_day_action(room, "u2", "和平", "yes")
    engine.start_vote(room)

    with pytest.raises(GameRuleError):
        engine.submit_day_action(room, "u2", "和平", "yes")


# ---------------------------------------------------------------------------
# NonVote 与 lynchAttempt（Werewolf.cs:2661）
# ---------------------------------------------------------------------------

def test_non_vote_accumulates_only_on_the_first_lynch_attempt() -> None:
    engine, clock = _engine()
    room = _room(
        [Role.WOLF, Role.TROUBLEMAKER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        clock,
        phase=GamePhase.DAY,
    )
    engine.submit_day_action(room, "u2", "捣乱", "yes")
    engine.start_vote(room)

    engine.resolve_vote(room)  # 第一轮全员沉默
    assert [player.non_vote_count for player in room.players] == [1, 1, 1, 1, 1]
    assert room.statistics["lynch_attempt"] == 2

    engine.resolve_vote(room)  # 第二轮全员沉默
    assert [player.non_vote_count for player in room.players] == [1, 1, 1, 1, 1]
    assert room.phase is GamePhase.NIGHT
