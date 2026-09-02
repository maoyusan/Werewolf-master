"""官方多语言子系统的等价移植（`Languages/*.xml`）。

官方来源（提交 ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7）：

- `Werewolf Node/Helpers/LangFile.cs`
  语言文件元数据：Name/Base/Variant/FileName/FilePath/Doc/LatestUpdate；
  Control 版另有 IsDefault(`isDefault="true"`) 与 LangCode(`code`)。
- `Werewolf Node/Helpers/SpecialStrings.cs`
  10 条硬编码字符串，优先级高于 XML 查表。
- `Werewolf Node/Werewolf.cs:284-345` LoadLanguage
  随机变体：同 Base 的文件里若存在非 nsfw 文件则先过滤掉 nsfw，再随机取一个；
  Fallback：同 Base、文件名不同且 IsDefault 的那个文件。
- `Werewolf Node/Werewolf.cs:346-422` GetSpecialString / GetLocaleString
  SpecialStrings → Locale → Fallback → English；key **区分大小写**；
  多个 `<value>` 随机取一个；命中 `/join` 抛异常强制回落 English；
  空白值重新从 English 取；最后 `String.Format` 再把字面 `\\n` 换成换行。
- `Werewolf Control/Handlers/UpdateHandler.cs:2071-2107` SelectLanguage
  `base` 模式下按 `OrderBy(!IsDefault).ThenBy(Variant)` 排列变体，多于一个时给菜单。
- `Werewolf Control/Handlers/UpdateHandler.cs:2109-2140` Control 版 GetLocaleString
  无 SpecialStrings、无 Fallback，异常时返回 ""。
- `Werewolf Control/Commands/Helpers.cs:318-343` GetAbout
  key **不区分大小写**，语言文件缺失回退 English，仍缺失返回 null（调用方静默）。

平台适配说明（唯一与官方不同之处）：官方 `FormatHTML()` 会把 `& < > "` 转义成
HTML 实体，因为 Telegram 用 `ParseMode.Html` 发送、渲染时再还原；QQ 机器人只发
纯文本、不做 HTML 解析，因此「转义 + 渲染还原」这一对操作在 QQ 上是恒等的，
移植后直接省略，保证玩家看到的字面内容与官方渲染结果完全一致。
"""

from __future__ import annotations

import logging
import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger(__name__)

# `Werewolf Control/Bot.cs` LanguageDirectory —— 官方把 Languages 目录整个部署到运行目录。
LANGUAGE_DIRECTORY = Path(__file__).resolve().parent.parent / "languages"

# 官方 `Program.English` / `Bot.English` 固定回退文件。
ENGLISH = "English"

# QQ 版只提供简体中文：官方新玩家默认 "English"（GeneralCommands.cs:124），
# 本移植面向中文 QQ 群，强制使用简体中文普通版（`code="zh-CN"`）。
# `get_locale_string` / `get_about` 会忽略调用方传入的 language 参数，
# 保证任何历史数据、任何调用路径都不会把英文文案发给玩家。
DEFAULT_LANGUAGE = "SimplifiedChinese"

# 是否锁定为单一语言。项目当前不提供语言切换，后续需要多语言时改为 False 即可恢复。
LANGUAGE_LOCKED = True

# `<language>` 节点在 212 个官方文件里最深出现在第 9677 字节（Cantonese - CU.xml），
# 读取头部 64 KB 足够覆盖，避免为了拿元数据而全量解析 23 MB 语料。
_HEAD_BYTES = 65536

# `Werewolf Node/Helpers/SpecialStrings.cs` —— 官方硬编码 10 条，优先于 XML 查表。
# 本移植只提供中文，因此这里直接给出对应的简体中文文案（语义与官方一致）。
SPECIAL_STRINGS: dict[str, str] = {
    "Detonation": "{0} 的南瓜炸了！{1} 正好挡在爆炸路线上，被南瓜碎片贯穿，{1} 已经死亡。{2}",
    "DetonatedWiseElder": "{0} 的南瓜在最糟糕的时刻炸了！无所不知的 {1} 死了！",
    "AskDetonate": "南瓜越长越大，随时可能爆炸！慌乱之下，你要跑向谁？",
    "AboutSpumpkin": "南瓜农 \U0001f383 以种出饱满的大南瓜闻名，这些南瓜能长到足以爆炸的程度！引爆时，南瓜农和他选中的另一名玩家会一起被炸死。南瓜农与村民阵营同胜。",
    "RoleInfoSpumpkin": "你是南瓜农！村里人都知道你能种出最大、最熟、最好吃的南瓜，你走到哪儿都抱着自己的宝贝南瓜。其中一个南瓜长得太大了，随时可能爆炸！慌乱之中，你可以在它炸开的一刻跑向某个人，让你们两个一起送命。",
    "Spumpkin": "南瓜农 \U0001f383",
    "SpumpkinFailDetonate": "你抱着那颗大南瓜冲向了 {0}，然后……看来这颗南瓜还不够大，没能炸开。回家继续种地吧！",
    "BlackDeathWinner": "恭喜 {0}，你独自在 {1} 之下活了下来！愚人节快乐！",
    "BlackDeathKilledAll": "全村人都被 {0} 杀死了，只剩满地尸体！愚人节快乐！",
    "BlackDeathLovers": "恭喜 {0} 和 {1}，你们爱得如此炽烈，甚至一起在 {2} 之下活了下来！愚人节快乐！",
}


@dataclass(frozen=True)
class LangFile:
    """`Werewolf Node/Helpers/LangFile.cs` + Control 版的 IsDefault/LangCode。"""

    name: str
    base: str
    variant: str
    file_name: str
    file_path: Path
    is_default: bool = False
    lang_code: str = ""
    latest_update: datetime | None = None

    @property
    def display(self) -> str:
        """`UpdateHandler.cs:1497` —— `slang.Base + (变体为空 ? "" : ": " + slang.Variant)`。"""

        if not self.variant.strip():
            return self.base
        return f"{self.base}: {self.variant}"


def _read_language_node(path: Path) -> dict[str, str] | None:
    """只读文件头部，取出 `<language .../>` 的属性。

    对应 `LangFile` 构造函数里的 `Doc.Descendants("language").First()`；
    这里用增量解析避免把 23 MB 语料全部读进内存。
    """

    parser = ET.XMLPullParser(("start",))
    try:
        with path.open("rb") as handle:
            remaining = _HEAD_BYTES
            while remaining > 0:
                chunk = handle.read(min(8192, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                parser.feed(chunk)
                for _, element in parser.read_events():
                    if element.tag == "language":
                        return dict(element.attrib)
    except (OSError, ET.ParseError):
        return None
    return None


def _parse_string_table(path: Path) -> dict[str, tuple[str, ...]]:
    """把 `<string key="X"><value>…</value></string>` 解析成 key -> 所有 value。

    - 官方用 `FirstOrDefault`，重复 key 只取文档里第一个，故用 setdefault；
    - `<value>` 取值等价于 XElement.Value：拼接所有后代文本、忽略注释与子标签，
      因此这里用 `"".join(element.itertext())`。
    """

    table: dict[str, tuple[str, ...]] = {}
    try:
        for _, element in ET.iterparse(str(path), events=("end",)):
            if element.tag != "string":
                continue
            key = element.get("key")
            if key is not None:
                table.setdefault(
                    key,
                    tuple("".join(value.itertext()) for value in element.iter("value")),
                )
            element.clear()
    except (OSError, ET.ParseError):
        return table
    return table


class LanguageCatalog:
    """语言文件目录：元数据即时扫描，字符串表按文件懒加载并缓存。"""

    def __init__(self, directory: Path | str = LANGUAGE_DIRECTORY) -> None:
        self.directory = Path(directory)
        self._files: dict[str, LangFile] | None = None
        self._tables: dict[str, dict[str, tuple[str, ...]]] = {}
        self._lower_tables: dict[str, dict[str, tuple[str, ...]]] = {}

    # -- 元数据 ---------------------------------------------------------
    @property
    def files(self) -> dict[str, LangFile]:
        if self._files is None:
            self._files = self._scan()
        return self._files

    def _scan(self) -> dict[str, LangFile]:
        result: dict[str, LangFile] = {}
        if not self.directory.is_dir():
            return result
        for path in sorted(self.directory.glob("*.xml")):
            attributes = _read_language_node(path)
            if attributes is None:
                continue
            file_name = path.stem
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                stamp = None
            result[file_name] = LangFile(
                name=attributes.get("name", file_name),
                base=attributes.get("base", file_name),
                variant=attributes.get("variant", ""),
                file_name=file_name,
                file_path=path,
                is_default=attributes.get("isDefault", "").lower() == "true",
                lang_code=attributes.get("code", ""),
                latest_update=stamp,
            )
        return result

    def get(self, file_name: str) -> LangFile | None:
        return self.files.get(file_name)

    def reload(self) -> None:
        self._files = None
        self._tables.clear()
        self._lower_tables.clear()

    # -- 字符串表 -------------------------------------------------------
    def table(self, file_name: str) -> dict[str, tuple[str, ...]]:
        cached = self._tables.get(file_name)
        if cached is not None:
            return cached
        lang = self.get(file_name)
        table = _parse_string_table(lang.file_path) if lang is not None else {}
        self._tables[file_name] = table
        return table

    def lower_table(self, file_name: str) -> dict[str, tuple[str, ...]]:
        """GetAbout 用的小写 key 索引（官方按 `ToLower()` 比较，取文档里第一个匹配）。"""

        cached = self._lower_tables.get(file_name)
        if cached is not None:
            return cached
        lowered: dict[str, tuple[str, ...]] = {}
        for key, values in self.table(file_name).items():
            lowered.setdefault(key.lower(), values)
        self._lower_tables[file_name] = lowered
        return lowered

    # -- 语言选择 -------------------------------------------------------
    def bases(self) -> list[str]:
        """`GeneralCommands.cs:161` —— `langs.Select(x => x.Base).Distinct().OrderBy(x => x)`。"""

        return sorted({lang.base for lang in self.files.values()})

    def variants(self, base: str) -> list[LangFile]:
        """`UpdateHandler.cs:2078` —— `OrderBy(!IsDefault).ThenBy(Variant)`。"""

        return sorted(
            (lang for lang in self.files.values() if lang.base == base),
            key=lambda lang: (not lang.is_default, lang.variant),
        )

    def select(self, base: str, variant: str | None = None) -> LangFile | None:
        """复刻 `SelectLanguage`：变体为空时，只有唯一变体才直接返回，否则返回 None。"""

        if variant is None or variant == "null":
            candidates = self.variants(base)
            if len(candidates) == 1:
                return candidates[0]
            return None
        return next(
            (lang for lang in self.files.values() if lang.base == base and lang.variant == variant),
            None,
        )

    def random_variant(self, language: str) -> LangFile | None:
        """`Werewolf.cs:288-296` —— RandomLangVariant 每局重新挑同 Base 的一个文件。"""

        current = self.get(language)
        if current is None:
            return None
        base_files = [lang for lang in self.files.values() if lang.base == current.base]
        if not base_files:
            return None
        without_nsfw = [lang for lang in base_files if "nsfw" not in lang.file_name.lower()]
        if without_nsfw:
            base_files = without_nsfw
        return base_files[random.randrange(len(base_files))]

    def default_for_base(self, base: str) -> LangFile | None:
        """`UpdateHandler.cs:1465-1470` —— RandomLangVariant 落库时用的基准文件。"""

        base_files = self.variants(base)
        if not base_files:
            return None
        for lang in base_files:
            if lang.is_default or lang.variant.lower() in {"normal", "standard", "default"}:
                return lang
        return base_files[0]

    def fallback_for(self, language: str) -> LangFile | None:
        """`Werewolf.cs:322-325` —— 同 Base、文件名不同且 IsDefault 的那个文件。"""

        current = self.get(language)
        if current is None:
            return None
        return next(
            (
                lang
                for lang in self.files.values()
                if lang.base == current.base and lang.file_name != language and lang.is_default
            ),
            None,
        )

    def search(self, pattern: str) -> list[LangFile]:
        """`GeneralCommands.cs:446` —— `Directory.GetFiles(dir, pattern + ".xml")`（精确文件名）。"""

        lang = self.get(pattern)
        return [lang] if lang is not None else []


CATALOG = LanguageCatalog()


# ---------------------------------------------------------------------------
# 取串
# ---------------------------------------------------------------------------


def _pick(values: tuple[str, ...]) -> str:
    """`Program.R.Next(values.Count())` —— 多个 `<value>` 随机取一个。"""

    if not values:
        raise LookupError("取词失败：候选值列表为空")
    return values[random.randrange(len(values))]


def _finalize(template: str, args: tuple[object, ...]) -> str:
    """`String.Format(selected.FormatHTML(), args).Replace("\\\\n", NewLine)`。

    HTML 转义按模块文档说明省略（QQ 不解析 HTML）；`String.Format` 与 Python 的
    `str.format` 对 `{0}`/`{{`/缺参数的行为一致，格式异常同样向上冒泡，
    由调用处回落到 English，与官方 catch 分支等价。
    """

    return template.format(*args).replace("\\n", "\n")


def _lookup(key: str, file_name: str) -> tuple[str, ...] | None:
    values = CATALOG.table(file_name).get(key)
    return values if values else None


def _lookup_ci(key: str, file_name: str) -> tuple[str, ...] | None:
    values = CATALOG.lower_table(file_name).get(key.lower())
    return values if values else None


def get_locale_string(key: str, language: str | None = None, *args: object) -> str:
    """复刻 `Werewolf Node/Werewolf.cs:359-422` GetLocaleString。

    查找顺序：SpecialStrings → 当前语言 → Fallback（同 Base 的默认文件）→ English；
    key 区分大小写；多值随机；命中 `/join` 或格式化失败一律回落 English；
    English 也取不到时返回 ""（对应 Control 版 `catch { return ""; }`，
    避免把一次取串失败升级成整局崩溃）。

    本移植锁定简体中文（LANGUAGE_LOCKED）：忽略调用方传入的 language，
    保证历史数据里残留的其他语言不会把英文文案发给玩家。English 仅作为
    中文包缺 key 时的最后兜底，命中即写 WARNING 日志，便于在观测页发现。
    """

    language = DEFAULT_LANGUAGE if LANGUAGE_LOCKED else (language or DEFAULT_LANGUAGE)
    try:
        special = SPECIAL_STRINGS.get(key)
        if special is not None:
            return _finalize(special, args)

        values = _lookup(key, language)
        if values is None:
            fallback = CATALOG.fallback_for(language)
            if fallback is not None:
                values = _lookup(key, fallback.file_name)
        if values is None:
            _LOG.warning("语言文案缺失，已回退英文包: key=%s language=%s", key, language)
            values = _lookup(key, ENGLISH)
        if values is None:
            raise LookupError(key)

        selected = _pick(values)
        # 官方注释：disable bluetexting /join!
        if "/join" in selected.lower():
            raise ValueError("文案里出现 /join，按官方做法改用英文包取词")
        if not selected.strip():
            selected = _pick(_lookup(key, ENGLISH) or ())
        return _finalize(selected, args)
    except Exception:
        english = _lookup(key, ENGLISH)
        if english:
            try:
                return _finalize(_pick(english), args)
            except Exception:
                return ""
        return ""


def get_about(key: str, language: str | None = None, *args: object) -> str | None:
    """复刻 `Werewolf Control/Commands/Helpers.cs:318-343` GetAbout。

    key 不区分大小写；当前语言缺失回退 English；仍缺失返回 None（调用方静默）；
    多值随机；空白值重新从 English 取；最后 `String.Format` 并把字面 `\\n` 换成换行。

    本移植锁定简体中文，忽略传入的 language。
    """

    language = DEFAULT_LANGUAGE if LANGUAGE_LOCKED else (language or DEFAULT_LANGUAGE)
    values = _lookup_ci(key, language) or _lookup_ci(key, ENGLISH)
    if not values:
        return None
    selected = _pick(values)
    if not selected.strip():
        english = _lookup_ci(key, ENGLISH)
        if english:
            selected = _pick(english)
    arguments = args or (key,)
    try:
        return _finalize(selected, arguments)
    except Exception:
        return selected.replace("\\n", "\n")


def get_lang_file(file_name: str) -> LangFile | None:
    return CATALOG.get(file_name)


def language_bases() -> list[str]:
    return CATALOG.bases()


def language_variants(base: str) -> list[LangFile]:
    return CATALOG.variants(base)


def select_language(base: str, variant: str | None = None) -> LangFile | None:
    return CATALOG.select(base, variant)


def resolve_language(value: str) -> LangFile | None:
    """QQ 版没有 InlineKeyboard，把官方三级菜单折叠成一次文本输入。

    依次尝试：文件名 → `Base: Variant` → 语言 name → 唯一变体的 Base → 唯一匹配的 Variant，
    全部忽略大小写与首尾空白；命中多个 Base 变体时返回 None，由调用方按官方
    `SelectLanguage` 的行为改发变体菜单。
    """

    text = (value or "").strip()
    if not text:
        return None
    files = CATALOG.files
    if text in files:
        return files[text]
    lowered = text.casefold()
    for lang in files.values():
        if lang.file_name.casefold() == lowered:
            return lang
    for separator in ("：", ":"):
        if separator in text:
            base, _, variant = text.partition(separator)
            found = CATALOG.select(base.strip(), variant.strip())
            if found is not None:
                return found
    for lang in files.values():
        if lang.name.casefold() == lowered:
            return lang
    for base in CATALOG.bases():
        if base.casefold() == lowered:
            return CATALOG.select(base)
    matches = [lang for lang in files.values() if lang.variant.casefold() == lowered]
    if len(matches) == 1:
        return matches[0]
    return None


# ---------------------------------------------------------------------------
# `/validatelangs`：`Werewolf Control/Helpers/LanguageHelper.cs:64-175` +
# `GetFileErrors`（602-666）。官方用 InlineKeyboard 选 Base，QQ 版改成
# `/validatelangs [Base]`，校验规则与报告字段逐条对应。
# ---------------------------------------------------------------------------

# LanguageHelper.cs:663 —— 语言文件里不允许出现邀请链接或 @用户名（官方白名单除外）。
_AD_PATTERN = re.compile(
    r"((https?://)?t(elegram)?\.me/(\+|joinchat/)([a-zA-Z0-9_\-]+))"
    r"|( @(?!werewolfbot)(?!werewolfbetabot)(?!greywolfdev)(?!werewolfsupport)"
    r"(?!greywolfsupport)(?!para949)\w)",
    re.IGNORECASE,
)

# LanguageHelper.cs:606-613 —— 官方只对这两个 key 做重复检查。
_DUPLICATE_KEYS = ("CultConvertSerialKiller", "CupidChosen")

# LanguageHelper.cs:690 ErrorLevel。
DUPLICATED_STRING = "DuplicatedString"
MISSING_STRING = "MissingString"
ERROR = "Error"
FATAL_ERROR = "FatalError"
JOIN_LINK = "JoinLink"
ADS = "Ads"


@dataclass(frozen=True)
class LanguageError:
    """`LanguageHelper.cs:672-688` LanguageError。"""

    file: str
    key: str
    message: str
    level: str = MISSING_STRING


def _raw_entries(path: Path) -> list[tuple[str, list[str], dict[str, str]]]:
    """按文档顺序返回 `(key, values, attributes)`，保留重复 key 供查重使用。"""

    entries: list[tuple[str, list[str], dict[str, str]]] = []
    try:
        for _, element in ET.iterparse(str(path), events=("end",)):
            if element.tag != "string":
                continue
            key = element.get("key")
            if key is not None:
                entries.append(
                    (
                        key,
                        ["".join(value.itertext()) for value in element.iter("value")],
                        dict(element.attrib),
                    )
                )
            element.clear()
    except (OSError, ET.ParseError):
        return entries
    return entries


def _file_errors(lang: LangFile, master: list[tuple[str, list[str], dict[str, str]]]) -> list[LanguageError]:
    entries = _raw_entries(lang.file_path)
    errors: list[LanguageError] = []
    counts: dict[str, int] = {}
    for key, _, _ in entries:
        counts[key] = counts.get(key, 0) + 1
    for key in _DUPLICATE_KEYS:
        if counts.get(key, 0) > 1:
            errors.append(LanguageError(lang.file_name, key, f"{key} 条目重复", DUPLICATED_STRING))
    table: dict[str, list[str]] = {}
    for key, values, _ in entries:
        table.setdefault(key, values)
    join_seen: set[str] = set()
    for key, master_values, attributes in master:
        deprecated = "deprecated" in attributes
        is_gif = "isgif" in attributes
        values = table.get(key)
        if values is None:
            if not deprecated:
                errors.append(LanguageError(lang.file_name, key, "缺少文案内容"))
            continue
        master_string = master_values[0] if master_values else ""
        # LanguageHelper.cs:634-637 —— 只看 {0}~{4}，取最大命中下标 + 1。
        variables = 0
        for index in range(5):
            if "{" + str(index) + "}" in master_string:
                variables = index + 1
        for value in values:
            for index in range(5):
                token = "{" + str(index) + "}"
                if token not in value and variables - 1 >= index:
                    errors.append(LanguageError(lang.file_name, key, f"缺少占位符 {token}", ERROR))
                elif token in value and variables - 1 < index:
                    errors.append(LanguageError(lang.file_name, key, f"多余占位符 {token}", ERROR))
            if is_gif and len(value) > 1000:
                errors.append(
                    LanguageError(
                        lang.file_name, key,
                        "GIF 文案长度不能超过 1000 个字符", FATAL_ERROR,
                    )
                )
            if "/join" in value.lower() and key not in join_seen:
                join_seen.add(key)
                errors.append(LanguageError(lang.file_name, key, "", JOIN_LINK))
            if _AD_PATTERN.search(value):
                errors.append(
                    LanguageError(
                        lang.file_name, key, "语言文件中不允许出现用户名或外部链接", ADS
                    )
                )
    return errors


def validate_language_files(base: str | None = None) -> list[LanguageError]:
    """`LanguageHelper.ValidateFiles` —— 以 English.xml 为基准逐文件校验。

    官方还会 `TestLength` 检查 Base/Variant/文件名能否塞进 Telegram 的 64 字节
    callback data；QQ 版没有 InlineKeyboard 回调，该项不适用故不检查。
    """

    master_file = CATALOG.get(ENGLISH)
    if master_file is None:
        return []
    master = _raw_entries(master_file.file_path)
    errors: list[LanguageError] = []
    for lang in sorted(CATALOG.files.values(), key=lambda item: item.file_name):
        if lang.file_name == ENGLISH:
            continue
        if base is not None and lang.base != base:
            continue
        errors.extend(_file_errors(lang, master))
    return errors


def format_validation_report(errors: list[LanguageError], base: str | None = None) -> list[str]:
    """`LanguageHelper.cs:84-118` —— 先发逐文件明细，再发一条汇总。"""

    lines: list[str] = []
    for file_name in dict.fromkeys(error.file for error in errors):
        lang = CATALOG.get(file_name)
        stamp = lang.latest_update.strftime("%m月%d日") if lang and lang.latest_update else "未知"
        own = [error for error in errors if error.file == file_name]
        lines.append(f"{file_name}.xml（最近更新：{stamp}）")
        ads = [error.key for error in own if error.level == ADS]
        if ads:
            lines.append("发现广告内容：" + "、".join(dict.fromkeys(ads)))
            continue
        duplicated = [error.key for error in own if error.level == DUPLICATED_STRING]
        if duplicated:
            lines.append("重复条目：" + "、".join(dict.fromkeys(duplicated)))
        joins = [error.key for error in own if error.level == JOIN_LINK]
        if joins:
            lines.append("含 join 指令的条目：" + "、".join(dict.fromkeys(joins)))
        lines.append(
            f"缺失文案数量：{sum(1 for error in own if error.level == MISSING_STRING)}"
        )
        for error in own:
            if error.level == ERROR:
                lines.append(f"错误 - {error.key}\n{error.message}")
        lines.append("")
    detail = "\n".join(lines).strip() or "所有语言文件均未发现问题。"
    files = [
        lang for lang in CATALOG.files.values() if base is None or lang.base == base
    ]
    files.sort(key=lambda item: item.latest_update or datetime.min.replace(tzinfo=timezone.utc))
    summary = (
        "校验完成\n"
        f"错误数量：{sum(1 for error in errors if error.level == ERROR)}\n"
        f"缺失文案数量：{sum(1 for error in errors if error.level == MISSING_STRING)}"
    )
    if files:
        newest, oldest = files[-1], files[0]
        summary += (
            f"\n最近更新的文件：{newest.file_name}.xml"
            f"（{newest.latest_update.strftime('%m月%d日') if newest.latest_update else '未知'}）"
            f"\n最久未更新的文件：{oldest.file_name}.xml"
            f"（{oldest.latest_update.strftime('%m月%d日') if oldest.latest_update else '未知'}）"
        )
    return [detail, summary]
