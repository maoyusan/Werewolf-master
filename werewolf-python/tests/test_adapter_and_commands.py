from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from adapters.qq.adapter import QQBotAdapter
from adapters.qq.events import (
    normalize_c2c_message,
    normalize_channel_message,
    normalize_direct_message,
    normalize_group_message,
)
from application.commands import parse_command
from application.contracts import PlatformEvent, PlatformSession, SessionType
from application.service import GameApplication
from domain.models import GamePhase, GameRoom, NightAction, Player, Role
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
    assert parse_command("未知指令").name == "unknown"


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
