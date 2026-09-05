from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from adapters.napcat.adapter import NapCatAdapter
from adapters.napcat.events import (
    message_text,
    normalize_group_message,
    normalize_private_message,
)
from application.commands import parse_command
from application.contracts import (
    PermanentSendError,
    PlatformEvent,
    PlatformSession,
    SessionType,
)
from application.service import GameApplication
from domain.engine import GameRoomEngine, STALE_TIMEOUT_SECONDS
from domain.models import DomainEvent, GamePhase, GameRoom, NightAction, Player, Role, Team
from domain.rules import GameRuleError, ruleset_official
from infrastructure.config import Settings
from infrastructure.db import DeliveryRecord, PostgreSQLStore


class InMemoryRoomStore:
    """用快照模拟数据库读写，确保测试走过重启后的还原路径。"""

    def __init__(self, rooms: list[GameRoom]):
        self.snapshots = {room.session_id: room.snapshot() for room in rooms}
        self.claimed_events: set[str] = set()
        self.committed: list[tuple[GameRoom | None, list[object]]] = []

    async def list_active_rooms(self) -> list[GameRoom]:
        active = {GamePhase.LOBBY, GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}
        return [
            GameRoom.from_snapshot(deepcopy(snapshot))
            for snapshot in self.snapshots.values()
            if GamePhase(snapshot["phase"]) in active
        ]

    async def get_room(self, session_id: str) -> GameRoom | None:
        snapshot = self.snapshots.get(session_id)
        return GameRoom.from_snapshot(deepcopy(snapshot)) if snapshot else None

    async def begin_event(self, event_id: str, _session_key: str) -> tuple[bool, list[object] | None]:
        if event_id in self.claimed_events:
            return False, []
        self.claimed_events.add(event_id)
        return True, None

    async def commit_result(self, *, room, event_id, messages, **_kwargs) -> None:
        if room is not None:
            self.snapshots[room.session_id] = room.snapshot()
        self.committed.append((room, list(messages)))

    async def delete_room(self, session_id: str) -> None:
        self.snapshots.pop(session_id, None)


class InMemoryGroupConfigPool:
    """只实现群规则读写所需的数据库接口。"""

    def __init__(self):
        self.values: dict[str, str] = {}

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def fetchval(self, _query: str, group_id: str) -> str | None:
        return self.values.get(group_id)

    async def execute(self, _query: str, group_id: str, rules_json: str, _updated_by: str) -> None:
        self.values[group_id] = rules_json


class FakePlatform:
    """NapCat 适配器的最小替身：只提供应用层用到的三种群能力。"""

    def __init__(self, *, admin: bool = True):
        self.admin = admin
        self.cards: list[tuple[str, str, str]] = []

    async def is_group_admin(self, _group_id: str) -> bool:
        return self.admin

    async def set_group_card(self, group_id: str, user_id: str, card: str) -> bool:
        self.cards.append((group_id, user_id, card))
        return True


def _group_payload(text: str, *, user: str = "10001", group: str = "20001", **overrides) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1001,
        "group_id": int(group),
        "user_id": int(user),
        "time": 1767225600,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": int(user), "nickname": "沉潜", "card": "1号", "role": "member"},
    }
    payload.update(overrides)
    return payload


def test_parse_core_and_role_commands() -> None:
    assert parse_command("加入").name == "join"
    assert parse_command("/投票 3").name == "vote"
    assert parse_command("查验 2").name == "seer"
    assert parse_command("弃票").name == "abstain"
    assert parse_command("/abstain").name == "abstain"
    assert parse_command("未知指令").name == "unknown"


def test_parse_command_strips_cq_codes_so_at_is_optional() -> None:
    """NapCat 版不要求 @Bot：带不带 @ 都必须解析成同一条指令。"""
    assert parse_command("[CQ:at,qq=10000] /join").name == "join"
    assert parse_command("[CQ:at,qq=10000]加入").name == "join"
    assert parse_command("加入").name == "join"


def test_parse_official_game_start_commands() -> None:
    assert parse_command("/startgame").name == "create"
    assert parse_command("开始游戏").name == "create"
    assert parse_command("/players").name == "status"
    assert parse_command("/引燃").name == "ignite"


def test_parse_official_helper_commands() -> None:
    assert parse_command("/rolelist").name == "rolelist"
    assert parse_command("/grouplist").name == "grouplist"
    assert parse_command("/nextgame").name == "nextgame"
    assert parse_command("/setlang zh").name == "setlang"
    assert parse_command("/getlang").name == "getlang"
    assert parse_command("/ping").name == "ping"
    assert parse_command("/config vote_seconds=30").name == "config"
    assert parse_command("确认").name == "confirm"
    assert parse_command("重选").name == "reselect"


def test_private_step_action_can_select_reselect_and_cancel() -> None:
    app = GameApplication(object())
    room = GameRoom(
        session_id="20001",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("10001", "丘比特", 1, role=Role.CUPID),
            Player("10002", "甲", 2, role=Role.VILLAGER),
            Player("10003", "乙", 3, role=Role.VILLAGER),
        ],
    )
    event = PlatformEvent(
        event_id="step-1",
        session=PlatformSession(SessionType.C2C, "10001"),
        user_id="10001",
        display_name="丘比特",
        text="/cupid",
    )
    prompt = app._begin_pending_action(room, event, "cupid")
    assert "第 1/1 个目标" in prompt[0].text

    selected = asyncio.run(app._continue_pending_action(room, event, parse_command("2")))
    assert selected is not None and "发送“确认”提交" in selected[0].text
    restarted = asyncio.run(app._continue_pending_action(room, event, parse_command("重选")))
    assert restarted is not None and "第 1/1 个目标" in restarted[0].text
    cancelled = asyncio.run(app._continue_pending_action(room, event, parse_command("取消")))
    assert cancelled is not None and cancelled[0].text == "已取消本次选择。"
    assert room.statistics["pending_actions"] == {}


def test_room_snapshot_restores_deadline_actions_and_pending_choice() -> None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    room = GameRoom(
        session_id="restart-night",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=3,
        players=[
            Player("10001", "丘比特", 1, role=Role.CUPID),
            Player("10002", "甲", 2, role=Role.VILLAGER),
            Player("10003", "乙", 3, role=Role.VILLAGER),
        ],
        stage_deadline=deadline,
    )
    room.night_actions["10001"] = NightAction(
        actor_id="10001", action="恋人", target_id="10002", day=3, second_target_id="10003"
    )
    room.statistics["pending_actions"] = {
        "10001": {"action": "cupid", "targets": ["2"], "required": 2, "phase": "night", "day": 3}
    }

    restored = GameRoom.from_snapshot(room.snapshot())

    assert restored.stage_deadline == deadline
    assert restored.night_actions["10001"].second_target_id == "10003"
    assert restored.statistics["pending_actions"] == room.statistics["pending_actions"]


def test_service_restart_processes_overdue_room_and_skips_finished_room() -> None:
    overdue = GameRoom(
        session_id="restart-lobby",
        rules=ruleset_official(),
        phase=GamePhase.LOBBY,
        host_user_id="10001",
        stage_deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        players=[Player(f"1000{seat}", f"玩家{seat}", seat) for seat in range(1, 6)],
    )
    finished = GameRoom(
        session_id="finished-room",
        rules=ruleset_official(),
        phase=GamePhase.FINISHED,
        stage_deadline=None,
    )
    store = InMemoryRoomStore([overdue, finished])

    processed = asyncio.run(GameApplication(store).process_due_rooms())
    restored = asyncio.run(store.get_room("restart-lobby"))

    assert processed == 1
    assert restored is not None and restored.phase == GamePhase.NIGHT
    assert restored.stage_deadline is not None
    assert asyncio.run(store.list_active_rooms()) == [restored]


def test_engine_abandon_overdue_uses_timeout_notice() -> None:
    room = GameRoom(
        session_id="stale-night",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        host_user_id="10001",
        stage_deadline=datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SECONDS + 10),
        players=[Player("10001", "甲", 1), Player("10002", "乙", 2)],
    )
    events = GameRoomEngine().abandon_overdue(room)
    assert room.phase == GamePhase.CANCELLED
    assert room.stage_deadline is None
    assert [event.text for event in events] == ["该局因超时自动解散"]


def test_stale_overdue_room_is_abandoned_instead_of_advancing() -> None:
    overdue = GameRoom(
        session_id="zombie-night",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        host_user_id="10001",
        stage_deadline=datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SECONDS + 5),
        players=[
            Player("10001", "甲", 1, role=Role.VILLAGER),
            Player("10002", "乙", 2, role=Role.WOLF),
        ],
    )
    store = InMemoryRoomStore([overdue])

    processed = asyncio.run(GameApplication(store).process_due_rooms())

    assert processed == 1
    assert asyncio.run(store.get_room("zombie-night")) is None
    assert asyncio.run(store.list_active_rooms()) == []
    assert any(
        "该局因超时自动解散" in getattr(message, "text", "")
        for _room, messages in store.committed
        for message in messages
    )


def test_group_rule_config_survives_new_store_instance() -> None:
    pool = InMemoryGroupConfigPool()
    first_store = PostgreSQLStore("postgresql://test")
    first_store._pool = pool  # type: ignore[assignment]

    asyncio.run(first_store.save_group_rule_config("group-a", {"vote_seconds": 45}, "admin"))

    restarted_store = PostgreSQLStore("postgresql://test")
    restarted_store._pool = pool  # type: ignore[assignment]
    assert asyncio.run(restarted_store.get_group_rule_config("group-a")) == {"vote_seconds": 45}
    assert asyncio.run(restarted_store.get_group_rule_config("group-b")) == {}


def test_private_day_ability_requires_explicit_confirmation() -> None:
    app = GameApplication(object())
    room = GameRoom(
        session_id="20001",
        rules=ruleset_official(),
        phase=GamePhase.DAY,
        day=2,
        players=[Player("10001", "市长", 1, role=Role.MAYOR)],
    )
    event = PlatformEvent(
        event_id="step-day-1",
        session=PlatformSession(SessionType.C2C, "10001"),
        user_id="10001",
        display_name="市长",
        text="/mayor",
    )
    prompt = app._begin_pending_action(room, event, "mayor")
    assert "发送“确认”使用" in prompt[0].text


def test_private_mayor_reveal_can_begin_during_vote() -> None:
    app = GameApplication(object())
    room = GameRoom(
        session_id="20001",
        rules=ruleset_official(),
        phase=GamePhase.VOTE,
        day=1,
        players=[Player("10001", "市长", 1, role=Role.MAYOR)],
    )
    event = PlatformEvent(
        event_id="step-mayor-vote-1",
        session=PlatformSession(SessionType.C2C, "10001"),
        user_id="10001",
        display_name="市长",
        text="/mayor",
    )

    prompt = app._begin_pending_action(room, event, "mayor")
    assert "发送“确认”使用" in prompt[0].text


# ---------------- NapCat 事件归一化 ----------------


def test_normalize_group_message_uses_real_qq_numbers() -> None:
    event = normalize_group_message(_group_payload("加入"))
    assert event.session.session_type == SessionType.GROUP
    assert event.session.session_id == "20001"
    assert event.user_id == "10001"
    assert event.text == "加入"
    assert event.reply_message_id == "1001"
    assert event.group_role == "member"
    # 展示名取 QQ 昵称而不是群名片：名片开局后会被改成「N号」。
    assert event.display_name == "沉潜"


def test_normalize_group_message_drops_at_segments() -> None:
    payload = _group_payload(
        "/join",
        message=[
            {"type": "at", "data": {"qq": "10000"}},
            {"type": "text", "data": {"text": " /join "}},
        ],
        raw_message="[CQ:at,qq=10000] /join",
    )
    assert normalize_group_message(payload).text == "/join"
    # string 格式的消息体走 CQ 码兜底，结果必须一致。
    assert message_text({"message": "[CQ:at,qq=10000] /join"}) == "/join"


def test_normalize_group_message_recognizes_only_the_bot_mention() -> None:
    segment_payload = _group_payload(
        "加入",
        self_id=10000,
        message=[
            {"type": "at", "data": {"qq": "10000"}},
            {"type": "text", "data": {"text": " 加入"}},
        ],
    )
    string_payload = _group_payload(
        "[CQ:at,qq=10000] 加入",
        self_id=10000,
        message="[CQ:at,qq=10000] 加入",
    )
    other_payload = _group_payload(
        "加入",
        self_id=10000,
        message=[
            {"type": "at", "data": {"qq": "10009"}},
            {"type": "text", "data": {"text": " 加入"}},
        ],
    )
    assert normalize_group_message(segment_payload).is_bot_mentioned is True
    assert normalize_group_message(string_payload).is_bot_mentioned is True
    assert normalize_group_message(other_payload).is_bot_mentioned is False


def test_group_plain_chat_is_ignored_but_slash_or_bot_mention_is_dispatched() -> None:
    app = GameApplication(InMemoryRoomStore([]))
    plain = normalize_group_message(_group_payload("今晚吃什么", message_id=1101))
    slash = normalize_group_message(_group_payload("/join", message_id=1102))
    mentioned = normalize_group_message(
        _group_payload(
            "加入", message_id=1103, self_id=10000,
            message=[
                {"type": "at", "data": {"qq": "10000"}},
                {"type": "text", "data": {"text": " 加入"}},
            ],
        )
    )

    assert asyncio.run(app.handle_event(plain)) == []
    assert "/startgame" in asyncio.run(app.handle_event(slash))[0].text
    assert "/startgame" in asyncio.run(app.handle_event(mentioned))[0].text


def test_normalize_private_message_session_is_the_qq_number() -> None:
    event = normalize_private_message({
        "post_type": "message",
        "message_type": "private",
        "message_id": 2002,
        "user_id": 10001,
        "time": 1767225600,
        "raw_message": "查验 1",
        "message": [{"type": "text", "data": {"text": "查验 1"}}],
        "sender": {"user_id": 10001, "nickname": "沉潜"},
    })
    assert event.session.session_type == SessionType.C2C
    assert event.session.session_id == "10001"
    assert event.user_id == "10001"
    assert event.text == "查验 1"


def test_normalize_rejects_events_without_identifiers() -> None:
    with pytest.raises(ValueError):
        normalize_group_message({"message_id": 1, "user_id": 10001})
    with pytest.raises(ValueError):
        normalize_private_message({"user_id": 10001})


# ---------------- NapCat 投递与群管理能力 ----------------


def _napcat_adapter(result: object = None):
    adapter = NapCatAdapter("ws://127.0.0.1:3001", "", object())
    adapter.min_send_interval = 0.0
    calls: list[tuple[str, dict]] = []

    async def fake_call(action: str, **params):
        calls.append((action, params))
        return result

    adapter.call = fake_call  # type: ignore[assignment]
    return adapter, calls


def _record(**overrides) -> DeliveryRecord:
    data = dict(
        delivery_id="delivery-1",
        target_type=SessionType.GROUP,
        target_id="20001",
        text="天黑请闭眼。",
        room_id="20001",
        reply_to=None,
        source_event_id="napcat:1001",
        event_id=None,
        state_version=1,
        status="pending",
        attempts=0,
        last_error=None,
        next_attempt_at=None,
    )
    data.update(overrides)
    return DeliveryRecord(**data)


def test_group_delivery_calls_send_group_msg() -> None:
    adapter, calls = _napcat_adapter()
    asyncio.run(adapter.send_delivery(_record()))
    assert calls == [("send_group_msg", {"group_id": 20001, "message": "天黑请闭眼。"})]


def test_group_context_private_delivery_uses_temporary_session() -> None:
    adapter, calls = _napcat_adapter()
    record = _record(
        delivery_id="delivery-2",
        target_type=SessionType.C2C,
        target_id="10001",
        text="你的身份：预言家",
    )
    asyncio.run(adapter.send_delivery(record))
    assert calls == [
        (
            "send_msg",
            {
                "message_type": "private",
                "user_id": 10001,
                "group_id": 20001,
                "message": "你的身份：预言家",
            },
        )
    ]


def test_private_delivery_without_group_context_calls_send_private_msg() -> None:
    adapter, calls = _napcat_adapter()
    record = _record(
        delivery_id="delivery-2b",
        target_type=SessionType.C2C,
        target_id="10001",
        room_id=None,
        text="直接私聊",
    )
    asyncio.run(adapter.send_delivery(record))
    assert calls == [("send_private_msg", {"user_id": 10001, "message": "直接私聊"})]


def test_delivery_to_non_numeric_target_is_permanently_dead() -> None:
    """官方 openid 时代遗留的队列数据在 NapCat 下永远发不出去，直接判死。"""
    adapter, calls = _napcat_adapter()
    with pytest.raises(PermanentSendError):
        asyncio.run(adapter.send_delivery(_record(target_id="USEROPENID1")))
    assert calls == []


def test_is_group_admin_reads_role_and_caches() -> None:
    adapter, calls = _napcat_adapter({"role": "admin"})
    adapter.self_id = "10000"
    assert asyncio.run(adapter.is_group_admin("20001")) is True
    assert asyncio.run(adapter.is_group_admin("20001")) is True
    assert len(calls) == 1
    assert calls[0][0] == "get_group_member_info"


def test_is_group_admin_false_for_plain_member() -> None:
    adapter, _calls = _napcat_adapter({"role": "member"})
    adapter.self_id = "10000"
    assert asyncio.run(adapter.is_group_admin("20001")) is False


def test_set_group_card_failure_is_swallowed() -> None:
    adapter = NapCatAdapter("ws://127.0.0.1:3001", "", object())

    async def boom(_action: str, **_params):
        raise RuntimeError("权限不足")

    adapter.call = boom  # type: ignore[assignment]
    assert asyncio.run(adapter.set_group_card("20001", "10001", "1号")) is False


def test_group_member_names_prefer_nickname_over_card() -> None:
    members = [
        {"user_id": 10001, "nickname": "沉潜", "card": "1号"},
        {"user_id": 10002, "nickname": "", "card": "2号"},
    ]
    adapter, _calls = _napcat_adapter(members)
    assert asyncio.run(adapter.get_group_member_names("20001")) == {
        "10001": "沉潜",
        "10002": "2号",
    }


# ---------------- 应用层：私聊路由、名片同步、死者拦截 ----------------


def test_private_message_targets_the_players_qq_number() -> None:
    app = GameApplication(InMemoryRoomStore([]))
    source = normalize_group_message(_group_payload("/go"))
    message = asyncio.run(app._private_message("10002", "你的身份：预言家", source))
    assert message.target.session_type == SessionType.C2C
    assert message.target.session_id == "10002"
    assert message.reply_to is None
    assert message.event_id is None


def test_private_message_from_group_keeps_group_context_for_temporary_session() -> None:
    app = GameApplication(InMemoryRoomStore([]))
    source = normalize_group_message(
        {
            "message_type": "group",
            "group_id": 20001,
            "user_id": 10001,
            "message": "/身份",
            "message_id": 88,
        }
    )
    message = asyncio.run(app._private_message("10002", "你的身份：预言家", source))
    assert message.target == PlatformSession(SessionType.C2C, "10002")
    assert message.room_id == "20001"


def test_private_message_quotes_the_players_own_private_message() -> None:
    app = GameApplication(InMemoryRoomStore([]))
    source = normalize_private_message({
        "message_id": 2002,
        "user_id": 10002,
        "raw_message": "/seer",
        "sender": {"user_id": 10002, "nickname": "乙"},
    })
    message = asyncio.run(app._private_message("10002", "查验结果：好人", source))
    assert message.target.session_id == "10002"
    assert message.reply_to == "2002"
    assert message.room_id is None


def test_private_message_from_group_temporary_session_keeps_group_context() -> None:
    app = GameApplication(InMemoryRoomStore([]))
    source = normalize_private_message(
        {
            "message_id": 2003,
            "user_id": 10002,
            "group_id": 20001,
            "sub_type": "group",
            "raw_message": "/身份",
            "sender": {"user_id": 10002, "nickname": "乙"},
        }
    )
    message = asyncio.run(app._private_message("10002", "身份：预言家", source))
    assert message.room_id == "20001"


def test_identity_delivery_needs_no_binding_hint() -> None:
    room = GameRoom(
        session_id="20001",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("10001", "甲", 1, role=Role.SEER),
            Player("10002", "乙", 2, role=Role.WOLF),
        ],
    )
    app = GameApplication(InMemoryRoomStore([room]))
    source = normalize_group_message(_group_payload("/go"))
    events = [
        DomainEvent("game_started", "游戏开始。", public=True),
        DomainEvent("identity", "你的身份：预言家", public=False, target_user_id="10001"),
    ]
    messages = asyncio.run(app._events_to_messages(events, room, source))
    private = [item for item in messages if item.target.session_type == SessionType.C2C]
    public = [item for item in messages if item.target.session_type == SessionType.GROUP]
    assert [item.target.session_id for item in private] == ["10001"]
    assert "当前座位：1号(甲)" in private[0].text
    assert "玩家：1号(甲)、2号(乙)" in private[0].text
    # 不再有「无好友关系」「先私聊 /link」这类绑定引导。
    assert not any("好友" in item.text or "绑定" in item.text for item in public)


def test_duplicate_join_reports_the_requested_message() -> None:
    room = GameRoom(
        session_id="20001", rules=ruleset_official(), phase=GamePhase.LOBBY,
        players=[Player("10001", "甲", 1)],
    )
    with pytest.raises(GameRuleError, match="你已加入对局，无法重复加入"):
        GameRoomEngine().join(room, "10001", "甲")


def test_vote_details_keep_voter_target_and_seat_mapping() -> None:
    room = GameRoom(
        session_id="20001", rules=ruleset_official(), phase=GamePhase.VOTE,
        players=[
            Player("u1", "甲", 1, role=Role.VILLAGER),
            Player("u2", "乙", 2, role=Role.WOLF),
            Player("u3", "沐年", 3, role=Role.VILLAGER),
            Player("u4", "丙", 4, role=Role.VILLAGER),
            Player("u5", "丁", 5, role=Role.VILLAGER),
        ],
    )
    room.votes = {"u1": "u2", "u3": "u4", "u4": "u4", "u5": "u4", "u2": "u1"}
    events = GameRoomEngine().resolve_vote(room)
    details = next(event.text for event in events if event.kind == "vote_details")

    assert "3号(沐年) 投票 4号(丙)" in details
    assert "4号(丙) 已获得：3票" in details
    assert room.players[3].alive is False
    assert room.players[1].alive is True


def test_finish_always_reveals_every_role_and_team() -> None:
    room = GameRoom(
        session_id="20001", rules=ruleset_official(), phase=GamePhase.DAY,
        players=[
            Player("u1", "甲", 1, role=Role.SEER),
            Player("u2", "乙", 2, role=Role.WOLF, alive=False),
        ],
    )
    summary = next(event.text for event in GameRoomEngine().finish(room, Team.VILLAGE) if event.kind == "game_summary")
    assert "1号(甲)" in summary and "先知" in summary and "村民阵营" in summary
    assert "2号(乙)" in summary and "狼人" in summary and "狼人阵营" in summary


def test_sync_cards_numbers_the_living_and_marks_the_dead() -> None:
    room = GameRoom(
        session_id="20001",
        rules=ruleset_official(),
        phase=GamePhase.DAY,
        day=1,
        players=[
            Player("10001", "沉潜", 1, role=Role.SEER),
            Player("10002", "乙", 2, role=Role.WOLF, alive=False),
        ],
    )
    app = GameApplication(InMemoryRoomStore([room]))
    platform = FakePlatform()
    app.bind_platform(platform)

    asyncio.run(app._sync_cards(room))
    assert platform.cards == [
        ("20001", "10001", "1号"),
        ("20001", "10002", "2号（已出局）"),
    ]

    # 幂等：状态没变就不再重复发请求。
    asyncio.run(app._sync_cards(room))
    assert len(platform.cards) == 2

    # 终局清空名片，把展示权还给玩家。
    room.phase = GamePhase.FINISHED
    asyncio.run(app._sync_cards(room))
    assert platform.cards[-2:] == [("20001", "10001", ""), ("20001", "10002", "")]


def test_sync_cards_skipped_when_bot_is_not_group_admin() -> None:
    room = GameRoom(
        session_id="20001",
        rules=ruleset_official(),
        phase=GamePhase.DAY,
        day=1,
        players=[Player("10001", "沉潜", 1, role=Role.SEER)],
    )
    app = GameApplication(InMemoryRoomStore([room]))
    platform = FakePlatform(admin=False)
    app.bind_platform(platform)
    asyncio.run(app._sync_cards(room))
    assert platform.cards == []


def test_dead_player_actions_are_rejected_but_queries_still_work() -> None:
    room = GameRoom(
        session_id="20001",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("10001", "甲", 1, role=Role.SEER),
            Player("10002", "乙", 2, role=Role.WOLF, alive=False),
        ],
    )
    event = normalize_group_message(_group_payload("查验 1", user="10002"))
    with pytest.raises(GameRuleError, match="已经出局"):
        GameApplication._reject_if_dead(room, event, parse_command("查验 1"))
    # 查询类指令照常放行。
    GameApplication._reject_if_dead(room, event, parse_command("/status"))
    # 活着的玩家不受影响。
    alive_event = normalize_group_message(_group_payload("查验 2", user="10001"))
    GameApplication._reject_if_dead(room, alive_event, parse_command("查验 2"))


def test_process_deliveries_marks_permanent_failure_as_dead() -> None:
    record = _record(target_id="USEROPENID1")

    class Store:
        def __init__(self):
            self.dead = None

        async def pending_deliveries(self, *_args, **_kwargs):
            return [record]

        async def claim_delivery(self, _delivery_id):
            return record

        async def mark_delivery_dead(self, delivery_id, error):
            self.dead = (delivery_id, error)

        async def mark_delivery_failure(self, *_args, **_kwargs):
            raise AssertionError("永久失败不应该走重试路径")

        async def purge_finished_deliveries(self, *_args, **_kwargs):
            return 0

    class Sender:
        async def send_delivery(self, _record):
            raise PermanentSendError("目标标识不是 QQ 号：USEROPENID1")

    store = Store()
    processed = asyncio.run(GameApplication(store).process_deliveries(Sender(), 5))
    assert processed == 1
    assert store.dead is not None and store.dead[0] == record.delivery_id


# ---------------- 配置 ----------------


def _clear_napcat_env(monkeypatch) -> None:
    for name in ("NAPCAT_WS_URL", "NAPCAT_ACCESS_TOKEN", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_settings_reject_missing_endpoint_and_sqlite(monkeypatch) -> None:
    _clear_napcat_env(monkeypatch)
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("NAPCAT_WS_URL", "http://127.0.0.1:3001")
    monkeypatch.setenv("DATABASE_URL", "postgresql://werewolf:werewolf@127.0.0.1:5432/werewolf")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp.db")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_settings_accept_napcat_websocket_and_postgres(monkeypatch) -> None:
    monkeypatch.setenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001?access_token=secret-token")
    monkeypatch.setenv("NAPCAT_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://werewolf:werewolf@127.0.0.1:5432/werewolf")
    settings = Settings.from_env()
    assert settings.napcat_ws_url.startswith("ws://")
    assert settings.database_url.startswith("postgresql://")
    # 日志端点不能泄露 token。
    assert "secret-token" not in settings.napcat_endpoint_masked


def test_settings_passes_group_rule_switches_to_new_rooms(monkeypatch) -> None:
    monkeypatch.setenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
    monkeypatch.setenv("DATABASE_URL", "postgresql://werewolf:werewolf@127.0.0.1:5432/werewolf")
    monkeypatch.setenv("RANDOM_LYNCH", "true")
    monkeypatch.setenv("SECRET_LYNCH", "true")
    monkeypatch.setenv("SECRET_LYNCH_SHOW_VOTES", "true")
    monkeypatch.setenv("SECRET_LYNCH_SHOW_VOTERS", "true")
    monkeypatch.setenv("THIEF_FULL", "true")
    monkeypatch.setenv("ALLOW_ARSONIST", "false")
    monkeypatch.setenv("BURNING_OVERKILL", "false")
    rules = Settings.from_env().rules
    assert rules.random_lynch and rules.secret_lynch
    assert rules.secret_lynch_show_votes and rules.secret_lynch_show_voters
    assert rules.thief_full and not rules.allow_arsonist and not rules.burning_overkill


def test_settings_passes_role_visibility_to_new_rooms(monkeypatch) -> None:
    monkeypatch.setenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
    monkeypatch.setenv("DATABASE_URL", "postgresql://werewolf:werewolf@127.0.0.1:5432/werewolf")
    monkeypatch.setenv("SHOW_ROLES_ON_DEATH", "false")
    monkeypatch.setenv("SHOW_ROLES_END", "All")
    rules = Settings.from_env().rules
    assert rules.show_roles_on_death is False
    assert rules.show_roles_end == "All"
