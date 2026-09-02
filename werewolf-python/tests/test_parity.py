from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.fixtures import OfficialFixtureRunner, load_fixture
from domain.models import GamePhase, KillMethod, Role, Team


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _run(fixture: dict) -> dict:
    return OfficialFixtureRunner().run(fixture).as_dict()


def _base(roles: list[str], **kwargs) -> dict:
    count = len(roles)
    fixture = {
        "seed": kwargs.pop("seed", 1),
        "clock": "2026-01-01T00:00:00+00:00",
        "room_id": kwargs.pop("room_id", "parity"),
        "player_ids": [f"u{index}" for index in range(1, count + 1)],
        "players": [f"P{index}" for index in range(1, count + 1)],
        "operations": [{"kind": "set_roles", "roles": roles}, *kwargs.pop("operations", ())],
    }
    fixture.update(kwargs)
    return fixture


def _player(result: dict, user_id: str) -> dict:
    return next(player for player in result["players"] if player["id"] == user_id)


def test_example_lifecycle_golden() -> None:
    OfficialFixtureRunner().assert_expected(load_fixture(FIXTURE_DIR / "example-lifecycle.json"))


def test_all_json_fixtures() -> None:
    files = sorted(path for path in FIXTURE_DIR.glob("*.json") if path.name != "README.md")
    assert files
    for path in files:
        OfficialFixtureRunner().assert_expected(load_fixture(path))


def test_guard_blocks_wolf_including_harlot() -> None:
    result = _run(_base(
        ["Wolf", "GuardianAngel", "Harlot", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "guard", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "visit", "target": "u5"},
        ],
    ))
    assert _player(result, "u3")["alive"] is True
    assert result["phase"] == GamePhase.DAY.value


def test_cursed_turns_immediately() -> None:
    result = _run(_base(
        ["Wolf", "Cursed", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u2")["alive"] is True
    assert _player(result, "u2")["role"] == Role.WOLF.value


def test_alpha_bite_is_delayed() -> None:
    result = _run(_base(
        ["AlphaWolf", "Villager", "Villager", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 100},
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u2")["alive"] is True
    assert _player(result, "u2")["role"] == Role.VILLAGER.value
    assert result["phase"] == GamePhase.DAY.value


def test_alpha_bite_transforms_next_night() -> None:
    result = _run(_base(
        ["AlphaWolf", "Villager", "Villager", "Villager", "Villager"],
        rules={"alpha_wolf_conversion_chance": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u5"},
            {"kind": "vote", "actor": "u2", "target": "u5"},
            {"kind": "vote", "actor": "u3", "target": "u5"},
            {"kind": "vote", "actor": "u4", "target": "u5"},
            {"kind": "vote", "actor": "u5", "target": "u4"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert _player(result, "u2")["role"] == Role.WOLF.value
    assert result["phase"] in {GamePhase.NIGHT.value, GamePhase.FINISHED.value}


def test_seer_sees_lycan_as_villager_and_wolfman_as_wolf() -> None:
    lycan = OfficialFixtureRunner().run(_base(
        ["Seer", "Wolf", "Lycan", "WolfMan", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u3"},
        ],
    ))
    wolfman = OfficialFixtureRunner().run(_base(
        ["Seer", "Wolf", "Lycan", "WolfMan", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u3", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u1", "action": "seer", "target": "u4"},
        ],
    ))
    lycan_event = next(event for event in lycan.events if event["kind"] == "seer_result")
    wolfman_event = next(event for event in wolfman.events if event["kind"] == "seer_result")
    assert lycan_event["metadata"]["seen_role"] == Role.VILLAGER.value
    assert wolfman_event["metadata"]["seen_role"] == Role.WOLF.value


def test_lovers_death_chain() -> None:
    result = _run(_base(
        ["Wolf", "Cupid", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "cupid", "target": "u3"},
            {"kind": "night_action", "actor": "u2", "action": "cupid", "target": "u4"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u4")["alive"] is False
    assert _player(result, "u4")["kill_method"] == KillMethod.LOVER_DIED.value


def test_wild_child_turns_when_idol_dies() -> None:
    result = _run(_base(
        ["Wolf", "WildChild", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "idol", "target": "u3"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u2")["role"] == Role.WOLF.value
    assert _player(result, "u3")["alive"] is False


def test_doppelganger_copies_dead_model() -> None:
    result = _run(_base(
        ["Wolf", "Doppelgänger", "Seer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "copy", "target": "u3"},
            {"kind": "night_action", "actor": "u3", "action": "seer", "target": "u1"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u2")["role"] == Role.SEER.value


def test_apprentice_becomes_seer() -> None:
    result = _run(_base(
        ["Wolf", "Seer", "ApprenticeSeer", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u2", "action": "seer", "target": "u1"},
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"},
        ],
    ))
    assert _player(result, "u3")["role"] == Role.SEER.value


def test_vote_rejects_change() -> None:
    runner = OfficialFixtureRunner()
    fixture = _base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u2", "target": "u1"},
            {"kind": "vote", "actor": "u2", "target": "u3"},
        ],
    )
    with pytest.raises(Exception):
        runner.run(fixture)


def test_prince_survives_first_lynch() -> None:
    result = _run(_base(
        ["Wolf", "Prince", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "vote", "actor": "u5", "target": "u2"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert _player(result, "u2")["alive"] is True
    assert result["phase"] == GamePhase.NIGHT.value


def test_tanner_wins_only_on_lynch() -> None:
    result = _run(_base(
        ["Wolf", "Tanner", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u2"},
            {"kind": "vote", "actor": "u5", "target": "u2"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert result["winner"] == Team.TANNER.value
    assert _player(result, "u2")["won"] is True
    assert _player(result, "u1")["won"] is False


def test_vote_tie_lynches_nobody() -> None:
    result = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "vote", "actor": "u2", "target": "u1"},
            {"kind": "vote", "actor": "u3", "target": "u2"},
            {"kind": "vote", "actor": "u4", "target": "u1"},
            {"kind": "vote", "actor": "u5", "target": "skip"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert _player(result, "u1")["alive"] is True
    assert _player(result, "u2")["alive"] is True
    assert any(event["kind"] == "vote_tie" for event in result["events"])


def test_pacifist_cancels_lynch() -> None:
    result = _run(_base(
        ["Wolf", "Pacifist", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "day_action", "actor": "u2", "action": "pacifist", "target": "yes"},
            # Werewolf.cs:2559-2564：LynchCycle 检测到 _pacifistUsed 立即 return，
            # 处决菜单不会发出，本轮直接跳到下一夜。
            {"kind": "timeout", "seconds": 60},
        ],
    ))
    assert _player(result, "u3")["alive"] is True
    assert any(event["kind"] == "pacifist_vote" for event in result["events"])
    assert result["phase"] == "night"


def test_snow_wolf_counts_for_majority() -> None:
    result = _run(_base(
        ["Wolf", "SnowWolf", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "night_action", "actor": "u2", "action": "freeze", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u1", "target": "u3"},
            {"kind": "vote", "actor": "u2", "target": "u3"},
            {"kind": "vote", "actor": "u4", "target": "u3"},
            {"kind": "vote", "actor": "u5", "target": "u3"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert result["winner"] == Team.WOLF.value


def test_village_wins_when_wolves_gone() -> None:
    result = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "skip"},
            {"kind": "timeout", "seconds": 60},
            {"kind": "vote", "actor": "u2", "target": "u1"},
            {"kind": "vote", "actor": "u3", "target": "u1"},
            {"kind": "vote", "actor": "u4", "target": "u1"},
            {"kind": "vote", "actor": "u5", "target": "u1"},
            {"kind": "vote", "actor": "u1", "target": "u2"},
            {"kind": "resolve_vote"},
        ],
    ))
    assert result["winner"] == Team.VILLAGE.value


def test_serial_killer_blocks_other_wins() -> None:
    result = _run(_base(
        ["Wolf", "SerialKiller", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            {"kind": "night_action", "actor": "u2", "action": "serial_kill", "target": "u4"},
        ],
    ))
    assert result["winner"] is None
    assert result["phase"] == GamePhase.DAY.value
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u4")["alive"] is False


def test_thief_does_not_block_village_and_cannot_win_as_own_team() -> None:
    result = _run(_base(
        ["Thief", "Villager", "Villager", "Villager", "Villager"],
        operations=[{"kind": "check_winners"}],
    ))
    assert result["winner"] == Team.VILLAGE.value
    assert all(_player(result, f"u{index}")["won"] is False for index in range(1, 2))
    assert _player(result, "u1")["team"] == Team.THIEF.value


def test_json_roundtrip_dump() -> None:
    """Keep a machine-readable dump for fixtures that only have operations."""
    payload = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert payload["players"][1]["kill_method"] == "Eat"
    json.dumps(payload)
