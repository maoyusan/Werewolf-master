from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from adapters.qq.adapter import QQBotAdapter, _GROUP_PANEL_ITEMS
from adapters.qq.events import (
    normalize_c2c_message,
    normalize_channel_message,
    normalize_direct_message,
    normalize_group_message,
)
from application.commands import parse_command
from application.contracts import (
    NeedsAnchorSendError,
    PlatformEvent,
    PlatformSession,
    SessionType,
)
from application.service import GameApplication
from domain.engine import GameRoomEngine, STALE_TIMEOUT_SECONDS
from domain.models import DomainEvent, GamePhase, GameRoom, NightAction, Player, Role
from domain.rules import ruleset_official
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

    async def get_direct_session(self, _user_id: str) -> None:
        return None

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


def test_parse_core_and_role_commands() -> None:
    assert parse_command("加入").name == "join"
    assert parse_command("/投票 3").name == "vote"
    assert parse_command("查验 2").name == "seer"
    assert parse_command("弃票").name == "abstain"
    assert parse_command("/abstain").name == "abstain"
    assert parse_command("未知指令").name == "unknown"


def test_group_panel_names_parse_to_known_commands() -> None:
    assert 1 <= len(_GROUP_PANEL_ITEMS) <= 20
    for item in _GROUP_PANEL_ITEMS:
        parsed = parse_command(item["name"])
        assert parsed.name != "unknown", item["name"]


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
        session_id="group-1",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("u1", "丘比特", 1, role=Role.CUPID),
            Player("u2", "甲", 2, role=Role.VILLAGER),
            Player("u3", "乙", 3, role=Role.VILLAGER),
        ],
    )
    event = PlatformEvent(
        event_id="step-1",
        session=PlatformSession(SessionType.C2C, "u1"),
        user_id="u1",
        display_name="丘比特",
        text="/cupid",
    )
    prompt = app._begin_pending_action(room, event, "cupid")
    assert "第 1/1 个目标" in prompt[0].text

    import asyncio

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
            Player("u1", "丘比特", 1, role=Role.CUPID),
            Player("u2", "甲", 2, role=Role.VILLAGER),
            Player("u3", "乙", 3, role=Role.VILLAGER),
        ],
        stage_deadline=deadline,
    )
    room.night_actions["u1"] = NightAction(
        actor_id="u1", action="恋人", target_id="u2", day=3, second_target_id="u3"
    )
    room.statistics["pending_actions"] = {
        "u1": {"action": "cupid", "targets": ["2"], "required": 2, "phase": "night", "day": 3}
    }

    restored = GameRoom.from_snapshot(room.snapshot())

    assert restored.stage_deadline == deadline
    assert restored.night_actions["u1"].second_target_id == "u3"
    assert restored.statistics["pending_actions"] == room.statistics["pending_actions"]


def test_service_restart_processes_overdue_room_and_skips_finished_room() -> None:
    overdue = GameRoom(
        session_id="restart-lobby",
        rules=ruleset_official(),
        phase=GamePhase.LOBBY,
        host_user_id="u1",
        stage_deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        players=[Player(f"u{seat}", f"玩家{seat}", seat) for seat in range(1, 6)],
    )
    finished = GameRoom(
        session_id="finished-room",
        rules=ruleset_official(),
        phase=GamePhase.FINISHED,
        stage_deadline=None,
    )
    store = InMemoryRoomStore([overdue, finished])

    import asyncio

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
        host_user_id="u1",
        stage_deadline=datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SECONDS + 10),
        players=[Player("u1", "甲", 1), Player("u2", "乙", 2)],
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
        host_user_id="u1",
        stage_deadline=datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SECONDS + 5),
        players=[Player("u1", "甲", 1, role=Role.VILLAGER), Player("u2", "乙", 2, role=Role.WOLF)],
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

    import asyncio

    asyncio.run(first_store.save_group_rule_config("group-a", {"vote_seconds": 45}, "admin"))

    restarted_store = PostgreSQLStore("postgresql://test")
    restarted_store._pool = pool  # type: ignore[assignment]
    assert asyncio.run(restarted_store.get_group_rule_config("group-a")) == {"vote_seconds": 45}
    assert asyncio.run(restarted_store.get_group_rule_config("group-b")) == {}


def test_private_day_ability_requires_explicit_confirmation() -> None:
    app = GameApplication(object())
    room = GameRoom(
        session_id="group-1",
        rules=ruleset_official(),
        phase=GamePhase.DAY,
        day=2,
        players=[Player("u1", "市长", 1, role=Role.MAYOR)],
    )
    event = PlatformEvent(
        event_id="step-day-1",
        session=PlatformSession(SessionType.C2C, "u1"),
        user_id="u1",
        display_name="市长",
        text="/mayor",
    )
    prompt = app._begin_pending_action(room, event, "mayor")
    assert "发送“确认”使用" in prompt[0].text


def test_private_mayor_reveal_can_begin_during_vote() -> None:
    app = GameApplication(object())
    room = GameRoom(
        session_id="group-1",
        rules=ruleset_official(),
        phase=GamePhase.VOTE,
        day=1,
        players=[Player("u1", "市长", 1, role=Role.MAYOR)],
    )
    event = PlatformEvent(
        event_id="step-mayor-vote-1",
        session=PlatformSession(SessionType.C2C, "u1"),
        user_id="u1",
        display_name="市长",
        text="/mayor",
    )

    prompt = app._begin_pending_action(room, event, "mayor")
    assert "发送“确认”使用" in prompt[0].text


def test_normalize_group_and_c2c() -> None:
    group = normalize_group_message({
        "id": "e1",
        "group_openid": "g1",
        "content": "加入",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "author": {"member_openid": "u1", "username": "甲"},
    })
    assert group.session.session_type == SessionType.GROUP
    assert group.user_id == "u1"
    c2c = normalize_c2c_message({
        "id": "e2",
        "content": "查验 1",
        "author": {"user_openid": "u1", "username": "甲"},
    })
    assert c2c.session.session_type == SessionType.C2C
    channel = normalize_channel_message({
        "id": "e3",
        "channel_id": "c1",
        "content": "状态",
        "author": {"id": "u1", "username": "甲"},
    })
    assert channel.session.session_type == SessionType.CHANNEL


def test_normalize_official_direct_message() -> None:
    direct = normalize_direct_message({
        "id": "dm-message-1",
        "event_id": "dm-event-1",
        "guild_id": "dm-session-1",
        "content": "/身份",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "author": {"id": "u1", "username": "甲", "bot": False},
    })
    assert direct.session.session_type == SessionType.DIRECT
    assert direct.session.session_id == "dm-session-1"
    assert direct.user_id == "u1"
    assert direct.event_id == "dm-event-1"
    assert direct.reply_message_id == "dm-message-1"
    assert direct.text == "/身份"


def test_direct_delivery_uses_official_post_dms() -> None:
    class FakeApi:
        def __init__(self):
            self.calls = []

        async def post_dms(self, **kwargs):
            self.calls.append(kwargs)

    class FakeClient:
        def __init__(self, api):
            self.api = api

    api = FakeApi()
    adapter = QQBotAdapter("12345", "secret-16-chars", object())
    adapter._client = FakeClient(api)
    record = DeliveryRecord(
        delivery_id="delivery-1",
        target_type=SessionType.DIRECT,
        target_id="dm-session-1",
        text="你的身份是【预言家】。",
        room_id="group-1",
        reply_to="dm-message-1",
        source_event_id="dm-event-1",
        event_id="dm-event-1",
        state_version=1,
        status="pending",
        attempts=0,
        last_error=None,
        next_attempt_at=None,
    )

    import asyncio

    asyncio.run(adapter.send_delivery(record))
    assert api.calls == [{
        "guild_id": "dm-session-1",
        "content": "你的身份是【预言家】。",
        "msg_id": "dm-message-1",
        "event_id": "dm-event-1",
    }]


def test_creates_official_group_command_panel_once() -> None:
    class FakeRoute:
        def __init__(self, method, path):
            self.method = method
            self.path = path

    class FakeHttp:
        def __init__(self):
            self.calls = []

        async def request(self, route, **kwargs):
            self.calls.append((route.method, route.path, kwargs))
            if route.method == "GET":
                return {"records": []}
            return {"panel_id": "panel-1"}

    class FakeClient:
        def __init__(self):
            self.api = type("Api", (), {"_http": FakeHttp()})()

    adapter = QQBotAdapter("12345", "secret-16-chars", object())
    adapter._client = FakeClient()
    adapter._route_factory = FakeRoute
    import asyncio

    asyncio.run(adapter.ensure_group_command_panel())
    calls = adapter.client.api._http.calls
    assert calls[0][0:2] == ("GET", "/v2/panels?scope=group&limit=50")
    assert calls[1][0:2] == ("POST", "/v2/panels")
    assert calls[1][2]["json"]["scope"] == "group"
    assert calls[1][2]["json"]["panel"]["items"][0]["name"] == "/startgame"
    items = calls[1][2]["json"]["panel"]["items"]
    names = [item["name"] for item in items]
    for required in ("/link", "/bindqq", "/flee", "/extend"):
        assert required in names
    assert "/players" not in names
    assert len(items) <= 20
    for item in items:
        assert item["type"] == "command"
        assert item["only_admin"] is False
        assert item["desc"].strip()
        assert any("\u4e00" <= ch <= "\u9fff" for ch in item["desc"])


def test_settings_reject_sqlite_and_missing_secrets(monkeypatch) -> None:
    monkeypatch.delenv("APP_ID", raising=False)
    monkeypatch.delenv("APP_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        Settings.from_env()
        raised = False
    except ValueError:
        raised = True
    assert raised
    monkeypatch.setenv("APP_ID", "12345")
    monkeypatch.setenv("APP_SECRET", "x" * 16)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp.db")
    try:
        Settings.from_env()
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_settings_accept_postgres(monkeypatch) -> None:
    monkeypatch.setenv("APP_ID", "12345")
    monkeypatch.setenv("APP_SECRET", "official-secret-16")
    monkeypatch.setenv("DATABASE_URL", "postgresql://werewolf:werewolf@127.0.0.1:5432/werewolf")
    settings = Settings.from_env()
    assert settings.app_id_masked != settings.app_id
    assert "official-secret" not in settings.app_id_masked
    assert settings.database_url.startswith("postgresql://")
    now = datetime.now(timezone.utc)
    assert now.tzinfo is not None


def test_settings_passes_group_rule_switches_to_new_rooms(monkeypatch) -> None:
    monkeypatch.setenv("APP_ID", "12345")
    monkeypatch.setenv("APP_SECRET", "official-secret-16")
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
    monkeypatch.setenv("APP_ID", "12345")
    monkeypatch.setenv("APP_SECRET", "official-secret-16")
    monkeypatch.setenv("DATABASE_URL", "postgresql://werewolf:werewolf@127.0.0.1:5432/werewolf")
    monkeypatch.setenv("SHOW_ROLES_ON_DEATH", "false")
    monkeypatch.setenv("SHOW_ROLES_END", "All")
    rules = Settings.from_env().rules
    assert rules.show_roles_on_death is False
    assert rules.show_roles_end == "All"


def _c2c_record(**overrides):
    data = dict(
        delivery_id="delivery-c2c-1",
        target_type=SessionType.C2C,
        target_id="USEROPENID1",
        text="你的身份是【预言家】。",
        room_id="group-1",
        reply_to=None,
        source_event_id="group-event-1",
        event_id=None,
        state_version=1,
        status="pending",
        attempts=0,
        last_error=None,
        next_attempt_at=None,
    )
    data.update(overrides)
    return DeliveryRecord(**data)


def _http_adapter(request_impl):
    class FakeRoute:
        def __init__(self, method, path, **parameters):
            self.method = method
            self.path = path
            self.parameters = parameters

    class FakeHttp:
        def __init__(self):
            self.calls = []

        async def request(self, route, **kwargs):
            self.calls.append((route.method, route.path, route.parameters, kwargs))
            return await request_impl(route, **kwargs)

    class FakeClient:
        def __init__(self):
            self.api = type("Api", (), {"_http": FakeHttp()})()

    adapter = QQBotAdapter("12345", "secret-16-chars", object())
    adapter._client = FakeClient()
    adapter._route_factory = FakeRoute
    return adapter


def test_c2c_active_payload_omits_null_msg_id() -> None:
    async def ok(_route, **_kwargs):
        return {}

    adapter = _http_adapter(ok)
    asyncio.run(adapter.send_delivery(_c2c_record()))
    method, path, params, kwargs = adapter.client.api._http.calls[0]
    assert method == "POST"
    assert path == "/v2/users/{openid}/messages"
    assert params["openid"] == "USEROPENID1"
    payload = kwargs["json"]
    assert payload["content"].startswith("你的身份是")
    assert "msg_id" not in payload
    assert "msg_seq" not in payload
    assert "event_id" not in payload


def test_c2c_passive_payload_keeps_msg_id_and_seq() -> None:
    async def ok(_route, **_kwargs):
        return {}

    adapter = _http_adapter(ok)
    asyncio.run(
        adapter.send_delivery(_c2c_record(reply_to="c2c-msg-1", event_id="c2c-evt-1", msg_seq=2))
    )
    payload = adapter.client.api._http.calls[0][3]["json"]
    assert payload["msg_id"] == "c2c-msg-1"
    assert payload["event_id"] == "c2c-evt-1"
    assert payload["msg_seq"] == 2


def test_c2c_no_friend_raises_needs_anchor() -> None:
    async def boom(_route, **_kwargs):
        raise RuntimeError("消息发送失败, 无好友关系")

    adapter = _http_adapter(boom)
    with pytest.raises(NeedsAnchorSendError, match="无好友关系"):
        asyncio.run(adapter.send_delivery(_c2c_record()))


def test_private_message_from_group_does_not_reuse_group_msg_id() -> None:
    app = GameApplication(InMemoryRoomStore([]))
    source = normalize_group_message({
        "id": "group-msg-1",
        "event_id": "group-evt-1",
        "group_openid": "g1",
        "content": "/go",
        "timestamp": "2026-09-02T00:00:00+00:00",
        "author": {"member_openid": "u1", "username": "甲"},
    })
    message = asyncio.run(app._private_message("u2", "你的身份是【预言家】。", source))
    assert message.target.session_type == SessionType.C2C
    assert message.target.session_id == "u2"
    assert message.reply_to is None
    assert message.event_id is None


def test_private_message_reuses_recent_c2c_anchor() -> None:
    class Store(InMemoryRoomStore):
        async def get_user_reply_anchor(self, user_id):
            return {
                "session_type": "c2c",
                "session_id": user_id,
                "msg_id": "c2c-msg-9",
                "event_id": "c2c-evt-9",
                "uses": 0,
                "updated_at": datetime.now(timezone.utc),
            }

        async def note_user_reply_anchor_use(self, _user_id):
            self.noted = True

    store = Store([])
    app = GameApplication(store)
    source = normalize_group_message({
        "id": "group-msg-1",
        "event_id": "group-evt-1",
        "group_openid": "g1",
        "content": "/go",
        "timestamp": "2026-09-02T00:00:00+00:00",
        "author": {"member_openid": "u1", "username": "甲"},
    })
    message = asyncio.run(app._private_message("u2", "你的身份是【预言家】。", source))
    assert message.target.session_type == SessionType.C2C
    assert message.reply_to == "c2c-msg-9"
    assert message.event_id == "c2c-evt-9"
    assert store.noted is True


def test_start_from_group_hints_when_private_anchor_missing() -> None:
    room = GameRoom(
        session_id="g1",
        rules=ruleset_official(),
        phase=GamePhase.NIGHT,
        day=1,
        players=[
            Player("u1", "甲", 1, role=Role.SEER),
            Player("u2", "乙", 2, role=Role.WOLF),
        ],
    )
    app = GameApplication(InMemoryRoomStore([room]))
    source = normalize_group_message({
        "id": "group-msg-1",
        "event_id": "group-evt-1",
        "group_openid": "g1",
        "content": "/go",
        "timestamp": "2026-09-02T00:00:00+00:00",
        "author": {"member_openid": "u1", "username": "甲"},
    })
    events = [
        DomainEvent("game_started", "游戏开始。", public=True),
        DomainEvent("identity", "你的身份是【预言家】。", public=False, target_user_id="u1"),
    ]
    messages = asyncio.run(app._events_to_messages(events, room, source))
    private = [item for item in messages if item.target.session_type == SessionType.C2C]
    public = [item for item in messages if item.target.session_type == SessionType.GROUP]
    assert private and all(item.reply_to is None for item in private)
    assert any("无好友关系" in item.text for item in public)


def test_process_deliveries_parks_no_friend_as_waiting() -> None:
    record = _c2c_record()

    class Store:
        def __init__(self):
            self.parked = None

        async def pending_deliveries(self, *args, **kwargs):
            return [record]

        async def claim_delivery(self, _delivery_id):
            return record

        async def park_delivery_waiting(self, delivery_id, error):
            self.parked = (delivery_id, error)

        async def expire_waiting_deliveries(self, *_args, **_kwargs):
            return 0

        async def purge_finished_deliveries(self, *_args, **_kwargs):
            return 0

    class Sender:
        async def send_delivery(self, _record):
            raise NeedsAnchorSendError("消息发送失败, 无好友关系")

    store = Store()
    processed = asyncio.run(GameApplication(store).process_deliveries(Sender(), 5))
    assert processed == 1
    assert store.parked[0] == record.delivery_id
    assert "无好友关系" in store.parked[1]
