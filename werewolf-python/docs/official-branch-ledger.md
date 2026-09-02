# 官方玩法分支总表

## 基线

- 官方固定版本：`work/upstream-official`，提交 `ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7`
- 官方源码根目录：`D:\Project\ScriptProjects\Werewolf-master\work\upstream-official\Werewolf for Telegram`
- 检查日期：`2026-09-01`，`2026-09-02` 追加白天/处决循环、猎人最后一枪与结算播报的分支
- Python 源码根目录：`D:\Project\ScriptProjects\Werewolf-master\werewolf-python`
- 版本控制状态：当前目录及 Python 子目录均未发现 Git 元数据；本轮以 SHA256、副本和可执行回退脚本保存改动。

### Python 修改前哈希

| 文件 | SHA256 |
| --- | --- |
| `domain/models.py` | `676B4666AB6E1B541C07064BB65F4C53FB58DD1601C559809AC6BB891D6372A5` |
| `domain/rules.py` | `9B1DD055F691CDAE1308E5D34742F9AC22EE629041169E7A1E06B41136044887` |
| `domain/engine.py` | `D50BABA31482439E0B39B176EC32981E771669AF44C1F3A48F57FEC299145A3A` |
| `application/service.py` | `269EA4F2C945B093ADBB6872F5F44F2B7C26334FDBC62600FD04D0A14964BE1A` |
| `application/commands.py` | `246865FD8D941D1635AEE14FED1F747281DCE65DB4BA20B9879CB71F434A6030` |
| `adapters/qq/adapter.py` | `20D05506DF95C5F74026382E14DB03327DA3B5C9BA00B6EF56D68827C2D547B4` |
| `infrastructure/db.py` | `440F6E52CFB823FF730759E529557168013CB55198B945AF4E1455C21778B0A4` |

## 计数

| 总条目 | 已通过 | 已差异 | 已修复通过 | 未检查 | 未决 | 已确认未调用 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 94 | 69 | 0 | 25 | 0 | 0 | 0 |

总数会在每一个官方实际判断登记后更新。只有每条记录都有固定输入和实际运行结果，才可以把状态改为“已通过”。

## 实际玩法入口与纳入范围

| 官方文件 | 入口或作用 | 纳入原因 |
| --- | --- | --- |
| `Werewolf Control/Commands/GameCommands.cs:21-202` | `/startgame`、`/startchaos`、`/join`、`/forcestart`、`/players`、`/flee`、`/extend`、`/stopwaiting` | Telegram 群命令直接创建或操作对局。 |
| `Werewolf Node/Werewolf.cs:429-639` | 计时主循环、开始、首夜、夜晚、白天、投票 | 实际推进一局游戏的主路径。 |
| `Werewolf Node/Werewolf.cs:685-876` | 加入与离开 | 改变房间和玩家状态。 |
| `Werewolf Node/Werewolf.cs:1453-2030` | 发身份、初始关系、转换、继承 | 影响初始身份、阵营和后续能力。 |
| `Werewolf Node/Werewolf.cs:2323-2489` | 访问、偷窃、入教 | 决定夜晚访问、死亡和转换。 |
| `Werewolf Node/Werewolf.cs:2537-4637` | 投票、白天能力、夜晚结算、胜负 | 决定行动结果、死亡、阶段与游戏结束。 |
| `Werewolf Node/Werewolf.cs:4950-5319` | 可选行动和目标菜单 | 决定哪些玩家能在何时执行哪种动作。 |
| `Werewolf Node/Werewolf.cs:5357-5716` | 逃跑、跳过投票、猎人最后一枪、死亡与恋人死亡 | 直接改变玩家存活、死因、连锁结算。 |
| `Shared/Roles.cs:11-183` | 角色、禁用、属性 | 官方身份目录与角色设置。 |
| `Shared/GameBalancing.cs:34-285` | 发牌、人数、强度和重发 | 官方开局身份组合。 |
| `Shared/GameMode.cs:1-14` | 普通、混乱等模式 | 影响发牌与流程入口。 |
| `Werewolf Node/Models/IPlayer.cs` | 玩家状态字段 | 定义官方规则保存的状态。 |
| `Werewolf Node/Helpers/Settings.cs` | 概率、时长和默认规则 | 上述实际流程读取的固定设置。 |
| `Database/Models/Group.cs` 及直接引用的群设置模型 | 群规则开关 | 官方房间启动时读取的规则开关。 |

## 不纳入玩法总数的文件

| 范围 | 原因 |
| --- | --- |
| `Werewolf Website`、`DonationSite` | 网页与捐赠，不进入一局对局。 |
| `StatsRotation`、统计和排行更新代码 | 只记录结果，不改变已完成对局的规则。 |
| `Languages`、GIF、按钮排版、成就展示 | 是文字或展示形式；QQ 只核对同一文字指令能否进入规则流程。 |
| 数据库写入、日志、异常输出 | 只保存或显示结果；快照和恢复中会单独核对实际影响规则的字段。 |
| 注释掉的历史代码 | 从游戏入口没有调用链，不作为复刻目标。 |

## 分支记录格式

| 编号 | 官方文件:行号 | 调用来源 | 条件 | 官方结果 | Python 位置 | 测试 | 固定输入 | 实测结果 | 状态 | 差异编号 | 复查日期 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

后续每一行只记录一个会改变对局结果的最小判断；同一角色在不同阶段、不同目标或不同设置下会拆成独立编号。

## 角色目录：已实测

固定测试：`python -m pytest tests/test_official_parity.py::test_official_role_catalog_emoji_team_disable_strength tests/test_official_parity.py::test_every_role_can_be_dealt_in_a_set_roles_fixture -q`，输出 `4 passed`，退出状态 `0`。

## 身份与夜间提示：已实测

固定测试：`python -m pytest tests/test_official_parity.py -k 'official_identity_rosters_include_the_recipient or official_cupid_first_prompt_only_offers_the_first_lover or official_grave_digger_notice_keeps_player_prompt_order or official_wolf_night_prompt_names_other_awake_eating_wolves or official_cultist_night_prompt_names_other_cultists or official_night_prompt_clears_previous_choice_before_sending_menu or official_first_night_mandatory_picks_have_no_skip' -q`，修复后于 2026-09-01 重跑，输出 `10 passed`，退出状态 `0`（覆盖 D-001 至 D-007）。

## 房间流程：已实测

固定测试：`python -m pytest tests/test_official_parity.py -k 'official_lobby or official_duplicate_or_started_join or official_startgame_creator_waits_for_join or official_flee_by_non_player or official_force_start_with_too_few_players or official_insufficient_lobby_timeout_removes_room' -q`，修复前输出 `16 failed`（D-008 至 D-019，其中 D-012 有 6 组固定名称输入）。修复后于 2026-09-01 重跑，输出 `16 passed`，退出状态 `0`。

阶段计时固定测试：`python -m pytest tests/test_official_parity.py -k 'official_late_lobby_join_keeps_at_least_one_minute or official_day_time_adds_one_minute_for_a_five_player_game or official_first_night_special_role_lasts_at_least_two_minutes or official_first_night_classic_thief_lasts_at_least_two_minutes' -q`，修复前输出 `7 failed`（D-020 至 D-023；D-021 覆盖 5 人和 20 人两组时长，D-022 覆盖丘比特、分身和野孩子 3 组固定输入）。修复后于 2026-09-01 重跑，输出 `7 passed`，退出状态 `0`。

阶段提前结算固定测试：`python -m pytest tests/test_official_parity.py -k 'official_night_resolves_when_every_required_action_is_submitted or official_vote_resolves_when_every_living_player_has_voted' -q`，输出 `2 passed`，退出状态 `0`。

无可用夜间行动固定测试：`python -m pytest tests/test_official_parity.py::test_official_night_without_available_actions_ends_after_one_second -q`，修复前输出 `1 failed`（D-024）。修复后于 2026-09-01 重跑，输出 `1 passed`，退出状态 `0`。

阶段超时固定测试：`python -m pytest tests/test_official_parity.py::test_official_phase_timeout_moves_to_its_next_stage -q`，输出 `3 passed`，退出状态 `0`。

## 白天与处决循环、猎人最后一枪、结算播报：已实测

固定测试：`python -m pytest tests/test_audit_lynch_cycle.py -q`，输出 `17 passed`，退出状态 `0`（覆盖 P-012 至 P-016 与 D-025；其中 `test_pacifist_*` 五项在修复 `_begin_vote` / `resolve_vote` / `submit_day_action` 之前失败）。

固定测试：`python -m pytest tests/test_audit_hunter_final_shot.py tests/test_audit_endgame_narration.py tests/test_audit_day_prompts.py -q`，输出 `42 passed`，退出状态 `0`（覆盖 P-017 至 P-022）。

D-025 的修复内容：官方 `LynchCycle` 在发处决菜单之前（2559-2564）和倒计时内每一秒（2574-2604）两处检查 `_pacifistUsed`，一旦置位立刻 `PacifistNoLynchNow` 并 `return`。端口此前把和平的效果推迟到投票时间走完后才在 `resolve_vote` 里改写结果，导致多计了票数、多累计了 `NonVote`、多播报了秘密投票并多加了市长亮牌后的处决计数。修复后两处都直接短路到 `_after_lynch`，并写入一条 `counts` 为空的 `vote_history`，`/result` 历史结构保持不变。

| 编号 | 官方文件:行号 | 调用来源 | 条件 | 官方结果 | Python 位置 | 测试 | 固定输入 | 实测结果 | 状态 | 差异编号 | 复查日期 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| R-001 | `Shared/Roles.cs:17` | `RoleConfigHelper.GetRoles` | 读取 `Villager` | 固定值、图标、不可禁用、村庄、默认强度一致 | `domain/models.py:139-181` | `test_official_role_catalog_emoji_team_disable_strength` | `OFFICIAL_ROLE_CATALOG[Role.VILLAGER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-002 | `Shared/Roles.cs:20` | 同上 | 读取 `Drunk` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.DRUNK]` | 通过 | 已通过 | - | 2026-09-01 |
| R-003 | `Shared/Roles.cs:23` | 同上 | 读取 `Harlot` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.HARLOT]` | 通过 | 已通过 | - | 2026-09-01 |
| R-004 | `Shared/Roles.cs:26` | 同上 | 读取 `Seer` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.SEER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-005 | `Shared/Roles.cs:29` | 同上 | 读取 `Traitor` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.TRAITOR]` | 通过 | 已通过 | - | 2026-09-01 |
| R-006 | `Shared/Roles.cs:32` | 同上 | 读取 `GuardianAngel` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.GUARDIAN_ANGEL]` | 通过 | 已通过 | - | 2026-09-01 |
| R-007 | `Shared/Roles.cs:35` | 同上 | 读取 `Detective` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.DETECTIVE]` | 通过 | 已通过 | - | 2026-09-01 |
| R-008 | `Shared/Roles.cs:38` | 同上 | 读取 `Wolf` | 固定值、图标、不可禁用、狼人、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.WOLF]` | 通过 | 已通过 | - | 2026-09-01 |
| R-009 | `Shared/Roles.cs:41` | 同上 | 读取 `Cursed` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.CURSED]` | 通过 | 已通过 | - | 2026-09-01 |
| R-010 | `Shared/Roles.cs:44` | 同上 | 读取 `Gunner` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.GUNNER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-011 | `Shared/Roles.cs:47` | 同上 | 读取 `Tanner` | 固定值、图标、禁用、独立阵营、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.TANNER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-012 | `Shared/Roles.cs:50` | 同上 | 读取 `Fool` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.FOOL]` | 通过 | 已通过 | - | 2026-09-01 |
| R-013 | `Shared/Roles.cs:53` | 同上 | 读取 `WildChild` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.WILD_CHILD]` | 通过 | 已通过 | - | 2026-09-01 |
| R-014 | `Shared/Roles.cs:56` | 同上 | 读取 `Beholder` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.BEHOLDER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-015 | `Shared/Roles.cs:59` | 同上 | 读取 `ApprenticeSeer` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.APPRENTICE_SEER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-016 | `Shared/Roles.cs:62` | 同上 | 读取 `Cultist` | 固定值、图标、禁用、邪教、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.CULTIST]` | 通过 | 已通过 | - | 2026-09-01 |
| R-017 | `Shared/Roles.cs:65` | 同上 | 读取 `CultistHunter` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.CULTIST_HUNTER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-018 | `Shared/Roles.cs:68` | 同上 | 读取 `Mason` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.MASON]` | 通过 | 已通过 | - | 2026-09-01 |
| R-019 | `Shared/Roles.cs:71` | 同上 | 读取 `Doppelgänger` | 固定值、图标、禁用、盗贼阵营、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.DOPPELGANGER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-020 | `Shared/Roles.cs:74` | 同上 | 读取 `Cupid` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.CUPID]` | 通过 | 已通过 | - | 2026-09-01 |
| R-021 | `Shared/Roles.cs:77` | 同上 | 读取 `Hunter` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.HUNTER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-022 | `Shared/Roles.cs:80` | 同上 | 读取 `SerialKiller` | 固定值、图标、禁用、连环杀手、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.SERIAL_KILLER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-023 | `Shared/Roles.cs:83` | 同上 | 读取 `Sorcerer` | 固定值、图标、禁用、狼人、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.SORCERER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-024 | `Shared/Roles.cs:86` | 同上 | 读取 `AlphaWolf` | 固定值、图标、禁用、狼人、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.ALPHA_WOLF]` | 通过 | 已通过 | - | 2026-09-01 |
| R-025 | `Shared/Roles.cs:89` | 同上 | 读取 `WolfCub` | 固定值、图标、禁用、狼人、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.WOLF_CUB]` | 通过 | 已通过 | - | 2026-09-01 |
| R-026 | `Shared/Roles.cs:92` | 同上 | 读取 `Blacksmith` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.BLACKSMITH]` | 通过 | 已通过 | - | 2026-09-01 |
| R-027 | `Shared/Roles.cs:95` | 同上 | 读取 `ClumsyGuy` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.CLUMSY_GUY]` | 通过 | 已通过 | - | 2026-09-01 |
| R-028 | `Shared/Roles.cs:98` | 同上 | 读取 `Mayor` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.MAYOR]` | 通过 | 已通过 | - | 2026-09-01 |
| R-029 | `Shared/Roles.cs:101` | 同上 | 读取 `Prince` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.PRINCE]` | 通过 | 已通过 | - | 2026-09-01 |
| R-030 | `Shared/Roles.cs:104` | 同上 | 读取 `Lycan` | 固定值、图标、禁用、狼人、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.LYCAN]` | 通过 | 已通过 | - | 2026-09-01 |
| R-031 | `Shared/Roles.cs:107` | 同上 | 读取 `Pacifist` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.PACIFIST]` | 通过 | 已通过 | - | 2026-09-01 |
| R-032 | `Shared/Roles.cs:110` | 同上 | 读取 `WiseElder` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.WISE_ELDER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-033 | `Shared/Roles.cs:113` | 同上 | 读取 `Oracle` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.ORACLE]` | 通过 | 已通过 | - | 2026-09-01 |
| R-034 | `Shared/Roles.cs:116` | 同上 | 读取 `Sandman` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.SANDMAN]` | 通过 | 已通过 | - | 2026-09-01 |
| R-035 | `Shared/Roles.cs:119` | 同上 | 读取 `WolfMan` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.WOLF_MAN]` | 通过 | 已通过 | - | 2026-09-01 |
| R-036 | `Shared/Roles.cs:122` | 同上 | 读取 `Thief` | 固定值、图标、禁用、盗贼阵营、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.THIEF]` | 通过 | 已通过 | - | 2026-09-01 |
| R-037 | `Shared/Roles.cs:125` | 同上 | 读取 `Troublemaker` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.TROUBLEMAKER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-038 | `Shared/Roles.cs:128` | 同上 | 读取 `Chemist` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.CHEMIST]` | 通过 | 已通过 | - | 2026-09-01 |
| R-039 | `Shared/Roles.cs:131` | 同上 | 读取 `SnowWolf` | 固定值、图标、禁用、狼人、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.SNOW_WOLF]` | 通过 | 已通过 | - | 2026-09-01 |
| R-040 | `Shared/Roles.cs:134` | 同上 | 读取 `GraveDigger` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.GRAVE_DIGGER]` | 通过 | 已通过 | - | 2026-09-01 |
| R-041 | `Shared/Roles.cs:137` | 同上 | 读取 `Augur` | 固定值、图标、禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.AUGUR]` | 通过 | 已通过 | - | 2026-09-01 |
| R-042 | `Shared/Roles.cs:140` | 同上 | 读取 `Arsonist` | 固定值、图标、禁用、纵火者、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.ARSONIST]` | 通过 | 已通过 | - | 2026-09-01 |
| R-043 | `Shared/Roles.cs:143` | 同上 | 读取 `Spumpkin` | 固定值、图标、不可禁用、村庄、默认强度一致 | 同上 | 同上 | `OFFICIAL_ROLE_CATALOG[Role.SPUMPKIN]` | 通过 | 已通过 | - | 2026-09-01 |
| R-044 | `Shared/GameBalancing.cs:216-285` | `GameBalancing.Balance` | 牌组包含会影响强度的身份组合 | 预言家、守护、被诅咒者、鞣皮者、观察者、邪教徒、邪教捕手、石匠、狼崽按组合重新计算强度 | `domain/rules.py:35-66` | `test_official_role_strength_dynamic_conditions` | 15 组固定角色列表 | 通过 | 已通过 | - | 2026-09-01 |
| A-001 | `Shared/GameBalancing.cs:152-214` | `GameBalancing.Balance` | 5 人、11 人、禁用纵火者、首张为雪狼 | 狼人数量、孤雪狼替换、邪教门槛、石匠和村民追加、禁用过滤均一致 | `domain/rules.py:68-104` | `test_official_role_list_card_pool_thresholds_and_lone_snow_wolf` | 固定首选随机源和人数 `5`、`11` | 通过 | 已通过 | - | 2026-09-01 |
| A-002 | `Shared/GameBalancing.cs:66-93` | `GameBalancing.Balance` | 最终手牌缺少真狼、邪教捕手或预言家 | 替换为真狼、邪教捕手或预言家 | `domain/rules.py:124-143` | `test_official_balance_repairs_missing_wolf_cult_hunter_and_seer` | 三组固定洗牌顺序 | 通过 | 已通过 | - | 2026-09-01 |
| A-003 | `Shared/GameBalancing.cs:95-129` | `GameBalancing.Balance` | 普通模式的牌组强度、公开身份或阻止击杀身份超限 | 普通模式重发；混乱模式保留同一副牌 | `domain/rules.py:154-173` | `test_official_normal_balance_retries_revealed_and_blocking_role_limits` | 两组先拒绝后合法的固定洗牌顺序 | 通过 | 已通过 | - | 2026-09-01 |
| D-001 | `Werewolf Node/Werewolf.cs:1660-1665,1677-1706` | `GameTimer` → `NotifyRoles` → `GetRoleInfo` | 石匠、狼群或邪教徒人数超过一人时发送开局身份说明 | 同伴名单包含接收者本人 | `domain/engine.py:258-292` | `test_official_identity_rosters_include_the_recipient` | 2 名石匠、2 名狼、2 名邪教徒 | 修复前失败，修复后通过 | 已修复通过 | D-001 | 2026-09-01 |
| D-002 | `Werewolf Node/Werewolf.cs:1065-1086,5240-5248` | `GameTimer` → `NightCycle` → `SendNightActions` | 第 1 夜丘比特获得恋人选择 | 先只发送第一位恋人的选择；确认后才发送第二位选择 | `domain/models.py:239`、`domain/engine.py:389-403,548-566` | `test_official_cupid_first_prompt_only_offers_the_first_lover` | 丘比特与 2 名村民，第 1 夜 | 修复前失败，修复后通过 | 已修复通过 | D-002 | 2026-09-01 |
| D-003 | `Werewolf Node/Werewolf.cs:5152-5314` | `GameTimer` → `NightCycle` → `SendNightActions` | 预言家排在墓地看守之前，且没有新坟墓 | 按玩家顺序先提示预言家，再发送墓地看守的无墓穴消息 | `domain/engine.py:365-418,1987-2007` | `test_official_grave_digger_notice_keeps_player_prompt_order` | 预言家、墓地看守、村民，第 1 夜 | 修复前失败，修复后通过 | 已修复通过 | D-003 | 2026-09-01 |
| D-004 | `Werewolf Node/Werewolf.cs:5187-5201` | `GameTimer` → `NightCycle` → `SendNightActions` | 狼人收到夜晚袭击提示，另有其他狼人 | 提示点名其他未醉的吃人狼；不点名雪狼和醉狼 | `domain/engine.py:389-403` | `test_official_wolf_night_prompt_names_other_awake_eating_wolves` | 2 名狼人、1 名醉狼、1 名雪狼、1 名村民，第 1 夜 | 修复前失败，修复后通过 | 已修复通过 | D-004 | 2026-09-01 |
| D-005 | `Werewolf Node/Werewolf.cs:5206-5215` | `GameTimer` → `NightCycle` → `SendNightActions` | 邪教徒收到夜晚转化提示，另有邪教同伴 | 提示点名其他邪教徒 | `domain/engine.py:389-403` | `test_official_cultist_night_prompt_names_other_cultists` | 2 名邪教徒、1 名村民，第 1 夜 | 修复前失败，修复后通过 | 已修复通过 | D-005 | 2026-09-01 |
| D-006 | `Werewolf Node/Werewolf.cs:2976-2983,5152-5155` | `GameTimer` → `NightCycle` → `SendNightActions` | 存活玩家进入下一夜并收到菜单 | 发送菜单前清空上一夜的第一、第二选择 | `domain/engine.py:365-418` | `test_official_night_prompt_clears_previous_choice_before_sending_menu` | 第 2 夜预言家保留上一夜两个目标 | 修复前失败，修复后通过 | 已修复通过 | D-006 | 2026-09-01 |
| D-007 | `Werewolf Node/Werewolf.cs:1753-1786,4138,5303-5304` | `GameTimer` → `NightCycle` → `SendNightActions` → `ValidateSpecialRoleChoices` | 第 1 夜的丘比特、野孩子、分身和普通盗贼收到目标菜单 | 不提供“跳过”；超时后由系统随机补全恋人、模板或盗取目标 | `domain/engine.py:365-418,523-579,754-787,1649-1685` | `test_official_first_night_mandatory_picks_have_no_skip` | 四种身份各配 2 名固定目标，第 1 夜 | 修复前失败，修复后通过 | 已修复通过 | D-007 | 2026-09-01 |
| D-008 | `Werewolf Node/Werewolf.cs:685-868` | `GameCommands.StartGame` → `Werewolf.AddPlayer` / `RemovePlayer` | 首名加入者离开，或其他等待玩家仍在房间 | 所有等待玩家地位相同；没有房主字段，也不转移房主 | `domain/models.py:433-483`、`domain/engine.py:153-179,235-243` | `test_official_lobby_has_no_player_owner` | 首位、第二位依次加入，首位退出 | 修复前失败，修复后通过 | 已修复通过 | D-008 | 2026-09-01 |
| D-009 | `Werewolf Node/Werewolf.cs:815-817` | `Werewolf.AddPlayer` → `GameTimer` | 加入者达到允许人数上限 | 设置 `KillTimer`，等待循环立刻结束并进入开局流程 | `domain/engine.py:153-167` | `test_official_lobby_fullness_starts_without_waiting_for_original_deadline` | `max_players=5`，第 5 人加入 | 修复前失败，修复后通过 | 已修复通过 | D-009 | 2026-09-01 |
| D-010 | `Werewolf Node/Werewolf.cs:695-699` | `Werewolf.AddPlayer` | 已加入玩家再次发送加入 | 直接忽略，房间状态不改变 | `domain/engine.py:153-167` | `test_official_duplicate_or_started_join_leaves_room_unchanged` | 同一用户连续加入两次 | 修复前失败，修复后通过 | 已修复通过 | D-010 | 2026-09-01 |
| D-011 | `Werewolf Node/Werewolf.cs:689-693` | `Werewolf.AddPlayer` | 游戏已开始后有人发送加入 | 直接忽略，房间状态不改变 | `domain/engine.py:153-167` | `test_official_duplicate_or_started_join_leaves_room_unchanged` | 已进入夜晚后新用户加入 | 修复前失败，修复后通过 | 已修复通过 | D-011 | 2026-09-01 |
| D-012 | `Werewolf Node/Werewolf.cs:702-729` | `Werewolf.AddPlayer` | 显示名为空、以 `/` 开头、等于 `skip` 或本地化“跳过” | 拒绝加入，玩家列表不改变 | `domain/engine.py:153-167` | `test_official_lobby_rejects_reserved_join_names` | 空名、`/命令`、`skip` | 修复前失败，修复后通过 | 已修复通过 | D-012 | 2026-09-01 |
| D-013 | `Werewolf Node/Werewolf.cs:726-729` | `Werewolf.AddPlayer` | 与已有玩家显示名完全相同 | 拒绝加入，玩家列表不改变 | `domain/engine.py:153-167` | `test_official_lobby_rejects_duplicate_display_name` | 两个用户使用同一显示名加入 | 修复前失败，修复后通过 | 已修复通过 | D-013 | 2026-09-01 |
| D-014 | `Werewolf Node/Werewolf.cs:219` | `Program.GameStartInfo` → `new Werewolf` | 用户发送 `/startgame` 创建房间 | 创建者不会自动加入，必须另行发送 `/join` 或点加入按钮 | `application/service.py:151-176` | `test_official_startgame_creator_waits_for_join` | 群内创建者发送 `/startgame` | 修复前失败，修复后通过 | 已修复通过 | D-014 | 2026-09-01 |
| D-015 | `Werewolf Node/Werewolf.cs:702-709` | `Werewolf.AddPlayer` | 加入者昵称含换行或首尾空格 | 删除所有换行并去掉首尾空格后再保存、再参与后续校验 | `domain/engine.py:153-167` | `test_official_lobby_normalizes_display_name_before_saving` | 昵称为 `  甲\\n乙  ` | 修复前失败，修复后通过 | 已修复通过 | D-015 | 2026-09-01 |
| D-016 | `Werewolf Node/Werewolf.cs:702-709` | `Werewolf.AddPlayer` | 加入者昵称长度超过 64 个字符 | 官方保存完整的已清理昵称 | `domain/engine.py:160-162` | `test_official_lobby_keeps_full_display_name` | 65 个 `甲` | 修复前失败，修复后通过 | 已修复通过 | D-016 | 2026-09-01 |
| D-017 | `Werewolf Node/Werewolf.cs:840-841` | `GameCommands.Flee` → `Werewolf.RemovePlayer` | 未加入的用户发送 `/flee` | 直接忽略，房间状态不改变 | `domain/engine.py:192-202` | `test_official_flee_by_non_player_leaves_lobby_unchanged` | 仅 `u1` 已加入，`u2` 退出 | 修复前失败，修复后通过 | 已修复通过 | D-017 | 2026-09-01 |
| D-018 | `Werewolf Node/Werewolf.cs:442-445,511-516` | `GameCommands.ForceStart` → `GameTimer` | 管理员强制结束等待时人数不足最少人数 | 等待结束后不发牌、不进入夜晚，移除本局 | `application/service.py:231-235`、`domain/engine.py:235-250` | `test_official_force_start_with_too_few_players_does_not_start` | 2 人房间执行强制开始 | 修复前失败，修复后通过 | 已修复通过 | D-018 | 2026-09-01 |
| D-019 | `Werewolf Node/Werewolf.cs:511-516` | `GameTimer` | 等候时间结束且人数不足最少人数 | 发送人数不足提示后移除整局房间；后续命令找不到活动房间 | `application/service.py:496-532`、`infrastructure/db.py:180-206` | `test_official_insufficient_lobby_timeout_removes_room` | 1 人房间，等待时间 1 秒后将固定时钟推进 1 秒 | 修复前失败，修复后通过 | 已修复通过 | D-019 | 2026-09-01 |
| D-020 | `Werewolf Node/Werewolf.cs:450-453` | `GameTimer` | 等候最后 30 秒有新玩家加入 | 倒计时回拨，保证至少还剩约 60 秒 | `domain/engine.py:153-167` | `test_official_late_lobby_join_keeps_at_least_one_minute` | 180 秒等候期推进 150 秒后加入第 2 人 | 修复前失败，修复后通过 | 已修复通过 | D-020 | 2026-09-01 |
| D-021 | `Werewolf Node/Werewolf.cs:2824-2835` | `GameTimer` → `DayCycle` | 5 或 20 名存活玩家进入白天，基础白天时长为 60 秒 | 5 人局为 120 秒，20 人局为 150 秒 | `domain/engine.py:2137-2149` | `test_official_day_time_adds_one_minute_for_a_five_player_game` | 1 狼和其余村民，第 1 天；人数固定为 5、20 | 修复前失败，修复后通过 | 已修复通过 | D-021 | 2026-09-01 |
| D-022 | `Werewolf Node/Werewolf.cs:3003-3009` | `GameTimer` → `NightCycle` | 第 1 夜有丘比特、分身或野孩子 | 夜晚至少为 120 秒 | `domain/engine.py:235-250` | `test_official_first_night_special_role_lasts_at_least_two_minutes` | 5 人局，分别固定加入三种身份 | 修复前失败，修复后通过 | 已修复通过 | D-022 | 2026-09-01 |
| D-023 | `Werewolf Node/Werewolf.cs:3008-3009` | `GameTimer` → `NightCycle` | 第 1 夜有普通盗贼且未开启完整盗贼模式 | 夜晚至少为 120 秒 | `domain/engine.py:235-250` | `test_official_first_night_classic_thief_lasts_at_least_two_minutes` | 5 人局固定加入普通盗贼 | 修复前失败，修复后通过 | 已修复通过 | D-023 | 2026-09-01 |
| D-024 | `Werewolf Node/Werewolf.cs:3034-3041` | `GameTimer` → `NightCycle` | 银粉阻断唯一狼人，场上没有任何可用夜间菜单 | 等待约 1 秒后立即开始夜晚结算 | `domain/engine.py:365-418,2698-2699` | `test_official_night_without_available_actions_ends_after_one_second` | 第 2 夜 1 狼 4 村民，预置银粉 | 修复前失败，修复后通过 | 已修复通过 | D-024 | 2026-09-01 |
| P-007 | `Werewolf Node/Werewolf.cs:3034-3041` | `GameTimer` → `NightCycle` | 所有需要夜间选择的存活玩家都已完成操作 | 立即进入夜晚结算，不继续等待倒计时 | `domain/engine.py:446-490,613-628` | `test_official_night_resolves_when_every_required_action_is_submitted` | 第 2 夜，狼人和预言家依次提交动作 | 通过 | 已通过 | - | 2026-09-01 |
| P-008 | `Werewolf Node/Werewolf.cs:2584-2589` | `GameTimer` → `LynchCycle` | 所有存活玩家都已投票或弃票 | 立即统计票数并进入下一夜 | `domain/engine.py:2295-2318` | `test_official_vote_resolves_when_every_living_player_has_voted` | 5 名存活玩家依次弃票 | 通过 | 已通过 | - | 2026-09-01 |
| P-009 | `Werewolf Node/Werewolf.cs:3034-3041` | `GameTimer` → `NightCycle` | 夜晚倒计时结束 | 进入夜晚结算，再开始白天 | `domain/engine.py:2666-2694` | `test_official_phase_timeout_moves_to_its_next_stage` | 第 1 夜倒计时为 0 | 通过 | 已通过 | - | 2026-09-01 |
| P-010 | `Werewolf Node/Werewolf.cs:2829-2835` | `GameTimer` → `DayCycle` | 白天倒计时结束 | 处理白天行动后开始投票 | `domain/engine.py:2152-2163,2666-2694` | `test_official_phase_timeout_moves_to_its_next_stage` | 第 1 天倒计时为 0 | 通过 | 已通过 | - | 2026-09-01 |
| P-011 | `Werewolf Node/Werewolf.cs:2567-2589` | `GameTimer` → `LynchCycle` | 投票倒计时结束且无人投票 | 统计空票后开始下一夜 | `domain/engine.py:2340-2479,2666-2694` | `test_official_phase_timeout_moves_to_its_next_stage` | 第 1 轮投票倒计时为 0 | 通过 | 已通过 | - | 2026-09-01 |
| P-001 | `Werewolf Node/Werewolf.cs:1612-1660` | `GameTimer` → `NotifyRoles` → `GetRoleInfo` | 开局向预言家、傻瓜、观察者和村民发身份 | 保持玩家顺序逐个私聊；傻瓜显示预言家；观察者得知预言家 | `domain/engine.py:258-292` | `test_official_initial_identity_delivery_is_private_and_keeps_player_order` | 预言家、傻瓜、观察者、村民 | 通过 | 已通过 | - | 2026-09-01 |
| P-002 | `Werewolf Node/Werewolf.cs:5162-5292` | `GameTimer` → `NightCycle` → `SendNightActions` | 首夜可行动身份收到夜间菜单 | 18 个身份分别得到官方对应的唯一行动 | `domain/models.py:222-264`、`domain/engine.py:365-403` | `test_official_first_night_prompt_catalog_for_action_roles` | 18 个首夜可行动身份的固定房间 | 通过 | 已通过 | - | 2026-09-01 |
| P-003 | `Werewolf Node/Werewolf.cs:5162-5292` | `GameTimer` → `NightCycle` → `SendNightActions` | 首夜为白天身份、普通身份和未觉醒学徒生成菜单 | 不发送夜间菜单 | `domain/models.py:222-270`、`domain/engine.py:365-403,594-611` | `test_official_first_night_excludes_day_only_and_unawakened_roles` | 13 个无首夜菜单身份的固定房间 | 通过 | 已通过 | - | 2026-09-01 |
| P-004 | `Werewolf Node/Werewolf.cs:1757-1778` | `GameTimer` → `NightCycle` → `ValidateSpecialRoleChoices` | 第 1 夜野孩子或分身未选模板 | 系统按固定随机顺序补选一名其他存活玩家 | `domain/engine.py:754-777` | `test_official_first_night_unselected_role_models_are_randomly_completed` | 野孩子、分身各 1 名与 2 名固定目标 | 通过 | 已通过 | - | 2026-09-01 |
| P-005 | `Werewolf Node/Werewolf.cs:5258-5271` | `GameTimer` → `NightCycle` → `SendNightActions` | 化学家第一次夜晚尚未配药 | 标记为已配药、仅发配药私聊、不发目标菜单 | `domain/engine.py:373-385` | `test_official_first_night_chemist_brews_without_a_target_menu` | 化学家与 1 名村民，第 1 夜 | 通过 | 已通过 | - | 2026-09-01 |
| P-006 | `Werewolf Node/Werewolf.cs:5152-5314` | `GameTimer` → `NightCycle` → `SendNightActions` | 首夜多名玩家依序获得行动或自动信息 | 所有私聊按玩家顺序发送，配药消息不会挪到队首或队尾 | `domain/engine.py:365-418` | `test_official_first_night_messages_keep_player_order` | 预言家、化学家、狼人、村民固定顺序 | 通过 | 已通过 | - | 2026-09-01 |
| P-012 | `Werewolf Node/Werewolf.cs:614-635,637` | `GameTimer` | 一个 GameDay 内依次进入夜晚、白天、处决 | `CheckLongHaul` 在三个 Cycle 之前各调用一次 | `domain/engine.py:_finish_or_start_day`、`start_vote`、`_begin_vote` | `test_check_long_haul_runs_before_each_of_the_three_cycles` | 5 人局走完夜→昼→处决 | 通过 | 已通过 | - | 2026-09-02 |
| P-013 | `Werewolf Node/Werewolf.cs:614-618` | `GameTimer` | `GameDay++` 位于 while 循环开头 | 夜、昼、处决共用同一个 GameDay，处决结束后才加一 | `domain/engine.py:_after_lynch` | `test_game_day_increments_only_after_the_lynch` | 5 人局逐阶段读取 `room.day` | 通过 | 已通过 | - | 2026-09-02 |
| P-014 | `Werewolf Node/Werewolf.cs:2543-2811` | `GameTimer` → `LynchCycle` | 捣蛋鬼触发双重处决 | do/while 第二轮仍在同一 GameDay，`lynchAttempt` 变为 2 | `domain/engine.py:resolve_vote` | `test_double_lynch_second_round_keeps_the_same_game_day` | 捣蛋鬼白天按下双重处决后走完第一轮 | 通过 | 已通过 | - | 2026-09-02 |
| P-015 | `Werewolf Node/Werewolf.cs:2841-2851` | `GameTimer` → `DayCycle` 收尾 | 白天结束时清理未使用的白天菜单 | 只有 `QuestionType.Mayor` 与 `Pacifist` 的菜单保留到处决阶段，其余改为 TimesUp | `domain/engine.py:_begin_vote`、`submit_day_action` | `test_mayor_and_pacifist_menus_survive_into_the_lynch`、`test_troublemaker_menu_does_not_survive_into_the_lynch` | 投票阶段分别提交市长与捣蛋鬼动作 | 通过 | 已通过 | - | 2026-09-02 |
| P-016 | `Werewolf Node/Werewolf.cs:2661` | `GameTimer` → `LynchCycle` | 处决结束统计未投票玩家 | `NonVote` 仅在 `lynchAttempt < 2` 时累计 | `domain/engine.py:resolve_vote` | `test_non_vote_accumulates_only_on_the_first_lynch_attempt` | 双重处决两轮全员沉默 | 通过 | 已通过 | - | 2026-09-02 |
| P-017 | `Werewolf Node/Werewolf.cs:5420-5487,4420-4425` | `DayCycle` / `LynchCycle` → `HunterFinalShot(delay: false)` | 猎人被处决 | 立即发出开枪菜单并阻塞后续流程直到开枪或超时 | `domain/engine.py:_hunter_window`、`submit_hunter_kill` | `test_lynched_hunter_gets_official_lynch_menu_and_blocks_the_night` | 5 人局处决猎人 | 通过 | 已通过 | - | 2026-09-02 |
| P-018 | `Werewolf Node/Werewolf.cs:5420-5487,5620-5626` | `NightCycle` 结算 → `HunterFinalShot(delay: true)` | 猎人夜间遇害 | 延迟到白天播报前给出开枪菜单，措辞用 shot 文案 | `domain/engine.py:_resolve_hunter_window` | `test_night_killed_hunter_gets_shot_menu_and_blocks_the_day`、`test_night_shot_skip_uses_the_shot_wording`、`test_night_hit_uses_the_shot_wording` | 夜间狼人吃掉猎人 | 通过 | 已通过 | - | 2026-09-02 |
| P-019 | `Werewolf Node/Werewolf.cs:5455-5487` | `HunterFinalShot` | 开枪窗口超时、跳过或命中另一名猎人 | 超时随机开枪、跳过用官方文案、命中猎人重开窗口 | `domain/engine.py:_resolve_hunter_window`、`submit_hunter_kill` | `test_timeout_uses_official_no_choice_wording_and_resumes_the_flow`、`test_skip_uses_official_skip_wording_and_resumes_the_flow`、`test_hunter_shooting_a_hunter_reopens_the_window` | 固定随机种子的 5 人局 | 通过 | 已通过 | - | 2026-09-02 |
| P-020 | `Werewolf Node/Werewolf.cs:4505-4600` | `CheckForGameEnd` | 场上只剩两人且构成官方对决组合 | 按官方顺序判定恋人、猎人决斗、SK、纵火者、邪教分支 | `domain/engine.py:_check_for_game_end` | `test_hunter_wins_duel_uses_official_wording_before_the_kill`、`test_lovers_are_checked_before_the_hunter_duel`、`test_serial_killer_is_checked_before_the_cult_branch` | 各分支固定二人残局 | 通过 | 已通过 | - | 2026-09-02 |
| P-021 | `Werewolf Node/Werewolf.cs:4770-4901,4906-4923` | `DoGameEnd` → `ShowRolesEnd` | 对局结束播报胜负与名单 | 胜利文案按队伍取键、Overpower 先结算再播报、名单三种模式与 EndTime 一致 | `domain/engine.py:finish`、`_show_roles_end` | `test_win_announcement_uses_official_locale_per_team`、`test_roster_none_mode_lists_names_in_death_order_without_roles`、`test_roster_all_mode_marks_status_lover_and_result`、`test_roster_living_mode_lists_survivors_in_team_order`、`test_roster_ends_with_official_game_length` | 各队伍胜利与三种名单模式 | 通过 | 已通过 | - | 2026-09-02 |
| P-022 | `Werewolf Node/Werewolf.cs:5019-5150` | `GameTimer` → `DayCycle` → `SendDayActions` | 白天开始向可行动身份发菜单 | 触发条件、推送顺序与 `FirstOrDefault` 只提示首位同名身份一致 | `domain/engine.py:_day_prompts` | `test_prompt_order_matches_official_send_day_actions`、`test_duplicated_role_only_prompts_the_first_player`、`test_gunner_prompt_reports_remaining_bullets_and_needs_ammo` | 8 种白天身份的固定房间 | 通过 | 已通过 | - | 2026-09-02 |
| D-025 | `Werewolf Node/Werewolf.cs:2559-2564,2574-2604` | `GameTimer` → `LynchCycle` | 和平主义者在白天或处决倒计时中按下和平 | LynchCycle 立即公告 `PacifistNoLynchNow` 并 `return`：不发处决菜单、不计票、不累计 NonVote、不公布秘密投票结果，直接进入下一夜 | `domain/engine.py:_begin_vote`、`resolve_vote`、`submit_day_action` | `test_pacifist_pressed_in_daytime_skips_the_lynch_entirely`、`test_pacifist_pressed_during_the_lynch_aborts_it_immediately`、`test_pacifist_abort_does_not_tally_votes`、`test_pacifist_abort_does_not_accumulate_non_votes`、`test_pacifist_pressed_in_daytime_cancels_the_double_lynch` | 白天按下与投票阶段按下两组 5 人局，含开启秘密投票和市长已亮牌 | 修复前失败，修复后通过 | 已修复通过 | D-025 | 2026-09-02 |
