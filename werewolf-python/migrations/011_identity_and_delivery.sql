-- 011：玩家身份可读化 + 投递状态机补齐
--
-- 背景一：QQ 开放平台只给第三方机器人下发 openid（32 位十六进制），不下发真实 QQ 号，
--         所以「1号 qq号：3183848638｜昵称：张三」里的 QQ 号只能靠玩家自助绑定获得。
--         这里给 players 增加 qq_number 列，由 /bindqq 写入，展示层优先使用它。
--
-- 背景二：QQ 官方限制机器人「主动推送」，没有 msg_id/event_id 锚点的消息会直接被拒，
--         错误文案是「主动消息失败, 无权限」。旧实现把这类永久错误当成普通异常，
--         按指数退避重试 5 次后置 failed 并永久留在表里，于是单个房间攒出 25 条
--         failed 记录。新增两个状态把这条链路补完整：
--           waiting —— 入队时就没有锚点，挂起等待同会话的下一条真实消息来「借」锚点；
--           dead    —— 明确的永久失败（无权限、等待锚点超时），不再重试也不再计入告警。

ALTER TABLE players ADD COLUMN IF NOT EXISTS qq_number TEXT;

CREATE INDEX IF NOT EXISTS idx_players_qq_number
    ON players(qq_number) WHERE qq_number IS NOT NULL;

ALTER TABLE deliveries DROP CONSTRAINT IF EXISTS deliveries_status_check;
ALTER TABLE deliveries ADD CONSTRAINT deliveries_status_check
    CHECK (status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'waiting', 'dead'));

-- 补锚点时要按「目标会话」把挂起的消息按入队顺序捞出来。
CREATE INDEX IF NOT EXISTS idx_deliveries_waiting
    ON deliveries(target_type, target_id, created_at) WHERE status = 'waiting';

-- 超时清理与定期归档都按 (status, updated_at) 扫描。
CREATE INDEX IF NOT EXISTS idx_deliveries_status_updated
    ON deliveries(status, updated_at);

-- msg_seq 原本只按 reply_to 分组，event_id 锚点被漏掉，导致「同一个 event_id 回复多条」
-- 时 msg_seq 恒为 1，第二条起被官方判成重复请求丢弃。改成按 COALESCE(reply_to, event_id)。
CREATE INDEX IF NOT EXISTS idx_deliveries_anchor_seq
    ON deliveries(COALESCE(reply_to, event_id), msg_seq DESC);

-- 历史遗留：把已经堆在 failed 里的「无权限」记录一次性归档成 dead，
-- 避免观测页反复告警一批永远不可能成功的消息。
UPDATE deliveries
   SET status = 'dead', next_attempt_at = NULL, updated_at = now()
 WHERE status = 'failed'
   AND (last_error ILIKE '%无权限%' OR last_error ILIKE '%主动消息%');
