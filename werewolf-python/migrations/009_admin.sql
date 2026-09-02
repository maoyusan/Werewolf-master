-- 对应官方 EF 模型 Database/Player.cs、Database/GlobalBan.cs、Database/Group.cs 中
-- 管理命令实际读写的列，以及 Program.cs:35 的 MaintMode 开关。
-- 命令来源：Commands/AdminCommands.cs 与 Commands/DevCommands.cs。

-- Database/Player.cs：TelegramId / Name / TempBanCount，以及 /getban 用到的“首次游玩时间”。
CREATE TABLE IF NOT EXISTS players (
    user_id TEXT PRIMARY KEY,
    name TEXT,
    temp_ban_count INTEGER NOT NULL DEFAULT 0,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Database/GlobalBan.cs：TelegramId / Name / Reason / BannedBy / BanDate / Expires。
-- 官方永封写 SqlDateTime.MaxValue（9999-12-31），/getban 以 365 天为界区分永封与临时封。
CREATE TABLE IF NOT EXISTS global_bans (
    user_id TEXT PRIMARY KEY,
    name TEXT,
    reason TEXT NOT NULL DEFAULT '',
    banned_by TEXT,
    ban_date TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_global_bans_expires ON global_bans(expires);

-- Database/Group.cs：GroupId / Name / GroupLink / Preferred / BotInGroup / CreatedBy。
-- CreatedBy = 'BAN' 是官方 /bangroup 的封群标记（Helpers.cs:92-96）。
-- 官方 Preferred 是可空 bool，判定一律写作 `Preferred != false`（DevCommands.cs:595），
-- 即“未设置视为启用”，故这里默认 TRUE。
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT,
    group_link TEXT,
    preferred BOOLEAN NOT NULL DEFAULT TRUE,
    bot_in_group BOOLEAN NOT NULL DEFAULT TRUE,
    created_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Program.cs:35 MaintMode 等进程级开关，改为落库以便重启后保持。
CREATE TABLE IF NOT EXISTS bot_flags (
    name TEXT PRIMARY KEY,
    value BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
