"""群作用域 openid ↔ 单聊作用域 openid 的关联链路。

背景：QQ 机器人 v2 协议对同一个人下发两个互不相同、也无法互相换算的标识——
群消息给 `author.member_openid`，单聊给 `author.user_openid`。玩家几乎都在群里
`/join`，房间快照存下来的是群作用域的那一个；过去直接拿它去调
`POST /v2/users/{openid}/messages`，QQ 侧查不到这个单聊会话，于是无论玩家有没有
添加机器人，都固定返回「消息发送失败, 无好友关系」。

这组测试锁住修复后的行为：没关联时不再盲发（挂起并点名引导），
/link 与「两侧绑同一个 QQ 号」都能完成关联并把卡住的消息改写目标重新入队。
"""
from __future__ import annotations

import asyncio
import itertools

from application.contracts import PlatformEvent, SessionType
from application.service import GameApplication
from adapters.qq.events import normalize_c2c_message, normalize_group_message
from domain.models import GamePhase, GameRoom, Player
from domain.rules import ruleset_official


_ids = itertools.count(1)


def run(coro):
    return asyncio.run(coro)


class LinkStore:
    """带 openid 映射能力的存储替身，语义对齐 migrations/013_openid_links.sql。"""

    def __init__(self, rooms=()):
        self.rooms: dict[str, GameRoom] = {room.session_id: room for room in rooms}
        self.event_cache: dict[str, list] = {}
        self.commits: list[tuple[GameRoom | None, list]] = []
        self.links: dict[str, str] = {}
        self.codes: dict[str, str] = {}
        self.scopes: dict[str, str] = {}
        self.qq_numbers: dict[str, str] = {}
        self.requeued: list[tuple[str, str]] = []

    async def begin_event(self, event_id, _session_key):
        if event_id in self.event_cache:
            return False, self.event_cache[event_id]
        self.event_cache[event_id] = []
        return True, None

    async def commit_result(self, *, room, event_id, messages, **_kwargs):
        if room is not None:
            self.rooms[room.session_id] = room
        self.event_cache[event_id] = list(messages)
        self.commits.append((room, list(messages)))

    async def get_room(self, session_id):
        return self.rooms.get(session_id)

    async def list_active_rooms(self):
        return list(self.rooms.values())

    async def delete_room(self, session_id):
        self.rooms.pop(session_id, None)

    async def find_active_rooms_for_user(self, user_id):
        return [r for r in self.rooms.values() if any(p.user_id == user_id for p in r.players)]

    async def find_rooms_for_user(self, user_id):
        return await self.find_active_rooms_for_user(user_id)

    async def get_direct_session(self, _user_id):
        return None

    async def get_group_rule_config(self, _group_id):
        return {}

    async def get_user_reply_anchor(self, _user_id):
        return None

    async def touch_player(self, user_id, _name=None):
        self.scopes.setdefault(user_id, "")

    # ---- openid 映射 ----

    async def note_openid_scope(self, user_id, scope):
        self.scopes[user_id] = scope

    async def is_known_c2c_openid(self, openid):
        return openid in self.links.values() or self.scopes.get(openid) == "c2c"

    async def get_c2c_openid(self, member_openid):
        return self.links.get(member_openid)

    async def link_openid(self, member_openid, c2c_openid, *, source="code", union_openid=None):
        self.links[member_openid] = c2c_openid
        self.codes = {c: m for c, m in self.codes.items() if m != member_openid}
        self.requeued.append((member_openid, c2c_openid))
        return 2  # 假装改写了两条卡住的私聊消息

    async def create_link_code(self, member_openid, code):
        self.codes = {c: m for c, m in self.codes.items() if m != member_openid}
        self.codes[code] = member_openid

    async def consume_link_code(self, code, max_age_seconds=1800):
        return self.codes.pop(code, None)

    async def set_player_qq_number(self, user_id, value):
        if value is None:
            self.qq_numbers.pop(user_id, None)
        else:
            self.qq_numbers[user_id] = value

    async def get_player_qq_number(self, user_id):
        return self.qq_numbers.get(user_id)

    async def get_player(self, user_id):
        return {"user_id": user_id, "qq_number": self.qq_numbers.get(user_id), "name": ""}

    async def find_qq_number_peer(self, user_id, qq_number, wanted_scope):
        for other, number in self.qq_numbers.items():
            if other != user_id and number == qq_number and self.scopes.get(other) == wanted_scope:
                return other
        return None


def group_event(content, *, user="member-1", group="g1", name="张三") -> PlatformEvent:
    return normalize_group_message({
        "id": f"evt-{next(_ids)}",
        "group_openid": group,
        "content": content,
        "timestamp": "2026-09-01T00:00:00+00:00",
        "author": {"member_openid": user, "username": name},
    })


def c2c_event(content, *, user="c2c-1", name="张三") -> PlatformEvent:
    return normalize_c2c_message({
        "id": f"evt-{next(_ids)}",
        "content": content,
        "timestamp": "2026-09-01T00:00:00+00:00",
        "author": {"user_openid": user, "username": name},
    })


def lobby_room(session_id="g1") -> GameRoom:
    return GameRoom(
        session_id=session_id,
        rules=ruleset_official(),
        phase=GamePhase.LOBBY,
        players=[Player("member-1", "张三", 1), Player("member-2", "李四", 2)],
    )


def make_app(rooms=()):
    store = LinkStore(rooms)
    return GameApplication(store), store


def test_group_openid_never_used_as_private_target():
    """只有群作用域 openid 时，私聊消息必须挂起，不能拿它去撞「无好友关系」。"""
    app, store = make_app([lobby_room()])
    source = group_event("/status")
    target = run(app._private_target("member-1", source))
    assert target.requires_anchor is True
    assert target.reply_to is None


def test_linked_openid_becomes_private_target():
    app, store = make_app([lobby_room()])
    store.links["member-1"] = "c2c-1"
    source = group_event("/status")
    target = run(app._private_target("member-1", source))
    assert target.requires_anchor is False
    assert target.session.session_type == SessionType.C2C
    assert target.session.session_id == "c2c-1"


def test_link_code_roundtrip_opens_private_channel():
    app, store = make_app([lobby_room()])
    issued = run(app.handle_event(group_event("/link")))
    code = next(c for c in store.codes)
    assert code in "\n".join(m.text for m in issued)

    confirmed = run(app.handle_event(c2c_event(f"/link {code}")))
    assert "私聊通道已开通" in "\n".join(m.text for m in confirmed)
    assert store.links["member-1"] == "c2c-1"
    assert store.codes == {}

    target = run(app._private_target("member-1", group_event("/status")))
    assert target.session.session_id == "c2c-1"
    assert target.requires_anchor is False


def test_link_code_rejects_unknown_code():
    app, _ = make_app([lobby_room()])
    messages = run(app.handle_event(c2c_event("/link 000000")))
    assert "关联码无效或已过期" in "\n".join(m.text for m in messages)


def test_link_code_must_not_be_sent_in_group():
    app, _ = make_app([lobby_room()])
    messages = run(app.handle_event(group_event("/link 123456")))
    assert "私聊机器人发送" in "\n".join(m.text for m in messages)


def test_same_qq_number_on_both_sides_bridges_automatically():
    app, store = make_app([lobby_room()])
    run(app.handle_event(group_event("/bindqq 3183848638")))
    messages = run(app.handle_event(c2c_event("/bindqq 3183848638")))
    assert store.links["member-1"] == "c2c-1"
    assert "私聊通道已自动开通" in "\n".join(m.text for m in messages)


def test_bare_bindqq_replies_with_usage():
    app, _ = make_app([lobby_room()])
    messages = run(app.handle_event(group_event("/bindqq")))
    text = "\n".join(m.text for m in messages)
    assert "/bindqq" in text
    assert "3183848638" in text or "QQ号" in text


def test_unlinked_players_are_named_in_group_hint():
    """群里开局时必须点名「谁还没开通」，而不是只给一句通用提示。"""
    app, _ = make_app([lobby_room()])
    room = lobby_room()
    messages: list = []
    app._append_private_delivery_hint(messages, group_event("/status"), room, ["member-2"])
    text = "\n".join(m.text for m in messages)
    assert "2号" in text and "李四" in text
    assert "/link" in text
