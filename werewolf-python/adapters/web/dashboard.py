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
    """QQ 的 openid 是 32 位十六进制，观测页只留前 8 位方便肉眼比对。"""
    if not user_id:
        return "—"
    text = str(user_id)
    return text if len(text) <= 10 else f"{text[:8]}…"


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
        "显示名": player.public_name,
        "用户号": _short_id(player.user_id),
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
    return {
        "房间号": room.session_id,
        "群号": room.session_id,
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
<title>狼人杀·后台实时观测</title>
<style>
:root{
  --bg:#0d1117; --panel:#151b23; --panel2:#1c232d; --line:#2a3341;
  --fg:#e6edf3; --muted:#8b98a9; --accent:#58a6ff;
  --ok:#3fb950; --warn:#d29922; --err:#f85149; --tip:#58a6ff;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;
  font-size:13px;line-height:1.5;}
code,.mono{font-family:Consolas,"Cascadia Mono",Menlo,monospace;}
header{position:sticky;top:0;z-index:9;background:#0d1117ee;backdrop-filter:blur(6px);
  border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;
  gap:14px;align-items:center;}
header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.5px;}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;}
.dot.on{background:var(--ok);box-shadow:0 0 6px var(--ok);}
.dot.off{background:var(--err);box-shadow:0 0 6px var(--err);}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:6px;
  padding:5px 10px;display:flex;gap:8px;align-items:baseline;}
.stat b{font-size:17px;font-weight:700;}
.stat span{color:var(--muted);font-size:12px;}
.stat.bad b{color:var(--err);}
main{padding:14px 16px 40px;display:flex;flex-direction:column;gap:14px;}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;}
.card>h2{margin:0;padding:8px 12px;font-size:13px;font-weight:600;background:var(--panel2);
  border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;}
.card>.body{padding:10px 12px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:10px;}
.room{border:1px solid var(--line);border-radius:8px;background:var(--panel);}
.room>.head{padding:9px 12px;background:var(--panel2);border-bottom:1px solid var(--line);
  display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;}
.room>.head .title{font-weight:700;font-size:14px;}
.badge{border-radius:4px;padding:1px 7px;font-size:12px;border:1px solid var(--line);
  background:#222b36;color:var(--muted);}
.badge.phase{color:var(--accent);border-color:#2b4b70;background:#12243a;}
.diag{padding:8px 12px;border-bottom:1px solid var(--line);font-size:12.5px;}
.diag .lv{font-weight:700;margin-right:8px;}
.diag.正常{background:#0f2417;} .diag.正常 .lv{color:var(--ok);}
.diag.提示{background:#0f1f33;} .diag.提示 .lv{color:var(--tip);}
.diag.警告{background:#2a2211;} .diag.警告 .lv{color:var(--warn);}
.diag.错误{background:#2d1416;} .diag.错误 .lv{color:var(--err);}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:12.5px;padding:8px 12px;}
.kv dt{color:var(--muted);white-space:nowrap;}
.kv dd{margin:0;word-break:break-all;}
.sec{border-top:1px solid var(--line);}
.sec>h3{margin:0;padding:6px 12px;font-size:12px;color:var(--muted);font-weight:600;
  background:#131922;cursor:pointer;user-select:none;display:flex;justify-content:space-between;}
.sec>.inner{padding:8px 12px;max-height:340px;overflow:auto;}
table{width:100%;border-collapse:collapse;font-size:12px;}
th,td{border-bottom:1px solid var(--line);padding:3px 6px;text-align:left;vertical-align:top;}
th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel);}
tr.dead td{color:#6b7683;text-decoration:line-through;}
tr.pend td{background:#1f1a0d;}
ul.log{list-style:none;margin:0;padding:0;font-size:12px;}
ul.log li{padding:3px 0;border-bottom:1px solid #222a35;display:flex;gap:8px;}
ul.log time{color:var(--muted);white-space:nowrap;font-family:Consolas,monospace;}
.tag{font-size:11px;border-radius:3px;padding:0 5px;white-space:nowrap;
  border:1px solid var(--line);color:var(--muted);}
.tag.evt{color:#a371f7;border-color:#3c2d5c;}
.tag.trc{color:#3fb950;border-color:#1f4227;}
.st-sent{color:var(--ok);} .st-failed{color:var(--err);font-weight:700;}
.st-retry{color:var(--warn);} .st-pending{color:var(--muted);} .st-sending{color:var(--tip);}
.count{font-variant-numeric:tabular-nums;}
.err-box{background:#2d1416;border:1px solid #5c2226;border-radius:6px;padding:8px 10px;
  color:#ffb4ae;font-size:12.5px;margin-bottom:10px;}
.muted{color:var(--muted);}
.wait{color:var(--warn);}
.empty{color:var(--muted);padding:6px 0;}
.pill{display:inline-block;background:#222b36;border:1px solid var(--line);border-radius:10px;
  padding:0 7px;margin:1px 3px 1px 0;font-size:11.5px;}
</style>
</head>
<body>
<header>
  <h1>狼人杀 · 后台实时观测</h1>
  <span id="conn"><span class="dot off"></span>连接中…</span>
  <span class="stat"><b id="s-rooms">0</b><span>活跃对局</span></span>
  <span class="stat"><b id="s-players">0</b><span>玩家（存活 <i id="s-alive">0</i>）</span></span>
  <span class="stat"><b id="s-queue">0</b><span>排队中</span></span>
  <span class="stat bad"><b id="s-failed">0</b><span>失败投递</span></span>
  <span class="stat"><b id="s-gw">—</b><span>网关</span></span>
  <span class="muted" id="s-lastsent">最近成功投递：—</span>
  <span class="muted" id="s-time">—</span>
</header>
<main>
  <div id="errors"></div>
  <div id="rooms" class="grid"></div>
  <div class="card">
    <h2>全局流程日志（TRACE 实时埋点）<span class="muted" id="trace-sum"></span></h2>
    <div class="body" style="max-height:300px;overflow:auto"><ul class="log" id="gtrace"></ul></div>
  </div>
  <div class="card">
    <h2>异常投递（failed / retry）<span class="muted">定位「消息发出去了却没反应」的第一现场</span></h2>
    <div class="body" style="max-height:320px;overflow:auto"><div id="gfailed"></div></div>
  </div>
</main>
<script>
var state = { snap:null, live:[], recvAt:0 };
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

function setConn(ok, text){
  document.getElementById('conn').innerHTML =
    '<span class="dot '+(ok?'on':'off')+'"></span>'+esc(text);
}

function renderTop(s){
  var o = s['总览']||{}, b = s['机器人主持']||{};
  document.getElementById('s-rooms').textContent = o['活跃对局']||0;
  document.getElementById('s-players').textContent = o['总玩家数']||0;
  document.getElementById('s-alive').textContent = o['存活玩家数']||0;
  document.getElementById('s-queue').textContent = b['排队中']||0;
  document.getElementById('s-failed').textContent = b['失败投递数']||0;
  document.getElementById('s-gw').textContent = b['网关就绪'] ? '就绪' : '离线';
  document.getElementById('s-gw').style.color = b['网关就绪'] ? 'var(--ok)' : 'var(--err)';
  var q = b['出站队列']||{}, qs = [];
  for(var k in q){ qs.push(k+':'+q[k]); }
  document.getElementById('s-lastsent').textContent =
    '最近成功投递：'+full(b['最近成功投递'])+(qs.length?'　队列 '+qs.join(' / '):'');
  document.getElementById('s-time').textContent = '快照 '+t(s['生成时间']);
  var errs = s['错误']||[];
  document.getElementById('errors').innerHTML = errs.length
    ? '<div class="err-box"><b>观测页读取异常：</b>'+errs.map(esc).join('；')+'</div>' : '';
}

function tableOf(cols, rows, rowClass){
  if(!rows || !rows.length) return '<div class="empty">暂无数据</div>';
  var h = '<table><thead><tr>';
  cols.forEach(function(c){ h += '<th>'+esc(c[0])+'</th>'; });
  h += '</tr></thead><tbody>';
  rows.forEach(function(r){
    h += '<tr class="'+(rowClass?rowClass(r):'')+'">';
    cols.forEach(function(c){ h += '<td>'+c[1](r)+'</td>'; });
    h += '</tr>';
  });
  return h + '</tbody></table>';
}

function sec(title, inner, open){
  return '<div class="sec"><h3 onclick="this.nextElementSibling.style.display='+
    "(this.nextElementSibling.style.display==='none'?'block':'none')"+'">'+
    esc(title)+'<span class="muted">▾</span></h3><div class="inner"'+
    (open===false?' style="display:none"':'')+'>'+inner+'</div></div>';
}

function playerTable(r){
  return tableOf([
    ['座位', function(p){ return esc(p['座位']); }],
    ['玩家', function(p){ return esc(p['显示名'])+
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
  });
}

function votePanel(v){
  if(!v) return '<div class="empty">暂无数据</div>';
  var h = '<div class="muted">第 '+esc(v['轮次'])+' 轮 · 历史 '+esc(v['历史轮次'])+' 轮</div>';
  h += tableOf([['投票人',function(x){return esc(x['投票人']);}],
                ['投给',function(x){return esc(x['投给']);}],
                ['票重',function(x){return esc(x['票重']);}]], v['票型']);
  h += '<div style="margin-top:6px"><b>票数统计：</b>'+
    ((v['票数统计']&&v['票数统计'].length)
      ? v['票数统计'].map(function(x){ return '<span class="pill">'+esc(x['候选'])+' × '+
          esc(x['票数'])+'</span>'; }).join('') : '<span class="muted">无</span>')+'</div>';
  h += '<div><b>弃票：</b>'+((v['弃票']||[]).map(esc).join('、')||'<span class="muted">无</span>')+'</div>';
  h += '<div><b>未投票：</b><span class="wait">'+
    ((v['未投票']||[]).map(esc).join('、')||'<span class="muted">无</span>')+'</span></div>';
  return h;
}

function nightPanel(n){
  if(!n) return '<div class="empty">暂无数据</div>';
  var h = '<div><b>已提交（'+(n['已提交']||[]).length+'）</b></div>';
  h += tableOf([['玩家',function(x){return esc(x['玩家']);}],
                ['身份',function(x){return esc(x['身份']);}],
                ['动作',function(x){return esc(x['动作']);}]], n['已提交']);
  h += '<div style="margin-top:6px"><b class="wait">未提交（'+(n['未提交']||[]).length+'）</b></div>';
  h += tableOf([['玩家',function(x){return esc(x['玩家']);}],
                ['身份',function(x){return esc(x['身份']);}],
                ['应做',function(x){return esc(x['应做']);}]], n['未提交']);
  return h;
}

function mergedLog(room){
  var items = [];
  (room['系统事件']||[]).forEach(function(e){
    items.push({ ts:e['时间'], kind:'evt', label:e['类型'],
      text:(e['公开']?'':'[私聊 '+(e['定向用户']||'').slice(0,8)+'] ')+(e['内容']||'') });
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
      '</span><span>'+esc(i.text)+'</span></li>'; }).join('')+'</ul>';
}

function deliveryTable(rows){
  return tableOf([
    ['时间', function(d){ return t(d['创建时间']); }],
    ['状态', function(d){ return '<span class="st-'+esc(d['状态'])+'">'+esc(d['状态'])+'</span>'; }],
    ['序号', function(d){ return esc(d['消息序号']); }],
    ['锚点', function(d){ return '<span class="mono">'+esc((d['回复锚点']||'—').slice(0,10))+'</span>'; }],
    ['内容', function(d){ return esc((d['内容']||'').slice(0,80)); }],
    ['次数', function(d){ return esc(d['尝试次数']); }],
    ['错误', function(d){ return '<span class="st-failed">'+esc(d['最近错误']||'')+'</span>'; }]
  ], rows);
}

function roomCard(r){
  var d = r['诊断']||{};
  var h = '<div class="room" id="room-'+esc(r['房间号'])+'">';
  h += '<div class="head"><span class="title">群 '+esc(r['群号'])+'</span>'+
    '<span><span class="badge phase">'+esc(r['房间状态'])+'</span> '+
    '<span class="badge">第 '+esc(r['当前轮次'])+' 轮</span> '+
    '<span class="badge">v'+esc(r['状态版本'])+'</span> '+
    '<span class="badge">'+esc(r['人数']['存活'])+'/'+esc(r['人数']['总人数'])+' 存活</span></span></div>';
  h += '<div class="diag '+esc(d['级别']||'正常')+'"><span class="lv">'+
    esc(d['级别']||'')+'｜'+esc(d['结论']||'')+'</span>'+esc(d['说明']||'')+'</div>';
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
  h += sec('玩家（'+(r['玩家']||[]).length+'）', playerTable(r));
  h += sec('投票面板', votePanel(r['投票']));
  h += sec('夜间行动', nightPanel(r['夜间行动']));
  h += sec('系统事件 / 流程日志', mergedLog(r));
  h += sec('最近消息（出站投递）', deliveryTable(r['最近消息']));
  if((r['异常投递']||[]).length){
    h += sec('本房间异常投递（'+r['异常投递'].length+'）', deliveryTable(r['异常投递']));
  }
  return h + '</div>';
}

function render(){
  var s = state.snap; if(!s) return;
  renderTop(s);
  var rooms = s['对局']||[];
  document.getElementById('rooms').innerHTML = rooms.length
    ? rooms.map(roomCard).join('')
    : '<div class="card"><div class="body empty">当前没有正在进行的对局。</div></div>';
  var ts = s['追踪摘要']||{};
  document.getElementById('trace-sum').textContent =
    '累计 '+(ts['总条数']||0)+' 条 / 缓冲 '+(ts['缓冲条数']||0)+
    ' / 订阅 '+(ts['订阅者']||0)+' / 丢弃 '+(ts['推送丢弃']||0);
  renderGlobalTrace();
  document.getElementById('gfailed').innerHTML = (s['异常投递']||[]).length
    ? tableOf([
        ['时间', function(d){ return t(d['更新时间']); }],
        ['房间', function(d){ return esc(d['房间']); }],
        ['状态', function(d){ return '<span class="st-'+esc(d['状态'])+'">'+esc(d['状态'])+'</span>'; }],
        ['次数', function(d){ return esc(d['尝试次数']); }],
        ['下次重试', function(d){ return t(d['下次重试']); }],
        ['内容', function(d){ return esc((d['内容']||'').slice(0,60)); }],
        ['最近错误', function(d){ return '<span class="st-failed">'+esc(d['最近错误']||'')+'</span>'; }]
      ], s['异常投递'])
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
          '</span><span class="muted">'+esc(x['房间']||'全局')+'</span><span>'+
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

var ws = null, retry = 0, poller = null;
function connect(){
  var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  try { ws = new WebSocket(proto + location.host + '/ws/observe'); }
  catch(e){ startPolling(); return; }
  ws.onopen = function(){ retry = 0; stopPolling(); setConn(true, '实时连接已建立'); };
  ws.onmessage = function(ev){
    var msg;
    try { msg = JSON.parse(ev.data); } catch(e){ return; }
    if(msg['类型'] === '快照'){
      state.snap = msg['数据']; state.recvAt = Date.now(); render();
    } else if(msg['类型'] === '追踪'){
      state.live.unshift(msg['数据']);
      if(state.live.length > 300) state.live.length = 300;
      renderGlobalTrace();
    }
  };
  ws.onclose = function(){
    setConn(false, '连接断开，重连中…');
    retry = Math.min(retry + 1, 10);
    startPolling();
    setTimeout(connect, Math.min(1000 * retry, 8000));
  };
  ws.onerror = function(){ try{ ws.close(); }catch(e){} };
}
function startPolling(){
  if(poller) return;
  poller = setInterval(function(){
    fetch('/api/observe').then(function(r){ return r.json(); }).then(function(d){
      state.snap = d; state.recvAt = Date.now(); render();
      setConn(false, '轮询模式（WebSocket 不可用）');
    }).catch(function(){ setConn(false, '接口不可达'); });
  }, 2000);
}
function stopPolling(){ if(poller){ clearInterval(poller); poller = null; } }
connect();
</script>
</body>
</html>
"""
