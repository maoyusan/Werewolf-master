# 官方规则来源矩阵

基线：`work/upstream-official` commit `ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7`。官方源码是唯一期望值来源；Python 旧行为不作为证明。

| 规则范围 | 官方源码位置 | Python 位置 | 测试、输入和关键输出 |
| --- | --- | --- | --- |
| 角色目录、图标、可禁用、强度 | `Shared/Roles.cs:11-145`；`Shared/GameBalancing.cs:34-314` | `domain/models.py:_ROLE_ROWS`；`domain/rules.py:35-144` | `test_official_role_catalog_emoji_team_disable_strength`；43 个角色逐项比较，退出 `0` |
| 狼人牌和狼人多数 | `Werewolf Node/Werewolf.cs:48`；`Shared/GameBalancing.cs:32` | `domain/models.py:WOLF_ROLES`、`MAJORITY_WOLF_ROLES` | `test_official_wolf_roles_exclude_snow_wolf`、`test_snow_wolf_counts_for_majority`；SnowWolf 不投吃但计多数 |
| 设置概率和转化表 | `Werewolf Node/Helpers/Settings.cs:73-151` | `domain/models.py:274-365`；`domain/rules.py:180-204` | `test_official_settings_chances_match_settings_cs`、`test_every_official_cult_conversion_role_honors_zero_and_hundred`；逐项比较及 0/100 边界 |
| 玩家状态、死因、恋人和统计 | `Werewolf Node/Models/IPlayer.cs:15-219` | `domain/models.py:374-433` | 各 `test_*` 对比 `alive`、`role`、`team`、`lover_id`、`kill_method`、`won` 和事件 metadata |
| 夜间总顺序 | `Werewolf Node/Werewolf.cs:3061-4195` | `domain/engine.py:671-1038` | `test_parity.py` 夜间 fixture 及 `test_official_parity.py` 夜间用例；检查事件顺序和阶段 |
| 访问、连环杀手和掘墓人 | `Werewolf.cs:2323-2426,3501-3545,5294-5300` | `domain/engine.py:1363-1427,1987-2030` | `test_*visit*`、`test_serial_killer_*`、`test_grave_digger_*`；80%、50%、人数公式和 `FLEE`/`IDLE` 排除 |
| 转化、恋人、野孩子、分身、学徒 | `Werewolf.cs:1930-2030,2550-3038,3609-3780` | `domain/engine.py:1238-1361,2033-2117` | `test_cult_*`、`test_lovers_death_chain`、`test_doppelganger_*`；检查转化角色、团队和死亡链 |
| RandomLynch | `Werewolf.cs:2690-2710`；`Werewolf Node/Helpers/Settings.cs:149` | `domain/engine.py:2324-2388`；`domain/models.py:284` | `test_random_lynch_resolves_tied_candidates`；开启开关后平票候选只出局 1 人 |
| SecretLynch 展示开关 | `Werewolf.cs:2648-2655,2717-2740` | `domain/engine.py:2394-2435` | `test_secret_lynch_records_votes_and_mayor_weight`、`test_secret_lynch_hides_result_when_show_votes_is_off`；分别检查票数、投票人和隐藏结果 |
| Augur 可能身份顺序 | `Werewolf.cs:3991-4049` | `domain/engine.py:1040-1065` | `test_augur_shuffles_persistent_possible_roles_in_place`、`test_augur_sees_absent_possible_role`；原列表被打乱并排除已在场身份 |
| 盗贼夜间流程 | `Werewolf.cs:4132-4186`；角色转换 `2212-2260` | `domain/engine.py:1649-1785` | `test_thief_*`；经典模式首夜超时随机偷取，完整模式按概率且不能偷狼人 |
| 化学家、巫师和访问死亡 | `Werewolf.cs:3792-3838,3860-3940` | `domain/engine.py:1363-1647` | `test_chemist_success_and_suicide_and_ignores_ga`、`test_chemist_guardian_and_thief_visit_death_branches` |
| 投票、白天能力和阶段限制 | `Werewolf.cs:2537-2813,4196-4476` | `domain/engine.py:2152-2477`；`application/service.py:181-276` | `test_vote_*`、`test_private_mayor_reveal_can_begin_during_vote`；检查不能改票、平票、能力时机和错误提示 |
| 胜负和二人/三人残局 | `Werewolf.cs:4477-5116`，重点 `4514-4593,4693-4811` | `domain/engine.py:2478-2650` | `test_win_*`、`test_no_one_*`；检查狼、村民、教会、SK、纵火和 NoOne |
| QQ 指令和群配置 | 平台无 Telegram 对应源码；规则开关来源仍为上述官方设置 | `application/commands.py:1-170`；`application/service.py:34-195,440-488`；`adapters/qq/adapter.py` | `test_parse_official_game_start_commands`、`test_creates_official_group_command_panel_once`、`test_settings_passes_group_rule_switches_to_new_rooms` |
| 管理员和开发者命令 | `Werewolf Control/Commands/AdminCommands.cs`、`Commands/DevCommands.cs`、`Attributes/CommandAttribute.cs`、`Handlers/UpdateHandler.cs:330-334`、`Commands/Helpers.cs:92-96`、`Program.cs:35,393` | `application/commands.py:143-169`；`application/service.py:_ADMIN_COMMANDS 及 _admin_* 方法`；`domain/locale.py:validate_language_files` | `tests/test_audit_admin_commands.py`；覆盖权限分档、封禁静默丢弃、维护模式、封群、smite/killgame/skipvote、封禁增删查、成就增删、`/validatelangs`、`/broadcast` |
| 开局快照和重启恢复 | `IPlayer.cs:15-219` 及官方房间状态字段 | `domain/models.py:455-523`；`infrastructure/db.py` | `test_room_snapshot_restores_deadline_actions_and_pending_choice`、`test_service_restart_processes_overdue_room_and_skips_finished_room`、`test_group_rule_config_survives_new_store_instance` |
| SendGif 随附的死亡私聊文案 | `Werewolf.cs:3203,3312,3365,3434,3479-3481,3533,3544-3546,4399-4404` | `domain/engine.py:_victim_notice`、`_maybe_spot_grave_digger`、`_death_notice`、`_resolve_arsonist`、`_resolve_wolf_attack`、`_resolve_serial_killer` | `tests/test_audit_victim_notices.py`；覆盖 `Burn`、`WolvesEatYou`、`WolvesSpottedYou`、`SerialKillerKilledYou`、`SerialKillerSpottedYou` 以及凶手侧私聊与掘墓人公开播报分支 |
| SendDayActions 白天技能菜单 | `Werewolf.cs:5019-5150` | `domain/engine.py:_day_prompts` | `tests/test_audit_day_prompts.py`；逐角色核对 Detective/Mayor/Pacifist/Sandman/Blacksmith/Gunner/Spumpkin/Troublemaker 的触发条件、推送顺序与 `FirstOrDefault` 语义 |
| 猎人临终一枪 HunterFinalShot | `Werewolf.cs:5420-5487`、`5620-5626`、`4420-4425` | `domain/engine.py:_hunter_window`、`submit_hunter_kill`、`_resolve_hunter_window` | `tests/test_audit_hunter_final_shot.py`；覆盖夜间 `delay=True` 与白天 `delay=False` 的阻塞时机、开枪窗口时长、超时随机开枪与恋人连锁 |
| 残局决斗与结算播报 | `Werewolf.cs:4505-4600,4770-4901,4906-4923` | `domain/engine.py:_check_for_game_end`、`finish`、`_show_roles_end` | `tests/test_audit_endgame_narration.py`；覆盖二人残局分支、各队伍胜利文案、Overpower 结算与 ShowRolesEnd 的三种名单和 EndTime |
| DevOnly 调试命令 | `Werewolf Control/Commands/DevCommands.cs:114-151,162-166,499-625,705-747,1330-1376`；`Helpers/Charting.cs:21-207`；`Commands/AdminCommands.cs:671-684`；`Program.cs:251-295` | `application/commands.py:170-179`；`application/service.py:_DEV_COMMANDS 及 _admin_winchart/_admin_test/_admin_usage/_admin_checkgroups/_admin_clearcount/_admin_getcommands/_admin_reloadenglish/_admin_moveachv/_admin_ohaider/_admin_fi` | `tests/test_audit_dev_commands.py`；覆盖 10 条 DevOnly 命令的接线、非开发者拒绝、`/winchart` 间隔与模式解析、`/test` 月度补零、`/usage` 采样、`/checkgroups` 名称折叠、`/getcommands` 一分钟窗口、`/ohaider` 四个分支与 `/fi` 全部计数行 |
| GameTimer 主循环与 LynchCycle 边界 | `Werewolf.cs:614-635,2537-2812,2815-2972,878-1000` | `domain/engine.py:_finish_or_start_day`、`start_vote`、`_begin_vote`、`resolve_vote`、`_after_lynch`、`submit_day_action` | `tests/test_audit_lynch_cycle.py`；覆盖 `timeToAdd` 时长公式、`CheckLongHaul` 的三处调用点、`GameDay++` 时机、双重处决第二轮、白天结束保留 Mayor/Pacifist 菜单、和平主义者在白天与处决倒计时中的两处立即中止（不计票、不累计 NonVote、不公布秘密投票） |

## 验收记录

执行目录：`D:\Project\ScriptProjects\Werewolf-master\werewolf-python`。

| 命令 | 输出 | 退出状态 |
| --- | --- | --- |
| `python -m pytest tests/test_official_parity.py -q` | `200 passed` | `0` |
| `python -m pytest tests/test_official_parity.py tests/test_parity.py tests/test_adapter_and_commands.py -q` | `238 passed` | `0` |
| `python -m pytest -q` | `586 passed` | `0` |

JSON 金样由 `tests/test_parity.py::test_all_json_fixtures` 执行；QQ 只做命令和适配层检查，不代替用户进行真实群聊。
