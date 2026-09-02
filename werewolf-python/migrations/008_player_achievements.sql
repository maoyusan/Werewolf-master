-- 对应官方 Players.NewAchievements（BitArray(200)，位下标等于 AchievementsReworked 枚举值）。
-- QQ 版改为存整数下标数组，语义与官方位图完全一致。
CREATE TABLE IF NOT EXISTS player_achievements (
    user_id TEXT PRIMARY KEY,
    achievements INTEGER[] NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
