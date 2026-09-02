# 官方玩法对照清单

## 结论

基于官方快照 `work/upstream-official`，commit `ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7`，Python 核心玩法已完成代码级对照。验收标准是阶段、角色、阵营、存活、恋人、死因、胜负和事件数据，不比较翻译后的文字样式。

## 验收命令

```powershell
cd werewolf-python
python -m pytest tests/test_official_parity.py -q
python -m pytest tests/test_official_parity.py tests/test_parity.py tests/test_adapter_and_commands.py -q
python -m pytest -q
```

本次记录结果：官方对照 `200 passed`；定向验收 `238 passed`；全量 `586 passed`；三个命令退出状态均为 `0`。

## 已闭环的规则

| 范围 | 代码位置 | 通过测试 |
| --- | --- | --- |
| 角色目录、阵营、角色池、强度和禁用 | `domain/models.py`、`domain/rules.py` | `test_official_role_catalog_emoji_team_disable_strength`、`test_official_assignment_invariants`、`test_every_role_can_be_dealt_in_a_set_roles_fixture` |
| 狼人投吃和狼人多数，含雪狼边界 | `domain/models.py:WOLF_ROLES`、`MAJORITY_WOLF_ROLES` | `test_official_wolf_roles_exclude_snow_wolf`、`test_snow_wolf_counts_for_majority`、`test_snow_wolf_cannot_cast_eat_vote` |
| 夜晚顺序、访问、狼人、守护、预言、猎人、转换 | `domain/engine.py:671`、`1040`、`1238`、`1363` | `test_guardian_*`、`test_seer_*`、`test_hunter_*`、`test_cult_*`、`test_conversion_*` |
| 连环杀手：访问死亡、80% 绊倒、墓地后 50% 改杀 | `domain/engine.py:1363`、`1389`、`1392` | `test_wolf_visiting_away_serial_killer_uses_pinned_80_percent_rule`、`test_snow_wolf_visiting_away_serial_killer_uses_same_rule`、`test_serial_killer_stumble_reroll_is_fixed_at_50_percent` |
| 掘墓人：按正常死亡计墓，排除 `FLEE`/`IDLE`，人数公式 | `domain/engine.py:1987` | `test_grave_digger_ignores_flee_and_idle_deaths`、`test_grave_digger_default_fall_formula_uses_grave_count`、`test_grave_digger_falls_and_is_spotted_when_chances_pin` |
| RandomLynch、秘密投票和市长票权 | `domain/engine.py:2324`、`2372`、`2394` | `test_random_lynch_resolves_tied_candidates`、`test_secret_lynch_records_votes_and_mayor_weight`、`test_secret_lynch_hides_result_when_show_votes_is_off` |
| Augur 顺序和可能身份 | `domain/engine.py:1040` | `test_augur_shuffles_persistent_possible_roles_in_place`、`test_augur_sees_absent_possible_role` |
| 盗贼、化学家、巫师、分身及二人/三人残局 | `domain/engine.py:1238`、`1649`、`2478` | `test_thief_*`、`test_chemist_*`、`test_no_one_*`、`test_doppelganger_*`、`test_win_*_two_player` |
| 恋人连锁、野孩子、学徒、头狼和雪狼转化 | `domain/engine.py:1238`、`2033` | `test_lovers_death_chain`、`test_bitten_wild_child_and_doppelganger_do_not_transform_after_lynch`、`test_alpha_bite_*` |
| 白天能力、闲置、投票、死因和胜负 | `domain/engine.py:2152`、`2295`、`2478` | `test_vote_*`、`test_idle_two_nonvotes_kills_without_hunter_or_lover`、`test_win_*` |

## 概率边界

官方 `Settings.cs` 中的转化概率表已逐角色核对；测试 `test_every_official_cult_conversion_role_honors_zero_and_hundred` 固定检查每个角色在 `0%` 和 `100%` 时分别不转化和一定转化，并检查角色、存活和事件结果。头狼、猎人、妓女、掘墓人、Augur、纵火等独立概率也有固定边界测试。

## 官方设置的特殊说明

`SerialKillerConversionChance = 20` 出现在官方设置表中，但官方夜间 `ConvertToCult` 调用没有传入它，实际使用的是 `0` 或默认 `100`。Python 保持官方实际流程，不额外启用这段未调用的设置；这属于“官方死代码已记录”，不是遗漏。

## 平台差异，不计入核心玩法

- Telegram 的按钮、GIF、语言包和成就统计，QQ 以文字命令和文字消息对应。
- QQ 的命令面板依赖 QQ 平台接口；代码已检查创建 `/startgame` 等入口，但菜单是否显示仍由用户的 QQ 平台配置决定。
- QQ Gateway、私信投递、数据库和 Docker 连接属于部署验收，不改变官方规则；真实 QQ 群流程由用户自行测试。

## SendGif 随附文案

官方 `SendGif(GetLocaleString(key), gif, chatId)` 由两部分组成：Telegram 的 GIF `file_id` 和一段文案。
GIF 素材是 Telegram 专有资源，QQ 端不适用；但文案是发给**遇害者本人**的私聊死亡告知，属于游戏流程消息，
必须保留。端口用 `domain/engine.py` 的 `_victim_notice()` 生成 `victim_death_notice` 私聊事件，
经 `_events_to_messages` 投递到遇害者的 C2C/私信会话：

| 官方位置 | 文案键 | 触发时机 |
| --- | --- | --- |
| `Werewolf.cs:3203` | `Burn` | 纵火者引燃，逐个通知被烧死者 |
| `Werewolf.cs:3312`、`3365`、`3434` | `WolvesEatYou` | 狼人吃人（酒鬼 / 猎人反杀后 / 默认三个分支） |
| `Werewolf.cs:3481` | `WolvesSpottedYou` | 狼群发现并杀死掘墓人 |
| `Werewolf.cs:3533` | `SerialKillerKilledYou` | 连环杀手得手 |
| `Werewolf.cs:3546` | `SerialKillerSpottedYou` | 连环杀手发现并杀死掘墓人 |

同一段落的凶手侧私聊（`Werewolf.cs:3479-3481` 的 `WolvesSpotted` 逐个发给投票狼、`3544-3546` 的
`SerialKillerSpotted` 发给连环杀手）以及 `Werewolf.cs:4399-4404` 按凶手身份区分的掘墓人公开播报
（`KillerSpottedDiggerPublic` / `WolvesSpottedDiggerPublic`）一并补齐，验收见 `tests/test_audit_victim_notices.py`。

胜负播报处的 `SendWithQueue(msg, GetRandomImage(...))`（`Werewolf.cs:4812-4902`）只附带装饰性图片，
文案本身已由端口的结算播报覆盖，不额外补消息。

## 管理员与开发者命令

官方三档权限（`GroupAdminOnly` / `GlobalAdminOnly` + `DevOnly` / `LangAdminOnly`）在 QQ 端合并为两档：
`ADMIN_USER_IDS` 对应群管理员，`DEV_USER_IDS` 同时覆盖 GlobalAdmin、Dev 与 LangAdmin。原因是 QQ 开放接口
没有 Telegram 的群管理员查询能力，无法在运行时判定发言人是否为该群管理员。开发者同时具备群管理员权限，
与官方 `Helpers.cs:186` 一致。

已实现（35 条）：`/smite`、`/getidles`、`/setlink`、`/remlink`、`/resetlink`、`/killgame`、`/skipvote`、
`/maintenance`、`/getroles`、`/playtime`、`/whois`、`/user`、`/getban`、`/getbans`、`/permban`、`/remban`、
`/notifyban`、`/notifyspam`、`/preferred`、`/bangroup`、`/leavegroup`、`/addach`、`/remach`、`/validatelangs`、
`/broadcast`；以及 10 条 DevOnly 调试命令 `/winchart`、`/usage`、`/checkgroups`、`/clearcount`、`/moveachv`、
`/ohaider`、`/test`、`/getcommands`、`/reloadenglish`、`/fi`，验收见 `tests/test_audit_dev_commands.py`。

`/winchart` 与 `/test` 官方输出的是 `Charting.cs` 生成的 PNG 图表并附带同样内容的文本行；QQ 端不上传图片，
只保留文本行（含官方的 `M/y` 月份格式与缺月补 0）。`/usage` 官方读 Windows 性能计数器，端口改读 `/proc/stat`
与 `/proc/meminfo`，采样次数、间隔和 `CPU Usage: x%\r\nRAM available: yMB` 的输出格式保持一致。
`/moveachv` 官方把旧位图列迁移到新成就表，端口只有新版成就数组，因此固定走
`doesn't have old achievements records that can be moved` 分支。

`/validatelangs` 复刻 `LanguageHelper.ValidateFiles` 与 `GetFileErrors` 的全部校验项（重复键、缺失 `<values>`
且尊重 `deprecated` 属性、`{0}`–`{4}` 占位符与 `English.xml` 的比对、gif 字符串超长、`/join` 出现、广告正则），
只省略 `TestLength` 的 Telegram 回调数据 64 字节限制——QQ 没有 inline callback data，该项无对应概念。

## 官方存在但 QQ 端不适用的命令

以下命令依赖 Telegram 或官方自建基础设施，QQ 端没有对应概念，属于“平台不适用”，不是缺功能：

- 节点管理：`/stopnode`、`/killnode`、`/replacenodes`、`/startnodes`、`/build`、`/update`、`/asplode`。
  官方 Control/Node 是两个进程经 TCP 通信；本端口是单进程，没有节点集群可管理。
- GIF 与捐赠：`/dumpgifs`、`/learngif`、`/reviewgifs`、`/approvegifs`、`/disapprovegifs`、`/fixgifs`、
  `/donate`、`/donatenew`、`/customgif`、`/adddonation`。依赖 Telegram 动画消息与 Telegram Payments。
- Telegram inline 专属：`/restore`、`/preferred` 的按钮菜单、`/movelang` 的确认按钮、`/updatestatus`。
  QQ 没有 InlineKeyboard 与 callback_query，`/preferred` 已改为直接取反的文字命令。
- 宿主文件操作：`/sql`、`/getlogs`、`/clearlogs`、`/uploadlang`、`/commitlangs`。依赖官方运维机的文件系统与
  Git 仓库，属于部署侧操作，不在机器人进程内提供。
- `#if BETA` 编译开关下的 `/unlockbeta`、`/lockbeta`：官方仅在 Beta 构建中存在。
