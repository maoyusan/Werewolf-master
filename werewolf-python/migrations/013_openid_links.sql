-- 013：QQ 两套 openid 的映射表（「无好友关系」的真正根因）
--
-- QQ 机器人 v2 协议对同一个人会下发两个互不相同、互不可转换的标识：
--   * 群聊事件 author.member_openid —— 只在「这个群」里有意义；
--   * 单聊事件 author.user_openid   —— 只在「和机器人的单聊」里有意义。
-- 官方没有提供二者的换算接口。玩家几乎都是在群里 /join 进来的，房间快照、
-- players 主键、私聊投递目标此前存的全是 member_openid；发身份牌时却拿它去调
-- POST /v2/users/{openid}/messages，QQ 侧当然查不到这个单聊会话，于是无论玩家
-- 有没有添加机器人，都固定返回「消息发送失败, 无好友关系」。
--
-- 这张表就是缺失的那一环：把同一个人的两个 openid 关联起来。
--   * union：平台若下发 author.union_openid，两侧自动对上，玩家无感；
--   * code ：玩家在群里 /link 拿一次性码，私聊回码完成关联；
--   * qq   ：两侧都用 /bindqq 登记了同一个真实 QQ 号，自动对上。

CREATE TABLE IF NOT EXISTS openid_links (
    member_openid TEXT PRIMARY KEY,
    c2c_openid TEXT,
    union_openid TEXT,
    source TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_openid_links_c2c ON openid_links(c2c_openid);
CREATE INDEX IF NOT EXISTS idx_openid_links_union ON openid_links(union_openid);

-- 一次性关联码。只在玩家主动 /link 时生成，用掉即删，过期由服务侧判断。
CREATE TABLE IF NOT EXISTS openid_link_codes (
    code TEXT PRIMARY KEY,
    member_openid TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_openid_link_codes_member
    ON openid_link_codes(member_openid);

-- 记住每个 openid 是在哪种会话里见到的，才能用「同一个 QQ 号」把两侧配对。
ALTER TABLE players ADD COLUMN IF NOT EXISTS openid_scope TEXT;

CREATE INDEX IF NOT EXISTS idx_players_qq_number
    ON players(qq_number) WHERE qq_number IS NOT NULL;

-- 历史遗留：所有「拿群作用域 openid 当单聊 openid 发」而失败的私聊消息，
-- 重试和补锚点都救不回来（地址本身就是错的），统一归档，别再堆在告警里。
UPDATE deliveries
   SET status = 'dead',
       next_attempt_at = NULL,
       updated_at = now(),
       last_error = COALESCE(NULLIF(last_error, ''), '') || '（已归档：群作用域 openid 无法用于单聊）'
 WHERE status IN ('failed', 'retry', 'waiting', 'pending')
   AND target_type = 'c2c'
   AND last_error ILIKE '%无好友%';
