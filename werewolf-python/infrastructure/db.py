from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import asyncpg

from domain.models import GamePhase, GameRoom, KillMethod


CURRENT_SCHEMA_VERSION = 10


class ConcurrentStateError(RuntimeError):
    """内存中的旧房间快照将会覆盖数据库里更新的版本时抛出。"""


def _session_type(value: str):
    from application.contracts import SessionType

    return SessionType(value)


def _parse_time(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _snapshot_value(value: Any) -> dict[str, Any]:
    """未注册 JSON 编解码器时，asyncpg 会把 JSONB 原样返回成文本。"""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("房间数据格式不正确")
    return value


def _iso(value: Any) -> str | None:
    """把数据库时间字段转成 ISO 字符串，供观测页 JSON 序列化。"""
    parsed = _parse_time(value) if value is not None else None
    return parsed.isoformat() if parsed else None


def _delivery_view(row: Any) -> dict[str, Any]:
    """把一行 deliveries 转成观测页使用的中文字段。"""
    return {
        "投递号": str(row["delivery_id"])[:12],
        "会话类型": row["target_type"],
        "会话号": row["target_id"],
        "内容": str(row["text"]),
        "回复锚点": row["reply_to"],
        "事件号": row["event_id"],
        "消息序号": int(row["msg_seq"]) if row["msg_seq"] is not None else 1,
        "状态": row["status"],
        "尝试次数": int(row["attempts"] or 0),
        "最近错误": row["last_error"],
        "下次重试": _iso(row["next_attempt_at"]),
        "创建时间": _iso(row["created_at"]),
        "更新时间": _iso(row["updated_at"]),
    }


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    target_type: Any
    target_id: str
    text: str
    room_id: str | None
    reply_to: str | None
    source_event_id: str | None
    event_id: str | None
    state_version: int | None
    status: str
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    msg_seq: int = 1

    @property
    def target(self):
        from application.contracts import PlatformSession

        return PlatformSession(self.target_type, self.target_id)

    def to_message(self):
        from application.contracts import OutboundMessage

        return OutboundMessage(
            target=self.target,
            text=self.text,
            room_id=self.room_id,
            reply_to=self.reply_to,
            source_event_id=self.source_event_id,
            event_id=self.event_id,
        )


class PostgreSQLStore:
    """单进程应用使用的异步事务存储。"""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 10):
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL 必须是 PostgreSQL 连接串")
        self.database_url = database_url
        self.min_size = min_size
        self.max_size = max_size
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgreSQL 连接池尚未初始化")
        return self._pool

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=self.min_size,
                max_size=self.max_size,
                command_timeout=15,
            )

    async def initialize(self) -> None:
        await self.connect()
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            )
            if row is None:
                raise RuntimeError("数据库尚未迁移，请先执行 python -m main migrate")
            version = int(row["version"])
            if version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前支持版本 {CURRENT_SCHEMA_VERSION}"
                )
            if version < CURRENT_SCHEMA_VERSION:
                raise RuntimeError(f"缺少从数据库版本 {version} 到当前版本的迁移")

    async def migrate(self, migration_dir: str | Path) -> None:
        await self.connect()
        files = sorted(Path(migration_dir).glob("*.sql"))
        if not files:
            raise RuntimeError(f"迁移目录为空: {migration_dir}")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version "
                    "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                current = await connection.fetchval(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_version"
                )
                if int(current) > CURRENT_SCHEMA_VERSION:
                    raise RuntimeError(
                        f"数据库版本 {current} 高于当前支持版本 {CURRENT_SCHEMA_VERSION}"
                    )
                for file in files:
                    version = int(file.stem.split("_", 1)[0])
                    if version <= int(current):
                        continue
                    if version != int(current) + 1:
                        raise RuntimeError(f"迁移版本不连续: {file.name}")
                    await connection.execute(file.read_text(encoding="utf-8"))
                    await connection.execute(
                        "INSERT INTO schema_version(version) VALUES($1)", version
                    )
                    current = version

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def health(self) -> bool:
        try:
            await self.connect()
            async with self.pool.acquire() as connection:
                return await connection.fetchval("SELECT 1") == 1
        except (asyncpg.PostgresError, OSError):
            return False

    async def pending_delivery_count(self) -> int:
        async with self.pool.acquire() as connection:
            return int(
                await connection.fetchval(
                    "SELECT COUNT(*) FROM deliveries "
                    "WHERE status IN ('pending', 'retry', 'sending')"
                )
            )

    async def get_room(self, session_id: str) -> GameRoom | None:
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT snapshot_json FROM rooms WHERE session_id=$1", session_id
            )
            return GameRoom.from_snapshot(_snapshot_value(value)) if value else None

    async def delete_room(self, session_id: str) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute("DELETE FROM rooms WHERE session_id=$1", session_id)

    async def list_active_rooms(self) -> list[GameRoom]:
        phases = tuple(
            phase.value for phase in (GamePhase.LOBBY, GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE)
        )
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT snapshot_json FROM rooms WHERE phase = ANY($1::text[])", list(phases)
            )
            return [GameRoom.from_snapshot(_snapshot_value(row["snapshot_json"])) for row in rows]

    async def find_active_rooms_for_user(self, user_id: str) -> list[GameRoom]:
        rooms = await self.list_active_rooms()
        return [
            room
            for room in rooms
            if any(player.user_id == user_id and player.alive for player in room.players)
        ]

    async def find_rooms_for_user(self, user_id: str) -> list[GameRoom]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch("SELECT snapshot_json FROM rooms ORDER BY updated_at DESC")
        return [
            room for row in rows
            if (room := GameRoom.from_snapshot(_snapshot_value(row["snapshot_json"])))
            and any(player.user_id == user_id for player in room.players)
        ]

    async def begin_event(
        self, event_id: str, session_key: str
    ) -> tuple[bool, list[Any] | None]:
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(minutes=10)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    "INSERT INTO platform_events(event_id, session_key, status, created_at, updated_at) "
                    "VALUES($1, $2, 'processing', $3, $3) "
                    "ON CONFLICT(event_id) DO NOTHING RETURNING event_id",
                    event_id,
                    session_key,
                    now,
                )
                if inserted:
                    return True, None
                row = await connection.fetchrow(
                    "SELECT status, result_json, updated_at FROM platform_events WHERE event_id=$1 FOR UPDATE",
                    event_id,
                )
                if row["status"] == "done":
                    return False, self._messages_from_json(row["result_json"])
                if row["updated_at"] < stale_before:
                    await connection.execute(
                        "UPDATE platform_events SET status='processing', updated_at=$1 WHERE event_id=$2",
                        now,
                        event_id,
                    )
                    return True, None
                return False, None

    async def commit_result(
        self,
        *,
        room: GameRoom | None,
        event_id: str,
        messages: Iterable[Any],
        action_key: str | None = None,
        action_data: dict[str, Any] | None = None,
    ) -> None:
        message_list = list(messages)
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                if room is not None:
                    existing = await connection.fetchrow(
                        "SELECT phase, state_version FROM rooms WHERE session_id=$1 FOR UPDATE", room.session_id
                    )
                    existing_done = bool(
                        existing
                        and existing["phase"]
                        in {GamePhase.FINISHED.value, GamePhase.CANCELLED.value}
                    )
                    # 同一群再开新局：用新的入场房间覆盖已结束/已取消记录。
                    replacing_finished = existing_done and room.phase is GamePhase.LOBBY
                    if (
                        existing_done
                        and not replacing_finished
                        and room.phase
                        not in {GamePhase.FINISHED, GamePhase.CANCELLED}
                    ):
                        raise RuntimeError("已结束房间只读保护触发")
                    if (
                        existing
                        and not replacing_finished
                        and int(existing["state_version"]) > room.state_version
                    ):
                        raise ConcurrentStateError(
                            f"房间状态版本冲突: stored={existing['state_version']} incoming={room.state_version}"
                        )
                    await connection.execute(
                        "INSERT INTO rooms(session_id, phase, state_version, snapshot_json, created_at, updated_at) "
                        "VALUES($1, $2, $3, $4::jsonb, $5, $5) "
                        "ON CONFLICT(session_id) DO UPDATE SET phase=EXCLUDED.phase, "
                        "state_version=EXCLUDED.state_version, snapshot_json=EXCLUDED.snapshot_json, "
                        "updated_at=EXCLUDED.updated_at",
                        room.session_id,
                        room.phase.value,
                        room.state_version,
                        json.dumps(room.snapshot(), ensure_ascii=False, separators=(",", ":")),
                        now,
                    )
                await connection.execute(
                    "UPDATE platform_events SET status='done', result_json=$1::jsonb, updated_at=$2 "
                    "WHERE event_id=$3",
                    json.dumps(
                        [self._message_to_dict(message) for message in message_list],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                    event_id,
                )
                if action_key:
                    data = action_data or {}
                    await connection.execute(
                        "INSERT INTO actions(action_key, event_id, room_id, user_id, action_name, result_json, created_at) "
                        "VALUES($1, $2, $3, $4, $5, $6::jsonb, $7) ON CONFLICT(action_key) DO NOTHING",
                        action_key,
                        event_id,
                        room.session_id if room else None,
                        data.get("user_id"),
                        data.get("name", "command"),
                        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                        now,
                    )
                for message in message_list:
                        # msg_seq 必须按 reply_to（QQ 的 msg_id）递增：同一条群消息回复多条时，
                        # 官方接口会把重复的 msg_seq 判为重复请求丢弃，导致用户完全收不到。
                        await connection.execute(
                            "INSERT INTO deliveries(delivery_id, target_type, target_id, text, room_id, reply_to, "
                            "source_event_id, event_id, state_version, msg_seq, status, attempts, next_attempt_at, created_at, updated_at) "
                            "SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, "
                            "CASE WHEN $6::text IS NULL OR $6::text = '' THEN 1 ELSE "
                            "COALESCE((SELECT MAX(d.msg_seq) FROM deliveries d WHERE d.reply_to = $6::text), 0) + 1 END, "
                            "'pending', 0, $10, $10, $10 "
                            "ON CONFLICT(delivery_id) DO NOTHING",
                            message.delivery_id,
                            message.target.session_type.value,
                            message.target.session_id,
                            message.text,
                            message.room_id,
                            message.reply_to,
                            message.source_event_id,
                            message.event_id,
                            room.state_version if room else None,
                            now,
                        )
                        if room is not None:
                            await connection.execute(
                                "INSERT INTO game_events(room_id, event_id, state_version, kind, is_public, "
                                "target_user_id, payload, created_at) "
                                "VALUES($1, $2, $3, $4, $5, $6, $7::jsonb, $8) "
                                "ON CONFLICT(room_id, state_version, kind, target_user_id) DO NOTHING",
                                room.session_id,
                                event_id,
                                room.state_version,
                                "outbound",
                                message.target.session_type.value not in {"c2c", "direct"},
                                message.target.session_id if message.target.session_type.value in {"c2c", "direct"} else None,
                                json.dumps(self._message_to_dict(message), ensure_ascii=False, separators=(",", ":")),
                                now,
                            )
                if room is not None:
                    await self._persist_room_projections(connection, room, now)

    async def pending_deliveries(self, limit: int = 50) -> list[DeliveryRecord]:
        now = datetime.now(timezone.utc)
        stale = now - timedelta(minutes=10)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "UPDATE deliveries SET status='retry', next_attempt_at=$1, updated_at=$1 "
                    "WHERE status='sending' AND updated_at < $2",
                    now,
                    stale,
                )
                rows = await connection.fetch(
                    "SELECT * FROM deliveries WHERE status IN ('pending','retry') "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= $1) "
                    "ORDER BY created_at LIMIT $2",
                    now,
                    limit,
                )
                return [self._delivery_from_row(row) for row in rows]

    async def claim_delivery(self, delivery_id: str) -> DeliveryRecord | None:
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "UPDATE deliveries SET status='sending', updated_at=$1 "
                    "WHERE delivery_id=$2 AND status IN ('pending','retry') RETURNING *",
                    now,
                    delivery_id,
                )
                return self._delivery_from_row(row) if row else None

    async def mark_delivery_sent(self, delivery_id: str) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE deliveries SET status='sent', last_error=NULL, next_attempt_at=NULL, updated_at=$1 "
                "WHERE delivery_id=$2 AND status='sending'",
                datetime.now(timezone.utc),
                delivery_id,
            )

    async def save_gateway_session(self, shard_id: int, session_id: str, last_sequence: int) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO gateway_sessions(shard_id, session_id, last_sequence, updated_at) "
                "VALUES($1, $2, $3, now()) "
                "ON CONFLICT(shard_id) DO UPDATE SET session_id=EXCLUDED.session_id, "
                "last_sequence=EXCLUDED.last_sequence, updated_at=EXCLUDED.updated_at",
                shard_id,
                session_id,
                last_sequence,
            )

    async def load_gateway_session(self, shard_id: int = 0) -> dict[str, Any] | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT session_id, last_sequence, updated_at FROM gateway_sessions WHERE shard_id=$1",
                shard_id,
            )
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "last_sequence": row["last_sequence"],
            "updated_at": row["updated_at"],
        }

    async def save_direct_session(self, user_id: str, guild_id: str) -> None:
        """记住用户已经打开的 QQ 私信会话。"""
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO direct_sessions(user_id, guild_id, updated_at) "
                "VALUES($1, $2, now()) "
                "ON CONFLICT(user_id) DO UPDATE SET guild_id=EXCLUDED.guild_id, "
                "updated_at=EXCLUDED.updated_at",
                str(user_id),
                str(guild_id),
            )

    async def get_direct_session(self, user_id: str) -> str | None:
        async with self.pool.acquire() as connection:
            return await connection.fetchval(
                "SELECT guild_id FROM direct_sessions WHERE user_id=$1", str(user_id)
            )

    async def get_group_rule_config(self, group_id: str) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT rules_json FROM group_rule_configs WHERE group_id=$1", str(group_id)
            )
        if value is None:
            return {}
        if isinstance(value, str):
            value = json.loads(value)
        return dict(value) if isinstance(value, dict) else {}

    async def save_group_rule_config(
        self, group_id: str, values: dict[str, Any], updated_by: str
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO group_rule_configs(group_id, rules_json, updated_by, updated_at) "
                "VALUES($1, $2::jsonb, $3, now()) "
                "ON CONFLICT(group_id) DO UPDATE SET rules_json=EXCLUDED.rules_json, "
                "updated_by=EXCLUDED.updated_by, updated_at=now()",
                str(group_id), json.dumps(values, ensure_ascii=False), str(updated_by),
            )

    async def get_user_language(self, user_id: str) -> str:
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT language FROM user_languages WHERE user_id=$1", str(user_id)
            )
        return str(value) if value else "中文"

    async def save_user_language(self, user_id: str, language: str) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO user_languages(user_id, language, updated_at) VALUES($1, $2, now()) "
                "ON CONFLICT(user_id) DO UPDATE SET language=EXCLUDED.language, updated_at=now()",
                str(user_id), str(language),
            )

    async def get_player_achievements(self, user_id: str) -> list[int]:
        """对应官方 Players.NewAchievements 读取（InlineCommand.cs:59-72）。"""
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT achievements FROM player_achievements WHERE user_id=$1", str(user_id)
            )
        return sorted({int(item) for item in (value or [])})

    async def lifetime_stats(self, user_ids: Iterable[str]) -> dict[str, dict[str, int]]:
        """Werewolf.cs:5850-5865 —— p.GamePlayers.Count() / Count(x => x.Survived)。
        官方在开局即写入 GamePlayers，因此本局也计入；引擎会在此基础上 +1。"""
        keys = [str(item) for item in user_ids]
        if not keys:
            return {}
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT user_id, count(*) AS games, count(*) FILTER (WHERE survived) AS survived "
                "FROM game_players WHERE user_id = ANY($1::text[]) GROUP BY user_id",
                keys,
            )
        return {
            row["user_id"]: {"games": int(row["games"] or 0), "survived": int(row["survived"] or 0)}
            for row in rows
        }

    async def merge_player_achievements(self, user_id: str, values: Iterable[int]) -> None:
        """对应 Werewolf.cs:6053-6056 / 5919-5921 的 BitArray 置位并保存（只增不减）。"""
        items = sorted({int(item) for item in values})
        if not items:
            return
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO player_achievements(user_id, achievements, updated_at) "
                "VALUES($1, $2::int[], now()) ON CONFLICT(user_id) DO UPDATE SET "
                "achievements=(SELECT array_agg(DISTINCT v ORDER BY v) FROM unnest("
                "player_achievements.achievements || EXCLUDED.achievements) AS v), "
                "updated_at=now()",
                str(user_id), items,
            )

    async def remove_player_achievements(self, user_id: str, values: Iterable[int]) -> None:
        """AdminCommands.cs:410-494 /remach —— 官方把对应位清 0 后 SaveChanges。"""
        items = sorted({int(item) for item in values})
        if not items:
            return
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE player_achievements SET achievements=coalesce("
                "(SELECT array_agg(v ORDER BY v) FROM unnest(achievements) AS v "
                "WHERE v <> ALL($2::int[])), '{}'), updated_at=now() WHERE user_id=$1",
                str(user_id), items,
            )

    async def add_wait_list(self, group_id: str, user_id: str) -> bool:
        """GeneralCommands.cs:418-430 —— 已在名单返回 False（AlreadyOnWaitList）。"""
        async with self.pool.acquire() as connection:
            status = await connection.execute(
                "INSERT INTO notify_games(group_id, user_id, created_at) VALUES($1, $2, now()) "
                "ON CONFLICT(group_id, user_id) DO NOTHING",
                str(group_id), str(user_id),
            )
        return status.rsplit(" ", 1)[-1] != "0"

    async def remove_wait_list(self, group_id: str, user_id: str) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM notify_games WHERE group_id=$1 AND user_id=$2",
                str(group_id), str(user_id),
            )

    async def list_wait_list(self, group_id: str) -> list[str]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT user_id FROM notify_games WHERE group_id=$1 ORDER BY created_at",
                str(group_id),
            )
        return [row["user_id"] for row in rows]

    async def clear_wait_list(self, group_id: str) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute("DELETE FROM notify_games WHERE group_id=$1", str(group_id))

    async def _persist_room_projections(self, connection: asyncpg.Connection, room: GameRoom, now: datetime) -> None:
        await connection.execute(
            "INSERT INTO rule_snapshots(room_id, rules_json, created_at) "
            "VALUES($1, $2::jsonb, $3) ON CONFLICT(room_id) DO UPDATE SET rules_json=EXCLUDED.rules_json",
            room.session_id,
            json.dumps(room.snapshot()["rules"], ensure_ascii=False, separators=(",", ":")),
            now,
        )
        for player in room.players:
            if not player.alive and player.kill_method:
                await connection.execute(
                    "INSERT INTO death_events(room_id, victim_id, killer_id, method, day, state_version, created_at) "
                    "VALUES($1, $2, $3, $4, $5, $6, $7) "
                    "ON CONFLICT(room_id, victim_id, method, day) DO NOTHING",
                    room.session_id,
                    player.user_id,
                    player.killed_by,
                    player.kill_method,
                    int(player.metadata.get("death_day", room.day)),
                    room.state_version,
                    now,
                )
        if room.vote_history:
            latest = room.vote_history[-1]
            await connection.execute(
                "INSERT INTO votes(room_id, day, votes_json, created_at) "
                "VALUES($1, $2, $3::jsonb, $4) ON CONFLICT(room_id, day) DO UPDATE SET votes_json=EXCLUDED.votes_json",
                room.session_id,
                int(latest.get("day", room.day)),
                json.dumps(latest, ensure_ascii=False, separators=(",", ":")),
                now,
            )
        if room.phase == GamePhase.FINISHED:
            await connection.execute(
                "INSERT INTO game_statistics(room_id, winner, result_json, created_at) "
                "VALUES($1, $2, $3::jsonb, $4) "
                "ON CONFLICT(room_id) DO NOTHING",
                room.session_id,
                room.winner.value if room.winner else None,
                json.dumps({
                    "winners": list(room.winners),
                    "statistics": room.statistics,
                    "players": [
                        {
                            "user_id": player.user_id,
                            "role": player.role.value if player.role else None,
                            "won": player.won,
                            "survived": player.alive,
                        }
                        for player in room.players
                    ],
                }, ensure_ascii=False, separators=(",", ":")),
                now,
            )
        await self._persist_game_history(connection, room, now)

    async def _persist_game_history(self, connection: asyncpg.Connection, room: GameRoom, now: datetime) -> None:
        """Werewolf.cs:563-610 建局写 Games/GamePlayers；DBKill(5632-5745) 写 GameKills；
        结算(4649-4921) 回填 TimeEnded/Winner/Won/Survived。"""
        started = _parse_time(room.statistics.get("game_started_at"))
        if started is None:
            return
        game_id = f"{room.session_id}#{started.isoformat()}"
        await connection.execute(
            "INSERT INTO games(game_id, group_id, group_name, mode, time_started, player_count) "
            "VALUES($1, $2, $3, $4, $5, $6) ON CONFLICT(game_id) DO UPDATE SET player_count=EXCLUDED.player_count",
            game_id,
            room.session_id,
            room.statistics.get("group_name"),
            room.rules.mode.value,
            started,
            len(room.players),
        )
        for player in room.players:
            await connection.execute(
                "INSERT INTO game_players(game_id, user_id, name, role, survived, won) "
                "VALUES($1, $2, $3, $4, $5, $6) ON CONFLICT(game_id, user_id) DO UPDATE SET "
                "name=EXCLUDED.name, role=EXCLUDED.role, survived=EXCLUDED.survived, won=EXCLUDED.won",
                game_id,
                player.user_id,
                player.display_name,
                player.role.value if player.role else None,
                player.alive,
                player.won,
            )
            if player.achievements:
                await connection.execute(
                    "INSERT INTO player_achievements(user_id, achievements, updated_at) "
                    "VALUES($1, $2::int[], now()) ON CONFLICT(user_id) DO UPDATE SET "
                    "achievements=(SELECT array_agg(DISTINCT v ORDER BY v) FROM unnest("
                    "player_achievements.achievements || EXCLUDED.achievements) AS v), "
                    "updated_at=now()",
                    player.user_id,
                    sorted({int(item) for item in player.achievements}),
                )
            if not player.alive and player.kill_method:
                await connection.execute(
                    "INSERT INTO game_kills(game_id, victim_id, killer_id, method, day, created_at) "
                    "VALUES($1, $2, $3, $4, $5, $6) ON CONFLICT(game_id, victim_id) DO NOTHING",
                    game_id,
                    player.user_id,
                    player.killed_by,
                    player.kill_method,
                    int(player.metadata.get("death_day", room.day)),
                    now,
                )
        if room.phase == GamePhase.FINISHED:
            await connection.execute(
                "UPDATE games SET time_ended=$2, winner=$3 WHERE game_id=$1",
                game_id,
                now,
                room.winner.value if room.winner else None,
            )

    async def count_idle_kills_24h(self, user_id: str, group_id: str | None = None) -> int:
        """werewolf.sql GetIdleKills24Hours / GetGroupIdleKills24Hours（KillMethodId=16，24 小时窗口）。"""
        window = datetime.now(timezone.utc) - timedelta(days=1)
        async with self.pool.acquire() as connection:
            if group_id is None:
                value = await connection.fetchval(
                    "SELECT count(*) FROM game_kills WHERE victim_id=$1 AND method=$2 AND created_at > $3",
                    str(user_id), KillMethod.IDLE.value, window,
                )
            else:
                value = await connection.fetchval(
                    "SELECT count(*) FROM game_kills gk JOIN games g ON g.game_id = gk.game_id "
                    "WHERE gk.victim_id=$1 AND gk.method=$2 AND gk.created_at > $3 AND g.group_id=$4",
                    str(user_id), KillMethod.IDLE.value, window, str(group_id),
                )
        return int(value or 0)

    async def player_history_stats(self, user_id: str) -> dict[str, Any] | None:
        """StatsController.PlayerStats + PlayerRoles/PlayerMostKilled/PlayerMostKilledBy。
        击杀统计沿用官方 `KillMethodId <> 8`（GuardWolf）过滤。"""
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT count(*) AS games, "
                "count(*) FILTER (WHERE won) AS won, "
                "count(*) FILTER (WHERE survived) AS survived "
                "FROM game_players WHERE user_id=$1",
                str(user_id),
            )
            games = int(row["games"]) if row else 0
            if games == 0:
                return None
            role_row = await connection.fetchrow(
                "SELECT role, count(*) AS times FROM game_players "
                "WHERE user_id=$1 AND role IS NOT NULL GROUP BY role ORDER BY count(*) DESC LIMIT 1",
                str(user_id),
            )
            killed_row = await connection.fetchrow(
                "SELECT gp.name AS name, gk.victim_id AS user_id, count(*) AS times FROM game_kills gk "
                "LEFT JOIN game_players gp ON gp.game_id = gk.game_id AND gp.user_id = gk.victim_id "
                "WHERE gk.killer_id=$1 AND gk.method <> $2 GROUP BY gp.name, gk.victim_id "
                "ORDER BY count(*) DESC LIMIT 1",
                str(user_id), KillMethod.GUARD_WOLF.value,
            )
            killer_row = await connection.fetchrow(
                "SELECT gp.name AS name, gk.killer_id AS user_id, count(*) AS times FROM game_kills gk "
                "LEFT JOIN game_players gp ON gp.game_id = gk.game_id AND gp.user_id = gk.killer_id "
                "WHERE gk.victim_id=$1 AND gk.killer_id IS NOT NULL AND gk.method <> $2 "
                "GROUP BY gp.name, gk.killer_id ORDER BY count(*) DESC LIMIT 1",
                str(user_id), KillMethod.GUARD_WOLF.value,
            )
        won = int(row["won"] or 0)
        survived = int(row["survived"] or 0)
        return {
            "games": games,
            "won": won,
            "lost": games - won,
            "survived": survived,
            "most_common_role": (role_row["role"], int(role_row["times"])) if role_row else None,
            "most_killed": (killed_row["name"] or killed_row["user_id"], int(killed_row["times"])) if killed_row else None,
            "most_killed_by": (killer_row["name"] or killer_row["user_id"], int(killer_row["times"])) if killer_row else None,
        }

    async def group_history_stats(self, group_id: str) -> dict[str, Any]:
        """StatsController.GroupStats + werewolf.sql GroupSurvivor（至少 20 局才计入最佳幸存者）。"""
        async with self.pool.acquire() as connection:
            games = await connection.fetchval(
                "SELECT count(*) FROM games WHERE group_id=$1", str(group_id)
            )
            survivor = await connection.fetchrow(
                "SELECT gp.user_id, max(gp.name) AS name, "
                "count(*) FILTER (WHERE gp.survived) * 100 / count(*) AS pct "
                "FROM game_players gp JOIN games g ON g.game_id = gp.game_id "
                "WHERE g.group_id=$1 GROUP BY gp.user_id HAVING count(*) > 19 "
                "ORDER BY count(*) FILTER (WHERE gp.survived) * 100 / count(*) DESC LIMIT 1",
                str(group_id),
            )
        return {
            "games": int(games or 0),
            "best_survivor": (
                (survivor["name"] or survivor["user_id"], int(survivor["pct"])) if survivor else None
            ),
        }

    async def list_public_groups(self, limit: int = 10) -> list[dict[str, Any]]:
        """Werewolf Control/Helpers/PublicGroups.cs + UpdateHandler.cs:1352-1362。

        官方从 `v_GroupRanking` 取该语言的群，过滤 `LastRefresh >= 今天-21 天`，
        按 LastRefresh 倒序、再按 Ranking 倒序取前 10 条。QQ 版没有独立的排行视图，
        用 games 表里最近 21 天的对局做等价聚合：最近开局时间倒序、局数倒序。
        `/preferred` 关闭的群与 `/bangroup` 封禁的群一律排除（DevCommands.cs:595）。
        """
        window = datetime.now(timezone.utc) - timedelta(days=21)
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT g.group_id, max(g.group_name) AS name, count(*) AS games, "
                "max(g.time_started) AS last_played FROM games g "
                "LEFT JOIN groups gr ON gr.group_id = g.group_id "
                "WHERE g.time_started >= $1 AND coalesce(gr.preferred, TRUE) "
                "AND coalesce(gr.created_by, '') <> 'BAN' "
                "GROUP BY g.group_id ORDER BY max(g.time_started) DESC, count(*) DESC LIMIT $2",
                window,
                int(limit),
            )
        return [
            {
                "group_id": row["group_id"],
                "name": row["name"],
                "games": int(row["games"] or 0),
                "last_played": row["last_played"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 观测页只读查询（adapters/web/dashboard.py）
    # ------------------------------------------------------------------

    async def recent_game_events(self, room_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """按房间倒序取最近的 game_events，用于观测页的「流程日志」。"""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT event_pk, event_id, state_version, kind, is_public, target_user_id, "
                "payload, created_at FROM game_events WHERE room_id=$1 "
                "ORDER BY created_at DESC, event_pk DESC LIMIT $2",
                str(room_id),
                int(limit),
            )
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {"text": payload}
            result.append(
                {
                    "序号": int(row["event_pk"]),
                    "事件号": row["event_id"],
                    "状态版本": int(row["state_version"]),
                    "类型": row["kind"],
                    "公开": bool(row["is_public"]),
                    "定向用户": row["target_user_id"],
                    "内容": (payload or {}).get("text") if isinstance(payload, dict) else str(payload),
                    "时间": _iso(row["created_at"]),
                }
            )
        return result

    async def recent_deliveries(self, room_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """按房间倒序取最近的出站消息，用于观测页的「最近消息」和卡顿定位。"""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT delivery_id, target_type, target_id, text, reply_to, event_id, msg_seq, "
                "status, attempts, last_error, next_attempt_at, created_at, updated_at "
                "FROM deliveries WHERE room_id=$1 ORDER BY created_at DESC LIMIT $2",
                str(room_id),
                int(limit),
            )
        return [_delivery_view(row) for row in rows]

    async def failed_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        """全局失败/重试中的出站消息，用于观测页顶部的「异常」栏。"""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT delivery_id, target_type, target_id, text, reply_to, event_id, msg_seq, "
                "status, attempts, last_error, next_attempt_at, created_at, updated_at, room_id "
                "FROM deliveries WHERE status IN ('failed', 'retry') "
                "ORDER BY updated_at DESC LIMIT $1",
                int(limit),
            )
        views = []
        for row in rows:
            view = _delivery_view(row)
            view["房间"] = row["room_id"]
            views.append(view)
        return views

    async def delivery_summary(self) -> dict[str, int]:
        """出站队列各状态的条数，用来判断「机器人是不是卡在发送环节」。"""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT status, count(*) AS total FROM deliveries GROUP BY status"
            )
        return {str(row["status"]): int(row["total"]) for row in rows}

    # ------------------------------------------------------------------
    # 管理命令用到的表（migrations/009_admin.sql）
    # ------------------------------------------------------------------

    async def touch_player(self, user_id: str, name: str | None = None) -> None:
        """Helpers.cs:60-78 —— 每次收到消息都会 upsert Players 的 Name。"""
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO players(user_id, name) VALUES($1, $2) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "name=coalesce(EXCLUDED.name, players.name), updated_at=now()",
                str(user_id),
                name,
            )

    async def get_player(self, user_id: str) -> dict[str, Any] | None:
        """DevCommands.cs:730-740 /whois、1568-1610 /user 读取的玩家档案。"""
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT p.user_id, p.name, p.temp_ban_count, p.first_seen, "
                "(SELECT count(*) FROM game_players gp WHERE gp.user_id = p.user_id) AS games, "
                "(SELECT min(g.time_started) FROM game_players gp JOIN games g "
                " ON g.game_id = gp.game_id WHERE gp.user_id = p.user_id) AS first_game "
                "FROM players p WHERE p.user_id=$1",
                str(user_id),
            )
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "temp_ban_count": int(row["temp_ban_count"] or 0),
            "first_seen": row["first_seen"],
            "games": int(row["games"] or 0),
            "first_game": row["first_game"],
        }

    async def get_global_ban(self, user_id: str) -> dict[str, Any] | None:
        """AdminCommands.cs:131-160 /getban —— 只看 GlobalBans 表。"""
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT user_id, name, reason, banned_by, ban_date, expires "
                "FROM global_bans WHERE user_id=$1",
                str(user_id),
            )
        return dict(row) if row is not None else None

    async def list_global_bans(self) -> list[dict[str, Any]]:
        """DevCommands.cs:817-846 /getbans —— 按 Expires 升序列出。"""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT user_id, name, reason, banned_by, ban_date, expires "
                "FROM global_bans ORDER BY expires ASC, user_id ASC"
            )
        return [dict(row) for row in rows]

    async def add_global_ban(
        self,
        user_id: str,
        *,
        name: str | None,
        reason: str,
        banned_by: str | None,
        expires: datetime,
    ) -> None:
        """DevCommands.cs:848-980 /permban —— 永封写 SqlDateTime.MaxValue。"""
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO global_bans(user_id, name, reason, banned_by, ban_date, expires) "
                "VALUES($1, $2, $3, $4, now(), $5) ON CONFLICT(user_id) DO UPDATE SET "
                "name=EXCLUDED.name, reason=EXCLUDED.reason, banned_by=EXCLUDED.banned_by, "
                "ban_date=now(), expires=EXCLUDED.expires",
                str(user_id),
                name,
                reason,
                banned_by,
                expires,
            )

    async def remove_global_ban(self, user_id: str) -> bool:
        """DevCommands.cs:982-1056 /remban。"""
        async with self.pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM global_bans WHERE user_id=$1", str(user_id)
            )
        return result.rsplit(" ", 1)[-1] != "0"

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT group_id, name, group_link, preferred, bot_in_group, created_by "
                "FROM groups WHERE group_id=$1",
                str(group_id),
            )
        return dict(row) if row is not None else None

    async def save_group_fields(self, group_id: str, **fields: Any) -> None:
        """按需更新 groups 表；官方对应 db.Groups 上的逐字段赋值 + SaveChanges。"""
        allowed = {"name", "group_link", "preferred", "bot_in_group", "created_by"}
        columns = {key: value for key, value in fields.items() if key in allowed}
        if not columns:
            return
        names = list(columns)
        assignments = ", ".join(f"{name}=${index + 2}" for index, name in enumerate(names))
        insert_columns = ", ".join(["group_id", *names])
        placeholders = ", ".join(f"${index + 1}" for index in range(len(names) + 1))
        async with self.pool.acquire() as connection:
            await connection.execute(
                f"INSERT INTO groups({insert_columns}) VALUES({placeholders}) "
                f"ON CONFLICT(group_id) DO UPDATE SET {assignments}, updated_at=now()",
                str(group_id),
                *[columns[name] for name in names],
            )

    async def get_bot_flag(self, name: str) -> bool:
        """Program.cs:35 MaintMode 之类的进程级开关。"""
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT value FROM bot_flags WHERE name=$1", str(name)
            )
        return bool(value)

    async def set_bot_flag(self, name: str, value: bool) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO bot_flags(name, value) VALUES($1, $2) "
                "ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                str(name),
                bool(value),
            )

    async def playtime_stats(self, player_count: int) -> dict[str, float] | None:
        """DevCommands.cs:315-329 /playtime —— werewolf.sql getPlayTime，单位分钟。"""
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT min(extract(epoch FROM (time_ended - time_started)) / 60) AS minimum, "
                "max(extract(epoch FROM (time_ended - time_started)) / 60) AS maximum, "
                "avg(extract(epoch FROM (time_ended - time_started)) / 60) AS average "
                "FROM games WHERE time_ended IS NOT NULL AND player_count=$1",
                int(player_count),
            )
        if row is None or row["minimum"] is None:
            return None
        return {
            "minimum": float(row["minimum"]),
            "maximum": float(row["maximum"]),
            "average": float(row["average"]),
        }

    async def win_chart_stats(
        self, start: datetime, mode: str | None = None
    ) -> list[dict[str, int]]:
        """Helpers/Charting.cs:52-72 TeamWinChart 的聚合查询。

        官方 SQL 先按 GameId 聚合出每局人数（`HAVING COUNT(gp.PlayerId) >= 5`），
        再按 (Winner, Players) 分组算胜率；最终真正发出的文本只用到
        `Distinct(Players, Games)`（Charting.cs:206，图片发送在官方已被注释掉）。
        端口的 `games.player_count` 就是官方那层 `COUNT(gp.PlayerId)`，因此按人数
        直接分组即等价。
        """
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT player_count AS players, count(*) AS games FROM games "
                "WHERE winner IS NOT NULL AND time_started > $1 AND player_count >= 5 "
                "AND ($2::text IS NULL OR mode = $2) "
                "GROUP BY player_count ORDER BY player_count",
                start,
                mode,
            )
        return [
            {"players": int(row["players"]), "games": int(row["games"])} for row in rows
        ]

    async def monthly_player_counts(self, start: datetime) -> list[dict[str, Any]]:
        """DevCommands.cs:531-543 /test —— 逐月统计 `games.Sum(x => x.GamePlayers.Count)`。"""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT date_trunc('month', g.time_started) AS month, count(gp.*) AS players "
                "FROM games g LEFT JOIN game_players gp ON gp.game_id = g.game_id "
                "WHERE g.time_started >= $1 GROUP BY 1 ORDER BY 1",
                start,
            )
        return [
            {"month": row["month"], "players": int(row["players"] or 0)} for row in rows
        ]

    async def list_linked_groups(self) -> list[dict[str, Any]]:
        """DevCommands.cs:592-594 /checkgroups —— `Preferred != false && GroupLink != null`。

        官方还排除 `Settings.MainChatId` / `Settings.VeteranChatId` 两个官方大群，
        QQ 版没有这两个配置项，故不需要排除。
        """
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT group_id, name, group_link FROM groups "
                "WHERE preferred AND group_link IS NOT NULL ORDER BY group_id"
            )
        return [
            {
                "group_id": row["group_id"],
                "name": row["name"],
                "group_link": row["group_link"],
            }
            for row in rows
        ]

    async def coplayers_missing_achievement(self, user_id: str, value: int) -> list[str]:
        """UpdateHandler.cs:1069-1090 ohai 回调 —— 与目标玩家同局过、且尚未拿到该成就的玩家。"""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT DISTINCT other.user_id FROM game_players self "
                "JOIN game_players other ON other.game_id = self.game_id "
                "LEFT JOIN player_achievements pa ON pa.user_id = other.user_id "
                "WHERE self.user_id = $1 AND NOT ($2 = ANY("
                "coalesce(pa.achievements, ARRAY[]::int[]))) ORDER BY other.user_id",
                str(user_id),
                int(value),
            )
        return [str(row["user_id"]) for row in rows]

    async def mark_delivery_failure(self, delivery_id: str, error: str, max_attempts: int) -> None:
        now = datetime.now(timezone.utc)
        safe_error = " ".join(str(error).split())[:500]
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                attempts = await connection.fetchval(
                    "SELECT attempts FROM deliveries WHERE delivery_id=$1 FOR UPDATE", delivery_id
                )
                attempts = int(attempts or 0) + 1
                terminal = attempts >= max_attempts
                next_at = None if terminal else now + timedelta(seconds=min(60, 2**attempts))
                await connection.execute(
                    "UPDATE deliveries SET status=$1, attempts=$2, last_error=$3, next_attempt_at=$4, updated_at=$5 "
                    "WHERE delivery_id=$6",
                    "failed" if terminal else "retry",
                    attempts,
                    safe_error,
                    next_at,
                    now,
                    delivery_id,
                )

    @staticmethod
    def _message_to_dict(message: Any) -> dict[str, Any]:
        return {
            "session_type": message.target.session_type.value,
            "session_id": message.target.session_id,
            "text": message.text,
            "room_id": message.room_id,
            "reply_to": message.reply_to,
            "source_event_id": message.source_event_id,
            "event_id": message.event_id,
            "sequence": getattr(message, "sequence", 0),
        }

    @classmethod
    def _message_from_dict(cls, data: dict[str, Any]):
        from application.contracts import OutboundMessage, PlatformSession, SessionType

        return OutboundMessage(
            target=PlatformSession(SessionType(data["session_type"]), str(data["session_id"])),
            text=str(data["text"]),
            room_id=data.get("room_id"),
            reply_to=data.get("reply_to"),
            source_event_id=data.get("source_event_id"),
            event_id=data.get("event_id"),
            sequence=int(data.get("sequence") or 0),
        )

    @classmethod
    def _messages_from_json(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, str):
            value = json.loads(value)
        return [cls._message_from_dict(item) for item in value]

    @staticmethod
    def _delivery_from_row(row: asyncpg.Record | None) -> DeliveryRecord:
        if row is None:
            raise LookupError("投递记录不存在")
        return DeliveryRecord(
            delivery_id=str(row["delivery_id"]),
            target_type=_session_type(str(row["target_type"])),
            target_id=str(row["target_id"]),
            text=str(row["text"]),
            room_id=row["room_id"],
            reply_to=row["reply_to"],
            source_event_id=row["source_event_id"],
            event_id=row["event_id"],
            state_version=int(row["state_version"]) if row["state_version"] is not None else None,
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            next_attempt_at=_parse_time(row["next_attempt_at"]),
            msg_seq=int(row["msg_seq"]) if row.get("msg_seq") is not None else 1,
        )
