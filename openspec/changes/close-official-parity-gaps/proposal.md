## 为什么要做

上一轮已经补齐了大部分角色和主流程，但对照清单仍有少数官方分支没有逐项验证。只要这些分支还没有和官方源码一一对应，就不能把“100%复刻”当作已经完成。本次任务专门收尾这些差异。

## 要改什么

- 逐项核对官方源码中的剩余规则，补齐 `RandomLynch`、`SecretLynch`、Augur 顺序、掘墓人公式和特殊残局。
- 补齐所有仍缺少的角色转换概率、访问死亡、狼人转换和二人/三人残局场景。
- 让每个修复都有独立的代码级测试，测试结果以官方源码为标准。
- 检查官方命令、阶段提示、死亡原因、结算信息和 QQ 文字入口是否覆盖；只记录必须人工在 QQ 确认的部分。
- 清理对照清单中的假完成项；存在未覆盖行为时保持未完成状态。

## 能力范围

### 新增能力

- `official-parity-completion`: 覆盖官方源码剩余规则、流程分支和特殊残局的逐项对照。
- `official-parity-evidence`: 为每条规则保存来源、输入、结果和测试记录，作为 100% 复刻的判断依据。

### 修改能力

- `official-game-flow`: 补齐随机处决、秘密投票、夜晚顺序、转换和残局判断要求。
- `official-command-set`: 补齐官方命令入口、参数、权限、阶段限制和错误提示要求。
- `group-rule-configuration`: 补齐官方开关及其在每局开始和进行中的生效规则。
- `qq-official-interaction`: 确认 QQ 文字指令与官方按钮流程的对应关系，不把 QQ 平台限制误判为玩法差异。

## 影响范围

- 主要代码：`werewolf-python/domain/engine.py`、`domain/rules.py`、`domain/models.py`、`application/service.py`、`application/commands.py`。
- 主要测试：`werewolf-python/tests/test_official_parity.py`、`tests/test_parity.py`、`tests/test_adapter_and_commands.py`。
- 主要文档：`werewolf-python/docs/official-parity-checklist.md`、`docs/official-parity-matrix.md`。
- 不更换 QQ Gateway、PostgreSQL 或 Docker；不运行真实 QQ 游戏流程。
