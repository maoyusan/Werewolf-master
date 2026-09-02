CREATE TABLE IF NOT EXISTS rule_snapshots (
    room_id TEXT PRIMARY KEY,
    rules_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS death_events (
    death_pk BIGSERIAL PRIMARY KEY,
    room_id TEXT NOT NULL,
    victim_id TEXT NOT NULL,
    killer_id TEXT,
    method TEXT NOT NULL,
    day INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(room_id, victim_id, method, day)
);

CREATE INDEX IF NOT EXISTS idx_death_events_room ON death_events(room_id, day);

CREATE TABLE IF NOT EXISTS votes (
    room_id TEXT NOT NULL,
    day INTEGER NOT NULL,
    votes_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (room_id, day)
);
