# Linux 运行、备份与回滚

官方 Python 服务只使用 PostgreSQL 和 QQ 官方 Gateway WebSocket。凭据必须通过环境变量或受限 `EnvironmentFile` 注入，不得写入镜像或仓库。

## 最小命令

```bash
cd /opt/werewolf-python
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a && . /etc/werewolf/werewolf.env && set +a
python -m main migrate
python -m main run
```

健康检查：

```bash
curl -fsS http://127.0.0.1:18100/healthz
curl -fsS http://127.0.0.1:18100/readyz
```

官方 parity fixture：

```bash
python tests/run_fixture.py tests/fixtures/example-lifecycle.json
python -m pytest tests/test_parity.py
```

## systemd

`deploy/werewolf-qqbot.service` 以非 root 用户运行。发布前确认 `EnvironmentFile=-/etc/werewolf/werewolf.env` 权限为 `0600`。

## Docker

```bash
cp .env.example .env
# 编辑 .env，填入 APP_ID、APP_SECRET 和可选的 ADMIN_USER_IDS
docker compose -f deploy/docker-compose.yml up -d --build
```

容器以非 root 启动，等待 PostgreSQL 正常后自动执行数据库升级，再启动机器人。

## 发布

1. `pg_dump "$DATABASE_URL" -Fc -f /var/backups/werewolf/pre-release.dump`
2. 校验 `DATABASE_URL`、`APP_ID`、`APP_SECRET` 存在且不是仓库中的历史值。
3. 安装新版本代码，执行 `python -m main migrate`。
4. 启动服务并等待 `/readyz` 成功。
5. 用测试群跑一局：建局、私聊行动、投票、Gateway 断线恢复。

## 回滚

1. 停止新版本：`systemctl stop werewolf-qqbot` 或停止容器。
2. 恢复上一版本代码和 `/etc/werewolf/werewolf.env`。
3. 如 schema 不兼容：`pg_restore --clean --if-exists -d "$DATABASE_URL" /var/backups/werewolf/pre-release.dump`
4. 启动上一版本并确认 `/readyz`。
5. 保留失败日志和原备份，不要覆盖。

## 密钥

历史上在聊天中出现过的 AppSecret 必须在正式接入前轮换。新密钥只通过环境注入，不写入 git、镜像层或普通日志。
