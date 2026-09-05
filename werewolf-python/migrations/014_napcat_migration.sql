-- 014：迁移到 NapCat（OneBot 11），拆掉官方 QQBot 时代的全部遗留结构
--
-- NapCat 直接以真实 QQ 号作为 user_id，群聊与私聊完全一致，于是官方 QQBot
-- 时代为了绕开限制而堆出来的三套机制同时失去意义：
--   * openid 映射（013）：member_openid / c2c_openid / union_openid 与 /link
--     绑定码，NapCat 下不存在两套标识，整套映射与绑定流程一并删除；
--   * 被动回复锚点（011/012）：官方要求回消息必须挂在 5 分钟内的 msg_id 上，
--     所以投递多了 waiting 状态、user_reply_anchors 表和一堆锚点索引。
--     OneBot 可以随时主动发消息，锚点体系整体退役；
--   * qq_number / openid_scope（011/013）：user_id 本身就是 QQ 号，冗余列删除。
-- direct_sessions（003）是频道私信会话缓存，同样是官方协议专属，一并删除。
--
-- 存量数据处理：所有 openid 时代产生的未投递消息在 NapCat 下永远发不出去
-- （目标 id 是 openid，不是 QQ 号），统一判死，避免投递循环空转重试。

-- 1) 未投递的历史消息判死；waiting 必须先清空，否则下面的新约束加不上。
UPDATE deliveries
   SET status = 'dead',
       updated_at = now()
 WHERE status IN ('pending', 'sending', 'retry', 'waiting');

-- 2) 投递状态收敛：去掉 waiting。
ALTER TABLE deliveries DROP CONSTRAINT IF EXISTS deliveries_status_check;
ALTER TABLE deliveries ADD CONSTRAINT deliveries_status_check
    CHECK (status IN ('pending', 'sending', 'retry', 'sent', 'failed', 'dead'));

-- 3) 锚点相关索引全部退役（reply_to 列保留，它现在只是「引用回复」的装饰）。
DROP INDEX IF EXISTS idx_deliveries_waiting;
DROP INDEX IF EXISTS idx_deliveries_anchor_seq;
DROP INDEX IF EXISTS idx_deliveries_reply_seq;
DROP TABLE IF EXISTS user_reply_anchors;

-- 4) openid 映射与绑定码。
DROP TABLE IF EXISTS openid_link_codes;
DROP TABLE IF EXISTS openid_links;

-- 5) 官方协议专属的频道私信会话缓存。
DROP INDEX IF EXISTS idx_direct_sessions_guild;
DROP TABLE IF EXISTS direct_sessions;

-- 6) players 上的冗余身份列。
DROP INDEX IF EXISTS idx_players_qq_number;
ALTER TABLE players DROP COLUMN IF EXISTS qq_number;
ALTER TABLE players DROP COLUMN IF EXISTS openid_scope;
