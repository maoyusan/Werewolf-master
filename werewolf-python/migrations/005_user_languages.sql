CREATE TABLE IF NOT EXISTS user_languages (
    user_id TEXT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT '中文',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
