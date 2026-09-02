CREATE TABLE IF NOT EXISTS group_rule_configs (
    group_id TEXT PRIMARY KEY,
    rules_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
