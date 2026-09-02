from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from aiohttp import web

from adapters.qq.adapter import QQBotAdapter
from adapters.web.dashboard import setup_dashboard
from application.service import GameApplication
from infrastructure.config import Settings
from infrastructure.db import PostgreSQLStore
from infrastructure.logging import configure_logging


ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


async def _health_handler(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    healthy = await runtime.store.health()
    payload = {
        "status": "ok" if healthy else "degraded",
        "database": healthy,
        "gateway": runtime.adapter.is_ready,
    }
    return web.json_response(payload, status=200 if healthy else 503)


async def _ready_handler(request: web.Request) -> web.Response:
    runtime = request.app["runtime"]
    database = await runtime.store.health()
    queue = await runtime.store.pending_delivery_count()
    ready = database and runtime.adapter.is_ready and queue <= runtime.queue_limit
    payload = {
        "status": "ready" if ready else "not_ready",
        "database": database,
        "gateway": runtime.adapter.is_ready,
        "pending_deliveries": queue,
    }
    return web.json_response(payload, status=200 if ready else 503)


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = PostgreSQLStore(settings.database_url)
        self.application = GameApplication(
            self.store,
            rules=settings.rules,
            admin_user_ids=settings.admin_user_ids,
            dev_user_ids=settings.dev_user_ids,
        )
        self.adapter = QQBotAdapter(
            settings.app_id, settings.app_secret, self.application, store=self.store
        )
        self.queue_limit = 1000
        self._stop = asyncio.Event()

    async def delivery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.application.process_deliveries(
                    self.adapter, self.settings.delivery_max_attempts
                )
            except Exception:
                log.exception("投递工作协程异常")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.delivery_poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def timer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.application.process_due_rooms()
            except Exception:
                log.exception("定时器工作协程异常")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def serve(self) -> None:
        await self.store.initialize()
        web_app = web.Application()
        web_app["runtime"] = self
        web_app.router.add_get("/healthz", _health_handler)
        web_app.router.add_get("/readyz", _ready_handler)
        # 后台实时观测页（只读）：/dashboard 看盘，/api/observe 取快照，/ws/observe 实时推送。
        setup_dashboard(web_app)
        runner = web.AppRunner(web_app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.settings.host, self.settings.port)
        await site.start()
        log.info(
            "服务已启动",
            extra={"session_id": self.settings.app_id_masked},
        )
        log.info(
            "后台实时观测页地址：http://%s:%s/dashboard",
            self.settings.host,
            self.settings.port,
        )
        tasks = [asyncio.create_task(self.delivery_loop()), asyncio.create_task(self.timer_loop())]
        try:
            await self.adapter.run()
        finally:
            self._stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.adapter.close()
            await runner.cleanup()
            await self.store.close()


async def migrate(settings: Settings) -> None:
    store = PostgreSQLStore(settings.database_url)
    try:
        await store.migrate(ROOT / "migrations")
    finally:
        await store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="狼人杀 QQ 官方机器人")
    parser.add_argument("command", nargs="?", choices=("run", "migrate"), default="run")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if args.command == "migrate":
        asyncio.run(migrate(settings))
    else:
        asyncio.run(Runtime(settings).serve())


if __name__ == "__main__":
    main()
