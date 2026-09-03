## Context

动机见 `proposal.md`。当前 QQ 群斜杠菜单硬编码在 `adapters/qq/adapter.py` 的 `_GROUP_PANEL_ITEMS`（16 条）。应用层别名在 `application/commands.py`，帮助文案在 `HELP_TEXT`。启动时已调用 `ensure_group_command_panel()` 并用 `_panel_signature` 比对，因此改元组即可在下次就绪时刷新远端菜单，无需新同步管道。

QQ 面板条目数量有限（平台侧；软上限按约 20 条处理）。`/players` 与 `/status` 重复。`/bindqq` 缺参时已有用法 `GameRuleError`；`/extend` 与 `/abstain` 需小幅服务/解析修复。

## Goals / Non-Goals

**Goals:**

- 精选 `_GROUP_PANEL_ITEMS`，让关键 QQ 专用与局中指令可见，并带清晰中文 `desc`
- 保证每条面板名称都能经 `_ALIASES` 解析
- 无参 `/extend` 给出短用法回复（并保持无参 `/bindqq` 既有用法）
- 将 `/abstain` 映射为与 `/弃票` 相同
- 用测试覆盖面板成员与解析

**Non-Goals:**

- C2C/私信作用域的官方面板（仅群面板）
- 把管理/开发命令放进玩家菜单
- 改变 `/vote` 无目标弃票语义（见文档 Q-DIFF-07）
- 改变群内无参 `/link`「发码」行为
- 本变更不做 HELP 与面板的代码级单一数据源（手工对齐即可）

## Decisions

### 1. 面板成员（按优先级填满）

保留现有核心流程与查询；去掉 `/players`；补上高价值缺失项。

**纳入：**  
`/startgame` `/join` `/go` `/leave` `/cancel` `/status` `/vote` `/flee` `/extend` `/help` `/rolelist` `/stats` `/config` `/nextgame` `/link` `/bindqq` `/whoami` `/ping` `/startchaos` `/version`

**不上板（仍在帮助/教程）：**  
`/unbindqq`、`/changelog`、`/grouplist`、夜间/白天角色行动（私聊）、管理/开发命令

**理由：** 优先首局与局中动作，少用的解绑/更新说明不占菜单。目标 ≤20 条。  
**备选：** 再加 `/unbindqq` 与 `/changelog` → 易超平台限制并挤占菜单。

建议顺序（按使用频率）：大厅 → 局中 → QQ 身份 → 查询 → 杂项。

### 2. 用 desc 作为「选择时的说明」

QQ 在斜杠选择器里展示每条 `desc`。每条必须有简短中文用途（非空、非纯英文）。参数型指令在篇幅允许时把期望形式写进 `desc`，例如 `/bindqq` →「登记真实QQ号（需跟号码）」、`/extend` →「延长当前阶段（需跟秒数）」、`/link` →「开通私聊通道（领关联码）」。

**备选：** 每次点菜单都再发一条帮助气泡 → 对 `/join` 这类无参指令太吵。

### 3. 仅在需要处做无参用法提示

| 指令 | 无参行为 |
|------|----------|
| `/bindqq` | 保持既有用法错误 |
| `/extend` | 缺参时改为明确用法回复（避免含糊失败） |
| `/link` | 不变（群内发码） |
| `/vote` | 不变（弃票） |

### 4. 在 `_ALIASES` 修复 `/abstain`

增加 `"abstain": "abstain"`（保留 `"弃票": "abstain"`）。若处理逻辑已按 `abstain` / 弃票路径分发则无需改服务名——实现时核对，若目前仅中文路径生效则补上接线。

### 5. 测试

- 扩展 `test_creates_official_group_command_panel_once`（或并列用例）：断言必含名称、不含 `/players`、每条 `desc` 非空
- 解析测试：`parse_command("/abstain").name == "abstain"`
- 服务测试：无参 `/extend` 返回用法文案；无参 `/bindqq` 仍返回用法

## Risks / Trade-offs

- **[风险] 平台在条目数/`desc` 长度超未公开限制时拒绝面板** → 缓解：≤20 条；`desc` 保持短；POST/PUT 失败只记日志并继续跑（现有行为）
- **[风险] 远端面板缺 `panel_id` 卡住** → 缓解：现有告警路径；运维可能需在 QQ 控制台手工删除
- **[取舍] `/unbindqq` / `/changelog` 不上菜单** → 可通过 `/help` 发现；名额有余时可再上
- **[取舍] HELP 与面板手工同步** → 本变更接受；在任务清单记录最终列表

## Migration Plan

1. 发布更新后的 `_GROUP_PANEL_ITEMS` 与解析/服务修复
2. 重启机器人（或等网关重连），使 `on_ready` → `ensure_group_command_panel` 刷新远端菜单
3. 在测试群冒烟：斜杠菜单出现新条目与说明
4. 回滚：还原 `_GROUP_PANEL_ITEMS` 并重新部署；签名不一致会恢复旧菜单

## Open Questions

无待定问题；成员与无参规则已在上文固定，可直接实现。
