## Why

当前 QQ 版已经具备官方 Telegram 版的大部分核心身份和胜负判断，但操作方式、阶段提示、部分多步骤能力、群组配置和辅助命令仍有差异。需要把官方 Telegram 源码作为唯一对照，补齐这些差异，让玩家在 QQ 中得到同样的规则结果和完整游玩流程。

## What Changes

- 逐项对齐官方 Telegram 版的建局、入场、开局、夜晚、白天、投票、结算和重新开局流程。
- 补齐所有官方身份的行动提示、可用条件、目标选择、行动顺序、失败结果和身份变化。
- 在 QQ 中提供与官方流程相匹配的群聊指令、私聊指令和分步选择方式，覆盖按钮操作无法直接照搬的场景。
- 补齐官方辅助命令，包括玩家列表、身份列表、群配置相关命令、状态、版本和帮助信息中实际支持的内容。
- 支持按群保存官方规则配置，包括身份池、计时、投票、死亡身份显示和特殊身份开关。
- 统一官方角色名称、阶段通知、行动反馈、死亡原因、胜利条件和结算内容。
- 增加基于官方源码的规则对照测试，覆盖每个身份和关键分支；未对齐的行为不得标记为完成。
- 保留 QQ Gateway、Docker、PostgreSQL 和现有数据，不改变已经可用的连接方式。

## Capabilities

### New Capabilities

- `official-game-flow`: 完整官方游戏阶段、身份行动、投票和结算流程。
- `qq-official-interaction`: QQ 群聊、私聊、指令菜单和分步行动交互。
- `group-rule-configuration`: 按群保存和使用官方规则配置。
- `official-command-set`: 官方辅助命令、帮助内容和命令参数。

### Modified Capabilities

无。当前项目尚未建立可复用的主规格文件，本次以新能力规格建立完整契约。

## Impact

- 主要影响 `domain/`、`application/`、`adapters/qq/`、`infrastructure/` 和 `tests/`。
- 可能增加数据库迁移，用于保存群级配置和必要的阶段状态。
- 可能调整 QQ 指令面板和消息发送方式，但保留现有 App 配置和 Docker 启动方式。
- 官方 Telegram 源码 `work/upstream-official/Werewolf for Telegram` 继续作为规则对照来源，不直接运行其中的 Telegram 组件。
