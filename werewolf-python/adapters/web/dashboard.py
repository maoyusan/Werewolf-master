"""后台实时观测页：只读地展示每一局狼人杀「现在进行到哪一步、在等什么」。

存在的理由很直接——机器人在群里发完一条消息后突然不动了，光看群消息完全
无法判断是「在等玩家私聊提交夜间行动」「阶段到点了但定时器没跑」还是「消息
根本没投递出去」。这三种情况在群里的表现一模一样，但处理方式完全不同。

本模块把四份数据源拼在一起，给出明确结论：
- `store.list_active_rooms()`：房间快照（阶段、轮次、玩家、投票、夜间行动）；
- `store.recent_game_events()`：引擎实际产出的领域事件；
- `store.recent_deliveries()` / `failed_deliveries()`：出站消息队列与失败原因；
- `infrastructure.observability.TRACE`：进程内流程埋点，覆盖数据库里看不到的
  「走到这里就返回了」的分支。

页面全部只读：不提供任何会改动游戏状态的接口，误点也不会影响正在进行的对局。
所有 handler 都用 try/except 兜住，观测页出错只会在 JSON 里返回中文错误说明，
绝不把异常传导回机器人主流程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import WSMsgType, web

from domain.models import (
    DAY_ACTIONS,
    ROLE_ACTIONS,
    GamePhase,
    GameRoom,
    Player,
    QuestionType,
    Role,
    Team,
    qq_short_id,
)
from domain.roleinfo import role_display_name
from infrastructure.observability import TRACE


log = logging.getLogger(__name__)

# WebSocket 全量快照的推送间隔（秒）。观测页是开发工具，1.5 秒足够跟上节奏，
# 又不至于把数据库压出额外负载。
SNAPSHOT_INTERVAL_SECONDS = 1.5

# 阶段已到期、且房间 state_version 持续不变超过这个秒数，就判定为定时器卡住。
# 定时器 timer_loop 每秒跑一次，正常情况下不会连续 3 秒推不动。
TIMER_STALL_SECONDS = 3.0

# 全量快照里每个房间附带的明细条数，房间详情接口会取更多。
SNAPSHOT_EVENT_LIMIT = 15
SNAPSHOT_DELIVERY_LIMIT = 12
SNAPSHOT_TRACE_LIMIT = 20
DETAIL_LIMIT = 40

# 全量快照里最多深挖多少个房间的明细，避免房间特别多时把接口拖慢。
MAX_DETAILED_ROOMS = 20

# 观测页 WebSocket 连接集合在 app 里的键名。
_WS_KEY = "观测页WebSocket集合"


PHASE_NAMES: dict[GamePhase, str] = {
    GamePhase.LOBBY: "大厅·等待加入",
    GamePhase.NIGHT: "夜晚",
    GamePhase.DAY: "白天讨论",
    GamePhase.VOTE: "投票",
    GamePhase.FINISHED: "已结束",
    GamePhase.CANCELLED: "已取消",
}

TEAM_NAMES: dict[Team, str] = {
    Team.VILLAGE: "村民阵营",
    Team.WOLF: "狼人阵营",
    Team.CULT: "教会阵营",
    Team.SERIAL_KILLER: "连环杀手",
    Team.ARSONIST: "纵火者",
    Team.LOVERS: "恋人",
    Team.TANNER: "坦纳",
    Team.NEUTRAL: "中立",
    Team.SK_HUNTER: "杀手猎人",
    Team.THIEF: "盗贼",
    Team.NO_ONE: "无人",
}

# 与 domain.engine._command_name 保持一致的中文动作名，观测页只做展示。
ACTION_NAMES: dict[str, str] = {
    QuestionType.KILL.value: "狼人袭击",
    QuestionType.KILL_2.value: "狼人二击",
    QuestionType.SEE.value: "查验",
    QuestionType.GUARD.value: "守护",
    QuestionType.VISIT.value: "访问",
    QuestionType.DETECT.value: "侦查",
    QuestionType.CONVERT.value: "转化",
    QuestionType.ROLE_MODEL.value: "模仿/偶像",
    QuestionType.LOVER_1.value: "指定恋人一",
    QuestionType.LOVER_2.value: "指定恋人二",
    QuestionType.SERIAL_KILL.value: "连环杀",
    QuestionType.HUNT.value: "猎杀教徒",
    QuestionType.HUNTER_KILL.value: "猎人开枪",
    QuestionType.SHOOT.value: "开枪",
    QuestionType.SPREAD_SILVER.value: "撒银",
    QuestionType.SANDMAN.value: "催眠",
    QuestionType.PACIFIST.value: "和平主义",
    QuestionType.TROUBLE.value: "捣乱",
    QuestionType.CHEMISTRY.value: "化学实验",
    QuestionType.FREEZE.value: "冻结",
    QuestionType.DOUSE.value: "纵火/引燃",
    QuestionType.THIEF.value: "盗取",
    QuestionType.MAYOR.value: "亮镇长",
    QuestionType.LYNCH.value: "处决投票",
}

# Player.kill_method 存的是 KillMethod 的英文 value，这里只做展示翻译。
KILL_METHOD_NAMES: dict[str, str] = {
    "Lynch": "被处决",
    "Eat": "被狼人吃掉",
    "Shoot": "被枪杀",
    "VisitWolf": "访问时撞上狼人",
    "VisitVictim": "访问了当晚的受害者",
    "VisitBurning": "访问了燃烧中的玩家",
    "VisitKiller": "访问时撞上杀手",
    "GuardWolf": "守护时被狼人杀死",
    "Idle": "无所事事而死",
    "SerialKilled": "被连环杀手杀死",
    "Hunter": "被猎人击杀",
    "HunterCult": "被猎人当作教徒击杀",
    "Hunt": "猎杀反噬",
    "LoverDied": "殉情",
    "Burn": "被烧死",
    "Chemistry": "化学实验失败",
    "FallGrave": "掘墓时坠亡",
    "Spotted": "掘墓时被发现",
    "Suicide": "自尽",
    "Flee": "逃跑出局",
}

DELIVERY_STATUS_NAMES: dict[str, str] = {
    "pending": "待发送",
    "sending": "发送中",
    "sent": "已送达",
    "failed": "失败",
    "retry": "重试中",
    "waiting": "等待回复锚点",
    "dead": "已判死",
}

# 「阶段到期但状态版本没变」的观察表：房间号 -> (上次看到的状态版本, 首次看到超时的单调时间)。
# 只在本进程内存活，纯粹用于诊断，掉了也不影响任何业务。
_STALL_WATCH: dict[str, tuple[int, float]] = {}


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _dumps(payload: Any) -> str:
    """观测页统一的 JSON 序列化：保留中文、认不出来的对象退化成字符串。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _phase_name(phase: GamePhase) -> str:
    return PHASE_NAMES.get(phase, phase.value)


def _team_name(team: Team | None) -> str:
    if team is None:
        return "未知阵营"
    return TEAM_NAMES.get(team, team.value)


def _role_name(role: Role | None) -> str:
    """身份中文名走 domain.roleinfo，与发给玩家的文案保持一致。"""
    if role is None:
        return "未分配"
    try:
        return role_display_name(role)
    except Exception:  # noqa: BLE001 - 语言包异常不能影响观测页
        return role.value


def _action_name(action: str | None) -> str:
    if not action:
        return "未知动作"
    return ACTION_NAMES.get(action, action)


def _short_id(user_id: str | None) -> str:
    """QQ 的 openid 是 32 位十六进制，观测页只留前 8 位方便肉眼比对。

    统一走 domain.models.qq_short_id，保证后台看到的短码和群里
    /whoami、show_ids 显示的「内部号」是同一串，方便交叉核对。
    """
    if not user_id:
        return "—"
    text = str(user_id)
    short = qq_short_id(text)
    return short if short == text else f"{short}…"


def _player_name(room: GameRoom, user_id: str | None) -> str:
    if not user_id:
        return "—"
    for player in room.players:
        if player.user_id == user_id:
            return player.list_label
    return _short_id(user_id)


def _remaining_seconds(room: GameRoom) -> float | None:
    """距离阶段截止还剩多少秒；没有截止时间返回 None，已超时返回负数。"""
    if room.stage_deadline is None:
        return None
    deadline = room.stage_deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (deadline - _now()).total_seconds()


def _expected_night_actions(player: Player) -> tuple[QuestionType, ...]:
    """该玩家在夜晚「本该提交」的动作（排除白天技能）。"""
    if player.role is None:
        return ()
    return tuple(
        action for action in ROLE_ACTIONS.get(player.role, ()) if action not in DAY_ACTIONS
    )


def _submitted_night_actions(room: GameRoom, player: Player) -> list[Any]:
    """本轮（当前天数）该玩家已提交的夜间行动，含狼人二击这类附加键。"""
    found = []
    for key, action in room.night_actions.items():
        if action.actor_id != player.user_id:
            continue
        if action.day != room.day:
            continue
        found.append(action)
    return found


def _submitted_day_actions(room: GameRoom, player: Player) -> list[Any]:
    return [
        action
        for action in room.day_actions.values()
        if action.actor_id == player.user_id and action.day == room.day
    ]


def _describe_action(room: GameRoom, action: Any) -> str:
    name = _action_name(getattr(action, "action", None))
    target = _player_name(room, getattr(action, "target_id", None))
    second = getattr(action, "second_target_id", None)
    if second:
        return f"{name} → {target} / {_player_name(room, second)}"
    if getattr(action, "target_id", None):
        return f"{name} → {target}"
    return name


def _done_label(room: GameRoom, player: Player) -> str:
    """玩家「本轮已执行操作」的中文描述。"""
    parts = [_describe_action(room, action) for action in _submitted_night_actions(room, player)]
    parts.extend(_describe_action(room, action) for action in _submitted_day_actions(room, player))
    if room.phase == GamePhase.VOTE and player.user_id in room.votes:
        target = room.votes.get(player.user_id)
        parts.append("投票 → 弃票" if target is None else f"投票 → {_player_name(room, target)}")
    return "；".join(parts) if parts else "无"


def _pending_label(room: GameRoom, player: Player) -> str:
    """玩家「本轮待执行操作」的中文描述，也是判断卡在谁身上的依据。"""
    if not player.alive:
        return "已出局"
    if player.fled:
        return "已逃跑"
    if room.phase == GamePhase.LOBBY:
        return "等待开局"
    if room.phase == GamePhase.VOTE:
        return "无（已投票）" if player.user_id in room.votes else "待投票"
    if room.phase == GamePhase.NIGHT:
        if player.frozen or player.is_frozen:
            return "被冻结，本夜无需行动"
        expected = _expected_night_actions(player)
        if not expected:
            return "无夜间行动"
        if _submitted_night_actions(room, player):
            return "无（已提交）"
        return "、".join(_action_name(action.value) for action in expected)
    if room.phase == GamePhase.DAY:
        return "自由发言（无强制操作）"
    return "无"


def _vote_label(room: GameRoom, player: Player) -> str:
    if player.user_id not in room.votes:
        return "未投票" if room.phase == GamePhase.VOTE and player.alive else "—"
    target = room.votes.get(player.user_id)
    return "弃票" if target is None else _player_name(room, target)


def _player_view(room: GameRoom, player: Player) -> dict[str, Any]:
    if player.fled:
        status = "已逃跑"
    elif player.alive:
        status = "存活"
    else:
        status = "出局"
    tags = []
    if player.frozen or player.is_frozen:
        tags.append("冻结")
    if player.doused:
        tags.append("被浇油")
    if player.burning:
        tags.append("燃烧中")
    if player.lover_id:
        tags.append(f"恋人:{_player_name(room, player.lover_id)}")
    if player.has_revealed:
        tags.append("已亮身份")
    if player.died_last_night:
        tags.append("昨夜死亡")
    return {
        "座位": player.seat,
        "昵称": player.nickname,
        "QQ号": player.qq_id,
        "内部号": _short_id(player.user_id),
        "身份": _role_name(player.role),
        "初始身份": _role_name(player.original_role) if player.original_role else "—",
        "阵营": _team_name(player.team),
        "存活": player.alive,
        "状态": status,
        "死因": KILL_METHOD_NAMES.get(player.kill_method or "", player.kill_method or "—"),
        "凶手": _player_name(room, player.killed_by) if player.killed_by else "—",
        "已执行操作": _done_label(room, player),
        "待执行操作": _pending_label(room, player),
        "投票给": _vote_label(room, player),
        "得票": player.votes_received,
        "标记": tags,
    }


def _vote_view(room: GameRoom) -> dict[str, Any]:
    """当前票型：谁投谁、票数统计、弃票名单、未投名单。"""
    cast: list[dict[str, Any]] = []
    tally: dict[str, int] = {}
    skipped: list[str] = []
    not_voted: list[str] = []
    for player in room.players:
        if player.user_id in room.votes:
            target = room.votes.get(player.user_id)
            cast.append(
                {
                    "投票人": player.list_label,
                    "投给": "弃票" if target is None else _player_name(room, target),
                    "票重": player.vote_weight,
                }
            )
            if target is None:
                skipped.append(player.list_label)
            else:
                name = _player_name(room, target)
                tally[name] = tally.get(name, 0) + max(1, player.vote_weight)
        elif player.alive:
            not_voted.append(player.list_label)
    ranked = sorted(tally.items(), key=lambda item: item[1], reverse=True)
    return {
        "轮次": room.vote_round,
        "票型": cast,
        "票数统计": [{"候选": name, "票数": count} for name, count in ranked],
        "弃票": skipped,
        "未投票": not_voted,
        "历史轮次": len(room.vote_history),
    }


def _night_view(room: GameRoom) -> dict[str, Any]:
    """夜间行动面板：各角色已提交 / 未提交。"""
    submitted: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for player in room.players:
        actions = _submitted_night_actions(room, player)
        if actions:
            submitted.append(
                {
                    "玩家": player.list_label,
                    "身份": _role_name(player.role),
                    "动作": "；".join(_describe_action(room, action) for action in actions),
                }
            )
            continue
        if not player.alive or player.fled:
            continue
        if player.frozen or player.is_frozen:
            continue
        expected = _expected_night_actions(player)
        if not expected:
            continue
        missing.append(
            {
                "玩家": player.list_label,
                "身份": _role_name(player.role),
                "应做": "、".join(_action_name(action.value) for action in expected),
            }
        )
    return {"已提交": submitted, "未提交": missing}


def _waiting_for(room: GameRoom) -> list[dict[str, Any]]:
    """当前在等谁操作。返回空列表代表没有卡在任何玩家身上。"""
    waiting: list[dict[str, Any]] = []
    if room.phase == GamePhase.LOBBY:
        missing = room.rules.min_players - len(room.players)
        if missing > 0:
            waiting.append({"座位": "—", "玩家": "更多玩家", "缺少操作": f"还差 {missing} 人才能开局"})
        else:
            host = _player_name(room, room.host_user_id) if room.host_user_id else "任一在场玩家"
            waiting.append({"座位": "—", "玩家": host, "缺少操作": "发送 /startgame 开局"})
        return waiting
    if room.phase == GamePhase.NIGHT:
        for player in room.players:
            if not player.alive or player.fled:
                continue
            if player.frozen or player.is_frozen:
                continue
            expected = _expected_night_actions(player)
            if not expected or _submitted_night_actions(room, player):
                continue
            waiting.append(
                {
                    "座位": player.seat,
                    "玩家": player.list_label,
                    "缺少操作": "私聊提交：" + "、".join(
                        _action_name(action.value) for action in expected
                    ),
                }
            )
        return waiting
    if room.phase == GamePhase.VOTE:
        for player in room.players:
            if player.alive and not player.fled and player.user_id not in room.votes:
                waiting.append(
                    {"座位": player.seat, "玩家": player.list_label, "缺少操作": "提交处决投票"}
                )
        return waiting
    return waiting


def _current_step(room: GameRoom) -> str:
    """当前正在执行的流程。"""
    return {
        GamePhase.LOBBY: f"等待玩家加入（已 {len(room.players)}/{room.rules.max_players} 人）",
        GamePhase.NIGHT: f"收集第 {room.day} 夜的夜间行动（玩家私聊提交）",
        GamePhase.DAY: f"第 {room.day} 天白天讨论中",
        GamePhase.VOTE: f"第 {room.day} 天处决投票（第 {room.vote_round} 轮）",
        GamePhase.FINISHED: "对局已结束，等待清理",
        GamePhase.CANCELLED: "对局已取消",
    }.get(room.phase, room.phase.value)


def _next_step(room: GameRoom) -> str:
    """截止之后引擎将要执行什么。"""
    return {
        GamePhase.LOBBY: (
            f"人数达到 {room.rules.min_players} 人且发起人发送 /startgame 即开局；"
            "加入时间到点后由定时器自动开局或因人数不足解散"
        ),
        GamePhase.NIGHT: "截止或全员提交后执行夜晚结算（守护/袭击/查验判定、死亡与转化），再进入白天讨论",
        GamePhase.DAY: "讨论时间到点后进入投票阶段，并私聊/群发投票菜单",
        GamePhase.VOTE: "截止或全员投完后统计处决结果，判定胜负；未分胜负则进入下一夜",
        GamePhase.FINISHED: "结算已完成，房间将被归档",
        GamePhase.CANCELLED: "房间将被清理",
    }.get(room.phase, "—")


def _track_stall(room: GameRoom, remaining: float | None) -> float | None:
    """返回「阶段已到期且状态版本一直没变」持续了多少秒；未超时返回 None。

    定时器 timer_loop 每秒调用一次 process_due_rooms，正常推进时房间的
    state_version 一定会变。若持续不变，说明定时器没跑到这个房间，或者推进过程
    中抛了异常被吞掉了——这正是「机器人发完消息就不动了」最常见的原因。
    """

    key = room.session_id
    if remaining is None or remaining > 0:
        _STALL_WATCH.pop(key, None)
        return None
    now = time.monotonic()
    recorded = _STALL_WATCH.get(key)
    if recorded is None or recorded[0] != room.state_version:
        _STALL_WATCH[key] = (room.state_version, now)
        return 0.0
    return now - recorded[1]


def _diagnose(
    room: GameRoom,
    waiting: list[dict[str, Any]],
    remaining: float | None,
    room_failed: list[dict[str, Any]],
) -> dict[str, str]:
    """「为什么停滞」的判定，结论直接显示在房间卡片顶部。

    判定顺序按「排查代价从低到高」排列：先看有没有消息根本没发出去，再看定时器
    是不是没推进，最后才归因到玩家没操作。
    """

    if room_failed:
        first = room_failed[0]
        reason = first.get("最近错误") or "未记录错误原因"
        return {
            "级别": "错误",
            "结论": "消息投递失败",
            "说明": (
                f"该房间有 {len(room_failed)} 条出站消息处于 failed/重试状态，"
                f"最近一条错误：{reason}。机器人「发了消息却没反应」通常就卡在这里，"
                "请看下方「异常投递」卡片。"
                "（已确认无法投递的记录会转为「已判死」，不会再计入这里反复告警；"
                "等待被动回复锚点的记录状态是「等待回复锚点」，会在该会话下一条消息到达时自动补发。）"
            ),
        }
    if room.phase in {GamePhase.FINISHED, GamePhase.CANCELLED}:
        return {"级别": "正常", "结论": "对局已收尾", "说明": "房间处于终局阶段，不再推进。"}

    stalled = _track_stall(room, remaining)
    if stalled is not None and stalled >= TIMER_STALL_SECONDS:
        return {
            "级别": "错误",
            "结论": "等待定时器",
            "说明": (
                f"阶段截止时间已过去 {abs(remaining or 0):.0f} 秒，但房间状态版本"
                f"一直停在 {room.state_version}（已持续 {stalled:.0f} 秒未变）。"
                "定时器 timer_loop / process_due_rooms 可能没扫到这个房间或推进时抛了异常，"
                "请对照下方「流程日志」里「定时器」阶段的记录。"
            ),
        }
    if remaining is not None and remaining <= 0:
        return {
            "级别": "提示",
            "结论": "阶段刚到期，结算中",
            "说明": "截止时间刚过，定时器将在约 1 秒内推进到下一阶段。",
        }
    if waiting:
        names = "、".join(str(item["玩家"]) for item in waiting[:6])
        more = f" 等 {len(waiting)} 人" if len(waiting) > 6 else ""
        return {
            "级别": "提示",
            "结论": "等待玩家操作",
            "说明": f"阶段未到期，还在等：{names}{more}。到点后会由定时器强制推进。",
        }
    if room.phase in {GamePhase.NIGHT, GamePhase.VOTE}:
        return {
            "级别": "警告",
            "结论": "全员已操作，等待结算",
            "说明": (
                "该阶段所有人都已提交，但阶段还没结束。正常情况下引擎会立刻结算；"
                "若长时间停在这里，说明结算条件判定或状态落库出了问题。"
            ),
        }
    return {"级别": "正常", "结论": "正常推进中", "说明": "阶段未到期，没有发现阻塞点。"}


def _room_view(
    room: GameRoom,
    *,
    room_failed: list[dict[str, Any]],
    events: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
    traces: list[dict[str, Any]],
) -> dict[str, Any]:
    """把一个房间拼成观测页需要的全部信息。"""
    remaining = _remaining_seconds(room)
    waiting = _waiting_for(room)
    alive = [player for player in room.players if player.alive]
    # 私聊事件只存了 openid，直接显示既不可读也是内部标识外泄；
    # 这里统一映射成「座位号 + QQ号 + 昵称」，查不到才退化成短内部号。
    for item in events:
        target = item.get("定向用户")
        item["定向玩家"] = _player_name(room, str(target)) if target else ""
    return {
        # 「房间号」是内部主键，前端拿它做锚点和详情路由，必须保持完整；
        # 「群号」只用于卡片标题展示，压成短码，不把 32 位 openid 摊在页面上。
        "房间号": room.session_id,
        "群号": _short_id(room.session_id),
        "房间状态": _phase_name(room.phase),
        "阶段代号": room.phase.value,
        "当前轮次": room.day,
        "状态版本": room.state_version,
        "发起人": _player_name(room, room.host_user_id) if room.host_user_id else "无（房主已离场）",
        "规则名": room.rules.name,
        "人数": {
            "总人数": len(room.players),
            "存活": len(alive),
            "出局": len(room.players) - len(alive),
            "最少": room.rules.min_players,
            "最多": room.rules.max_players,
        },
        "阶段开始": _iso(room.stage_started_at),
        "阶段截止": _iso(room.stage_deadline),
        "剩余秒数": None if remaining is None else round(remaining, 1),
        "已超时": bool(remaining is not None and remaining <= 0),
        "当前流程": _current_step(room),
        "下一步": _next_step(room),
        "等待谁": waiting,
        "诊断": _diagnose(room, waiting, remaining, room_failed),
        "玩家": [_player_view(room, player) for player in sorted(room.players, key=lambda p: p.seat)],
        "投票": _vote_view(room),
        "夜间行动": _night_view(room),
        "系统事件": events,
        "流程日志": traces,
        "最近消息": deliveries,
        "异常投递": room_failed,
    }


def _last_sent_at(deliveries: list[dict[str, Any]]) -> str | None:
    """从一批投递记录里找出最近一次成功送达的时间。"""
    times = [
        item.get("更新时间")
        for item in deliveries
        if item.get("状态") == "sent" and item.get("更新时间")
    ]
    return max(times) if times else None


# ----------------------------------------------------------------------
# 数据组装
# ----------------------------------------------------------------------


async def _build_snapshot(runtime: Any) -> dict[str, Any]:
    """组装全量快照。任何一处查询失败都只记进「错误」里，不往外抛。"""
    errors: list[str] = []
    store = runtime.store

    rooms: list[GameRoom] = []
    try:
        rooms = await store.list_active_rooms()
    except Exception as exc:  # noqa: BLE001 - 观测页永远不能把异常带回主流程
        log.exception("观测页读取活跃对局失败")
        errors.append(f"读取活跃对局失败：{exc}")

    queue_summary: dict[str, int] = {}
    try:
        queue_summary = await store.delivery_summary()
    except Exception as exc:  # noqa: BLE001
        log.exception("观测页读取出站队列统计失败")
        errors.append(f"读取出站队列统计失败：{exc}")

    failed: list[dict[str, Any]] = []
    try:
        failed = await store.failed_deliveries(50)
    except Exception as exc:  # noqa: BLE001
        log.exception("观测页读取失败投递失败")
        errors.append(f"读取失败投递失败：{exc}")

    failed_by_room: dict[str, list[dict[str, Any]]] = {}
    for item in failed:
        failed_by_room.setdefault(str(item.get("房间")), []).append(item)

    # 清理已经不活跃的房间在停滞观察表里的残留，避免长期运行后无限增长。
    active_ids = {room.session_id for room in rooms}
    for stale in [key for key in _STALL_WATCH if key not in active_ids]:
        _STALL_WATCH.pop(stale, None)

    room_views: list[dict[str, Any]] = []
    last_sent: str | None = None
    for room in rooms[:MAX_DETAILED_ROOMS]:
        events: list[dict[str, Any]] = []
        deliveries: list[dict[str, Any]] = []
        try:
            events = await store.recent_game_events(room.session_id, SNAPSHOT_EVENT_LIMIT)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"房间 {room.session_id} 读取系统事件失败：{exc}")
        try:
            deliveries = await store.recent_deliveries(room.session_id, SNAPSHOT_DELIVERY_LIMIT)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"房间 {room.session_id} 读取最近消息失败：{exc}")
        candidate = _last_sent_at(deliveries)
        if candidate and (last_sent is None or candidate > last_sent):
            last_sent = candidate
        try:
            room_views.append(
                _room_view(
                    room,
                    room_failed=failed_by_room.get(room.session_id, []),
                    events=events,
                    deliveries=deliveries,
                    traces=TRACE.recent(room.session_id, SNAPSHOT_TRACE_LIMIT),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单个房间渲染失败不影响其它房间
            log.exception("观测页组装房间视图失败", extra={"room_id": room.session_id})
            errors.append(f"房间 {room.session_id} 组装失败：{exc}")

    total_players = sum(len(room.players) for room in rooms)
    alive_players = sum(len([p for p in room.players if p.alive]) for room in rooms)
    blocked = [
        view["房间号"] for view in room_views if view["诊断"]["级别"] in {"错误", "警告"}
    ]

    settings = getattr(runtime, "settings", None)
    adapter = getattr(runtime, "adapter", None)
    return {
        "生成时间": _iso(_now()),
        "总览": {
            "活跃对局": len(rooms),
            "总玩家数": total_players,
            "存活玩家数": alive_players,
            "异常对局": len(blocked),
        },
        "机器人主持": {
            "网关就绪": bool(getattr(adapter, "is_ready", False)),
            "监听地址": (
                f"http://{settings.host}:{settings.port}" if settings is not None else "—"
            ),
            "出站队列": queue_summary,
            "排队中": sum(
                queue_summary.get(status, 0) for status in ("pending", "sending", "retry")
            ),
            # waiting 是「等同会话下一条真实消息来补回复锚点」的挂起态，
            # dead 是已确认不可能成功（无权限/等锚点超时）的记录：
            # 两者都不该混进「失败投递数」去反复告警，但要能单独看到条数。
            "等待锚点": queue_summary.get("waiting", 0),
            "已判死": queue_summary.get("dead", 0),
            "失败投递数": len(failed),
            "最近成功投递": last_sent,
            "需关注房间": blocked,
        },
        "对局": room_views,
        "异常投递": failed,
        "追踪摘要": TRACE.summary(),
        "全局流程日志": TRACE.recent(None, 60),
        "错误": errors,
    }


async def _build_room_detail(runtime: Any, room_id: str) -> dict[str, Any]:
    """单房间详情：明细条数比全量快照多，用于深入排查。"""
    errors: list[str] = []
    store = runtime.store
    room: GameRoom | None = None
    try:
        rooms = await store.list_active_rooms()
        room = next((item for item in rooms if item.session_id == room_id), None)
    except Exception as exc:  # noqa: BLE001
        log.exception("观测页读取房间失败", extra={"room_id": room_id})
        errors.append(f"读取房间失败：{exc}")
    if room is None:
        return {"生成时间": _iso(_now()), "房间号": room_id, "错误": errors or ["房间不存在或已结束"]}

    events: list[dict[str, Any]] = []
    deliveries: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    try:
        events = await store.recent_game_events(room_id, DETAIL_LIMIT)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"读取系统事件失败：{exc}")
    try:
        deliveries = await store.recent_deliveries(room_id, DETAIL_LIMIT)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"读取最近消息失败：{exc}")
    try:
        failed = [
            item for item in await store.failed_deliveries(50)
            if str(item.get("房间")) == room_id
        ]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"读取失败投递失败：{exc}")

    view = _room_view(
        room,
        room_failed=failed,
        events=events,
        deliveries=deliveries,
        traces=TRACE.recent(room_id, 200),
    )
    view["生成时间"] = _iso(_now())
    view["错误"] = errors
    return view


# ----------------------------------------------------------------------
# 路由处理
# ----------------------------------------------------------------------


async def _dashboard_page(request: web.Request) -> web.Response:
    """返回内嵌的观测页 HTML，不依赖任何外部资源。"""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")


async def _api_observe(request: web.Request) -> web.Response:
    try:
        payload = await _build_snapshot(request.app["runtime"])
        return web.json_response(payload, dumps=_dumps)
    except Exception as exc:  # noqa: BLE001 - 观测页出错只反馈给页面
        log.exception("观测页全量快照接口异常")
        return web.json_response(
            {"错误": [f"生成观测快照失败：{exc}"], "生成时间": _iso(_now())},
            status=500,
            dumps=_dumps,
        )


async def _api_observe_room(request: web.Request) -> web.Response:
    room_id = request.match_info.get("room_id", "")
    try:
        payload = await _build_room_detail(request.app["runtime"], room_id)
        return web.json_response(payload, dumps=_dumps)
    except Exception as exc:  # noqa: BLE001
        log.exception("观测页房间详情接口异常", extra={"room_id": room_id})
        return web.json_response(
            {"房间号": room_id, "错误": [f"生成房间详情失败：{exc}"]},
            status=500,
            dumps=_dumps,
        )


async def _push_loop(
    ws: web.WebSocketResponse, runtime: Any, queue: "asyncio.Queue[dict[str, Any]]"
) -> None:
    """WebSocket 推送循环：先来一发全量快照，之后增量 + 定时全量。

    增量来自 TRACE 的订阅队列（埋点一发生就推），全量来自定时轮询数据库
    （房间状态只能靠查库拿到）。两者合起来才能既及时又完整。
    """

    last_snapshot = 0.0
    while not ws.closed:
        now = time.monotonic()
        if now - last_snapshot >= SNAPSHOT_INTERVAL_SECONDS:
            last_snapshot = now
            try:
                payload = await _build_snapshot(runtime)
            except Exception as exc:  # noqa: BLE001
                log.exception("观测页 WebSocket 组装快照失败")
                payload = {"错误": [f"生成观测快照失败：{exc}"], "生成时间": _iso(_now())}
            if ws.closed:
                return
            await ws.send_json({"类型": "快照", "数据": payload}, dumps=_dumps)
        try:
            # 用短超时轮询订阅队列：既能第一时间推送新埋点，又能按时触发全量快照。
            entry = await asyncio.wait_for(queue.get(), timeout=0.4)
        except asyncio.TimeoutError:
            continue
        if ws.closed:
            return
        await ws.send_json({"类型": "追踪", "数据": entry}, dumps=_dumps)


async def _ws_observe(request: web.Request) -> web.WebSocketResponse:
    """观测页的实时通道。心跳由 aiohttp 的 heartbeat 负责，断开即彻底退订。"""
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    request.app[_WS_KEY].add(ws)
    queue = TRACE.subscribe()
    pusher = asyncio.create_task(_push_loop(ws, request.app["runtime"], queue))
    try:
        async for message in ws:
            # 页面只需要接收；这里读消息主要是为了尽快感知客户端断开。
            if message.type in {WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING}:
                break
    except Exception:  # noqa: BLE001 - 客户端异常断开不算故障
        log.debug("观测页 WebSocket 异常断开", exc_info=True)
    finally:
        pusher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pusher
        TRACE.unsubscribe(queue)
        request.app[_WS_KEY].discard(ws)
        with contextlib.suppress(Exception):
            await ws.close()
    return ws


async def _close_websockets(app: web.Application) -> None:
    """进程退出时主动关掉所有观测页连接，避免 runner.cleanup() 挂住。"""
    for ws in list(app[_WS_KEY]):
        with contextlib.suppress(Exception):
            await ws.close(code=1001, message=b"server shutdown")
    app[_WS_KEY].clear()


def setup_dashboard(app: web.Application) -> None:
    """把观测页的路由挂到已有的 aiohttp 应用上。

    调用方需要保证 `app["runtime"]` 已经是 Runtime 实例（main.py 里已经这么做）。
    这里注册的全部是只读接口，不会改动任何游戏状态。
    """

    app[_WS_KEY] = set()
    app.router.add_get("/dashboard", _dashboard_page)
    app.router.add_get("/api/observe", _api_observe)
    app.router.add_get("/api/observe/{room_id}", _api_observe_room)
    app.router.add_get("/ws/observe", _ws_observe)
    app.on_shutdown.append(_close_websockets)


# ----------------------------------------------------------------------
# 页面（纯原生 HTML + CSS + JS，不引任何外部资源）
# ----------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>狼人杀 · 运行检测台</title>
<style>
:root{
  /* 软件即服务值班台令牌：石板底 + 状态绿。业务色只写在这里。 */
  --bg:#0F172A; --sidebar:#0B1220; --card:#1B2336; --panel:#1E293B; --panel2:#1E293B; --panel3:#272F42;
  --fg:#F8FAFC; --muted:#CBD5E1; --dim:#94A3B8;
  --line:#475569; --line-soft:#334155;
  --ok:#22C55E; --warn:#D29922; --err:#EF4444; --tip:#38BDF8; --accent:#22C55E;
  --ok-bg:#052E1A; --warn-bg:#2A1F0A; --err-bg:#3F0D12; --tip-bg:#082F49; --accent-bg:#052E1A;
  --ok-line:#166534; --warn-line:#854D0E; --err-line:#7F1D1D; --tip-line:#075985;
  --focus:#FFFFFF; --shadow:0 1px 2px rgba(0,0,0,.35),0 12px 28px -18px rgba(0,0,0,.7);
  --scroll-thumb:#334155; --scroll-thumb-hover:#475569;
  --evt:#C4B5FD; --evt-bg:#1E1B4B; --evt-line:#4C1D95;
  --row-even:rgba(248,250,252,.03); --row-hover:rgba(248,250,252,.06);
  --topbar:rgba(15,23,42,.88);
  --space-1:8px; --space-2:16px; --space-3:24px; --space-4:32px; --space-5:40px;
  --text-12:12px; --text-14:14px; --text-16:16px; --text-20:20px;
  --r-sm:4px; --r-md:8px; --r-lg:12px;
  --sidebar-w:240px; --topbar-h:56px;
  --ease:ease; --dur:.16s;
}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:var(--scroll-thumb) transparent;}
*::-webkit-scrollbar{width:8px;height:8px;}
*::-webkit-scrollbar-track{background:transparent;}
*::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border-radius:99px;
  border:2px solid transparent;background-clip:padding-box;}
*::-webkit-scrollbar-thumb:hover{background:var(--scroll-thumb-hover);background-clip:padding-box;}
*::-webkit-scrollbar-corner{background:transparent;}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:var(--r-sm);}
html{scroll-padding-top:calc(var(--topbar-h) + var(--space-2));}
html,body{height:100%;}
body{margin:0;color:var(--fg);background:var(--bg);
  font-family:system-ui,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:var(--text-14);line-height:1.5;-webkit-font-smoothing:antialiased;}
code,.mono{font-family:ui-monospace,"Cascadia Mono",Consolas,Menlo,monospace;
  font-size:var(--text-12);letter-spacing:.2px;}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none !important;transition-duration:0s !important;}
}
.skip{position:absolute;left:-999px;top:var(--space-1);z-index:30;background:var(--card);
  color:var(--fg);padding:var(--space-1) var(--space-2);border-radius:var(--r-sm);
  border:1px solid var(--line);}
.skip:focus{left:var(--space-2);}
.app{display:flex;min-height:100dvh;overflow:hidden;}
.sidebar{width:var(--sidebar-w);flex-shrink:0;background:var(--sidebar);
  border-right:1px solid var(--line-soft);display:flex;flex-direction:column;
  min-height:100dvh;position:sticky;top:0;z-index:8;}
.brand-block{display:flex;gap:var(--space-1);align-items:center;
  padding:var(--space-2);border-bottom:1px solid var(--line-soft);}
.logo{width:32px;height:32px;border-radius:var(--r-md);background:var(--accent-bg);
  border:1px solid var(--ok-line);display:grid;place-items:center;flex-shrink:0;color:var(--ok);}
.logo svg{width:18px;height:18px;display:block;}
.brand-copy{min-width:0;}
.brand-kicker{margin:0;font-size:10px;font-weight:700;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ok);}
.brand-name{margin:2px 0 0;font-size:var(--text-14);font-weight:600;color:var(--fg);}
.nav{display:flex;flex-direction:column;gap:4px;padding:var(--space-2);
  flex:1;min-height:0;}
.nav a{display:flex;align-items:center;gap:var(--space-1);min-height:40px;
  padding:0 10px;border-radius:var(--r-md);color:var(--muted);text-decoration:none;
  border:1px solid transparent;cursor:pointer;transition:background var(--dur) var(--ease),
  color var(--dur) var(--ease),border-color var(--dur) var(--ease);}
.nav a:hover{background:var(--panel3);color:var(--fg);}
.nav a[aria-current="page"]{background:var(--accent-bg);color:var(--fg);
  border-color:var(--ok-line);}
.nav .ico{width:16px;height:16px;flex-shrink:0;}
.nav-count{margin-left:auto;font-variant-numeric:tabular-nums;font-size:var(--text-12);
  color:var(--dim);background:var(--panel3);border-radius:99px;padding:1px 8px;}
.nav a[aria-current="page"] .nav-count{background:var(--ok-line);color:var(--fg);}
.sidebar-foot{padding:var(--space-2);border-top:1px solid var(--line-soft);
  display:flex;flex-direction:column;gap:var(--space-1);font-size:var(--text-12);}
.workspace{flex:1;min-width:0;min-height:100dvh;overflow:auto;background:var(--bg);}
.topbar{position:sticky;top:0;z-index:9;min-height:var(--topbar-h);
  display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-1) var(--space-2);
  padding:10px var(--space-3);background:var(--topbar);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line-soft);}
.topbar h1{margin:0;font-size:var(--text-20);font-weight:600;letter-spacing:.2px;}
.conn{display:inline-flex;align-items:center;gap:8px;min-height:32px;
  padding:0 10px;border-radius:99px;border:1px solid var(--line);background:var(--card);
  color:var(--muted);font-size:var(--text-12);}
.conn .ico{width:16px;height:16px;}
.conn-ok{color:var(--ok);border-color:var(--ok-line);background:var(--ok-bg);}
.conn-warn{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg);}
.conn-err{color:var(--err);border-color:var(--err-line);background:var(--err-bg);}
.conn-wait{color:var(--tip);border-color:var(--tip-line);background:var(--tip-bg);}
.top-meta{margin-left:auto;color:var(--dim);font-size:var(--text-12);
  font-variant-numeric:tabular-nums;}
main{padding:var(--space-3);display:flex;flex-direction:column;gap:var(--space-3);}
.section-head{display:flex;align-items:baseline;justify-content:space-between;
  gap:var(--space-2);margin:0 0 var(--space-2);}
.section-head h2{margin:0;font-size:var(--text-16);font-weight:600;}
.section-head p{margin:0;color:var(--dim);font-size:var(--text-12);}
.section-gap{margin-top:var(--space-3);}
.health{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:var(--space-1);}
.chip{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;
  border-radius:var(--r-md);border:1px solid var(--line-soft);background:var(--card);
  min-height:56px;transition:border-color var(--dur) var(--ease),background var(--dur) var(--ease);}
.chip .ico{width:18px;height:18px;margin-top:2px;flex-shrink:0;}
.chip b{display:block;font-size:var(--text-14);font-weight:600;}
.chip span{display:block;color:var(--dim);font-size:var(--text-12);margin-top:2px;}
.chip-ok{border-color:var(--ok-line);background:var(--ok-bg);color:var(--ok);}
.chip-ok span{color:var(--muted);}
.chip-warn{border-color:var(--warn-line);background:var(--warn-bg);color:var(--warn);}
.chip-warn span{color:var(--muted);}
.chip-err{border-color:var(--err-line);background:var(--err-bg);color:var(--err);}
.chip-err span{color:var(--muted);}
.chip-wait{border-color:var(--tip-line);background:var(--tip-bg);color:var(--tip);}
.chip-wait span{color:var(--muted);}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:var(--space-1);}
.metric{background:var(--card);border:1px solid var(--line-soft);border-radius:var(--r-md);
  padding:var(--space-2);min-height:88px;display:flex;flex-direction:column;gap:6px;
  box-shadow:var(--shadow);transition:border-color var(--dur) var(--ease),background var(--dur) var(--ease);}
.metric span{color:var(--dim);font-size:var(--text-12);}
.metric b{font-size:28px;line-height:1.1;font-weight:700;letter-spacing:-.4px;
  font-variant-numeric:tabular-nums;}
.metric i{font-style:normal;color:var(--muted);font-size:var(--text-12);
  font-variant-numeric:tabular-nums;}
.metric.bad{background:var(--err-bg);border-color:var(--err-line);}
.metric.bad b{color:var(--err);}
.metric.warn{background:var(--warn-bg);border-color:var(--warn-line);}
.metric.warn b{color:var(--warn);}
.card{background:var(--card);border:1px solid var(--line-soft);border-radius:var(--r-lg);
  overflow:hidden;box-shadow:var(--shadow);}
.card>h2,.card-head{margin:0;padding:12px var(--space-2);font-size:var(--text-14);
  font-weight:600;background:var(--panel);border-bottom:1px solid var(--line-soft);
  display:flex;justify-content:space-between;align-items:center;gap:var(--space-2);}
.card>h2 .muted,.card-head .muted{font-weight:400;font-size:var(--text-12);
  font-variant-numeric:tabular-nums;color:var(--dim);}
.card>.body{padding:var(--space-2);}
.scroll-sm{max-height:320px;overflow:auto;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,520px),1fr));
  gap:var(--space-2);}
.grid-single{grid-template-columns:1fr;}
.room{border:1px solid var(--line-soft);border-radius:var(--r-lg);background:var(--card);
  overflow:hidden;box-shadow:var(--shadow);}
.room>.head{padding:12px var(--space-2);background:var(--panel);
  border-bottom:1px solid var(--line-soft);display:flex;flex-wrap:wrap;gap:8px;
  align-items:center;justify-content:space-between;}
.room>.head .title{font-weight:700;font-size:var(--text-14);}
.badge{border-radius:99px;padding:2px 8px;font-size:var(--text-12);
  border:1px solid var(--line);background:var(--panel3);color:var(--muted);
  font-variant-numeric:tabular-nums;}
.badge.phase{color:var(--ok);border-color:var(--ok-line);background:var(--ok-bg);font-weight:600;}
.diag{display:flex;align-items:flex-start;gap:10px;padding:12px var(--space-2);
  border-bottom:1px solid var(--line-soft);font-size:var(--text-14);color:var(--muted);
  border-left:3px solid var(--dim);}
.diag .ico{width:18px;height:18px;flex-shrink:0;margin-top:2px;}
.diag .lv{font-weight:700;margin-right:8px;color:var(--fg);}
.diag.正常{background:var(--ok-bg);border-left-color:var(--ok);} .diag.正常 .lv,.diag.正常 .ico{color:var(--ok);}
.diag.提示{background:var(--tip-bg);border-left-color:var(--tip);} .diag.提示 .lv,.diag.提示 .ico{color:var(--tip);}
.diag.警告{background:var(--warn-bg);border-left-color:var(--warn);} .diag.警告 .lv,.diag.警告 .ico{color:var(--warn);}
.diag.错误{background:var(--err-bg);border-left-color:var(--err);} .diag.错误 .lv,.diag.错误 .ico{color:var(--err);}
.room.lv-警告{border-color:var(--warn-line);}
.room.lv-错误{border-color:var(--err-line);box-shadow:0 0 0 1px var(--err-line),var(--shadow);}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px var(--space-2);
  font-size:var(--text-14);padding:var(--space-2);border-bottom:1px solid var(--line-soft);}
.kv dt{color:var(--dim);white-space:nowrap;}
.kv dd{margin:0;word-break:break-word;overflow-wrap:anywhere;}
.room-sections{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));}
@media (max-width:960px){.room-sections{grid-template-columns:1fr;}}
.room-sections .sec-wide{grid-column:1/-1;}
.sec{border-top:1px solid var(--line-soft);}
.sec>h3{margin:0;}
.sec>h3>button{width:100%;min-height:40px;padding:8px 12px;font:inherit;font-size:var(--text-12);
  font-weight:600;color:var(--muted);background:var(--panel);border:0;cursor:pointer;
  display:flex;justify-content:space-between;align-items:center;text-align:left;
  transition:background var(--dur) var(--ease),color var(--dur) var(--ease);}
.sec>h3>button:hover{background:var(--panel3);color:var(--fg);}
.sec .chev{width:14px;height:14px;transition:opacity var(--dur) var(--ease);}
.sec.collapsed .chev{opacity:.45;}
.sec.collapsed .inner{display:none;}
.sec>.inner{padding:12px;max-height:280px;overflow:auto;}
.table-wrap{overflow-x:auto;margin:0;}
table.data{width:100%;border-collapse:collapse;font-size:var(--text-12);table-layout:fixed;}
table.data th,table.data td{border-bottom:1px solid var(--line-soft);padding:8px;
  text-align:left;vertical-align:top;word-break:break-word;overflow-wrap:anywhere;}
table.data th{color:var(--dim);font-weight:600;position:sticky;top:0;background:var(--card);
  z-index:1;white-space:nowrap;border-bottom:1px solid var(--line);}
table.data td{font-variant-numeric:tabular-nums;}
tr:nth-child(even) td{background:var(--row-even);}
tr.dead td{color:var(--dim);text-decoration:line-through;background:transparent;}
tr.pend td{background:var(--warn-bg);box-shadow:inset 2px 0 0 var(--warn);}
tr:hover td{background:var(--row-hover);}
ul.log{list-style:none;margin:0;padding:0;font-size:var(--text-12);}
ul.log li{padding:6px;margin:0 -6px;border-bottom:1px solid var(--line-soft);
  display:flex;gap:8px;align-items:flex-start;border-radius:var(--r-sm);
  transition:background var(--dur) var(--ease);}
ul.log li:hover{background:var(--panel);}
ul.log li:last-child{border-bottom:0;}
ul.log li>span:last-child,.log-text{flex:1;min-width:0;word-break:break-word;
  overflow-wrap:anywhere;line-height:1.5;}
ul.log time{color:var(--dim);white-space:nowrap;flex-shrink:0;
  font-family:ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums;}
.tag{font-size:10px;border-radius:var(--r-sm);padding:1px 6px;white-space:nowrap;
  flex-shrink:0;border:1px solid var(--line);color:var(--muted);font-weight:600;}
.tag.evt{color:var(--evt);border-color:var(--evt-line);background:var(--evt-bg);}
.tag.trc{color:var(--ok);border-color:var(--ok-line);background:var(--ok-bg);}
.st-sent{color:var(--ok);} .st-failed{color:var(--err);font-weight:700;}
.st-waiting{color:var(--warn);} .st-dead{color:var(--dim);text-decoration:line-through;}
.st-retry{color:var(--warn);} .st-pending{color:var(--muted);} .st-sending{color:var(--tip);}
.count{font-variant-numeric:tabular-nums;font-weight:700;}
.alert{display:flex;gap:10px;align-items:flex-start;background:var(--err-bg);
  border:1px solid var(--err-line);border-radius:var(--r-md);padding:12px var(--space-2);
  color:var(--fg);box-shadow:var(--shadow);}
.alert:focus{outline:2px solid var(--focus);outline-offset:2px;}
.alert .ico{width:18px;height:18px;color:var(--err);flex-shrink:0;margin-top:2px;}
.alert h2{margin:0 0 4px;font-size:var(--text-14);color:var(--err);}
.alert p{margin:0;color:var(--muted);font-size:var(--text-14);}
.muted{color:var(--muted);}
.wait{color:var(--warn);}
.empty{color:var(--dim);padding:var(--space-2) 0;font-size:var(--text-14);}
.empty-card{border:1px dashed var(--line);border-radius:var(--r-lg);background:var(--card);
  padding:var(--space-4) var(--space-3);text-align:left;}
.empty-card h3{margin:0 0 8px;font-size:var(--text-16);}
.empty-card p{margin:0;color:var(--muted);max-width:42em;}
.pill{display:inline-block;background:var(--panel3);border:1px solid var(--line);
  border-radius:99px;padding:1px 8px;margin:2px 4px 2px 0;font-size:var(--text-12);
  font-variant-numeric:tabular-nums;}
.vote-meta{margin-bottom:8px;font-size:var(--text-12);}
.vote-meta>div{margin-top:6px;}
.panel-label{margin:0 0 6px;font-size:var(--text-12);color:var(--dim);}
.panel-label.wait{color:var(--warn);}
.panel-gap{margin-top:var(--space-2);}
.ico{display:block;fill:none;stroke:currentColor;stroke-width:1.75;
  stroke-linecap:round;stroke-linejoin:round;}
html{overflow-x:hidden;}
@media (max-width:900px){
  body{font-size:var(--text-16);}
  .app{flex-direction:column;overflow:visible;}
  .sidebar{width:100%;min-height:0;position:sticky;top:0;z-index:10;}
  .brand-block{padding:10px var(--space-2);}
  .nav{flex-direction:row;overflow-x:auto;padding:8px var(--space-2) var(--space-2);
    flex:none;}
  .nav a{flex:0 0 auto;}
  .sidebar-foot{display:none;}
  .workspace{min-height:0;overflow:visible;}
  .topbar{position:sticky;top:76px;}
  main{padding:var(--space-2);}
  .metrics,.health{grid-template-columns:1fr;}
}
</style>
</head>
<body>
<a class="skip" href="#main">跳到主内容</a>
<div class="app">
<aside class="sidebar" aria-label="检测台导航">
  <div class="brand-block">
    <div class="logo" aria-hidden="true">
      <svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
    </div>
    <div class="brand-copy">
      <p class="brand-kicker">狼人杀</p>
      <p class="brand-name">运行检测台</p>
    </div>
  </div>
  <nav class="nav" aria-label="页内分区">
    <a href="#overview" id="nav-overview">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/></svg>
      总览
    </a>
    <a href="#rooms" id="nav-rooms-link">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><path d="M4 18c.6-2.6 2.6-4 5-4s4.4 1.4 5 4"/><circle cx="17" cy="9" r="2.4"/><path d="M20.5 18c-.4-1.8-1.8-3-3.5-3"/></svg>
      对局 <span class="nav-count" id="nav-rooms">0</span>
    </a>
    <a href="#logs" id="nav-logs">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6h12M6 12h12M6 18h8"/></svg>
      流程日志
    </a>
    <a href="#failed" id="nav-failed-link">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4l9 16H3L12 4z"/><path d="M12 10v5"/><circle cx="12" cy="17.2" r=".8" fill="currentColor" stroke="none"/></svg>
      异常投递 <span class="nav-count" id="nav-failed">0</span>
    </a>
  </nav>
  <div class="sidebar-foot">
    <div id="side-gw" class="muted">网关：读取中</div>
    <div id="side-sent" class="muted">最近成功投递：—</div>
  </div>
</aside>
<div class="workspace">
  <header class="topbar">
    <h1>运行检测台</h1>
    <div id="conn" class="conn conn-wait" role="status" aria-atomic="true">
      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
      <span>正在连接实时通道</span>
    </div>
    <span class="top-meta" id="s-time">快照时间未到达</span>
  </header>
  <main id="main">
    <div id="errors"></div>
    <section id="overview" aria-labelledby="overview-title">
      <div class="section-head">
        <h2 id="overview-title">服务健康</h2>
        <p>只读快照字段，不探测数据库</p>
      </div>
      <div class="health" id="health">
        <div class="chip chip-wait" id="h-gw"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><div><b>网关</b><span>读取中</span></div></div>
        <div class="chip chip-wait" id="h-ch"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><div><b>实时通道</b><span>正在连接</span></div></div>
        <div class="chip chip-wait" id="h-fail"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><div><b>失败投递</b><span>读取中</span></div></div>
      </div>
      <div class="section-head section-gap">
        <h2 id="metrics-title">总览指标</h2>
        <p id="s-lastsent">最近成功投递：—</p>
      </div>
      <div class="metrics">
        <article class="metric"><span>活跃对局</span><b id="s-rooms">0</b></article>
        <article class="metric"><span>玩家（存活 <i id="s-alive">0</i>）</span><b id="s-players">0</b></article>
        <article class="metric"><span>排队中</span><b id="s-queue">0</b></article>
        <article class="metric" id="st-waiting"><span>等待锚点</span><b id="s-waiting">0</b></article>
        <article class="metric" id="st-failed"><span>失败投递</span><b id="s-failed">0</b></article>
        <article class="metric"><span>已判死</span><b id="s-dead">0</b></article>
      </div>
    </section>
    <section id="rooms" aria-labelledby="rooms-title">
      <div class="section-head">
        <h2 id="rooms-title">对局</h2>
        <p>先看诊断，再展开明细</p>
      </div>
      <div id="rooms-wrap">
        <div id="rooms-grid" class="grid">
          <div class="empty-card" id="rooms-placeholder">
            <h3>正在读取对局</h3>
            <p>正在连接观测接口。有对局时这里会出现诊断卡；没有对局时会留下说明，总览和日志仍可看。</p>
          </div>
        </div>
      </div>
    </section>
    <section id="logs" aria-labelledby="logs-title">
      <div class="card">
        <h2 id="logs-title">全局流程日志<span class="muted" id="trace-sum"></span></h2>
        <div class="body scroll-sm"><ul class="log" id="gtrace"><li class="empty">正在读取流程埋点。</li></ul></div>
      </div>
    </section>
    <section id="failed" aria-labelledby="failed-title">
      <div class="card">
        <h2 id="failed-title">异常投递<span class="muted">定位消息发出去却没反应</span></h2>
        <div class="body" id="gfailed"><div class="empty">正在读取投递队列。</div></div>
      </div>
    </section>
  </main>
</div>
</div>
<script>
var state = { snap:null, live:[], recvAt:0, lastErrKey:'', channelKind:'wait', channelText:'正在连接实时通道' };
var ICO = {
  ok:'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8 12.4l2.6 2.6L16.4 9"/></svg>',
  tip:'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><circle cx="12" cy="8" r=".8" fill="currentColor" stroke="none"/></svg>',
  warn:'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4l9 16H3L12 4z"/><path d="M12 10v5"/><circle cx="12" cy="17.2" r=".8" fill="currentColor" stroke="none"/></svg>',
  err:'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/></svg>',
  wait:'<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  chev:'<svg class="ico chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>'
};
var LV_ICO = { '正常':'ok', '提示':'tip', '警告':'warn', '错误':'err' };
var esc = function(v){
  if(v===null||v===undefined) return '';
  return String(v).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});
};
var t = function(iso){
  if(!iso) return '—';
  var d = new Date(iso);
  if(isNaN(d.getTime())) return String(iso);
  var p = function(n){ return (n<10?'0':'')+n; };
  return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
};
var full = function(iso){
  if(!iso) return '—';
  var d = new Date(iso);
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleString('zh-CN');
};
function setChip(id, kind, title, detail){
  var el = document.getElementById(id);
  if(!el) return;
  el.className = 'chip chip-'+kind;
  el.innerHTML = ICO[kind]+'<div><b>'+esc(title)+'</b><span>'+esc(detail)+'</span></div>';
}
function setConn(ok, text, kind){
  var k = kind || (ok ? 'ok' : 'warn');
  state.channelKind = k;
  state.channelText = text;
  var el = document.getElementById('conn');
  el.className = 'conn conn-'+k;
  el.innerHTML = ICO[k]+'<span>'+esc(text)+'</span>';
  var label = {ok:'已连接', warn:'降级或重试', err:'已断开', wait:'正在连接'}[k] || text;
  setChip('h-ch', k, '实时通道', label+'。'+text);
}
function renderErrors(errs){
  var box = document.getElementById('errors');
  if(!errs.length){ box.innerHTML=''; state.lastErrKey=''; return; }
  var key = errs.join('\n');
  box.innerHTML = '<div class="alert" id="obs-alert" role="alert" tabindex="-1">'+
    ICO.err+'<div><h2>观测读取失败</h2><p>'+errs.map(esc).join('；')+'</p></div></div>';
  if(key !== state.lastErrKey){
    state.lastErrKey = key;
    var a = document.getElementById('obs-alert');
    if(a) a.focus();
  }
}
function renderTop(s){
  var o = s['总览']||{}, b = s['机器人主持']||{};
  document.getElementById('s-rooms').textContent = o['活跃对局']||0;
  document.getElementById('s-players').textContent = o['总玩家数']||0;
  document.getElementById('s-alive').textContent = o['存活玩家数']||0;
  document.getElementById('s-queue').textContent = b['排队中']||0;
  var waiting = b['等待锚点']||0, failed = b['失败投递数']||0;
  document.getElementById('s-waiting').textContent = waiting;
  document.getElementById('s-failed').textContent = failed;
  document.getElementById('st-waiting').className = 'metric' + (waiting ? ' warn' : '');
  document.getElementById('st-failed').className = 'metric' + (failed ? ' bad' : '');
  document.getElementById('s-dead').textContent = b['已判死']||0;
  document.getElementById('nav-rooms').textContent = o['活跃对局']||0;
  document.getElementById('nav-failed').textContent = failed;
  var gwOk = !!b['网关就绪'];
  setChip('h-gw', gwOk?'ok':'err', gwOk?'网关就绪':'网关未就绪',
    gwOk?'机器人主持通道可用':'机器人主持通道离线，群消息可能发不出去');
  document.getElementById('side-gw').textContent = gwOk ? '网关：就绪' : '网关：未就绪';
  setChip('h-fail', failed?'err':'ok', failed?'存在失败投递':'没有失败投递',
    failed?('当前有 '+failed+' 条失败或重试中的出站消息'):'出站队列没有失败或重试记录');
  var q = b['出站队列']||{}, qs = [];
  for(var k in q){ qs.push(k+':'+q[k]); }
  var sent = '最近成功投递：'+full(b['最近成功投递'])+(qs.length?'　队列 '+qs.join(' / '):'');
  document.getElementById('s-lastsent').textContent = sent;
  document.getElementById('side-sent').textContent = sent;
  document.getElementById('s-time').textContent = '快照 '+full(s['生成时间']);
  renderErrors(s['错误']||[]);
  setChip('h-ch', state.channelKind, '实时通道', state.channelText);
}
function tableOf(cols, rows, rowClass, tableClass){
  if(!rows || !rows.length) return '<div class="empty">暂无数据</div>';
  var h = '<div class="table-wrap"><table class="data'+(tableClass?' '+tableClass:'')+'"><thead><tr>';
  cols.forEach(function(c){ h += '<th>'+esc(c[0])+'</th>'; });
  h += '</tr></thead><tbody>';
  rows.forEach(function(r){
    h += '<tr class="'+(rowClass?rowClass(r):'')+'">';
    cols.forEach(function(c){ h += '<td>'+c[1](r)+'</td>'; });
    h += '</tr>';
  });
  return h + '</tbody></table></div>';
}
function toggleSec(btn){
  var p = btn.closest('.sec');
  p.classList.toggle('collapsed');
  btn.setAttribute('aria-expanded', p.classList.contains('collapsed') ? 'false' : 'true');
}
function sec(title, inner, open, extraClass){
  var closed = open === false;
  return '<div class="sec'+(extraClass?' '+extraClass:'')+(closed?' collapsed':'')+'">'+
    '<h3><button type="button" aria-expanded="'+(closed?'false':'true')+'" onclick="toggleSec(this)">'+
    esc(title)+ICO.chev+'</button></h3><div class="inner">'+inner+'</div></div>';
}
function playerTable(r){
  return tableOf([
    ['座位', function(p){ return esc(p['座位']); }],
    ['QQ号', function(p){ return '<span class="mono">'+esc(p['QQ号'])+'</span>'; }],
    ['昵称', function(p){ return esc(p['昵称'])+
        (p['标记']&&p['标记'].length?' '+p['标记'].map(function(x){
          return '<span class="pill">'+esc(x)+'</span>';}).join(''):''); }],
    ['身份', function(p){ return esc(p['身份']); }],
    ['阵营', function(p){ return esc(p['阵营']); }],
    ['状态', function(p){ return esc(p['状态'])+(p['存活']?'':' <span class="muted">('+
        esc(p['死因'])+')</span>'); }],
    ['已执行', function(p){ return esc(p['已执行操作']); }],
    ['待执行', function(p){ return '<span class="'+
        (/待|等待/.test(p['待执行操作'])?'wait':'muted')+'">'+esc(p['待执行操作'])+'</span>'; }],
    ['投给', function(p){ return esc(p['投票给']); }]
  ], r['玩家'], function(p){
    if(!p['存活']) return 'dead';
    return /^待/.test(p['待执行操作']) ? 'pend' : '';
  }, 'players');
}
function votePanel(v){
  if(!v) return '<div class="empty">暂无数据</div>';
  var h = '<div class="vote-meta muted">第 '+esc(v['轮次'])+' 轮 · 历史 '+esc(v['历史轮次'])+' 轮</div>';
  h += tableOf([['投票人',function(x){return esc(x['投票人']);}],
                ['投给',function(x){return esc(x['投给']);}],
                ['票重',function(x){return esc(x['票重']);}]], v['票型']);
  h += '<div class="vote-meta"><div><b>票数统计：</b>'+
    ((v['票数统计']&&v['票数统计'].length)
      ? v['票数统计'].map(function(x){ return '<span class="pill">'+esc(x['候选'])+' × '+
          esc(x['票数'])+'</span>'; }).join('') : '<span class="muted">无</span>')+'</div>';
  h += '<div><b>弃票：</b>'+((v['弃票']||[]).map(esc).join('、')||'<span class="muted">无</span>')+'</div>';
  h += '<div><b>未投票：</b><span class="wait">'+
    ((v['未投票']||[]).map(esc).join('、')||'<span class="muted">无</span>')+'</span></div></div>';
  return h;
}
function nightPanel(n){
  if(!n) return '<div class="empty">暂无数据</div>';
  var h = '<p class="panel-label">已提交（'+(n['已提交']||[]).length+'）</p>';
  h += tableOf([['玩家',function(x){return esc(x['玩家']);}],
                ['身份',function(x){return esc(x['身份']);}],
                ['动作',function(x){return esc(x['动作']);}]], n['已提交']);
  h += '<p class="panel-label wait panel-gap">未提交（'+(n['未提交']||[]).length+'）</p>';
  h += tableOf([['玩家',function(x){return esc(x['玩家']);}],
                ['身份',function(x){return esc(x['身份']);}],
                ['应做',function(x){return esc(x['应做']);}]], n['未提交']);
  return h;
}
function hasVoteData(v){
  if(!v) return false;
  return (v['票型']||[]).length || (v['票数统计']||[]).length ||
    (v['弃票']||[]).length || (v['未投票']||[]).length;
}
function hasNightData(n){
  if(!n) return false;
  return (n['已提交']||[]).length || (n['未提交']||[]).length;
}
function mergedLog(room){
  var items = [];
  (room['系统事件']||[]).forEach(function(e){
    items.push({ ts:e['时间'], kind:'evt', label:e['类型'],
      text:(e['公开']?'':'[私聊 '+(e['定向玩家']||'未知玩家')+'] ')+(e['内容']||'') });
  });
  var seen = {};
  (room['流程日志']||[]).concat(state.live.filter(function(x){
    return x['房间'] === room['房间号']; })).forEach(function(x){
    if(seen[x['序号']]) return; seen[x['序号']] = 1;
    var extra = '';
    if(x['附加'] && Object.keys(x['附加']).length) extra = ' '+JSON.stringify(x['附加']);
    items.push({ ts:x['时间'], kind:'trc', label:x['阶段'], text:(x['详情']||'')+extra });
  });
  items.sort(function(a,b){ return (b.ts||'').localeCompare(a.ts||''); });
  if(!items.length) return '<div class="empty">暂无记录</div>';
  return '<ul class="log">'+items.slice(0,80).map(function(i){
    return '<li><time>'+t(i.ts)+'</time><span class="tag '+i.kind+'">'+esc(i.label)+
      '</span><span class="log-text">'+esc(i.text)+'</span></li>'; }).join('')+'</ul>';
}
var DSTATUS = {pending:'待发送', sending:'发送中', sent:'已送达', failed:'失败',
  retry:'重试中', waiting:'等待回复锚点', dead:'已判死'};
function deliveryTable(rows){
  return tableOf([
    ['时间', function(d){ return t(d['创建时间']); }],
    ['状态', function(d){ var s=d['状态']||'';
      return '<span class="st-'+esc(s)+'">'+esc(DSTATUS[s]||s)+'</span>'; }],
    ['序号', function(d){ return esc(d['消息序号']); }],
    ['锚点', function(d){ return '<span class="mono">'+esc((d['回复锚点']||d['事件号']||'—').slice(0,10))+'</span>'; }],
    ['内容', function(d){ return esc(d['内容']||''); }],
    ['次数', function(d){ return esc(d['尝试次数']); }],
    ['错误', function(d){ return '<span class="st-failed">'+esc(d['最近错误']||'')+'</span>'; }]
  ], rows, null, 'delivery');
}
function roomCard(r){
  var d = r['诊断']||{};
  var lv = d['级别'] || '正常';
  var ico = ICO[LV_ICO[lv]||'tip'];
  var h = '<article class="room lv-'+esc(lv)+'" id="room-'+esc(r['房间号'])+'">';
  h += '<div class="head"><span class="title">群 '+esc(r['群号'])+'</span>'+
    '<span><span class="badge phase">'+esc(r['房间状态'])+'</span> '+
    '<span class="badge">第 '+esc(r['当前轮次'])+' 轮</span> '+
    '<span class="badge">v'+esc(r['状态版本'])+'</span> '+
    '<span class="badge">'+esc(r['人数']['存活'])+'/'+esc(r['人数']['总人数'])+' 存活</span></span></div>';
  h += '<div class="diag '+esc(lv)+'">'+ico+'<div><span class="lv">'+
    esc(lv)+'｜'+esc(d['结论']||'')+'</span>'+esc(d['说明']||'')+'</div></div>';
  h += '<dl class="kv">'+
    '<dt>发起人</dt><dd>'+esc(r['发起人'])+'</dd>'+
    '<dt>当前流程</dt><dd>'+esc(r['当前流程'])+'</dd>'+
    '<dt>下一步</dt><dd>'+esc(r['下一步'])+'</dd>'+
    '<dt>阶段截止</dt><dd>'+full(r['阶段截止'])+
      ' <b class="count" data-deadline="'+esc(r['剩余秒数'])+'">—</b></dd>'+
    '<dt>等待谁</dt><dd>'+((r['等待谁']||[]).length
        ? '<span class="wait">'+r['等待谁'].map(function(w){
            return esc(w['玩家'])+'（'+esc(w['缺少操作'])+'）'; }).join('；')+'</span>'
        : '<span class="muted">无人阻塞</span>')+'</dd>'+
    '</dl>';
  h += '<div class="room-sections">';
  h += sec('玩家（'+(r['玩家']||[]).length+'）', playerTable(r), true);
  h += sec('投票面板', votePanel(r['投票']), hasVoteData(r['投票']));
  h += sec('夜间行动', nightPanel(r['夜间行动']), hasNightData(r['夜间行动']));
  h += sec('系统事件 / 流程日志', mergedLog(r), true, 'sec-wide');
  h += sec('最近消息（出站投递）', deliveryTable(r['最近消息']), true, 'sec-wide');
  if((r['异常投递']||[]).length){
    h += sec('本房间异常投递（'+r['异常投递'].length+'）', deliveryTable(r['异常投递']), true, 'sec-wide');
  }
  h += '</div>';
  return h + '</article>';
}
function render(){
  var s = state.snap; if(!s) return;
  renderTop(s);
  var rooms = s['对局']||[];
  var grid = document.getElementById('rooms-grid');
  grid.className = 'grid' + (rooms.length === 1 ? ' grid-single' : '');
  if(!rooms.length){
    grid.innerHTML = '<div class="empty-card"><h3>当前没有正在进行的对局</h3>'+
      '<p>在群里发起一局后，诊断卡会自动出现在这里。总览指标、流程日志和异常投递仍可继续查看。</p></div>';
  } else {
    grid.innerHTML = rooms.map(roomCard).join('');
  }
  var ts = s['追踪摘要']||{};
  document.getElementById('trace-sum').textContent =
    '累计 '+(ts['总条数']||0)+' 条 / 缓冲 '+(ts['缓冲条数']||0)+
    ' / 订阅 '+(ts['订阅者']||0)+' / 丢弃 '+(ts['推送丢弃']||0);
  renderGlobalTrace();
  document.getElementById('gfailed').innerHTML = (s['异常投递']||[]).length
    ? tableOf([
        ['时间', function(d){ return t(d['更新时间']); }],
        ['房间', function(d){ return '<span class="mono">'+esc(String(d['房间']||'—').slice(0,8))+'</span>'; }],
        ['状态', function(d){ var s2=d['状态']||'';
          return '<span class="st-'+esc(s2)+'">'+esc(DSTATUS[s2]||s2)+'</span>'; }],
        ['次数', function(d){ return esc(d['尝试次数']); }],
        ['下次重试', function(d){ return t(d['下次重试']); }],
        ['内容', function(d){ return esc(d['内容']||''); }],
        ['最近错误', function(d){ return '<span class="st-failed">'+esc(d['最近错误']||'')+'</span>'; }]
      ], s['异常投递'], null, 'delivery')
    : '<div class="empty">没有失败或重试中的投递。</div>';
  tick();
}
function renderGlobalTrace(){
  var base = (state.snap && state.snap['全局流程日志']) || [];
  var seen = {}, items = [];
  state.live.concat(base).forEach(function(x){
    if(seen[x['序号']]) return; seen[x['序号']] = 1; items.push(x);
  });
  items.sort(function(a,b){ return (b['序号']||0) - (a['序号']||0); });
  document.getElementById('gtrace').innerHTML = items.length
    ? items.slice(0,120).map(function(x){
        var extra = (x['附加'] && Object.keys(x['附加']).length)
          ? ' <span class="muted">'+esc(JSON.stringify(x['附加']))+'</span>' : '';
        return '<li><time>'+t(x['时间'])+'</time><span class="tag trc">'+esc(x['阶段'])+
          '</span><span class="muted">'+esc(x['房间']||'全局')+'</span><span class="log-text">'+
          esc(x['详情'])+extra+'</span></li>'; }).join('')
    : '<li class="empty">暂无埋点。</li>';
}
function tick(){
  if(!state.snap) return;
  var elapsed = (Date.now() - state.recvAt)/1000;
  var nodes = document.querySelectorAll('[data-deadline]');
  for(var i=0;i<nodes.length;i++){
    var raw = nodes[i].getAttribute('data-deadline');
    if(raw === 'null' || raw === '' || raw === 'undefined'){ nodes[i].textContent = '（无截止）'; continue; }
    var left = parseFloat(raw) - elapsed;
    nodes[i].textContent = left > 0
      ? '（剩余 '+Math.floor(left)+' 秒）' : '（已超时 '+Math.floor(-left)+' 秒）';
    nodes[i].style.color = left > 0 ? 'var(--ok)' : 'var(--err)';
  }
}
setInterval(tick, 1000);
function bindNav(){
  var links = document.querySelectorAll('.nav a');
  var root = document.querySelector('.workspace');
  if(!('IntersectionObserver' in window) || !root) return;
  var io = new IntersectionObserver(function(entries){
    entries.forEach(function(e){
      if(!e.isIntersecting) return;
      links.forEach(function(a){
        if(a.getAttribute('href') === '#'+e.target.id) a.setAttribute('aria-current','page');
        else a.removeAttribute('aria-current');
      });
    });
  }, { root: root, rootMargin:'-15% 0px -65% 0px', threshold:0.05 });
  ['overview','rooms','logs','failed'].forEach(function(id){
    var el = document.getElementById(id);
    if(el) io.observe(el);
  });
}
var ws = null, retry = 0, poller = null, wsTimer = null;
function applySnapshot(d, live){
  state.snap = d;
  state.recvAt = Date.now();
  if(live !== undefined) state.live = live;
  render();
}
function fetchSnapshot(onOk, onErr){
  fetch('/api/observe').then(function(r){
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }).then(onOk).catch(onErr || function(){
    setConn(false, '观测接口不可达，正在重试', 'err');
    renderErrors(['观测读取失败：接口不可达。页面会自动重试，也可刷新浏览器。']);
  });
}
function connect(){
  fetchSnapshot(function(d){
    applySnapshot(d);
    setConn(false, '正在建立实时连接', 'wait');
  });
  var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  try { ws = new WebSocket(proto + location.host + '/ws/observe'); }
  catch(e){ startPolling(); return; }
  if(wsTimer) clearTimeout(wsTimer);
  wsTimer = setTimeout(function(){
    if(!ws || ws.readyState === WebSocket.CONNECTING){
      setConn(false, '实时连接超时，已改为轮询刷新', 'warn');
      try{ ws.close(); }catch(e){}
      startPolling();
    }
  }, 4000);
  ws.onopen = function(){
    if(wsTimer) clearTimeout(wsTimer);
    retry = 0;
    stopPolling();
    setConn(true, '实时连接已建立', 'ok');
  };
  ws.onmessage = function(ev){
    var msg;
    try { msg = JSON.parse(ev.data); } catch(e){ return; }
    if(msg['类型'] === '快照'){
      applySnapshot(msg['数据']);
    } else if(msg['类型'] === '追踪'){
      state.live.unshift(msg['数据']);
      if(state.live.length > 300) state.live.length = 300;
      renderGlobalTrace();
    }
  };
  ws.onclose = function(){
    if(wsTimer) clearTimeout(wsTimer);
    setConn(false, '实时连接已断开，正在重连', 'warn');
    retry = Math.min(retry + 1, 10);
    startPolling();
    setTimeout(connect, Math.min(1000 * retry, 8000));
  };
  ws.onerror = function(){ try{ ws.close(); }catch(e){} };
}
function startPolling(){
  if(poller) return;
  poller = setInterval(function(){
    fetchSnapshot(function(d){
      applySnapshot(d);
      if(!ws || ws.readyState !== WebSocket.OPEN){
        setConn(false, '实时通道不可用，正在以轮询刷新', 'warn');
      }
    });
  }, 2000);
}
function stopPolling(){ if(poller){ clearInterval(poller); poller = null; } }
bindNav();
connect();
</script>
</body>
</html>
"""
