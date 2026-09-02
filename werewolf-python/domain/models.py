from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

_OPAQUE_USER_ID = re.compile(r"^[0-9A-Fa-f]{32}$")


def is_opaque_display_name(name: str) -> bool:
    """QQ 群消息经常没有昵称，只能拿到 32 位 member_openid。"""
    return bool(_OPAQUE_USER_ID.fullmatch((name or "").strip()))


class Clock(Protocol):
    def now(self) -> datetime: ...


class RandomSource(Protocol):
    def shuffle(self, values: list[Any]) -> None: ...
    def choice(self, values: list[Any]) -> Any: ...
    def randrange(self, stop: int) -> int: ...


class ClockSource(Protocol):
    def now(self) -> datetime: ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GamePhase(str, Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    DAY = "day"
    VOTE = "vote"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class GameMode(str, Enum):
    NORMAL = "Normal"
    CHAOS = "Chaos"


class QuestionType(str, Enum):
    """Official question/action names from Werewolf Node."""

    LYNCH = "Lynch"
    KILL = "Kill"
    VISIT = "Visit"
    SEE = "See"
    SHOOT = "Shoot"
    GUARD = "Guard"
    DETECT = "Detect"
    CONVERT = "Convert"
    ROLE_MODEL = "RoleModel"
    HUNT = "Hunt"
    HUNTER_KILL = "HunterKill"
    SERIAL_KILL = "SerialKill"
    LOVER_1 = "Lover1"
    LOVER_2 = "Lover2"
    MAYOR = "Mayor"
    SPREAD_SILVER = "SpreadSilver"
    KILL_2 = "Kill2"
    SANDMAN = "Sandman"
    PACIFIST = "Pacifist"
    THIEF = "Thief"
    TROUBLE = "Trouble"
    CHEMISTRY = "Chemistry"
    FREEZE = "Freeze"
    DOUSE = "Douse"


class KillMethod(str, Enum):
    LYNCH = "Lynch"
    EAT = "Eat"
    SHOOT = "Shoot"
    VISIT_WOLF = "VisitWolf"
    VISIT_VICTIM = "VisitVictim"
    VISIT_BURNING = "VisitBurning"
    VISIT_KILLER = "VisitKiller"
    GUARD_WOLF = "GuardWolf"
    IDLE = "Idle"
    SERIAL_KILLED = "SerialKilled"
    HUNTER = "Hunter"
    HUNTER_CULT = "HunterCult"
    HUNT = "Hunt"
    LOVER_DIED = "LoverDied"
    BURN = "Burn"
    CHEMISTRY = "Chemistry"
    FALL_GRAVE = "FallGrave"
    SPOTTED = "Spotted"
    SUICIDE = "Suicide"
    FLEE = "Flee"


class Role(str, Enum):
    VILLAGER = "Villager"
    DRUNK = "Drunk"
    HARLOT = "Harlot"
    SEER = "Seer"
    TRAITOR = "Traitor"
    GUARDIAN_ANGEL = "GuardianAngel"
    DETECTIVE = "Detective"
    WOLF = "Wolf"
    CURSED = "Cursed"
    GUNNER = "Gunner"
    TANNER = "Tanner"
    FOOL = "Fool"
    WILD_CHILD = "WildChild"
    BEHOLDER = "Beholder"
    APPRENTICE_SEER = "ApprenticeSeer"
    CULTIST = "Cultist"
    CULTIST_HUNTER = "CultistHunter"
    MASON = "Mason"
    DOPPELGANGER = "Doppelgänger"
    CUPID = "Cupid"
    HUNTER = "Hunter"
    SERIAL_KILLER = "SerialKiller"
    SORCERER = "Sorcerer"
    ALPHA_WOLF = "AlphaWolf"
    WOLF_CUB = "WolfCub"
    BLACKSMITH = "Blacksmith"
    CLUMSY_GUY = "ClumsyGuy"
    MAYOR = "Mayor"
    PRINCE = "Prince"
    LYCAN = "Lycan"
    PACIFIST = "Pacifist"
    WISE_ELDER = "WiseElder"
    ORACLE = "Oracle"
    SANDMAN = "Sandman"
    WOLF_MAN = "WolfMan"
    THIEF = "Thief"
    TROUBLEMAKER = "Troublemaker"
    CHEMIST = "Chemist"
    SNOW_WOLF = "SnowWolf"
    GRAVE_DIGGER = "GraveDigger"
    AUGUR = "Augur"
    ARSONIST = "Arsonist"
    SPUMPKIN = "Spumpkin"


class Team(str, Enum):
    VILLAGE = "Village"
    CULT = "Cult"
    WOLF = "Wolf"
    TANNER = "Tanner"
    NEUTRAL = "Neutral"
    SERIAL_KILLER = "SerialKiller"
    LOVERS = "Lovers"
    ARSONIST = "Arsonist"
    SK_HUNTER = "SKHunter"
    NO_ONE = "NoOne"
    THIEF = "Thief"


@dataclass(frozen=True)
class RoleMetadata:
    role: Role
    emoji: str
    team: Team
    can_be_disabled: bool = True
    strength: int = 0
    night_action: str | None = None


# Source: Shared/Roles.cs at the pinned official snapshot.
_ROLE_ROWS: tuple[tuple[Role, str, Team, bool, int, str | None], ...] = (
    (Role.VILLAGER, "👱", Team.VILLAGE, False, 1, None),
    (Role.DRUNK, "🍻", Team.VILLAGE, True, 3, "drunk"),
    (Role.HARLOT, "💋", Team.VILLAGE, True, 6, "visit"),
    (Role.SEER, "👳", Team.VILLAGE, True, 7, "seer"),
    (Role.TRAITOR, "🖕", Team.VILLAGE, True, 0, None),
    (Role.GUARDIAN_ANGEL, "👼", Team.VILLAGE, True, 7, "guard"),
    (Role.DETECTIVE, "🕵", Team.VILLAGE, True, 6, "detect"),
    (Role.WOLF, "🐺", Team.WOLF, False, 10, "wolf"),
    (Role.CURSED, "😾", Team.VILLAGE, True, 1, None),
    (Role.GUNNER, "🔫", Team.VILLAGE, True, 6, "shoot"),
    (Role.TANNER, "👺", Team.TANNER, True, 0, None),
    (Role.FOOL, "🃏", Team.VILLAGE, True, 3, None),
    (Role.WILD_CHILD, "👶", Team.VILLAGE, True, 1, "idol"),
    (Role.BEHOLDER, "👁", Team.VILLAGE, True, 1, "behold"),
    (Role.APPRENTICE_SEER, "🙇", Team.VILLAGE, True, 6, "seer"),
    (Role.CULTIST, "👤", Team.CULT, True, 10, None),
    (Role.CULTIST_HUNTER, "💂", Team.VILLAGE, True, 7, "hunt_cult"),
    (Role.MASON, "👷", Team.VILLAGE, True, 1, None),
    (Role.DOPPELGANGER, "🎭", Team.THIEF, True, 2, "copy"),
    (Role.CUPID, "🏹", Team.VILLAGE, True, 2, "cupid"),
    (Role.HUNTER, "🎯", Team.VILLAGE, True, 6, "hunt"),
    (Role.SERIAL_KILLER, "🔪", Team.SERIAL_KILLER, True, 15, "serial_kill"),
    (Role.SORCERER, "🔮", Team.WOLF, True, 2, "sorcerer"),
    (Role.ALPHA_WOLF, "⚡️", Team.WOLF, True, 12, "alpha"),
    (Role.WOLF_CUB, "🐶", Team.WOLF, True, 10, "wolf"),
    (Role.BLACKSMITH, "⚒", Team.VILLAGE, True, 5, "blacksmith"),
    (Role.CLUMSY_GUY, "🤕", Team.VILLAGE, True, -1, None),
    (Role.MAYOR, "🎖", Team.VILLAGE, True, 4, None),
    (Role.PRINCE, "👑", Team.VILLAGE, True, 3, None),
    (Role.LYCAN, "🐺🌝", Team.WOLF, True, 10, None),
    (Role.PACIFIST, "☮️", Team.VILLAGE, True, 3, None),
    (Role.WISE_ELDER, "📚", Team.VILLAGE, True, 3, None),
    (Role.ORACLE, "🌀", Team.VILLAGE, True, 4, "oracle"),
    (Role.SANDMAN, "💤", Team.VILLAGE, True, 3, "sandman"),
    (Role.WOLF_MAN, "👱🌚", Team.VILLAGE, True, 1, None),
    (Role.THIEF, "😈", Team.THIEF, True, 0, "steal"),
    (Role.TROUBLEMAKER, "🤯", Team.VILLAGE, True, 5, "trouble"),
    (Role.CHEMIST, "👨‍🔬", Team.VILLAGE, True, 0, "chemistry"),
    (Role.SNOW_WOLF, "🐺☃️", Team.WOLF, True, 15, "snow_wolf"),
    (Role.GRAVE_DIGGER, "☠️", Team.VILLAGE, True, 5, "grave"),
    (Role.AUGUR, "🦅", Team.VILLAGE, True, 5, "augur"),
    (Role.ARSONIST, "🔥", Team.ARSONIST, True, 8, "arson"),
    (Role.SPUMPKIN, "🎃", Team.VILLAGE, False, 2, None),
)

ROLE_METADATA: dict[Role, RoleMetadata] = {row[0]: RoleMetadata(*row) for row in _ROLE_ROWS}
ROLE_TEAM: dict[Role, Team] = {role: metadata.team for role, metadata in ROLE_METADATA.items()}
ALL_ROLES: tuple[Role, ...] = tuple(ROLE_METADATA)
# Official Werewolf.cs / GameBalancing.WolfRoles. SnowWolf is wolf-team
# for majority and identity, but is not an eat-voting WolfRole.
WOLF_ROLES: frozenset[Role] = frozenset({
    Role.WOLF, Role.ALPHA_WOLF, Role.WOLF_CUB, Role.LYCAN,
})
MAJORITY_WOLF_ROLES: frozenset[Role] = WOLF_ROLES | {Role.SNOW_WOLF}

# Werewolf Node/Helpers/Settings.cs:130-137 —— 官方节点级硬上限，与群配置无关。
OFFICIAL_MAX_PLAYERS: int = 35
OFFICIAL_GAME_JOIN_TIME: int = 180
OFFICIAL_MAX_JOIN_TIME: int = 300

# The mapping mirrors the QuestionType selected in Werewolf.cs NightCycle.
# A role may still have a day ability; those are kept in DAY_ACTIONS below.
ROLE_ACTIONS: dict[Role, tuple[QuestionType, ...]] = {
    Role.DRUNK: (),
    Role.HARLOT: (QuestionType.VISIT,),
    Role.SEER: (QuestionType.SEE,),
    Role.TRAITOR: (),
    Role.GUARDIAN_ANGEL: (QuestionType.GUARD,),
    Role.DETECTIVE: (QuestionType.DETECT,),
    Role.WOLF: (QuestionType.KILL,),
    Role.CURSED: (),
    Role.GUNNER: (QuestionType.SHOOT,),
    Role.FOOL: (QuestionType.SEE,),
    Role.WILD_CHILD: (QuestionType.ROLE_MODEL,),
    Role.BEHOLDER: (),
    Role.APPRENTICE_SEER: (QuestionType.SEE,),
    Role.CULTIST: (QuestionType.CONVERT,),
    Role.CULTIST_HUNTER: (QuestionType.HUNT,),
    Role.DOPPELGANGER: (QuestionType.ROLE_MODEL,),
    Role.CUPID: (QuestionType.LOVER_1, QuestionType.LOVER_2),
    Role.HUNTER: (),
    Role.SERIAL_KILLER: (QuestionType.SERIAL_KILL,),
    Role.SORCERER: (QuestionType.SEE,),
    Role.ALPHA_WOLF: (QuestionType.KILL,),
    Role.WOLF_CUB: (QuestionType.KILL,),
    Role.BLACKSMITH: (QuestionType.SPREAD_SILVER,),
    Role.CLUMSY_GUY: (),
    Role.MAYOR: (QuestionType.MAYOR,),
    Role.PRINCE: (),
    Role.LYCAN: (QuestionType.KILL,),
    Role.PACIFIST: (QuestionType.PACIFIST,),
    Role.WISE_ELDER: (),
    Role.ORACLE: (QuestionType.SEE,),
    Role.SANDMAN: (QuestionType.SANDMAN,),
    Role.WOLF_MAN: (),
    Role.THIEF: (QuestionType.THIEF,),
    Role.TROUBLEMAKER: (QuestionType.TROUBLE,),
    Role.CHEMIST: (QuestionType.CHEMISTRY,),
    Role.SNOW_WOLF: (QuestionType.FREEZE,),
    # GraveDigger and Augur receive automatic night information.  They do not
    # select a player in the upstream NightCycle.
    Role.GRAVE_DIGGER: (),
    Role.AUGUR: (),
    Role.ARSONIST: (QuestionType.DOUSE,),
}

DAY_ACTIONS: frozenset[QuestionType] = frozenset({
    QuestionType.LYNCH, QuestionType.SHOOT, QuestionType.DETECT,
    QuestionType.MAYOR, QuestionType.SPREAD_SILVER, QuestionType.SANDMAN,
    QuestionType.PACIFIST, QuestionType.TROUBLE,
})


@dataclass(frozen=True)
class GameRules:
    name: str
    min_players: int
    max_players: int
    join_seconds: int
    night_seconds: int
    day_seconds: int
    vote_seconds: int
    allow_vote_change: bool = False
    allow_vote_skip: bool = True
    allow_flee: bool = True
    allow_extend: bool = False
    max_extend: int = 60
    random_mode: bool = False
    random_lynch: bool = False
    secret_lynch: bool = False
    secret_lynch_show_votes: bool = False
    secret_lynch_show_voters: bool = False
    show_roles_on_death: bool = True
    show_roles_end: str = "Living"
    show_ids: bool = False
    shuffle_player_list: bool = False
    allow_nsfw: bool = False
    role_version: str = "official-ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7"
    mode: GameMode = GameMode.NORMAL
    disabled_roles: tuple[str, ...] = ()
    required_roles: tuple[str, ...] = ()
    burning_overkill: bool = False
    thief_full: bool = False
    allow_arsonist: bool = True
    detective_caught_chance: int = 40
    hunter_kill_wolf_chance_base: int = 30
    alpha_wolf_conversion_chance: int = 20
    hunter_conversion_chance: int = 50
    hunter_kill_cult_chance: int = 50
    thief_steal_chance: int = 50
    chemist_success_chance: int = 50
    harlot_discover_cult_chance: int = 50
    spumpkin_detonation_chance: int = 40
    guardian_wolf_death_chance: int = 50
    snow_hunter_freeze_chance: int = 50
    serial_killer_stumble_chance: int = 80
    clumsy_retarget_chance: int = 50
    seer_traitor_wolf_chance: int = 50
    grave_digger_fall_chance: int = -1
    grave_digger_spot_chance: int = -1
    cult_conversion_chances: tuple[tuple[str, int], ...] = (
        (Role.SEER.value, 40),
        (Role.GUARDIAN_ANGEL.value, 60),
        (Role.DETECTIVE.value, 70),
        (Role.CURSED.value, 60),
        (Role.HARLOT.value, 70),
        (Role.HUNTER.value, 50),
        (Role.SORCERER.value, 40),
        (Role.BLACKSMITH.value, 75),
        (Role.ORACLE.value, 50),
        (Role.SANDMAN.value, 60),
        (Role.WISE_ELDER.value, 30),
        (Role.PACIFIST.value, 80),
        (Role.GRAVE_DIGGER.value, 30),
        (Role.AUGUR.value, 40),
        (Role.DOPPELGANGER.value, 0),
        (Role.THIEF.value, 0),
        (Role.SPUMPKIN.value, 0),
    )
    source_commit: str = "ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7"

    def cult_conversion_chance(self, role: Role) -> int:
        return dict(self.cult_conversion_chances).get(role.value, 100)

    def validate(self) -> None:
        # 官方正式构建 MinPlayers=5；DEBUG 构建为 1。测试开局允许降到 1。
        if self.min_players < 1 or self.max_players < self.min_players or self.max_players > 35:
            raise ValueError("规则人数范围无效")
        if any(value <= 0 for value in (
            self.join_seconds, self.night_seconds, self.day_seconds, self.vote_seconds
        )):
            raise ValueError("阶段时长必须为正数")
        if self.max_extend <= 0:
            raise ValueError("最大延长时间必须为正数")
        if any(not 0 <= value <= 100 for value in (
            self.detective_caught_chance,
            self.hunter_kill_wolf_chance_base,
            self.alpha_wolf_conversion_chance,
            self.hunter_conversion_chance,
            self.hunter_kill_cult_chance,
            self.thief_steal_chance,
            self.chemist_success_chance,
            self.harlot_discover_cult_chance,
            self.spumpkin_detonation_chance,
            self.guardian_wolf_death_chance,
            self.snow_hunter_freeze_chance,
            self.serial_killer_stumble_chance,
            self.clumsy_retarget_chance,
            self.seer_traitor_wolf_chance,
        )):
            raise ValueError("角色概率必须介于 0 到 100")
        if any(not 0 <= value <= 100 for _, value in self.cult_conversion_chances):
            raise ValueError("教会转化概率必须介于 0 到 100")
        if self.show_roles_end not in {"None", "Living", "All"}:
            raise ValueError("结算身份显示只能是 None、Living 或 All")
        for value in (*self.disabled_roles, *self.required_roles):
            Role(value)
        if set(self.disabled_roles) & set(self.required_roles):
            raise ValueError("角色不能同时禁用和必选")
        if not self.allow_arsonist and Role.ARSONIST.value in self.required_roles:
            raise ValueError("关闭纵火者时不能将纵火者设为必选")


@dataclass
class Player:
    user_id: str
    display_name: str
    seat: int

    @property
    def public_name(self) -> str:
        if is_opaque_display_name(self.display_name):
            return f"{self.seat}号"
        return self.display_name

    @property
    def list_label(self) -> str:
        if is_opaque_display_name(self.display_name):
            return f"{self.seat}号"
        return f"{self.seat}号 {self.display_name}"

    role: Role | None = None
    alive: bool = True
    original_role: Role | None = None
    lover_id: str | None = None
    role_model: str | None = None
    changed_roles_count: int = 0
    has_used_ability: bool = False
    won: bool = False
    fled: bool = False
    frozen: bool = False
    bullet_count: int = 2
    drunk: bool = False
    day_cult: int = 0
    killed_by: str | None = None
    kill_method: str | None = None
    choice: str | None = None
    choice2: str | None = None
    non_vote_count: int = 0
    has_been_voted: bool = False
    was_saved_last_night: bool = False
    bitten: bool = False
    doused: bool = False
    burning: bool = False
    died_last_night: bool = False
    vote_weight: int = 1
    votes_received: int = 0
    has_revealed: bool = False
    is_frozen: bool = False
    action_day: int | None = None
    # 以下字段对应官方 IPlayer 的成就统计字段（Werewolf Node/Models/Player.cs），
    # 因为 _assign_roles 会整体重置 metadata，所以必须是真实字段而非 metadata 键。
    achievements: list[int] = field(default_factory=list)
    clumsy_correct_lynch_count: int = 0
    first_stone: int = 0
    cult_leader: bool = False
    converted_to_cult: bool = False
    speed_dating: bool = False
    alpha_convert_count: int = 0
    mayor_lynch_after_reveal_count: int = 0
    bullet_hit_baddies: int = 0
    froze_harlot: bool = False
    serial_killed_wolves_count: int = 0
    ch_hunted_cult_count: int = 0
    chemist_visit_survive_count: int = 0
    players_visited: list[str] = field(default_factory=list)
    has_repeated_visit: bool = False
    has_stayed_home: bool = False
    trustworthy: bool = False
    fool_correct_see_count: int = 0
    fool_correctly_seen_bh: bool = False
    has_seen_impossible: bool = False
    ga_guard_wolf_count: int = 0
    has_cleaned_doused: int = 0
    busy_night: bool = False
    being_visited_same_night_count: int = 0
    killed_last_night: int = 0
    has_shot_hunter_attacker: int = 0
    has_shot_hunter_attacker_this_night: bool = False
    correct_snooped: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def team(self) -> Team | None:
        return ROLE_TEAM.get(self.role) if self.role else None


@dataclass(frozen=True)
class DomainEvent:
    kind: str
    text: str
    public: bool = True
    target_user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NightAction:
    actor_id: str
    action: str
    target_id: str | None
    day: int
    second_target_id: str | None = None
    request_id: str | None = None


@dataclass
class GameRoom:
    session_id: str
    rules: GameRules
    phase: GamePhase = GamePhase.LOBBY
    players: list[Player] = field(default_factory=list)
    host_user_id: str | None = None
    day: int = 0
    winner: Team | None = None
    winners: tuple[str, ...] = ()
    stage_started_at: datetime = field(default_factory=utc_now)
    stage_deadline: datetime | None = None
    day_actions: dict[str, NightAction] = field(default_factory=dict)
    night_actions: dict[str, NightAction] = field(default_factory=dict)
    votes: dict[str, str | None] = field(default_factory=dict)
    vote_round: int = 1
    vote_history: list[dict[str, Any]] = field(default_factory=list)
    event_history: list[dict[str, Any]] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)
    settled_boundaries: list[str] = field(default_factory=list)
    state_version: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "rules": {**asdict(self.rules), "mode": self.rules.mode.value},
            "phase": self.phase.value,
            "players": [
                {
                    **asdict(p),
                    "role": p.role.value if p.role else None,
                    "original_role": p.original_role.value if p.original_role else None,
                }
                for p in self.players
            ],
            "host_user_id": self.host_user_id,
            "day": self.day,
            "winner": self.winner.value if self.winner else None,
            "winners": list(self.winners),
            "stage_started_at": self.stage_started_at.isoformat(),
            "stage_deadline": self.stage_deadline.isoformat() if self.stage_deadline else None,
            "day_actions": {key: asdict(action) for key, action in self.day_actions.items()},
            "night_actions": {key: asdict(action) for key, action in self.night_actions.items()},
            "votes": self.votes,
            "vote_round": self.vote_round,
            "vote_history": self.vote_history,
            "event_history": self.event_history,
            "statistics": self.statistics,
            "settled_boundaries": self.settled_boundaries,
            "state_version": self.state_version,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> GameRoom:
        rule_data = dict(data["rules"])
        rule_data["mode"] = GameMode(rule_data.get("mode", GameMode.NORMAL.value))
        rule_data.setdefault("role_version", "official-ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7")
        rule_data.setdefault("disabled_roles", ())
        rule_data.setdefault("required_roles", ())
        rule_data.setdefault("burning_overkill", False)
        rule_data.setdefault("allow_flee", True)
        rule_data.setdefault("allow_extend", False)
        rule_data.setdefault("max_extend", 60)
        rule_data.setdefault("allow_arsonist", True)
        rule_data.setdefault("random_lynch", False)
        rule_data.setdefault("secret_lynch", False)
        rule_data.setdefault("secret_lynch_show_votes", False)
        rule_data.setdefault("secret_lynch_show_voters", False)
        rule_data.setdefault("show_roles_on_death", True)
        rule_data.setdefault("show_roles_end", "Living")
        rule_data.setdefault("show_ids", False)
        rule_data.setdefault("shuffle_player_list", False)
        rule_data.setdefault("allow_nsfw", False)
        rule_data.setdefault("source_commit", "ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7")
        for key, value in {
            "guardian_wolf_death_chance": 50,
            "snow_hunter_freeze_chance": 50,
            "serial_killer_stumble_chance": 80,
            "clumsy_retarget_chance": 50,
            "seer_traitor_wolf_chance": 50,
            "grave_digger_fall_chance": -1,
            "grave_digger_spot_chance": -1,
        }.items():
            rule_data.setdefault(key, value)
        room = cls(
            session_id=str(data["session_id"]),
            rules=GameRules(**rule_data),
            phase=GamePhase(data["phase"]),
            host_user_id=data.get("host_user_id"),
            day=int(data.get("day", 0)),
            winner=Team(data["winner"]) if data.get("winner") else None,
            winners=tuple(str(item) for item in data.get("winners", ())),
            stage_started_at=datetime.fromisoformat(data["stage_started_at"]),
            stage_deadline=(datetime.fromisoformat(data["stage_deadline"])
                            if data.get("stage_deadline") else None),
            votes={str(k): v for k, v in data.get("votes", {}).items()},
            vote_round=int(data.get("vote_round", 1)),
            vote_history=list(data.get("vote_history", [])),
            event_history=list(data.get("event_history", [])),
            statistics=dict(data.get("statistics", {})),
            settled_boundaries=[str(item) for item in data.get("settled_boundaries", [])],
            state_version=int(data.get("state_version", 0)),
        )
        room.players = [
            Player(
                user_id=str(item["user_id"]), display_name=str(item["display_name"]),
                seat=int(item["seat"]), role=Role(item["role"]) if item.get("role") else None,
                alive=bool(item.get("alive", True)),
                original_role=Role(item["original_role"]) if item.get("original_role") else None,
                lover_id=item.get("lover_id"), role_model=item.get("role_model"),
                changed_roles_count=int(item.get("changed_roles_count", 0)),
                has_used_ability=bool(item.get("has_used_ability", False)),
                won=bool(item.get("won", False)), fled=bool(item.get("fled", False)),
                frozen=bool(item.get("frozen", False)), bullet_count=int(item.get("bullet_count", 2)),
                drunk=bool(item.get("drunk", False)), day_cult=int(item.get("day_cult", 0)),
                killed_by=item.get("killed_by"), kill_method=item.get("kill_method"),
                choice=item.get("choice"), choice2=item.get("choice2"),
                non_vote_count=int(item.get("non_vote_count", 0)),
                has_been_voted=bool(item.get("has_been_voted", False)),
                was_saved_last_night=bool(item.get("was_saved_last_night", False)),
                bitten=bool(item.get("bitten", False)), doused=bool(item.get("doused", False)),
                burning=bool(item.get("burning", False)),
                died_last_night=bool(item.get("died_last_night", False)),
                vote_weight=int(item.get("vote_weight", 1)),
                votes_received=int(item.get("votes_received", 0)),
                has_revealed=bool(item.get("has_revealed", False)),
                is_frozen=bool(item.get("is_frozen", False)),
                action_day=(int(item["action_day"]) if item.get("action_day") is not None else None),
                achievements=[int(a) for a in item.get("achievements", [])],
                clumsy_correct_lynch_count=int(item.get("clumsy_correct_lynch_count", 0)),
                first_stone=int(item.get("first_stone", 0)),
                cult_leader=bool(item.get("cult_leader", False)),
                converted_to_cult=bool(item.get("converted_to_cult", False)),
                speed_dating=bool(item.get("speed_dating", False)),
                alpha_convert_count=int(item.get("alpha_convert_count", 0)),
                mayor_lynch_after_reveal_count=int(item.get("mayor_lynch_after_reveal_count", 0)),
                bullet_hit_baddies=int(item.get("bullet_hit_baddies", 0)),
                froze_harlot=bool(item.get("froze_harlot", False)),
                serial_killed_wolves_count=int(item.get("serial_killed_wolves_count", 0)),
                ch_hunted_cult_count=int(item.get("ch_hunted_cult_count", 0)),
                chemist_visit_survive_count=int(item.get("chemist_visit_survive_count", 0)),
                players_visited=[str(v) for v in item.get("players_visited", [])],
                has_repeated_visit=bool(item.get("has_repeated_visit", False)),
                has_stayed_home=bool(item.get("has_stayed_home", False)),
                trustworthy=bool(item.get("trustworthy", False)),
                fool_correct_see_count=int(item.get("fool_correct_see_count", 0)),
                fool_correctly_seen_bh=bool(item.get("fool_correctly_seen_bh", False)),
                has_seen_impossible=bool(item.get("has_seen_impossible", False)),
                ga_guard_wolf_count=int(item.get("ga_guard_wolf_count", 0)),
                has_cleaned_doused=int(item.get("has_cleaned_doused", 0)),
                busy_night=bool(item.get("busy_night", False)),
                being_visited_same_night_count=int(item.get("being_visited_same_night_count", 0)),
                killed_last_night=int(item.get("killed_last_night", 0)),
                has_shot_hunter_attacker=int(item.get("has_shot_hunter_attacker", 0)),
                has_shot_hunter_attacker_this_night=bool(
                    item.get("has_shot_hunter_attacker_this_night", False)),
                correct_snooped=[str(v) for v in item.get("correct_snooped", [])],
                metadata=dict(item.get("metadata", {})),
            ) for item in data.get("players", [])
        ]
        room.night_actions = {
            str(key): NightAction(
                actor_id=str(value["actor_id"]), action=str(value["action"]),
                target_id=value.get("target_id"), day=int(value["day"]),
                second_target_id=value.get("second_target_id"),
                request_id=value.get("request_id"),
            ) for key, value in data.get("night_actions", {}).items()
        }
        room.day_actions = {
            str(key): NightAction(
                actor_id=str(value["actor_id"]), action=str(value["action"]),
                target_id=value.get("target_id"), day=int(value["day"]),
                second_target_id=value.get("second_target_id"),
                request_id=value.get("request_id"),
            ) for key, value in data.get("day_actions", {}).items()
        }
        return room


PlayerIdFactory = Callable[[int], str]
