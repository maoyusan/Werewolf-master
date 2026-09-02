"""独立复核审计：猎人临终一枪 HunterFinalShot。

期望值全部取自官方 `Werewolf Node/Werewolf.cs:5420-5487`（HunterFinalShot 本体）
以及 5620-5626（KillPlayer 的猎人分支，夜间 delay=True）、4420-4425（夜间死亡的
猎人在所有夜间播报之后、DayCycle 之前统一开枪），commit
ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from domain.engine import GameRoomEngine, GameRuleError, _HUNTER_WINDOW_SECONDS
from domain.fixtures import FrozenClock
from domain.locale import get_locale_string
from domain.models import (
    GamePhase,
    GameRoom,
    KillMethod,
    NightAction,
    Player,
    QuestionType,
    Role,
)
from domain.rules import ruleset_official


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))


def _room(
    roles: list[Role],
    *,
    phase: GamePhase = GamePhase.VOTE,
    day: int = 1,
    rules=None,
) -> tuple[GameRoom, list[Player]]:
    players = [
        Player(f"u{index}", f"P{index}", index, role=role)
        for index, role in enumerate(roles, 1)
    ]
    room = GameRoom(
        "audit-hunter-final-shot",
        rules or ruleset_official(),
        phase=phase,
        players=players,
        day=day,
    )
    return room, players


def _texts(events, kind: str) -> list[str]:
    return [event.text for event in events if event.kind == kind]


# ---------------------------------------------------------------------------
# 菜单文案与 30 秒窗口
# ---------------------------------------------------------------------------


def test_lynched_hunter_gets_official_lynch_menu_and_blocks_the_night() -> None:
    """Werewolf.cs:5439 SendMenu 用 HunterLynchedChoice；5442-5449 30 秒阻塞循环
    独占流程，因此处决后不得立刻进入夜晚。"""
    clock = _clock()
    engine = GameRoomEngine(clock=clock)
    room, players = _room([Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}

    events = engine.resolve_vote(room)

    prompts = [event for event in events if event.kind == "hunter_prompt"]
    assert len(prompts) == 1
    assert prompts[0].public is False and prompts[0].target_user_id == "u2"
    assert get_locale_string("HunterLynchedChoice") in prompts[0].text
    assert prompts[0].metadata["action"] == QuestionType.HUNTER_KILL.value
    # 窗口未关闭前阶段不推进：仍停留在 VOTE，且没有 night_started。
    assert room.phase == GamePhase.VOTE
    assert not _texts(events, "night_started")
    assert room.statistics["hunter_window"] == "night"
    assert room.stage_deadline == clock.value.replace(microsecond=0) + timedelta(
        seconds=_HUNTER_WINDOW_SECONDS
    )


def test_night_killed_hunter_gets_shot_menu_and_blocks_the_day() -> None:
    """Werewolf.cs:5624 夜间死亡 delay=True；4420-4425 播报结束后才开枪，
    因此夜间结算不得直接产出 day_started。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room(
        [Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.NIGHT,
        rules=replace(ruleset_official(), hunter_kill_wolf_chance_base=0),
    )
    room.night_actions["u1"] = NightAction(
        actor_id="u1", action=QuestionType.KILL.value, target_id="u2", day=1
    )

    events = engine.resolve_night(room)

    prompts = [event for event in events if event.kind == "hunter_prompt"]
    assert len(prompts) == 1 and prompts[0].target_user_id == "u2"
    assert get_locale_string("HunterShotChoice") in prompts[0].text
    assert room.phase == GamePhase.NIGHT
    assert not _texts(events, "day_started")
    assert room.statistics["hunter_window"] == "day"


# ---------------------------------------------------------------------------
# 超时 / 跳过 / 命中 三条官方分支
# ---------------------------------------------------------------------------


def test_timeout_uses_official_no_choice_wording_and_resumes_the_flow() -> None:
    """Werewolf.cs:5452-5456 —— Choice == 0：公开播报 HunterNoChoiceLynched，
    并把猎人的私聊菜单改写成 TimesUp；随后流程继续。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room([Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}
    engine.resolve_vote(room)

    events = engine.on_timeout(room)

    assert _texts(events, "hunter_no_choice") == [
        get_locale_string("HunterNoChoiceLynched", None, "P2（2号）")
    ]
    times_up = [event for event in events if event.kind == "times_up"]
    assert len(times_up) == 1
    assert times_up[0].public is False and times_up[0].target_user_id == "u2"
    assert times_up[0].text == get_locale_string("TimesUp")
    # 窗口关闭后继续被暂停的那一段流程：处决后进入夜晚。
    assert "hunter_window" not in room.statistics
    assert players[1].metadata.get("pending_hunt") is None
    assert room.phase == GamePhase.NIGHT
    assert _texts(events, "night_started")


def test_skip_uses_official_skip_wording_and_resumes_the_flow() -> None:
    """Werewolf.cs:5457-5461 —— Choice == -1：公开播报 HunterSkipChoiceLynched。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room([Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}
    engine.resolve_vote(room)

    events = engine.submit_night_action(room, "u2", "猎杀", "跳过")

    assert _texts(events, "hunter_skipped") == [
        get_locale_string("HunterSkipChoiceLynched", None, "P2（2号）")
    ]
    assert all(player.alive for player in players if player.user_id != "u2")
    assert room.phase == GamePhase.NIGHT
    assert _texts(events, "night_started")


def test_night_shot_skip_uses_the_shot_wording() -> None:
    """Werewolf.cs:5460 —— 非处决死亡走 HunterSkipChoiceShot。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room(
        [Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.NIGHT,
        rules=replace(ruleset_official(), hunter_kill_wolf_chance_base=0),
    )
    room.night_actions["u1"] = NightAction(
        actor_id="u1", action=QuestionType.KILL.value, target_id="u2", day=1
    )
    engine.resolve_night(room)

    events = engine.submit_night_action(room, "u2", "猎杀", "跳过")

    assert _texts(events, "hunter_skipped") == [
        get_locale_string("HunterSkipChoiceShot", None, "P2（2号）")
    ]
    assert room.phase == GamePhase.DAY
    assert _texts(events, "day_started")


def test_hit_uses_official_final_shot_wording_and_names_the_shooter() -> None:
    """Werewolf.cs:5464-5467 —— HunterKilledFinalLynched 公开点名猎人与中枪者，
    {2} 位置补身份揭示（ShowRolesDeath 开启时）。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room([Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}
    engine.resolve_vote(room)

    events = engine.submit_night_action(room, "u2", "猎杀", "3")

    died = [event for event in events if event.kind == "player_died"]
    assert died and died[0].metadata["user_id"] == "u3"
    expected = get_locale_string(
        "HunterKilledFinalLynched", None, "P2（2号）", "P3（3号）", ""
    ).strip()
    assert died[0].text.startswith(expected)
    assert "P2（2号）" in died[0].text
    assert players[2].alive is False
    assert players[2].kill_method == KillMethod.HUNTER.value


def test_night_hit_uses_the_shot_wording() -> None:
    """Werewolf.cs:5466 —— 非处决死亡走 HunterKilledFinalShot。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room(
        [Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.NIGHT,
        rules=replace(ruleset_official(), hunter_kill_wolf_chance_base=0),
    )
    room.night_actions["u1"] = NightAction(
        actor_id="u1", action=QuestionType.KILL.value, target_id="u2", day=1
    )
    engine.resolve_night(room)

    events = engine.submit_night_action(room, "u2", "猎杀", "3")

    died = [event for event in events if event.kind == "player_died"]
    expected = get_locale_string(
        "HunterKilledFinalShot", None, "P2（2号）", "P3（3号）", ""
    ).strip()
    assert died and died[0].text.startswith(expected)


# ---------------------------------------------------------------------------
# 长老 / 连锁 / 窗口生命周期
# ---------------------------------------------------------------------------


def test_killing_the_wise_elder_is_announced_publicly() -> None:
    """Werewolf.cs:5473 —— HunterKilledWiseElder 走 SendWithQueue，是公开播报，
    不是私聊；同时猎人降级为村民。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room(
        [Role.WOLF, Role.HUNTER, Role.WISE_ELDER, Role.VILLAGER, Role.VILLAGER]
    )
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}
    engine.resolve_vote(room)

    events = engine.submit_night_action(room, "u2", "猎杀", "3")

    elder_events = [event for event in events if event.kind == "hunter_killed_elder"]
    assert len(elder_events) == 1
    assert elder_events[0].public is True
    assert elder_events[0].target_user_id is None
    assert elder_events[0].text == get_locale_string(
        "HunterKilledWiseElder", None, "P2（2号）", "P3（3号）"
    )
    assert players[1].role == Role.VILLAGER


def test_hunter_shooting_a_hunter_reopens_the_window() -> None:
    """Werewolf.cs:5481-5486 —— Domino：被打死的猎人自己也会触发 HunterFinalShot，
    官方以递归方式再阻塞 30 秒，端口对应续窗口而不是直接推进阶段。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room([Role.WOLF, Role.HUNTER, Role.HUNTER, Role.VILLAGER, Role.VILLAGER])
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}
    engine.resolve_vote(room)

    events = engine.submit_night_action(room, "u2", "猎杀", "3")

    assert room.statistics.get("hunter_window") == "night"
    assert room.phase == GamePhase.VOTE
    assert not _texts(events, "night_started")
    assert players[2].metadata["pending_hunt"] == {
        "method": KillMethod.HUNTER.value, "lynched": False
    }
    # 第二位猎人放弃后，被暂停的流程才继续。
    follow_up = engine.submit_night_action(room, "u3", "猎杀", "跳过")
    assert _texts(follow_up, "night_started")
    assert room.phase == GamePhase.NIGHT


def test_submitting_without_a_pending_shot_is_rejected() -> None:
    engine = GameRoomEngine(clock=_clock())
    room, players = _room([Role.WOLF, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    with pytest.raises(GameRuleError):
        engine.submit_night_action(room, "u2", "猎杀", "3")


def test_window_does_not_survive_a_finished_game() -> None:
    """猎人开枪直接结束对局时，窗口必须一起关闭，避免房间卡在暂停状态。"""
    engine = GameRoomEngine(clock=_clock())
    room, players = _room([Role.WOLF, Role.HUNTER, Role.VILLAGER])
    room.votes = {"u1": "u2", "u3": "u2"}
    engine.resolve_vote(room)
    assert room.statistics.get("hunter_window") == "night"

    engine.submit_night_action(room, "u2", "猎杀", "1")

    assert room.phase == GamePhase.FINISHED
    assert "hunter_window" not in room.statistics
