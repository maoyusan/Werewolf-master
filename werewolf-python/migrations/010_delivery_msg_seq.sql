-- QQ 官方被动回复：同一条 msg_id 回复多条消息时必须携带递增的 msg_seq，
-- 否则第二条起会被官方接口判为重复消息直接丢弃（表现为「后台执行了但用户没收到」）。
-- msg_seq 按 reply_to（即 QQ 的 msg_id）分配，因此需要一个可查询最大值的列和索引。

ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS msg_seq INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_deliveries_reply_seq ON deliveries(reply_to, msg_seq DESC);

-- 观测页需要按房间倒序拉取最近的出站消息与失败记录。
CREATE INDEX IF NOT EXISTS idx_deliveries_room_created ON deliveries(room_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_game_events_room_created ON game_events(room_id, created_at DESC);
