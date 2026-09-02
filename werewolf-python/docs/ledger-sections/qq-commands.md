# QQ 文字指令入口复刻审计分片

独立复刻审计（openspec 变更 independent-official-parity-audit 任务 8.1–8.5）。
官方期望值唯一来源：`work/upstream-official/Werewolf for Telegram`，提交 `ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7`。

- Python 源码根目录：`D:\Project\ScriptProjects\Werewolf-master\werewolf-python`
- 检查日期：`2026-09-01`
- 测试文件：`tests/test_audit_qq_commands.py`（48 个通过 + 8 个 strict xfail）
- 基线：全量 `python -m pytest -q` → 286 passed + 8 xfailed（strict），无失败无跳过。

状态：已通过 / 已差异 / 已确认未调用 / 未决。
每条记录均有可重跑固定输入与实测结果（`-k <测试名>` 即重跑）。

## 总表分片

| 编号 | 官方文件:行号 | 调用来源 | 条件 | 官方结果 | Python 位置 | 测试 | 固定输入 | 实测结果 | 状态 | 差异编号 | 复查日期 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Q-001 | GameCommands.cs:20-30 | `/startgame` 群命令 | 群里无局 | 建局（Normal）并展示加入入口 | application/service.py:152-176 | test_startgame_creates_room_with_join_guidance | `/startgame`（群 u1） | 输出“房间已创建 /join”，房间 phase=LOBBY，mode=Normal | 已通过 | - | 2026-09-01 |
| Q-002 | GameCommands.cs:32-42 | `/startchaos` 群命令 | 群里无局 | 建局（Chaos）并展示加入入口 | application/service.py:159 | test_startchaos_creates_chaos_mode_room | `/startchaos`（群 u1） | 输出“房间已创建”，mode=Chaos | 已通过 | - | 2026-09-01 |
| Q-003 | GameCommands.cs:20-30 + Helpers.cs:121-143 | `/startgame` 群命令 | 群已有局 | 重发 ShowJoinButton（加入入口） | service.py:156-157 | test_startgame_during_lobby_reprompts_join_instead_of_error | 同群 u1 建局后 u2 `/startgame` | 输出“当前群已有活动房间”，未引导 /join | 已差异 | Q-DIFF-01 | 2026-09-01 |
| Q-004 | Helpers.cs:43-48 | `/startgame` 私聊 | 私聊 | StartFromGroup：请在群里开局 | service.py:153-154 | test_startgame_in_private_rejected_and_guides_group_slash_command | `/startgame`（C2C u1） | “请在群里发送 /startgame” | 已通过 | - | 2026-09-01 |
| Q-005 | Helpers.cs:126-135 | `/startgame` 建局 | 玩家已在其他对局 | AlreadyInGame：拒绝 | service.py:157-158,585-588 | test_startgame_when_user_active_in_other_group_rejected | u1 在 g-other 后于 g1 `/startgame` | “你已在其他群的活动房间中” | 已通过 | - | 2026-09-01 |
| Q-006 | GameCommands.cs:44-74 | `/join` 群命令 | 无局 | NoGame：无局提示 | service.py:214-216 | test_join_without_room_guides_startgame | `/join`（群 u1） | 输出含 /startgame 引导 | 已通过 | - | 2026-09-01 |
| Q-007 | Werewolf.cs:685-824 | AddPlayer（join） | 群在入场期且玩家未加入 | 加入并展示 YouJoined | service.py:219-221 / engine.py:153-181 | test_join_success_then_repeat_join_is_silent | 建局后 `/join`（u2 “甲”） | “甲 加入游戏”，玩家入座 | 已通过 | - | 2026-09-01 |
| Q-008 | Werewolf.cs:695-699 | AddPlayer | 玩家已加入 | 静默忽略（不报错） | engine.py:156-157 | test_join_success_then_repeat_join_is_silent | 同一玩家再次 `/join` | 返回空消息，无重复入座 | 已通过 | - | 2026-09-01 |
| Q-009 | GameCommands.cs:44-74 + UpdateHandler.cs:428-432 | `/join` 带 @提及/空格 | 群命令 | Command 解析去除 @ 与多余空格后命中同一流程 | application/commands.py:150-151 | test_join_with_bot_mention_and_extra_spaces_still_joins | `<@!bot-openid-1>   /join   `（群 u2） | “乙 加入游戏” | 已通过 | - | 2026-09-01 |
| Q-010 | Werewolf.cs:689-693 | AddPlayer | 对局已开始（!IsJoining） | 静默忽略 | engine.py:154-155 | test_join_during_running_game_is_silent | 夜晚对局中 `/join`（u9） | 返回空消息，未入座 | 已通过 | - | 2026-09-01 |
| Q-011 | GameCommands.cs:50-55 | `/join` 私聊 | 私聊 | JoinFromGroup：请在群里加入 | service.py:218-219 | test_join_in_private_rejected | `/join`（C2C u1） | “该命令请在群里发送” | 已通过 | - | 2026-09-01 |
| Q-012 | Werewolf.cs:731-735 | AddPlayer | 人数已满 | PlayerLimitReached：拒绝 | engine.py:163-164 | test_join_when_room_full_rejected | 5 人满房 `/join`（u9） | “房间人数已满” | 已通过 | - | 2026-09-01 |
| Q-013 | GameCommands.cs:76-102 | `/forcestart` 群命令 | 无局 | NoGame：无局提示 | service.py:214-216 | test_forcestart_without_room_guides_startgame | `/forcestart`（群 admin） | 输出含 /startgame 引导 | 已通过 | - | 2026-09-01 |
| Q-014 | UpdateHandler.cs:433-440 | `/forcestart` 群命令 | 非管理员 | GroupAdminOnly：拒绝 | service.py:231-234 | test_forcestart_non_admin_rejected | 群 u1 `/forcestart`（admin_user_ids 仅 admin） | “只有管理员可以强制开始” | 已通过 | - | 2026-09-01 |
| Q-015 | Werewolf.cs:2517-2520 | ForceStart | 管理员触发 | KillTimer=true：结束等待计时，随后按人数开局 | engine.py:256-261 | test_forcestart_admin_ends_waiting_and_game_starts_on_timer | admin `/forcestart`（5 人）后 process_due_rooms | “等待时间已结束”；timer 后 phase=NIGHT | 已通过 | - | 2026-09-01 |
| Q-016 | Werewolf.cs:2517-2520 + Werewolf.cs:637-640 | ForceStart | 管理员触发但人数不足 | 等待结束后人数不足即取消本局 | engine.py:2799-2804 | test_forcestart_admin_with_too_few_players_cancels_like_official | 3 人房 admin `/forcestart` 后 timer | 房间取消 | 已通过 | - | 2026-09-01 |
| Q-017 | GameCommands.cs:104-119 | `/players` 群命令 | 有局 | 展示玩家与进度 | service.py:246-248 | test_players_reports_roster_in_group | `/players`（群 u1，5 人局） | “当前阶段”“玩家u1” | 已通过 | - | 2026-09-01 |
| Q-018 | GameCommands.cs:104-119 | `/players` 群命令 | 无局 | NoGame：无局提示 | service.py:214-216 | test_players_without_room_guides_startgame | `/players`（群 u1 无局） | 输出含 /startgame 引导 | 已通过 | - | 2026-09-01 |
| Q-019 | GameCommands.cs:121-151 + Werewolf.cs:863-868 | `/flee` 群命令 | 群在入场期，玩家在局 | RemovePlayer 离开等待并重新编号 | engine.py:212-218 | test_flee_lobby_removes_player_and_reseats | `/flee`（u2，5 人局） | “已退出等待中的游戏”，座位重新编号 | 已通过 | - | 2026-09-01 |
| Q-020 | GameCommands.cs:135-146 | `/flee` 群命令 | 本群有局但玩家不在局 | 无输出 | engine.py:209-211 | test_flee_when_not_seated_is_silent | `/flee`（u9，5 人局） | 返回空消息 | 已通过 | - | 2026-09-01 |
| Q-021 | GameCommands.cs:147-150 | `/flee` 群命令 | 无局 | NoGame：无局提示 | service.py:214-216 | test_flee_without_room_guides_startgame | `/flee`（群 u1 无局） | 输出含 /startgame 引导 | 已通过 | - | 2026-09-01 |
| Q-022 | Werewolf.cs:849-862 | RemovePlayer（flee 进行中） | 对局进行中 | 玩家死亡（Flee）并公告 | engine.py:220-230 | test_flee_running_game_kills_player | 夜晚局 `/flee`（u3 存活平民） | “已退出游戏”；died_by_flee_or_idle=True | 已通过 | - | 2026-09-01 |
| Q-023 | Werewolf.cs:842-846 | RemovePlayer | 玩家已死 | DeadFlee：拒绝 | engine.py:221-222 | test_flee_dead_player_rejected | 夜晚局 dead u3 `/flee` | “你已经出局” | 已通过 | - | 2026-09-01 |
| Q-024 | Werewolf.cs:835-839 | RemovePlayer（禁逃跑） | 入场期 | AllowFlee 只限制进行中；入场期仍可退出 | engine.py:207-208 | test_flee_lobby_with_flee_disabled_still_removes_player | allow_flee=False 入场局 `/flee`（u2） | 静默返回空，玩家未移除 | 已差异 | Q-DIFF-02 | 2026-09-01 |
| Q-025 | Werewolf.cs:835-839 | RemovePlayer（禁逃跑） | 进行中 | FleeDisabled 公告 | engine.py:207-208 | test_flee_disabled_running_game_announces_flee_disabled | allow_flee=False 夜晚局 `/flee`（u3） | 静默返回空，玩家存活 | 已差异 | Q-DIFF-02 | 2026-09-01 |
| Q-026 | GameCommands.cs:153-187 + Werewolf.cs:2521-2536 | `/extend` 群命令 | 群开延长、玩家可延长 | MaxExtend 截断；只可延长一次 | engine.py:232-254 | test_extend_success_clamped_then_repeat_rejected | `/extend 999` 后 `/extend 10`（u1, max_extend=60） | “延长 60 秒”；再次“已经延长过一次” | 已通过 | - | 2026-09-01 |
| Q-027 | GameCommands.cs:165-166 | `/extend` 群命令 | 玩家不在局 | NotPlaying：拒绝 | engine.py:237-239 | test_extend_by_non_player_rejected | 5 人局 `/extend 30`（u9） | “你不在当前房间” | 已通过 | - | 2026-09-01 |
| Q-028 | GameCommands.cs:170-171 | `/extend` 群命令 | 负数且非管理员 | GroupAdminOnly：拒绝 | engine.py:241-242 | test_extend_negative_by_player_requires_admin | `/extend -10`（u1） | “只有管理员可以缩短等待时间” | 已通过 | - | 2026-09-01 |
| Q-029 | GameCommands.cs:176-183 | `/extend` 群命令 | 群未开 AllowExtend | 普通玩家不可延长 | engine.py:235-236 | test_extend_without_allow_extend_flag_rejected_for_players | allow_extend=False `/extend 30`（u1） | “本群未开启玩家延长等待时间” | 已通过 | - | 2026-09-01 |
| Q-030 | GameCommands.cs:169 | `/extend` 群命令 | 参数非数字 | int.TryParse 失败按默认 30 秒 | service.py:242-244 | test_extend_non_numeric_argument_defaults_to_30_seconds | `/extend abc`（u1） | “延长时间请输入秒数”（报错） | 已差异 | Q-DIFF-03 | 2026-09-01 |
| Q-031 | Werewolf.cs:2524-2534 | ExtendTime | 管理员但不在局 | p==null 静默无效，不改变等待时间 | engine.py:237-238 | test_extend_by_admin_not_seated_is_noop | allow_extend=False `/extend 30`（admin 不在局） | 等待时间被延长 | 已差异 | Q-DIFF-04 | 2026-09-01 |
| Q-032 | GeneralCommands.cs:390-433 | `/nextgame` 群命令 | 群内 | 加入 NotifyGame 等待名单并私聊确认，不建局 | service.py:149-150 | test_nextgame_subscribes_wait_list_instead_of_creating_room | `/nextgame`（群 u1 无局） | 被映射为建局（房间创建） | 已差异 | Q-DIFF-05 | 2026-09-01 |
| Q-033 | GameCommands.cs:189-218 | `/stopwaiting` | 群内/等待名单 | DeletedFromWaitList 发到用户私聊 | service.py:178-179 | test_stopwaiting_confirms_in_private_chat | `/stopwaiting`（群 u1） | “已停止接收本群的等待通知”回在群里 | 已差异 | Q-DIFF-06 | 2026-09-01 |
| Q-034 | Werewolf.cs:4950-4966 + HandleReply:930-933 | 白天投票（/vote） | 投票期 | 官方回调选人后 CurrentQuestion 清空，票固定 | service.py:249-254 / engine.py:2411-2438 | test_vote_success_and_no_revote | `/vote 2` 后 `/vote 3`（u1） | “投票已记录”；再次“已经投过票”，票未变 | 已通过 | - | 2026-09-01 |
| Q-035 | UpdateHandler.cs:336-372 | 投票非法目标 | 投票期 | 菜单无非法目标；文字需拦截并提示座位号 | engine.py:130-140 | test_vote_invalid_seat_prompts_seat_usage | `/vote 99`（u1） | “找不到目标座位” | 已通过 | - | 2026-09-01 |
| Q-036 | Werewolf.cs:4950-4966 | 投票无目标 | 投票期 | 无“空投票”入口，未选择不算投票 | engine.py:2419-2422 | test_vote_without_target_does_not_consume_the_vote | `/vote`（u1）再 `/vote 2` | `/vote` 被当作弃票记录，随后再投被拒 | 已差异 | Q-DIFF-07 | 2026-09-01 |
| Q-037 | Werewolf.cs:4950-4966 | 投票自己 | 投票期 | 候选列表排除自己 | engine.py:139-140 | test_vote_self_rejected_like_official_menu | `/vote 1`（u1=座位1） | “不能选择自己” | 已通过 | - | 2026-09-01 |
| Q-038 | Werewolf.cs:4950-4966 | 投票（死人） | 投票期 | 只给存活玩家发菜单 | engine.py:2415-2416 | test_vote_by_dead_player_rejected | 死亡 u1 `/vote 2` | “出局玩家不能投票” | 已通过 | - | 2026-09-01 |
| Q-039 | handle_reply 阶段检查 | 投票错误阶段 | 非投票期 | 无投票菜单 | engine.py:2412-2413 | test_vote_wrong_phase_rejected | 夜晚局 `/vote 2`（u3） | “现在不是投票阶段” | 已通过 | - | 2026-09-01 |
| Q-040 | 见总表分片 P-008 | 弃票（/vote abstain、弃票、跳过、/abstain） | 投票期 | 淘汰主循环按闲置计（弃票等价于不投） | engine.py:2420-2422 | test_abstain_and_skip_record_non_vote | `弃票`/`跳过`/`/vote abstain`（u1/u2/u3） | “投票已记录”，votes 记录为 None | 已通过 | - | 2026-09-01 |
| Q-041 | application/commands.py:13-146（别名表缺失） | `/abstain` | 投票期 | QQ 版无此入口 | commands.py:149-168 | test_bare_abstain_slash_command_falls_back_to_help | `/abstain`（群 u1） | “未识别命令 + /startgame 引导”（提示层差异，见展示差异） | 已通过 | - | 2026-09-01 |
| Q-042 | Werewolf.cs:4950-4966 | 改票 | 投票期 | 官方无改票回调 | engine.py:2417-2418, service.py:280-281 | test_change_vote_rejected_like_official | `/vote 2` 后 `改票 3` | “官方规则投票后不可改票” | 已通过 | - | 2026-09-01 |
| Q-043 | HandleReply:995-1156 | 夜间私聊（/查验） | 夜晚 | 回调只记录一次；再点无效且结果不变 | engine.py:517-592 | test_night_seer_private_action_success_and_repeat_rejected | C2C `/查验 3` 后 `/查验 4`（u1 预言家） | “行动已记录”，target 不变；再次“该夜间行动已经提交” | 已通过 | - | 2026-09-01 |
| Q-044 | Werewolf.cs SendNightActions | 夜间行动在群里 | 夜晚 | 只在 PM 菜单发起 | service.py:259-260 | test_night_action_in_group_redirected_to_private | 群 `/查验 3`（u1） | “夜间行动请私聊机器人”，未记录 | 已通过 | - | 2026-09-01 |
| Q-045 | HandleReply | 夜间行动错误阶段 | 白天 | 无夜间按钮 | engine.py:532-533 | test_night_action_wrong_phase_rejected | 白天局 C2C `/查验 2` | “现在不是夜晚行动阶段” | 已通过 | - | 2026-09-01 |
| Q-046 | GameCommands.cs:20-30 | 夜间行动无房 | 无局 | 无局时需先建局 | service.py:214-216 | test_night_action_without_room_rejected | C2C `/查验 2`（无局） | “先发送 /startgame”引导 | 已通过 | - | 2026-09-01 |
| Q-047 | Werewolf.cs SendMenu + HandleReply | 夜间无目标（step_action 文字化） | 夜晚 | 列出候选，回座位号，确认提交 | service.py:296-388 | test_night_action_without_target_opens_text_menu_and_confirms | C2C `/查验`→`3`→`确认` | “请选择第 1/1 个目标”→“已选择 3号”→“行动已记录” | 已通过 | - | 2026-09-01 |
| Q-048 | 官方按钮无非法目标等价 | 夜间非法座位 | 夜晚 | 拦截并重新给列表 | service.py:376-377 | test_night_menu_rejects_invalid_seat_and_reprompts | `/查验`→`99` | “这个座位不能选”+重复提示列表 | 已通过 | - | 2026-09-01 |
| Q-049 | Werewolf.cs:1031-1058 | 狼人袭击狼人 | 夜晚 | 菜单不含狼人目标 | engine.py:691-692 | test_wolf_cannot_target_wolf_via_command | C2C `/狼人 2`（u1 狼，u2 狼） | “狼人不能袭击狼人” | 已通过 | - | 2026-09-01 |
| Q-050 | Werewolf.cs:2537-2700 | 白天开枪 | 白天 | 回调记录；只一次 | engine.py:2331,2372-2385 | test_day_shoot_success_and_repeat_rejected | C2C `/开枪 2` 后 `/开枪 3`（u1 枪手） | “开枪目标已记录”；再次“该白天行动已经提交” | 已通过 | - | 2026-09-01 |
| Q-051 | Werewolf.cs SendNightActions | 白天能力在群里 | 白天 | 只在 PM 菜单 | service.py:271-274 | test_day_ability_in_group_redirected_to_private | 群 `/开枪 2` | “私聊”引导 | 已通过 | - | 2026-09-01 |
| Q-052 | HandleReply:899-909 | 市长 reveal（确认式） | 白天/投票 | Reveal 按钮 → 文字“确认” | service.py:304-389 | test_mayor_confirm_only_flow_via_text | C2C `/mayor`→`确认` | “发送确认使用”→“公开市长身份”，vote_weight=2 | 已通过 | - | 2026-09-01 |
| Q-053 | UpdateHandler.cs:336-372 | 未识别输入 | 任何 | Telegram 静默忽略非命令；QQ 需引导可用斜杠指令 | service.py:146-147 | test_unknown_group_text_returns_command_help | 群“随便说点什么” | “未识别命令”+含 /startgame 帮助 | 已通过 | - | 2026-09-01 |
| Q-054 | UpdateHandler.cs:336-372 | 仅 @提及机器人 | 群 | 无命令则引导 | service.py:146-147,150-154 | test_mention_only_message_returns_help | `<@!bot-openid-1>` | 含 /startgame 帮助 | 已通过 | - | 2026-09-01 |
| Q-055 | HandleReply | 身份查询（/身份） | 私聊 | 私聊身份值 | service.py:202-207 | test_identity_query_in_private_returns_role | C2C `/身份`（u1 预言家） | “你的身份是【Seer】”（英文枚举值，中文化见展示差异） | 已通过 | - | 2026-09-01 |
| Q-056 | application/commands.py:150-151 | 重复空格/多个@提及 | 群 | 解析稳定命中同一条命令 | commands.py:149-168 | test_parse_command_tolerates_repeated_spaces_and_mentions | `<@!b> <@!b2> /players`、`/vote    3` | 均正确解析为对应 command | 已通过 | - | 2026-09-01 |
| Q-057 | 事件去重（application/service.py:83-89） | 同一事件号重推 | 群 | Telegram 不会重复；QQ 需幂等 | service.py:83-102 | test_startgame_duplicate_event_id_replays_cached_result | 同一 `fixed-start-1` 事件 `/startgame` 两次 | 第二次返回缓存，只提交一次 | 已通过 | - | 2026-09-01 |

## 计数

| 总条目 | 已通过 | 已差异 | 已确认未调用 | 未决 |
| ---: | ---: | ---: | ---: | ---: |
| 57 | 50 | 7 | 0 | 0 |

（Q-DIFF-02 由两条测试覆盖，计入一条差异项；条目数按差异编号去重：Q-DIFF-01..07 共 7 条。）

## 差异编号

| 编号 | 描述 | 固定输入 | 测试（`-k` 重跑） |
| --- | --- | --- | --- |
| Q-DIFF-01 | 入场期再 `/startgame` 官方重发加入入口；Python 报“当前群已有活动房间” | 同群建局后 u2 `/startgame` | test_startgame_during_lobby_reprompts_join_instead_of_error |
| Q-DIFF-02 | 官方 AllowFlee 只限制进行中：入场期仍可退出、进行中公告 FleeDisabled；Python 在 allow_flee=False 时两处都静默 | allow_flee=False 的入场局/夜晚局 `/flee` | test_flee_lobby_with_flee_disabled_still_removes_player；test_flee_disabled_running_game_announces_flee_disabled |
| Q-DIFF-03 | 官方 `/extend` 参数非数字按默认 30 秒；Python 报“延长时间请输入秒数” | `/extend abc` | test_extend_non_numeric_argument_defaults_to_30_seconds |
| Q-DIFF-04 | 官方 ExtendTime 只对局内玩家生效，局外管理员静默无效；Python 允许局外管理员直接延长 | allow_extend=False `/extend 30`（admin 不在局） | test_extend_by_admin_not_seated_is_noop |
| Q-DIFF-05 | 官方 `/nextgame` 只加入 NotifyGame 等待名单，不建局；Python 把它映射为建局 | 无局群 `/nextgame` | test_nextgame_subscribes_wait_list_instead_of_creating_room |
| Q-DIFF-06 | 官方 `/stopwaiting` 确认发到用户私聊；Python 回在群里且无真实等待名单 | `/stopwaiting` | test_stopwaiting_confirms_in_private_chat |
| Q-DIFF-07 | 官方处决无“空投票”入口，未选择不算投票；Python 把无目标 `/vote` 当弃票记录，随后再投被拒 | `/vote` 再 `/vote 2` | test_vote_without_target_does_not_consume_the_vote |

## 展示差异（按钮→文字指令对应；不计入玩法差异）

Telegram 按钮/动图/语言包/成就/QQ 菜单外观不在本分片计入玩法差异。仅记录 QQ 文字入口与官方按钮操作的一一对应，供交付展示层使用：

| 官方交互 | 官方载体 | QQ 文字入口 | Python 命令 | 说明 |
| --- | --- | --- | --- | --- |
| `/startgame` | 群命令 | `/startgame` / `开始游戏` / `开始` | create | 面板“开始一局狼人杀” |
| `/startchaos` | 群命令 | `/startchaos` / `混乱开始` | create_chaos | 面板“混乱模式开局” |
| `/join` | 群命令 + JoinByButton 按钮 | `/join` / `加入` / `参加` | join | 面板“加入当前对局” |
| `/forcestart` | 群命令（仅管理员） | `/forcestart` / `强制开始` / `go` | force_start | 仅管理员；QQ 版无“启动对局”按钮 |
| `/players` | 群命令 | `/players` / `players` / `状态` | status | 面板“查看玩家和进度” |
| `/flee` | 群命令 | `/flee` / `逃跑` | flee | |
| `/extend` | 群命令 | `/extend` / `延长` | extend | |
| `/stopwaiting` | 群命令 + 取消按钮 | `/stopwaiting` / `停止等待` | stop_waiting | |
| `/nextgame` | 群命令 | `/nextgame` | nextgame | 面板“准备下一局”；行为差异见 Q-DIFF-05 |
| 处决选人菜单 | 内联按钮 | `弃票` `跳过` `/vote abstain` | vote（推断为 None） | `空投`语义差异见 Q-DIFF-07 |
| `/abstain` | 无（官方弃票靠内联“跳过”） | `/abstain` | 未映射（落回帮助/`弃票`） | 仅提示层差异，见 Q-041 |
| 夜间/白天能力按钮 | 内联按钮（PM） | `/狼人` `/查验` `/守护` `/开枪` 等 + 座位号 + `确认` `重选` `取消` | 各 role 指令 + step_action | C2C/DIRECT 私聊 |
| 身份展示 | 私聊文本 | `/身份` | identity | 角色名当前显示官方英文枚举值（Seer 而非 预言家），中文化待展示层 |
| 面板指令集 | QQ 命令面板 | `/help` `/stats` `/rolelist` `/config` `/ping` 等 | 见面板 _GROUP_PANEL_ITEMS | 仅外观 |

## 复查说明

- 全量 `python -m pytest -q` → `286 passed + 8 xfailed（strict）`，无失败、无跳过。
- 分片 `python -m pytest tests/test_audit_qq_commands.py -q` → `48 passed + 8 xfailed（strict）`。
- 未改动 `domain/`、`application/`、`adapters/`、`infrastructure/` 及 `test_official_parity.py`、`test_parity.py`、`test_adapter_and_commands.py`、`conftest.py`、`docs/official-branch-ledger.md`。
