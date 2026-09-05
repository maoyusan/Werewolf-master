# 官方规则 QQ 狼人杀

这个版本把官方 `Werewolf for Telegram` 的角色、夜晚行动、白天讨论、投票、死亡连锁和胜负判断搬到了 Python，并通过 **NapCat（OneBot 11）** 接入 QQ。机器人就是一个普通 QQ 号，玩家不需要绑定、不需要 @ 机器人。

## 前置：先跑起来 NapCat

1. 部署 NapCat 并登录一个专门给机器人用的 QQ 号。
2. 在 NapCat 里开启 **正向 WebSocket** 服务（默认端口 3001），可选地设置 access token。
3. 把机器人 QQ 拉进游戏群。**建议设为群管理员**：只有具备管理员/群主权限时，机器人才能自动把玩家群名片改成「1号」「2号」，并在出局时改成「N号（已出局）」。不给管理员权限也能玩，只是号码要靠群内消息自己记。

## 本地 Docker 启动

1. 复制 `.env.example` 为 `.env`。
2. 在 `.env` 填入 `NAPCAT_WS_URL`（例如 `ws://127.0.0.1:3001`），NapCat 配了 token 就再填 `NAPCAT_ACCESS_TOKEN`。也可以填写 `ADMIN_USER_IDS`，多个 QQ 号用逗号分开。
   容器内的 `127.0.0.1` 指向容器自己，NapCat 跑在宿主机时请改成 `ws://host.docker.internal:3001` 或宿主机内网 IP。
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

群里发送 `/startgame` 建普通局，发送 `/startchaos` 建混乱局；玩家发送 `/join` 加入。指令直接发在群里即可，**不用 @ 机器人**，习惯性 @ 一下也不影响识别。

人数达到下限后，**本局发起人直接发送 `/go` 或再发一次 `/startgame` 就能立刻开局，不需要管理员权限**。身份、阵营、胜利目标和夜间可用操作会一次性私聊发给玩家，夜晚行动私聊进行，白天投票在群里发送，例如 `/vote 3` 或 `/弃票`。投票阶段群里会公示当前存活玩家名单（「1号 沉潜、2号 …」）。

玩家出局后本局内不再受理其任何操作指令，机器人也会把其群名片标成「（已出局）」。

常用命令：`/status`、`/flee`、`/extend 30`、`/stopwaiting`、`/身份`、`/结算`。管理员 QQ 号需要提前填入 `ADMIN_USER_IDS`。

完整玩法、逐角色行动指令和流程说明见 [docs/游玩教程.md](docs/游玩教程.md)。

## 后台运行检测台

服务启动后打开 `http://127.0.0.1:18100/dashboard`。这是只读的运行检测台：左侧是品牌与分区导航，右侧是连接态、服务健康、总览指标、对局诊断卡、流程日志和异常投递。页面通过 WebSocket 实时刷新，失败后会降级轮询，用来判断对局卡在等人、定时器没跑，还是消息没发出去。

## 本地检查

```bash
python -m pytest -q
python -m compileall -q adapters application domain infrastructure main.py
```

真实 QQ 对局由你启动后自行测试；本地检查不会连接 QQ，也不会替你发送消息。
