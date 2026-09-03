## Why

QQ 群指令面板（`_GROUP_PANEL_ITEMS`）与应用层已实现指令、`HELP_TEXT`、游玩教程不同步：玩家在输入框快捷菜单里看不到 `/link`、`/bindqq`、`/flee`、`/extend` 等关键指令；部分需参数的指令选中后也缺少用法说明。现在补齐面板并修好别名缺口，避免首局玩家卡在「无好友关系」或找不到常用入口。

## What Changes

- 同步群指令面板条目：补上已实现但未上板的玩家向指令（`/link`、`/bindqq`、`/unbindqq`、`/whoami`、`/flee`、`/extend`；`/changelog` 等按面板容量取舍）
- 去掉与 `/status` 重复的 `/players` 面板项，腾出名额给关键指令
- 每条面板项保留清晰中文 `desc`，用户在 QQ 指令菜单选择时能看到用途说明
- 需参数的指令（如 `/bindqq`、`/extend`、`/vote`、`/link`）在仅选中无参数时，回复简短用法说明，而不是含糊失败或静默
- 修复 `/abstain` 别名缺失（与 `/弃票` 对齐），避免英文斜杠写法落到「未识别命令」
- 更新面板同步相关测试；教程/帮助若与最终面板不一致则对齐文案

## Capabilities

### New Capabilities

- `qq-group-command-panel`：QQ 群指令面板与应用层指令同步、菜单说明文案、无参选中时的用法提示

### Modified Capabilities

- （无；仓库尚无已归档的主 specs）

## Impact

- `adapters/qq/adapter.py`：`_GROUP_PANEL_ITEMS` 与启动时 `ensure_group_command_panel` 同步逻辑（条目变更后应触发更新/重建）
- `application/commands.py`：补 `/abstain` 等别名
- `application/service.py`：无参时用法提示；必要时微调 `HELP_TEXT`
- `tests/test_adapter_and_commands.py`、相关 audit/openid 测试
- 可选：`docs/游玩教程.md` 与面板最终列表对齐
- 运行时：机器人重启/网关就绪后会按现有逻辑把面板推到 QQ 开放平台；已部署实例需重启或等下次 ready 才会刷新菜单
