"""独立官方对照审计：SendGif 随附文案（遇害者本人的私聊死亡告知）。

官方 `Werewolf Node/Werewolf.cs` 在若干击杀点调用 `SendGif(GetLocaleString(key), gif, id)`，
其中 GIF 素材是 Telegram file_id（QQ 端不适用），但随附文案属于游戏流程消息，必须保留：

* 3203  `Burn`                    —— 纵火者引燃，私聊被烧死者
* 3312 / 3365 / 3434 `WolvesEatYou`  —— 狼人吃人（酒鬼 / 猎人 / 默认三个分支）
* 3481  `WolvesSpottedYou`        —— 狼群发现掘墓人
* 3533  `SerialKillerKilledYou`   —— 连环杀手击杀
* 3546  `SerialKillerSpottedYou`  —— 连环杀手发现掘墓人

同时校对 3479-3481 / 3544-3546 私聊凶手的 `WolvesSpotted` / `SerialKillerSpotted`，
以及 4399-4404 掘墓人被发现后按凶手身份区分的公开播报
（`KillerSpottedDiggerPublic` / `WolvesSpottedDiggerPublic`）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from domain.engine import GameRoomEngine
from domain.fixtures import FrozenClock, SeededRandom
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
from tests.test_official_parity import _events


def _notices(result: dict, key: str) -> list[dict]:
    return [
        event
        for event in _events(result, "victim_death_notice")
        if event["metadata"].get("locale_key") == key
    ]


def _spot_case(killer_role: Role) -> tuple[GameRoom, Player, Player, list[DomainEvent]]:
    """构造「掘墓人必定被发现」的最小场景，直接驱动 `_maybe_spot_grave_digger`。"""
    rules = replace(ruleset_official(), grave_digger_spot_chance=100)
    killer = Player("u1", "P1", 1, role=killer_role)
    digger = Player("u2", "P2", 2, role=Role.GRAVE_DIGGER)
    other = Player("u3", "P3", 3, role=Role.VILLAGER)
    digger.metadata["dug_graves_last_night"] = 1
    room = GameRoom(
        "audit-spot", rules, phase=GamePhase.NIGHT, day=2, players=[killer, digger, other]
    )
    engine = GameRoomEngine(
        rng=SeededRandom(1),
        clock=FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )
    events: list[DomainEvent] = []
    return room, killer, digger, events, engine


# ---------------------------------------------------------------------------
# WolvesEatYou
# ---------------------------------------------------------------------------


def test_wolves_eat_sends_private_notice_to_victim() -> None:
    """Werewolf.cs:3434 —— default 分支吃人后私聊被吃者本人。"""
    result = _run(_base(
        ["Wolf", "Villager", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u2")["alive"] is False
    notices = _notices(result, "WolvesEatYou")
    assert len(notices) == 1
    assert notices[0]["public"] is False
    assert notices[0]["target_user_id"] == "u2"


def test_wolves_eat_drunk_shares_the_same_private_notice() -> None:
    """Werewolf.cs:3311-3312 —— 酒鬼分支（未被头狼咬中）同样私聊 WolvesEatYou。"""
    result = _run(_base(
        ["Wolf", "Drunk", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u2")["alive"] is False
    assert [event["target_user_id"] for event in _notices(result, "WolvesEatYou")] == ["u2"]


def test_hunter_eaten_after_return_fire_gets_private_notice() -> None:
    """Werewolf.cs:3365 —— 猎人反杀后若狼群多于一头，猎人仍被吃并收到 WolvesEatYou。"""
    result = _run(_base(
        ["Wolf", "Wolf", "Hunter", "Villager", "Villager"],
        rules={"hunter_kill_wolf_chance_base": 100},
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u3"},
            {"kind": "night_action", "actor": "u2", "action": "wolf", "target": "u3"},
        ],
    ))
    assert _player(result, "u3")["alive"] is False
    assert _player(result, "u3")["kill_method"] == KillMethod.EAT.value
    assert [event["target_user_id"] for event in _notices(result, "WolvesEatYou")] == ["u3"]


def test_wise_elder_first_attack_has_no_victim_notice() -> None:
    """Werewolf.cs:3398-3404 —— 长老首次挡下攻击不走 KillPlayer，也就没有 SendGif。"""
    result = _run(_base(
        ["Wolf", "WiseElder", "Villager", "Villager", "Villager"],
        operations=[{"kind": "night_action", "actor": "u1", "action": "wolf", "target": "u2"}],
    ))
    assert _player(result, "u2")["alive"] is True
    assert _notices(result, "WolvesEatYou") == []


# ---------------------------------------------------------------------------
# SerialKillerKilledYou
# ---------------------------------------------------------------------------


def test_serial_killer_kill_sends_private_notice() -> None:
    """Werewolf.cs:3533 —— 连环杀手得手后私聊被杀者本人。"""
    result = _run(_base(
        ["SerialKiller", "Villager", "Villager", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u1", "action": "serial_kill", "target": "u2"}
        ],
    ))
    assert _player(result, "u2")["alive"] is False
    notices = _notices(result, "SerialKillerKilledYou")
    assert len(notices) == 1
    assert notices[0]["public"] is False
    assert notices[0]["target_user_id"] == "u2"


def test_guard_blocked_serial_killer_has_no_victim_notice() -> None:
    """Werewolf.cs:3516-3527 —— 被守卫挡下时不调用 KillPlayer，也没有 SendGif。"""
    result = _run(_base(
        ["SerialKiller", "Villager", "GuardianAngel", "Villager", "Villager"],
        operations=[
            {"kind": "night_action", "actor": "u3", "action": "guard", "target": "u2"},
            {"kind": "night_action", "actor": "u1", "action": "serial_kill", "target": "u2"},
        ],
    ))
    assert _player(result, "u2")["alive"] is True
    assert _notices(result, "SerialKillerKilledYou") == []


# ---------------------------------------------------------------------------
# Burn
# ---------------------------------------------------------------------------


def test_arsonist_ignite_sends_private_notice() -> None:
    """Werewolf.cs:3203 —— 引燃后逐个私聊被烧死者。"""
    arsonist = Player("u1", "P1", 1, role=Role.ARSONIST)
    first = Player("u2", "P2", 2, role=Role.VILLAGER, doused=True)
    second = Player("u3", "P3", 3, role=Role.VILLAGER, doused=True)
    bystander = Player("u4", "P4", 4, role=Role.VILLAGER)
    room = GameRoom(
        "audit-burn",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        players=[arsonist, first, second, bystander],
    )
    arsonist.metadata["arsonist_ignite"] = True
    action = NightAction(arsonist.user_id, QuestionType.DOUSE.value, None, room.day)
    engine = GameRoomEngine()

    events = engine._resolve_arsonist(room, {QuestionType.DOUSE.value: [action]})

    assert first.kill_method == KillMethod.BURN.value
    assert second.kill_method == KillMethod.BURN.value
    notices = [
        event
        for event in events
        if event.kind == "victim_death_notice" and event.metadata["locale_key"] == "Burn"
    ]
    assert [event.target_user_id for event in notices] == ["u2", "u3"]
    assert all(event.public is False for event in notices)


def test_guard_blocked_ignite_has_no_victim_notice() -> None:
    """Werewolf.cs:3196-3201 —— 守卫挡下引燃时不调用 KillPlayer，也没有 SendGif。"""
    arsonist = Player("u1", "P1", 1, role=Role.ARSONIST)
    guardian = Player("u2", "P2", 2, role=Role.GUARDIAN_ANGEL)
    target = Player("u3", "P3", 3, role=Role.VILLAGER, doused=True)
    room = GameRoom(
        "audit-burn-guard",
        ruleset_official(),
        phase=GamePhase.NIGHT,
        day=2,
        players=[arsonist, guardian, target],
    )
    arsonist.metadata["arsonist_ignite"] = True
    action = NightAction(arsonist.user_id, QuestionType.DOUSE.value, None, room.day)

    events = GameRoomEngine()._resolve_arsonist(
        room, {QuestionType.DOUSE.value: [action]}, guardian, target.user_id
    )

    assert target.alive is True
    assert [event for event in events if event.kind == "victim_death_notice"] == []


# ---------------------------------------------------------------------------
# WolvesSpottedYou / SerialKillerSpottedYou
# ---------------------------------------------------------------------------


def test_wolves_spot_grave_digger_notifies_pack_and_digger() -> None:
    """Werewolf.cs:3478-3481 —— 逐个私聊投票狼 WolvesSpotted，并私聊掘墓人 WolvesSpottedYou。"""
    room, killer, digger, events, engine = _spot_case(Role.WOLF)
    pack = [killer, Player("u4", "P4", 4, role=Role.WOLF)]
    room.players.append(pack[1])
    engine._maybe_spot_grave_digger(room, killer, events, killers=pack)

    assert digger.alive is False
    assert digger.kill_method == KillMethod.SPOTTED.value
    told = [event for event in events if event.kind == "wolves_spotted_digger"]
    assert [event.target_user_id for event in told] == ["u1", "u4"]
    assert all(event.public is False for event in told)
    notice = next(event for event in events if event.kind == "victim_death_notice")
    assert notice.metadata["locale_key"] == "WolvesSpottedYou"
    assert notice.public is False and notice.target_user_id == "u2"


def test_serial_killer_spots_grave_digger_notifies_killer_and_digger() -> None:
    """Werewolf.cs:3544-3546 —— 私聊连环杀手 SerialKillerSpotted，
    并私聊掘墓人 SerialKillerSpottedYou。"""
    room, killer, digger, events, engine = _spot_case(Role.SERIAL_KILLER)
    engine._maybe_spot_grave_digger(room, killer, events)

    assert digger.alive is False
    told = [event for event in events if event.kind == "serial_killer_spotted_digger"]
    assert [event.target_user_id for event in told] == ["u1"]
    notice = next(event for event in events if event.kind == "victim_death_notice")
    assert notice.metadata["locale_key"] == "SerialKillerSpottedYou"
    assert notice.public is False and notice.target_user_id == "u2"


def test_grave_digger_is_not_spotted_without_digging() -> None:
    """Werewolf.cs:3471 / 3536 —— DugGravesLastNight > 0 才进入发现判定。"""
    room, killer, digger, events, engine = _spot_case(Role.SERIAL_KILLER)
    digger.metadata["dug_graves_last_night"] = 0
    engine._maybe_spot_grave_digger(room, killer, events)
    assert digger.alive is True
    assert events == []


def test_spotted_public_notice_differs_by_killer_role() -> None:
    """Werewolf.cs:4399-4404 —— KilledByRole 为连环杀手时用 KillerSpottedDiggerPublic，
    否则用 WolvesSpottedDiggerPublic。"""
    wolf_room, wolf, _, wolf_events, wolf_engine = _spot_case(Role.WOLF)
    wolf_engine._maybe_spot_grave_digger(wolf_room, wolf, wolf_events, killers=[wolf])
    wolf_public = next(event for event in wolf_events if event.kind == "player_died")

    sk_room, sk, _, sk_events, sk_engine = _spot_case(Role.SERIAL_KILLER)
    sk_engine._maybe_spot_grave_digger(sk_room, sk, sk_events)
    sk_public = next(event for event in sk_events if event.kind == "player_died")

    assert "狼群" in wolf_public.text
    assert "刀伤" in sk_public.text
    assert wolf_public.text != sk_public.text
    assert wolf_public.public is True and sk_public.public is True


def test_every_victim_notice_key_is_covered() -> None:
    """守卫测试：官方五个 SendGif 文案键必须都能取到中文文案。"""
    victim = Player("u9", "P9", 9, role=Role.VILLAGER)
    keys = [
        "WolvesEatYou",
        "WolvesSpottedYou",
        "SerialKillerKilledYou",
        "SerialKillerSpottedYou",
        "Burn",
    ]
    texts = {key: GameRoomEngine._victim_notice(victim, key).text for key in keys}
    assert all(texts.values())
    assert len(set(texts.values())) == len(keys)
