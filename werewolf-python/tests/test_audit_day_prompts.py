"""独立官方对照审计：白天技能角色的私聊行动菜单（SendDayActions）。

官方 `Werewolf Node/Werewolf.cs:5019-5150` 在 `DayCycle` 里 `SendPlayerList` 之后调用
`SendDayActions()`，逐个角色推送 inline 菜单。QQ 端没有按钮，改为等价语义的私聊文字提示，
但触发条件、推送顺序与 `FirstOrDefault` 语义必须与官方一致：

* 5029-5042 Detective   —— 存活且存在其他存活玩家
* 5044-5055 Mayor       —— 存活且 `GameDay == 1`
* 5057-5068 Pacifist    —— 存活且 `GameDay == 1`
* 5070-5081 Sandman     —— 存活且 `!HasUsedAbility`
* 5083-5095 Blacksmith  —— 存活且 `!HasUsedAbility`
* 5097-5115 Gunner      —— 存活、`Bullet > 0` 且存在其他存活玩家，文案带剩余子弹数
* 5117-5131 Spumpkin    —— 存活且存在其他存活玩家
* 5133-5148 Troublemaker—— 存活且 `!HasUsedAbility`
"""

from __future__ import annotations

from domain.engine import GameRoomEngine
from domain.models import (
    DomainEvent,
    GamePhase,
    GameRoom,
    Player,
    QuestionType,
    Role,
)
from domain.rules import ruleset_official
from tests.test_parity import _base, _run
from tests.test_official_parity import _events


def _room(*roles: Role, day: int = 1) -> tuple[GameRoom, list[Player]]:
    players = [
        Player(f"u{index}", f"P{index}", index, role=role)
        for index, role in enumerate(roles, start=1)
    ]
    room = GameRoom(
        "audit-day-prompt",
        ruleset_official(),
        phase=GamePhase.DAY,
        day=day,
        players=players,
    )
    return room, players


def _prompts(room: GameRoom) -> list[DomainEvent]:
    return GameRoomEngine()._day_prompts(room)


def _actions(events: list[DomainEvent]) -> list[str]:
    return [event.metadata["actions"][0] for event in events]


# ---------------------------------------------------------------------------
# 逐个角色的触发条件
# ---------------------------------------------------------------------------


def test_detective_prompt_requires_another_living_player() -> None:
    """Werewolf.cs:5033-5041 —— options.Any() 才推送菜单。"""
    room, players = _room(Role.DETECTIVE, Role.VILLAGER)
    events = _prompts(room)
    assert _actions(events) == [QuestionType.DETECT.value]
    assert events[0].public is False and events[0].target_user_id == "u1"

    players[1].alive = False
    assert _prompts(room) == []


def test_mayor_prompt_only_on_the_first_day() -> None:
    """Werewolf.cs:5045 —— `mayor != null && GameDay == 1`。"""
    room, _ = _room(Role.MAYOR, Role.VILLAGER, day=1)
    assert _actions(_prompts(room)) == [QuestionType.MAYOR.value]
    room.day = 2
    assert _prompts(room) == []


def test_pacifist_prompt_only_on_the_first_day() -> None:
    """Werewolf.cs:5058 —— `pacifist != null && GameDay == 1`。"""
    room, _ = _room(Role.PACIFIST, Role.VILLAGER, day=1)
    assert _actions(_prompts(room)) == [QuestionType.PACIFIST.value]
    room.day = 3
    assert _prompts(room) == []


def test_sandman_prompt_stops_after_the_ability_is_used() -> None:
    """Werewolf.cs:5070 —— 谓词里带 `!x.HasUsedAbility`。"""
    room, players = _room(Role.SANDMAN, Role.VILLAGER, day=2)
    assert _actions(_prompts(room)) == [QuestionType.SANDMAN.value]
    players[0].has_used_ability = True
    assert _prompts(room) == []


def test_blacksmith_prompt_stops_after_the_ability_is_used() -> None:
    """Werewolf.cs:5083 —— 谓词里带 `!x.HasUsedAbility`。"""
    room, players = _room(Role.BLACKSMITH, Role.VILLAGER, day=2)
    assert _actions(_prompts(room)) == [QuestionType.SPREAD_SILVER.value]
    players[0].has_used_ability = True
    assert _prompts(room) == []


def test_troublemaker_prompt_stops_after_the_ability_is_used() -> None:
    """Werewolf.cs:5133 —— 谓词里带 `!x.HasUsedAbility`。"""
    room, players = _room(Role.TROUBLEMAKER, Role.VILLAGER, day=2)
    assert _actions(_prompts(room)) == [QuestionType.TROUBLE.value]
    players[0].has_used_ability = True
    assert _prompts(room) == []


def test_gunner_prompt_reports_remaining_bullets_and_needs_ammo() -> None:
    """Werewolf.cs:5104-5113 —— `AskShoot` 带 `gunner.Bullet`，且 Bullet > 0 才推送。"""
    room, players = _room(Role.GUNNER, Role.VILLAGER, day=2)
    players[0].bullet_count = 2
    events = _prompts(room)
    assert _actions(events) == [QuestionType.SHOOT.value]
    assert "2" in events[0].text

    players[0].bullet_count = 1
    assert "1" in _prompts(room)[0].text

    players[0].bullet_count = 0
    assert _prompts(room) == []


def test_spumpkin_prompt_needs_a_target_and_ignores_bullets() -> None:
    """Werewolf.cs:5117-5130 —— 南瓜头不检查子弹，只要求存在其他存活玩家。"""
    room, players = _room(Role.SPUMPKIN, Role.VILLAGER, day=4)
    assert _actions(_prompts(room)) == [QuestionType.SHOOT.value]
    players[1].alive = False
    assert _prompts(room) == []


def test_plain_roles_get_no_day_prompt() -> None:
    """官方 SendDayActions 只覆盖 8 个白天技能角色，其余角色白天没有私聊菜单。"""
    room, _ = _room(Role.VILLAGER, Role.WOLF, Role.SEER, Role.HARLOT, day=2)
    assert _prompts(room) == []


# ---------------------------------------------------------------------------
# 顺序与 FirstOrDefault 语义
# ---------------------------------------------------------------------------


def test_prompt_order_matches_official_send_day_actions() -> None:
    """官方按侦探→市长→和平→沙人→铁匠→枪手→南瓜头→捣乱的固定顺序推送。"""
    room, players = _room(
        Role.TROUBLEMAKER,
        Role.SPUMPKIN,
        Role.GUNNER,
        Role.BLACKSMITH,
        Role.SANDMAN,
        Role.PACIFIST,
        Role.MAYOR,
        Role.DETECTIVE,
        day=1,
    )
    players[2].bullet_count = 2
    events = _prompts(room)
    assert [event.target_user_id for event in events] == [
        "u8",  # Detective
        "u7",  # Mayor
        "u6",  # Pacifist
        "u5",  # Sandman
        "u4",  # Blacksmith
        "u3",  # Gunner
        "u2",  # Spumpkin
        "u1",  # Troublemaker
    ]
    assert _actions(events) == [
        QuestionType.DETECT.value,
        QuestionType.MAYOR.value,
        QuestionType.PACIFIST.value,
        QuestionType.SANDMAN.value,
        QuestionType.SPREAD_SILVER.value,
        QuestionType.SHOOT.value,
        QuestionType.SHOOT.value,
        QuestionType.TROUBLE.value,
    ]
    assert all(event.public is False for event in events)


def test_duplicated_role_only_prompts_the_first_player() -> None:
    """官方一律用 FirstOrDefault：分身复制出的第二个侦探不会再收到菜单。"""
    room, _ = _room(Role.DETECTIVE, Role.DETECTIVE, Role.VILLAGER, day=2)
    events = _prompts(room)
    assert [event.target_user_id for event in events] == ["u1"]


def test_dead_players_are_skipped_and_choices_are_cleared() -> None:
    """Werewolf.cs:5022-5026 —— 先清空所有人的选择；谓词一律带 `!x.IsDead`。"""
    room, players = _room(Role.DETECTIVE, Role.SANDMAN, Role.VILLAGER, day=2)
    players[0].alive = False
    players[0].choice = "u3"
    players[1].choice2 = "u3"
    events = _prompts(room)
    assert [event.target_user_id for event in events] == ["u2"]
    assert all(player.choice is None and player.choice2 is None for player in room.players)


# ---------------------------------------------------------------------------
# 与白天流程、命令的联通
# ---------------------------------------------------------------------------


def test_day_start_delivers_prompts_privately() -> None:
    """整局流程里，第 1 天开始时应随 day_started 一起推送私聊菜单。"""
    result = _run(_base(
        ["Wolf", "Detective", "Gunner", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u4"}],
    ))
    prompts = _events(result, "day_prompt")
    assert {event["target_user_id"] for event in prompts} == {"u2", "u3"}
    assert all(event["public"] is False for event in prompts)
    gunner = next(event for event in prompts if event["target_user_id"] == "u3")
    assert gunner["metadata"]["actions"] == [QuestionType.SHOOT.value]


def test_prompted_commands_are_accepted_by_submit_day_action() -> None:
    """菜单里提示的命令必须真的能被 submit_day_action 接受。"""
    room, players = _room(
        Role.DETECTIVE, Role.BLACKSMITH, Role.SANDMAN, Role.TROUBLEMAKER, Role.VILLAGER, day=1
    )
    engine = GameRoomEngine()
    assert len(_prompts(room)) == 4
    assert engine.submit_day_action(room, "u1", "侦查", "5")
    assert engine.submit_day_action(room, "u2", "撒银", "是")
    assert engine.submit_day_action(room, "u3", "催眠", "是")
    assert engine.submit_day_action(room, "u4", "捣乱", None)
    assert players[1].has_used_ability and players[2].has_used_ability
    assert players[3].has_used_ability
