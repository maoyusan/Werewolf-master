# 官方规则 QQ 狼人杀

这个版本把官方 `Werewolf for Telegram` 的角色、夜晚行动、白天讨论、投票、死亡连锁和胜负判断搬到了 Python，并接入 QQ 官方机器人。

## 本地 Docker 启动

1. 复制 `.env.example` 为 `.env`。
2. 在 `.env` 填入 QQ 机器人的 `APP_ID`、`APP_SECRET`。也可以填写 `ADMIN_USER_IDS`，多个用户 ID 用逗号分开。
3. 在本目录运行：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
```

查看是否启动：

```bash
curl http://127.0.0.1:18100/healthz
```

停止：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml down
```

数据库数据会保存在 Docker 数据卷中，删除容器不会丢失。

## QQ 中怎么用

群里发送 `/startgame` 建普通局，发送 `/startchaos` 建混乱局；玩家发送 `/join` 加入。

人数达到下限后，**本局发起人直接发送 `/go` 或再发一次 `/startgame` 就能立刻开局，不需要管理员权限**。身份和夜晚行动会私聊发给玩家，白天投票在群里发送，例如 `/vote 3` 或 `/弃票`。

常用命令：`/status`、`/flee`、`/extend 30`、`/stopwaiting`、`/身份`、`/结算`。管理员 ID 需要提前填入 `ADMIN_USER_IDS`。

完整玩法、逐角色行动指令和流程说明见 [docs/游玩教程.md](docs/游玩教程.md)。

## 后台运行检测台

服务启动后打开 `http://127.0.0.1:18100/dashboard`。这是只读的运行检测台：左侧是品牌与分区导航，右侧是连接态、服务健康、总览指标、对局诊断卡、流程日志和异常投递。页面通过 WebSocket 实时刷新，失败后会降级轮询，用来判断对局卡在等人、定时器没跑，还是消息没发出去。

## 本地检查

```bash
python -m pytest -q
python -m compileall -q adapters application domain infrastructure main.py
```

真实 QQ 对局由你启动后自行测试；本地检查不会连接 QQ，也不会替你发送消息。
