# 死亡链 / 白天公布 / 投票 / 胜负 复刻审计分片

独立复刻审计（openspec 变更 independent-official-parity-audit 任务 5.1–5.5、6.1–6.6）。
官方期望值唯一来源：`work/upstream-official/Werewolf for Telegram`，提交 `ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7`。

- 官方主文件：`Werewolf Node/Werewolf.cs`（下表行号均指该文件，另有注明除外）
- Python 主实现：`domain/engine.py`
- 新增测试：`tests/test_audit_death_vote_win.py`（9 通过 + 12 strict xfail）
- 本分片测试运行：`python -m pytest tests/test_audit_death_vote_win.py -q` → `9 passed, 12 xfailed`
- 全量：`python -m pytest -q` → `312 passed, 29 xfailed`（无失败；xfail 全部 strict）
- 引用的既有测试位于 `tests/test_official_parity.py` / `tests/test_parity.py`，均可用 `-k <测试名>` 重跑，本次审计已重跑确认全部通过。
- 差异编号本分片使用 `W-DIFF-01`–`W-DIFF-12`（其他分片使用 F-DIFF/Q-DIFF 前缀，无冲突）。

| 编号 | 官方文件:行号 | 调用来源 | 条件 | 官方结果 | Python 位置 | 测试 | 固定输入 | 实测结果 | 状态 | 差异编号 | 复查日期 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| W-DV-01 | Werewolf.cs:5585-5607 | KillPlayer（各击杀点） | 任意击杀 | IsDead/TimeDied/KilledByRole/DiedByVisiting* 落库，DiedLastNight=isNight 且非 LoverDied | engine.py:2005-2043 `_kill` | test_audit_lynch_lover_chain_and_hunter_counter | 5 人处决 u2（u2/u3 恋人，u3 猎人） | kill_method=Lynch、died_last_night=False、death_sequence 递增 | 已通过 | | 2026-09-01 |
| W-DV-02 | Werewolf.cs:5609-5615 | KillPlayer → KillLover | 死者 InLove 且恋人存活 | 恋人以 LoverDied 殉情，链先于猎人分支 | engine.py:2053-2062 | test_audit_lynch_lover_chain_and_hunter_counter | 同上 | 恋人 kill_method=LoverDied，death_sequence 在其后 | 已通过 | | 2026-09-01 |
| W-DV-03 | Werewolf.cs:5623-5625, 5706-5716 | KillLover → KillPlayer(LoverDied) | 殉情者是猎人 | hunterFinalShot 默认 true，猎人仍获最后一击 | engine.py:2063-2065 | test_audit_lynch_lover_chain_and_hunter_counter | 同上 | u3 收到 hunter_prompt，pending_hunt=True | 已通过 | | 2026-09-01 |
| W-DV-04 | Werewolf.cs:5602-5607, 5610 | FleePlayer → KillPlayer(Flee) | 逃跑者 InLove | Flee 早退，恋人不殉情 | engine.py:205-230 flee 未传 lover_chain=False | test_audit_flee_does_not_kill_lover | 白天 u1（恋人 u2）flee | Python 恋人被杀 LoverDied；断言官方行为 xfail | 已差异 | W-DIFF-01 | 2026-09-01 |
| W-DV-05 | Werewolf.cs:5602-5607 vs 5618-5621 | KillPlayer(Flee/Idle) | 死者是狼崽 | 早退跳过 WolfCubKilled，狼群无第二刀 | engine.py:2066-2067（无条件设置） | test_audit_wolf_cub_flee_does_not_arm_second_kill | 白天狼崽 flee | Python statistics.wolf_cub_killed=True；xfail | 已差异 | W-DIFF-02 | 2026-09-01 |
| W-DV-06 | Werewolf.cs:5376, 5588 | FleePlayer | 夜间逃跑 | isNight:false → DiedLastNight=false；妓女访问其空屋得"不在家" | engine.py:2039（按 room.phase 判定） | test_audit_night_flee_is_not_a_night_death | 第 2 夜 u1(狼)flee，妓女访问 u1 | Python died_last_night=True 且妓女死于 VisitVictim；xfail | 已差异 | W-DIFF-03 | 2026-09-01 |
| W-DV-07 | Werewolf.cs:2679-2684, 5602-5607 | LynchCycle 计票 | 连续 2 轮未投票 | Idle 击杀：无恋人链、无猎人反击 | engine.py:2448-2456 | test_idle_two_nonvotes_kills_without_hunter_or_lover（既有） | 恋人+猎人 u2 连续两轮不投票 | kill_method=Idle，无 hunter_prompt，恋人存活 | 已通过 | | 2026-09-01 |
| W-DV-08 | Werewolf.cs:5420-5489, 2795, 4441 | HunterFinalShot | 猎人补枪命中 | 击杀后立即 CheckRoleChanges + CheckForGameEnd（可当场终局） | engine.py:756-776 `_submit_hunter_action`（无收尾检查） | test_audit_hunter_final_shot_ends_game_immediately | 猎人死后"猎杀 2"打死最后一狼 | Python phase 仍 NIGHT、winner=None；xfail | 已差异 | W-DIFF-04 | 2026-09-01 |
| W-DV-09 | Werewolf.cs:5471-5476 | HunterFinalShot | 补枪打中长老 | 猎人降级为村民 | engine.py:766-774 | test_hunter_final_shot_demotes_on_wise_elder（既有） | 猎人补枪长老 | 猎人 role=Villager | 已通过 | | 2026-09-01 |
| W-DV-10 | Werewolf.cs:5431 | HunterFinalShot 菜单 | 补枪目标 | 仅存活玩家可选 | engine.py:763 `_target`(allow_dead=False) | test_audit_dead_player_loses_all_actions_and_candidacy | 投给死人/自投报错 | GameRuleError | 已通过 | | 2026-09-01 |
| W-DV-11 | Werewolf.cs:3061-4195（狼区在 SK 区前）+ VisitPlayer AlreadyDead | NightCycle 结算 | 同夜狼与 SK 同一目标 | 只死一次，死因保持 Eat | engine.py:812-828、1470-1487 | test_audit_same_night_wolf_then_sk_keeps_first_kill_method | 狼杀 u3、SK 杀 u3 | 单条 player_died，kill_method=Eat | 已通过 | | 2026-09-01 |
| W-DV-12 | Werewolf.cs:4955, 5029-5114 | SendLynchMenu/SendDayActions/SendNightActions | 玩家已死 | 不发任何菜单（无投票/行动资格） | engine.py:2414-2416, 525-526, 2334-2336 | test_audit_dead_player_loses_all_actions_and_candidacy | 死者投票/夜间/白天行动 | 均 GameRuleError | 已通过 | | 2026-09-01 |
| W-DV-13 | Werewolf.cs:5609-5610 | KillPlayer(dyingSimultaneously) | 恋人同被纵火烧死 | 不再互相触发殉情 | engine.py:2053-2061 | test_arsonist_burns_lovers_together_and_burning_home_kills_visitors（既有） | 纵火同时点燃恋人二人 | 双 Burn 无 LoverDied | 已通过 | | 2026-09-01 |
| W-DV-14 | Werewolf.cs:2323 附近 GraveDigger 逻辑 + 5605 | 掘墓人夜检 | 尸体死于 Flee/Idle | 不算新墓 | engine.py:2096-2119 | test_grave_digger_ignores_flee_and_idle_deaths（既有） | flee/idle 尸体 | dug_graves=0 | 已通过 | | 2026-09-01 |
| W-DV-15 | Werewolf.cs:2780, 2989-2998 | LynchCycle → CheckRoleChanges(true) | 处决后被咬者待转化 | 野孩子/分身/学徒暂不转化 | engine.py:2560, 1209-1218, 1976-1984 | test_bitten_wild_child_and_doppelganger_do_not_transform_after_lynch、test_lynch_bitten_apprentice_does_not_promote（既有） | 咬伤+偶像被处决 | 不转化 | 已通过 | | 2026-09-01 |
| W-DV-16 | Werewolf.cs:4198-4245 + GroupConfig.ShowRolesDeath | 夜死公告 | 开/关死亡显身份 | 公告含/不含角色 | engine.py:2044-2052 | test_death_notice_can_show_or_hide_role（既有） | show_roles_on_death 两态 | 文案含/不含身份 | 已通过 | | 2026-09-01 |
| W-DV-17 | Werewolf.cs:4198-4405 | 夜死公告 | 各死因 | 死因文案家族（Eat/SerialKilled/Burn/…） | engine.py:2070-2094 `_death_notice` | test_death_notice_keeps_official_cause（既有） | 各 KillMethod | 对应中文文案 | 已通过 | | 2026-09-01 |
| W-DV-18 | Werewolf.cs:4433-4437 | NightCycle 末尾 | 无人夜死 | 公告 NoAttack（平安夜） | engine.py:resolve_night（无对应公开事件） | test_audit_quiet_night_announces_no_attack | 狼跳过的平安夜 | Python 仅 day_started，无夜晚结果公告；xfail | 已差异 | W-DIFF-12 | 2026-09-01 |
| W-DV-19 | Werewolf.cs:2824-2831 | DayCycle | 白天开场 | 时长=DayTime+max((alive/5-1)*30,60)，发玩家名单 | engine.py:2259-2263 | test_official_day_time_adds_one_minute_for_a_five_player_game（既有） | 5 人局 | +60 秒并附名单 | 已通过 | | 2026-09-01 |
| W-DV-20 | Werewolf.cs:4906-4917 | DoGameEnd | ShowRolesEnd=None/All/默认 | 三种结算名单 | engine.py:2669-2689 | test_finish_uses_official_role_visibility_mode（既有） | 三种模式 | 名单形态一致 | 已通过 | | 2026-09-01 |
| W-DV-21 | Werewolf.cs:2871-2898 | DayCycle 结束 | 枪手开枪 | 讨论结束统一结算；打长老降级为村民且子弹清零 | engine.py:2287-2299 | test_gunner_shoots_and_is_demoted_for_elder、test_gunner_can_fire_two_bullets_then_cannot_fire_again（既有） | 枪手射长老/两发 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-22 | Werewolf.cs:2902-2930 | DayCycle 结束 | 南瓜头引爆 | 40% 双亡否则私聊失败 | engine.py:2300-2305 | test_spumpkin_detonation_pinned（既有） | 钉死概率 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-23 | Werewolf.cs:2933-2968 | DayCycle 结束 | 侦探侦查 | 结束时私聊真实身份，40% 暴露给狼 | engine.py:2307-2327 | test_detective_sees_true_role_and_can_be_caught（既有） | 钉死概率 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-24 | Werewolf.cs:4959 | SendLynchMenu | 投票候选 | 不含自己、不含死者 | engine.py:2425 `_target` | test_audit_dead_player_loses_all_actions_and_candidacy | 自投/投死者 | GameRuleError | 已通过 | | 2026-09-01 |
| W-DV-25 | Werewolf.cs:2637-2660（菜单一次性） | LynchCycle | 重复投票/改票 | 官方内联菜单一次性，无改票 | engine.py:2417-2418 | test_vote_rejects_change（既有） | 二次投票 | 抛异常 | 已通过 | | 2026-09-01 |
| W-DV-26 | Werewolf.cs:2659-2663 | LynchCycle 计票 | 投票者/弃权者 | 投票 NonVote=0；弃权与超时 NonVote+1 | engine.py:2448-2458 | test_audit_all_abstain_counts_nonvote_and_lynches_nobody | 4 人全弃权/超时 | 全员 non_vote_count=1，无人死 | 已通过 | | 2026-09-01 |
| W-DV-27 | Werewolf.cs:2661-2684 | LynchCycle 计票 | NonVote≥2 | Idle 击杀并 CheckRoleChanges | engine.py:2450-2456 | test_idle_two_nonvotes_kills_without_hunter_or_lover（既有） | 连续两轮不投票 | Idle 出局 | 已通过 | | 2026-09-01 |
| W-DV-28 | Werewolf.cs:2661 `lynchAttempt < 2` | 双重处决第二轮 | 第二轮未投票 | 不累计 NonVote | engine.py:2448-2456（无轮次判断） | test_audit_double_lynch_second_round_skips_idle_penalty | 捣乱双处决，u5 两轮不投 | Python 第二轮累计到 2 并 Idle 击杀；xfail | 已差异 | W-DIFF-05 | 2026-09-01 |
| W-DV-29 | Werewolf.cs:2559-2604 | LynchCycle | 和平主义者生效 | 整轮处决与计票直接跳过（不计 NonVote） | engine.py:2448-2458 在 2474 pacifist 判断之前 | test_audit_pacifist_round_skips_idle_penalty | pacifist_used + u5 不投票 | Python non_vote_count=1；xfail（处决取消本身已由 test_pacifist_cancels_lynch 通过） | 已差异 | W-DIFF-06 | 2026-09-01 |
| W-DV-30 | Werewolf.cs:2651-2657 | LynchCycle 计票 | 市长已亮身份 | 该票计两次（含 SecretLynch 记录） | engine.py:2463-2468 | test_mayor_vote_weight_two、test_secret_lynch_records_votes_and_mayor_weight（既有） | 市长亮后投票 | 权重 2 | 已通过 | | 2026-09-01 |
| W-DV-31 | Werewolf.cs:2694-2697, 2790-2793 | LynchCycle 结果 | 全零票 | NoLynchVotes，无人出局 | engine.py:2508-2509 | test_audit_all_abstain_counts_nonvote_and_lynches_nobody | 全弃权 | vote_empty 事件 | 已通过 | | 2026-09-01 |
| W-DV-32 | Werewolf.cs:2698-2712, 2783-2788 | LynchCycle 结果 | 平票 | RandomLynch 开：洗牌选一；关：LynchTie 无人死 | engine.py:2488-2507 | test_random_lynch_resolves_tied_candidates、test_vote_tie_lynches_nobody（既有） | 平票两态 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-33 | Werewolf.cs:2745-2749, 2763 | LynchCycle 结果 | 王子首次被处决 | 公开身份免死（HasUsedAbility），二次照杀 | engine.py:2482-2501 | test_prince_survives_first_lynch、test_prince_dies_on_second_lynch（既有） | 王子两次被投 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-34 | Werewolf.cs:2752-2777 | LynchCycle 结果 | 坦纳被处决 | KillPlayer 后立即 DoGameEnd(Tanner) 结束（含双处决中断） | engine.py:2556-2559 | test_tanner_wins_only_on_lynch（既有）、test_audit_tanner_win_carries_lover | 坦纳被投 | winner=Tanner、坦纳 won=True | 已通过 | | 2026-09-01 |
| W-DV-35 | Werewolf.cs:2717-2741 | LynchCycle 结果 | SecretLynch(+ShowVotes/ShowVoters) | 公布票数/投票人；关则隐藏 | engine.py:2510-2528 | test_secret_lynch_records_votes_and_mayor_weight、test_secret_lynch_hides_result_when_show_votes_is_off（既有） | 两态 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-36 | Werewolf.cs:2544-2547, 2812 | LynchCycle | 捣乱者启用 | do/while 连续两轮处决 | engine.py:2564-2573 | test_troublemaker_starts_second_vote_cycle（既有） | 双轮处决 | 第二轮 vote_started | 已通过 | | 2026-09-01 |
| W-DV-37 | Werewolf Node（ClumsyGuy 投票分支） | 投票提交 | 冒失鬼投票 | 50% 改投随机活人 | engine.py:2426-2431 | test_clumsy_retarget_pinned（既有） | 钉死概率 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-38 | Werewolf.cs:4514-4517 | CheckForGameEnd | 0 人存活 | NoOne | engine.py:2598-2599 | test_audit_win_zero_and_one_alive_table | 全员死亡 | winner=NoOne | 已通过 | | 2026-09-01 |
| W-DV-39 | Werewolf.cs:4518-4523 | CheckForGameEnd | 1 人存活 | Tanner/Sorcerer/Thief/DG→NoOne，否则按队伍 | engine.py:2604-2609 | test_audit_win_zero_and_one_alive_table、test_win_noone_tanner_last_and_thief_last（既有） | 独活教徒/坦纳/盗贼 | Cult / NoOne | 已通过 | | 2026-09-01 |
| W-DV-40 | Werewolf.cs:4524-4527 | CheckForGameEnd | 2 人皆 InLove | Lovers 优先于 SK/狼等一切 | engine.py:2611-2613 | test_audit_win_two_player_table、test_win_lovers_two_alive（既有） | SK+村民恋人 | winner=Lovers，双胜 | 已通过 | | 2026-09-01 |
| W-DV-41 | Werewolf.cs:4528-4530, 4591-4594, 4695-4806 | CheckForGameEnd/DoGameEnd | 2-3 人全为特殊角色 | NoOne + 专属结局文案（坦纳自杀等） | engine.py:2613-2631, 2702-2752 | test_official_noone_special_end_matrix、test_no_one_tanner_special_end_records_the_official_suicide（既有） | 组合矩阵 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-42 | Werewolf.cs:4532-4554 | CheckForGameEnd | 2 人猎人+狼/雪狼 | 按 HunterKillWolfChanceBase 决斗：村胜或狼胜 | engine.py:2223-2230 | test_win_hunter_vs_wolf_two_player_pinned（既有）、test_audit_hunter_vs_snow_wolf_duel_pinned | 钉死 100/0；猎人+雪狼 | 一致，雪狼死于 Hunter | 已通过 | | 2026-09-01 |
| W-DV-43 | Werewolf.cs:4534-4536 | CheckForGameEnd | 2 人双猎人 | other==null → 村民胜 | engine.py:2646-2647（落到村胜） | test_audit_win_two_player_table | 猎人+猎人 | winner=Village | 已通过 | | 2026-09-01 |
| W-DV-44 | Werewolf.cs:4537-4538, 4880-4897 | CheckForGameEnd/DoGameEnd | 2 人猎人+SK | SKHunter：互杀、无人计胜 | engine.py:2231-2235, 2595-2596, 2758-2763 | test_win_sk_hunter_nobody_won（既有） | 猎人+SK | 双亡、won 全 False | 已通过 | | 2026-09-01 |
| W-DV-45 | Werewolf.cs:4556-4558 | CheckForGameEnd | 2 人含 SK（非上述） | SerialKiller 胜 | engine.py:2616-2617 | test_win_serial_killer_two_player（既有） | SK+村民 | winner=SerialKiller | 已通过 | | 2026-09-01 |
| W-DV-46 | Werewolf.cs:4858-4868 | DoGameEnd(SerialKiller) | SK 胜时另一人存活 | KillPlayer(SerialKilled) 补杀 | engine.py:2654-2700 finish（无补杀） | test_audit_sk_two_player_win_kills_survivor | SK+村民终局 | Python 村民仍存活；xfail | 已差异 | W-DIFF-08 | 2026-09-01 |
| W-DV-47 | Werewolf.cs:4560-4561 | CheckForGameEnd | 2 人含纵火犯 | 枪手有子弹则不胜，否则纵火犯胜 | engine.py:2618-2619 | test_win_arsonist_two_player_blocked_by_gunner_bullets（既有） | 两态 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-48 | Werewolf.cs:4835-4847 | DoGameEnd(Arsonist) | 纵火犯胜时另一人存活 | IsDead=true（Burn 记录）补杀 | engine.py:2654-2700 finish（无补杀） | test_audit_arsonist_two_player_win_kills_survivor | 纵火犯+村民终局 | Python 村民仍存活；xfail | 已差异 | W-DIFF-09 | 2026-09-01 |
| W-DV-49 | Werewolf.cs:4563-4588 | CheckForGameEnd | 2 人含教徒 | 对狼→狼胜；对 CH→村胜；对普通→自动转化教会胜；对盗贼/分身→不转化教会胜 | engine.py:2620-2628, 2236-2247 | test_audit_win_two_player_table、test_cult_two_player_hunter_kills_cultist、test_cult_two_player_wolf_eats_and_villager_converts（既有） | 教徒+狼/CH/猎人/盗贼 | winner 一致，转化一致 | 已通过 | | 2026-09-01 |
| W-DV-50 | Werewolf.cs:4576-4580 | CheckForGameEnd(2 人 CH+教徒) | CH 对教徒 | 仅 DBKill 记录，教徒 IsDead 不置位（结算名单显示存活） | engine.py:2239-2240（真正击杀） | test_audit_ch_vs_cultist_endgame_keeps_cultist_alive | CH+教徒终局 | Python 教徒 alive=False kill_method=Hunt；xfail | 已差异 | W-DIFF-10 | 2026-09-01 |
| W-DV-51 | Werewolf.cs:4600-4604 | CheckForGameEnd | >2 人且 SK/纵火犯存活 | 阻断一切胜利判定 | engine.py:2632-2633 | test_serial_killer_blocks_other_wins（既有） | SK 在场 | winner=None | 已通过 | | 2026-09-01 |
| W-DV-52 | Werewolf.cs:4606-4607 | CheckForGameEnd | 全员教徒 | Cult 胜 | engine.py:2634-2635 | test_audit_wolf_parity_and_cult_sweep | 3 教徒 | winner=Cult | 已通过 | | 2026-09-01 |
| W-DV-53 | Werewolf.cs:4610-4627 | CheckForGameEnd | 狼(含雪狼)数≥其他 | 狼胜；枪手在 wolves==others 或 双狼恋人+1 时挡住 | engine.py:2636-2645 | test_audit_wolf_parity_and_cult_sweep、test_win_gunner_blocks_wolf_parity、test_lovers_wolves_one_ahead_are_blocked_by_gunner（既有） | 2 狼 2 村 / 枪手局 | 一致 | 已通过 | | 2026-09-01 |
| W-DV-54 | Werewolf.cs:4629-4632 | CheckForGameEnd | 无狼无教无 SK 无纵火 | 村胜（checkbitten 有被咬则暂缓） | engine.py:2646-2647 | test_village_wins_when_wolves_gone（既有）、test_audit_win_two_player_table | 坦纳+村民 | winner=Village，坦纳 won=False | 已通过 | | 2026-09-01 |
| W-DV-55 | Werewolf.cs:4485-4511 | CheckForGameEnd 开头 | 无食狼角色存活 | 雪狼优先转普狼，否则叛徒转狼 | engine.py:2197-2216 | test_snow_wolf_promotes_before_traitor、test_traitor_promotes_when_no_wolf_roles（既有） | 雪狼+叛徒 | 雪狼转狼 | 已通过 | | 2026-09-01 |
| W-DV-56 | Werewolf.cs:4490-4497, 4504-4509 | CheckForGameEnd(checkbitten) | 雪狼/叛徒待补位且有人被咬 | `return false` 中止整次胜负检查（含狼数优势） | engine.py:2186-2195（仅跳过补位，仍判胜负） | test_audit_checkbitten_pending_bite_aborts_end_check | 雪狼+被咬村民，checkbitten | Python 直接判狼胜 FINISHED；xfail | 已差异 | W-DIFF-11 | 2026-09-01 |
| W-DV-57 | Werewolf.cs:4651-4665 | DoGameEnd(Lovers) | 恋人胜 | 所有 InLove 玩家 Won=true | engine.py:2760-2761 | test_win_lovers_two_alive（既有）、test_audit_win_two_player_table | 恋人终局 | 双胜 | 已通过 | | 2026-09-01 |
| W-DV-58 | Werewolf.cs:4668-4691 | DoGameEnd（队伍胜） | 胜方队伍成员+其恋人 | 队伍成员 Won（SK/纵火需存活，坦纳需被处决者）；InLove 恋人连带 Won | engine.py:2754-2777 `_player_won` | test_lover_of_winner_also_wins（既有） | 狼胜+村民恋人 | 恋人连带胜 | 已通过 | | 2026-09-01 |
| W-DV-59 | Werewolf.cs:4675-4690 | DoGameEnd(Tanner) | 被处决坦纳 InLove | 坦纳 Won 且其恋人 Won | engine.py:2764-2765（TANNER 分支提前 return，恋人分支不可达） | test_audit_tanner_win_carries_lover | 坦纳(恋人 u4)被处决 | Python 恋人 won=False；xfail | 已差异 | W-DIFF-07 | 2026-09-01 |
| W-DV-60 | Werewolf.cs:4643（IsRunning=false） | DoGameEnd 后 | 游戏已结束 | 一切后续操作被丢弃 | engine.py:2412, 2532, 2779（按阶段拒绝/空返回） | test_audit_finished_room_rejects_further_operations | FINISHED 后投票/夜行动/结算 | 异常或空事件 | 已通过 | | 2026-09-01 |
| W-DV-61 | Werewolf.cs:2922（Spumpkin 自杀 killMethod:null） | DayCycle | 南瓜头引爆自杀 | 自杀不记 DBKill 死因（null） | engine.py:2302（记为 Shoot） | —（仅元数据差异，状态机一致，见 test_spumpkin_detonation_pinned） | 引爆成功 | Python 自杀 kill_method=Shoot；仅统计字段差异，不影响公开信息/胜负 | 未决 | | 2026-09-01 |

## 计数

- 总行数：61
- 已通过：48
- 已差异：12（W-DIFF-01 ~ W-DIFF-12，均有 strict xfail 测试）
- 已确认未调用：0
- 未决：1（W-DV-61，Spumpkin 自杀死因元数据；不影响可见行为，留待后续定夺）

## 差异清单（W-DIFF）

| 差异编号 | 摘要 | xfail 测试 |
|---|---|---|
| W-DIFF-01 | flee 触发恋人殉情；官方 KillPlayer 对 Flee/Idle 早退（Werewolf.cs:5602-5607） | test_audit_flee_does_not_kill_lover |
| W-DIFF-02 | flee/idle 击杀狼崽仍置 wolf_cub_killed；官方早退跳过（Werewolf.cs:5618-5621） | test_audit_wolf_cub_flee_does_not_arm_second_kill |
| W-DIFF-03 | 夜间 flee 记为夜死（died_last_night=True）并触发妓女 VisitVictim；官方 isNight:false（Werewolf.cs:5376） | test_audit_night_flee_is_not_a_night_death |
| W-DIFF-04 | 猎人最后一枪后缺 CheckRoleChanges/CheckForGameEnd，胜负延迟到下一边界（Werewolf.cs:5480-5485、4441） | test_audit_hunter_final_shot_ends_game_immediately |
| W-DIFF-05 | 双重处决第二轮仍累计未投票惩罚；官方 lynchAttempt<2 才累计（Werewolf.cs:2661） | test_audit_double_lynch_second_round_skips_idle_penalty |
| W-DIFF-06 | 和平主义者生效轮仍累计未投票惩罚；官方整轮跳过（Werewolf.cs:2559-2604） | test_audit_pacifist_round_skips_idle_penalty |
| W-DIFF-07 | 坦纳胜利时其恋人未连带计胜；官方 DoGameEnd 恋人 Won（Werewolf.cs:4681-4690） | test_audit_tanner_win_carries_lover |
| W-DIFF-08 | SK 二人残局胜利未补杀另一存活者（Werewolf.cs:4858-4868） | test_audit_sk_two_player_win_kills_survivor |
| W-DIFF-09 | 纵火犯二人残局胜利未补杀另一存活者（Werewolf.cs:4835-4847） | test_audit_arsonist_two_player_win_kills_survivor |
| W-DIFF-10 | CH vs 教徒二人残局 Python 击杀教徒；官方仅 DBKill、教徒存活（Werewolf.cs:4576-4580） | test_audit_ch_vs_cultist_endgame_keeps_cultist_alive |
| W-DIFF-11 | checkbitten 且雪狼/叛徒待补位、有 Bitten 时官方中止整次胜负检查；Python 判狼胜（Werewolf.cs:4490-4497） | test_audit_checkbitten_pending_bite_aborts_end_check |
| W-DIFF-12 | 平安夜缺 NoAttack 公告（Werewolf.cs:4433-4437） | test_audit_quiet_night_announces_no_attack |
