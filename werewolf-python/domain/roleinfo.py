"""身份索引：/aboutXxx 的 XML key 与 /rolelist 的分页顺序。

官方来源（提交 ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7）：
- `Werewolf Control/Commands/HelpCommands.cs:87-156` /rolelist：
  五段私聊分别 10/10/9/10/3 行，每行 `"/aboutXX - " + GetLocaleString("<RoleKey>")`；
- `Werewolf Control/Commands/Helpers.cs:318-343` GetAbout：
  按 key 不区分大小写查语言文件，缺失回退 English，仍缺失返回 null；
- `Werewolf Control/UpdateHandler.cs:339-362` /aboutXxx 前缀路由：
  命中则私聊发送，`GetAbout` 返回 null 时整个分支静默返回。

文案与身份名一律不在此硬编码，统一经 `domain.locale` 从官方 `Languages/*.xml`
按玩家所选语言取用，与官方运行期查表行为一致。
"""

from __future__ import annotations

from domain.locale import get_about as _lookup_about
from domain.locale import get_locale_string
from domain.models import Role


# (about 触发词即 XML key, 对应身份)
# 顺序逐条对应 HelpCommands.cs:92-155，不可调整。
_ROLE_INFO_ROWS: tuple[tuple[str, Role], ...] = (
    # 第 1 段（HelpCommands.cs:96-105）
    ('aboutVG', Role.VILLAGER),
    ('aboutWW', Role.WOLF),
    ('aboutDrunk', Role.DRUNK),
    ('aboutSeer', Role.SEER),
    ('aboutCursed', Role.CURSED),
    ('aboutHarlot', Role.HARLOT),
    ('aboutBH', Role.BEHOLDER),
    ('aboutGunner', Role.GUNNER),
    ('aboutTraitor', Role.TRAITOR),
    ('aboutGA', Role.GUARDIAN_ANGEL),
    # 第 2 段（HelpCommands.cs:110-119）
    ('aboutDetective', Role.DETECTIVE),
    ('aboutAppS', Role.APPRENTICE_SEER),
    ('aboutCult', Role.CULTIST),
    ('aboutCH', Role.CULTIST_HUNTER),
    ('aboutWC', Role.WILD_CHILD),
    ('aboutFool', Role.FOOL),
    ('aboutMason', Role.MASON),
    ('aboutDG', Role.DOPPELGANGER),
    ('aboutCupid', Role.CUPID),
    ('aboutHunter', Role.HUNTER),
    # 第 3 段（HelpCommands.cs:124-132）
    ('aboutSK', Role.SERIAL_KILLER),
    ('aboutTanner', Role.TANNER),
    ('aboutMayor', Role.MAYOR),
    ('aboutPrince', Role.PRINCE),
    ('aboutSorcerer', Role.SORCERER),
    ('aboutClumsy', Role.CLUMSY_GUY),
    ('aboutBlacksmith', Role.BLACKSMITH),
    ('aboutAlphaWolf', Role.ALPHA_WOLF),
    ('aboutWolfCub', Role.WOLF_CUB),
    # 第 4 段（HelpCommands.cs:137-146）
    ('aboutSandman', Role.SANDMAN),
    ('aboutOracle', Role.ORACLE),
    ('aboutWolfMan', Role.WOLF_MAN),
    ('aboutLycan', Role.LYCAN),
    ('aboutPacifist', Role.PACIFIST),
    ('aboutWiseElder', Role.WISE_ELDER),
    ('aboutThief', Role.THIEF),
    ('aboutTroublemaker', Role.TROUBLEMAKER),
    ('aboutChemist', Role.CHEMIST),
    ('aboutSnowWolf', Role.SNOW_WOLF),
    # 第 5 段（HelpCommands.cs:151-155）
    ('aboutGraveDigger', Role.GRAVE_DIGGER),
    ('aboutArsonist', Role.ARSONIST),
    ('aboutAugur', Role.AUGUR),
)


ROLE_ABOUT_TRIGGERS: tuple[str, ...] = tuple(row[0] for row in _ROLE_INFO_ROWS)

# about 触发词 -> Role（保留官方大小写用于展示）。
ABOUT_TRIGGER_ROLE: dict[str, Role] = {row[0]: row[1] for row in _ROLE_INFO_ROWS}


def role_display_name(role: Role, language: str | None = None) -> str:
    """官方 `GetLocaleString(role.ToString())`；查不到时回退枚举名。"""

    return get_locale_string(role.value, language) or role.value


def get_about(key: str, language: str | None = None) -> str | None:
    """`Werewolf Control/Commands/Helpers.cs:318-343` GetAbout，缺失返回 None。"""

    return _lookup_about(key, language)


# /rolelist 的五段私聊内容与顺序，逐条对应 HelpCommands.cs:92-155。
ROLE_LIST_PAGES: tuple[tuple[str, ...], ...] = (
    ROLE_ABOUT_TRIGGERS[0:10],
    ROLE_ABOUT_TRIGGERS[10:20],
    ROLE_ABOUT_TRIGGERS[20:29],
    ROLE_ABOUT_TRIGGERS[29:39],
    ROLE_ABOUT_TRIGGERS[39:42],
)


def role_list_pages(language: str | None = None) -> list[str]:
    """官方每段形如 "/aboutXX - <身份名>\\n" 逐行拼接。"""

    pages: list[str] = []
    for page in ROLE_LIST_PAGES:
        pages.append(
            "".join(
                f"/{trigger} - {role_display_name(ABOUT_TRIGGER_ROLE[trigger], language)}\n"
                for trigger in page
            )
        )
    return pages
