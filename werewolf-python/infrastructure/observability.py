"""进程内流程追踪：给「机器人发完消息就不动了」这类问题留下可查的现场。

机器人的一次推进要跨好几层：指令分发（application）→ 引擎推进（domain）→
消息生成（application）→ 入库排队 → 投递（adapters/qq）。任何一层静默返回，
群里看到的现象都一样——「刚才还在发消息，现在没反应了」。数据库里的
game_events / deliveries 只记录了「已经成功产出的结果」，而卡住的那一步
恰恰不会留下任何行；所以这里再补一层进程内的环形缓冲，把每一步「做了什么、
为什么没往下走」都记下来，供 /dashboard 观测页实时查看。

设计约束（调用方遍布热路径，必须便宜）：
- record() 同步、不抛异常、不阻塞，绝不能因为观测把主流程拖垮或搞崩；
- 缓冲总量有硬上限，长时间运行不会把内存吃满；
- 订阅队列写满就丢最旧的一条，宁可丢观测数据也不阻塞业务协程；
- 退订必须彻底，断开的 WebSocket 不能留下永远没人消费的队列。

用法：

    from infrastructure.observability import TRACE

    TRACE.record(room.session_id, "引擎推进", "夜晚结算完成", 死亡人数=2)
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any


# 环形缓冲的默认容量。按每局每阶段十余条估算，2000 条足够覆盖数个并行对局的
# 最近若干轮；超出后自动丢弃最旧的记录。
DEFAULT_CAPACITY = 2000

# 单个订阅者（一个 WebSocket 连接）的积压上限。前端只关心最近发生了什么，
# 积压超过这个数说明该连接已经跟不上了，直接丢弃最旧的记录。
DEFAULT_QUEUE_SIZE = 500

# 扩展字段做 JSON 安全化时允许的最大递归深度与容器长度，防止有人误传大对象。
_MAX_DEPTH = 3
_MAX_ITEMS = 30
_MAX_REPR = 200


def _utc_now_iso() -> str:
    """统一用带时区的 UTC ISO 字符串，前端按本地时区渲染。"""
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any, depth: int = 0) -> Any:
    """把任意扩展字段转成可以直接 json.dumps 的形式。

    观测数据不值得为了序列化再抛一次异常，所以认不出来的对象一律退化成
    截断后的 repr 字符串。
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if depth >= _MAX_DEPTH:
        return repr(value)[:_MAX_REPR]
    if isinstance(value, dict):
        items = list(value.items())[:_MAX_ITEMS]
        return {str(key): _json_safe(item, depth + 1) for key, item in items}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    return repr(value)[:_MAX_REPR]


class ProcessTracer:
    """协程 / 线程安全的流程追踪环形缓冲。

    所有公开方法都可以在任意线程或任意协程里调用；内部用一把普通锁保护
    缓冲区与订阅者列表，临界区里只做纯内存操作，不会 await、不会阻塞。
    """

    def __init__(
        self, capacity: int = DEFAULT_CAPACITY, queue_size: int = DEFAULT_QUEUE_SIZE
    ) -> None:
        self._capacity = max(1, int(capacity))
        self._queue_size = max(1, int(queue_size))
        self._lock = threading.Lock()
        self._records: deque[dict[str, Any]] = deque(maxlen=self._capacity)
        self._subscribers: list[asyncio.Queue] = []
        self._sequence = 0
        self._total = 0
        self._stage_counts: Counter[str] = Counter()
        self._last_at: str | None = None
        self._dropped = 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def record(self, room_id: str | None, stage: str, detail: str, **fields: object) -> None:
        """记录一条流程事件。

        参数：
        - room_id：房间号（群号）。与具体对局无关的全局事件传 None。
        - stage：流程阶段标识，例如「指令分发」「引擎推进」「消息生成」
          「定时器」「投递」，前端按它做分组和着色。
        - detail：中文说明，直接展示给管理员看，写清楚「做了什么 / 卡在哪」。
        - fields：任意扩展字段，会原样收进「附加」里；不可序列化的对象自动转成
          截断的 repr。

        这个方法在任何情况下都不会抛异常，也不会阻塞调用方——它被放在指令处理
        和定时器的热路径上，观测本身绝不能成为故障源。
        """

        try:
            entry = {
                "序号": 0,
                "时间": _utc_now_iso(),
                "房间": str(room_id) if room_id is not None else None,
                "阶段": str(stage),
                "详情": str(detail),
                "附加": {str(key): _json_safe(value) for key, value in fields.items()},
            }
            with self._lock:
                self._sequence += 1
                self._total += 1
                entry["序号"] = self._sequence
                self._stage_counts[entry["阶段"]] += 1
                self._last_at = entry["时间"]
                self._records.append(entry)
                subscribers = list(self._subscribers)
            # 推送放在锁外面：即便某个队列的实现有意外，也不至于卡住写入路径。
            for queue in subscribers:
                self._offer(queue, entry)
        except Exception:  # noqa: BLE001 - 观测失败必须被吞掉，绝不影响主流程
            pass

    def _offer(self, queue: asyncio.Queue, entry: dict[str, Any]) -> None:
        """非阻塞投递给一个订阅者，队列满就丢最旧的一条腾位置。"""
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(entry)
            except Exception:  # noqa: BLE001 - 订阅者跟不上就放弃这一条
                pass
            with self._lock:
                self._dropped += 1
        except Exception:  # noqa: BLE001 - 队列已被弃用等异常一律忽略
            pass

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def recent(self, room_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """取最近的记录，按时间倒序（最新的在最前面）。

        room_id 为 None 时返回全部房间的记录；传了房间号则只返回该房间的记录，
        全局记录（房间为 None 的那些）不会混进来，避免观测页串房间。
        """

        count = max(0, int(limit))
        if count == 0:
            return []
        with self._lock:
            snapshot = list(self._records)
        if room_id is not None:
            target = str(room_id)
            snapshot = [item for item in snapshot if item["房间"] == target]
        # deque 里是按时间正序追加的，取尾部再反转就是最新的 count 条。
        return [dict(item) for item in reversed(snapshot[-count:])]

    def summary(self) -> dict[str, Any]:
        """全局计数，用于观测页顶部的「追踪摘要」。"""
        with self._lock:
            return {
                "总条数": self._total,
                "缓冲条数": len(self._records),
                "缓冲上限": self._capacity,
                "各阶段条数": dict(self._stage_counts),
                "最近时间": self._last_at,
                "订阅者": len(self._subscribers),
                "推送丢弃": self._dropped,
            }

    # ------------------------------------------------------------------
    # 订阅（供 WebSocket 增量推送）
    # ------------------------------------------------------------------

    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        """登记一个新订阅者，返回接收新事件的队列。

        队列有界；订阅者消费不过来时由 record() 丢弃最旧的记录，不会反压业务。
        用完必须调用 unsubscribe()，否则队列会一直被持有导致内存泄漏。
        """

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        """注销订阅者。重复调用安全，传入未登记的队列也不会报错。"""
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                return
        # 清空残留数据，帮助尽快释放引用。
        while True:
            try:
                queue.get_nowait()
            except Exception:  # noqa: BLE001 - 队列空了就结束
                break

    def clear(self) -> None:
        """清空缓冲与计数，仅供本地调试和测试使用；不影响已有订阅者。"""
        with self._lock:
            self._records.clear()
            self._stage_counts.clear()
            self._total = 0
            self._dropped = 0
            self._last_at = None


# 模块级单例：其它模块统一 `from infrastructure.observability import TRACE`。
TRACE = ProcessTracer()
