-- 012：单聊回复锚点 + 「无好友关系」归档
--
-- QQ 单聊主动推送会报「消息发送失败, 无好友关系」。把机器人拉进群、在资料页点
-- 添加，都不等于官方接口认可的可推送会话。用户主动私聊一次之后，那条 msg_id
-- 可以在约 5 分钟内作为被动回复锚点，身份和夜间提示才能发得出去。
-- 这里把每名用户最近一条私聊锚点存下来，供开局时复用。

CREATE TABLE IF NOT EXISTS user_reply_anchors (
    user_id TEXT PRIMARY KEY,
    session_type TEXT NOT NULL CHECK (session_type IN ('c2c', 'direct')),
    session_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    event_id TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_reply_anchors_updated
    ON user_reply_anchors(updated_at);

-- 历史遗留：曾经把「无好友关系」当普通失败重试 5 次，堆在 failed 里。
-- 这类记录已经不可能靠重试发出去，归档成 dead，避免观测页反复告警。
UPDATE deliveries
   SET status = 'dead', next_attempt_at = NULL, updated_at = now()
 WHERE status = 'failed'
   AND last_error ILIKE '%无好友%';
