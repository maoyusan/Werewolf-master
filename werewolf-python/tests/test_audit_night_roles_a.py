"""独立官方对照审计（任务 4.1-4.3：狼系 / 查验系 / 守护与白天系）。

期望值全部取自官方 `Werewolf for Telegram`（commit
ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7）的 `Werewolf Node/Werewolf.cs`、
`Werewolf Node/Helpers/Settings.cs`。Python 现有行为不作为证明；
与官方不一致的分支按官方行为断言并标记 strict xfail（NA-DIFF-XX）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from domain.engine import GameRoomEngine, GameRuleError
from domain.models import (
    DomainEvent,
    GamePhase,
    GameRoom,
    KillMethod,
    NightAction,
    Player,
    QuestionType,
    Role,
)
from domain.rules import ruleset_official
from tests.test_parity import _base, _player, _run
from tests.test_official_parity import _event, _events, _skip_to_next_night


def _direct_room(
    roles: list[Role],
    *,
    phase: GamePhase = GamePhase.NIGHT,
    day: int = 1,
    rules=None,
) -> GameRoom:
    players = [
        Player(f"u{index}", f"P{index}", index, role=role)
        for index, role in enumerate(roles, 1)
    ]
    return GameRoom(
        "audit-night-a", rules or ruleset_official(), phase=phase, day=day, players=players
    )


# ---------------------------------------------------------------------------
# 4.1 狼系
# ---------------------------------------------------------------------------


def test_na_wolf_tie_vote_takes_player_list_order() -> None:
    """Werewolf.cs:3244-3250：狼票平票时按玩家列表顺序取第一个得票者，
    与提交顺序无关（OrderByDescending 为稳定排序）。"""
    result = _run(_base(
        ["Wolf", "Wolf", "Villager", "Villager", "Villager"],
        operations=[
            # u2 先提交并投给列表更靠后的 u4；官方仍应吃掉列表更靠前的 u3。
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "u4"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u3")["kill_method"] == KillMethod.EAT.value
    assert _player(result, "u4")["alive"] is True


def test_na_wolves_eat_traitor_and_sorcerer() -> None:
    """Werewolf.cs:3408-3419（Traitor 未被咬走 default 分支被吃）、
    3421-3437（Sorcerer 走 default 分支被吃）。"""
    traitor = _run(_base(
        ["Wolf", "Traitor", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    sorcerer = _run(_base(
        ["Wolf", "Sorcerer", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "sorcerer", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
        ],
    ))
    assert _player(traitor, "u2")["alive"] is False
    assert _player(traitor, "u2")["kill_method"] == KillMethod.EAT.value
    assert _player(sorcerer, "u2")["alive"] is False
    assert _player(sorcerer, "u2")["kill_method"] == KillMethod.EAT.value


def test_na_hunter_failed_shot_still_bitten() -> None:
    """Werewolf.cs:3325-3376：猎人反击判定失败时 goto default，
    再判定头狼咬人（3422-3424），猎人存活且被标记 Bitten。"""
    result = _run(_base(
        ["AlphaWolf", "Hunter", "Villager", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 100, "hunter_kill_wolf_chance_base": 0},
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u1")["alive"] is True
    assert _player(result, "u2")["alive"] is True
    bitten = _event(result, "player_bitten")
    assert bitten["target_user_id"] == "u2"


def test_na_wolf_attack_away_harlot_fails() -> None:
    """Werewolf.cs:2424-2427,3442-3449：妓女外出时狼人袭击返回 Fail，不造成死亡；
    妓女自己的访问照常结算（3843-3874）。"""
    result = _run(_base(
        ["Wolf", "Harlot", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "visit", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
        ],
    ))
    assert _player(result, "u2")["alive"] is True
    visited = _event(result, "harlot_visited_you")
    assert visited["target_user_id"] == "u3"


def test_na_frozen_harlot_counts_as_home_and_skips_visit() -> None:
    """Werewolf.cs:3127-3133（妓女被冻结）、2424（Frozen 视为在家）、
    3846-3847（冻结妓女不出访）：同夜雪狼先冻结，狼人可以吃到在家的妓女。"""
    result = _run(_base(
        ["SnowWolf", "Wolf", "Harlot", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "visit", "target": "u4"},
            {"kind": "night_action", "actor": "u1", "action": "freeze", "target": "u3"},
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u3")["kill_method"] == KillMethod.EAT.value
    assert not _events(result, "harlot_visited_you")
    assert not _events(result, "harlot_visit")


def test_na_snow_freeze_sk_blocks_serial_kill() -> None:
    """Werewolf.cs:3101-3106（雪狼冻结连环杀手）、3502（冻结的 SK 不行动）。
    钉住 serial_killer_stumble_chance=0 使雪狼访问 SK 必然存活。"""
    result = _run(_base(
        ["SnowWolf", "SerialKiller", "Villager", "Villager", "Villager"],
        rules={"serial_killer_stumble_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "serial_kill", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "freeze", "target": "u2"},
        ],
    ))
    assert _player(result, "u1")["alive"] is True
    assert _player(result, "u3")["alive"] is True
    assert _events(result, "snow_frozen")


def test_na_ga_blocks_snow_freeze_on_seer() -> None:
    """Werewolf.cs:3107-3111：守护天使守护冻结目标（非 SK）时阻止冻结，
    目标当夜仍可行动（预言家照常得到结果，3925-3955）。"""
    result = _run(_base(
        ["SnowWolf", "Seer", "GuardianAngel", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "freeze", "target": "u2"},
            {"kind": "night_action", "actor": "u3", "action": "guard", "target": "u2"},
            {"kind": "night_action", "actor": "u2", "action": "seer", "target": "u1"},
        ],
    ))
    blocked = _event(result, "guard_blocked_snow")
    assert blocked["target_user_id"] == "u1"
    seer = _event(result, "seer_result")
    assert seer["metadata"]["seen_role"] == Role.SNOW_WOLF.value


def test_na_snow_freeze_grave_digger_resets_graves() -> None:
    """Werewolf.cs:3160-3168：雪狼冻结掘墓人时，当夜坟墓数清零
    （lastGrave 回退，DugGravesLastNight=0）。"""
    room = _direct_room([Role.SNOW_WOLF, Role.GRAVE_DIGGER, Role.VILLAGER], day=2)
    digger = room.players[1]
    digger.metadata["dug_graves_last_night"] = 2
    engine = GameRoomEngine()
    events = engine._resolve_snow_freeze(
        room, NightAction("u1", QuestionType.FREEZE.value, "u2", 2), None, None
    )
    assert digger.is_frozen is True
    assert digger.metadata["dug_graves_last_night"] == 0
    assert any(event.kind == "snow_frozen" for event in events)


def test_na_diff01_snow_freezes_guarded_serial_killer() -> None:
    """Werewolf.cs:3101-3106：即使 GA 守护 SK，雪狼仍冻结 SK，SK 当夜不杀人。"""
    result = _run(_base(
        ["SnowWolf", "SerialKiller", "GuardianAngel", "Villager", "Villager"],
        rules={"serial_killer_stumble_chance": 0},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "freeze", "target": "u2"},
            {"kind": "night_action", "actor": "u3", "action": "guard", "target": "u2"},
            {"kind": "night_action", "actor": "u2", "action": "serial_kill", "target": "u4"},
        ],
    ))
    assert _player(result, "u4")["alive"] is True


def test_na_diff04_bitten_escape_has_no_public_notice() -> None:
    """Werewolf.cs:3421-3424,4193-4437：头狼咬人当夜公开频道不出现逃生提示。"""
    result = _run(_base(
        ["AlphaWolf", "Villager", "Villager", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 100},
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert not [
        event for event in result["events"]
        if event["kind"] == "night_saved" and event["public"]
    ]


def test_na_diff04_wise_elder_escape_has_no_public_notice() -> None:
    """Werewolf.cs:3389-3406,4193-4437：长老首次被吃当夜公开频道不出现逃生提示。"""
    result = _run(_base(
        ["Wolf", "WiseElder", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u2")["alive"] is True
    assert not [
        event for event in result["events"]
        if event["kind"] == "wise_elder_saved" and event["public"]
    ]


def test_na_diff04_guard_block_has_no_public_notice() -> None:
    """Werewolf.cs:3281-3286,4193-4437：GA 挡狼袭当夜公开频道不出现被守护提示。"""
    result = _run(_base(
        ["Wolf", "GuardianAngel", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is True
    assert not [
        event for event in result["events"]
        if event["kind"] == "night_saved" and event["public"]
    ]


def test_na_diff05_wolves_told_guard_blocked_their_eat() -> None:
    """Werewolf.cs:3283-3284：袭击被守护挡下后，狼人应收到私聊反馈。"""
    room = _direct_room(
        [Role.WOLF, Role.WOLF, Role.GUARDIAN_ANGEL, Role.VILLAGER, Role.VILLAGER]
    )
    room.night_actions = {
        "u1": NightAction("u1", QuestionType.KILL.value, "u4", 1),
        "u2": NightAction("u2", QuestionType.KILL.value, "u4", 1),
        "u3": NightAction("u3", QuestionType.GUARD.value, "u4", 1),
    }
    events = GameRoomEngine().resolve_night(room)
    assert room.players[3].alive is True
    assert any(
        not event.public and event.target_user_id == "u1" for event in events
    ), "狼 u1 未收到守护阻挡的私聊反馈"


def test_na_diff05_wolves_told_about_alpha_bite() -> None:
    """Werewolf.cs:1943-1950：头狼咬人后全体狼人收到 PlayerBittenWolves 私聊。"""
    rules = replace(ruleset_official(), alpha_wolf_conversion_chance=100)
    room = _direct_room(
        [Role.ALPHA_WOLF, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        rules=rules,
    )
    room.night_actions = {
        "u1": NightAction("u1", QuestionType.KILL.value, "u3", 1),
        "u2": NightAction("u2", QuestionType.KILL.value, "u3", 1),
    }
    events = GameRoomEngine().resolve_night(room)
    assert room.players[2].bitten is True
    assert any(
        not event.public and event.target_user_id == "u2" for event in events
    ), "狼 u2 未收到头狼咬人的私聊通知"


# ---------------------------------------------------------------------------
# 4.2 查验系
# ---------------------------------------------------------------------------


def test_na_seer_sees_snow_wolf_and_sorcerer_true() -> None:
    """Werewolf.cs:3933-3954：预言家的伪装表只覆盖 Traitor/WolfCub/AlphaWolf/
    WolfMan/Lycan；雪狼与巫师按真实身份显示。"""
    snow = _run(_base(
        ["Seer", "SnowWolf", "Sorcerer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "sorcerer", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u2"},
        ],
    ))
    sorcerer = _run(_base(
        ["Seer", "SnowWolf", "Sorcerer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "sorcerer", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u3"},
        ],
    ))
    assert _event(snow, "seer_result")["metadata"]["seen_role"] == Role.SNOW_WOLF.value
    assert _event(sorcerer, "seer_result")["metadata"]["seen_role"] == Role.SORCERER.value


def test_na_sorcerer_sees_lycan_as_non_wolf() -> None:
    """Werewolf.cs:3965-3980：巫师的狼人分支只含 AlphaWolf/Wolf/WolfCub，
    Lycan 落到 SorcererOther（非狼、非预言家）。"""
    result = _run(_base(
        ["Sorcerer", "Lycan", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "sorcerer", "target": "u2"},
        ],
    ))
    seen = _event(result, "sorcerer_result")["metadata"]["seen_role"]
    assert seen not in {Role.WOLF.value, Role.LYCAN.value, Role.SEER.value, Role.SNOW_WOLF.value}


def test_na_frozen_checkers_get_no_results() -> None:
    """Werewolf.cs:3959（Sorcerer !Frozen）、4022（Oracle !Frozen）、
    4046（Augur !Frozen）：冻结的查验类角色当夜不产生任何结果。"""
    result = _run(_base(
        ["Sorcerer", "Oracle", "Augur", "Wolf", "Villager"],
        operations=[
            {"kind": "set_state", "player": "u1", "values": {"is_frozen": True}},
            {"kind": "set_state", "player": "u2", "values": {"is_frozen": True}},
            {"kind": "set_state", "player": "u3", "values": {"is_frozen": True}},
            {"kind": "night_action", "actor": "u1", "action": "sorcerer", "target": "u5"},
            {"kind": "night_action", "actor": "u2", "action": "oracle", "target": "u5"},
            {"kind": "night_action", "actor": "u4", "action": "wolf", "target": "skip"},
        ],
    ))
    for kind in ("sorcerer_result", "oracle_result", "augur_result", "augur_nothing"):
        assert not _events(result, kind), kind


def test_na_augur_substitutes_apprentice_for_missing_seer() -> None:
    """Werewolf.cs:4052-4053：奥古看到的缺席身份是 Seer、且场上学徒存活而无
    存活预言家时，替换为 ApprenticeSeer。"""
    result = _run(_base(
        ["Augur", "ApprenticeSeer", "Wolf", "Villager", "Villager"],
        operations=[
            {"kind": "set_statistics", "values": {"possible_roles": ["Seer"]}},
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
        ],
    ))
    assert _event(result, "augur_result")["metadata"]["role"] == Role.APPRENTICE_SEER.value


def test_na_apprentice_promotion_notifies_beholder() -> None:
    """Werewolf.cs:1719-1730（学徒接任）与 Transform:2055-2057（BeholderNewSeer）：
    预言家死亡当夜学徒转正，观察者被告知新预言家。"""
    result = _run(_base(
        ["Wolf", "Seer", "ApprenticeSeer", "Beholder", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "seer", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
        ],
    ))
    assert _player(result, "u2")["alive"] is False
    assert _player(result, "u3")["role"] == Role.SEER.value
    promoted = _event(result, "apprentice_now_seer")
    assert promoted["target_user_id"] == "u3"
    told = _event(result, "beholder_new_seer")
    assert told["target_user_id"] == "u4"


def test_na_detective_caught_notifies_snow_wolf() -> None:
    """Werewolf.cs:2937-2943：侦探暴露的通知名单含雪狼
    （wolves 数组包括 IRole.SnowWolf）；2949 侦查显示真实身份。"""
    result = _run(_base(
        ["SnowWolf", "Detective", "Villager", "Villager", "Villager"],
        rules={"detective_caught_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "freeze", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "detect", "target": "u3"},
            {"kind": "start_vote"},
        ],
    ))
    caught = _event(result, "detective_caught")
    assert caught["target_user_id"] == "u1"
    assert _event(result, "detect_result")["metadata"]["seen_role"] == Role.VILLAGER.value


# ---------------------------------------------------------------------------
# 4.3 守护 / 白天系
# ---------------------------------------------------------------------------


def test_na_ga_blocks_serial_killer_on_villager() -> None:
    """Werewolf.cs:3517-3527：GA 守护非妓女目标时挡下连环杀手。"""
    result = _run(_base(
        ["SerialKiller", "GuardianAngel", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "serial_kill", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is True
    blocked = _event(result, "guard_blocked_serial")
    assert blocked["target_user_id"] == "u1"


def test_na_ga_dies_visiting_serial_killer() -> None:
    """Werewolf.cs:2340-2346：非狼访客访问连环杀手必死；GA 守护 SK 时
    在 GA 结算区死于 VisitKiller（4107-4118 GuardKiller）。"""
    result = _run(_base(
        ["SerialKiller", "GuardianAngel", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u1"},
            {"kind": "night_action", "actor": "u1", "action": "serial_kill", "target": "u3"},
        ],
    ))
    assert _player(result, "u2")["alive"] is False
    assert _player(result, "u2")["kill_method"] == KillMethod.VISIT_KILLER.value


def test_na_ga_may_guard_same_target_on_consecutive_nights() -> None:
    """Werewolf.cs:5182-5186：GA 夜间菜单只排除自己，没有连续守护限制；
    连续两夜守护同一目标均生效。"""
    result = _run(_base(
        ["Wolf", "GuardianAngel", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            *_skip_to_next_night(),
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is True


def test_na_sk_kills_away_harlot() -> None:
    """Werewolf.cs:2330-2331：连环杀手从不扑空（除掘墓人外），
    外出的妓女仍会被 SK 杀死。"""
    result = _run(_base(
        ["SerialKiller", "Harlot", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "visit", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "serial_kill", "target": "u2"},
        ],
    ))
    assert _player(result, "u2")["alive"] is False
    assert _player(result, "u2")["kill_method"] == KillMethod.SERIAL_KILLED.value


def test_na_sandman_night_clears_wolf_cub_double_kill() -> None:
    """Werewolf.cs:3011-3019：沙人催眠的夜晚重置 WolfCubKilled（3015），
    狼崽白天被处决也不会在之后获得双杀。"""
    result = _run(_base(
        ["Wolf", "WolfCub", "Sandman", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u3", "action": "sandman", "target": "yes"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u2", "target": "skip"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "vote", "actor": "u5", "target": "u2"},
            {"kind": "vote", "actor": "u6", "target": "u2"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "skip"},
            {"kind": "vote", "actor": "u3", "target": "skip"},
            {"kind": "vote", "actor": "u4", "target": "skip"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
            {"kind": "vote", "actor": "u6", "target": "skip"},
        ],
    ))
    assert _player(result, "u2")["alive"] is False
    assert _events(result, "sandman")
    assert not _events(result, "wolf_cub_second_prompt")
    assert result["phase"] == GamePhase.NIGHT.value
    assert result["day"] == 3


def test_na_day_one_shot_abilities_are_single_use() -> None:
    """Werewolf.cs:899-975,5070-5145：铁匠、沙人、和平主义者、捣乱者的
    菜单以 HasUsedAbility 为闸门，只能使用一次。"""
    engine = GameRoomEngine()
    room = _direct_room(
        [Role.BLACKSMITH, Role.SANDMAN, Role.PACIFIST, Role.TROUBLEMAKER, Role.VILLAGER],
        phase=GamePhase.DAY,
        day=2,
    )
    engine.submit_day_action(room, "u1", "silver", "yes")
    engine.submit_day_action(room, "u2", "sandman", "yes")
    engine.submit_day_action(room, "u3", "pacifist", "yes")
    engine.submit_day_action(room, "u4", "trouble", "yes")
    for user_id, action in (
        ("u1", "silver"), ("u2", "sandman"), ("u3", "pacifist"), ("u4", "trouble"),
    ):
        with pytest.raises(GameRuleError):
            engine.submit_day_action(room, user_id, action, "yes")


def test_na_chemist_not_demoted_when_roles_hidden() -> None:
    """Werewolf.cs:4217-4220：ShowRolesDeath 关闭（secret）时死亡公告走
    GenericDeathNoReveal，不进入 4279-4284 的化学家降级分支。"""
    result = _run(_base(
        ["Wolf", "Chemist", "WiseElder", "Villager", "Villager"],
        rules={"chemist_success_chance": 100, "show_roles_on_death": False},
        operations=[
            {"kind": "set_state", "player": "u2", "values": {"has_used_ability": True}},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "chemistry", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u2")["role"] == Role.CHEMIST.value


def test_na_diff03_chemist_demoted_for_killing_elder_when_roles_shown() -> None:
    """Werewolf.cs:4279-4284：非隐藏模式下化学家杀死长老 → 化学家变村民。"""
    result = _run(_base(
        ["Wolf", "Chemist", "WiseElder", "Villager", "Villager"],
        rules={"chemist_success_chance": 100, "show_roles_on_death": True},
        operations=[
            {"kind": "set_state", "player": "u2", "values": {"has_used_ability": True}},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "chemistry", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u2")["role"] == Role.VILLAGER.value


def test_na_diff02_gunner_announcement_names_gunner() -> None:
    """Werewolf.cs:2886-2894：枪手开枪的公开公告应包含枪手姓名。"""
    engine = GameRoomEngine()
    room = _direct_room(
        [Role.GUNNER, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.DAY,
    )
    engine.submit_day_action(room, "u1", "shoot", "3")
    events = engine.start_vote(room)
    died = next(event for event in events if event.kind == "player_died")
    assert room.players[2].alive is False
    assert "P1" in died.text


def test_na_diff02_blacksmith_announcement_names_actor() -> None:
    """Werewolf.cs:935-941：撒银公告应包含铁匠姓名。"""
    engine = GameRoomEngine()
    room = _direct_room(
        [Role.BLACKSMITH, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.DAY,
    )
    events = engine.submit_day_action(room, "u1", "silver", "yes")
    announced = next(event for event in events if event.kind == "silver_spread")
    assert announced.public is True
    assert "P1" in announced.text


def test_na_diff02_sandman_announcement_names_actor() -> None:
    """Werewolf.cs:950-956：催眠公告应包含沙人姓名。"""
    engine = GameRoomEngine()
    room = _direct_room(
        [Role.SANDMAN, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.DAY,
    )
    events = engine.submit_day_action(room, "u1", "sandman", "yes")
    announced = next(event for event in events if event.kind == "sandman")
    assert announced.public is True
    assert "P1" in announced.text


def test_na_diff02_pacifist_announcement_names_actor() -> None:
    """Werewolf.cs:913-918：和平公告应包含和平主义者姓名。"""
    engine = GameRoomEngine()
    room = _direct_room(
        [Role.PACIFIST, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.DAY,
    )
    events = engine.submit_day_action(room, "u1", "pacifist", "yes")
    announced = next(event for event in events if event.kind == "pacifist")
    assert announced.public is True
    assert "P1" in announced.text


def test_na_diff02_troublemaker_announcement_names_actor() -> None:
    """Werewolf.cs:965-972：捣乱公告应包含捣乱者姓名。"""
    engine = GameRoomEngine()
    room = _direct_room(
        [Role.TROUBLEMAKER, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.DAY,
    )
    events = engine.submit_day_action(room, "u1", "trouble", "yes")
    announced = next(event for event in events if event.kind == "troublemaker")
    assert announced.public is True
    assert "P1" in announced.text
