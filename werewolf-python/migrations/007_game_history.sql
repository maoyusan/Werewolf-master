-- 对应官方 EF 模型 Database/Game.cs、GamePlayer.cs、GameKill.cs、Player.cs 的跨局历史表。
-- 官方在 Werewolf.cs:555-620 建局时写 Games/GamePlayers，在 DBKill(5632-5745) 写 GameKills，
-- 在结算(4649-4921) 回填 TimeEnded/Winner/Won/Survived。

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    group_name TEXT,
    mode TEXT NOT NULL,
    time_started TIMESTAMPTZ NOT NULL,
    time_ended TIMESTAMPTZ,
    winner TEXT,
    player_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_games_group ON games(group_id, time_started DESC);

CREATE TABLE IF NOT EXISTS game_players (
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    name TEXT,
    role TEXT,
    survived BOOLEAN NOT NULL DEFAULT TRUE,
    won BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (game_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_game_players_user ON game_players(user_id);

CREATE TABLE IF NOT EXISTS game_kills (
    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    victim_id TEXT NOT NULL,
    killer_id TEXT,
    method TEXT NOT NULL,
    day INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (game_id, victim_id)
);

CREATE INDEX IF NOT EXISTS idx_game_kills_method ON game_kills(method, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_game_kills_victim ON game_kills(victim_id, created_at DESC);
