from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable

from .models import ALL_ROLES, ROLE_METADATA, WOLF_ROLES, GameMode, GameRules, RandomSource, Role


OFFICIAL_COMMIT = "ca547ccb0ed01e6f282f9e8e71f7a24d547b24d7"
OFFICIAL_ROLE_SOURCE = "Shared/Roles.cs"
OFFICIAL_BALANCE_SOURCE = "Shared/GameBalancing.cs"
# `GameBalancing.WolfRoles` deliberately excludes SnowWolf. The card can be
# drawn, but a lone SnowWolf is replaced with one of these ordinary wolf roles.
BALANCE_WOLF_ROLES: tuple[Role, ...] = (
    Role.WOLF,
    Role.ALPHA_WOLF,
    Role.WOLF_CUB,
    Role.LYCAN,
)


@dataclass(frozen=True)
class RoleAssignment:
    roles: tuple[Role, ...]
    possible_roles: tuple[Role, ...]
    mode: GameMode
    source_commit: str = OFFICIAL_COMMIT


def _as_role_set(values: Iterable[str | Role]) -> set[Role]:
    return {value if isinstance(value, Role) else Role(value) for value in values}


def role_strength(role: Role, all_roles: list[Role]) -> int:
    """移植官方 Normal 平衡算法所用的 GetStrength 强度表。"""
    wolf_count = sum(item in WOLF_ROLES or item == Role.SNOW_WOLF for item in all_roles)
    if role == Role.SEER:
        return 7 - all_roles.count(Role.LYCAN) - all_roles.count(Role.WOLF_MAN) * 2
    if role == Role.GUARDIAN_ANGEL:
        return 7 + int(Role.ARSONIST in all_roles)
    if role == Role.CURSED:
        return 1 - wolf_count // 2
    if role == Role.BEHOLDER:
        return 1 + (4 if Role.SEER in all_roles else 0) + (1 if Role.FOOL in all_roles else 0)
    if role == Role.CULTIST:
        non_convertible = {
            Role.SEER, Role.GUARDIAN_ANGEL, Role.DETECTIVE, Role.CURSED, Role.HARLOT,
            Role.HUNTER, Role.DOPPELGANGER, Role.WOLF, Role.ALPHA_WOLF, Role.WOLF_CUB,
            Role.SERIAL_KILLER, Role.LYCAN, Role.THIEF, Role.SNOW_WOLF,
        }
        return 10 + sum(item not in non_convertible for item in all_roles)
    if role == Role.CULTIST_HUNTER:
        return 1 if Role.CULTIST not in all_roles else 7
    if role == Role.MASON:
        count = all_roles.count(Role.MASON)
        return 1 if count <= 1 else count + 3
    if role == Role.WOLF_CUB:
        return 12 if any(item in all_roles for item in {
            Role.ALPHA_WOLF, Role.WOLF, Role.LYCAN, Role.SNOW_WOLF,
            Role.WILD_CHILD, Role.DOPPELGANGER, Role.CURSED, Role.TRAITOR,
        }) else 10
    if role == Role.TANNER:
        return len(all_roles) // 2
    return ROLE_METADATA[role].strength


def official_role_list(
    player_count: int,
    disabled_roles: Iterable[str | Role] = (),
    rng: RandomSource | None = None,
) -> list[Role]:
    """移植 GetRoleList：最终随机抽取之前的身份牌池。"""
    if player_count < 1:
        raise ValueError("玩家数必须为正数")
    rng = rng or random.Random()
    disabled = _as_role_set(disabled_roles)
    possible_wolves = [
        role for role in (Role.WOLF, Role.ALPHA_WOLF, Role.WOLF_CUB, Role.LYCAN, Role.SNOW_WOLF)
        if role not in disabled
    ]
    if not possible_wolves:
        raise ValueError("至少需要保留一个可用狼人角色")
    result: list[Role] = []
    wolf_role = rng.choice(possible_wolves)
    for _ in range(min(max(player_count // 5, 1), 5)):
        result.append(wolf_role)
        if wolf_role != Role.WOLF:
            possible_wolves.remove(wolf_role)
        wolf_role = rng.choice(possible_wolves or [Role.WOLF])
    if len(result) == 1 and result[0] == Role.SNOW_WOLF:
        result[0] = Role.WOLF
    for role in ALL_ROLES:
        if role in WOLF_ROLES or role in {Role.SPUMPKIN, Role.SNOW_WOLF}:
            continue
        if role in {Role.CULTIST, Role.CULTIST_HUNTER} and player_count <= 10:
            continue
        result.append(role)
    result.extend([Role.MASON, Role.MASON])
    if Role.CULTIST_HUNTER in result:
        result.extend([Role.CULTIST, Role.CULTIST])
    result.extend([Role.VILLAGER] * (player_count // 4))
    return [role for role in result if role not in disabled]


def assign_official_roles(
    player_count: int,
    *,
    mode: GameMode = GameMode.NORMAL,
    disabled_roles: Iterable[str | Role] = (),
    required_roles: Iterable[str | Role] = (),
    burning_overkill: bool = False,
    allow_arsonist: bool = True,
    rng: RandomSource | None = None,
) -> RoleAssignment:
    """生成一份与官方一致的身份分配结果，并保留本次使用的牌池。"""
    rng = rng or random.Random()
    disabled = _as_role_set(disabled_roles)
    required = _as_role_set(required_roles)
    if not allow_arsonist:
        disabled.add(Role.ARSONIST)
    if disabled & required:
        raise ValueError("角色不能同时禁用和必选")
    for role in disabled:
        if not ROLE_METADATA[role].can_be_disabled:
            raise ValueError(f"官方角色不可禁用: {role.value}")
    for _ in range(500):
        pool = official_role_list(player_count, disabled, rng)
        while len(pool) < player_count:
            pool.append(Role.VILLAGER)
        possible = list(pool)
        rng.shuffle(pool)
        roles = pool[:player_count]
        bad_roles = {Role.SORCERER, Role.TRAITOR, Role.SNOW_WOLF}
        if bad_roles & set(roles) and not any(role in roles for role in BALANCE_WOLF_ROLES):
            candidates = [role for role in roles if role in bad_roles]
            wolves = [role for role in BALANCE_WOLF_ROLES if role not in disabled]
            roles[roles.index(candidates[0])] = rng.choice(wolves)
        if Role.CULTIST in roles and Role.CULTIST_HUNTER not in roles and Role.CULTIST_HUNTER not in disabled:
            village_roles = {
                Role.CULTIST, Role.SERIAL_KILLER, Role.TANNER, Role.WOLF, Role.ALPHA_WOLF,
                Role.SORCERER, Role.WOLF_CUB, Role.LYCAN, Role.THIEF, Role.SNOW_WOLF, Role.ARSONIST,
            }
            index = next((i for i, role in enumerate(roles) if role not in village_roles), None)
            if index is not None:
                roles[index] = Role.CULTIST_HUNTER
        if Role.APPRENTICE_SEER in roles and Role.SEER not in roles:
            roles[roles.index(Role.APPRENTICE_SEER)] = Role.SEER
        if required and not required.issubset(set(roles)):
            continue
        non_village = {
            Role.CULTIST, Role.SERIAL_KILLER, Role.TANNER, Role.WOLF, Role.ALPHA_WOLF,
            Role.SORCERER, Role.WOLF_CUB, Role.LYCAN, Role.THIEF, Role.SNOW_WOLF, Role.ARSONIST,
        }
        if not any(role not in non_village for role in roles):
            continue
        if not any(role in non_village and role not in {Role.SORCERER, Role.TANNER, Role.THIEF} for role in roles):
            continue
        if sum(role in non_village for role in roles) >= sum(role not in non_village for role in roles):
            continue
        if Role.SERIAL_KILLER in roles and Role.ARSONIST in roles and not burning_overkill:
            continue
        if mode == GameMode.NORMAL and player_count >= 5:
            village_strength = sum(role_strength(role, roles) for role in roles if role not in non_village)
            enemy_strength = sum(role_strength(role, roles) for role in roles if role in non_village)
            if abs(village_strength - enemy_strength) > player_count // 4 + 1:
                continue
            revealed = {Role.BLACKSMITH, Role.MAYOR, Role.PACIFIST, Role.GUNNER, Role.SANDMAN, Role.TROUBLEMAKER}
            if sum(role in revealed for role in roles) * 3 > len(roles):
                continue
            blockers = {Role.TROUBLEMAKER, Role.SANDMAN}
            if not ({Role.ARSONIST, Role.SERIAL_KILLER, Role.CULTIST} & set(roles)):
                blockers.add(Role.BLACKSMITH)
            if sum(role in blockers for role in roles) * 4 > len(roles):
                continue
        return RoleAssignment(tuple(roles), tuple(possible), mode)
    raise ValueError(f"无法按官方规则生成平衡配置：玩家数 {player_count}")


def try_balance(
    disabled_roles: Iterable[str | Role] = (),
    max_players: int = 35,
    rng: RandomSource | None = None,
) -> bool:
    """Port GameBalancing.TryBalance —— 5 至 MaxPlayers 全部能配平才算有效。"""
    rng = rng or random.Random()
    for player_count in range(5, max_players + 1):
        try:
            assign_official_roles(
                player_count,
                mode=GameMode.NORMAL,
                disabled_roles=disabled_roles,
                burning_overkill=True,
                rng=rng,
            )
        except ValueError:
            return False
    return True


def ruleset_official(
    *, mode: GameMode = GameMode.NORMAL, min_players: int = 5, max_players: int = 35,
    join_seconds: int = 180,
    night_seconds: int = 90, day_seconds: int = 60, vote_seconds: int = 90,
    disabled_roles: Iterable[str | Role] = (), required_roles: Iterable[str | Role] = (),
    burning_overkill: bool = False, thief_full: bool = False, allow_arsonist: bool = True,
    random_lynch: bool = False, secret_lynch: bool = False,
    secret_lynch_show_votes: bool = False, secret_lynch_show_voters: bool = False,
    show_roles_on_death: bool = True, show_roles_end: str = "Living",
    allow_flee: bool = True, allow_extend: bool = False, max_extend: int = 60,
    random_mode: bool = False, show_ids: bool = False,
    shuffle_player_list: bool = False, allow_nsfw: bool = False,
) -> GameRules:
    disabled = tuple(role.value for role in _as_role_set(disabled_roles))
    required = tuple(role.value for role in _as_role_set(required_roles))
    rules = GameRules(
        name=f"official-{mode.value}", mode=mode, min_players=min_players, max_players=max_players,
        join_seconds=join_seconds, night_seconds=night_seconds, day_seconds=day_seconds,
        vote_seconds=vote_seconds, role_version=f"official-{OFFICIAL_COMMIT}",
        disabled_roles=disabled, required_roles=required, burning_overkill=burning_overkill,
        thief_full=thief_full, allow_arsonist=allow_arsonist, random_lynch=random_lynch,
        secret_lynch=secret_lynch, secret_lynch_show_votes=secret_lynch_show_votes,
        secret_lynch_show_voters=secret_lynch_show_voters,
        show_roles_on_death=show_roles_on_death, show_roles_end=show_roles_end,
        allow_flee=allow_flee, allow_extend=allow_extend, max_extend=max_extend,
        random_mode=random_mode,
        show_ids=show_ids, shuffle_player_list=shuffle_player_list,
        allow_nsfw=allow_nsfw,
        source_commit=OFFICIAL_COMMIT,
    )
    rules.validate()
    return rules


def ruleset_v1(**kwargs: int) -> GameRules:
    """兼容用的旧名字，现在固定返回锁定的官方 Normal 规则。"""
    return ruleset_official(**kwargs)


def wolf_count(player_count: int) -> int:
    return min(max(player_count // 5, 1), 5)
