"""独立复刻审计：官方 DevOnly 调试命令。

官方期望值来源（唯一）：
`work/upstream-official/Werewolf for Telegram`。
- `/winchart`：`Werewolf Control/Commands/DevCommands.cs:162-166` + `Helpers/Charting.cs:21-207`
- `/test`：`Werewolf Control/Commands/DevCommands.cs:499-555`
- `/usage`：`Werewolf Control/Commands/DevCommands.cs:562-586`
- `/checkgroups`：`Werewolf Control/Commands/DevCommands.cs:588-625`
- `/reloadenglish`：`Werewolf Control/Commands/DevCommands.cs:705-710`
- `/clearcount`：`Werewolf Control/Commands/DevCommands.cs:712-716`
- `/getcommands`：`Werewolf Control/Commands/DevCommands.cs:741-747`
- `/moveachv`：`Werewolf Control/Commands/DevCommands.cs:114-151`
- `/ohaider`：`Werewolf Control/Commands/DevCommands.cs:1330-1376`
  + `Handlers/UpdateHandler.cs:1069-1124`
- `/fi`：`Werewolf Control/Commands/AdminCommands.cs:671-684` + `Program.cs:251-295`
"""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from adapters.napcat.events import normalize_group_message, normalize_private_message
from application.contracts import PlatformEvent
from application.service import GameApplication
from domain.achievements import Achievement
from domain.models import GamePhase, GameRoom, Player
from domain.rules import ruleset_official


_ids = itertools.count(1)


def run(coro):
    return asyncio.run(coro)


class DevStore:
    """只实现调试命令用到的读写接口。"""

    def __init__(self):
        self.event_cache: dict[str, list] = {}
        self.rooms: dict[str, GameRoom] = {}
        self.players: dict[str, dict] = {}
        self.achievements: dict[str, set[int]] = {}
        self.groups: list[dict] = []
        self.win_rows: list[dict] = []
        self.month_rows: list[dict] = []
        self.coplayers: list[str] = []
        self.win_chart_calls: list[tuple] = []
        self.merged: list[tuple[str, list[int]]] = []

    async def begin_event(self, event_id, _session_key):
        if event_id in self.event_cache:
            return False, self.event_cache[event_id]
        self.event_cache[event_id] = []
        return True, None

    async def commit_result(self, *, room, event_id, messages, **_kwargs):
        self.event_cache[event_id] = list(messages)

    async def get_room(self, session_id):
        return self.rooms.get(session_id)

    async def list_active_rooms(self):
        active = {GamePhase.LOBBY, GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}
        return [room for room in self.rooms.values() if room.phase in active]

    async def find_active_rooms_for_user(self, user_id):
        return []

    async def get_group_rule_config(self, _group_id):
        return {}

    async def get_global_ban(self, _user_id):
        return None

    async def get_player(self, user_id):
        return self.players.get(user_id)

    async def touch_player(self, user_id, name=None):
        self.players.setdefault(user_id, {"user_id": user_id, "name": name})

    async def get_player_achievements(self, user_id):
        return sorted(self.achievements.get(user_id, set()))

    async def merge_player_achievements(self, user_id, values):
        self.merged.append((user_id, list(values)))
        self.achievements.setdefault(user_id, set()).update(values)

    # --- 调试命令新增的查询 -------------------------------------------
    async def win_chart_stats(self, start, mode=None):
        self.win_chart_calls.append((start, mode))
        return list(self.win_rows)

    async def monthly_player_counts(self, _start):
        return list(self.month_rows)

    async def list_linked_groups(self):
        return list(self.groups)

    async def coplayers_missing_achievement(self, _user_id, _value):
        return list(self.coplayers)


def make_app(devs=("dev",)):
    store = DevStore()
    return GameApplication(store, admin_user_ids=("adm",), dev_user_ids=devs), store


def group_event(content, *, user="dev", group="g1") -> PlatformEvent:
    return normalize_group_message({
        "message_id": f"evt-{next(_ids)}",
        "group_id": group,
        "user_id": user,
        "raw_message": content,
        "message": [{"type": "text", "data": {"text": content}}],
        "time": int(datetime.now(timezone.utc).timestamp()),
        "sender": {"user_id": user, "nickname": f"玩家{user}", "role": "member"},
    })


def c2c_event(content, *, user="dev") -> PlatformEvent:
    return normalize_private_message({
        "message_id": f"evt-{next(_ids)}",
        "user_id": user,
        "raw_message": content,
        "message": [{"type": "text", "data": {"text": content}}],
        "time": int(datetime.now(timezone.utc).timestamp()),
        "sender": {"user_id": user, "nickname": f"玩家{user}"},
    })


def texts(messages) -> list[str]:
    return [message.text for message in messages]


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------

DEV_TRIGGERS = (
    "winchart", "usage", "checkgroups", "clearcount", "moveachv",
    "ohaider", "test", "getcommands", "reloadenglish", "fi",
)


@pytest.mark.parametrize("trigger", DEV_TRIGGERS)
def test_dev_debug_command_is_wired(trigger):
    """十条调试命令必须同时出现在命令表、权限表并且有实现。"""
    from application.commands import _ALIASES, parse_command
    from application.service import _DEV_COMMANDS

    assert _ALIASES[trigger] == trigger
    assert parse_command(f"/{trigger}").name == trigger
    assert trigger in _DEV_COMMANDS
    assert hasattr(GameApplication, f"_admin_{trigger}")


@pytest.mark.parametrize("trigger", DEV_TRIGGERS)
def test_dev_debug_command_rejects_non_dev(trigger):
    """DevOnly：普通群管理员一律拒绝（Attributes/CommandAttribute.cs）。"""
    app, _ = make_app()
    messages = run(app.handle_event(group_event(f"/{trigger}", user="adm")))
    assert "只有开发者可以使用该命令" in "\n".join(texts(messages))


# ---------------------------------------------------------------------------
# /winchart —— Charting.cs:21-72、206
# ---------------------------------------------------------------------------

def test_winchart_defaults_to_2016_05_15_and_prints_player_game_counts():
    app, store = make_app()
    store.win_rows = [{"players": 5, "games": 12}, {"players": 7, "games": 3}]
    messages = run(app.handle_event(group_event("/winchart")))
    start, mode = store.win_chart_calls[0]
    assert (start.year, start.month, start.day) == (2016, 5, 15)
    assert mode is None
    assert texts(messages) == ["\n5: 12\n7: 3"]


@pytest.mark.parametrize(
    "argument,delta",
    [("2 weeks", timedelta(days=14)), ("3 days", timedelta(days=3)), ("6 hours", timedelta(hours=6))],
)
def test_winchart_parses_intervals(argument, delta):
    app, store = make_app()
    store.win_rows = [{"players": 5, "games": 1}]
    run(app.handle_event(group_event(f"/winchart {argument}")))
    start, _mode = store.win_chart_calls[0]
    assert abs((datetime.now(timezone.utc) - delta) - start) < timedelta(seconds=5)


def test_winchart_rejects_unknown_interval_but_still_reports():
    """官方 default 分支只提示一句，不 return，仍按默认起点输出。"""
    app, store = make_app()
    store.win_rows = [{"players": 5, "games": 1}]
    messages = run(app.handle_event(group_event("/winchart 2 fortnights")))
    assert texts(messages) == ["时间区间只支持：hour(s) 小时、day(s) 天、week(s) 周。", "\n5: 1"]
    start, _mode = store.win_chart_calls[0]
    assert (start.year, start.month, start.day) == (2016, 5, 15)


def test_winchart_single_argument_is_a_mode_filter():
    app, store = make_app()
    run(app.handle_event(group_event("/winchart Chaos")))
    assert store.win_chart_calls[0][1] == "Chaos"


def test_winchart_third_argument_is_a_mode_filter():
    app, store = make_app()
    run(app.handle_event(group_event("/winchart 2 weeks Chaos")))
    assert store.win_chart_calls[0][1] == "Chaos"


def test_winchart_without_data_sends_nothing():
    """官方 Aggregate 出空串，Telegram 无法发送空消息，端口直接不发。"""
    app, _ = make_app()
    assert run(app.handle_event(group_event("/winchart"))) == []


# ---------------------------------------------------------------------------
# /test —— DevCommands.cs:499-555
# ---------------------------------------------------------------------------

def test_test_command_reports_usage_per_month():
    app, store = make_app()
    store.month_rows = [
        {"month": datetime(2016, 4, 1, tzinfo=timezone.utc), "players": 42},
        {"month": datetime(2016, 6, 1, tzinfo=timezone.utc), "players": 7},
    ]
    messages = run(app.handle_event(group_event("/test")))
    assert messages[0].text == "请稍候，正在生成统计……"
    lines = messages[1].text.split("\n")
    assert lines[0] == "逐月使用量统计"
    # 官方 `month.ToString("M/y")`：月份不补零、年份取后两位；缺数据的月份补 0。
    assert lines[1] == "4/16: 42"
    assert lines[2] == "5/16: 0"
    assert lines[3] == "6/16: 7"


def test_test_command_only_replies_once_per_session():
    """同一条事件拆出的多条回复共享同一个 source_event_id，靠批内序号区分。

    序号只用来分配 delivery_id，保证每段都是独立的投递记录；NapCat 可以
    主动发消息，不需要任何回复锚点。
    """
    app, store = make_app()
    store.month_rows = []
    messages = run(app.handle_event(group_event("/test")))
    assert all(message.event_id == messages[0].event_id for message in messages)
    assert [message.sequence for message in messages] == list(range(len(messages)))


# ---------------------------------------------------------------------------
# /usage —— DevCommands.cs:562-586
# ---------------------------------------------------------------------------

def test_usage_reports_cpu_and_ram(monkeypatch):
    monkeypatch.setattr("application.service._USAGE_INTERVAL", 0)
    ticks = itertools.count()
    # 每次采样 total 增加 100、idle 增加 25 —— 占用率恒为 75%。
    def fake_cpu_times():
        tick = next(ticks)
        return 100 * tick, 25 * tick

    monkeypatch.setattr(GameApplication, "_cpu_times", staticmethod(fake_cpu_times))
    monkeypatch.setattr(GameApplication, "_available_memory_mb", staticmethod(lambda: 2048))
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/usage")))
    assert messages[0].text == "请稍候，正在采集数据……"
    assert messages[1].text == "CPU 占用：75%\r\n可用内存：2048MB"


def test_usage_survives_missing_proc(monkeypatch):
    monkeypatch.setattr("application.service._USAGE_INTERVAL", 0)
    monkeypatch.setattr(GameApplication, "_cpu_times", staticmethod(lambda: None))
    monkeypatch.setattr(GameApplication, "_available_memory_mb", staticmethod(lambda: 0))
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/usage")))
    assert messages[1].text == "CPU 占用：0%\r\n可用内存：0MB"


# ---------------------------------------------------------------------------
# /checkgroups —— DevCommands.cs:588-625
# ---------------------------------------------------------------------------

def test_checkgroups_matches_official_and_wolf_spellings():
    app, store = make_app()
    store.groups = [
        {"group_id": "g1", "name": "Official Werewolf CN", "group_link": "l1"},
        {"group_id": "g2", "name": "Oficial Lupus Club", "group_link": "l2"},
        {"group_id": "g3", "name": "Just Werewolf", "group_link": "l3"},
        {"group_id": "g4", "name": "Official Chess", "group_link": "l4"},
    ]
    messages = run(app.handle_event(group_event("/checkgroups")))
    assert messages[0].text == "请稍候，正在检索……"
    assert messages[1].text == (
        "检测到群名同时包含「官方」与「狼」各种拼写变体的群：\n"
        "\nOfficial Werewolf CN - g1 - l1"
        "\nOficial Lupus Club - g2 - l2"
    )


def test_checkgroups_without_matches():
    app, store = make_app()
    store.groups = [{"group_id": "g3", "name": "Just Werewolf", "group_link": "l3"}]
    messages = run(app.handle_event(group_event("/checkgroups")))
    assert messages[1].text == "没有发现群名同时包含「官方」与「狼」变体的群。"


# ---------------------------------------------------------------------------
# /clearcount + /getcommands —— DevCommands.cs:712-747
# ---------------------------------------------------------------------------

def test_getcommands_lists_recent_commands_of_a_user():
    app, _ = make_app()
    run(app.handle_event(group_event("/ping", user="u9")))
    run(app.handle_event(group_event("/status", user="u9")))
    messages = run(app.handle_event(group_event("/getcommands u9")))
    assert texts(messages) == ["\n/ping\n/status"]


def test_clearcount_is_silent_and_empties_the_log():
    app, _ = make_app()
    run(app.handle_event(group_event("/ping", user="u9")))
    assert run(app.handle_event(group_event("/clearcount"))) == []
    messages = run(app.handle_event(group_event("/getcommands u9")))
    assert "没有该用户最近一分钟内的命令记录" in messages[0].text


def test_message_log_only_keeps_one_minute():
    """UpdateHandler.SpamDetection()：超过 1 分钟的记录会被清掉。"""
    app, _ = make_app()
    run(app.handle_event(group_event("/ping", user="u9")))
    stale = datetime.now(timezone.utc) - timedelta(minutes=2)
    app._user_messages["u9"] = [(stale, "/ping")]
    run(app.handle_event(group_event("/status", user="u9")))
    assert [text for _time, text in app._user_messages["u9"]] == ["/status"]


# ---------------------------------------------------------------------------
# /reloadenglish —— DevCommands.cs:705-710
# ---------------------------------------------------------------------------

def test_reloadenglish_is_silent_and_reloads_catalog():
    from domain.locale import CATALOG, get_locale_string

    app, _ = make_app()
    get_locale_string("Villager")
    assert CATALOG._files
    assert run(app.handle_event(group_event("/reloadenglish"))) == []
    assert not CATALOG._files
    # 重新载入后仍能正常取词条。
    assert get_locale_string("Villager")


# ---------------------------------------------------------------------------
# /moveachv —— DevCommands.cs:114-151
# ---------------------------------------------------------------------------

def test_moveachv_requires_a_user_id():
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/moveachv")))
    assert texts(messages) == ["命令格式：/moveachv <QQ号>"]


def test_moveachv_reports_unknown_player():
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/moveachv u404")))
    # NapCat 下 user_id 就是真实 QQ 号，管理命令直接回显它。
    assert texts(messages) == ["数据库里找不到 QQ 号 u404 对应的玩家。"]


def test_moveachv_reports_no_legacy_records():
    """端口只有新版成就数组，官方的旧位图列不存在，因此固定走这一分支。"""
    app, store = make_app()
    store.players["u1"] = {"user_id": "u1", "name": "甲"}
    messages = run(app.handle_event(group_event("/moveachv u1")))
    assert texts(messages) == ["玩家 QQ 号：u1｜昵称：甲 没有可迁移的旧成就记录。"]


# ---------------------------------------------------------------------------
# /ohaider —— DevCommands.cs:1330-1376 + UpdateHandler.cs:1069-1124
# ---------------------------------------------------------------------------

def test_ohaider_requires_private_chat():
    app, _ = make_app()
    messages = run(app.handle_event(group_event("/ohaider u1")))
    assert texts(messages) == ["这条命令只能私聊机器人使用。"]


def test_ohaider_requires_an_id():
    app, _ = make_app()
    messages = run(app.handle_event(c2c_event("/ohaider")))
    assert texts(messages) == ["QQ号无效。"]


def test_ohaider_reports_unknown_player():
    app, _ = make_app()
    messages = run(app.handle_event(c2c_event("/ohaider u404")))
    assert texts(messages) == ["找不到该QQ号对应的玩家档案。"]


def test_ohaider_awards_every_coplayer_without_the_achievement():
    app, store = make_app()
    store.players["u1"] = {"user_id": "u1", "name": "甲"}
    store.coplayers = ["u2", "u3"]
    messages = run(app.handle_event(c2c_event("/ohaider u1")))
    assert texts(messages) == [
        "发现 2 名新达成 OHAIDER 条件的玩家。\n"
        "已为 2 名玩家补发成就。\n处理完成。"
    ]
    assert store.merged == [
        ("u2", [Achievement.OHAIDER.value]),
        ("u3", [Achievement.OHAIDER.value]),
    ]


def test_ohaider_reports_failures():
    app, store = make_app()
    store.players["u1"] = {"user_id": "u1", "name": "甲"}

    async def boom(_user_id, _value):
        raise RuntimeError("db down")

    store.coplayers_missing_achievement = boom
    messages = run(app.handle_event(c2c_event("/ohaider u1")))
    assert texts(messages) == ["更新 OHAIDER 成就失败：db down"]


# ---------------------------------------------------------------------------
# /fi + /runinfo —— AdminCommands.cs:671-684、Program.cs:251-295、
# GeneralCommands.cs:68-77
# ---------------------------------------------------------------------------

def test_fi_reports_every_counter_line():
    app, store = make_app()
    store.rooms["g2"] = GameRoom(
        session_id="g2",
        rules=ruleset_official(),
        phase=GamePhase.DAY,
        players=[Player(f"u{i}", f"玩家{i}", i) for i in range(1, 6)],
    )
    text = texts(run(app.handle_event(group_event("/fi"))))[0]
    # 中文文案用全角冒号，标签侧用全角空格对齐，这里按全角冒号切分并去掉填充。
    labels = [line.split("：")[0].replace("　", "") for line in text.split("\n")]
    assert labels[:12] == [
        "运行信息", "运行时长", "节点数", "在场玩家", "进行中对局", "收到消息",
        "收到指令", "发出消息", "每秒收", "每秒发", "同时最多对局",
        "收到耗时",
    ]
    assert labels[12] == "回复耗时"
    assert "进行中对局： 1" in text
    assert "在场玩家　： 5" in text
    assert "收到指令　： 1" in text


def test_runinfo_matches_official_layout():
    app, store = make_app()
    store.rooms["g2"] = GameRoom(
        session_id="g2",
        rules=ruleset_official(),
        phase=GamePhase.LOBBY,
        players=[Player("u1", "玩家1", 1)],
    )
    text = texts(run(app.handle_event(group_event("/runinfo", user="u5"))))[0]
    lines = text.split("\n")
    assert lines[0] == "运行信息"
    assert lines[2] == "节点数：1"
    assert lines[3] == "进行中对局：1"
    assert lines[4] == "在场玩家：1"
