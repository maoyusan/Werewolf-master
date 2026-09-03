## Purpose

约定 QQ 群斜杠指令面板如何与已实现的玩家指令保持一致、在选择时展示清晰中文说明，以及无参选中参数型指令时如何给出简短用法提示。

## ADDED Requirements

### Requirement: 群指令面板列出已实现的玩家指令
系统必须发布一份 QQ 群指令面板，条目为应用层已接受的玩家向指令精选子集。面板除现有大厅与查询指令外，必须包含 `/link`、`/bindqq`、`/flee`、`/extend`。在已有 `/status` 的情况下，面板不得再单独列出 `/players`（二者解析为同一命令）。仅管理员与开发者命令不得出现在玩家面板上。

#### Scenario: 关键 QQ 入门指令出现在面板
- **WHEN** 机器人在网关就绪时确保群指令面板
- **THEN** 已发布的面板条目包含带非空中文 `desc` 的 `/link` 与 `/bindqq`

#### Scenario: 常用局中指令出现在面板
- **WHEN** 机器人在网关就绪时确保群指令面板
- **THEN** 已发布的面板条目包含带非空中文 `desc` 的 `/flee` 与 `/extend`

#### Scenario: 不出现重复的状态入口
- **WHEN** 机器人在网关就绪时确保群指令面板
- **THEN** 已发布的面板条目不包含 `/players`

### Requirement: 每条面板项都有选择说明
每条面板项必须将 `type` 设为 `command`、`only_admin` 设为 `false`，并提供非空中文 `desc`，用白话说明该指令用途，以便 QQ 客户端在斜杠菜单浏览或选中时展示。

#### Scenario: 选中菜单项时看到用途说明
- **WHEN** 用户打开本机器人的 QQ 群斜杠指令菜单
- **THEN** 每个列出的指令都附带其面板项 `desc` 中的中文说明

### Requirement: 无参的参数型指令回复用法提示
当玩家发送需要参数的面板指令（`/bindqq`、`/extend`）且未带参数时，系统必须回复简短用法提示，写明指令名与期望参数形式，不得静默丢弃。无目标的 `/vote` 保持既有弃票语义，不适用本用法提示规则。群内无参 `/link` 保持既有「发放关联码」行为。

#### Scenario: 无参 /bindqq 提示填写 QQ 号
- **WHEN** 玩家在群或私聊发送无参数的 `/bindqq`
- **THEN** 系统回复包含 `/bindqq` 与示例 QQ 号形式的用法文案

#### Scenario: 无参 /extend 提示填写秒数
- **WHEN** 玩家在可延长阶段的群内发送无参数的 `/extend`
- **THEN** 系统回复包含 `/extend` 与秒数示例的用法文案

### Requirement: 识别英文 /abstain 别名
命令解析器必须将 `/abstain`（大小写不敏感）映射为与 `/弃票` 相同的弃票动作，不得把无参 `/abstain` 当成未识别命令。

#### Scenario: /abstain 被正确识别
- **WHEN** 玩家在投票阶段于群内发送 `/abstain`
- **THEN** 系统按弃票处理，而不是返回未识别命令帮助

### Requirement: 面板签名变化时触发更新
当配置的面板条目列表（名称、说明或管理员标记）与远端备注为 `werewolf-python-group-panel` 的面板不一致时，机器人必须在下一次成功的 `ensure_group_command_panel` 运行中尝试更新或重建该面板，使玩家无需另做控制台手工操作即可看到新菜单。

#### Scenario: 条目变更后刷新远端面板
- **WHEN** `_GROUP_PANEL_ITEMS` 内容变更且机器人再次就绪
- **THEN** 适配器检测到签名不一致，并更新或重建远端群面板
