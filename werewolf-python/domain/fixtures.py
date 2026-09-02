from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dataclasses import replace

from .engine import GameRoomEngine
from .models import DomainEvent, GameMode, GameRoom, Role
from .rules import ruleset_official


@dataclass
class FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class SeededRandom:
    """对拍用例使用的随机源；种子本身也是用例输入的一部分。"""

    def __init__(self, seed: int):
        self._random = random.Random(seed)

    def shuffle(self, values: list[Any]) -> None:
        self._random.shuffle(values)

    def choice(self, values: list[Any]) -> Any:
        return self._random.choice(values)

    def randrange(self, stop: int) -> int:
        return self._random.randrange(stop)


PlayerIdFactory = Callable[[int], str]


def sequential_player_ids(prefix: str = "player") -> PlayerIdFactory:
    return lambda seat: f"{prefix}-{seat}"


@dataclass(frozen=True)
class FixtureResult:
    phase: str
    day: int
    winner: str | None
    winners: tuple[str, ...]
    players: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "day": self.day,
            "winner": self.winner,
            "winners": list(self.winners),
            "players": list(self.players),
            "events": list(self.events),
        }


class OfficialFixtureRunner:
    """以注入的时钟、随机源和玩家 ID 运行一份 JSON 对拍用例。"""

    def run(self, fixture: dict[str, Any]) -> FixtureResult:
        clock = FrozenClock(_parse_time(fixture.get("clock", "2026-01-01T00:00:00+00:00")))
        engine = GameRoomEngine(rng=SeededRandom(int(fixture.get("seed", 0))), clock=clock)
        rules = ruleset_official(mode=GameMode(fixture.get("mode", GameMode.NORMAL.value)))
        overrides = dict(fixture.get("rules") or {})
        if overrides:
            if "mode" in overrides:
                overrides["mode"] = GameMode(overrides["mode"])
            rules = replace(rules, **overrides)
            rules.validate()
        room = engine.create_room(str(fixture.get("room_id", "fixture-room")), rules)
        player_ids = _fixture_player_ids(fixture)
        events: list[DomainEvent] = []

        for seat, name in enumerate(fixture.get("players", ()), 1):
            events.extend(engine.join(room, player_ids[seat - 1], str(name)))
        if room.players:
            events.extend(engine.start(room, room.host_user_id or room.players[0].user_id, force=True))

        operations = fixture.get("operations", ())
        if any(operation.get("kind") == "set_roles" for operation in operations):
            # Branch fixtures supply their own role setup, so only compare the
            # resulting branch rather than the discarded random opening deal.
            events.clear()
        for operation in operations:
            events.extend(self._apply_operation(engine, room, operation, clock))
        return _result(room, events)

    def assert_expected(self, fixture: dict[str, Any]) -> FixtureResult:
        result = self.run(fixture)
        expected = fixture.get("expected")
        if expected is not None and result.as_dict() != expected:
            raise AssertionError(
                "对拍用例输出不一致\n"
                + json.dumps({"expected": expected, "actual": result.as_dict()}, ensure_ascii=False, indent=2)
            )
        return result

    @staticmethod
    def _apply_operation(
        engine: GameRoomEngine, room: GameRoom, operation: dict[str, Any], clock: FrozenClock
    ) -> list[DomainEvent]:
        kind = str(operation["kind"])
        if kind == "set_roles":
            assignments = operation["roles"]
            for player, role in zip(room.players, assignments, strict=True):
                player.role = Role(role)
                player.original_role = player.role
                player.has_used_ability = False
                player.bitten = False
                player.lover_id = None
                player.role_model = None
                player.metadata = {"role_actions": []}
            return []
        if kind == "set_state":
            player = next(item for item in room.players if item.user_id == str(operation["player"]))
            for key, value in operation.get("values", {}).items():
                if key == "role":
                    player.role = Role(value)
                elif key == "lover_id":
                    player.lover_id = value
                else:
                    setattr(player, key, value)
            player.metadata.update(operation.get("metadata", {}))
            return []
        if kind == "set_statistics":
            room.statistics.update(operation.get("values", {}))
            return []
        if kind == "night_action":
            return engine.submit_night_action(
                room, str(operation["actor"]), str(operation["action"]), operation.get("target")
            )
        if kind == "resolve_night":
            return engine.resolve_night(room)
        if kind == "day_action":
            return engine.submit_day_action(
                room, str(operation["actor"]), str(operation["action"]), operation.get("target")
            )
        if kind == "start_vote":
            return engine.start_vote(room)
        if kind == "vote":
            voter = next((item for item in room.players if item.user_id == str(operation["actor"])), None)
            if voter is None or not voter.alive:
                return []
            return engine.submit_vote(room, str(operation["actor"]), operation.get("target"))
        if kind == "resolve_vote":
            return engine.resolve_vote(room)
        if kind == "timeout":
            clock.advance(int(operation.get("seconds", 0)))
            return engine.on_timeout(room)
        if kind == "check_winners":
            return engine._check_for_game_end(room)
        if kind == "refresh_identity":
            return engine._identity_events(room)
        if kind == "night_prompts":
            return engine._night_prompts(room)
        raise ValueError(f"未知的对拍用例操作：{kind}")


def load_fixture(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fixture_player_ids(fixture: dict[str, Any]) -> list[str]:
    names = fixture.get("players", ())
    configured = fixture.get("player_ids")
    if configured is None:
        factory = sequential_player_ids(str(fixture.get("player_id_prefix", "player")))
        return [factory(seat) for seat in range(1, len(names) + 1)]
    if len(configured) != len(names):
        raise ValueError("player_ids 的长度必须与玩家数一致")
    return [str(value) for value in configured]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _result(room: GameRoom, events: list[DomainEvent]) -> FixtureResult:
    players = tuple(
        {
            "id": player.user_id,
            "role": player.role.value if player.role else None,
            "alive": player.alive,
            "team": player.team.value if player.team else None,
            "lover_id": player.lover_id,
            "kill_method": player.kill_method,
            "won": player.won,
        }
        for player in room.players
    )
    normalized_events = tuple(
        {
            "kind": event.kind,
            "public": event.public,
            "target_user_id": event.target_user_id,
            "metadata": event.metadata,
        }
        for event in events
    )
    return FixtureResult(
        phase=room.phase.value,
        day=room.day,
        winner=room.winner.value if room.winner else None,
        winners=room.winners,
        players=players,
        events=normalized_events,
    )
