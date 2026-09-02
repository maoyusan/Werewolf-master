# 官方行为 Fixture

每个 JSON fixture 固定 `seed`、`clock`、`player_ids` 与操作序列，因此相同输入会输出完全相同的阶段、角色、存活状态、胜者和领域事件公开性/目标。

顶层字段：

- `players`：显示名列表；`player_ids` 可显式注入平台用户 ID，否则由 `player_id_prefix` 生成。
- `operations`：支持 `set_roles`、`set_state`、`set_statistics`、`night_action`、`resolve_night`、`day_action`、`start_vote`、`vote`、`resolve_vote`、`timeout`、`check_winners`、`refresh_identity` 和 `night_prompts`。
- `expected`：存在时，由 `OfficialFixtureRunner.assert_expected()` 比较完整输出。

运行单个 fixture：

```powershell
python tests/run_fixture.py tests/fixtures/example-lifecycle.json
```

跑全部官方分支：

```powershell
python -m pytest tests/test_official_parity.py tests/test_parity.py
```

程序化字段级断言在 `tests/test_official_parity.py`，对照清单见 `docs/official-parity-checklist.md`。
