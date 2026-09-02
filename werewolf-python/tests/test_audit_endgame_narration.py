"""独立复核审计：残局决斗与结算播报。

期望值全部取自官方 `Werewolf Node/Werewolf.cs`（commit
ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7）：
4505-4600（CheckForGameEnd 的二人残局分支）、4770-4901（DoGameEnd 各队伍胜利
文案与 Overpower 结算）、4906-4923（ShowRolesEnd 三种名单 + EndTime）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from domain.engine import GameRoomEngine
from domain.fixtures import FrozenClock
from domain.locale import get_locale_string
from domain.models import GamePhase, GameRoom, KillMethod, Player, Role, Team
from domain.roleinfo import role_display_name
from domain.rules import ruleset_official


@pytest.fixture(autouse=True)
def _deterministic_locale_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    """官方 GetLocaleString 会在多条候选文案里随机挑一条，审计里固定取第一条。"""
    monkeypatch.setattr("domain.locale._pick", lambda values: values[0])


def _room(
    roles: list[Role],
    *,
    phase: GamePhase = GamePhase.DAY,
    rules=None,
) -> tuple[GameRoom, list[Player]]:
    players = [
        Player(f"u{index}", f"P{index}", index, role=role)
        for index, role in enumerate(roles, 1)
    ]
    room = GameRoom(
        "audit-endgame-narration",
        rules or ruleset_official(),
        phase=phase,
        players=players,
        day=3,
    )
    return room, players


def _text(events, kind: str) -> str:
    matched = [event.text for event in events if event.kind == kind]
    assert len(matched) == 1, f"{kind} 期望恰好一条，实际 {len(matched)} 条"
    return matched[0]


def _kinds(events) -> list[str]:
    return [event.kind for event in events]


# ---------------------------------------------------------------------------
# 4539-4554 猎人 vs 狼的决斗播报
# ---------------------------------------------------------------------------


def test_hunter_wins_duel_uses_official_wording_before_the_kill() -> None:
    """Werewolf.cs:4544-4546 —— 先 SendWithQueue(HunterKillsWolfEnd) 再 KillPlayer。"""
    rules = replace(ruleset_official(), hunter_kill_wolf_chance_base=100)
    room, players = _room([Role.HUNTER, Role.WOLF], rules=rules)

    events = GameRoomEngine()._check_for_game_end(room)

    assert _text(events, "hunter_kills_wolf_end") == get_locale_string(
        "HunterKillsWolfEnd", None, "P1（1号）", "P2（2号）"
    ).strip()
    kinds = _kinds(events)
    assert kinds.index("hunter_kills_wolf_end") < kinds.index("player_died")
    assert players[1].alive is False and players[1].kill_method == KillMethod.HUNTER.value
    assert room.winner == Team.VILLAGE


def test_wolf_wins_duel_uses_official_wording_before_the_kill() -> None:
    """Werewolf.cs:4550-4552 —— 掷点失败走 WolfKillsHunterEnd，猎人被吃。"""
    rules = replace(ruleset_official(), hunter_kill_wolf_chance_base=0)
    room, players = _room([Role.HUNTER, Role.WOLF], rules=rules)

    events = GameRoomEngine()._check_for_game_end(room)

    assert _text(events, "wolf_kills_hunter_end") == get_locale_string(
        "WolfKillsHunterEnd", None, "P1（1号）", "P2（2号）"
    ).strip()
    kinds = _kinds(events)
    assert kinds.index("wolf_kills_hunter_end") < kinds.index("player_died")
    assert players[0].alive is False and players[0].kill_method == KillMethod.EAT.value
    assert room.winner == Team.WOLF


def test_lovers_are_checked_before_the_hunter_duel() -> None:
    """Werewolf.cs:4526 —— 二人互为恋人时直接 DoGameEnd(Lovers)，不进决斗分支。"""
    rules = replace(ruleset_official(), hunter_kill_wolf_chance_base=100)
    room, players = _room([Role.HUNTER, Role.WOLF], rules=rules)
    players[0].lover_id, players[1].lover_id = "u2", "u1"

    events = GameRoomEngine()._check_for_game_end(room)

    assert room.winner == Team.LOVERS
    assert all(player.alive for player in players)
    assert "hunter_kills_wolf_end" not in _kinds(events)


# ---------------------------------------------------------------------------
# 4880-4894 SKHunter 结局
# ---------------------------------------------------------------------------


def test_sk_hunter_end_kills_both_in_official_order() -> None:
    """Werewolf.cs:4880-4894 —— 官方先由猎人打死连环杀手，再由连环杀手捅死猎人，
    最后补播 SKHunterEnd，双方解锁 DoubleKill。"""
    room, players = _room([Role.HUNTER, Role.SERIAL_KILLER])

    events = GameRoomEngine()._check_for_game_end(room)

    assert room.winner == Team.SK_HUNTER
    hunter, killer = players
    assert hunter.alive is False and hunter.kill_method == KillMethod.SERIAL_KILLED.value
    assert killer.alive is False and killer.kill_method == KillMethod.HUNTER.value
    # 死亡顺序：连环杀手先倒下。
    assert killer.metadata["death_sequence"] < hunter.metadata["death_sequence"]
    assert _text(events, "sk_hunter_end") == get_locale_string(
        "SKHunterEnd", None, "P2（2号）", "P1（1号）"
    ).strip()
    assert room.statistics["end_kind"] == "SKHunter"


def test_serial_killer_is_checked_before_the_cult_branch() -> None:
    """Werewolf.cs:4556-4561 —— SK 分支排在教会之前，教徒不得转化连环杀手。"""
    room, players = _room([Role.CULTIST, Role.SERIAL_KILLER])

    GameRoomEngine()._check_for_game_end(room)

    assert room.winner == Team.SERIAL_KILLER
    assert players[1].role == Role.SERIAL_KILLER


def test_arsonist_is_checked_before_the_cult_branch() -> None:
    """Werewolf.cs:4558-4561 —— 纵火犯（无枪手）同样抢在教会分支之前结算。"""
    room, players = _room([Role.CULTIST, Role.ARSONIST])

    GameRoomEngine()._check_for_game_end(room)

    assert room.winner == Team.ARSONIST
    assert players[0].role == Role.CULTIST


def test_cult_versus_wolf_does_not_kill_the_cultist() -> None:
    """Werewolf.cs:4570-4575 —— 教徒对狼只 DoGameEnd(Wolf)，官方不执行击杀。"""
    room, players = _room([Role.CULTIST, Role.WOLF])

    GameRoomEngine()._check_for_game_end(room)

    assert room.winner == Team.WOLF
    assert players[0].alive is True


def test_cult_hunter_end_uses_official_wording() -> None:
    """Werewolf.cs:4576-4580 —— CHKillsCultistEnd 只记战绩，教徒仍存活。"""
    room, players = _room([Role.CULTIST, Role.CULTIST_HUNTER])

    events = GameRoomEngine()._check_for_game_end(room)

    assert _text(events, "ch_kills_cultist_end") == get_locale_string(
        "CHKillsCultistEnd", None, "P1（1号）", "P2（2号）"
    ).strip()
    assert players[0].alive is True
    assert players[0].metadata["db_killed_by"] == "u2"
    assert room.winner == Team.VILLAGE


# ---------------------------------------------------------------------------
# 4770-4901 胜利播报
# ---------------------------------------------------------------------------


def test_win_announcement_uses_official_locale_per_team() -> None:
    """Werewolf.cs:4810-4901 —— 每个队伍对应固定的胜利文案键。"""
    engine = GameRoomEngine()
    table = [
        ([Role.VILLAGER, Role.SEER], Team.VILLAGE, "VillageWins"),
        ([Role.CULTIST, Role.CULTIST], Team.CULT, "CultWins"),
        ([Role.TANNER, Role.VILLAGER], Team.TANNER, "TannerWins"),
        ([Role.SERIAL_KILLER, Role.VILLAGER], Team.SERIAL_KILLER, "SerialKillerWins"),
        ([Role.ARSONIST, Role.VILLAGER], Team.ARSONIST, "ArsonistWins"),
        ([Role.VILLAGER, Role.VILLAGER], Team.LOVERS, "LoversWin"),
        ([Role.VILLAGER, Role.VILLAGER], Team.NO_ONE, "NoWinner"),
        ([Role.HUNTER, Role.SERIAL_KILLER], Team.SK_HUNTER, "NoWinner"),
    ]
    for roles, team, key in table:
        room, _ = _room(roles)
        events = engine.finish(room, team)
        assert _text(events, "game_finished") == get_locale_string(key).strip(), key


def test_single_wolf_and_wolf_pack_use_different_official_keys() -> None:
    """Werewolf.cs:4816 —— 存活狼多于一只时用 WolvesWin，否则 WolfWins。"""
    engine = GameRoomEngine()
    room, players = _room([Role.WOLF, Role.VILLAGER])
    players[1].alive = False
    assert _text(engine.finish(room, Team.WOLF), "game_finished") == get_locale_string(
        "WolfWins"
    ).strip()

    room, _ = _room([Role.WOLF, Role.WOLF])
    assert _text(engine.finish(room, Team.WOLF), "game_finished") == get_locale_string(
        "WolvesWin"
    ).strip()


def test_win_announcement_precedes_the_roster_and_follows_the_overpower_kill() -> None:
    """Werewolf.cs:4858-4868 + 4906 —— Overpower 击杀播报在胜利文案之前，
    名单是独立的第二条群消息。"""
    room, _ = _room([Role.SERIAL_KILLER, Role.VILLAGER])

    events = GameRoomEngine()._check_for_game_end(room)

    kinds = _kinds(events)
    assert kinds.index("serial_killer_overpower") < kinds.index("game_finished")
    assert kinds.index("game_finished") < kinds.index("game_summary")
    assert kinds.index("game_summary") < kinds.index("personal_result")


def test_overpower_messages_use_official_locale() -> None:
    """Werewolf.cs:4835-4868 —— ArsonistWinsOverpower / SerialKillerWinsOverpower。"""
    room, _ = _room([Role.ARSONIST, Role.VILLAGER])
    events = GameRoomEngine()._check_for_game_end(room)
    assert _text(events, "arsonist_overpower") == get_locale_string(
        "ArsonistWinsOverpower", None, "P1（1号）", "P2（2号）"
    ).strip()

    room, _ = _room([Role.SERIAL_KILLER, Role.VILLAGER])
    events = GameRoomEngine()._check_for_game_end(room)
    assert _text(events, "serial_killer_overpower") == get_locale_string(
        "SerialKillerWinsOverpower", None, "P1（1号）", "P2（2号）"
    ).strip()


# ---------------------------------------------------------------------------
# 4906-4923 结算名单
# ---------------------------------------------------------------------------


def test_roster_none_mode_lists_names_in_death_order_without_roles() -> None:
    """Werewolf.cs:4908-4911 —— ShowRolesEnd=None 只给「幸存者们 x / y」+ 纯名单，
    顺序按 OrderBy(TimeDied)：存活者在前，死者按死亡先后。"""
    rules = replace(ruleset_official(), show_roles_end="None")
    room, players = _room([Role.WOLF, Role.VILLAGER, Role.SEER], rules=rules)
    players[0].alive, players[0].metadata["death_sequence"] = False, 2
    players[1].alive, players[1].metadata["death_sequence"] = False, 1

    summary = _text(GameRoomEngine().finish(room, Team.VILLAGE), "game_summary")
    lines = summary.splitlines()

    assert lines[0] == f"{get_locale_string('PlayersAlive')}: 1 / 3"
    assert lines[1:4] == ["P3（3号）", "P2（2号）", "P1（1号）"]
    assert role_display_name(Role.WOLF) not in summary


def test_roster_all_mode_marks_status_lover_and_result() -> None:
    """Werewolf.cs:4912-4917 —— ShowRolesEnd=All 输出 存活/死亡/已逃跑 + 角色 +
    ❤️ 恋人标记 + 胜负。"""
    rules = replace(ruleset_official(), show_roles_end="All")
    room, players = _room([Role.WOLF, Role.VILLAGER, Role.SEER], rules=rules)
    players[1].alive, players[1].metadata["death_sequence"] = False, 1
    players[2].alive, players[2].metadata["fled"] = False, True
    players[2].metadata["death_sequence"] = 2
    players[0].lover_id = "u2"

    summary = _text(GameRoomEngine().finish(room, Team.WOLF), "game_summary")
    lines = summary.splitlines()

    assert lines[1] == (
        f"P1（1号）: {get_locale_string('Alive')} - {role_display_name(Role.WOLF)}❤️ "
        f"{get_locale_string('Won')}"
    )
    assert lines[2] == (
        f"P2（2号）: {get_locale_string('Dead')} - {role_display_name(Role.VILLAGER)} "
        f"{get_locale_string('Lost')}"
    )
    assert lines[3] == (
        f"P3（3号）: {get_locale_string('RanAway')} - {role_display_name(Role.SEER)} "
        f"{get_locale_string('Lost')}"
    )


def test_roster_living_mode_lists_survivors_in_team_order() -> None:
    """Werewolf.cs:4918-4923 —— 默认模式只列活人，OrderBy(Team) 按 ITeam 声明顺序：
    Village 在 Wolf 之前。"""
    room, players = _room([Role.WOLF, Role.VILLAGER, Role.SEER])
    players[2].alive = False

    summary = _text(GameRoomEngine().finish(room, Team.WOLF), "game_summary")
    lines = summary.splitlines()

    assert lines[0] == get_locale_string("RemainingPlayersEnd")
    assert lines[1].startswith("P2（2号）")
    assert get_locale_string("VillageTeamEnd") in lines[1]
    assert lines[2].startswith("P1（1号）")
    assert get_locale_string("WolfTeamEnd") in lines[2]
    assert "P3（3号）" not in summary


def test_roster_ends_with_official_game_length() -> None:
    """Werewolf.cs:4924 —— 名单末尾追加 EndTime，参数是 hh:mm:ss 的总用时。"""
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    room, _ = _room([Role.WOLF, Role.VILLAGER])
    room.statistics["game_started_at"] = clock.value.isoformat()
    clock.advance(3725)

    summary = _text(GameRoomEngine(clock=clock).finish(room, Team.WOLF), "game_summary")

    assert summary.splitlines()[-1] == get_locale_string("EndTime", None, "01:02:05")


def test_roster_omits_game_length_when_the_game_never_started() -> None:
    """没有 game_started_at（例如仅做结算断言）时不得输出脏时间。"""
    room, _ = _room([Role.WOLF, Role.VILLAGER])
    room.statistics.pop("game_started_at", None)

    summary = _text(GameRoomEngine().finish(room, Team.WOLF), "game_summary")

    assert get_locale_string("EndTime", None, "") not in summary
    assert "游戏进行了" not in summary
