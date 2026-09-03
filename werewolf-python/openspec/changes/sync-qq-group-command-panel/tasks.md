## 1. 面板成员与说明

- [x] 1.1 按设计更新 `adapters/qq/adapter.py` 中 `_GROUP_PANEL_ITEMS`：去掉 `/players`；增加 `/flee` `/extend` `/link` `/bindqq` `/whoami`；总数 ≤20；顺序按大厅 → 局中 → 身份 → 查询 → 杂项；确认每条都有非空中文 `desc`（参数指令写明需跟号码/秒数等）
- [x] 1.2 确认每条面板 `name` 经 `parse_command` 都能落到已知命令（不是 `unknown`）；用小段断言循环或单测验证

## 2. 解析与用法修复

- [x] 2.1 在 `application/commands.py` 的 `_ALIASES` 增加 `"abstain": "abstain"`；验证 `parse_command("/abstain").name == "abstain"`，且现有 `/弃票` 仍映射到 `abstain`
- [x] 2.2 确保投票阶段对命令名 `abstain` 的处理与中文「弃票」路径一致；用聚焦测试验证投票期 `/abstain` 不会落到未识别命令帮助
- [x] 2.3 确保无参 `/extend` 返回包含 `/extend` 与秒数示例的短用法回复；验证无参 `/bindqq` 仍返回既有用法文案

## 3. 帮助与教程对齐

- [x] 3.1 对照最终面板列表抽查 `HELP_TEXT`；仅在高优先级项缺失时补一句（不要发明新指令）
- [x] 3.2 若 `docs/游玩教程.md` 指令表与面板成员矛盾（例如仍暗示 `/players` 是独立面板项），改文案与之对齐

## 4. 测试与校验

- [x] 4.1 扩展或新增适配器面板测试：载荷包含 `/link` `/bindqq` `/flee` `/extend`；不含 `/players`；每条有 `desc` 且 `only_admin` 为 False
- [x] 4.2 运行 `python -m pytest tests/test_adapter_and_commands.py tests/test_openid_link.py tests/test_audit_qq_commands.py -q`，修到全绿
- [x] 4.3 若本地/CI 习惯使用则运行 `openspec validate sync-qq-group-command-panel`；否则在 apply 总结里注明已跳过
