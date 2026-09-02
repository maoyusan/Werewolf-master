from __future__ import annotations

import os
from dataclasses import dataclass

from domain.models import GameMode, Role
from domain.rules import ruleset_official


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"配置项 {name} 必须是整数") from exc


def _roles_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"配置项 {name} 必须是 true 或 false")


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    database_url: str
    host: str
    port: int
    mode: GameMode
    disabled_roles: tuple[str, ...]
    required_roles: tuple[str, ...]
    admin_user_ids: tuple[str, ...]
    dev_user_ids: tuple[str, ...]
    log_level: str
    min_players: int
    join_seconds: int
    night_seconds: int
    day_seconds: int
    vote_seconds: int
    allow_flee: bool
    allow_extend: bool
    max_extend: int
    random_lynch: bool
    secret_lynch: bool
    secret_lynch_show_votes: bool
    secret_lynch_show_voters: bool
    show_roles_on_death: bool
    show_roles_end: str
    thief_full: bool
    allow_arsonist: bool
    burning_overkill: bool
    random_mode: bool
    show_ids: bool
    shuffle_player_list: bool
    allow_nsfw: bool
    delivery_max_attempts: int
    delivery_poll_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        mode_name = os.getenv("GAME_MODE", GameMode.NORMAL.value).strip() or GameMode.NORMAL.value
        try:
            mode = GameMode(mode_name)
        except ValueError as exc:
            raise ValueError("配置项 GAME_MODE 必须为 Normal 或 Chaos") from exc
        values = cls(
            app_id=os.getenv("APP_ID", "").strip(),
            app_secret=os.getenv("APP_SECRET", "").strip(),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            host=os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_int_env("PORT", 18100),
            mode=mode,
            disabled_roles=_roles_env("DISABLED_ROLES"),
            required_roles=_roles_env("REQUIRED_ROLES"),
            admin_user_ids=_roles_env("ADMIN_USER_IDS"),
            # 官方把命令分为 GroupAdminOnly / GlobalAdminOnly / DevOnly 三档
            # （Attributes/CommandAttribute.cs）。QQ 版没有 Telegram 的群管理员接口，
            # 群管理员用 ADMIN_USER_IDS，全局管理员/开发者合并为 DEV_USER_IDS。
            dev_user_ids=_roles_env("DEV_USER_IDS"),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            min_players=_int_env("MIN_PLAYERS", 5),
            join_seconds=_int_env("JOIN_SECONDS", 180),
            night_seconds=_int_env("NIGHT_SECONDS", 90),
            day_seconds=_int_env("DAY_SECONDS", 60),
            vote_seconds=_int_env("VOTE_SECONDS", 90),
            allow_flee=_bool_env("ALLOW_FLEE", True),
            allow_extend=_bool_env("ALLOW_EXTEND", False),
            max_extend=_int_env("MAX_EXTEND", 60),
            random_lynch=_bool_env("RANDOM_LYNCH", False),
            secret_lynch=_bool_env("SECRET_LYNCH", False),
            secret_lynch_show_votes=_bool_env("SECRET_LYNCH_SHOW_VOTES", False),
            secret_lynch_show_voters=_bool_env("SECRET_LYNCH_SHOW_VOTERS", False),
            show_roles_on_death=_bool_env("SHOW_ROLES_ON_DEATH", True),
            show_roles_end=os.getenv("SHOW_ROLES_END", "Living").strip() or "Living",
            thief_full=_bool_env("THIEF_FULL", False),
            allow_arsonist=_bool_env("ALLOW_ARSONIST", True),
            burning_overkill=_bool_env("BURNING_OVERKILL", False),
            random_mode=_bool_env("RANDOM_MODE", False),
            show_ids=_bool_env("SHOW_IDS", False),
            shuffle_player_list=_bool_env("SHUFFLE_PLAYER_LIST", False),
            allow_nsfw=_bool_env("ALLOW_NSFW", False),
            delivery_max_attempts=_int_env("DELIVERY_MAX_ATTEMPTS", 5),
            delivery_poll_seconds=float(os.getenv("DELIVERY_POLL_SECONDS", "2")),
        )
        values.validate()
        return values

    def validate(self, *, require_credentials: bool = True, require_database: bool = True) -> None:
        if require_credentials and (not self.app_id or not self.app_secret):
            raise ValueError("配置项 APP_ID 和 APP_SECRET 必须同时设置")
        if require_database and not self.database_url:
            raise ValueError("配置项 DATABASE_URL 必须设置")
        if self.app_id and (not self.app_id.isdigit() or len(self.app_id) < 3):
            raise ValueError("配置项 APP_ID 格式无效")
        if self.app_secret and len(self.app_secret) < 16:
            raise ValueError("配置项 APP_SECRET 格式无效")
        if self.database_url and not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("配置项 DATABASE_URL 必须是 PostgreSQL 连接串")
        if not 1 <= self.port <= 65535:
            raise ValueError("配置项 PORT 超出范围")
        if self.delivery_max_attempts < 1 or self.delivery_poll_seconds <= 0:
            raise ValueError("投递重试配置无效")
        ruleset_official(
            mode=self.mode,
            min_players=self.min_players,
            join_seconds=self.join_seconds,
            night_seconds=self.night_seconds,
            day_seconds=self.day_seconds,
            vote_seconds=self.vote_seconds,
            allow_flee=self.allow_flee,
            allow_extend=self.allow_extend,
            max_extend=self.max_extend,
            disabled_roles=self.disabled_roles,
            required_roles=self.required_roles,
            random_lynch=self.random_lynch,
            secret_lynch=self.secret_lynch,
            secret_lynch_show_votes=self.secret_lynch_show_votes,
            secret_lynch_show_voters=self.secret_lynch_show_voters,
            show_roles_on_death=self.show_roles_on_death,
            show_roles_end=self.show_roles_end,
            thief_full=self.thief_full,
            allow_arsonist=self.allow_arsonist,
            burning_overkill=self.burning_overkill,
            random_mode=self.random_mode,
            show_ids=self.show_ids,
            shuffle_player_list=self.shuffle_player_list,
            allow_nsfw=self.allow_nsfw,
        )
        for value in (*self.disabled_roles, *self.required_roles):
            Role(value)

    @property
    def app_id_masked(self) -> str:
        if len(self.app_id) <= 4:
            return "***"
        return f"{self.app_id[:2]}***{self.app_id[-2:]}"

    @property
    def rules(self):
        return ruleset_official(
            mode=self.mode,
            min_players=self.min_players,
            join_seconds=self.join_seconds,
            night_seconds=self.night_seconds,
            day_seconds=self.day_seconds,
            vote_seconds=self.vote_seconds,
            allow_flee=self.allow_flee,
            allow_extend=self.allow_extend,
            max_extend=self.max_extend,
            disabled_roles=self.disabled_roles,
            required_roles=self.required_roles,
            random_lynch=self.random_lynch,
            secret_lynch=self.secret_lynch,
            secret_lynch_show_votes=self.secret_lynch_show_votes,
            secret_lynch_show_voters=self.secret_lynch_show_voters,
            show_roles_on_death=self.show_roles_on_death,
            show_roles_end=self.show_roles_end,
            thief_full=self.thief_full,
            allow_arsonist=self.allow_arsonist,
            burning_overkill=self.burning_overkill,
            random_mode=self.random_mode,
            show_ids=self.show_ids,
            shuffle_player_list=self.shuffle_player_list,
            allow_nsfw=self.allow_nsfw,
        )
