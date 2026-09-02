CREATE TABLE IF NOT EXISTS rooms (
    session_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    snapshot_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rooms_phase ON rooms(phase);

CREATE TABLE IF NOT EXISTS platform_events (
    event_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'done')),
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_platform_events_status ON platform_events(status, updated_at);

CREATE TABLE IF NOT EXISTS actions (
    action_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES platform_events(event_id),
    room_id TEXT,
    user_id TEXT,
    action_name TEXT NOT NULL,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK (target_type IN ('group', 'c2c')),
    target_id TEXT NOT NULL,
    text TEXT NOT NULL,
    room_id TEXT,
    reply_to TEXT,
    source_event_id TEXT,
    event_id TEXT,
    state_version INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'retry', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deliveries_due ON deliveries(status, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS gateway_sessions (
    shard_id INTEGER PRIMARY KEY,
    session_id TEXT,
    last_sequence BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS game_statistics (
    room_id TEXT PRIMARY KEY,
    winner TEXT,
    result_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS game_events (
    event_pk BIGSERIAL PRIMARY KEY,
    room_id TEXT NOT NULL,
    event_id TEXT,
    state_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    is_public BOOLEAN NOT NULL,
    target_user_id TEXT,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(room_id, state_version, kind, target_user_id)
);
