"""独立复刻审计（任务 14/19）：辅助命令 /aboutXxx、/rolelist、/grouplist。

官方期望值来源（唯一）：提交 ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7。
- `Werewolf Control/Handlers/UpdateHandler.cs:339-362` /aboutXxx 前缀路由：
  命中私聊发送；`GetAbout` 返回空则整个分支 return（静默）；
  群里的 SentPrivate 回执被官方注释掉了（350-354）。
- `Werewolf Control/Commands/Helpers.cs:318-343` GetAbout：key 不区分大小写，
  缺失回退 English，仍缺失返回 null；随机取一个 <value>；`\\n` 转真正换行。
- `Werewolf Control/Commands/HelpCommands.cs:87-156` /rolelist：
  五条私聊（10/10/9/10/3 行），每行 `"/aboutXX - " + GetLocaleString("<RoleKey>")`，
  仅第一条之后在非私聊会话补一条 SentPrivate。
- `Werewolf Control/Commands/HelpCommands.cs:24-84` /grouplist +
  `UpdateHandler.cs:1303-1370` `groups|` 回调：私聊列表 + 群里 SentPrivate；
  最终列表取最近 21 天活跃、按 LastRefresh 再按 Ranking 倒序的前 10 个群。
- `Languages/SimplifiedChinese.xml` 身份名与 About 文案。
"""

from __future__ import annotations

import asyncio

from application.commands import parse_command
from application.contracts import SessionType
from application.service import GameApplication
from domain.models import Role
from domain.roleinfo import (
    ABOUT_TRIGGER_ROLE,
    ROLE_ABOUT_TRIGGERS,
    get_about,
    role_display_name,
    role_list_pages,
)

from tests.test_audit_qq_commands import AuditStore, c2c_event, group_event, texts


def run(coro):
    return asyncio.run(coro)


class GroupListStore(AuditStore):
    """带公开群列表的存储替身，对应官方 PublicGroups.ForLanguage。"""

    def __init__(self, rooms=(), groups=()):
        super().__init__(rooms)
        self.groups = list(groups)

    async def list_public_groups(self, limit: int = 10):
        return self.groups[:limit]


# ---------------------------------------------------------------------------
# /aboutXxx（UpdateHandler.cs:339-362 + Helpers.cs:318-343）
# ---------------------------------------------------------------------------


def test_about_prefix_routes_before_normal_command_table() -> None:
    """339 —— `args[0].StartsWith("about")` 在命令查表之前拦截，不会落到 unknown。"""
    assert parse_command("/aboutVG") == parse_command("/aboutvg")
    assert parse_command("/aboutVG").name == "about"
    assert parse_command("/aboutVG").argument == "aboutvg"
    assert parse_command("/aboutNoSuchRole").name == "about"


def test_about_key_lookup_is_case_insensitive() -> None:
    """Helpers.cs:322 —— `x.Attribute("key").Value.ToLower() == args[0].ToLower()`。"""
    assert get_about("aboutvg") == get_about("AboutVG") == get_about("ABOUTVG")
    assert get_about("aboutvg").startswith("普通村民")


def test_about_unknown_key_returns_none_and_service_stays_silent() -> None:
    """Helpers.cs:326-327 返回 null；UpdateHandler.cs:341 因此不发任何消息。"""
    assert get_about("aboutnosuchrole") is None
    app = GameApplication(AuditStore())
    assert run(app.handle_event(group_event("/aboutNoSuchRole", user="u1"))) == []


def test_about_replies_privately_without_group_notice() -> None:
    """UpdateHandler.cs:349 只 `Send(reply, From.Id)`；350-354 的 SentPrivate 被注释掉。"""
    app = GameApplication(AuditStore())
    messages = run(app.handle_event(group_event("/aboutSeer", user="u1")))
    assert len(messages) == 1
    assert messages[0].target.session_type != SessionType.GROUP
    assert messages[0].text.startswith("👳先知")


def test_about_in_private_replies_in_same_session() -> None:
    app = GameApplication(AuditStore())
    messages = run(app.handle_event(c2c_event("/aboutWW", user="u1")))
    assert len(messages) == 1
    assert messages[0].target.session_type == SessionType.C2C
    assert "狼人" in messages[0].text


def test_about_escaped_newlines_are_converted() -> None:
    """Helpers.cs:342 —— `.Replace("\\\\n", Environment.NewLine)`。"""
    text = get_about("aboutPacifist")
    assert "\\n" not in text
    assert "\n" in text


def test_about_para_easter_egg_exists() -> None:
    """`Languages/SimplifiedChinese.xml` 有 AboutPara，但它不是身份。"""
    assert get_about("aboutpara") is not None
    assert "aboutPara" not in ROLE_ABOUT_TRIGGERS


def test_every_rolelist_trigger_has_about_text() -> None:
    """/rolelist 列出的每个 /aboutXX 都必须能查到文案，否则官方会静默。"""
    for trigger in ROLE_ABOUT_TRIGGERS:
        assert get_about(trigger), trigger


# ---------------------------------------------------------------------------
# /rolelist（HelpCommands.cs:87-156）
# ---------------------------------------------------------------------------


def test_role_list_pages_match_official_split() -> None:
    """92-155 —— 五条消息分别是 10/10/9/10/3 行。"""
    pages = role_list_pages()
    assert [page.count("\n") for page in pages] == [10, 10, 9, 10, 3]


def test_role_list_first_page_matches_official_order() -> None:
    """96-105 —— 第一条固定为 VG/WW/Drunk/Seer/Cursed/Harlot/BH/Gunner/Traitor/GA。"""
    first = role_list_pages()[0].splitlines()
    assert [line.split(" - ")[0] for line in first] == [
        "/aboutVG", "/aboutWW", "/aboutDrunk", "/aboutSeer", "/aboutCursed",
        "/aboutHarlot", "/aboutBH", "/aboutGunner", "/aboutTraitor", "/aboutGA",
    ]
    assert first[0] == f"/aboutVG - {role_display_name(Role.VILLAGER)}"


def test_role_list_lines_use_localized_role_names() -> None:
    """每行是 `"/aboutXX - " + GetLocaleString("<RoleKey>", lang)`。"""
    for page, triggers in zip(role_list_pages(), (
        ROLE_ABOUT_TRIGGERS[0:10], ROLE_ABOUT_TRIGGERS[10:20], ROLE_ABOUT_TRIGGERS[20:29],
        ROLE_ABOUT_TRIGGERS[29:39], ROLE_ABOUT_TRIGGERS[39:42],
    )):
        lines = page.splitlines()
        assert len(lines) == len(triggers)
        for line, trigger in zip(lines, triggers):
            assert line == f"/{trigger} - {role_display_name(ABOUT_TRIGGER_ROLE[trigger])}"


def test_role_list_in_group_sends_five_private_and_one_notice() -> None:
    """105-107 —— 只在第一条之后回 SentPrivate，其余四条静默。"""
    app = GameApplication(AuditStore())
    messages = run(app.handle_event(group_event("/rolelist", user="u1")))
    private = [m for m in messages if m.target.session_type != SessionType.GROUP]
    public = [m for m in messages if m.target.session_type == SessionType.GROUP]
    assert len(private) == 5 and len(public) == 1
    assert "私聊" in public[0].text
    assert [m.text for m in private] == role_list_pages()


def test_role_list_in_private_sends_no_notice() -> None:
    """106 —— `if (update.Message.Chat.Type != ChatType.Private)` 才发回执。"""
    app = GameApplication(AuditStore())
    messages = run(app.handle_event(c2c_event("/rolelist", user="u1")))
    assert len(messages) == 5
    assert all(m.target.session_type == SessionType.C2C for m in messages)


# ---------------------------------------------------------------------------
# /grouplist（HelpCommands.cs:24-84 + UpdateHandler.cs:1303-1370）
# ---------------------------------------------------------------------------


def test_group_list_sends_private_and_group_notice() -> None:
    """68-71 —— 私聊发菜单/列表，群里回 SentPrivate。"""
    store = GroupListStore(groups=[{"group_id": "g9", "name": "狼人杀一群", "games": 12}])
    app = GameApplication(store)
    messages = run(app.handle_event(group_event("/grouplist", user="u1")))
    private = [m for m in messages if m.target.session_type != SessionType.GROUP]
    public = [m for m in messages if m.target.session_type == SessionType.GROUP]
    assert len(private) == 1 and len(public) == 1
    assert "狼人杀一群" in private[0].text
    assert "私聊" in public[0].text


def test_group_list_renders_here_is_list_header() -> None:
    """UpdateHandler.cs:1364 —— `GetLocaleString("HereIsList", lang, choice)` + 群名列表。"""
    store = GroupListStore(groups=[
        {"group_id": "g9", "name": "甲群", "games": 30},
        {"group_id": "g8", "name": "乙群", "games": 2},
    ])
    app = GameApplication(store)
    text = texts(run(app.handle_event(c2c_event("/grouplist", user="u1"))))
    # HereIsList 的参数是语言的 Base（UpdateHandler.cs:1364 的 `choice`），
    # 简体中文四个变体（普通/原版/失忆模式/NSFW）的 Base 统一是「简体中文」。
    assert text.startswith("语言为 简体中文 的群组有:")
    assert text.index("甲群") < text.index("乙群")


def test_group_list_without_active_groups() -> None:
    """1361 —— 21 天过滤后可能为空，此时官方只发标题；QQ 版给出明确提示。"""
    app = GameApplication(GroupListStore())
    text = texts(run(app.handle_event(c2c_event("/grouplist", user="u1"))))
    assert "还没有活跃" in text


def test_group_list_falls_back_when_store_has_no_public_groups() -> None:
    """存储替身未实现 list_public_groups 时不得抛错（getattr 防御）。"""
    app = GameApplication(AuditStore())
    messages = run(app.handle_event(c2c_event("/grouplist", user="u1")))
    assert len(messages) == 1
