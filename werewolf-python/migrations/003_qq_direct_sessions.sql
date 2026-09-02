ALTER TABLE deliveries DROP CONSTRAINT IF EXISTS deliveries_target_type_check;
ALTER TABLE deliveries ADD CONSTRAINT deliveries_target_type_check
    CHECK (target_type IN ('group', 'c2c', 'channel', 'direct'));

CREATE TABLE IF NOT EXISTS direct_sessions (
    user_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_direct_sessions_guild ON direct_sessions(guild_id);
