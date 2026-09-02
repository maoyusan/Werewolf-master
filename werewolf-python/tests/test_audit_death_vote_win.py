"""独立复核审计：死亡链 / 白天公布 / 投票 / 胜负（openspec 任务 5.1-5.5、6.1-6.6）。

期望值全部取自官方 `Werewolf Node/Werewolf.cs`（commit
ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7）；Python 现状不作为证明。
发现的不一致按官方行为断言并标记 strict xfail（W-DIFF-01 起）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from domain.engine import GameRoomEngine, GameRuleError
from domain.models import (
    GamePhase,
    GameRoom,
    KillMethod,
    NightAction,
    Player,
    QuestionType,
    Role,
    Team,
)
from domain.rules import ruleset_official


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
        "audit-death-vote-win",
        rules or ruleset_official(),
        phase=phase,
        players=players,
        day=day,
    )
    return room, players


# ---------------------------------------------------------------------------
# 5.x 死亡链
# ---------------------------------------------------------------------------


def test_audit_lynch_lover_chain_and_hunter_counter() -> None:
    """Werewolf.cs:2766 处决 KillPlayer(Lynch)；5610-5615 恋人链在猎人分支之前；
    5706-5716 KillLover 用 KillMthd.LoverDied、killer=victim；5623-5625 猎人恋人
    死于 LoverDied 仍获得最后一击（hunterFinalShot 默认 true）。"""
    room, players = _room([Role.WOLF, Role.VILLAGER, Role.HUNTER, Role.VILLAGER, Role.VILLAGER])
    players[1].lover_id, players[2].lover_id = "u3", "u2"
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}
    events = GameRoomEngine().resolve_vote(room)

    lynched, lover = players[1], players[2]
    assert lynched.alive is False and lynched.kill_method == KillMethod.LYNCH.value
    assert lover.alive is False and lover.kill_method == KillMethod.LOVER_DIED.value
    # 官方 KillPlayer:5588 —— LoverDied 不算 DiedLastNight；白天处决也不算。
    assert lynched.died_last_night is False
    assert lover.died_last_night is False
    # 恋人链先于猎人分支：死亡顺序 lynched -> lover。
    assert lynched.metadata["death_sequence"] < lover.metadata["death_sequence"]
    # 猎人作为恋人殉情后仍可开最后一枪（官方 HunterFinalShot 被调用）。
    assert lover.metadata.get("pending_hunt") == {
        "method": KillMethod.LOVER_DIED.value, "lynched": False
    }
    assert any(
        event.kind == "hunter_prompt" and event.target_user_id == "u3"
        for event in events
    )


def test_audit_same_night_wolf_then_sk_keeps_first_kill_method() -> None:
    """Werewolf.cs 夜晚结算：狼袭区在 SK 区之前；SK 对已死目标走
    VisitPlayer=AlreadyDead 分支，不产生第二个死因，死因保持 Eat。"""
    room, players = _room(
        [Role.WOLF, Role.SERIAL_KILLER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.NIGHT,
        day=2,
    )
    room.night_actions = {
        "u1": NightAction("u1", QuestionType.KILL.value, "u3", 2),
        "u2": NightAction("u2", QuestionType.SERIAL_KILL.value, "u3", 2),
    }
    events = GameRoomEngine().resolve_night(room)

    victim = players[2]
    assert victim.alive is False
    assert victim.kill_method == KillMethod.EAT.value
    assert sum(1 for event in events if event.kind == "player_died") == 1


def test_audit_dead_player_loses_all_actions_and_candidacy() -> None:
    """官方死亡后残留状态：SendLynchMenu:4955 只给活人发投票菜单、候选仅活人
    (4959)；夜间/白天菜单同样只发给活人。Python 对应各入口守卫。"""
    engine = GameRoomEngine()
    room, players = _room([Role.WOLF, Role.SEER, Role.DETECTIVE, Role.VILLAGER])
    players[1].alive = False

    with pytest.raises(GameRuleError):
        engine.submit_vote(room, "u2", "1")  # 死人不能投票
    with pytest.raises(GameRuleError):
        engine.submit_vote(room, "u1", "2")  # 不能投给死人
    with pytest.raises(GameRuleError):
        engine.submit_vote(room, "u1", "1")  # 不能自投（菜单排除自己）

    room.phase = GamePhase.NIGHT
    with pytest.raises(GameRuleError):
        engine.submit_night_action(room, "u2", "查验", "1")  # 死人无夜间行动

    room.phase = GamePhase.DAY
    with pytest.raises(GameRuleError):
        engine.submit_day_action(room, "u2", "侦查", "1")  # 死人无白天行动


def test_audit_finished_room_rejects_further_operations() -> None:
    """官方 DoGameEnd 后 IsRunning=false，一切回调被丢弃；Python 各入口按阶段拒绝。"""
    engine = GameRoomEngine()
    room, _ = _room([Role.WOLF, Role.VILLAGER], phase=GamePhase.FINISHED)
    with pytest.raises(GameRuleError):
        engine.submit_vote(room, "u1", "2")
    with pytest.raises(GameRuleError):
        engine.submit_night_action(room, "u1", "狼人", "2")
    assert engine.resolve_vote(room) == []
    assert engine.resolve_night(room) == []


def test_audit_flee_does_not_kill_lover() -> None:
    """Werewolf.cs:5602-5607 —— Flee/Idle 死亡跳过一切后果（含 5610 恋人链）。"""
    room, players = _room(
        [Role.VILLAGER, Role.VILLAGER, Role.WOLF, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.DAY,
    )
    players[0].lover_id, players[1].lover_id = "u2", "u1"
    GameRoomEngine().flee(room, "u1")
    assert players[0].alive is False
    assert players[0].metadata.get("died_by_flee_or_idle") is True
    # 官方行为：恋人不殉情。
    assert players[1].alive is True


def test_audit_wolf_cub_flee_does_not_arm_second_kill() -> None:
    """Werewolf.cs:5602-5607 在 5618-5621 的 WolfCub 分支之前 return。"""
    room, players = _room(
        [Role.WOLF_CUB, Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.DAY,
    )
    GameRoomEngine().flee(room, "u1")
    assert players[0].alive is False
    # 官方行为：逃跑的狼崽不给狼群第二刀。
    assert not room.statistics.get("wolf_cub_killed")


def test_audit_night_flee_is_not_a_night_death() -> None:
    """Werewolf.cs:5376 + 5588 —— 夜间逃跑者 DiedLastNight=false，妓女访问其空屋
    走 AlreadyDead→"不在家"，不触发 VisitVictim（4358-4373）。"""
    room, players = _room(
        [Role.WOLF, Role.HARLOT, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.WOLF],
        phase=GamePhase.NIGHT,
        day=2,
    )
    engine = GameRoomEngine()
    engine.submit_night_action(room, "u2", "访问", "1")
    engine.flee(room, "u1")
    assert players[0].died_last_night is False  # 官方 isNight:false
    engine.submit_night_action(room, "u6", "狼人", "跳过")
    assert players[1].alive is True  # 官方：妓女只收到"不在家"


def test_audit_hunter_final_shot_ends_game_immediately() -> None:
    """猎人补枪打死最后一只狼时，官方当场结算村民胜利。"""
    room, players = _room(
        [Role.HUNTER, Role.WOLF, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.NIGHT,
        day=2,
    )
    players[0].alive = False
    players[0].metadata["pending_hunt"] = True
    GameRoomEngine().submit_night_action(room, "u1", "猎杀", "2")
    assert players[1].alive is False
    assert room.phase == GamePhase.FINISHED
    assert room.winner == Team.VILLAGE


# ---------------------------------------------------------------------------
# 6.2-6.3 投票
# ---------------------------------------------------------------------------


def test_audit_all_abstain_counts_nonvote_and_lynches_nobody() -> None:
    """Werewolf.cs:2694-2697 全零票走 -2 分支（NoLynchVotes，无人出局）；
    2661-2663 未投票者 NonVote+1（首次不足 2 不处死）。"""
    room, players = _room([Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    room.votes = {"u1": None, "u2": None}
    events = GameRoomEngine().resolve_vote(room)
    assert any(event.kind == "vote_empty" for event in events)
    assert all(player.alive for player in players)
    assert all(player.non_vote_count == 1 for player in players)


def test_audit_double_lynch_second_round_skips_idle_penalty() -> None:
    """Werewolf.cs:2661 `else if (!p.IsDead && lynchAttempt < 2)`。"""
    engine = GameRoomEngine()
    room, players = _room(
        [Role.WOLF, Role.TROUBLEMAKER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER]
    )
    room.statistics["double_lynch"] = True
    # 第一轮：u5 未投票 -> NonVote 1（官方同样累计）。
    room.votes = {"u1": "u3", "u2": "u3", "u3": "u1", "u4": "u3"}
    engine.resolve_vote(room)
    assert players[4].non_vote_count == 1 and room.phase == GamePhase.VOTE
    # 第二轮：u5 仍未投票 -> 官方不再累计，存活。
    room.votes = {"u1": "u4", "u2": "u4", "u4": "u1"}
    engine.resolve_vote(room)
    assert players[4].non_vote_count == 1
    assert players[4].alive is True


def test_audit_pacifist_round_skips_idle_penalty() -> None:
    """Werewolf.cs:2559-2564 —— peace 生效则本轮处决与计票整体跳过。"""
    room, players = _room(
        [Role.WOLF, Role.PACIFIST, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER]
    )
    room.statistics["pacifist_used"] = True
    room.votes = {"u1": "u3", "u2": "u3", "u3": "u1", "u4": "u3"}
    GameRoomEngine().resolve_vote(room)
    assert all(player.alive for player in players)
    # 官方行为：本轮没有任何 NonVote 变化。
    assert players[4].non_vote_count == 0


# ---------------------------------------------------------------------------
# 6.4-6.6 胜负
# ---------------------------------------------------------------------------


def test_audit_win_zero_and_one_alive_table() -> None:
    """Werewolf.cs:4514-4523 —— 0 人 NoOne；1 人按队伍，特殊角色 NoOne。"""
    engine = GameRoomEngine()
    room, players = _room([Role.WOLF, Role.VILLAGER], phase=GamePhase.DAY)
    for player in players:
        player.alive = False
    engine._check_for_game_end(room)
    assert room.winner == Team.NO_ONE

    room, _ = _room([Role.CULTIST], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.CULT


def test_audit_win_two_player_table() -> None:
    """Werewolf.cs:4524-4590 二人残局分支逐条验证。"""
    engine = GameRoomEngine()

    # 4526 恋人优先于 SK：SK+村民互为恋人 -> Lovers，双双获胜。
    room, players = _room([Role.SERIAL_KILLER, Role.VILLAGER], phase=GamePhase.DAY)
    players[0].lover_id, players[1].lover_id = "u2", "u1"
    engine._check_for_game_end(room)
    assert room.winner == Team.LOVERS
    assert players[0].won is True and players[1].won is True

    # 4532-4536 双猎人 -> other==null -> 村民胜。
    room, _ = _room([Role.HUNTER, Role.HUNTER], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.VILLAGE

    # 4563-4575 教徒+狼 -> 狼胜。
    room, _ = _room([Role.CULTIST, Role.WOLF], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.WOLF

    # 4581-4587 教徒+普通角色 -> 自动转化后教会胜。
    room, players = _room([Role.CULTIST, Role.HUNTER], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.CULT
    assert players[1].role == Role.CULTIST

    # 4583 教徒+盗贼/分身 -> 不转化但教会仍胜。
    room, players = _room([Role.CULTIST, Role.THIEF], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.CULT
    assert players[1].role == Role.THIEF

    # 坦纳+村民不命中任何二人分支 -> 落到 4629-4632 村民胜，坦纳失败。
    room, players = _room([Role.TANNER, Role.VILLAGER], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.VILLAGE
    assert players[0].won is False and players[1].won is True


def test_audit_hunter_vs_snow_wolf_duel_pinned() -> None:
    """Werewolf.cs:4539-4554 —— 猎人对雪狼同样进入决斗分支（SnowWolf 显式列入）。"""
    rules = replace(ruleset_official(), hunter_kill_wolf_chance_base=100)
    room, players = _room([Role.HUNTER, Role.SNOW_WOLF], phase=GamePhase.DAY, rules=rules)
    GameRoomEngine()._check_for_game_end(room)
    assert room.winner == Team.VILLAGE
    assert players[1].alive is False
    assert players[1].kill_method == KillMethod.HUNTER.value


def test_audit_wolf_parity_and_cult_sweep() -> None:
    """Werewolf.cs:4606-4627 —— 全教徒教会胜；狼数>=其他且无枪手例外则狼胜。"""
    engine = GameRoomEngine()
    room, _ = _room([Role.CULTIST, Role.CULTIST, Role.CULTIST], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.CULT

    room, _ = _room([Role.WOLF, Role.WOLF, Role.VILLAGER, Role.SEER], phase=GamePhase.DAY)
    engine._check_for_game_end(room)
    assert room.winner == Team.WOLF


def test_audit_tanner_win_carries_lover() -> None:
    """Werewolf.cs:4675-4690 —— 被处决坦纳 Won=true 且其恋人 Won=true。"""
    room, players = _room([Role.WOLF, Role.TANNER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER])
    players[1].lover_id, players[3].lover_id = "u4", "u2"
    room.votes = {"u1": "u2", "u3": "u2", "u4": "u2", "u5": "u2", "u2": "u1"}
    GameRoomEngine().resolve_vote(room)
    assert room.winner == Team.TANNER
    assert players[1].won is True
    # 官方行为：坦纳的恋人（已殉情）同样计胜。
    assert players[3].won is True


def test_audit_sk_two_player_win_kills_survivor() -> None:
    """Werewolf.cs:4858-4868 —— SerialKillerWinsOverpower + KillPlayer。"""
    room, players = _room([Role.SERIAL_KILLER, Role.VILLAGER], phase=GamePhase.DAY)
    GameRoomEngine()._check_for_game_end(room)
    assert room.winner == Team.SERIAL_KILLER
    assert players[1].alive is False
    assert players[1].kill_method == KillMethod.SERIAL_KILLED.value


def test_audit_arsonist_two_player_win_kills_survivor() -> None:
    """Werewolf.cs:4835-4847 —— ArsonistWinsOverpower 后另一人死亡。"""
    room, players = _room([Role.ARSONIST, Role.VILLAGER], phase=GamePhase.DAY)
    GameRoomEngine()._check_for_game_end(room)
    assert room.winner == Team.ARSONIST
    assert players[1].alive is False


def test_audit_ch_vs_cultist_endgame_keeps_cultist_alive() -> None:
    """Werewolf.cs:4576-4580 —— CHKillsCultistEnd 只调用 DBKill，不设 IsDead。"""
    room, players = _room([Role.CULTIST_HUNTER, Role.CULTIST], phase=GamePhase.DAY)
    GameRoomEngine()._check_for_game_end(room)
    assert room.winner == Team.VILLAGE
    # 官方行为：结算名单里教徒仍显示存活。
    assert players[1].alive is True


def test_audit_checkbitten_pending_bite_aborts_end_check() -> None:
    """Werewolf.cs:4487-4497 —— 存在雪狼且 checkbitten 有人被咬时 return false。"""
    room, players = _room([Role.SNOW_WOLF, Role.VILLAGER], phase=GamePhase.VOTE)
    players[1].bitten = True
    GameRoomEngine()._check_for_game_end(room, checkbitten=True)
    # 官方行为：本次检查不结束游戏（被咬者下一夜转狼后再判）。
    assert room.phase == GamePhase.VOTE
    assert room.winner is None


# ---------------------------------------------------------------------------
# 5.2 / 6.1 白天公布
# ---------------------------------------------------------------------------


def test_audit_quiet_night_announces_no_attack() -> None:
    """Werewolf.cs:4433-4437 —— `if (Players.Any(x => x.DiedLastNight))` 的 else 分支。"""
    room, _ = _room(
        [Role.WOLF, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
        phase=GamePhase.NIGHT,
        day=2,
    )
    room.night_actions = {"u1": NightAction("u1", QuestionType.KILL.value, None, 2)}
    events = GameRoomEngine().resolve_night(room)
    assert any(
        event.public and event.kind in {"no_attack", "night_saved"}
        for event in events
    )
