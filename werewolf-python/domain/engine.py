from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .locale import get_locale_string
from .models import (
    ClockSource,
    DomainEvent,
    GameMode,
    GamePhase,
    GameRoom,
    is_opaque_display_name,
    KillMethod,
    NightAction,
    Player,
    QuestionType,
    RandomSource,
    ROLE_ACTIONS,
    DAY_ACTIONS,
    MAJORITY_WOLF_ROLES,
    OFFICIAL_MAX_JOIN_TIME,
    OFFICIAL_MAX_PLAYERS,
    Role,
    Team,
    WOLF_ROLES,
)
from .rules import assign_official_roles
from .roleinfo import role_display_name
from .achievements import (
    Achievement,
    achievement_description,
    achievement_name,
    unlock_text,
)


class GameRuleError(ValueError):
    pass


_SKIP = {"", "跳过", "弃权", "弃票", "skip", "abstain", "-1", "0"}
_YES = {"是", "好", "确认", "yes", "y", "1"}
# Werewolf.cs:5442-5449 —— HunterFinalShot 用 30 次 1 秒轮询阻塞整局流程。
_HUNTER_WINDOW_SECONDS = 30
# Werewolf.cs:4917 —— 结算名单 `OrderBy(x => x.Team)` 按 ITeam 声明顺序排，
# Team 枚举的声明顺序与官方 ITeam 完全一致。
_TEAM_ORDER = {team: index for index, team in enumerate(Team)}


class GameRoomEngine:
    """与平台无关的状态迁移，规则集锁定为官方 Normal 玩法。"""

    def __init__(self, rng: RandomSource | None = None, clock: ClockSource | None = None):
        self.rng = rng or random.Random()
        self.clock = clock

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now(timezone.utc)

    @staticmethod
    def _find(room: GameRoom, user_id: str) -> Player:
        try:
            return next(player for player in room.players if player.user_id == user_id)
        except StopIteration as exc:
            raise GameRuleError("你不在当前房间") from exc

    @staticmethod
    def _players(room: GameRoom, *, alive: bool | None = True) -> list[Player]:
        return [p for p in room.players if alive is None or p.alive == alive]

    @staticmethod
    def _is_eat_wolf(player: Player) -> bool:
        return player.role in WOLF_ROLES

    @staticmethod
    def _is_majority_wolf(player: Player) -> bool:
        return player.role in MAJORITY_WOLF_ROLES

    @classmethod
    def _is_wolf(cls, player: Player) -> bool:
        return cls._is_majority_wolf(player) or bool(player.metadata.get("wolf_aligned"))

    @classmethod
    def _is_bad(cls, player: Player) -> bool:
        return cls._is_majority_wolf(player) or player.team in {
            Team.CULT, Team.SERIAL_KILLER, Team.ARSONIST,
        }

    # ------------------------------------------------------------------
    # 成就系统：等价迁移自 Werewolf.cs:6047-6062 AddAchievement 与
    # Werewolf.cs:5792-5930 UpdateAchievements。官方用 BitArray(200) 存储，
    # 位下标等于枚举整数值；本移植用 Player.achievements 保存整数列表。
    # ------------------------------------------------------------------
    def _add_achievement(self, player: Player, item: Achievement) -> list[DomainEvent]:
        """Werewolf.cs:6047-6062 —— 即时解锁；已拥有则静默返回。"""
        value = int(item)
        if value in player.achievements:
            return []
        player.achievements.append(value)
        return [
            DomainEvent(
                "achievement_unlocked",
                unlock_text(item),
                public=False,
                target_user_id=player.user_id,
                metadata={"achievement": value},
            )
        ]

    @staticmethod
    def _name(player: Player) -> str:
        """官方 `IPlayer.GetName()` 的端口版本：QQ 没有 @mention，改用「昵称（座位号）」。"""
        if is_opaque_display_name(player.display_name):
            return f"{player.seat}号"
        return f"{player.display_name}（{player.seat}号）"

    @staticmethod
    def _has_fled(player: Player) -> bool:
        # flee() 写 metadata["fled"]，_kill 也可能写 Player.fled，两者都算逃跑。
        return bool(player.fled or player.metadata.get("fled"))

    def _check_long_haul(self, room: GameRoom) -> list[DomainEvent]:
        """Werewolf.cs:5720-5731 CheckLongHaul —— 开局满一小时，存活未逃跑者解锁 LongHaul。"""
        if room.statistics.get("long_haul_reached"):
            return []
        started_at = room.statistics.get("game_started_at")
        if not started_at:
            return []
        try:
            started = datetime.fromisoformat(started_at)
        except (TypeError, ValueError):
            return []
        if (self._now() - started).total_seconds() < 3600:
            return []
        events: list[DomainEvent] = []
        for player in room.players:
            if player.alive and not self._has_fled(player):
                events.extend(self._add_achievement(player, Achievement.LONG_HAUL))
        room.statistics["long_haul_reached"] = True
        return events

    def _is_date_anywhere(self, day: int, month: int, year: int | None = None) -> bool:
        """Werewolf.cs:5736-5740 IsDateAnywhere —— 地球任意时区处于指定日期即成立。"""
        now = datetime.now(timezone.utc)
        dates = (now - timedelta(hours=11), now, now + timedelta(hours=14))
        return any(
            item.day == day and item.month == month and (year is None or item.year == year)
            for item in dates
        )

    def _update_achievements(self, room: GameRoom) -> list[DomainEvent]:
        """Werewolf.cs:5792-5930 UpdateAchievements —— 结算时的批量成就判定。

        官方在此处直接写 BitArray 并统一发送 "New Unlocks!"，不走 AddAchievement，
        因此这里同样直接写 Player.achievements，最后每人只发一条汇总私聊。
        """
        # Werewolf.cs:5797-5798 —— convention / beastResisted 先算一次。
        convention = sum(
            1 for p in room.players if p.role == Role.CULTIST and p.alive
        ) >= 10
        beast_roles = (Role.WILD_CHILD, Role.CURSED, Role.TRAITOR)
        beast_resisted = sum(
            1 for role in beast_roles
            if any(p.won and p.role == role for p in room.players)
        ) == 3
        total = len(room.players)
        # 官方 Language 为群语言包名，QQ 版只有中文包，保留同样的判定入口。
        language = str(room.statistics.get("language", ""))
        lifetime = room.statistics.get("lifetime_stats") or {}
        # Werewolf.cs:5866/5890 —— GetPlayersForRoles(WolfRoles, aliveOnly)。
        wolves_all = [p for p in room.players if p.role in WOLF_ROLES]
        wolves_alive = [p for p in wolves_all if p.alive]
        masons_alive = sum(1 for p in room.players if p.role == Role.MASON and p.alive)
        no_winner = sum(1 for p in room.players if p.won) == 0

        events: list[DomainEvent] = []
        # Werewolf.cs:5801 —— 逃跑/挂机者不参与成就结算。
        for player in room.players:
            if self._has_fled(player):
                continue
            stats = lifetime.get(player.user_id) or {}
            # 官方 GamePlayers 在开局即写入，因此计数包含本局。
            played = int(stats.get("games", 0)) + 1
            survived = int(stats.get("survived", 0)) + (1 if player.alive else 0)
            unlocked: list[Achievement] = []

            def grant(item: Achievement, condition: bool) -> None:
                if not condition or int(item) in player.achievements:
                    return
                player.achievements.append(int(item))
                unlocked.append(item)

            grant(Achievement.WELCOME_TO_HELL, True)
            grant(Achievement.WELCOME_TO_ASYLUM, room.rules.mode == GameMode.CHAOS)
            grant(Achievement.ALZHEIMER_PATIENT, "Amnesia" in language)
            # OHAIDER：官方已注释（Werewolf.cs:5838-5839），此处同样不判定。
            grant(Achievement.SPY_VS_SPY, not room.rules.show_roles_on_death)
            grant(
                Achievement.NO_IDEA_WHAT,
                not room.rules.show_roles_on_death and "Amnesia" in language,
            )
            grant(Achievement.ENOCHLOPHOBIA, total == 35)
            grant(Achievement.INTROVERT, total == 5)
            grant(Achievement.NAUGHTY, room.rules.allow_nsfw or "NSFW" in language)
            grant(Achievement.DEDICATED, played >= 100)
            grant(Achievement.OBSESSED, played >= 1000)
            grant(Achievement.VETERAN, played >= 500)
            grant(Achievement.MASOCHIST, player.won and player.role == Role.TANNER)
            grant(
                Achievement.WOBBLE,
                player.alive and player.role == Role.DRUNK and total >= 10,
            )
            grant(Achievement.SURVIVALIST, survived >= 100)
            grant(
                Achievement.MASON_BROTHER,
                player.role == Role.MASON and player.alive and masons_alive >= 2,
            )
            grant(
                Achievement.CHANGING_SIDES,
                player.changed_roles_count > 0 and player.won,
            )
            grant(
                Achievement.LONE_WOLF,
                room.rules.mode == GameMode.CHAOS
                and total >= 10
                and player.role in WOLF_ROLES
                and len(wolves_all) == 1
                and player.won,
            )
            grant(
                Achievement.INCONSPICUOUS,
                not player.has_been_voted and player.alive,
            )
            grant(
                Achievement.PROMISCUOUS,
                not player.has_stayed_home
                and not player.has_repeated_visit
                and len(player.players_visited) >= 5,
            )
            grant(
                Achievement.DOUBLE_SHIFTER,
                player.changed_roles_count - (1 if player.converted_to_cult else 0) >= 2,
            )
            grant(Achievement.BROKEN_CLOCK, player.fool_correct_see_count >= 2)
            grant(
                Achievement.SMART_GUNNER,
                player.role == Role.GUNNER and player.bullet_hit_baddies >= 2,
            )
            grant(
                Achievement.CULT_CON,
                player.role == Role.CULTIST and player.alive and convention,
            )
            grant(
                Achievement.SERIAL_SAMARITAN,
                player.role == Role.SERIAL_KILLER
                and player.serial_killed_wolves_count >= 3,
            )
            grant(
                Achievement.CULTIST_TRACKER,
                player.role == Role.CULTIST_HUNTER and player.ch_hunted_cult_count >= 3,
            )
            grant(
                Achievement.IM_NOT_DRUNK,
                player.role == Role.CLUMSY_GUY
                and player.clumsy_correct_lynch_count >= 3,
            )
            grant(
                Achievement.WUFFIE_CULT,
                player.role == Role.ALPHA_WOLF and player.alpha_convert_count >= 3,
            )
            grant(
                Achievement.DID_YOU_GUARD_YOURSELF,
                player.role == Role.GUARDIAN_ANGEL and player.ga_guard_wolf_count >= 3,
            )
            grant(
                Achievement.THREE_LITTLE_WOLVES,
                player.role == Role.SORCERER and player.alive and len(wolves_alive) >= 3,
            )
            grant(
                Achievement.PRESIDENT,
                player.role == Role.MAYOR
                and player.mayor_lynch_after_reveal_count >= 3,
            )
            grant(Achievement.IT_WAS_A_BUSY_NIGHT, player.busy_night)
            grant(
                Achievement.STRONGEST_ALPHA,
                bool(player.metadata.get("strongest_alpha")),
            )
            grant(Achievement.AM_I_YOUR_SEER, player.fool_correctly_seen_bh)
            grant(
                Achievement.TRUSTWORTHY,
                player.trustworthy and player.alive and player.won,
            )
            grant(
                Achievement.CULT_LEADER,
                player.cult_leader and player.alive and player.won,
            )
            grant(Achievement.DEATH_VILLAGE, no_winner)
            grant(
                Achievement.PSYCHOPATH_KILLER,
                total >= 35 and player.role == Role.SERIAL_KILLER and player.won,
            )
            grant(Achievement.COLD_AS_ICE, player.froze_harlot)
            grant(
                Achievement.RESIST_THE_BEAST,
                beast_resisted and player.won and player.role in beast_roles,
            )
            grant(Achievement.AM_I_HALLUCINATING, player.has_seen_impossible)
            grant(
                Achievement.IN_THE_MIDDLE_OF_THE_TROUBLE,
                bool(player.metadata.get("in_middle_of_trouble")),
            )

            if not unlocked:
                continue
            # Werewolf.cs:5924-5927 —— 汇总通知。
            body = "".join(
                f"{achievement_name(item)}\n{achievement_description(item)}\n\n"
                for item in unlocked
            )
            events.append(DomainEvent(
                "achievements_unlocked",
                f"新解锁成就！\n{body}".rstrip() + "\n",
                public=False,
                target_user_id=player.user_id,
                metadata={"achievements": [int(item) for item in unlocked]},
            ))
        return events

    def _transform_checks(
        self,
        player: Player,
        *,
        method: str = "",
        new_role_model: str | None = None,
    ) -> list[DomainEvent]:
        """Werewolf.cs:1979-1996 —— Transform() 变身前的成就判定与转化标记。

        必须在改写 player.role 之前调用，官方同样在赋值前检查旧身份。
        """
        events: list[DomainEvent] = []
        if player.role == Role.WISE_ELDER:
            events.extend(self._add_achievement(player, Achievement.I_LOST_MY_WISDOM))
        if player.role == Role.WOLF_MAN and method == "AlphaBitten":
            events.extend(self._add_achievement(player, Achievement.JUST_A_BEARDY_GUY))
        if new_role_model is not None and new_role_model == player.user_id:
            events.extend(self._add_achievement(player, Achievement.INDESTRUCTIBLE))
        if method in {"ConvertToCult", "AutoConvertToCult"}:
            player.converted_to_cult = True
        return events

    def _capture_ga(self, room: GameRoom) -> tuple[Player | None, str | None]:
        for player in room.players:
            if player.role != Role.GUARDIAN_ANGEL or not player.alive:
                continue
            action = room.night_actions.get(player.user_id)
            if (
                action
                and action.day == room.day
                and action.action == QuestionType.GUARD.value
                and action.target_id
            ):
                return player, action.target_id
        return None, None

    def _is_guarded(self, ga: Player | None, ga_target: str | None, target_id: str) -> bool:
        return bool(ga and ga_target == target_id)

    @staticmethod
    def _guarded_ids(room: GameRoom) -> set[str]:
        return {
            action.target_id
            for action in room.night_actions.values()
            if action.day == room.day
            and action.action == QuestionType.GUARD.value
            and action.target_id
        }

    @staticmethod
    def _is_away(room: GameRoom, player: Player) -> bool:
        # 官方 VisitPlayer 只看被访问者自己的 Choice；别人访问他，
        # 不会让他离开家。0、-1 和空值都代表没有行动。
        return (
            not (player.frozen or player.is_frozen)
            and player.choice not in {None, "", 0, "0", -1, "-1"}
        )

    @staticmethod
    def _parse_seat_token(value: str) -> int | None:
        text = (value or "").strip()
        match = re.match(r"^(\d+)\s*号(?:\s+.*)?$", text)
        if match:
            return int(match.group(1))
        try:
            return int(text)
        except ValueError:
            return None

    @staticmethod
    def _target(
        room: GameRoom,
        token: str,
        *,
        actor_id: str | None = None,
        allow_self: bool = False,
        allow_dead: bool = False,
    ) -> Player:
        value = (token or "").strip().lstrip("@").strip()
        folded = value.casefold()
        target = next(
            (
                player for player in room.players
                if value == player.user_id
                or folded == player.display_name.casefold()
                or folded == player.public_name.casefold()
                or folded == player.list_label.casefold()
            ),
            None,
        )
        if target is None:
            seat = GameRoomEngine._parse_seat_token(value)
            if seat is None:
                raise GameRuleError("找不到目标玩家，请使用座位号或玩家名")
            target = next((p for p in room.players if p.seat == seat), None)
        if target is None:
            raise GameRuleError("找不到目标座位")
        if not allow_dead and not target.alive:
            raise GameRuleError("目标玩家已经出局")
        if not allow_self and actor_id is not None and target.user_id == actor_id:
            raise GameRuleError("不能选择自己")
        return target

    def _deadline(self, seconds: int) -> datetime:
        return self._now() + timedelta(seconds=seconds)

    def create_room(self, session_id: str, rules, host_user_id: str | None = None) -> GameRoom:
        rules.validate()
        rules = self._apply_random_mode(rules)
        room = GameRoom(session_id=session_id, rules=rules)
        # 建局者即本局发起人（房主），后续 /startgame、/go、/cancel 都以此判定权限。
        room.host_user_id = host_user_id
        room.stage_started_at = self._now()
        room.stage_deadline = self._deadline(rules.join_seconds)
        return room

    def _apply_random_mode(self, rules):
        """Werewolf.cs:177-200 —— RandomMode 群设置在建局时随机化模式与机制开关。"""
        if not getattr(rules, "random_mode", False):
            return rules
        modes = list(GameMode)
        roll = self.rng.randrange(100)
        return replace(
            rules,
            mode=modes[self.rng.randrange(len(modes))],
            thief_full=self.rng.randrange(100) < 50,
            secret_lynch=self.rng.randrange(100) < 50,
            show_roles_on_death=self.rng.randrange(100) < 50,
            secret_lynch_show_votes=self.rng.randrange(100) < 50,
            secret_lynch_show_voters=self.rng.randrange(100) < 50,
            allow_arsonist=self.rng.randrange(100) < 50,
            burning_overkill=self.rng.randrange(100) < 1,
            show_roles_end="None" if roll < 33 else ("Living" if roll < 67 else "All"),
        )

    def join(self, room: GameRoom, user_id: str, display_name: str) -> list[DomainEvent]:
        if room.phase != GamePhase.LOBBY:
            return []
        if any(p.user_id == user_id for p in room.players):
            return []
        cleaned_name = display_name.replace("\r", "").replace("\n", "").strip()
        if not cleaned_name or cleaned_name.startswith("/") or cleaned_name.casefold() == "skip":
            raise GameRuleError("请先修改为可用的显示名后再加入")
        if any(p.display_name == cleaned_name for p in room.players):
            raise GameRuleError("当前房间已有相同显示名")
        if len(room.players) >= OFFICIAL_MAX_PLAYERS:
            raise GameRuleError("房间人数已满")
        room.players.append(
            Player(user_id=user_id, display_name=cleaned_name, seat=len(room.players) + 1)
        )
        # 建局者可能没有立刻 /join；第一个真正入场的玩家兜底成为房主，
        # 保证 /cancel、/startgame、/go 永远有一个明确的责任人。
        if room.host_user_id is None:
            room.host_user_id = user_id
        now = self._now()
        # Werewolf.cs:816-817 —— 达到群配置上限只是提前结束等待（KillTimer），
        # 真正拒绝加入的硬上限是 Settings.MaxPlayers=35。
        if len(room.players) >= room.rules.max_players:
            room.stage_deadline = now
        elif room.stage_started_at and room.stage_deadline:
            elapsed = max(0, int((now - room.stage_started_at).total_seconds()))
            reset_elapsed = min(elapsed, max(120, elapsed - 30))
            if reset_elapsed < elapsed:
                room.stage_deadline = now + timedelta(
                    seconds=max(60, room.rules.join_seconds - reset_elapsed)
                )
        room.state_version += 1
        needed = max(0, room.rules.min_players - len(room.players))
        # 玩家侧最常见的卡点是“人齐了却不知道怎么开”，所以直接把指令写进播报。
        suffix = "现在可以开始，房主发送 /startgame 立即开局。" if not needed else f"还需 {needed} 人。"
        player = room.players[-1]
        # QQ 群消息经常没有昵称，display_name 会落成 32 位 openid；群里回复已经 @ 了对方，不再把 ID 打进正文。
        shown_name = player.public_name
        # Werewolf.cs:456-459 —— ShowIDs 群设置会在加入播报中附带玩家 ID。
        if room.rules.show_ids:
            shown_name = f"{shown_name} (ID: {user_id})"
        return [DomainEvent("player_joined", f"{shown_name} 加入游戏，当前 {len(room.players)} 人。{suffix}")]

    def _reassign_host(self, room: GameRoom, leaving_user_id: str) -> None:
        """房主离场时把房主身份交给下一位在场玩家；不是房主离场则保持不变。

        旧实现无条件把 host_user_id 置空，导致 /cancel 的房主校验永远失败，
        并且“发起人可以直接开局”无处落地。
        """

        if room.host_user_id != leaving_user_id:
            return
        room.host_user_id = room.players[0].user_id if room.players else None

    def leave(self, room: GameRoom, user_id: str) -> list[DomainEvent]:
        if room.phase != GamePhase.LOBBY:
            raise GameRuleError("游戏开始后不能退出")
        player = self._find(room, user_id)
        room.players.remove(player)
        for seat, item in enumerate(room.players, 1):
            item.seat = seat
        self._reassign_host(room, user_id)
        room.state_version += 1
        return [DomainEvent("player_left", f"{player.public_name} 已退出，当前 {len(room.players)} 人。")]

    def cancel(self, room: GameRoom, user_id: str, *, admin: bool = False) -> list[DomainEvent]:
        if room.phase == GamePhase.CANCELLED:
            return [DomainEvent("cancelled", "房间已经取消。")]
        if room.phase != GamePhase.LOBBY:
            raise GameRuleError("只能取消入场中的房间")
        # 房主可以取消自己开的房；管理员兜底；房主已离场（host 为空）时允许在场玩家取消。
        if not admin and room.host_user_id is not None and room.host_user_id != user_id:
            raise GameRuleError("只有房主或管理员可以取消房间")
        if not admin and room.host_user_id is None and not any(
            player.user_id == user_id for player in room.players
        ):
            raise GameRuleError("你不在当前房间，无法取消")
        room.phase, room.stage_deadline = GamePhase.CANCELLED, None
        room.state_version += 1
        return [DomainEvent("cancelled", "本局房间已取消。")]

    def flee(self, room: GameRoom, user_id: str) -> list[DomainEvent]:
        """复刻官方弃权行为：入场阶段直接离场，开局之后则判定死亡。"""
        # Werewolf.cs:835-839 —— AllowFlee 只拦截“已开始且不在入场”的对局，
        # 并且会公开回复 FleeDisabled；入场阶段无论是否禁逃跑都可以退出。
        if not room.rules.allow_flee and room.phase in {GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}:
            return [DomainEvent("flee_disabled", get_locale_string("FleeDisabled"))]
        player = next((item for item in room.players if item.user_id == user_id), None)
        if player is None:
            return []
        if room.phase == GamePhase.LOBBY:
            room.players.remove(player)
            for seat, item in enumerate(room.players, 1):
                item.seat = seat
            self._reassign_host(room, user_id)
            room.state_version += 1
            # Werewolf.cs:848,862-864 —— 入场阶段先播 Flee，再播剩余人数。
            return [
                DomainEvent("player_fled", get_locale_string("Flee", None, player.public_name)),
                DomainEvent(
                    "players_remain",
                    get_locale_string("CountPlayersRemain", None, str(len(room.players))),
                ),
            ]
        if room.phase not in {GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}:
            raise GameRuleError("当前不能退出游戏")
        if not player.alive:
            # Werewolf.cs:842-846 —— 死人逃跑回 DeadFlee。
            raise GameRuleError(get_locale_string("DeadFlee"))
        events = self._kill(
            room, player, KillMethod.FLEE, source=player,
            hunter_final_shot=False, is_night=False,
        )
        player.metadata["fled"] = True
        events.insert(0, DomainEvent("player_fled", get_locale_string("Flee", None, player.public_name)))
        events.extend(self._resolve_role_changes(room))
        events.extend(self._check_for_game_end(room))
        return events

    def smite(self, room: GameRoom, user_id: str) -> list[DomainEvent]:
        """`Werewolf Node/Werewolf.cs:5357-5390` FleePlayer —— 群管理员 /smite。

        与玩家自助 /flee（RemovePlayer）的差异：不看 AllowFlee 群设置；
        目标不在场或已出局时完全静默；入场阶段移除后不播剩余人数。
        """

        player = next((item for item in room.players if item.user_id == user_id), None)
        if player is None or not player.alive:
            return []
        events = [DomainEvent("player_smited", get_locale_string("Flee", None, player.public_name))]
        if room.phase == GamePhase.LOBBY:
            room.players.remove(player)
            for seat, item in enumerate(room.players, 1):
                item.seat = seat
            self._reassign_host(room, user_id)
            room.state_version += 1
            return events
        if room.phase not in {GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}:
            return []
        events.extend(
            self._kill(
                room, player, KillMethod.FLEE, source=player,
                hunter_final_shot=False, is_night=False,
            )
        )
        player.metadata["fled"] = True
        events.extend(self._resolve_role_changes(room))
        events.extend(self._check_for_game_end(room))
        return events

    def skip_vote(self, room: GameRoom) -> list[DomainEvent]:
        """`Werewolf Node/Werewolf.cs:5414-5418` SkipVote —— 把 `Choice == 0` 的人置为跳过。

        官方只改选择、不推进计时；等所有人都有选择后由原有流程自行结算。
        本端口的处决票存放在 `room.votes`，夜间行动存放在 `room.night_actions`，
        因此按当前阶段分别补空选择。
        """

        skipped = 0
        if room.phase == GamePhase.VOTE:
            for player in self._players(room):
                if player.user_id not in room.votes:
                    room.votes[player.user_id] = None
                    skipped += 1
            if skipped:
                room.state_version += 1
            events = [DomainEvent("vote_skipped", f"已将 {skipped} 名未投票玩家标记为跳过。")]
            if {p.user_id for p in self._players(room)}.issubset(room.votes):
                events.extend(self.resolve_vote(room))
            return events
        if room.phase in {GamePhase.NIGHT, GamePhase.DAY}:
            # 夜间/白天没有独立的“空选择”容器，官方效果等价于让本阶段立即到点结算。
            room.stage_deadline = self._now()
            room.state_version += 1
            return [DomainEvent("vote_skipped", "已跳过当前阶段的剩余等待。")]
        return []

    def kill_game(self, room: GameRoom) -> list[DomainEvent]:
        """`Werewolf Node/Werewolf.cs:5393-5407` Kill —— 强制结束对局。"""

        if room.phase in {GamePhase.FINISHED, GamePhase.CANCELLED}:
            return [DomainEvent("game_removed", "本局已经结束或已被移除。")]
        room.phase = GamePhase.CANCELLED
        room.stage_deadline = None
        room.state_version += 1
        return [DomainEvent("game_removed", "本局已被强制结束并移除。")]

    def extend_time(self, room: GameRoom, user_id: str, seconds: int, *, admin: bool = False) -> list[DomainEvent]:
        if room.phase != GamePhase.LOBBY:
            raise GameRuleError("只有等待入场时可以调整时间")
        if not admin and not room.rules.allow_extend:
            raise GameRuleError("本群未开启玩家延长等待时间")
        # Werewolf.cs:2521-2535 —— ExtendTime 先按 Id 在 Players 中查人，
        # 查不到就整段静默跳过，管理员也不例外。
        if not any(player.user_id == user_id for player in room.players):
            if admin:
                return []
            raise GameRuleError("你不在当前房间")
        if seconds == 0:
            raise GameRuleError("延长时间不能为 0 秒")
        if seconds < 0 and not admin:
            raise GameRuleError("只有管理员可以缩短等待时间")
        seconds = max(-room.rules.max_extend, min(room.rules.max_extend, seconds))
        if not admin:
            extended = set(room.statistics.get("extended_users", []))
            if user_id in extended:
                raise GameRuleError("你已经延长过一次等待时间")
            extended.add(user_id)
            room.statistics["extended_users"] = sorted(extended)
        deadline = room.stage_deadline or self._now()
        target = deadline + timedelta(seconds=seconds)
        if room.stage_started_at:
            # Werewolf.cs:477-491 —— i = Max(i - secondsToAdd, GameJoinTime - MaxJoinTime)，
            # 等待总时长以 Settings.MaxJoinTime=300 秒封顶。
            target = min(
                target, room.stage_started_at + timedelta(seconds=OFFICIAL_MAX_JOIN_TIME)
            )
        room.stage_deadline = max(self._now(), target)
        room.state_version += 1
        action = "延长" if seconds > 0 else "缩短"
        return [DomainEvent("waiting_time_changed", f"等待时间已{action} {abs(seconds)} 秒。")]

    def force_start(
        self, room: GameRoom, user_id: str | None = None, *, admin: bool = False
    ) -> list[DomainEvent]:
        """`/startgame`、`/go` —— 由本局发起人（房主）或管理员立即开局。

        旧实现只把 stage_deadline 拉到当前时刻，真正开局交给 1 秒粒度的定时器循环。
        定时器路径产生的群播报没有 QQ 原始消息号，只能走主动推送，极易被平台拦截，
        于是出现“后台已经开局、玩家侧完全无感知”。现在改成当场开局，
        game_started / 身份私聊 / 夜间提示全部成为对这条群消息的被动回复。
        """

        if room.phase != GamePhase.LOBBY:
            raise GameRuleError("当前房间不在等待入场阶段，无法开始游戏")
        return self.start(room, user_id or (room.host_user_id or ""), force=admin)

    def _night_duration(self, room: GameRoom) -> int:
        duration = room.rules.night_seconds
        if room.day == 1 and any(
            player.role in {Role.CUPID, Role.DOPPELGANGER, Role.WILD_CHILD}
            or (player.role == Role.THIEF and not room.rules.thief_full)
            for player in room.players
        ):
            duration = max(duration, 120)
        return duration

    def _require_host(self, room: GameRoom, user_id: str) -> None:
        """开局权限：本局发起人（房主）即可，不需要群管理员。

        房主中途退出后 host_user_id 会转移给下一位在场玩家；
        万一整桌都退空又有人重新加入（host 为空），退化为“任意在场玩家可开局”。
        """

        host = room.host_user_id
        if host is not None:
            if host == user_id:
                return
            owner = next((p for p in room.players if p.user_id == host), None)
            who = f"（{owner.public_name}）" if owner else ""
            raise GameRuleError(f"只有本局发起人{who}或管理员可以开始游戏。")
        if not any(player.user_id == user_id for player in room.players):
            raise GameRuleError("本局暂无发起人，只有已加入的玩家或管理员可以开始游戏。")

    def start(self, room: GameRoom, user_id: str, *, force: bool = False) -> list[DomainEvent]:
        if room.phase != GamePhase.LOBBY:
            raise GameRuleError("当前房间不在入场阶段")
        if not room.players:
            raise GameRuleError("还没有玩家加入")
        # force=True 用于管理员强开与等待超时自动开局，其余情况必须是房主本人。
        if not force:
            self._require_host(room, user_id)
        if len(room.players) < room.rules.min_players:
            raise GameRuleError(
                f"人数不足，至少需要 {room.rules.min_players} 人，当前 {len(room.players)} 人。"
            )
        self._assign_roles(room)
        room.phase, room.day = GamePhase.NIGHT, 1
        room.stage_started_at, room.stage_deadline = self._now(), self._deadline(self._night_duration(room))
        # Werewolf.cs:563-610 —— 开局即写入 Games/GamePlayers，_timeStarted 作为对局标识。
        room.statistics["game_started_at"] = room.stage_started_at.isoformat()
        # Werewolf.cs:2977 —— 第一夜同样需要 nightStart 基准，供夜末 BloodyNight 判定。
        room.statistics["night_start_death_sequence"] = int(
            room.statistics.get("death_sequence", 0)
        )
        # Werewolf.cs:613-618 —— 不足 20 人时直接置 HasBeenVoted，禁用 Inconspicuous 成就。
        if len(room.players) < 20:
            for player in room.players:
                player.has_been_voted = True
        room.state_version += 1
        events = [
            DomainEvent(
                "game_started",
                f"游戏开始，共 {len(room.players)} 人。第 1 夜开始，请查看私聊身份和行动提示。",
            )
        ]
        events.extend(self._identity_events(room))
        events.extend(self._night_prompts(room))
        return events

    def _identity_events(self, room: GameRoom) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        seer = next((player for player in self._players(room) if player.role == Role.SEER), None)
        masons = [player for player in self._players(room) if player.role == Role.MASON]
        wolves = [player for player in self._players(room) if self._is_majority_wolf(player)]
        cultists = [player for player in self._players(room) if player.role == Role.CULTIST]
        for player in room.players:
            shown = Role.SEER if player.role == Role.FOOL else player.role
            extras = []
            if player.role == Role.BEHOLDER:
                extras.append(f"预言家是{seer.public_name}。" if seer else "本局没有预言家。")
            if player.role == Role.MASON and len(masons) > 1:
                extras.append("石匠同伴：" + "、".join(item.public_name for item in masons) + "。")
            if self._is_majority_wolf(player) and len(wolves) > 1:
                extras.append("狼人同伴：" + "、".join(item.public_name for item in wolves) + "。")
            if player.role == Role.CULTIST and len(cultists) > 1:
                extras.append("教会同伴：" + "、".join(item.public_name for item in cultists) + "。")
            suffix = (" " + "".join(extras)) if extras else ""
            events.append(
                DomainEvent(
                    "identity",
                    f"你的身份是【{role_display_name(shown) if shown else '未知'}】。{self._role_help(player.role)}{suffix}",
                    public=False,
                    target_user_id=player.user_id,
                    metadata={
                        "role": player.role.value if player.role else None,
                        "shown_role": shown.value if shown else None,
                        "team": player.team.value if player.team else None,
                        "seer_id": seer.user_id if player.role == Role.BEHOLDER and seer else None,
                        "mason_ids": [item.user_id for item in masons]
                        if player.role == Role.MASON else [],
                    },
                )
            )
        return events

    def _assign_roles(self, room: GameRoom) -> None:
        assignment = assign_official_roles(
            len(room.players),
            mode=room.rules.mode,
            disabled_roles=room.rules.disabled_roles,
            required_roles=room.rules.required_roles,
            burning_overkill=room.rules.burning_overkill,
            allow_arsonist=room.rules.allow_arsonist,
            rng=self.rng,
        )
        if len(assignment.roles) != len(room.players):
            raise GameRuleError("当前规则无法生成角色配置")
        for player, role in zip(room.players, assignment.roles):
            player.role = player.original_role = role
            player.alive = True
            player.metadata = {"role_actions": [q.value for q in ROLE_ACTIONS.get(role, ())]}
            # Werewolf.cs:1525 —— 开局即为教徒者记 CultLeader，用于结算成就。
            player.cult_leader = role == Role.CULTIST
            if role == Role.GUNNER:
                player.bullet_count = 2
            if role == Role.MAYOR:
                player.vote_weight = 1
        room.statistics["possible_roles"] = [role.value for role in assignment.possible_roles]
        room.statistics["rules_source_commit"] = room.rules.source_commit

    @staticmethod
    def _role_help(role: Role | None) -> str:
        help_text = {
            Role.VILLAGER: "你没有夜间技能，白天通过讨论和投票找出敌对阵营。",
            Role.WOLF: "夜晚发送“狼人 座位号”袭击目标。",
            Role.WOLF_CUB: "夜晚参与狼人袭击；你死亡后狼人本夜可获得额外击杀。",
            Role.ALPHA_WOLF: "夜晚发送“狼人 座位号”袭击；可发送“转化 座位号”使用转化能力。",
            Role.LYCAN: "你属于狼人阵营，夜晚参与袭击；预言家会把你看成村民。",
            Role.SNOW_WOLF: "夜晚发送“冻结 座位号”冻结目标；你不参与狼人袭击投票，但计入狼人多数。",
            Role.SEER: "夜晚发送“查验 座位号”查看身份。",
            Role.APPRENTICE_SEER: "预言家出局后你继承预言家身份，夜晚发送“查验 座位号”。",
            Role.GUARDIAN_ANGEL: "夜晚发送“守护 座位号”保护目标。",
            Role.HARLOT: "夜晚发送“访问 座位号”；访问狼人会触发访问死亡链。",
            Role.DETECTIVE: "白天发送“侦查 座位号”，讨论结束后获得该玩家身份。",
            Role.CULTIST: "夜晚发送“转化 座位号”发展教徒。",
            Role.CULTIST_HUNTER: "夜晚发送“猎杀教徒 座位号”猎杀教徒。",
            Role.SERIAL_KILLER: "夜晚发送“连环杀 座位号”。",
            Role.CUPID: "首夜发送“恋人 座位号 座位号”。",
            Role.DOPPELGANGER: "首夜发送“模仿 座位号”选择角色模板。",
            Role.WILD_CHILD: "首夜发送“偶像 座位号”；偶像死亡后转为狼人。",
            Role.GUNNER: "白天发送“开枪 座位号”消耗一颗子弹。",
            Role.HUNTER: "死亡时可发送“猎杀 座位号”。",
            Role.ARSONIST: "夜晚发送“纵火 座位号”标记，发送“引燃”焚烧所有标记者。",
            Role.CHEMIST: "夜晚发送“化学 座位号”进行一次药剂行动。",
            Role.GRAVE_DIGGER: "夜晚自动挖掘昨夜新墓，无需选择目标。",
            Role.BLACKSMITH: "白天发送“撒银”使下一夜狼人袭击失效。",
            Role.BEHOLDER: "开局会得知预言家是谁。",
            Role.FOOL: "你以为自己是预言家，夜晚可以查验。",
            Role.TRAITOR: "当所有狼人出局后，你转化为狼人。",
            Role.CURSED: "被狼人袭击时会转化为狼人而不是死亡。",
            Role.MASON: "开局会得知其他石匠。",
            Role.PRINCE: "首次被投票处决时公开身份并免疫。",
            Role.CLUMSY_GUY: "投票时有一半概率投向随机存活玩家。",
            Role.TANNER: "被投票处决则独自获胜。",
            Role.WOLF_MAN: "预言家会把你看成狼人。",
            Role.DRUNK: "你是村里人尽皆知的酒鬼，没有夜间技能。",
            Role.MAYOR: "白天发送“市长”公开身份，公开后你的处决票记两票。",
            Role.SPUMPKIN: "白天发送“开枪 座位号”引爆自己，与目标同归于尽。",
            Role.ORACLE: "夜晚查验会看到目标不是某个身份。",
            Role.AUGUR: "夜晚自动看到一个本局可能存在且当前未公开的身份。",
            Role.SORCERER: "夜晚查验只能分辨狼人和预言家。",
            Role.WISE_ELDER: "第一次被狼人袭击时会活下来。",
            Role.SANDMAN: "发送“催眠”使本夜其他行动暂停。",
            Role.TROUBLEMAKER: "白天发送“捣乱”启用双重处决。",
            Role.PACIFIST: "白天发送“和平”阻止本轮处决。",
            Role.THIEF: "夜晚发送“盗取 座位号”记录盗取目标。",
        }
        return help_text.get(role, "请根据私聊提示提交当前可用行动。")

    def _night_prompts(self, room: GameRoom) -> list[DomainEvent]:
        if room.statistics.get("sandman_sleep"):
            return []
        events: list[DomainEvent] = []
        has_action_menu = False
        silver = bool(room.statistics.get("silver_spread"))
        if silver:
            room.statistics["silver_night"] = True
        for player in self._players(room):
            # Official SendNightActions clears a living player's previous choices
            # before working through that player's next-night message.
            player.choice = None
            player.choice2 = None
            if player.role == Role.GRAVE_DIGGER:
                events.extend(self._resolve_grave_digger(room, player))
            if player.drunk:
                continue
            if player.role == Role.CHEMIST and not player.has_used_ability:
                # Official SendNightActions: brew night sets HasUsedAbility and skips the menu.
                player.has_used_ability = True
                events.append(
                    DomainEvent(
                        "chemist_brewing",
                        "你正在配制药剂，本夜不能行动；下一夜可发送“化学 座位号”。",
                        public=False,
                        target_user_id=player.user_id,
                    )
                )
                continue
            if silver and (player.role in WOLF_ROLES or player.role == Role.SNOW_WOLF):
                continue
            actions = tuple(action for action in ROLE_ACTIONS.get(player.role, ()) if action not in DAY_ACTIONS)
            if player.role == Role.CUPID and room.day == 1:
                actions = (QuestionType.LOVER_1,)
            if player.role == Role.APPRENTICE_SEER and player.metadata.get("awakened"):
                actions = (QuestionType.SEE,)
            if not self._night_required(room, player):
                continue
            if not actions:
                continue
            action_names = "、".join(self._command_name(action) for action in actions)
            teammates = ""
            if player.role in WOLF_ROLES:
                names = [
                    member.public_name
                    for member in self._wolf_voters(room)
                    if member.user_id != player.user_id
                ]
                if names:
                    teammates = " 本夜同行狼人：" + "、".join(names) + "。"
            elif player.role == Role.CULTIST:
                names = [
                    member.public_name
                    for member in self._players(room)
                    if member.role == Role.CULTIST and member.user_id != player.user_id
                ]
                if names:
                    teammates = " 本夜同行教徒：" + "、".join(names) + "。"
            required_first_night_pick = room.day == 1 and player.role in {
                Role.CUPID,
                Role.DOPPELGANGER,
                Role.WILD_CHILD,
                Role.THIEF,
            }
            skip_text = "" if required_first_night_pick else "；没有目标时发送“跳过”"
            text = f"第 {room.day} 夜可用行动：{action_names} 目标{skip_text}。{teammates}"
            if player.role == Role.ARSONIST:
                # Werewolf.cs:5281-5292、5326-5327 —— 已浇油的存活玩家单独列出，
                # 且只有存在已浇油目标时才提供“引燃”。
                doused = [
                    member.public_name
                    for member in self._players(room)
                    if member.doused and member.user_id != player.user_id
                ]
                if doused:
                    text = (
                        f"第 {room.day} 夜：要继续给别人的房子浇油，还是点燃所有已浇油的房子？"
                        "发送“纵火 座位号”浇油，发送“引燃”点火；不行动发送“跳过”。\n"
                        "你已经浇过油的房子：" + " 和 ".join(doused)
                    )
                else:
                    text = (
                        f"第 {room.day} 夜：你想给谁的房子浇油？"
                        "发送“纵火 座位号”；不行动发送“跳过”。"
                    )
            events.append(
                DomainEvent(
                    "night_prompt",
                    text,
                    public=False,
                    target_user_id=player.user_id,
                    metadata={"actions": [action.value for action in actions]},
                )
            )
            has_action_menu = True
        if room.statistics.get("wolf_cub_killed") and not silver:
            wolf_second_prompts = self._wolf_second_prompts(room)
            events.extend(wolf_second_prompts)
            has_action_menu = has_action_menu or bool(wolf_second_prompts)
        # Official SendNightActions: snow menus exclude Frozen, then Frozen is cleared.
        room.statistics["snow_blocked_ids"] = [
            player.user_id for player in room.players if player.frozen or player.is_frozen
        ]
        room.statistics.pop("silver_spread", None)
        for player in room.players:
            player.drunk = False
            player.frozen = False
            player.is_frozen = False
            player.burning = False
        if not has_action_menu:
            room.stage_deadline = self._deadline(1)
        return events

    def _day_prompts(self, room: GameRoom) -> list[DomainEvent]:
        """Werewolf.cs:5019-5150 SendDayActions —— 白天技能角色的私聊行动菜单。

        官方用 Telegram inline 按钮推送，QQ 端没有按钮，改为等价语义的私聊文字提示；
        触发条件（存活、是否第 1 天、是否已用过能力、剩余子弹、是否存在可选目标）
        与官方逐条一致。
        """

        events: list[DomainEvent] = []
        # Werewolf.cs:5022-5026 —— 先清空所有人的待答问题和上一次选择。
        for player in room.players:
            player.choice = None
            player.choice2 = None
        alive = self._players(room)
        has_targets = len(alive) > 1

        def first(role: Role, *, unused_ability: bool = False) -> Player | None:
            """官方一律用 FirstOrDefault，分身复制出的同名身份不会重复收到菜单。"""

            for player in alive:
                if player.role == role and (not unused_ability or not player.has_used_ability):
                    return player
            return None

        def prompt(player: Player, text: str, action: QuestionType) -> None:
            events.append(
                DomainEvent(
                    "day_prompt",
                    text,
                    public=False,
                    target_user_id=player.user_id,
                    metadata={"actions": [action.value]},
                )
            )

        # Werewolf.cs:5029-5042 AskDetect
        detective = first(Role.DETECTIVE)
        if detective is not None and has_targets:
            prompt(detective, "你想侦查谁？发送“侦查 座位号”，不侦查发送“跳过”。", QuestionType.DETECT)
        # Werewolf.cs:5044-5055 AskMayor
        mayor = first(Role.MAYOR)
        if mayor is not None and room.day == 1:
            prompt(mayor, "准备好公开市长身份时发送“市长”，公开后你的处决票记两票。", QuestionType.MAYOR)
        # Werewolf.cs:5057-5068 AskPacifist
        pacifist = first(Role.PACIFIST)
        if pacifist is not None and room.day == 1:
            prompt(pacifist, "想发表演说、阻止村庄进行下一次处决时，发送“和平”。", QuestionType.PACIFIST)
        # Werewolf.cs:5070-5081 AskSandman
        sandman = first(Role.SANDMAN, unused_ability=True)
        if sandman is not None:
            prompt(sandman, "今晚要让所有人入睡吗？发送“催眠 是”使用能力。", QuestionType.SANDMAN)
        # Werewolf.cs:5083-5095 SpreadDust
        blacksmith = first(Role.BLACKSMITH, unused_ability=True)
        if blacksmith is not None:
            prompt(
                blacksmith,
                "今天要撒下银粉、让狼人今夜无法袭击吗？发送“撒银 是”使用能力。",
                QuestionType.SPREAD_SILVER,
            )
        # Werewolf.cs:5097-5115 AskShoot，官方文案带剩余子弹数。
        gunner = first(Role.GUNNER)
        if gunner is not None and gunner.bullet_count > 0 and has_targets:
            prompt(
                gunner,
                f"你想开枪打谁？还剩 {gunner.bullet_count} 发子弹。"
                "发送“开枪 座位号”，不开枪发送“跳过”。",
                QuestionType.SHOOT,
            )
        # Werewolf.cs:5117-5131 AskDetonate
        spumpkin = first(Role.SPUMPKIN)
        if spumpkin is not None and has_targets:
            prompt(spumpkin, "你想对谁引爆自己？发送“开枪 座位号”，不引爆发送“跳过”。", QuestionType.SHOOT)
        # Werewolf.cs:5133-5148 AskTroublemaker
        troublemaker = first(Role.TROUBLEMAKER, unused_ability=True)
        if troublemaker is not None:
            prompt(troublemaker, "今天要制造麻烦吗？发送“捣乱”启用本轮双重处决。", QuestionType.TROUBLE)
        return events

    @staticmethod
    def _command_name(action: QuestionType) -> str:
        return {
            QuestionType.KILL: "狼人",
            QuestionType.SEE: "查验",
            QuestionType.GUARD: "守护",
            QuestionType.VISIT: "访问",
            QuestionType.DETECT: "侦查",
            QuestionType.CONVERT: "转化",
            QuestionType.ROLE_MODEL: "模仿/偶像",
            QuestionType.LOVER_1: "恋人",
            QuestionType.SERIAL_KILL: "连环杀",
            QuestionType.HUNT: "猎杀教徒",
            QuestionType.SHOOT: "开枪",
            QuestionType.SPREAD_SILVER: "撒银",
            QuestionType.SANDMAN: "催眠",
            QuestionType.TROUBLE: "捣乱",
            QuestionType.CHEMISTRY: "化学",
            QuestionType.FREEZE: "冻结",
            QuestionType.DOUSE: "纵火/引燃",
            QuestionType.THIEF: "盗取",
        }.get(action, action.value)

    def submit_night_action(
        self, room: GameRoom, user_id: str, action: str, target_token: str | None
    ) -> list[DomainEvent]:
        actor = self._find(room, user_id)
        if action.casefold() in {"hunt", "hunter_kill", "猎杀"}:
            return self._submit_hunter_action(room, actor, target_token)
        if action.casefold() in {"kill2", "wolf2", "狼人二击", "二击", "第二击"}:
            return self._submit_wolf_second_action(room, actor, target_token)
        if not actor.alive:
            raise GameRuleError("出局玩家不能行动")
        qtype = self._question_for_action(action)
        if actor.role == Role.CUPID and qtype == QuestionType.LOVER_1 and actor.metadata.get("cupid_first_target"):
            qtype = QuestionType.LOVER_2
        if qtype != QuestionType.LOVER_2 and actor.action_day == room.day:
            raise GameRuleError("该夜间行动已经提交")
        if room.phase != GamePhase.NIGHT:
            raise GameRuleError("现在不是夜晚行动阶段")
        if qtype in DAY_ACTIONS:
            raise GameRuleError("该行动请在白天私聊提交")
        dynamic = qtype == QuestionType.SEE and actor.role == Role.APPRENTICE_SEER and actor.metadata.get("awakened")
        if qtype not in ROLE_ACTIONS.get(actor.role, ()) and not dynamic:
            raise GameRuleError("当前身份不能执行这个行动")
        if not dynamic and not self._night_required(room, actor):
            raise GameRuleError("当前夜晚该能力不可用")
        if room.statistics.get("silver_spread") or room.statistics.get("silver_night"):
            if qtype in {QuestionType.KILL, QuestionType.FREEZE, QuestionType.KILL_2}:
                raise GameRuleError("银粉阻断了本夜的狼人与雪狼行动")
        if qtype != QuestionType.LOVER_2 and user_id in room.night_actions:
            raise GameRuleError("该夜间行动已经提交")
        target, second = self._parse_targets(room, actor, qtype, target_token)
        # Werewolf.cs:5282 —— 纵火者的目标列表排除已浇油的玩家。
        if qtype == QuestionType.DOUSE and target is not None and target.doused:
            raise GameRuleError("这户人家已经浇过油了")
        if qtype == QuestionType.LOVER_1:
            if target is None:
                raise GameRuleError("首夜必须选择目标")
            actor.metadata["cupid_first_target"] = target.user_id
            actor.choice = target.user_id
            actor.choice2 = None
            actor.action_day = room.day
            room.state_version += 1
            events = [DomainEvent(
                "lover_first",
                f"第一位恋人已选择为 {target.public_name}，请发送“恋人 座位号”选择第二位恋人。",
                public=False,
                target_user_id=user_id,
                metadata={"actions": [QuestionType.LOVER_2.value]},
            )]
            # Werewolf.cs:1069 —— 丘比特把自己选为恋人时解锁 SelfLoving。
            if target.user_id == actor.user_id:
                events.extend(self._add_achievement(actor, Achievement.SELF_LOVING))
            return events
        if qtype == QuestionType.LOVER_2:
            first_id = actor.metadata.pop("cupid_first_target", None)
            first = next((player for player in room.players if player.user_id == first_id), None)
            if first is None or target is None or target.user_id == first.user_id:
                raise GameRuleError("第二位恋人必须与第一位不同")
            # Werewolf.cs:1101 —— 第二位恋人同样判定 SelfLoving。
            self_loving = self._add_achievement(actor, Achievement.SELF_LOVING) \
                if target.user_id == actor.user_id else []
            target, second = first, target
            qtype = QuestionType.LOVER_1
        else:
            self_loving = []
        action_key = qtype.value
        room.night_actions[user_id] = NightAction(
            actor_id=user_id,
            action=action_key,
            target_id=target.user_id if target else None,
            day=room.day,
            second_target_id=second.user_id if second else None,
        )
        actor.choice = target.user_id if target else None
        actor.choice2 = second.user_id if second else None
        if qtype == QuestionType.DOUSE:
            actor.metadata["arsonist_ignite"] = (target_token or "").strip().casefold() in {
                "引燃", "burn", "ignite", "-2",
            }
        actor.action_day = room.day
        room.state_version += 1
        events = [DomainEvent("action_accepted", "行动已记录。", public=False, target_user_id=user_id)]
        events.extend(self_loving)
        if qtype == QuestionType.ROLE_MODEL and target:
            actor.role_model = target.user_id
        if qtype == QuestionType.LOVER_1 and target and second:
            events.append(DomainEvent("lovers_recorded", "两位恋人已记录，夜晚结算时建立关系。", public=False, target_user_id=user_id))
        if self._night_complete(room):
            events.extend(self.resolve_night(room))
        return events

    def _submit_wolf_second_action(
        self, room: GameRoom, actor: Player, target_token: str | None
    ) -> list[DomainEvent]:
        if room.phase != GamePhase.NIGHT or not room.statistics.get("wolf_cub_killed"):
            raise GameRuleError("当前没有狼崽死亡后的第二次袭击")
        if actor not in self._wolf_voters(room):
            raise GameRuleError("当前身份不能参与第二次狼人袭击")
        if actor.user_id not in room.night_actions:
            raise GameRuleError("请先提交本夜的第一次狼人袭击")
        value = (target_token or "").strip()
        target = None if value.casefold() in _SKIP else self._target(room, value, actor_id=actor.user_id)
        if target:
            if self._is_wolf(target):
                raise GameRuleError("狼人不能袭击狼人")
            first_choices = {
                action.target_id for action in room.night_actions.values()
                if action.day == room.day and action.action == QuestionType.KILL.value
            }
            if target.user_id in first_choices:
                raise GameRuleError("第二次袭击不能选择第一次袭击目标")
        room.night_actions[f"{actor.user_id}:Kill2"] = NightAction(
            actor_id=actor.user_id,
            action=QuestionType.KILL_2.value,
            target_id=target.user_id if target else None,
            day=room.day,
        )
        actor.choice2 = target.user_id if target else None
        room.state_version += 1
        events = [DomainEvent("action_accepted", "第二次袭击已记录。", public=False, target_user_id=actor.user_id)]
        if self._night_complete(room):
            events.extend(self._resolve_wolf_cub_second(room))
        return events

    @staticmethod
    def _question_for_action(action: str) -> QuestionType:
        aliases = {
            "wolf": QuestionType.KILL, "狼人": QuestionType.KILL, "kill": QuestionType.KILL, "袭击": QuestionType.KILL,
            "seer": QuestionType.SEE, "查验": QuestionType.SEE, "验人": QuestionType.SEE,
            "sorcerer": QuestionType.SEE, "巫师": QuestionType.SEE, "oracle": QuestionType.SEE, "神谕": QuestionType.SEE,
            "guard": QuestionType.GUARD, "守护": QuestionType.GUARD, "保护": QuestionType.GUARD,
            "visit": QuestionType.VISIT, "访问": QuestionType.VISIT, "harlot": QuestionType.VISIT,
            "detect": QuestionType.DETECT, "侦查": QuestionType.DETECT,
            "convert": QuestionType.CONVERT, "转化": QuestionType.CONVERT, "培养": QuestionType.CONVERT,
            "copy": QuestionType.ROLE_MODEL, "idol": QuestionType.ROLE_MODEL, "模仿": QuestionType.ROLE_MODEL, "偶像": QuestionType.ROLE_MODEL,
            "cupid": QuestionType.LOVER_1, "lover": QuestionType.LOVER_1, "恋人": QuestionType.LOVER_1,
            "serial_kill": QuestionType.SERIAL_KILL, "连环杀": QuestionType.SERIAL_KILL,
            "hunt_cult": QuestionType.HUNT, "猎杀教徒": QuestionType.HUNT,
            "shoot": QuestionType.SHOOT, "开枪": QuestionType.SHOOT,
            "silver": QuestionType.SPREAD_SILVER, "撒银": QuestionType.SPREAD_SILVER,
            "sandman": QuestionType.SANDMAN, "催眠": QuestionType.SANDMAN,
            "trouble": QuestionType.TROUBLE, "捣乱": QuestionType.TROUBLE,
            "chemistry": QuestionType.CHEMISTRY, "化学": QuestionType.CHEMISTRY,
            "freeze": QuestionType.FREEZE, "冻结": QuestionType.FREEZE,
            "douse": QuestionType.DOUSE, "纵火": QuestionType.DOUSE, "引燃": QuestionType.DOUSE,
            "thief": QuestionType.THIEF, "盗取": QuestionType.THIEF,
            "grave": QuestionType.VISIT, "挖掘": QuestionType.VISIT,
        }
        try:
            return aliases[action.strip().casefold()]
        except KeyError as exc:
            raise GameRuleError("未知角色行动") from exc

    def _parse_targets(
        self, room: GameRoom, actor: Player, qtype: QuestionType, raw: str | None
    ) -> tuple[Player | None, Player | None]:
        value = (raw or "").strip()
        if qtype in {QuestionType.SPREAD_SILVER, QuestionType.SANDMAN}:
            if value.casefold() not in _SKIP and value.casefold() not in _YES:
                raise GameRuleError("该行动只需发送确认或跳过")
            return None, None
        if qtype == QuestionType.DOUSE and value.casefold() in {"引燃", "burn", "ignite", "-2"}:
            return None, None
        if qtype == QuestionType.TROUBLE:
            if value.casefold() not in _SKIP and value.casefold() not in _YES:
                raise GameRuleError("该行动只需发送确认或跳过")
            return None, None
        if value.casefold() in _SKIP:
            if room.day == 1 and actor.role in {
                Role.CUPID,
                Role.DOPPELGANGER,
                Role.WILD_CHILD,
                Role.THIEF,
            }:
                raise GameRuleError("首夜必须选择目标")
            return None, None
        parts = value.replace(",", " ").split()
        if qtype == QuestionType.LOVER_1 and len(parts) != 1:
            raise GameRuleError("请先选择第一位恋人")
        if qtype == QuestionType.LOVER_2 and len(parts) != 1:
            raise GameRuleError("请单独选择第二位恋人")
        target = self._target(
            room,
            parts[0],
            actor_id=actor.user_id,
            allow_self=qtype in {QuestionType.LOVER_1, QuestionType.LOVER_2},
        )
        second = None
        if qtype == QuestionType.KILL and self._is_wolf(target):
            raise GameRuleError("狼人不能袭击狼人")
        if qtype == QuestionType.FREEZE and self._is_wolf(target):
            raise GameRuleError("雪狼不能冻结狼人")
        if qtype == QuestionType.FREEZE and target:
            blocked = set(room.statistics.get("snow_blocked_ids") or ())
            if target.user_id in blocked or target.frozen or target.is_frozen:
                raise GameRuleError("不能连续两夜冻结同一名玩家")
        return target, second

    def _night_required(self, room: GameRoom, player: Player) -> bool:
        # 官方 SendNightActions 会跳过醉酒玩家；这里同时拦截补交的文字行动。
        if not player.alive or player.drunk:
            return False
        if room.statistics.get("silver_spread") or room.statistics.get("silver_night"):
            if player.role in WOLF_ROLES or player.role == Role.SNOW_WOLF:
                return False
        if player.role == Role.APPRENTICE_SEER and not player.metadata.get("awakened"):
            return False
        if player.role in {Role.WILD_CHILD, Role.DOPPELGANGER, Role.CUPID} and room.day != 1:
            return False
        if player.role == Role.THIEF and (
            (not room.rules.thief_full and room.day != 1) or player.has_used_ability
        ):
            return False
        if player.role == Role.CHEMIST and not player.has_used_ability:
            return False
        return bool(tuple(action for action in ROLE_ACTIONS.get(player.role, ()) if action not in DAY_ACTIONS))

    def _night_complete(self, room: GameRoom) -> bool:
        if room.statistics.get("sandman_sleep"):
            return True
        required = {
            p.user_id for p in room.players if self._night_required(room, p)
        }
        if not required.issubset(room.night_actions):
            return False
        if not room.statistics.get("wolf_cub_killed"):
            return True
        wolves = {player.user_id for player in self._wolf_voters(room)}
        second_submissions = {
            action.actor_id for key, action in room.night_actions.items()
            if key.endswith(":Kill2") and action.day == room.day
        }
        return wolves.issubset(second_submissions)

    @staticmethod
    def _wolf_voters(room: GameRoom) -> list[Player]:
        return [
            player for player in room.players
            if player.alive and player.role in WOLF_ROLES and not player.drunk
        ]

    def _wolf_second_prompts(self, room: GameRoom) -> list[DomainEvent]:
        return [
            DomainEvent(
                "wolf_cub_second_prompt",
                "狼崽已死亡，狼人可以发送“狼人二击 座位号”进行第二次袭击，或发送“跳过”。",
                public=False,
                target_user_id=player.user_id,
                metadata={"action": QuestionType.KILL_2.value},
            )
            for player in self._wolf_voters(room)
        ]

    def _submit_hunter_action(
        self, room: GameRoom, actor: Player, target_token: str | None
    ) -> list[DomainEvent]:
        if room.phase not in {GamePhase.NIGHT, GamePhase.DAY, GamePhase.VOTE}:
            raise GameRuleError("当前阶段不能发动猎人能力")
        pending = actor.metadata.get("pending_hunt")
        if not pending:
            raise GameRuleError("你当前没有待处理的猎人行动")
        lynched = bool(pending.get("lynched")) if isinstance(pending, dict) else False
        events: list[DomainEvent] = []
        if (target_token or "").strip().casefold() in _SKIP:
            # Werewolf.cs:5457-5461 —— Choice == -1，公开播报猎人放弃开枪。
            actor.metadata.pop("pending_hunt", None)
            events.append(DomainEvent(
                "hunter_skipped",
                get_locale_string(
                    "HunterSkipChoiceLynched" if lynched else "HunterSkipChoiceShot",
                    None,
                    self._name(actor),
                ),
                metadata={"user_id": actor.user_id},
            ))
            events.append(DomainEvent(
                "action_accepted", "已放弃最后一击。", public=False, target_user_id=actor.user_id
            ))
            events.extend(self._resume_after_hunter_window(room))
            return events
        target = self._target(room, target_token or "", actor_id=actor.user_id)
        actor.metadata.pop("pending_hunt", None)
        # Werewolf.cs:5468-5487 —— HunterFinalShot 的官方顺序：
        # 长老降级 → HeyManNiceShot → CheckRoleChanges → Domino → 击杀 → CheckRoleChanges。
        if target.role == Role.WISE_ELDER:
            events.extend(self._transform_checks(actor, method="KillElder"))
            actor.role = Role.VILLAGER
            actor.changed_roles_count += 1
            # Werewolf.cs:5473 —— HunterKilledWiseElder 走 SendWithQueue，是公开播报。
            events.append(DomainEvent(
                "hunter_killed_elder",
                get_locale_string(
                    "HunterKilledWiseElder",
                    None,
                    self._name(actor),
                    self._name(target),
                ),
                metadata={"user_id": actor.user_id, "target_id": target.user_id},
            ))
            events.extend(self._add_achievement(actor, Achievement.DEMOTED_BY_THE_DEATH))
        if target.role in MAJORITY_WOLF_ROLES or target.role == Role.SERIAL_KILLER:
            events.extend(self._add_achievement(actor, Achievement.HEY_MAN_NICE_SHOT))
        events.extend(self._resolve_role_changes(room))
        if target.role == Role.HUNTER:
            events.extend(self._add_achievement(actor, Achievement.DOMINO))
        actor.metadata["final_shot_lynched"] = lynched
        events.extend(self._kill(room, target, KillMethod.HUNTER, source=actor, is_night=False))
        actor.metadata.pop("final_shot_lynched", None)
        events.append(DomainEvent("action_accepted", "猎人行动已记录。", public=False, target_user_id=actor.user_id))
        events.extend(self._resolve_role_changes(room))
        events.extend(self._check_for_game_end(room))
        events.extend(self._resume_after_hunter_window(room))
        return events

    def _hunter_window(self, room: GameRoom, resume: str) -> bool:
        """Werewolf.cs:5442-5449 —— HunterFinalShot 的 30 秒阻塞循环独占整局流程。

        端口没有阻塞线程，用一次性窗口复刻同样的串行语义：窗口关闭前不推进阶段，
        `resume` 记录窗口结束后要继续执行的那一段流程。
        """
        if room.phase == GamePhase.FINISHED:
            return False
        if not any(player.metadata.get("pending_hunt") for player in room.players):
            return False
        room.statistics["hunter_window"] = resume
        room.stage_started_at = self._now()
        room.stage_deadline = self._deadline(_HUNTER_WINDOW_SECONDS)
        room.state_version += 1
        return True

    def _resume_after_hunter_window(self, room: GameRoom) -> list[DomainEvent]:
        """猎人开枪或放弃后收尾：还有待处理的猎人就续窗口（对应官方递归调用），否则继续流程。"""
        resume = room.statistics.get("hunter_window")
        if resume is None:
            return []
        if room.phase != GamePhase.FINISHED and any(
            player.metadata.get("pending_hunt") for player in room.players
        ):
            room.stage_started_at = self._now()
            room.stage_deadline = self._deadline(_HUNTER_WINDOW_SECONDS)
            room.state_version += 1
            return []
        room.statistics.pop("hunter_window", None)
        return self._continue_after_hunter_window(room, str(resume))

    def _close_hunter_window(self, room: GameRoom) -> list[DomainEvent]:
        """窗口 30 秒到点：官方 Choice == 0 分支，公开播报没来得及开枪并私聊 TimesUp。"""
        resume = room.statistics.pop("hunter_window", None)
        events: list[DomainEvent] = []
        for player in room.players:
            pending = player.metadata.pop("pending_hunt", None)
            if not pending:
                continue
            lynched = bool(pending.get("lynched")) if isinstance(pending, dict) else False
            events.append(DomainEvent(
                "hunter_no_choice",
                get_locale_string(
                    "HunterNoChoiceLynched" if lynched else "HunterNoChoiceShot",
                    None,
                    self._name(player),
                ),
                metadata={"user_id": player.user_id},
            ))
            # Werewolf.cs:5455 —— 超时同时把猎人那条私聊菜单改写成 TimesUp。
            events.append(DomainEvent(
                "times_up",
                get_locale_string("TimesUp"),
                public=False,
                target_user_id=player.user_id,
            ))
        events.extend(self._continue_after_hunter_window(room, str(resume or "")))
        return events

    def _continue_after_hunter_window(self, room: GameRoom, resume: str) -> list[DomainEvent]:
        if room.phase == GamePhase.FINISHED:
            return []
        if resume == "day":
            return self._finish_or_start_day(room)
        if resume == "day_only":
            return self._finish_or_start_day(room, night_end=False)
        if resume == "vote":
            return self._begin_vote(room)
        if resume == "night":
            return self._after_lynch(room)
        return []

    def resolve_night(self, room: GameRoom) -> list[DomainEvent]:
        if room.phase != GamePhase.NIGHT:
            return []
        actions = [action for action in room.night_actions.values() if action.day == room.day]
        by_type: dict[str, list[NightAction]] = {}
        for action in actions:
            by_type.setdefault(action.action, []).append(action)
        events: list[DomainEvent] = []
        for player in self._players(room):
            player.was_saved_last_night = False
            player.died_last_night = False
        if room.statistics.get("sandman_sleep"):
            events.append(DomainEvent("sandman", "沙人催眠了村庄，本夜没有角色行动。"))
            room.day_actions.clear()
            room.statistics.pop("wolf_cub_killed", None)
            room.statistics.pop("silver_spread", None)
            room.statistics.pop("silver_night", None)
            for player in room.players:
                player.drunk = False
            events.extend(self._finish_or_start_day(room, night_end=False))
            return events
        events.extend(self._validate_first_night_picks(room, by_type))
        events.extend(self._resolve_role_models(room, by_type))
        events.extend(self._resolve_lovers(room, by_type))
        # Werewolf.cs:1753/1785 —— ValidateSpecialRoleChoices 只在首夜执行，末尾调用 NotifyLovers。
        if room.day == 1:
            events.extend(self._notify_lovers(room))
        ga, ga_target = self._capture_ga(room)
        silver_night = bool(
            room.statistics.pop("silver_night", False) or room.statistics.get("silver_spread")
        )
        if not silver_night:
            for action in by_type.get(QuestionType.FREEZE.value, ()):
                events.extend(self._resolve_snow_freeze(room, action, ga, ga_target))
                if ga and (ga.frozen or ga.is_frozen):
                    ga, ga_target = None, None
        events.extend(self._resolve_arsonist(room, by_type, ga, ga_target))
        cub_second = bool(room.statistics.pop("wolf_cub_killed", False))
        if not silver_night:
            room.statistics["eat_count"] = 0
            wolf_choice_ids: list[str] = []
            first_wolf_targets = self._wolf_vote_targets(room, by_type, QuestionType.KILL)
            for victim in first_wolf_targets:
                wolf_choice_ids.append(victim.user_id)
                events.extend(self._resolve_wolf_attack(room, victim, ga, ga_target))
            if cub_second:
                first_target_ids = {victim.user_id for victim in first_wolf_targets}
                for victim in self._wolf_vote_targets(
                    room, by_type, QuestionType.KILL_2, excluded_target_ids=first_target_ids
                ):
                    if victim.alive:
                        wolf_choice_ids.append(victim.user_id)
                        events.extend(self._resolve_wolf_attack(room, victim, ga, ga_target))
            # Werewolf.cs:3484-3492 —— 同夜两次成功进食时结算 IHelped / IncreaseThePack。
            if int(room.statistics.pop("eat_count", 0)) == 2:
                dead_cubs = [
                    player for player in room.players
                    if player.role == Role.WOLF_CUB and not player.alive
                ]
                if dead_cubs:
                    # 官方按 TimeDied 倒序取最近死亡的狼幼；本移植用死亡序号等价替代。
                    cub = max(dead_cubs, key=lambda p: int(p.metadata.get("death_sequence", 0)))
                    events.extend(self._add_achievement(cub, Achievement.I_HELPED))
                bitten_count = sum(
                    1 for player in room.players
                    if player.user_id in wolf_choice_ids and player.bitten
                )
                if bitten_count == 2:
                    alpha = next(
                        (
                            player for player in self._wolf_voters(room)
                            if player.role == Role.ALPHA_WOLF
                            and not player.drunk
                            and not player.is_frozen
                        ),
                        None,
                    )
                    if alpha is not None:
                        events.extend(
                            self._add_achievement(alpha, Achievement.INCREASE_THE_PACK)
                        )
        elif by_type.get(QuestionType.KILL.value) or by_type.get(QuestionType.FREEZE.value):
            events.append(DomainEvent("night_saved", "银粉阻断了本夜的狼人袭击。"))
        room.statistics.pop("silver_spread", None)
        for action in by_type.get(QuestionType.SERIAL_KILL.value, ()):
            events.extend(self._resolve_serial_kill(room, action, ga, ga_target))
        events.extend(self._resolve_cult_hunter(room, by_type))
        events.extend(self._resolve_conversions(room, by_type))
        events.extend(self._resolve_chemistry(room, by_type, ga))
        events.extend(self._resolve_visits(room, by_type))
        events.extend(self._resolve_information(room, by_type, {QuestionType.SEE}))
        events.extend(self._resolve_augur(room))
        events.extend(self._resolve_guard_visits(room, by_type, ga, ga_target))
        events.extend(self._wake_apprentice(room))
        events.extend(self._resolve_role_changes(room))
        events.extend(self._resolve_thief(room, by_type))
        # Werewolf.cs:4433-4437 —— 夜里无人死亡时公告 NoAttack（官方三条文案随机取一）。
        if not any(player.died_last_night for player in room.players):
            events.append(DomainEvent("no_attack", get_locale_string("NoAttack")))
        events.extend(self._finish_or_start_day(room))
        return events

    def _wolf_vote_targets(
        self,
        room: GameRoom,
        by_type: dict[str, list[NightAction]],
        question: QuestionType,
        excluded_target_ids: set[str] | None = None,
    ) -> list[Player]:
        votes = [
            action.target_id for action in by_type.get(question.value, ())
            if action.target_id and action.target_id not in (excluded_target_ids or set())
        ]
        if not votes:
            return []
        counts = Counter(votes)
        maximum = max(counts.values())
        tied = {key for key, value in counts.items() if value == maximum}
        winner = next((player for player in room.players if player.user_id in tied), None)
        return [winner] if winner else []

    def _validate_first_night_picks(
        self, room: GameRoom, by_type: dict[str, list[NightAction]]
    ) -> list[DomainEvent]:
        if room.day != 1:
            return []
        events: list[DomainEvent] = []
        living = [player for player in self._players(room)]
        for player in living:
            if player.role not in {Role.WILD_CHILD, Role.DOPPELGANGER} or player.role_model:
                continue
            choices = [item for item in room.players if item.user_id != player.user_id]
            if not choices:
                continue
            self.rng.shuffle(choices)
            self.rng.shuffle(choices)
            player.role_model = choices[0].user_id
            player.has_used_ability = True
            events.append(DomainEvent(
                "role_model_forced",
                f"你没有选择目标，系统已指定 {choices[0].public_name}。",
                public=False,
                target_user_id=player.user_id,
            ))
        lovers = [player for player in room.players if player.lover_id]
        cupid = next((player for player in living if player.role == Role.CUPID), None)
        # 官方在回调里就写好 LoverId，本移植延后到 _resolve_lovers；
        # 已提交完整选择时不再随机配对。
        cupid_picked = any(
            action.target_id and action.second_target_id
            for action in by_type.get(QuestionType.LOVER_1.value, ())
        )
        if cupid and not cupid_picked and len(lovers) != 2:
            pool = [player for player in living]
            self.rng.shuffle(pool)
            self.rng.shuffle(pool)
            if len(pool) >= 2:
                first, second = pool[0], pool[1]
                first.lover_id, second.lover_id = second.user_id, first.user_id
                first.metadata["in_love"] = second.metadata["in_love"] = True
                # Werewolf.cs:1898 AddLover —— 由系统补齐的恋人记 SpeedDating。
                first.speed_dating = second.speed_dating = True
                cupid.has_used_ability = True
                events.append(DomainEvent("lovers_linked", "丘比特未完整指定恋人，系统已随机配对。"))
        return events

    def _notify_lovers(self, room: GameRoom) -> list[DomainEvent]:
        """Werewolf.cs:1868-1900 NotifyLovers —— 仅在存在丘比特时结算恋人成就。"""
        if not any(player.role == Role.CUPID for player in room.players):
            return []
        lovers = [player for player in room.players if player.lover_id]
        if len(lovers) != 2:
            return []
        events: list[DomainEvent] = []
        seer_sorcerer = (
            any(player.role == Role.SEER for player in lovers)
            and any(player.role == Role.SORCERER for player in lovers)
        )
        for lover in lovers:
            if lover.speed_dating:
                events.extend(self._add_achievement(lover, Achievement.ONLINE_DATING))
            if lover.role == Role.DOPPELGANGER and lover.role_model == lover.lover_id:
                events.extend(self._add_achievement(lover, Achievement.DEEP_LOVE))
            if seer_sorcerer:
                events.extend(self._add_achievement(lover, Achievement.SEEING_BETWEEN_TEAMS))
        return events

    def _resolve_snow_freeze(
        self,
        room: GameRoom,
        action: NightAction,
        ga: Player | None,
        ga_target: str | None,
    ) -> list[DomainEvent]:
        actor = self._find(room, action.actor_id)
        target = next((player for player in room.players if player.user_id == action.target_id), None)
        if actor.role != Role.SNOW_WOLF or not actor.alive or target is None:
            return []
        events: list[DomainEvent] = []
        result = self._visit_player(room, actor, target, events)
        if result != "success":
            return events
        # Werewolf.cs:3101-3111 —— 连环杀手分支排在守护判定之前，
        # 守护天使守护连环杀手也无法阻止雪狼的冻结。
        if target.role == Role.SERIAL_KILLER:
            target.frozen = target.is_frozen = True
            events.append(DomainEvent(
                "snow_frozen", f"你冻结了 {target.public_name}。",
                public=False, target_user_id=actor.user_id,
            ))
            return events
        if self._is_guarded(ga, ga_target, target.user_id):
            target.was_saved_last_night = True
            events.append(DomainEvent(
                "guard_blocked_snow", "守护阻断了雪狼的冻结。",
                public=False, target_user_id=actor.user_id,
            ))
            return events
        if target.role == Role.HUNTER:
            if self.rng.randrange(100) < room.rules.snow_hunter_freeze_chance:
                target.frozen = target.is_frozen = True
                events.append(DomainEvent(
                    "snow_frozen", f"你冻结了 {target.public_name}。",
                    public=False, target_user_id=actor.user_id,
                ))
            else:
                events.extend(self._kill(room, actor, KillMethod.HUNTER, source=target, hunter_final_shot=False))
            return events
        target.frozen = target.is_frozen = True
        if target.role == Role.HARLOT:
            # Werewolf.cs:3131 —— 冻住娼妇后标记 FrozeHarlot，结算时解锁 ColdAsIce。
            actor.froze_harlot = True
        if target.role == Role.GRAVE_DIGGER:
            target.metadata["dug_graves_last_night"] = 0
        if target.role == Role.ARSONIST:
            events.append(DomainEvent(
                "arsonist_not_frozen", "雪狼试图冻结纵火者，纵火行动仍会结算。",
                public=False, target_user_id=actor.user_id,
            ))
        else:
            events.append(DomainEvent(
                "snow_frozen", f"你冻结了 {target.public_name}。",
                public=False, target_user_id=actor.user_id,
            ))
        return events

    def _resolve_serial_kill(
        self,
        room: GameRoom,
        action: NightAction,
        ga: Player | None,
        ga_target: str | None,
    ) -> list[DomainEvent]:
        actor = self._find(room, action.actor_id)
        target = next((player for player in room.players if player.user_id == action.target_id), None)
        events: list[DomainEvent] = []
        if not actor.alive or actor.is_frozen:
            return events
        if target is None:
            self._maybe_spot_grave_digger(room, actor, events)
            return events
        result = self._visit_player(room, actor, target, events)
        if result != "success":
            self._maybe_spot_grave_digger(room, actor, events)
            return events
        stumbled = int(actor.metadata.get("stumbled_grave_day", 0) or 0)
        old_target: Player | None = None
        # 官方在连环杀手前一夜踩到墓地后，下一夜改杀随机目标的概率固定为 50%。
        # serial_killer_stumble_chance 只用于狼人访问连环杀手时的绊倒判定。
        if stumbled and stumbled + 1 == room.day and self.rng.randrange(100) < 50:
            others = [player for player in self._players(room) if player.user_id not in {actor.user_id, target.user_id}]
            if others:
                old_target = target
                target = self.rng.choice(others)
                result = self._visit_player(room, actor, target, events)
                if result != "success":
                    self._maybe_spot_grave_digger(room, actor, events)
                    return events
        if self._is_guarded(ga, ga_target, target.user_id) and target.role != Role.HARLOT:
            target.was_saved_last_night = True
            events.append(DomainEvent(
                "guard_blocked_serial", "守护阻断了连环杀手的袭击。",
                public=False, target_user_id=actor.user_id,
            ))
            # Werewolf.cs:3524 —— 因绊倒改杀的随机目标被守卫挡下时解锁 ReallyBadLuck。
            if old_target is not None:
                events.extend(self._add_achievement(actor, Achievement.REALLY_BAD_LUCK))
        else:
            events.extend(self._kill(room, target, KillMethod.SERIAL_KILLED, source=actor))
            # Werewolf.cs:3531-3532 —— 杀掉狼系（含雪狼）累计 SerialKilledWolvesCount。
            if target.role in MAJORITY_WOLF_ROLES:
                actor.serial_killed_wolves_count += 1
            # Werewolf.cs:3533 —— SendGif(SerialKillerKilledYou) 私聊被杀者本人。
            events.append(self._victim_notice(target, "SerialKillerKilledYou"))
        self._maybe_spot_grave_digger(room, actor, events)
        return events

    def _maybe_spot_grave_digger(
        self,
        room: GameRoom,
        killer: Player,
        events: list[DomainEvent],
        killers: list[Player] | None = None,
    ) -> None:
        digger = next((player for player in self._players(room) if player.role == Role.GRAVE_DIGGER), None)
        if digger is None:
            return
        graves = int(digger.metadata.get("dug_graves_last_night", 0))
        if graves < 1:
            return
        chance = room.rules.grave_digger_spot_chance
        if chance < 0:
            chance = (20 + (30 - (30 * (0.5 ** (graves - 1))))) / 2
        if self.rng.randrange(100) < chance:
            events.extend(self._kill(
                room, digger, KillMethod.SPOTTED,
                source=killer, killers=killers, hunter_final_shot=False,
            ))
            # Werewolf.cs:3479-3481 / 3544-3546 —— 私聊凶手（狼群逐个 / 连环杀手）
            # 与被发现的掘墓人本人。
            if killer.role == Role.SERIAL_KILLER:
                events.append(DomainEvent(
                    "serial_killer_spotted_digger",
                    f"你提着还在滴血的刀走过墓地时，注意到了掘墓人 {digger.public_name}。"
                    f"顺着本能，你决定今天要有两名受害者……",
                    public=False,
                    target_user_id=killer.user_id,
                ))
                events.append(self._victim_notice(digger, "SerialKillerSpottedYou"))
            else:
                for wolf in (killers if killers is not None else [killer]):
                    events.append(DomainEvent(
                        "wolves_spotted_digger",
                        f"狼群跑过墓地时，突然注意到了掘墓人 {digger.public_name}。"
                        f"还没等他反应过来，你们中的一位就扑到他背上，一口结果了他。",
                        public=False,
                        target_user_id=wolf.user_id,
                    ))
                events.append(self._victim_notice(digger, "WolvesSpottedYou"))

    def _resolve_wolf_cub_second(self, room: GameRoom) -> list[DomainEvent]:
        """兼容入口：同一夜里狼人提交的第二次袭击投票。"""
        return self.resolve_night(room)

    def _resolve_wolf_attack(
        self,
        room: GameRoom,
        victim: Player,
        ga: Player | None = None,
        ga_target: str | None = None,
    ) -> list[DomainEvent]:
        attackers = [
            player for player in self._wolf_voters(room)
            if not player.drunk and not player.is_frozen
        ]
        if not attackers:
            return []
        events: list[DomainEvent] = []
        visitor = self.rng.choice(attackers)
        result = self._visit_player(room, visitor, victim, events)
        if result != "success":
            # Werewolf.cs:3478 —— KillPlayer(gd, Spotted, killers: voteWolves)。
            self._maybe_spot_grave_digger(room, visitor, events, killers=attackers)
            return events
        # Werewolf.cs:3440 —— VisitResult.Success 分支末尾统一 eatCount++（含守卫拦下的情况）。
        room.statistics["eat_count"] = int(room.statistics.get("eat_count", 0)) + 1
        if self._is_guarded(ga, ga_target, victim.user_id):
            victim.was_saved_last_night = True
            self._maybe_spot_grave_digger(room, visitor, events, killers=attackers)
            # Werewolf.cs:3281-3285 —— GuardBlockedWolf 只私聊每头投票狼，公开频道无提示。
            for wolf in attackers:
                events.append(DomainEvent(
                    "guard_blocked_wolf",
                    f"守卫守护了 {victim.public_name}，狼人今晚没吃的了。",
                    public=False,
                    target_user_id=wolf.user_id,
                ))
            return events
        if victim.role == Role.CURSED:
            events.extend(self._transform_checks(victim, method="BiteCursed"))
            victim.role = Role.WOLF
            victim.metadata["wolf_aligned"] = True
            victim.changed_roles_count += 1
            self._maybe_spot_grave_digger(room, visitor, events, killers=attackers)
            # Werewolf.cs:3300-3301 / 2018-2045 —— Transform(BiteCursed) 只发私聊，
            # 被咬者收到 CursedBitten + 狼群名单，狼群与雪狼收到 CursedBittenToWolves。
            pack = [
                member for member in self._players(room)
                if member.user_id != victim.user_id
                and member.role in MAJORITY_WOLF_ROLES
            ]
            events.append(DomainEvent(
                "cursed_turned_wolf",
                "你被狼人袭击，基因缺陷被激活，你转化为狼人了。"
                + (f" 当前狼群：{'、'.join(member.public_name for member in pack)}。" if pack else ""),
                public=False,
                target_user_id=victim.user_id,
                metadata={
                    "role": Role.WOLF.value,
                    "teammates": [member.user_id for member in pack],
                },
            ))
            for member in pack:
                events.append(DomainEvent(
                    "cursed_bitten_to_wolves",
                    f"{victim.public_name} 被袭击后没有死，反而转化成了狼人，成为你们的一员。",
                    public=False,
                    target_user_id=member.user_id,
                    metadata={"new_member_id": victim.user_id, "role": Role.WOLF.value},
                ))
            return events
        alpha = next((player for player in attackers if player.role == Role.ALPHA_WOLF), None)
        bitten = bool(
            alpha and self.rng.randrange(100) < room.rules.alpha_wolf_conversion_chance
        )
        # Werewolf.cs:3296-3298 —— 吃到在家的娼妇，全体投票狼解锁 DontStayHome。
        if victim.role == Role.HARLOT:
            for wolf in attackers:
                events.extend(self._add_achievement(wolf, Achievement.DONT_STAY_HOME))
        if victim.role == Role.DRUNK and bitten and alpha is not None:
            # Werewolf.cs:3306 —— 头狼咬中酒鬼且成功感染，自己不会喝醉。
            events.extend(self._add_achievement(alpha, Achievement.LUCKY_DAY))
        if victim.role == Role.TRAITOR and not bitten:
            # Werewolf.cs:3414-3417 —— 场上只剩一头狼系时吃掉叛徒解锁 ConditionRed。
            wolf_alive = sum(
                1 for member in self._players(room) if member.role in MAJORITY_WOLF_ROLES
            )
            if wolf_alive == 1 and attackers:
                events.extend(self._add_achievement(attackers[0], Achievement.CONDITION_RED))
        if victim.role == Role.HUNTER:
            chance = min(100, room.rules.hunter_kill_wolf_chance_base + (len(attackers) - 1) * 20)
            if self.rng.randrange(100) < chance:
                shot = self.rng.choice(attackers)
                # Werewolf.cs:3345-3355 —— 猎人反杀计数与同夜二次反杀成就。
                victim.has_shot_hunter_attacker += 1
                if victim.has_shot_hunter_attacker == 2:
                    events.extend(self._add_achievement(victim, Achievement.HELPFUL_PARANOIA))
                if victim.has_shot_hunter_attacker_this_night:
                    events.extend(self._add_achievement(victim, Achievement.S_TIER_HUNTER))
                victim.has_shot_hunter_attacker_this_night = True
                events.extend(self._kill(room, shot, KillMethod.HUNTER, source=victim))
                if len(attackers) > 1:
                    events.extend(
                        self._kill(
                            room,
                            victim,
                            KillMethod.EAT,
                            source=attackers[0],
                            # Werewolf.cs:3367 —— killers: voteWolves，全体投票狼计入击杀。
                            killers=attackers,
                            hunter_final_shot=False,
                        )
                    )
                    # Werewolf.cs:3365 —— SendGif(WolvesEatYou) 私聊被吃的猎人。
                    events.append(self._victim_notice(victim, "WolvesEatYou"))
                self._maybe_spot_grave_digger(room, visitor, events, killers=attackers)
                return events
        if victim.role == Role.WISE_ELDER and not bitten and not victim.has_used_ability:
            victim.has_used_ability = True
            victim.was_saved_last_night = True
            self._maybe_spot_grave_digger(room, visitor, events, killers=attackers)
            # Werewolf.cs:3398-3404 —— 长老首次被吃只私聊狼群和长老本人，公开频道无提示。
            for wolf in attackers:
                events.append(DomainEvent(
                    "wolves_tried_to_eat_wise_elder",
                    f"你们试图侵入 {victim.public_name} 的家，"
                    f"但 {victim.public_name} 是长老，成功阻止了这次攻击，好在他没有认出你们。",
                    public=False,
                    target_user_id=wolf.user_id,
                ))
            events.append(DomainEvent(
                "wise_elder_saved",
                "狼人似乎想吃掉你，但你成功阻止了攻击，可惜没有认出任何攻击者。",
                public=False,
                target_user_id=victim.user_id,
            ))
            return events
        if bitten:
            victim.bitten = True
            if alpha is not None and victim.role == Role.SERIAL_KILLER:
                alpha.metadata["strongest_alpha"] = True
            self._maybe_spot_grave_digger(room, visitor, events, killers=attackers)
            # Werewolf.cs:1943-1950 BitePlayer —— 私聊全部狼系（含雪狼）PlayerBittenWolves；
            # 被咬者的 PlayerBitten 私聊在 4452-4458 的夜末循环发送，公开频道无提示。
            alpha_name = alpha.public_name if alpha is not None else "头狼"
            for wolf in self._players(room):
                if wolf.role in MAJORITY_WOLF_ROLES:
                    events.append(DomainEvent(
                        "player_bitten_wolves",
                        f"狼人袭击 {victim.public_name} 时，头狼 {alpha_name} 阻止了大家，"
                        f"{alpha_name} 认为 {victim.public_name} 应该成为狼人，明日他将转化。",
                        public=False,
                        target_user_id=wolf.user_id,
                    ))
            events.append(DomainEvent(
                "player_bitten",
                "你被狼人袭击了，明日你将变成狼人。",
                public=False,
                target_user_id=victim.user_id,
            ))
            return events
        events.extend(self._kill(
            room,
            victim,
            KillMethod.EAT,
            source=attackers[0],
            # Werewolf.cs:3428 —— killers: voteWolves，全体投票狼计入击杀。
            killers=attackers,
            # Official queues HunterFinalShot after night notifications; do not drop it.
            hunter_final_shot=True,
        ))
        # Werewolf.cs:3311-3312 / 3434 —— SendGif(WolvesEatYou) 私聊被吃者本人
        # （酒鬼分支与默认分支共用同一段文案）。
        events.append(self._victim_notice(victim, "WolvesEatYou"))
        # Werewolf.cs:3430-3433 —— 吃掉自己的巫师，全体投票狼解锁 NoSorcery。
        if victim.role == Role.SORCERER:
            for wolf in attackers:
                events.extend(self._add_achievement(wolf, Achievement.NO_SORCERY))
        if victim.role == Role.DRUNK:
            for attacker in attackers:
                attacker.drunk = True
        self._maybe_spot_grave_digger(room, visitor, events, killers=attackers)
        return events

    def _resolve_information(
        self,
        room: GameRoom,
        by_type: dict[str, list[NightAction]],
        question_types: set[QuestionType],
    ) -> list[DomainEvent]:
        """只在官方的结算边界上发放信息，不提前也不延后。"""
        events: list[DomainEvent] = []
        if QuestionType.SEE in question_types:
            for action in by_type.get(QuestionType.SEE.value, ()):
                actor = self._find(room, action.actor_id)
                target = next((p for p in room.players if p.user_id == action.target_id), None)
                if not actor.alive or actor.is_frozen or target is None:
                    continue
                if actor.role == Role.FOOL:
                    possible_roles = [
                        player.role for player in self._players(room)
                        if player.user_id != actor.user_id and player.role not in {Role.SEER, None}
                    ]
                    self.rng.shuffle(possible_roles)
                    self.rng.shuffle(possible_roles)
                    role = possible_roles[0] if possible_roles else Role.VILLAGER
                    if role in WOLF_ROLES:
                        role = Role.WOLF
                    # Werewolf.cs:4003-4009 —— 傻子的幻觉是否恰好命中真实身份。
                    if role == target.role or (role == Role.WOLF and target.role in WOLF_ROLES):
                        actor.fool_correct_see_count += 1
                    if role == Role.BEHOLDER and target.role == Role.BEHOLDER:
                        actor.fool_correctly_seen_bh = True
                    if role in {Role.WOLF_MAN, Role.TRAITOR}:
                        actor.has_seen_impossible = True
                    event_name = "fool_result"
                elif actor.role == Role.SORCERER:
                    role = self._sorcerer_seen_role(target)
                    event_name = "sorcerer_result"
                elif actor.role == Role.ORACLE:
                    possible_roles = [
                        player.role for player in self._players(room)
                        if player.user_id != actor.user_id and player.role not in {target.role, None}
                    ]
                    self.rng.shuffle(possible_roles)
                    self.rng.shuffle(possible_roles)
                    if not possible_roles:
                        # Werewolf.cs:4034-4037 —— 无可用身份时预告失败并解锁 NowImBlind。
                        events.extend(self._add_achievement(actor, Achievement.NOW_IM_BLIND))
                    role = possible_roles[0] if possible_roles else Role.VILLAGER
                    event_name = "oracle_result"
                else:
                    # Werewolf.cs:3935-3938 —— 预言家查到守望者解锁 ShouldHaveKnown。
                    if target.role == Role.BEHOLDER:
                        events.extend(self._add_achievement(actor, Achievement.SHOULD_HAVE_KNOWN))
                    # Werewolf.cs:3946-3948 —— 狼人（WolfMan）被预言家查验后标记 Trustworthy。
                    if target.role == Role.WOLF_MAN:
                        target.trustworthy = True
                    role = self._seen_role(room, target)
                    event_name = "seer_result"
                verb = "不是" if actor.role == Role.ORACLE else "是"
                events.append(
                    DomainEvent(
                        event_name,
                        f"查验结果：{self._name(target)}的身份{verb}【{role_display_name(role)}】。",
                        public=False,
                        target_user_id=actor.user_id,
                        metadata={"seen_role": role.value, "target_id": target.user_id},
                    )
                )
        return events

    def _resolve_augur(self, room: GameRoom) -> list[DomainEvent]:
        """在官方夜晚边界上，揭示一个还没见过、且本局未出现的身份。"""
        events: list[DomainEvent] = []
        possible_roles = room.statistics.setdefault("possible_roles", [])
        if not isinstance(possible_roles, list):
            possible_roles = list(possible_roles)
            room.statistics["possible_roles"] = possible_roles
        # Werewolf.cs shuffles the persistent PossibleRoles collection once,
        # then takes the first role absent from the living roster.
        self.rng.shuffle(possible_roles)
        present_roles = {
            player.role for player in room.players
            if player.role is not None and (player.alive or player.died_last_night)
        }
        for augur in self._players(room):
            if augur.role != Role.AUGUR or augur.is_frozen:
                continue
            seen = list(augur.metadata.get("augur_saw_roles", ()))
            role = next(
                (
                    Role(item) for item in possible_roles
                    if item not in seen and Role(item) not in present_roles
                ),
                None,
            )
            if role is None:
                events.append(DomainEvent(
                    "augur_nothing", "你没有发现新的可能身份。",
                    public=False, target_user_id=augur.user_id,
                ))
                continue
            if (
                role == Role.SEER
                and any(player.alive and player.role == Role.APPRENTICE_SEER for player in room.players)
                and not any(player.alive and player.role == Role.SEER for player in room.players)
            ):
                role = Role.APPRENTICE_SEER
            seen.append(role.value)
            augur.metadata["augur_saw_roles"] = seen
            events.append(DomainEvent(
                "augur_result", f"你发现本局可能存在【{role_display_name(role)}】。",
                public=False, target_user_id=augur.user_id, metadata={"role": role.value},
            ))
        return events

    def _resolve_role_models(
        self, room: GameRoom, by_type: dict[str, list[NightAction]]
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for action in by_type.get(QuestionType.ROLE_MODEL.value, ()):
            actor = self._find(room, action.actor_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if not target or actor.has_used_ability:
                continue
            actor.has_used_ability = True
            actor.role_model = target.user_id
            if actor.role == Role.DOPPELGANGER:
                # The official implementation stores the role model first;
                # transformation is checked after the model dies.
                actor.metadata["copied_from"] = target.user_id
        return events

    def _resolve_role_changes(self, room: GameRoom, *, checkbitten: bool = False) -> list[DomainEvent]:
        """在与官方判定同一个边界上执行延迟生效的身份转化。"""
        events: list[DomainEvent] = []
        events.extend(self._promote_apprentice(room, checkbitten=checkbitten))
        for player in self._players(room):
            if checkbitten and player.bitten:
                continue
            model_id = player.role_model
            model = next((item for item in room.players if item.user_id == model_id), None)
            if not model or model.alive:
                continue
            if player.role == Role.WILD_CHILD:
                events.extend(self._transform_checks(player, method="WildChild"))
                player.role = Role.WOLF
                player.metadata["wolf_aligned"] = True
                player.metadata["role_actions"] = [
                    action.value for action in ROLE_ACTIONS.get(Role.WOLF, ())
                ]
                player.changed_roles_count += 1
                wolves = [
                    member for member in self._players(room)
                    if member.user_id != player.user_id
                    and member.role in MAJORITY_WOLF_ROLES
                ]
                events.append(DomainEvent(
                    "wild_child_changed", "你的偶像已经出局，你转化为狼人。",
                    public=False, target_user_id=player.user_id,
                    metadata={
                        "role": Role.WOLF.value,
                        "teammates": [member.user_id for member in wolves],
                    },
                ))
                for member in wolves:
                    events.append(DomainEvent(
                        "team_member_added",
                        f"{player.public_name} 已转化为狼人，加入狼人阵营。",
                        public=False,
                        target_user_id=member.user_id,
                        metadata={"new_member_id": player.user_id, "role": Role.WOLF.value},
                    ))
            elif player.role == Role.DOPPELGANGER:
                copied_role = model.role
                if copied_role is None:
                    continue
                # Werewolf.cs:1937 —— newRoleModel 取偶像的偶像，可能正是自己。
                events.extend(self._transform_checks(
                    player, method="Doppelgänger", new_role_model=model.role_model,
                ))
                player.role = copied_role
                player.role_model = model.role_model
                player.has_used_ability = False
                player.metadata["copied_from"] = model.user_id
                player.changed_roles_count += 1
                if copied_role in {Role.GUNNER, Role.SPUMPKIN}:
                    player.bullet_count = 2
                player.metadata["role_actions"] = [
                    action.value for action in ROLE_ACTIONS.get(copied_role, ())
                ]
                if copied_role in WOLF_ROLES:
                    player.metadata["wolf_aligned"] = True
                else:
                    player.metadata.pop("wolf_aligned", None)
                events.append(DomainEvent(
                    "role_changed", "你的模仿目标已经出局，身份转换已生效。",
                    public=False, target_user_id=player.user_id,
                    metadata={"role": copied_role.value},
                ))
                if copied_role == Role.MASON:
                    for member in self._players(room):
                        if member.role == Role.MASON and member.user_id != player.user_id:
                            events.append(DomainEvent(
                                "team_member_added",
                                f"{player.public_name} 已加入石匠阵营。",
                                public=False,
                                target_user_id=member.user_id,
                                metadata={"new_member_id": player.user_id, "role": copied_role.value},
                            ))
                elif copied_role == Role.SNOW_WOLF:
                    wolves = [
                        member for member in self._players(room)
                        if member.user_id != player.user_id
                        and member.role in WOLF_ROLES
                    ]
                    for member in wolves:
                        events.append(DomainEvent(
                            "team_member_added",
                            f"{player.public_name} 已加入雪狼阵营。",
                            public=False,
                            target_user_id=member.user_id,
                            metadata={"new_member_id": player.user_id, "role": copied_role.value},
                        ))
                elif copied_role in MAJORITY_WOLF_ROLES:
                    wolves = [
                        member for member in self._players(room)
                        if member.user_id != player.user_id
                        and member.role in MAJORITY_WOLF_ROLES
                    ]
                    for member in wolves:
                        events.append(DomainEvent(
                            "team_member_added",
                            f"{player.public_name} 已加入狼人阵营。",
                            public=False,
                            target_user_id=member.user_id,
                            metadata={"new_member_id": player.user_id, "role": copied_role.value},
                        ))
                elif copied_role == Role.CULTIST:
                    for member in self._players(room):
                        if member.role == Role.CULTIST and member.user_id != player.user_id:
                            events.append(DomainEvent(
                                "team_member_added",
                                f"{player.public_name} 已加入教会。",
                                public=False,
                                target_user_id=member.user_id,
                                metadata={"new_member_id": player.user_id, "role": copied_role.value},
                            ))
                elif copied_role == Role.SEER:
                    for member in self._players(room):
                        if member.role == Role.BEHOLDER:
                            events.append(DomainEvent(
                                "beholder_new_seer",
                                f"新的预言家是 {player.public_name}。",
                                public=False,
                                target_user_id=member.user_id,
                            ))
        events.extend(self._promote_apprentice(room, checkbitten=checkbitten))
        # Werewolf.cs:1736-1749 —— CheckRoleChanges 末尾统计狼群与预言家数量。
        wolves = [
            member for member in self._players(room)
            if member.role in WOLF_ROLES or member.role == Role.SNOW_WOLF
        ]
        if len(wolves) >= 7:
            for wolf in wolves:
                events.extend(self._add_achievement(wolf, Achievement.PACK_HUNTER))
        seers = [member for member in self._players(room) if member.role == Role.SEER]
        if len(seers) > 1:
            for seer in seers:
                events.extend(self._add_achievement(seer, Achievement.DOUBLE_VISION))
        return events

    def _resolve_lovers(
        self, room: GameRoom, by_type: dict[str, list[NightAction]]
    ) -> list[DomainEvent]:
        for action in by_type.get(QuestionType.LOVER_1.value, ()):
            actor = self._find(room, action.actor_id)
            target_a = next((p for p in room.players if p.user_id == action.target_id), None)
            target_b = next((p for p in room.players if p.user_id == action.second_target_id), None)
            if actor.role == Role.CUPID and target_a and target_b and target_a.user_id != target_b.user_id:
                target_a.lover_id, target_b.lover_id = target_b.user_id, target_a.user_id
                target_a.metadata["in_love"] = target_b.metadata["in_love"] = True
                actor.has_used_ability = True
                return [DomainEvent("lovers_linked", "丘比特已经建立恋人关系。")]
        return []

    def _resolve_conversions(
        self, room: GameRoom, by_type: dict[str, list[NightAction]]
    ) -> list[DomainEvent]:
        cultists = [
            player for player in self._players(room)
            if player.role == Role.CULTIST and not player.is_frozen
        ]
        proposals = [
            action for action in by_type.get(QuestionType.CONVERT.value, ())
            if action.actor_id in {player.user_id for player in cultists} and action.target_id
        ]
        if not cultists or not proposals:
            return []

        events: list[DomainEvent] = []
        votes = Counter(action.target_id for action in proposals)
        highest = max(votes.values())
        target_id = next(action.target_id for action in proposals if votes[action.target_id] == highest)
        target = next((player for player in room.players if player.user_id == target_id), None)
        if target is None or target.role == Role.CULTIST:
            return events
        visitor = max(cultists, key=lambda player: player.day_cult)
        result = self._visit_player(room, visitor, target, events)

        def tell_cultists(kind: str, text: str) -> None:
            events.extend(
                DomainEvent(kind, text, public=False, target_user_id=cultist.user_id)
                for cultist in cultists
            )

        def convert(chance: int = 100) -> None:
            if self.rng.randrange(100) < chance:
                # Werewolf.cs:2495-2497 —— 转化娼妇时全体投票教徒解锁 DontStayHome。
                if target.role == Role.HARLOT:
                    for cultist in cultists:
                        events.extend(
                            self._add_achievement(cultist, Achievement.DONT_STAY_HOME)
                        )
                events.extend(self._transform_checks(target, method="ConvertToCult"))
                target.role = Role.CULTIST
                target.metadata.pop("wolf_aligned", None)
                target.metadata["converted"] = True
                target.metadata["role_actions"] = [QuestionType.CONVERT.value]
                target.day_cult = room.day
                target.changed_roles_count += 1
                tell_cultists("cult_conversion_succeeded", f"{target.public_name} 已加入教会。")
                events.append(DomainEvent(
                    "cult_converted", "你已被教会转化。", public=False,
                    target_user_id=target.user_id, metadata={"role": Role.CULTIST.value},
                ))
            else:
                tell_cultists("cult_conversion_failed", f"未能转化 {target.public_name}。")
                events.append(DomainEvent(
                    "cult_attempt", "有人尝试转化你，但没有成功。", public=False,
                    target_user_id=target.user_id,
                ))

        if result == "already_dead":
            if not target.burning:
                tell_cultists("cult_target_dead", f"{target.public_name} 已经出局。")
            return events
        if result == "visitor_died":
            if target.role == Role.SERIAL_KILLER:
                tell_cultists("cult_visit_serial_killer", f"{visitor.public_name} 在访问连环杀手时出局。")
            elif target.role == Role.GRAVE_DIGGER:
                tell_cultists("cult_visit_grave_digger", f"{visitor.public_name} 在墓地守卫处出局。")
            return events
        if result != "success":
            tell_cultists("cult_visit_empty", f"{target.public_name} 不在家。")
            return events

        if target.role == Role.HUNTER:
            if self.rng.randrange(100) < room.rules.hunter_conversion_chance:
                convert()
            elif self.rng.randrange(100) < room.rules.hunter_kill_cult_chance:
                # Werewolf.cs:3625-3635 —— 猎人反击教徒同样累计反杀次数与成就。
                target.has_shot_hunter_attacker += 1
                if target.has_shot_hunter_attacker == 2:
                    events.extend(self._add_achievement(target, Achievement.HELPFUL_PARANOIA))
                if target.has_shot_hunter_attacker_this_night:
                    events.extend(self._add_achievement(target, Achievement.S_TIER_HUNTER))
                target.has_shot_hunter_attacker_this_night = True
                events.extend(self._kill(room, visitor, KillMethod.HUNTER_CULT, source=target))
                tell_cultists("cult_hunter_counter", f"{visitor.public_name} 被猎人反击。")
            else:
                tell_cultists("cult_conversion_failed", f"未能转化 {target.public_name}。")
                events.append(DomainEvent(
                    "cult_attempt", "有人尝试转化你，但没有成功。", public=False,
                    target_user_id=target.user_id,
                ))
        elif target.role == Role.CULTIST_HUNTER:
            events.extend(self._kill(room, visitor, KillMethod.HUNT, source=target))
            # Werewolf.cs:3657 —— 被派去转化教徒猎人的新教徒解锁 CultFodder。
            events.extend(self._add_achievement(visitor, Achievement.CULT_FODDER))
            tell_cultists("cult_hunter_counter", f"{visitor.public_name} 被教徒猎人击杀。")
            events.append(DomainEvent(
                "cultist_hunter_visit", "你击杀了前来转化的教徒。", public=False,
                target_user_id=target.user_id,
            ))
        elif target.role == Role.SNOW_WOLF:
            snow_left = any(
                action.actor_id == target.user_id and action.target_id
                for action in by_type.get(QuestionType.FREEZE.value, ())
            )
            if snow_left:
                tell_cultists("cult_visit_empty", f"{target.public_name} 不在家。")
            else:
                events.extend(self._kill(room, visitor, KillMethod.VISIT_WOLF, source=target))
                tell_cultists("cult_visit_wolf", f"{visitor.public_name} 在雪狼家中出局。")
        elif self._is_majority_wolf(target):
            wolf_voted = any(
                action.actor_id == target.user_id
                and action.target_id is not None
                for action in (*by_type.get(QuestionType.KILL.value, ()), *by_type.get(QuestionType.KILL_2.value, ()))
            )
            if wolf_voted:
                tell_cultists("cult_visit_empty", f"{target.public_name} 不在家。")
            else:
                events.extend(self._kill(room, visitor, KillMethod.VISIT_WOLF, source=target))
                tell_cultists("cult_visit_wolf", f"{visitor.public_name} 在狼人家中出局。")
                events.append(DomainEvent(
                    "cult_attempt", "有人尝试转化你，但没有成功。", public=False,
                    target_user_id=target.user_id,
                ))
        elif target.role == Role.ARSONIST:
            arsonist_acted = any(
                action.actor_id == target.user_id and (
                    action.target_id is not None or target.metadata.get("arsonist_ignite")
                )
                for action in by_type.get(QuestionType.DOUSE.value, ())
            )
            if target.is_frozen or not arsonist_acted:
                convert(0)
            else:
                tell_cultists("cult_visit_empty", f"{target.public_name} 不在家。")
        elif target.role in {Role.DOPPELGANGER, Role.THIEF, Role.SPUMPKIN}:
            convert(room.rules.cult_conversion_chance(target.role))
        else:
            convert(room.rules.cult_conversion_chance(target.role))
        return events

    def _visit_player(
        self, room: GameRoom, visitor: Player, target: Player | None, events: list[DomainEvent]
    ) -> str:
        """在调用点原样移植官方 VisitPlayer 的判定链。"""
        if target is None:
            return "target_null"
        target.metadata["being_visited_same_night"] = int(
            target.metadata.get("being_visited_same_night", 0)
        ) + 1
        if not target.alive and not target.burning and (
            room.rules.thief_full or visitor.role != Role.THIEF
        ):
            return "already_dead"
        if visitor.role == Role.SERIAL_KILLER and target.role != Role.GRAVE_DIGGER:
            return "already_dead" if not target.alive else "success"
        if target.burning:
            if visitor.role == Role.SERIAL_KILLER:
                return "already_dead"
            arsonist = next((p for p in room.players if p.role == Role.ARSONIST), None)
            events.extend(self._kill(room, visitor, KillMethod.VISIT_BURNING, source=arsonist))
            return "visitor_died"
        if target.role == Role.SERIAL_KILLER:
            # 官方把雪狼也算作会触发连环杀手绊倒判定的狼人访客。
            visitor_is_wolf = (
                visitor.role in MAJORITY_WOLF_ROLES
                or visitor.role == Role.SNOW_WOLF
            )
            serial_stayed_home = target.choice in {None, "", "-1", "0"} or target.frozen or target.is_frozen
            if (not visitor_is_wolf or serial_stayed_home
                    or self.rng.randrange(100) < room.rules.serial_killer_stumble_chance):
                events.extend(self._kill(room, visitor, KillMethod.VISIT_KILLER, source=target))
                return "visitor_died"
            return "success"
        # 官方经典盗贼访问狼人时仍可完成偷取；只有 ThiefFull 才会触发狼人访问规则。
        if visitor.role == Role.THIEF and not room.rules.thief_full:
            return "success"
        if visitor.role == Role.SNOW_WOLF:
            return "success"
        if self._is_wolf(target) and visitor.role in {
            Role.HARLOT, Role.GUARDIAN_ANGEL, Role.THIEF,
        }:
            if visitor.role == Role.HARLOT:
                events.extend(self._kill(room, visitor, KillMethod.VISIT_WOLF, source=target))
                return "visitor_died"
            if visitor.role == Role.GUARDIAN_ANGEL:
                if target.was_saved_last_night:
                    # 官方保留这个状态供成就和本轮结算读取。
                    visitor.metadata["in_middle_of_trouble"] = True
                    return "success"
                if self.rng.randrange(100) < room.rules.guardian_wolf_death_chance:
                    events.extend(self._kill(room, visitor, KillMethod.GUARD_WOLF, source=target))
                    return "visitor_died"
                return "success"
            return "fail"
        if target.role == Role.GRAVE_DIGGER:
            graves = int(target.metadata.get("dug_graves_last_night", 0))
            if graves < 1:
                return "success"
            if visitor.role == Role.SERIAL_KILLER:
                visitor.metadata["stumbled_grave_day"] = room.day
                events.append(DomainEvent(
                    "serial_killer_stumbled_grave", f"你在 {target.public_name} 的墓地附近迷路了。",
                    public=False, target_user_id=visitor.user_id,
                ))
                return "success"
            if visitor.role == Role.GUARDIAN_ANGEL and target.was_saved_last_night:
                return "success"
            chance = room.rules.grave_digger_fall_chance
            if chance < 0:
                chance = 20 + (30 - (30 * (0.5 ** (graves - 1))))
                if visitor.team == Team.VILLAGE:
                    chance /= 2
            if self.rng.randrange(100) < chance:
                events.extend(self._kill(
                    room, visitor, KillMethod.FALL_GRAVE, source=target, hunter_final_shot=False
                ))
                return "visitor_died"
            if visitor.role != Role.ARSONIST:
                return "fail"
        if visitor.role == Role.ARSONIST:
            return "success"
        harlot_away = target.role == Role.HARLOT and self._is_away(room, target)
        guardian_away = (
            target.role == Role.GUARDIAN_ANGEL
            and visitor.role not in WOLF_ROLES
            and self._is_away(room, target)
        )
        if harlot_away or guardian_away:
            return "fail"
        return "success"

    def _resolve_arsonist(
        self,
        room: GameRoom,
        by_type: dict[str, list[NightAction]],
        ga: Player | None = None,
        ga_target: str | None = None,
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for action in by_type.get(QuestionType.DOUSE.value, ()):
            actor = self._find(room, action.actor_id)
            if actor.role != Role.ARSONIST or not actor.alive:
                continue
            if action.target_id:
                target = next((p for p in room.players if p.user_id == action.target_id), None)
                if self._visit_player(room, actor, target, events) == "success" and target:
                    target.doused = True
                    events.append(DomainEvent(
                        "arsonist_doused", f"你已标记 {target.public_name}。",
                        public=False, target_user_id=actor.user_id,
                    ))
                continue
            if not actor.metadata.get("arsonist_ignite"):
                continue
            targets = [
                target for target in self._players(room)
                if target.doused and target.user_id != actor.user_id
            ]
            burning_ids = {target.user_id for target in targets}
            for target in targets:
                if self._is_guarded(ga, ga_target, target.user_id):
                    target.was_saved_last_night = True
                    events.append(DomainEvent(
                        "guard_saved_fire", "守护阻断了纵火者的引燃。",
                        public=False, target_user_id=target.user_id,
                    ))
                    continue
                target.doused = False
                target.burning = True
                events.extend(self._kill(
                    room,
                    target,
                    KillMethod.BURN,
                    source=actor,
                    hunter_final_shot=False,
                    simultaneously_dying_ids=burning_ids,
                ))
                # Werewolf.cs:3203 —— SendGif(Burn) 私聊被烧死者本人。
                events.append(self._victim_notice(target, "Burn"))
            # Werewolf.cs:3207-3215 —— 同夜点燃 5/10 间房子分别解锁 PlayingWithTheFire / Firework。
            burning_total = sum(1 for player in room.players if player.burning)
            if burning_total >= 5:
                events.extend(
                    self._add_achievement(actor, Achievement.PLAYING_WITH_THE_FIRE)
                )
                if burning_total >= 10:
                    events.extend(self._add_achievement(actor, Achievement.FIREWORK))
            if targets:
                events.append(DomainEvent("arsonist_burn", "昨夜有人被火焰吞噬。"))
        return events

    def _resolve_cult_hunter(
        self, room: GameRoom, by_type: dict[str, list[NightAction]]
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for action in by_type.get(QuestionType.HUNT.value, ()):
            hunter = self._find(room, action.actor_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if hunter.role != Role.CULTIST_HUNTER or not hunter.alive or hunter.is_frozen or target is None:
                continue
            result = self._visit_player(room, hunter, target, events)
            if result == "success" and target.role == Role.CULTIST:
                events.extend(self._kill(room, target, KillMethod.HUNT, source=hunter))
                # Werewolf.cs:3572 —— 猎杀教徒成功累计 CHHuntedCultCount。
                hunter.ch_hunted_cult_count += 1
                events.append(DomainEvent(
                    "cultist_hunter_success", f"你击杀了教徒 {target.public_name}。",
                    public=False, target_user_id=hunter.user_id,
                ))
                events.append(DomainEvent(
                    "cultist_hunter_killed", "你被教徒猎人击杀。",
                    public=False, target_user_id=target.user_id,
                ))
            elif result == "already_dead":
                events.append(DomainEvent(
                    "cultist_hunter_dead_target", f"{target.public_name} 已经出局。",
                    public=False, target_user_id=hunter.user_id,
                ))
            elif result == "success" or result == "fail":
                events.append(DomainEvent(
                    "cultist_hunter_missed", f"未能在 {target.public_name} 处找到教徒。",
                    public=False, target_user_id=hunter.user_id,
                ))
        return events

    def _resolve_visits(
        self, room: GameRoom, by_type: dict[str, list[NightAction]]
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        # Werewolf.cs:3846-3858 —— 娼妇未选目标视为留在家中，影响 Promiscuous 判定。
        acting_harlots = {
            action.actor_id for action in by_type.get(QuestionType.VISIT.value, ())
            if action.target_id
        }
        for harlot in self._players(room):
            if (
                harlot.role == Role.HARLOT
                and not harlot.is_frozen
                and harlot.user_id not in acting_harlots
            ):
                harlot.has_stayed_home = True
        for action in by_type.get(QuestionType.VISIT.value, ()):
            visitor = self._find(room, action.actor_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if visitor.role != Role.HARLOT or not visitor.alive or visitor.is_frozen or target is None:
                continue
            visitor.metadata["visited"] = target.user_id
            # Werewolf.cs:3851-3857 —— 拜访恋人解锁 Affectionate；重复拜访记录 HasRepeatedVisit。
            if visitor.lover_id == target.user_id:
                events.extend(self._add_achievement(visitor, Achievement.AFFECTIONATE))
            if target.user_id in visitor.players_visited:
                visitor.has_repeated_visit = True
            visitor.players_visited.append(target.user_id)
            result = self._visit_player(room, visitor, target, events)
            if result == "success":
                discovered_cult = (
                    target.role == Role.CULTIST
                    and self.rng.randrange(100) < room.rules.harlot_discover_cult_chance
                )
                text = (
                    f"你发现 {target.public_name} 是教徒。" if discovered_cult
                    else f"你拜访了 {target.public_name}，没有发现狼人。"
                )
                events.append(DomainEvent(
                    "harlot_visit", text, public=False, target_user_id=visitor.user_id,
                    metadata={"discovered_cult": discovered_cult, "target_id": target.user_id},
                ))
                if target.alive:
                    events.append(DomainEvent(
                        "harlot_visited_you", "昨夜有人拜访了你。",
                        public=False, target_user_id=target.user_id,
                    ))
                    # Werewolf.cs:3871-3873 —— 同夜熬过化学家失败的拜访解锁 LuckyNight。
                    chemist = next(
                        (
                            member for member in room.players
                            if member.role == Role.CHEMIST
                            and member.metadata.get("chemist_failed")
                            and member.metadata.get("chemist_choice") == target.user_id
                        ),
                        None,
                    )
                    if chemist is not None:
                        events.extend(self._add_achievement(target, Achievement.LUCKY_NIGHT))
            elif result == "already_dead":
                killed_by_role = target.metadata.get("killed_by_role")
                can_trigger_visit_victim = (
                    target.died_last_night
                    and killed_by_role in {role.value for role in WOLF_ROLES} | {Role.SERIAL_KILLER.value}
                    and not target.metadata.get("died_by_visiting_killer", False)
                    and not target.metadata.get("died_by_visiting_victim", False)
                    and not target.burning
                )
                if can_trigger_visit_victim:
                    events.extend(self._kill(room, visitor, KillMethod.VISIT_VICTIM, source=target))
                    visitor.role_model = target.user_id
                
                else:
                    events.append(DomainEvent(
                        "harlot_not_home", f"{target.public_name} 不在家。",
                        public=False, target_user_id=visitor.user_id,
                    ))
            elif result == "visitor_died":
                if not target.burning:
                    if target.role in MAJORITY_WOLF_ROLES:
                        events.append(DomainEvent(
                            "harlot_visit_wolf",
                            f"你在 {target.public_name} 家中遇到狼人，已经出局。",
                            public=False, target_user_id=visitor.user_id,
                        ))
                    elif target.role == Role.SERIAL_KILLER:
                        events.append(DomainEvent(
                            "harlot_visit_killer",
                            f"你在 {target.public_name} 家中遇到连环杀手，已经出局。",
                            public=False, target_user_id=visitor.user_id,
                        ))
                    elif target.role == Role.GRAVE_DIGGER:
                        events.append(DomainEvent(
                            "harlot_fell_grave",
                            f"你在 {target.public_name} 的墓地摔倒，已经出局。",
                            public=False, target_user_id=visitor.user_id,
                        ))
            elif result == "fail":
                events.append(DomainEvent(
                    "harlot_not_home", f"{target.public_name} 不在家。",
                    public=False, target_user_id=visitor.user_id,
                ))
        return events

    def _resolve_chemistry(
        self,
        room: GameRoom,
        by_type: dict[str, list[NightAction]],
        ga: Player | None = None,
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for action in by_type.get(QuestionType.CHEMISTRY.value, ()):
            actor = self._find(room, action.actor_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if actor.role == Role.CHEMIST and target and actor.has_used_ability and not actor.is_frozen:
                result = self._visit_player(room, actor, target, events)
                if result == "already_dead":
                    events.append(DomainEvent(
                        "chemistry_target_dead", f"{target.public_name} 已经出局。",
                        public=False, target_user_id=actor.user_id,
                    ))
                    continue
                if result == "fail":
                    events.append(DomainEvent(
                        "chemistry_target_away", f"{target.public_name} 不在家。",
                        public=False, target_user_id=actor.user_id,
                    ))
                    continue
                if result != "success":
                    continue
                actor.has_used_ability = False
                if self.rng.randrange(100) < room.rules.chemist_success_chance:
                    events.extend(self._kill(room, target, KillMethod.CHEMISTRY, source=actor))
                    events.append(DomainEvent("chemistry_success", "你的药剂生效了。", public=False, target_user_id=actor.user_id))
                    # Werewolf.cs:3806-3809 —— 连续成功 3 次解锁 GoodChoiceForYou；
                    # 若目标当晚曾被守卫救下，守卫解锁 AtLeastYouTried。
                    actor.chemist_visit_survive_count += 1
                    if actor.chemist_visit_survive_count == 3:
                        events.extend(
                            self._add_achievement(actor, Achievement.GOOD_CHOICE_FOR_YOU)
                        )
                    if target.was_saved_last_night and ga is not None:
                        events.extend(self._add_achievement(ga, Achievement.AT_LEAST_YOU_TRIED))
                    # Werewolf.cs:4279-4284 —— 非隐藏身份模式下毒杀长老，
                    # 化学家（若仍存活）被降级为村民并公开提示。
                    if (
                        target.role == Role.WISE_ELDER
                        and room.rules.show_roles_on_death
                        and actor.alive
                    ):
                        actor.role = Role.VILLAGER
                        actor.changed_roles_count += 1
                        events.append(DomainEvent(
                            "chemist_killed_elder",
                            "化学家毒杀了长老，失去了自己的能力，变成了普通村民。",
                        ))
                else:
                    events.extend(self._kill(room, actor, KillMethod.CHEMISTRY, source=actor))
                    # Werewolf.cs:3815 —— ChemistFailed 供娼妇 LuckyNight 判定使用。
                    actor.metadata["chemist_failed"] = True
                    actor.metadata["chemist_choice"] = target.user_id
                    events.append(DomainEvent("chemistry_failed", "药剂发生意外，你已出局。", public=False, target_user_id=actor.user_id))
        return events

    def _pick_random_living(self, room: GameRoom, exclude: Player) -> Player | None:
        """对应官方 ChooseRandomPlayerId(exclude, all: false)：只取存活玩家，洗牌两次。"""
        choices = [player for player in self._players(room) if player.user_id != exclude.user_id]
        if not choices:
            return None
        self.rng.shuffle(choices)
        self.rng.shuffle(choices)
        return choices[0]

    def _resolve_thief(
        self, room: GameRoom, by_type: dict[str, list[NightAction]]
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        seen: set[str] = set()
        actions = list(by_type.get(QuestionType.THIEF.value, ()))
        if not room.rules.thief_full and room.day == 1:
            acting = {action.actor_id for action in actions}
            for thief in self._players(room):
                if thief.role == Role.THIEF and thief.user_id not in acting:
                    actions.append(NightAction(
                        actor_id=thief.user_id,
                        action=QuestionType.THIEF.value,
                        target_id=None,
                        day=room.day,
                    ))
        for action in actions:
            actor = self._find(room, action.actor_id)
            if actor.user_id in seen or actor.role != Role.THIEF or not actor.alive:
                continue
            seen.add(actor.user_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if not room.rules.thief_full and room.day != 1:
                continue
            if room.rules.thief_full and actor.is_frozen:
                continue
            if target is None and not room.rules.thief_full:
                target = self._pick_random_living(room, actor)
            if target is None:
                events.append(DomainEvent(
                    "thief_no_target", "没有可盗取的目标。", public=False, target_user_id=actor.user_id,
                ))
                continue
            result = self._visit_player(room, actor, target, events)
            if result == "success":
                blocked_role = target.role in WOLF_ROLES or target.role in {Role.CULTIST, Role.SNOW_WOLF}
                if room.rules.thief_full and (
                    blocked_role or self.rng.randrange(100) >= room.rules.thief_steal_chance
                ):
                    events.append(DomainEvent(
                        "thief_failed", f"未能盗取 {target.public_name} 的身份。",
                        public=False, target_user_id=actor.user_id,
                    ))
                    continue
                events.extend(self._steal_role(room, actor, target))
            elif result == "already_dead":
                events.append(DomainEvent(
                    "thief_target_dead", f"{target.public_name} 已经出局。",
                    public=False, target_user_id=actor.user_id,
                ))
            elif result == "fail":
                events.append(DomainEvent(
                    "thief_failed", f"未能盗取 {target.public_name} 的身份。",
                    public=False, target_user_id=actor.user_id,
                ))
        return events

    def _steal_role(self, room: GameRoom, thief: Player, target: Player) -> list[DomainEvent]:
        if not target.alive:
            target = self._pick_random_living(room, thief)
            if target is None:
                return [DomainEvent(
                    "thief_no_target", "没有可盗取的目标。", public=False, target_user_id=thief.user_id,
                )]
            result = self._visit_player(room, thief, target, [])
            if result != "success":
                return [DomainEvent(
                    "thief_failed",
                    f"未能盗取 {target.public_name} 的身份。",
                    public=False,
                    target_user_id=thief.user_id,
                )]
        stolen_role = target.role
        if stolen_role is None:
            return []
        old_masons = [
            player for player in self._players(room)
            if player.role == Role.MASON and player.user_id != target.user_id
        ] if stolen_role == Role.MASON else []
        old_wolves = [
            player for player in self._players(room)
            if self._is_majority_wolf(player) and player.user_id != thief.user_id
        ] if stolen_role in WOLF_ROLES else []
        if stolen_role == Role.SNOW_WOLF:
            old_wolves = [
                player for player in self._players(room)
                if player.role in WOLF_ROLES and player.user_id != thief.user_id
            ]
        stolen_model = target.role_model
        stolen_bullets = target.bullet_count
        stolen_used_ability = target.has_used_ability
        # Werewolf.cs:2483/2486 —— 先 Transform 被偷者，再 Transform 盗贼。
        events: list[DomainEvent] = list(self._transform_checks(target, method="ThiefStolen"))
        target.role = Role.THIEF if room.rules.thief_full else Role.VILLAGER
        target.role_model = None
        target.bullet_count = 0
        target.has_used_ability = False
        target.changed_roles_count += 1
        target.metadata.pop("wolf_aligned", None)
        target.metadata["role_actions"] = [
            action.value for action in ROLE_ACTIONS.get(target.role, ())
        ]
        events.extend(self._transform_checks(
            thief, method="ThiefSteal", new_role_model=stolen_model,
        ))
        thief.role = stolen_role
        thief.role_model = stolen_model
        thief.bullet_count = stolen_bullets
        thief.has_used_ability = stolen_used_ability
        thief.changed_roles_count += 1
        thief.metadata["stole_from"] = target.user_id
        thief.metadata["role_actions"] = [
            action.value for action in ROLE_ACTIONS.get(stolen_role, ())
        ]
        if stolen_role in WOLF_ROLES:
            thief.metadata["wolf_aligned"] = True
        else:
            thief.metadata.pop("wolf_aligned", None)
        events.extend([
            DomainEvent(
                "thief_stole_role", f"你已完成盗取，当前身份为【{role_display_name(stolen_role)}】。",
                public=False, target_user_id=thief.user_id, metadata={"role": stolen_role.value},
            ),
            DomainEvent(
                "thief_role_lost", f"你的身份被盗取，当前身份为【{role_display_name(target.role)}】。",
                public=False, target_user_id=target.user_id, metadata={"role": target.role.value},
            ),
        ])
        if old_masons:
            events.extend(
                DomainEvent(
                    "team_member_converted",
                    f"石匠同伴 {target.public_name} 已转化为【{role_display_name(target.role)}】。",
                    public=False,
                    target_user_id=member.user_id,
                    metadata={"former_member_id": target.user_id, "role": target.role.value},
                )
                for member in old_masons
            )
        if stolen_role == Role.SEER:
            for member in self._players(room):
                if member.role == Role.BEHOLDER:
                    events.append(DomainEvent(
                        "beholder_seer_stolen",
                        f"预言家身份已被盗贼 {thief.public_name} 偷走。",
                        public=False,
                        target_user_id=member.user_id,
                        metadata={"new_seer_id": thief.user_id, "former_seer_id": target.user_id},
                    ))
        elif stolen_role == Role.SNOW_WOLF:
            for member in self._players(room):
                if member.role in WOLF_ROLES:
                    events.append(DomainEvent(
                        "team_member_added",
                        f"{thief.public_name} 已加入雪狼阵营。",
                        public=False,
                        target_user_id=member.user_id,
                        metadata={"new_member_id": thief.user_id, "role": stolen_role.value},
                    ))
        elif stolen_role in {Role.DOPPELGANGER, Role.WILD_CHILD}:
            model = next((item for item in room.players if item.user_id == stolen_model), None)
            if model:
                events.append(DomainEvent(
                    "thief_role_model_inherited",
                    f"你继承了 {model.public_name} 的身份目标。",
                    public=False,
                    target_user_id=thief.user_id,
                    metadata={"role_model_id": model.user_id, "role": stolen_role.value},
                ))
        if old_wolves:
            events.extend(
                DomainEvent(
                    "team_member_added",
                    f"{thief.public_name} 已加入狼人阵营，当前身份为【{role_display_name(stolen_role)}】。",
                    public=False,
                    target_user_id=member.user_id,
                    metadata={"new_member_id": thief.user_id, "role": stolen_role.value},
                )
                for member in old_wolves
            )
        return events

    def _resolve_guard_visits(
        self,
        room: GameRoom,
        by_type: dict[str, list[NightAction]],
        ga: Player | None = None,
        ga_target: str | None = None,
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for action in by_type.get(QuestionType.GUARD.value, ()):
            guardian = self._find(room, action.actor_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if guardian.role != Role.GUARDIAN_ANGEL or (guardian.is_frozen and guardian is not ga) or target is None:
                continue
            result = self._visit_player(room, guardian, target, events)
            if result in {"success", "already_dead"}:
                # Werewolf.cs:4073 —— 守护未遭袭击的狼系累计 GAGuardWolfCount。
                if target.role in WOLF_ROLES and not target.was_saved_last_night:
                    guardian.ga_guard_wolf_count += 1
                if target.was_saved_last_night:
                    events.append(DomainEvent(
                        "guardian_saved", f"你成功守护了 {target.public_name}。",
                        public=False, target_user_id=guardian.user_id,
                    ))
                elif target.doused and target.alive:
                    target.doused = False
                    # Werewolf.cs:4094-4098 —— 累计清理燃料 3 次解锁 Firefighter。
                    guardian.has_cleaned_doused += 1
                    if guardian.has_cleaned_doused == 3:
                        events.extend(
                            self._add_achievement(guardian, Achievement.FIREFIGHTER)
                        )
                    events.append(DomainEvent(
                        "guardian_cleaned_doused", f"你清除了 {target.public_name} 身上的燃料。",
                        public=False, target_user_id=guardian.user_id,
                    ))
                elif target.alive:
                    events.append(DomainEvent(
                        "guardian_no_attack", f"{target.public_name} 昨夜没有受到袭击。",
                        public=False, target_user_id=guardian.user_id,
                    ))
            elif result == "fail":
                events.append(DomainEvent(
                    "guardian_target_away", f"{target.public_name} 不在家。",
                    public=False, target_user_id=guardian.user_id,
                ))
        return events

    def _wake_apprentice(self, room: GameRoom) -> list[DomainEvent]:
        return self._promote_apprentice(room)

    def _promote_apprentice(self, room: GameRoom, *, checkbitten: bool = False) -> list[DomainEvent]:
        if any(player.alive and player.role == Role.SEER for player in room.players):
            return []
        events: list[DomainEvent] = []
        for player in self._players(room):
            if player.role != Role.APPRENTICE_SEER:
                continue
            if checkbitten and player.bitten:
                continue
            events.extend(self._transform_checks(player, method="ApprenticeSeer"))
            player.role = Role.SEER
            player.metadata["awakened"] = True
            player.changed_roles_count += 1
            events.append(DomainEvent(
                "apprentice_now_seer",
                "预言家已出局，你成为预言家。",
                public=False,
                target_user_id=player.user_id,
                metadata={"role": Role.SEER.value},
            ))
            for beholder in self._players(room):
                if beholder.role == Role.BEHOLDER:
                    events.append(DomainEvent(
                        "beholder_new_seer",
                        f"新的预言家是 {player.public_name}。",
                        public=False,
                        target_user_id=beholder.user_id,
                    ))
        return events

    def _kill(
        self,
        room: GameRoom,
        victim: Player,
        method: KillMethod,
        *,
        source: Player | None = None,
        killers: list[Player] | None = None,
        hunter_final_shot: bool = True,
        lover_chain: bool = True,
        simultaneously_dying_ids: set[str] | None = None,
        is_night: bool | None = None,
    ) -> list[DomainEvent]:
        if not victim.alive:
            return []
        if is_night is None:
            is_night = room.phase == GamePhase.NIGHT
        victim.alive = False
        victim.killed_by = source.user_id if source else None
        victim.kill_method = method.value
        if source and source.role:
            victim.metadata["killed_by_role"] = source.role.value
        victim.metadata["died_by_visiting_killer"] = method in {
            KillMethod.VISIT_WOLF,
            KillMethod.VISIT_KILLER,
            KillMethod.GUARD_WOLF,
            KillMethod.FALL_GRAVE,
            KillMethod.HUNTER_CULT,
            KillMethod.HUNT,
        }
        victim.metadata["died_by_visiting_victim"] = method in {
            KillMethod.VISIT_BURNING,
            KillMethod.VISIT_VICTIM,
        }
        victim.metadata["died_by_flee_or_idle"] = method in {
            KillMethod.FLEE,
            KillMethod.IDLE,
        }
        victim.died_last_night = is_night and method != KillMethod.LOVER_DIED
        victim.metadata["death_day"] = room.day
        death_sequence = int(room.statistics.get("death_sequence", 0)) + 1
        room.statistics["death_sequence"] = death_sequence
        victim.metadata["death_sequence"] = death_sequence
        events = [
            DomainEvent(
                "player_died",
                self._death_notice(victim, method, source)
                + (f" 身份是【{role_display_name(victim.role) if victim.role else '未分配'}】。"
                   if room.rules.show_roles_on_death else ""),
                metadata={"user_id": victim.user_id, "method": method.value},
            )
        ]
        # Werewolf.cs:5605-5607 —— DBKill 后按凶手累计 KilledLastNight（仅夜间）。
        kill_sources = list(killers) if killers is not None else ([source] if source else [])
        if kill_sources:
            for killer in kill_sources:
                if is_night:
                    killer.killed_last_night += 1
                # Werewolf.cs:5696-5702 —— DBKill 尾部：夜里杀死自己的恋人。
                if (
                    victim.lover_id == killer.user_id
                    and room.phase == GamePhase.NIGHT
                    and method != KillMethod.LOVER_DIED
                ):
                    if room.day == 1:
                        events.extend(self._add_achievement(killer, Achievement.OH_SHI))
                    elif killer.role in WOLF_ROLES:
                        events.extend(
                            self._add_achievement(killer, Achievement.SHOULDVE_MENTIONED)
                        )
        # Werewolf.cs:5602-5607 —— Idle/Flee 死亡跳过一切后续后果。
        if method in {KillMethod.FLEE, KillMethod.IDLE}:
            return events
        if lover_chain and victim.lover_id:
            lover = next((p for p in room.players if p.user_id == victim.lover_id), None)
            simultaneously_dying = {
                player.user_id for player in room.players
                if player.burning and not player.alive
            }
            if simultaneously_dying_ids:
                simultaneously_dying.update(simultaneously_dying_ids)
            if lover and lover.alive and lover.user_id not in simultaneously_dying:
                # Werewolf.cs:5613-5615 —— 单人枪杀情侣双方均非好人阵营时解锁 DoubleShot。
                if (
                    method in {KillMethod.HUNTER, KillMethod.SHOOT}
                    and len(kill_sources) == 1
                    and not any(
                        member.team in {Team.VILLAGE, Team.NEUTRAL, Team.THIEF}
                        for member in (victim, lover)
                    )
                ):
                    events.extend(
                        self._add_achievement(kill_sources[0], Achievement.DOUBLE_SHOT)
                    )
                events.extend(self._kill(room, lover, KillMethod.LOVER_DIED, source=victim))
        if hunter_final_shot and (victim.role == Role.HUNTER or victim.metadata.get("can_hunt_on_death")):
            # Werewolf.cs:5620-5626 —— 猎人死亡触发 HunterFinalShot，菜单文案按
            # 处决（HunterLynchedChoice）与被杀（HunterShotChoice）区分。
            lynched = method == KillMethod.LYNCH
            victim.metadata["pending_hunt"] = {"method": method.value, "lynched": lynched}
            events.append(DomainEvent(
                "hunter_prompt",
                get_locale_string("HunterLynchedChoice" if lynched else "HunterShotChoice")
                + f"\n请在 {_HUNTER_WINDOW_SECONDS} 秒内私聊发送“猎杀 座位号”，放弃发送“跳过”。",
                public=False,
                target_user_id=victim.user_id,
                metadata={"action": QuestionType.HUNTER_KILL.value},
            ))
        if victim.role == Role.WOLF_CUB:
            room.statistics["wolf_cub_killed"] = True
        return events

    @staticmethod
    def _death_notice(
        victim: Player, method: KillMethod, source: Player | None = None
    ) -> str:
        """沿用官方的公开死亡文案体系，同时把 QQ 侧的文本控制得更短。"""
        name = GameRoomEngine._name(victim)
        if method == KillMethod.SHOOT and source is not None:
            # Werewolf.cs:2886-2894 / 2913-2920 —— DefaultShot / Detonation 公开点名开枪者。
            shooter = GameRoomEngine._name(source)
            if source.user_id == victim.user_id:
                return f"{shooter} 引爆了自己。"
            return f"{shooter} 开枪打死了 {name}。"
        if method == KillMethod.HUNTER and "final_shot_lynched" in (source.metadata if source else {}):
            # Werewolf.cs:5464-5467 —— HunterKilledFinalLynched / HunterKilledFinalShot
            # 公开点名开枪的猎人与中枪者；身份揭示由 `_kill` 统一追加（对应官方 {2}）。
            assert source is not None
            return get_locale_string(
                "HunterKilledFinalLynched" if source.metadata["final_shot_lynched"]
                else "HunterKilledFinalShot",
                None,
                GameRoomEngine._name(source),
                name,
                "",
            ).strip()
        return {
            KillMethod.LYNCH: f"{name} 被投票处决。",
            KillMethod.EAT: f"昨夜 {name} 被狼人袭击。",
            KillMethod.SERIAL_KILLED: f"昨夜 {name} 被连环杀手杀死。",
            KillMethod.BURN: f"昨夜 {name} 被火焰吞噬。",
            KillMethod.CHEMISTRY: f"昨夜 {name} 死于化学药剂。",
            KillMethod.FALL_GRAVE: f"昨夜 {name} 在墓地遇难。",
            # Werewolf.cs:4399-4404 —— 掘墓人被发现处死时，按凶手身份区分公开播报。
            KillMethod.SPOTTED: (
                f"{name} 今天没有出现……是睡过头了吗？大家去找他，"
                f"却发现他倒在墓地里，铁锹还握在手上，身上是好几处刀伤……"
                if source is not None and source.role == Role.SERIAL_KILLER
                else f"{name} 没有来参加镇民集会。又睡过头了吗？大家去找他，"
                f"发现他倒在墓地里，铁锹还握在手上，看样子是夜里被狼群偷袭了……"
            ),
            KillMethod.VISIT_WOLF: f"昨夜 {name} 访问狼人时遇害。",
            KillMethod.VISIT_KILLER: f"昨夜 {name} 访问连环杀手时遇害。",
            KillMethod.GUARD_WOLF: f"昨夜 {name} 守护狼人时遇害。",
            KillMethod.HUNTER_CULT: f"昨夜 {name} 被教徒猎人击杀。",
            KillMethod.HUNT: f"昨夜 {name} 被猎杀。",
            KillMethod.VISIT_VICTIM: f"昨夜 {name} 因访问遇害者而死亡。",
            KillMethod.VISIT_BURNING: f"昨夜 {name} 访问着火的玩家时遇害。",
            KillMethod.HUNTER: f"{name} 被猎人击杀。",
            KillMethod.SHOOT: f"{name} 被枪击出局。",
            KillMethod.LOVER_DIED: f"{name} 因恋人死亡而殉情。",
            KillMethod.SUICIDE: f"{name} 自杀出局。",
            KillMethod.FLEE: f"{name} 逃离游戏。",
        }.get(method, f"{name} 出局。")

    @staticmethod
    def _victim_notice(victim: Player, key: str) -> DomainEvent:
        """Werewolf.cs:3203/3312/3365/3434/3481/3533/3546 —— SendGif 私聊遇害者本人。

        SendGif 的 GIF 素材是 Telegram file_id，QQ 端不适用；但随附的 GetLocaleString
        文案属于游戏流程消息（死者本人的死亡告知），必须原样保留。
        """
        text = {
            "WolvesEatYou": "哦不！狼人把你吃掉了。呜姆呜姆呜姆——",
            "WolvesSpottedYou": "你只是在干自己的活，突然一头狼从背后扑上来，把你咬死了！",
            "SerialKillerKilledYou": "连环杀手伏击了你，把你捆了起来，然后一刀刺穿了你的心脏！",
            "SerialKillerSpottedYou": "你只是在干自己的活，突然一个疯狂的杀手发现了你，一刀捅了下来。真可惜……",
            "Burn": "你真该备一个灭火器的！现在你的房子被烧毁，你也死在了里面……",
        }[key]
        return DomainEvent(
            "victim_death_notice",
            text,
            public=False,
            target_user_id=victim.user_id,
            metadata={"locale_key": key},
        )

    def _resolve_grave_digger(
        self, room: GameRoom, selected_player: Player | None = None
    ) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        players = [selected_player] if selected_player is not None else self._players(room)
        for player in players:
            if player.role != Role.GRAVE_DIGGER or player.is_frozen:
                continue
            previous_sequence = int(player.metadata.get("grave_checked_through_sequence", 0))
            graves = [
                target for target in room.players
                if not target.alive
                and int(target.metadata.get("death_sequence", 0)) > previous_sequence
                and not target.metadata.get("died_by_flee_or_idle", False)
            ]
            player.metadata["grave_checked_through_sequence"] = int(
                room.statistics.get("death_sequence", 0)
            )
            player.metadata["dug_graves_last_night"] = len(graves)
            text = "昨夜没有新的墓穴。" if not graves else "昨夜死亡玩家：" + "、".join(
                self._name(target) for target in graves
            )
            events.append(DomainEvent("grave_digger", text, public=False, target_user_id=player.user_id))
        return events

    def _seen_role(self, room: GameRoom, target: Player) -> Role:
        if target.role == Role.TRAITOR:
            return Role.WOLF if self.rng.randrange(100) < room.rules.seer_traitor_wolf_chance else Role.VILLAGER
        if target.role in {Role.ALPHA_WOLF, Role.WOLF_CUB}:
            return Role.WOLF
        if target.role == Role.WOLF_MAN:
            return Role.WOLF
        if target.role == Role.LYCAN:
            return Role.VILLAGER
        return target.role or Role.VILLAGER

    @staticmethod
    def _sorcerer_seen_role(target: Player) -> Role:
        if target.role in {Role.ALPHA_WOLF, Role.WOLF, Role.WOLF_CUB}:
            return Role.WOLF
        if target.role in {Role.SEER, Role.SNOW_WOLF}:
            return target.role
        return Role.VILLAGER

    def _apply_pending_bites(self, room: GameRoom) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        for player in room.players:
            if not player.bitten:
                continue
            # 官方在新一夜开始时先清掉所有 Bitten；出局者不会转化。
            player.bitten = False
            if not player.alive or player.role in WOLF_ROLES or player.role == Role.SNOW_WOLF:
                continue
            old_team = [
                member for member in self._players(room)
                if member.user_id != player.user_id
                and member.role in {Role.MASON, Role.CULTIST}
            ]
            events.extend(self._transform_checks(player, method="AlphaBitten"))
            player.role = Role.WOLF
            player.metadata["wolf_aligned"] = True
            player.metadata["role_actions"] = [
                action.value for action in ROLE_ACTIONS.get(Role.WOLF, ())
            ]
            player.changed_roles_count += 1
            # Werewolf.cs:2207 —— AlphaBitten 分支为头狼累加转化数，
            # GetPlayerForRole(AlphaWolf, false) 按 TimeDied 倒序，存活者优先。
            alpha = next(
                (member for member in room.players
                 if member.role == Role.ALPHA_WOLF and member.alive),
                None,
            ) or next(
                (member for member in room.players if member.role == Role.ALPHA_WOLF),
                None,
            )
            if alpha is not None:
                alpha.alpha_convert_count += 1
            events.append(DomainEvent(
                "alpha_turned_wolf",
                "你被头狼咬伤，已经转化为狼人。",
                public=False,
                target_user_id=player.user_id,
                metadata={"role": Role.WOLF.value},
            ))
            for member in old_team:
                events.append(DomainEvent(
                    "team_member_converted",
                    f"{player.public_name} 已被头狼转化为狼人。",
                    public=False,
                    target_user_id=member.user_id,
                    metadata={"former_member_id": player.user_id, "role": Role.WOLF.value},
                ))
            for member in self._players(room):
                if member.user_id != player.user_id and member.role in MAJORITY_WOLF_ROLES | {Role.SNOW_WOLF}:
                    events.append(DomainEvent(
                        "team_member_added",
                        f"{player.public_name} 已加入狼人阵营。",
                        public=False,
                        target_user_id=member.user_id,
                        metadata={"new_member_id": player.user_id, "role": Role.WOLF.value},
                    ))
        return events

    def _check_for_game_end(self, room: GameRoom, *, checkbitten: bool = False) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        alive = self._players(room)
        pending_bite = any(player.bitten for player in alive)
        # Werewolf.cs:4484-4510 —— 场上没有普通狼，但雪狼/叛徒待补位时：
        # 若 checkbitten 且有人被咬即将变狼，整次胜负检查直接 return false。
        needs_promotion = (
            not any(player.role in WOLF_ROLES for player in alive)
            and any(player.role in {Role.SNOW_WOLF, Role.TRAITOR} for player in alive)
        )
        if needs_promotion and checkbitten and pending_bite:
            return events
        events.extend(self._promote_lone_wolf_allies(room))
        events.extend(self._resolve_endgame_duels(room))
        winners = self.check_winners(room, checkbitten=checkbitten)
        if room.statistics.pop("gunner_saves_pending", False):
            for player in self._players(room):
                if player.team == Team.VILLAGE:
                    events.extend(self._add_achievement(player, Achievement.GUNNER_SAVES))
        if winners:
            events.extend(self.finish(room, winners))
        return events

    def _promote_lone_wolf_allies(self, room: GameRoom) -> list[DomainEvent]:
        if any(player.alive and player.role in WOLF_ROLES for player in room.players):
            return []
        events: list[DomainEvent] = []
        snow = next((player for player in self._players(room) if player.role == Role.SNOW_WOLF), None)
        traitor = next((player for player in self._players(room) if player.role == Role.TRAITOR), None)
        candidate = snow or traitor
        if candidate is None:
            return events
        events.extend(self._transform_checks(candidate, method="Traitor"))
        candidate.role = Role.WOLF
        candidate.metadata["wolf_aligned"] = True
        candidate.changed_roles_count += 1
        events.append(DomainEvent(
            "role_changed",
            "狼人阵营已经没有其他狼人，你转化为狼人。",
            public=False,
            target_user_id=candidate.user_id,
            metadata={"role": Role.WOLF.value},
        ))
        return events

    def _resolve_endgame_duels(self, room: GameRoom) -> list[DomainEvent]:
        alive = self._players(room)
        events: list[DomainEvent] = []
        if len(alive) != 2:
            return events
        # Werewolf.cs:4524-4529 —— 二人残局先判恋人与全特殊角色，命中后不再进入决斗分支。
        if all(player.lover_id for player in alive):
            return events
        if all(
            player.role in {Role.SORCERER, Role.TANNER, Role.THIEF, Role.DOPPELGANGER}
            for player in alive
        ):
            return events
        hunter = next((player for player in alive if player.role == Role.HUNTER), None)
        other = next((player for player in alive if hunter and player.user_id != hunter.user_id), None)
        if hunter and other and self._is_majority_wolf(other):
            if self.rng.randrange(100) < room.rules.hunter_kill_wolf_chance_base:
                # Werewolf.cs:4544 —— 先播报 HunterKillsWolfEnd，再执行击杀。
                events.append(DomainEvent(
                    "hunter_kills_wolf_end",
                    get_locale_string(
                        "HunterKillsWolfEnd", None, self._name(hunter), self._name(other)
                    ).strip(),
                    metadata={"hunter_id": hunter.user_id, "wolf_id": other.user_id},
                ))
                events.extend(self._kill(room, other, KillMethod.HUNTER, source=hunter, hunter_final_shot=False))
            else:
                # Werewolf.cs:4550 —— WolfKillsHunterEnd 同样先播报后击杀。
                events.append(DomainEvent(
                    "wolf_kills_hunter_end",
                    get_locale_string(
                        "WolfKillsHunterEnd", None, self._name(hunter), self._name(other)
                    ).strip(),
                    metadata={"hunter_id": hunter.user_id, "wolf_id": other.user_id},
                ))
                events.extend(self._kill(room, hunter, KillMethod.EAT, source=other, hunter_final_shot=False))
            return events
        if hunter and other and other.role == Role.SERIAL_KILLER:
            # Werewolf.cs:4880-4894 —— SKHunter 结局：双方解锁 DoubleKill，官方先让猎人
            # 打死连环杀手，再由连环杀手捅死猎人，最后补播 SKHunterEnd。
            events.extend(self._add_achievement(other, Achievement.DOUBLE_KILL))
            events.extend(self._add_achievement(hunter, Achievement.DOUBLE_KILL))
            events.extend(self._kill(room, other, KillMethod.HUNTER, source=hunter, hunter_final_shot=False))
            events.extend(self._kill(room, hunter, KillMethod.SERIAL_KILLED, source=other, hunter_final_shot=False))
            events.append(DomainEvent(
                "sk_hunter_end",
                get_locale_string(
                    "SKHunterEnd", None, self._name(other), self._name(hunter)
                ).strip(),
                metadata={"serial_killer_id": other.user_id, "hunter_id": hunter.user_id},
            ))
            room.statistics["end_kind"] = "SKHunter"
            return events
        # Werewolf.cs:4556-4561 —— 教会分支排在 SK / 纵火之后，被它们抢先结算。
        if any(player.role == Role.SERIAL_KILLER for player in alive):
            return events
        if any(player.role == Role.ARSONIST for player in alive) and not any(
            player.role == Role.GUNNER and player.bullet_count > 0 for player in alive
        ):
            return events
        cultist = next((player for player in alive if player.role == Role.CULTIST), None)
        if cultist:
            other = next(player for player in alive if player.user_id != cultist.user_id)
            if other.role == Role.CULTIST_HUNTER:
                # Werewolf.cs:4576-4580 —— CHKillsCultistEnd 只调用 DBKill 记录战绩，
                # 不设置 IsDead，教徒在结算名单里仍然存活。
                cultist.metadata["db_killed_by"] = other.user_id
                cultist.metadata["db_kill_method"] = KillMethod.HUNT.value
                events.append(DomainEvent(
                    "ch_kills_cultist_end",
                    get_locale_string(
                        "CHKillsCultistEnd", None, self._name(cultist), self._name(other)
                    ).strip(),
                    metadata={"cultist_id": cultist.user_id, "hunter_id": other.user_id},
                ))
            elif self._is_majority_wolf(other):
                # Werewolf.cs:4570-4575 —— 教徒对狼直接 DoGameEnd(Wolf)，官方不杀教徒。
                pass
            elif other.role not in {Role.DOPPELGANGER, Role.THIEF}:
                events.extend(self._transform_checks(other, method="AutoConvertToCult"))
                other.role = Role.CULTIST
                other.day_cult = room.day
                other.changed_roles_count += 1
        return events

    def _finish_or_start_day(self, room: GameRoom, *, night_end: bool = True) -> list[DomainEvent]:
        if room.phase == GamePhase.FINISHED:
            return []
        # Werewolf.cs:4420-4425 —— 夜间死亡的猎人 delay=True，等所有夜间播报结束、
        # 进入 DayCycle 之前才统一开枪，期间整局流程被 HunterFinalShot 阻塞。
        if self._hunter_window(room, "day" if night_end else "day_only"):
            return []
        events: list[DomainEvent] = []
        if night_end:
            events.extend(self._night_end_achievements(room))
        events.extend(self._check_for_game_end(room))
        if room.phase == GamePhase.FINISHED:
            return events
        if night_end:
            # Werewolf.cs:4442-4462 —— CheckForGameEnd 之后统一重置夜间状态。
            for player in room.players:
                player.died_last_night = False
                player.killed_last_night = 0
                player.was_saved_last_night = False
                player.choice = None
                player.votes_received = 0
                if int(player.metadata.get("being_visited_same_night", 0)) >= 3:
                    player.busy_night = True
                player.being_visited_same_night_count = 0
                player.metadata.pop("being_visited_same_night", None)
                player.has_shot_hunter_attacker_this_night = False
        room.night_actions.clear()
        room.votes.clear()
        room.statistics.pop("sandman_sleep", None)
        # Werewolf.cs:633 —— DayCycle 之前调用 CheckLongHaul。
        events.extend(self._check_long_haul(room))
        room.phase = GamePhase.DAY
        time_to_add = max(((sum(player.alive for player in room.players) // 5) - 1) * 30, 60)
        day_seconds = room.rules.day_seconds + time_to_add
        room.stage_started_at, room.stage_deadline = self._now(), self._deadline(day_seconds)
        room.state_version += 1
        events.append(DomainEvent("day_started", f"第 {room.day} 天开始，讨论阶段 {day_seconds} 秒后进入投票。\n{self.public_roster(room)}"))
        # Werewolf.cs:2833 —— SendPlayerList 之后紧接 SendDayActions。
        events.extend(self._day_prompts(room))
        return events

    def _night_end_achievements(self, room: GameRoom) -> list[DomainEvent]:
        """Werewolf.cs:4196-4431 —— 夜间死亡播报区块内的成就结算。"""
        events: list[DomainEvent] = []
        if not any(player.died_last_night for player in room.players):
            return events
        # Werewolf.cs:4408-4412 —— TripleKill 位于非 secret 播报循环内，
        # 纵火焚烧死亡在非 secret 模式下走单独播报，不进入该循环。
        secret = not room.rules.show_roles_on_death
        notified = [
            player for player in room.players
            if player.died_last_night and (
                secret or not (
                    not player.metadata.get("died_by_visiting_victim")
                    and player.metadata.get("killed_by_role") == Role.ARSONIST.value
                )
            )
        ]
        if notified and not secret:
            for player in room.players:
                if (
                    player.role in WOLF_ROLES or player.role == Role.SERIAL_KILLER
                ) and player.killed_last_night >= 3:
                    events.extend(self._add_achievement(player, Achievement.TRIPLE_KILL))
        # Werewolf.cs:4427-4431 —— 本夜死亡人数达到 4 人，全员解锁 BloodyNight。
        night_start = int(room.statistics.get("night_start_death_sequence", 0))
        bloody = [
            player for player in room.players
            if not player.alive
            and int(player.metadata.get("death_sequence", 0)) > night_start
        ]
        if len(bloody) >= 4:
            for player in bloody:
                events.extend(self._add_achievement(player, Achievement.BLOODY_NIGHT))
        return events

    def start_vote(self, room: GameRoom) -> list[DomainEvent]:
        if room.phase != GamePhase.DAY:
            return []
        events = self._resolve_day_actions(room)
        events.extend(self._check_for_game_end(room))
        if room.phase == GamePhase.FINISHED:
            return events
        # 白天枪手打死猎人时，官方在 KillPlayer 里立即阻塞开枪，之后才进入 LynchCycle。
        if self._hunter_window(room, "vote"):
            return events
        events.extend(self._begin_vote(room))
        return events

    def _begin_vote(self, room: GameRoom) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        # Werewolf.cs:637 —— LynchCycle 之前调用 CheckLongHaul。
        events.extend(self._check_long_haul(room))
        # Werewolf.cs:2559-2564 —— 白天已按下和平时，LynchCycle 在发处决菜单之前
        # 直接 PacifistNoLynchNow 并 return，本轮根本不进入投票阶段。
        if room.statistics.pop("pacifist_used", None):
            room.statistics.pop("double_lynch", None)
            events.append(DomainEvent("pacifist_vote", "和平主义者能力生效，本轮无人出局。"))
            events.extend(self._after_lynch(room))
            return events
        room.phase, room.stage_started_at = GamePhase.VOTE, self._now()
        room.stage_deadline, room.votes = self._deadline(room.rules.vote_seconds), {}
        room.statistics["lynch_attempt"] = 1
        # Werewolf.cs:4954 —— SendLynchMenu 每轮开始重置 NoOneCastLynch。
        room.statistics["no_one_cast_lynch"] = True
        room.state_version += 1
        events.append(DomainEvent("vote_started", f"讨论结束，进入投票阶段（{room.rules.vote_seconds} 秒）。请发送：投票 座位号或弃票。官方规则投票后不可改票。\n{self.public_roster(room)}"))
        return events

    def _resolve_day_actions(self, room: GameRoom) -> list[DomainEvent]:
        """在讨论结束之后、处决执行之前结算官方的白天行动。"""
        actions = [action for action in room.day_actions.values() if action.day == room.day]
        by_type: dict[str, list[NightAction]] = {}
        for action in actions:
            by_type.setdefault(action.action, []).append(action)
        events: list[DomainEvent] = []

        # Werewolf.cs:2864-2868 —— 沙人与铁匠同日发动时，铁匠银粉白费。
        if room.statistics.get("sandman_sleep") and room.statistics.get("silver_spread"):
            blacksmith = next(
                (player for player in self._players(room) if player.role == Role.BLACKSMITH),
                None,
            )
            if blacksmith is not None:
                events.extend(self._add_achievement(blacksmith, Achievement.WASTED_SILVER))

        for action in by_type.get(QuestionType.SHOOT.value, ()):
            actor = self._find(room, action.actor_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if not actor.alive or target is None:
                continue
            if actor.role == Role.GUNNER and actor.bullet_count > 0:
                actor.bullet_count -= 1
                actor.has_used_ability = True
                # Werewolf.cs:2879-2881 —— 命中坏人角色累计 BulletHitBaddies。
                if target.role in {
                    Role.WOLF, Role.ALPHA_WOLF, Role.WOLF_CUB, Role.CULTIST,
                    Role.SERIAL_KILLER, Role.LYCAN, Role.SNOW_WOLF, Role.ARSONIST,
                }:
                    actor.bullet_hit_baddies += 1
                if target.role == Role.WISE_ELDER:
                    # Werewolf.cs:2891 —— Transform(gunner, Villager, KillElder, bullet: 0)。
                    events.extend(self._transform_checks(actor, method="KillElder"))
                    actor.role = Role.VILLAGER
                    actor.bullet_count = 0
                    actor.changed_roles_count += 1
                events.extend(self._kill(room, target, KillMethod.SHOOT, source=actor))
            elif actor.role == Role.SPUMPKIN:
                if self.rng.randrange(100) < room.rules.spumpkin_detonation_chance:
                    if target.role == Role.WISE_ELDER:
                        # Werewolf.cs:2916 —— Transform(spumpkin, Villager, KillElder)。
                        events.extend(self._transform_checks(actor, method="KillElder"))
                        actor.role = Role.VILLAGER
                        actor.changed_roles_count += 1
                    events.extend(self._kill(room, actor, KillMethod.SHOOT, source=actor))
                    events.extend(self._kill(room, target, KillMethod.SHOOT, source=actor))
                else:
                    events.append(DomainEvent("spumpkin_failed", "南瓜头的引爆失败了。", public=False, target_user_id=actor.user_id))

        for action in by_type.get(QuestionType.DETECT.value, ()):
            actor = self._find(room, action.actor_id)
            target = next((p for p in room.players if p.user_id == action.target_id), None)
            if actor.role != Role.DETECTIVE or not actor.alive or target is None:
                continue
            if self.rng.randrange(100) < room.rules.detective_caught_chance:
                for wolf in self._players(room):
                    if self._is_majority_wolf(wolf):
                        events.append(DomainEvent(
                            "detective_caught",
                            "侦探正在调查村庄。",
                            public=False,
                            target_user_id=wolf.user_id,
                        ))
            events.append(DomainEvent(
                "detect_result",
                f"侦查结果：{self._name(target)}的身份是【{role_display_name(target.role or Role.VILLAGER)}】。",
                public=False,
                target_user_id=actor.user_id,
                metadata={"seen_role": target.role.value if target.role else Role.VILLAGER.value, "target_id": target.user_id},
            ))
            # Werewolf.cs:2951-2966 —— 连续查到不同坏人 4 次解锁 Streetwise。
            if target.role not in {
                Role.WOLF, Role.ALPHA_WOLF, Role.WOLF_CUB, Role.LYCAN, Role.CULTIST,
                Role.SERIAL_KILLER, Role.SNOW_WOLF, Role.ARSONIST,
            }:
                actor.correct_snooped.clear()
            else:
                if target.user_id in actor.correct_snooped:
                    actor.correct_snooped.clear()
                actor.correct_snooped.append(target.user_id)
                if len(actor.correct_snooped) >= 4:
                    events.extend(self._add_achievement(actor, Achievement.STREETWISE))
                    actor.correct_snooped.clear()
        room.day_actions.clear()
        return events

    def submit_day_action(
        self, room: GameRoom, user_id: str, action: str, target_token: str | None = None
    ) -> list[DomainEvent]:
        actor = self._find(room, user_id)
        if not actor.alive:
            raise GameRuleError("出局玩家不能执行白天行动")
        if user_id in room.day_actions:
            raise GameRuleError("该白天行动已经提交")
        normalized = action.casefold()
        if normalized in {"detect", "侦查"}:
            if room.phase != GamePhase.DAY or actor.role != Role.DETECTIVE:
                raise GameRuleError("当前不能进行侦查")
            target, _ = self._parse_targets(room, actor, QuestionType.DETECT, target_token)
            if target is None:
                raise GameRuleError("侦查需要一个目标")
            room.day_actions[actor.user_id] = NightAction(
                actor_id=actor.user_id,
                action=QuestionType.DETECT.value,
                target_id=target.user_id,
                day=room.day,
            )
            room.state_version += 1
            return [DomainEvent("action_accepted", "侦查已记录，讨论结束后会私聊发送结果。", public=False, target_user_id=user_id)]
        if normalized in {"silver", "撒银"}:
            if room.phase != GamePhase.DAY or actor.role != Role.BLACKSMITH or actor.has_used_ability:
                raise GameRuleError("当前不能撒银")
            if (target_token or "").strip().casefold() not in _YES:
                return [DomainEvent("action_accepted", "本日未使用撒银能力。", public=False, target_user_id=user_id)]
            actor.has_used_ability = True
            room.statistics["silver_spread"] = True
            room.state_version += 1
            # Werewolf.cs:941 —— BlacksmithSpreadSilver 公开点名铁匠。
            return [DomainEvent("silver_spread", f"{actor.public_name} 撒下银粉，本夜狼人袭击会被阻断。")]
        if normalized in {"sandman", "催眠"}:
            if room.phase != GamePhase.DAY or actor.role != Role.SANDMAN or actor.has_used_ability:
                raise GameRuleError("当前不能催眠")
            if (target_token or "").strip().casefold() not in _YES:
                return [DomainEvent("action_accepted", "本日未使用催眠能力。", public=False, target_user_id=user_id)]
            actor.has_used_ability = True
            room.statistics["sandman_sleep"] = True
            room.state_version += 1
            # Werewolf.cs:956 —— SandmanSleepAll 公开点名沙人。
            return [DomainEvent("sandman", f"{actor.public_name} 将在今晚催眠村庄。")]
        if normalized in {"shoot", "开枪"}:
            if room.phase != GamePhase.DAY or actor.role not in {Role.GUNNER, Role.SPUMPKIN}:
                raise GameRuleError("当前不能开枪或已没有子弹")
            if actor.role == Role.GUNNER and actor.bullet_count <= 0:
                raise GameRuleError("当前不能开枪或已没有子弹")
            target = self._target(room, target_token or "", actor_id=user_id)
            room.day_actions[actor.user_id] = NightAction(
                actor_id=actor.user_id,
                action=QuestionType.SHOOT.value,
                target_id=target.user_id,
                day=room.day,
            )
            room.state_version += 1
            return [DomainEvent("action_accepted", "开枪目标已记录，讨论结束后统一结算。", public=False, target_user_id=user_id)]
        if normalized in {"mayor", "市长"}:
            # Werewolf.cs:899-911 —— Mayor reveal 在 CurrentQuestion 校验之前，
            # 只要求存活且未用过能力，夜晚同样生效。
            if room.phase not in {GamePhase.DAY, GamePhase.VOTE, GamePhase.NIGHT} or actor.role != Role.MAYOR or actor.has_revealed:
                raise GameRuleError("你不是市长或已经公开身份")
            actor.has_revealed = True
            actor.has_used_ability = True
            actor.vote_weight = 2
            room.state_version += 1
            return [DomainEvent("mayor_revealed", f"{actor.public_name} 已公开市长身份，投票权重提升。")]
        if normalized in {"pacifist", "和平"}:
            # Werewolf.cs:913-926 —— 和平按钮同样在 CurrentQuestion 校验之前，
            # 处决阶段按下会立即中止本轮处决，并覆盖捣乱者的双重处决。
            if room.phase not in {GamePhase.DAY, GamePhase.VOTE} or actor.role != Role.PACIFIST or actor.has_used_ability:
                raise GameRuleError("当前不能使用和平能力")
            actor.has_used_ability = True
            room.statistics["pacifist_used"] = True
            room.statistics.pop("double_lynch", None)
            room.state_version += 1
            # Werewolf.cs:2591-2604 —— 和平生效瞬间统计当前处决票，判定
            # EveryManForHimself / MySweetieSoStrong。
            events: list[DomainEvent] = []
            alive_count = len(self._players(room))
            votes_for_self = sum(
                1 for target_id in room.votes.values() if target_id == actor.user_id
            )
            if votes_for_self > alive_count / 2:
                events.extend(self._add_achievement(actor, Achievement.EVERY_MAN_FOR_HIMSELF))
            elif actor.lover_id:
                votes_for_lover = sum(
                    1 for target_id in room.votes.values() if target_id == actor.lover_id
                )
                lover = next(
                    (member for member in room.players if member.user_id == actor.lover_id),
                    None,
                )
                if lover is not None and votes_for_lover > alive_count / 2:
                    events.extend(self._add_achievement(lover, Achievement.MY_SWEETIE_SO_STRONG))
            # Werewolf.cs:918 —— PacifistNoLynch 公开点名和平主义者。
            events.append(DomainEvent("pacifist", f"{actor.public_name} 阻止了本轮处决。"))
            # Werewolf.cs:2574-2604 —— 处决倒计时每秒检查 _pacifistUsed，一旦置位
            # 立即中止本轮处决，不必等投票时间走完。
            if room.phase == GamePhase.VOTE:
                events.extend(self.resolve_vote(room))
            return events
        if normalized in {"trouble", "捣乱"}:
            if room.phase != GamePhase.DAY or actor.role != Role.TROUBLEMAKER or actor.has_used_ability:
                raise GameRuleError("当前不能使用捣乱能力")
            actor.has_used_ability = True
            room.statistics["double_lynch"] = True
            room.statistics.pop("pacifist_used", None)
            room.state_version += 1
            # Werewolf.cs:972 —— TroublemakerDoubleLynch 公开点名捣乱者。
            return [DomainEvent("troublemaker", f"{actor.public_name} 启用了本轮双重处决。")]
        raise GameRuleError("当前不支持该白天行动")

    def submit_vote(self, room: GameRoom, user_id: str, target_token: str | None) -> list[DomainEvent]:
        if room.phase != GamePhase.VOTE:
            raise GameRuleError("现在不是投票阶段")
        voter = self._find(room, user_id)
        if not voter.alive:
            raise GameRuleError("出局玩家不能投票")
        if user_id in room.votes:
            raise GameRuleError("已经投过票，官方规则不允许改票")
        normalized = (target_token or "").strip()
        # SendLynchMenu(Werewolf.cs:4950-4966) 的处决菜单里没有“空投票”按钮，
        # 未做选择的玩家不算投过票，计时内仍可继续投票。
        if not normalized:
            raise GameRuleError("请填写要投票的座位号，例如 /vote 3；弃票请发送“弃票”")
        extra: list[DomainEvent] = []
        if normalized.casefold() in _SKIP:
            if not room.rules.allow_vote_skip:
                raise GameRuleError("当前规则不允许弃票")
            room.votes[user_id] = None
        else:
            target = self._target(room, normalized, actor_id=voter.user_id)
            if voter.role == Role.CLUMSY_GUY:
                # Werewolf.cs:999-1012 —— 迷糊者 50% 改票；改后仍是原目标或未改票都算正确处决。
                if self.rng.randrange(100) < room.rules.clumsy_retarget_chance:
                    others = [player for player in self._players(room) if player.user_id != voter.user_id]
                    if others:
                        self.rng.shuffle(others)
                        self.rng.shuffle(others)
                        if others[0].user_id == target.user_id:
                            voter.clumsy_correct_lynch_count += 1
                        target = others[0]
                else:
                    voter.clumsy_correct_lynch_count += 1
            room.votes[user_id] = target.user_id
            target.has_been_voted = True
            # Werewolf.cs:1138-1152 —— 本轮第一个投出处决票者累计 FirstStone（弃票不算）。
            if room.statistics.get("no_one_cast_lynch"):
                voter.first_stone += 1
                room.statistics["no_one_cast_lynch"] = False
                if voter.first_stone == 5:
                    extra.extend(self._add_achievement(voter, Achievement.FIRST_STONE))
        room.state_version += 1
        events = [DomainEvent("vote_accepted", "投票已记录。", public=False, target_user_id=user_id)]
        events.extend(extra)
        if {p.user_id for p in self._players(room)}.issubset(room.votes):
            events.extend(self.resolve_vote(room))
        return events

    def resolve_vote(self, room: GameRoom) -> list[DomainEvent]:
        if room.phase != GamePhase.VOTE:
            return []
        # Werewolf.cs:2574-2604 —— 处决倒计时里一旦 _pacifistUsed 置位，LynchCycle
        # 立刻公告 PacifistNoLynchNow 并 return：既不计票、不累计 NonVote，也不公布
        # 秘密投票结果，直接回到 GameTimer 进入下一夜。
        if room.statistics.pop("pacifist_used", None):
            room.statistics.pop("double_lynch", None)
            for player in room.players:
                player.votes_received = 0
            events = [DomainEvent("pacifist_vote", "和平主义者能力生效，本轮无人出局。")]
            room.vote_history.append({
                "day": room.day,
                "votes": dict(room.votes),
                "counts": {},
                "secret_lynch": None,
                "eliminated": [],
            })
            room.votes.clear()
            events.extend(self._after_lynch(room))
            return events
        for player in room.players:
            player.votes_received = 0
        counts: Counter[str] = Counter()
        voters_by_target: dict[str, list[dict[str, object]]] = {}
        events: list[DomainEvent] = []
        # Werewolf.cs:2661 —— NonVote 仅在 lynchAttempt < 2 时累计。
        lynch_attempt = int(room.statistics.get("lynch_attempt", 1) or 1)
        if lynch_attempt < 2:
            for player in self._players(room):
                voted = room.votes.get(player.user_id)
                if player.user_id not in room.votes or voted is None:
                    player.non_vote_count += 1
                    if player.non_vote_count >= 2:
                        events.extend(self._kill(
                            room, player, KillMethod.IDLE, source=player,
                            hunter_final_shot=False, lover_chain=False, is_night=False,
                        ))
                        events.append(DomainEvent("idle_killed", f"{player.public_name} 连续未投票，已出局。"))
                        events.extend(self._resolve_role_changes(room))
                else:
                    player.non_vote_count = 0
        else:
            for player in self._players(room):
                if room.votes.get(player.user_id):
                    player.non_vote_count = 0
        for voter_id, target_id in room.votes.items():
            voter = next((player for player in room.players if player.user_id == voter_id), None)
            if not voter or not voter.alive or not target_id:
                continue
            weight = max(1, voter.vote_weight)
            # Werewolf.cs:2653 —— 已公开的市长每次成功投票累计 MayorLynchAfterRevealCount。
            if voter.role == Role.MAYOR and voter.has_used_ability:
                voter.mayor_lynch_after_reveal_count += 1
            counts[target_id] += weight
            voters_by_target.setdefault(target_id, []).append({
                "user_id": voter.user_id,
                "weight": weight,
            })
        for target_id, count in counts.items():
            target = next((player for player in room.players if player.user_id == target_id), None)
            if target:
                target.votes_received = count
        eliminated: list[Player] = []
        if counts:
            maximum = max(counts.values())
            candidates = [key for key, value in counts.items() if value == maximum]
            if len(candidates) == 1:
                target = next(player for player in room.players if player.user_id == candidates[0])
                if target.alive and target.role == Role.PRINCE and not target.has_revealed:
                    target.has_revealed = True
                    target.has_used_ability = True
                    events.append(DomainEvent("prince_revealed", "王子被投中并公开身份，本轮免于处决。"))
                elif target.alive:
                    eliminated.append(target)
            elif room.rules.random_lynch:
                random_candidates = [
                    player for player in room.players if player.user_id in candidates and player.alive
                ]
                self.rng.shuffle(random_candidates)
                self.rng.shuffle(random_candidates)
                if random_candidates:
                    target = random_candidates[0]
                    if target.role == Role.PRINCE and not target.has_revealed:
                        target.has_revealed = True
                        target.has_used_ability = True
                        events.append(DomainEvent("prince_revealed", "王子被投中并公开身份，本轮免于处决。"))
                    else:
                        eliminated.append(target)
            else:
                names = "、".join(
                    next(player.public_name for player in room.players if player.user_id == key)
                    for key in candidates
                )
                events.append(DomainEvent("vote_tie", f"投票平票（{names}），本轮无人出局。"))
                # Werewolf.cs:2786-2788 —— 平票且替罪羊在最高票之列时解锁 SoClose。
                tanner = next(
                    (player for player in room.players
                     if player.user_id in candidates and player.role == Role.TANNER),
                    None,
                )
                if tanner is not None and maximum > 0:
                    events.extend(self._add_achievement(tanner, Achievement.SO_CLOSE))
        else:
            events.append(DomainEvent("vote_empty", "本轮没有有效投票，无人出局。"))
        if room.rules.secret_lynch and room.rules.secret_lynch_show_votes:
            secret_results = sorted([
                {
                    "target_id": player.user_id,
                    "votes": counts[player.user_id],
                    "voters": voters_by_target.get(player.user_id, [])
                    if room.rules.secret_lynch_show_voters else [],
                }
                for player in room.players
                if counts.get(player.user_id, 0) > 0
            ], key=lambda item: -item["votes"])
            events.append(DomainEvent(
                "secret_lynch_result",
                "秘密投票结果已公布。",
                metadata={
                    "results": secret_results,
                    "show_voters": room.rules.secret_lynch_show_voters,
                },
            ))
        for target in eliminated:
            # Werewolf.cs:2755-2766 —— 处决判定在击杀之前，读取的是击杀前的存活人数。
            if target.role == Role.TANNER:
                if len(self._players(room)) == 3:
                    events.extend(self._add_achievement(target, Achievement.THAT_CAME_UNEXPECTED))
                if target.lover_id:
                    lover = next(
                        (member for member in room.players if member.user_id == target.lover_id),
                        None,
                    )
                    if lover is not None:
                        events.extend(self._add_achievement(lover, Achievement.ROMEO_AND_JULIET))
            if target.role == Role.SEER and room.day == 1:
                events.extend(self._add_achievement(target, Achievement.LACK_OF_TRUST))
            if target.role == Role.PRINCE and target.has_used_ability:
                events.extend(self._add_achievement(target, Achievement.SPOILED_RICH_BRAT))
            # Werewolf.cs:2766 —— killers: Players.Where(x => x.Choice == lynched.Id)。
            lynch_killers = [
                member for member in room.players
                if room.votes.get(member.user_id) == target.user_id
            ]
            events.extend(self._kill(
                room, target, KillMethod.LYNCH, killers=lynch_killers, is_night=False,
            ))
            events.append(DomainEvent("vote_result", f"{self._name(target)}出局。"))
            # Werewolf.cs:2771-2773 —— 击杀后统计剩余存活者是否全票投给了替罪羊。
            if target.role == Role.TANNER:
                alive_rest = self._players(room)
                if alive_rest and all(
                    room.votes.get(member.user_id) == target.user_id for member in alive_rest
                ):
                    events.extend(self._add_achievement(target, Achievement.TANNER_OVERKILL))
        room.vote_history.append({
            "day": room.day,
            "votes": dict(room.votes),
            "counts": dict(counts),
            "secret_lynch": (
                {
                    "show_votes": room.rules.secret_lynch_show_votes,
                    "show_voters": room.rules.secret_lynch_show_voters,
                    "results": sorted([
                        {
                            "target_id": target_id,
                            "votes": count,
                            "voters": voters_by_target.get(target_id, [])
                            if room.rules.secret_lynch_show_voters else [],
                        }
                        for target_id, count in counts.items()
                    ], key=lambda item: -item["votes"]),
                }
                if room.rules.secret_lynch else None
            ),
            "eliminated": [player.user_id for player in eliminated],
        })
        room.votes.clear()
        if any(target.role == Role.TANNER for target in eliminated):
            room.statistics["end_kind"] = "Tanner"
            events.extend(self.finish(room, Team.TANNER))
            return events
        # Werewolf.cs:5620-5626 —— 白天被处决的猎人 delay=False，官方在 KillPlayer 内
        # 立即阻塞开枪，之后才继续 CheckRoleChanges / CheckForGameEnd / NightCycle。
        if self._hunter_window(room, "night"):
            return events
        events.extend(self._after_lynch(room))
        return events

    def _after_lynch(self, room: GameRoom) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        events.extend(self._resolve_role_changes(room, checkbitten=True))
        events.extend(self._check_for_game_end(room, checkbitten=True))
        if room.phase == GamePhase.FINISHED:
            return events
        if room.statistics.get("double_lynch"):
            room.statistics.pop("double_lynch", None)
            room.phase, room.stage_started_at = GamePhase.VOTE, self._now()
            room.stage_deadline, room.votes = self._deadline(room.rules.vote_seconds), {}
            room.statistics["lynch_attempt"] = 2
            room.statistics["no_one_cast_lynch"] = True
            room.state_version += 1
            events.append(DomainEvent(
                "vote_started",
                f"捣乱者发动第二轮处决。请再次投票（{room.rules.vote_seconds} 秒）。\n{self.public_roster(room)}",
            ))
            return events
        room.statistics.pop("lynch_attempt", None)
        room.day += 1
        room.phase, room.night_actions = GamePhase.NIGHT, {}
        # Werewolf.cs:2977 —— nightStart，用于夜末 BloodyNight 判定本夜死亡者。
        room.statistics["night_start_death_sequence"] = int(
            room.statistics.get("death_sequence", 0)
        )
        # Werewolf.cs:2979-2987 —— NightCycle 开头统一重置每位玩家的夜间状态。
        for player in room.players:
            player.burning = False
            player.choice = None
            player.choice2 = None
            player.votes_received = 0
            player.died_last_night = False
            player.killed_last_night = 0
            player.metadata.pop("being_visited_same_night", None)
        events.extend(self._apply_pending_bites(room))
        events.extend(self._resolve_role_changes(room))
        events.extend(self._check_for_game_end(room))
        if room.phase == GamePhase.FINISHED:
            return events
        # Werewolf.cs:629 —— NightCycle 之前调用 CheckLongHaul。
        events.extend(self._check_long_haul(room))
        if room.statistics.get("sandman_sleep"):
            events.append(DomainEvent("night_started", f"第 {room.day} 夜开始，但沙人催眠了村庄。"))
            events.extend(self.resolve_night(room))
            return events
        # Werewolf.cs:3021-3026 —— 狼队里有人被灌醉时，清醒的狼获得 ThanksJunior。
        alive_wolves = [
            player for player in self._players(room) if player.role in WOLF_ROLES
        ]
        if any(player.drunk for player in alive_wolves):
            for wolf in alive_wolves:
                if not wolf.drunk:
                    events.extend(self._add_achievement(wolf, Achievement.THANKS_JUNIOR))
        room.stage_started_at, room.stage_deadline = self._now(), self._deadline(self._night_duration(room))
        room.state_version += 1
        events.append(DomainEvent("night_started", f"第 {room.day} 夜开始，请查看私聊行动提示。"))
        events.extend(self._night_prompts(room))
        return events

    def check_winners(self, room: GameRoom, *, checkbitten: bool = False) -> tuple[Team, ...]:
        room.statistics.pop("gunner_saves_pending", None)
        if room.statistics.get("end_kind") == "SKHunter":
            return (Team.SK_HUNTER,)
        alive = self._players(room)
        if not alive:
            return (Team.NO_ONE,)
        wolves = [player for player in alive if self._is_majority_wolf(player)]
        cult = [player for player in alive if player.team == Team.CULT]
        serial = [player for player in alive if player.role == Role.SERIAL_KILLER]
        arson = [player for player in alive if player.role == Role.ARSONIST]
        no_winner_roles = {Role.TANNER, Role.SORCERER, Role.THIEF, Role.DOPPELGANGER}
        pending_bite = checkbitten and any(player.bitten and player.alive for player in room.players)

        if len(alive) == 1:
            player = alive[0]
            return (Team.NO_ONE,) if player.role in no_winner_roles else (player.team or Team.NO_ONE,)

        if len(alive) == 2:
            if all(player.lover_id for player in alive):
                return (Team.LOVERS,)
            if all(player.role in no_winner_roles for player in alive):
                return (Team.NO_ONE,)
            if serial:
                return (Team.SERIAL_KILLER,)
            if arson and not any(player.role == Role.GUNNER and player.bullet_count > 0 for player in alive):
                return (Team.ARSONIST,)
            if cult:
                other = next((player for player in alive if player.team != Team.CULT), None)
                if other is None:
                    return (Team.CULT,)
                if self._is_majority_wolf(other):
                    return (Team.WOLF,)
                if other.role == Role.CULTIST_HUNTER:
                    return (Team.VILLAGE,)
                return (Team.CULT,)

        if len(alive) == 3 and all(player.role in {Role.SORCERER, Role.THIEF, Role.DOPPELGANGER} for player in alive):
            return (Team.NO_ONE,)
        if serial or arson:
            return ()
        if cult and len(cult) == len(alive):
            return (Team.CULT,)
        others = len(alive) - len(wolves)
        if wolves and len(wolves) >= others:
            gunner = any(player.role == Role.GUNNER and player.bullet_count > 0 for player in alive)
            lovers_exception = (
                len(wolves) == others + 1
                and sum(1 for player in wolves if player.lover_id) == 2
            )
            if gunner and (len(wolves) == others or lovers_exception):
                # Werewolf.cs:4620-4624 —— 枪手的子弹让村民阵营免于失败，全体村民解锁 GunnerSaves。
                room.statistics["gunner_saves_pending"] = True
                return ()
            return (Team.WOLF,)
        if not wolves and not cult and not pending_bite:
            return (Team.VILLAGE,)
        return ()

    def check_winner(self, room: GameRoom) -> Team | None:
        winners = self.check_winners(room)
        return winners[0] if winners else None

    def finish(self, room: GameRoom, winner: Team | Iterable[Team]) -> list[DomainEvent]:
        if room.phase == GamePhase.FINISHED:
            return []
        winners = (winner,) if isinstance(winner, Team) else tuple(winner)
        winners = tuple(dict.fromkeys(winners)) or (Team.NO_ONE,)
        events: list[DomainEvent] = []
        # Werewolf.cs:4645 —— DoGameEnd 开头调用 CheckLongHaul。
        events.extend(self._check_long_haul(room))
        if winners == (Team.NO_ONE,):
            events.extend(self._resolve_no_one_end(room))
        room.winner, room.phase, room.stage_deadline = winners[0], GamePhase.FINISHED, None
        if Team.LOVERS in winners:
            # Werewolf.cs:4652-4661 —— 情侣获胜时的成就结算。
            lovers = [player for player in room.players if player.lover_id]
            forbidden = (
                any(player.role in WOLF_ROLES for player in lovers)
                and any(player.role == Role.VILLAGER for player in lovers)
            )
            for lover in lovers:
                if forbidden:
                    events.extend(self._add_achievement(lover, Achievement.FORBIDDEN_LOVE))
                if self._is_date_anywhere(14, 2, 2020):
                    events.extend(self._add_achievement(lover, Achievement.TODAYS_SPECIAL))
        room.winners = tuple(p.user_id for p in room.players if self._player_won(p, winners, room))
        for player in room.players:
            player.won = player.user_id in room.winners
        # Werewolf.cs:4929 —— 胜负标记完成后立即结算批量成就。
        events.extend(self._update_achievements(room))
        events.extend(self._resolve_overpower_end(room, winners))
        room.statistics.update({"winner_teams": [team.value for team in winners], "winner_ids": list(room.winners)})
        room.state_version += 1
        winner_text = "、".join(self._team_text(team) for team in winners)
        # Werewolf.cs:4770-4901 —— DoGameEnd 先播报胜利文案，再单独发结算名单。
        events.append(DomainEvent(
            "game_finished",
            self._win_announcement(room, winners),
            metadata={"winner_teams": [team.value for team in winners]},
        ))
        events.append(DomainEvent("game_summary", self._end_roster(room)))
        events.extend(
            DomainEvent(
                "personal_result",
                f"本局结算：获胜阵营为【{winner_text}】。你的身份是【{role_display_name(p.role) if p.role else '未知'}】，状态：{'存活' if p.alive else '出局'}，结果：{'胜利' if p.won else '失败'}。",
                public=False,
                target_user_id=p.user_id,
            )
            for p in room.players
        )
        return events

    def _win_announcement(self, room: GameRoom, winners: tuple[Team, ...]) -> str:
        """Werewolf.cs:4770-4901 —— DoGameEnd 中每个获胜队伍对应的公开胜利文案。"""
        keys: list[str] = []
        for team in winners:
            if team == Team.WOLF:
                # Werewolf.cs:4816 —— 官方这里的括号写法让「死掉的雪狼」同样计数，
                # 复刻时保留该判定，避免播报文案与官方不一致。
                pack = [
                    player for player in room.players
                    if (player.alive and player.role in WOLF_ROLES) or player.role == Role.SNOW_WOLF
                ]
                keys.append("WolvesWin" if len(pack) > 1 else "WolfWins")
            elif team == Team.TANNER:
                keys.append("TannerWins")
            elif team == Team.ARSONIST:
                keys.append("ArsonistWins")
            elif team == Team.CULT:
                keys.append("CultWins")
            elif team == Team.SERIAL_KILLER:
                keys.append("SerialKillerWins")
            elif team == Team.LOVERS:
                keys.append("LoversWin")
            elif team in {Team.NO_ONE, Team.SK_HUNTER}:
                keys.append("NoWinner")
            else:
                keys.append("VillageWins")
        return "\n".join(get_locale_string(key).strip() for key in dict.fromkeys(keys))

    def _end_roster(self, room: GameRoom) -> str:
        """Werewolf.cs:4906-4923 —— ShowRolesEnd 三种结算名单，末尾附 EndTime 用时。"""
        alive_count = sum(1 for player in room.players if player.alive)
        header = f"{get_locale_string('PlayersAlive')}: {alive_count} / {len(room.players)}"
        # 官方 `OrderBy(x => x.TimeDied)`：存活者 TimeDied 为 null 排在最前，其余按死亡先后。
        by_death = sorted(
            room.players, key=lambda player: int(player.metadata.get("death_sequence", 0))
        )
        lines: list[str]
        if room.rules.show_roles_end == "None":
            lines = [header]
            lines.extend(self._name(player) for player in by_death)
        elif room.rules.show_roles_end == "All":
            lines = [header]
            for player in by_death:
                status = (
                    get_locale_string("Alive") if player.alive
                    else get_locale_string("RanAway" if self._has_fled(player) else "Dead")
                )
                lines.append(
                    f"{self._name(player)}: {status} - "
                    f"{role_display_name(player.role) if player.role else ''}"
                    f"{'❤️' if player.lover_id else ''} "
                    f"{get_locale_string('Won' if player.won else 'Lost')}"
                )
        else:
            lines = [get_locale_string("RemainingPlayersEnd")]
            for player in sorted(
                (player for player in room.players if player.alive),
                key=lambda player: _TEAM_ORDER.get(player.team, len(_TEAM_ORDER)),
            ):
                lines.append(
                    f"{self._name(player)}: "
                    f"{role_display_name(player.role) if player.role else ''} "
                    f"{get_locale_string(f'{player.team.value}TeamEnd') if player.team else ''} "
                    f"{'❤️' if player.lover_id else ''} "
                    f"{get_locale_string('Won' if player.won else 'Lost')}"
                )
        started_at = room.statistics.get("game_started_at")
        if started_at:
            try:
                started = datetime.fromisoformat(started_at)
            except (TypeError, ValueError):
                started = None
            if started is not None:
                total = int(max((self._now() - started).total_seconds(), 0))
                lines.append(get_locale_string(
                    "EndTime",
                    None,
                    f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}",
                ))
        return "\n".join(lines)

    def _resolve_overpower_end(self, room: GameRoom, winners: tuple[Team, ...]) -> list[DomainEvent]:
        """Werewolf.cs:4834-4869 —— 纵火犯/连环杀手获胜时压制并杀死剩下的存活者。"""
        alive = self._players(room)
        if len(alive) <= 1:
            return []
        events: list[DomainEvent] = []
        if Team.ARSONIST in winners:
            arsonist = next((player for player in alive if player.role == Role.ARSONIST), None)
            other = next((player for player in alive if player.role != Role.ARSONIST), None)
            if arsonist and other:
                events.append(DomainEvent(
                    "arsonist_overpower",
                    get_locale_string(
                        "ArsonistWinsOverpower", None, self._name(arsonist), self._name(other)
                    ).strip(),
                    metadata={"arsonist_id": arsonist.user_id, "victim_id": other.user_id},
                ))
                # 官方只做 DBKill + IsDead，不触发恋人殉情/猎人开枪等后续。
                other.alive = False
                other.killed_by = arsonist.user_id
                other.kill_method = KillMethod.BURN.value
                other.metadata["killed_by_role"] = Role.ARSONIST.value
        elif Team.SERIAL_KILLER in winners:
            killer = next((player for player in alive if player.role == Role.SERIAL_KILLER), None)
            other = next((player for player in alive if player.role != Role.SERIAL_KILLER), None)
            if killer and other:
                events.append(DomainEvent(
                    "serial_killer_overpower",
                    get_locale_string(
                        "SerialKillerWinsOverpower", None, self._name(killer), self._name(other)
                    ).strip(),
                    metadata={"killer_id": killer.user_id, "victim_id": other.user_id},
                ))
                events.extend(self._kill(
                    room, other, KillMethod.SERIAL_KILLED, source=killer,
                    hunter_final_shot=False, is_night=False,
                ))
        return events

    def _resolve_no_one_end(self, room: GameRoom) -> list[DomainEvent]:
        """复刻官方只在结算时发出的皮匠、盗贼、二重身专属文案。"""
        alive = self._players(room)
        roles = {Role.SORCERER, Role.THIEF, Role.DOPPELGANGER}
        events: list[DomainEvent] = []
        if len(alive) == 3 and all(player.role in roles for player in alive):
            # Werewolf.cs:4710 —— 巫师活到三人残局，解锁 TimeToRetire。
            sorcerer = next((player for player in alive if player.role == Role.SORCERER), None)
            if sorcerer:
                events.extend(self._add_achievement(sorcerer, Achievement.TIME_TO_RETIRE))
            events.append(DomainEvent(
                "no_one_special_end",
                "巫师、盗贼和分身都活到了最后，本局无人获胜。",
                metadata={"roles": [player.role.value for player in alive]},
            ))
        elif len(alive) == 2:
            tanner = next((player for player in alive if player.role == Role.TANNER), None)
            special = next((player for player in alive if player.role in roles), None)
            if tanner and special:
                events.extend(self._kill(
                    room, tanner, KillMethod.SUICIDE, source=tanner,
                    hunter_final_shot=False,
                ))
                events.append(DomainEvent(
                    "no_one_special_end",
                    f"{tanner.public_name} 是坦纳，残局按官方规则结束。",
                    metadata={"tanner_id": tanner.user_id, "special_id": special.user_id},
                ))
                if special.role == Role.DOPPELGANGER and special.role_model:
                    model = next((item for item in room.players if item.user_id == special.role_model), None)
                    if model and not model.alive:
                        events.extend(self._resolve_role_changes(room))
                        if special.role == Role.TANNER and special.alive:
                            events.extend(self._kill(
                                room, special, KillMethod.SUICIDE, source=special,
                                hunter_final_shot=False,
                            ))
                # Werewolf.cs:4748-4751 —— 变身判定之后，若幸存者仍是巫师则解锁 TimeToRetire。
                if special.role == Role.SORCERER:
                    events.extend(self._add_achievement(special, Achievement.TIME_TO_RETIRE))
            elif all(player.role in roles for player in alive):
                # Werewolf.cs:4763 —— 巫师与盗贼/分身的双人残局，巫师解锁 TimeToRetire。
                sorcerer = next((player for player in alive if player.role == Role.SORCERER), None)
                if sorcerer:
                    events.extend(self._add_achievement(sorcerer, Achievement.TIME_TO_RETIRE))
                # Official DoGameEnd reports the two-player special-role ending
                # even though no player is marked as a winner.
                events.append(DomainEvent(
                    "no_one_special_end",
                    "、".join(
                        f"{player.public_name}（{role_display_name(player.role)}）"
                        for player in alive
                    ) + " 存活到残局，本局无人获胜。",
                    metadata={"roles": [player.role.value for player in alive]},
                ))
        elif len(alive) == 1 and alive[0].role in {Role.TANNER, *roles}:
            # Werewolf.cs:4795 —— 最后一名幸存者是巫师，解锁 TimeToRetire。
            if alive[0].role == Role.SORCERER:
                events.extend(self._add_achievement(alive[0], Achievement.TIME_TO_RETIRE))
            events.append(DomainEvent(
                "no_one_special_end",
                f"{alive[0].public_name} 的身份不会产生获胜阵营，本局无人获胜。",
                metadata={"player_id": alive[0].user_id, "role": alive[0].role.value},
            ))
        return events

    @classmethod
    def _player_won(cls, player: Player, winners: tuple[Team, ...], room: GameRoom | None = None) -> bool:
        if player.won:
            return True
        if room and room.statistics.get("end_kind") == "SKHunter":
            return False
        if Team.LOVERS in winners:
            return bool(player.alive and player.lover_id)
        if Team.SK_HUNTER in winners:
            return False
        if Team.TANNER in winners:
            # Werewolf.cs:4667-4690 —— 只有被处决的坦纳（DiedLastNight）计胜，
            # 且获胜者若有恋人，恋人一并计胜。
            if player.role == Role.TANNER and player.kill_method == KillMethod.LYNCH.value:
                return True
            if player.lover_id and room:
                lover = next((item for item in room.players if item.user_id == player.lover_id), None)
                if lover and lover.role == Role.TANNER and lover.kill_method == KillMethod.LYNCH.value:
                    return True
            return False
        won = player.team in winners or (Team.WOLF in winners and cls._is_majority_wolf(player))
        if won:
            return True
        if player.lover_id and room:
            lover = next((item for item in room.players if item.user_id == player.lover_id), None)
            if lover and (
                lover.team in winners
                or (Team.WOLF in winners and cls._is_majority_wolf(lover))
                or (Team.TANNER in winners and lover.role == Role.TANNER)
            ):
                return True
        return False

    @staticmethod
    def _team_text(team: Team) -> str:
        return {
            Team.VILLAGE: "村民阵营",
            Team.WOLF: "狼人阵营",
            Team.CULT: "教会阵营",
            Team.SERIAL_KILLER: "连环杀手",
            Team.ARSONIST: "纵火者",
            Team.LOVERS: "恋人",
            Team.SK_HUNTER: "猎人与连环杀手",
            Team.NO_ONE: "无人",
        }.get(team, team.value)

    @staticmethod
    def public_roster(room: GameRoom) -> str:
        return "玩家：" + "、".join(
            f"{p.list_label}{'' if p.alive else '（出局）'}"
            for p in room.players
        )

    def on_timeout(self, room: GameRoom) -> list[DomainEvent]:
        # 猎人临终一枪窗口优先于阶段计时器：官方期间整局流程被阻塞。
        if room.statistics.get("hunter_window") is not None:
            return self._close_hunter_window(room)
        if room.phase == GamePhase.LOBBY:
            if len(room.players) >= room.rules.min_players:
                # 等待超时自动开局：没有具体操作人，跳过房主校验。
                return self.start(
                    room, room.host_user_id or room.players[0].user_id, force=True
                )
            room.phase, room.stage_deadline = GamePhase.CANCELLED, None
            room.state_version += 1
            return [DomainEvent("join_timeout", "入场时间结束，人数不足，本局已取消。")]
        if room.phase == GamePhase.NIGHT:
            return self.resolve_night(room)
        if room.phase == GamePhase.DAY:
            return self.start_vote(room)
        if room.phase == GamePhase.VOTE:
            return self.resolve_vote(room)
        return []

    def due(self, room: GameRoom, now: datetime | None = None) -> bool:
        return bool(room.stage_deadline and room.stage_deadline <= (now or self._now()))
