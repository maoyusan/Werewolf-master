"""成就定义，等价迁移自官方 Database/AchievementsReworked.cs。

官方使用 BitArray(200) 存储，位下标等于枚举整数值；本移植保持同样的整数值，
以便与官方数据、成就编号完全一致。名称与描述一一对应官方 Display(Name/Description)，
但按本项目「仅简体中文」的要求做了本地化，角色称呼统一采用简体中文语言包里的译名。
"""

from __future__ import annotations

from enum import IntEnum


class Achievement(IntEnum):
    NONE = 0
    WELCOME_TO_HELL = 1
    WELCOME_TO_ASYLUM = 2
    ALZHEIMER_PATIENT = 3
    OHAIDER = 4
    SPY_VS_SPY = 5
    EXPLORER = 6
    LINGUIST = 7
    NO_IDEA_WHAT = 8
    ENOCHLOPHOBIA = 9
    INTROVERT = 10
    NAUGHTY = 11
    DEDICATED = 12
    OBSESSED = 13
    HERE_JOHNNY = 14
    GOT_YOUR_BACK = 15
    MASOCHIST = 16
    WOBBLE = 17
    INCONSPICUOUS = 18
    SURVIVALIST = 19
    BLACK_SHEEP = 20
    PROMISCUOUS = 21
    MASON_BROTHER = 22
    DOUBLE_SHIFTER = 23
    HEY_MAN_NICE_SHOT = 24
    DONT_STAY_HOME = 25
    DOUBLE_VISION = 26
    DOUBLE_KILL = 27
    SHOULD_HAVE_KNOWN = 28
    LACK_OF_TRUST = 29
    BLOODY_NIGHT = 30
    CHANGING_SIDES = 31
    FORBIDDEN_LOVE = 32
    DEVELOPER = 33
    FIRST_STONE = 34
    SMART_GUNNER = 35
    STREETWISE = 36
    ONLINE_DATING = 37
    BROKEN_CLOCK = 38
    SO_CLOSE = 39
    CULT_CON = 40
    SELF_LOVING = 41
    SHOULDVE_MENTIONED = 42
    TANNER_OVERKILL = 43
    SERIAL_SAMARITAN = 44
    CULT_FODDER = 45
    LONE_WOLF = 46
    PACK_HUNTER = 47
    GUNNER_SAVES = 48
    LONG_HAUL = 49
    OH_SHI = 50
    VETERAN = 51
    NO_SORCERY = 52
    CULTIST_TRACKER = 53
    IM_NOT_DRUNK = 54
    WUFFIE_CULT = 55
    DID_YOU_GUARD_YOURSELF = 56
    SPOILED_RICH_BRAT = 57
    THREE_LITTLE_WOLVES = 58
    PRESIDENT = 59
    I_HELPED = 60
    IT_WAS_A_BUSY_NIGHT = 61
    STRONGEST_ALPHA = 62
    AM_I_YOUR_SEER = 63
    DEMOTED_BY_THE_DEATH = 64
    WASTED_SILVER = 65
    TRUSTWORTHY = 66
    DEEP_LOVE = 67
    TIME_TO_RETIRE = 68
    SEEING_BETWEEN_TEAMS = 69
    JUST_A_BEARDY_GUY = 70
    THAT_CAME_UNEXPECTED = 71
    NOW_IM_BLIND = 72
    EVERY_MAN_FOR_HIMSELF = 73
    MY_SWEETIE_SO_STRONG = 74
    CULT_LEADER = 75
    THANKS_JUNIOR = 76
    DEATH_VILLAGE = 77
    I_LOST_MY_WISDOM = 78
    AFFECTIONATE = 79
    LUCKY_DAY = 80
    CONDITION_RED = 81
    INDESTRUCTIBLE = 82
    PSYCHOPATH_KILLER = 83
    TODAYS_SPECIAL = 84
    ROMEO_AND_JULIET = 85
    REALLY_BAD_LUCK = 86
    DOMINO = 87
    DOUBLE_SHOT = 88
    PLAYING_WITH_THE_FIRE = 89
    FIREWORK = 90
    COLD_AS_ICE = 91
    GOOD_CHOICE_FOR_YOU = 92
    INCREASE_THE_PACK = 93
    FIREFIGHTER = 94
    HELPFUL_PARANOIA = 95
    S_TIER_HUNTER = 96
    TRIPLE_KILL = 97
    RESIST_THE_BEAST = 98
    AT_LEAST_YOU_TRIED = 99
    LUCKY_NIGHT = 100
    IN_THE_MIDDLE_OF_THE_TROUBLE = 101
    AM_I_HALLUCINATING = 102


# 一一对应官方 [Display(Name = ..., Description = ...)]，值为 (成就名, 达成条件)。
# 文案已本地化为简体中文；角色称呼取自 languages/SimplifiedChinese.xml，
# 例如 seer=先知、harlot=秘密警察、guardian angel=守卫、serial killer=变态杀人狂。
ACHIEVEMENT_INFO: dict[Achievement, tuple[str, str]] = {
    Achievement.NONE: ("无", "你还没有玩过任何一局游戏！"),
    Achievement.WELCOME_TO_HELL: ("欢迎来到地狱", "参与一局游戏"),
    Achievement.WELCOME_TO_ASYLUM: ("欢迎来到疯人院", "参与一局混乱模式的游戏"),
    Achievement.ALZHEIMER_PATIENT: (
        "阿尔茨海默病人", "使用失忆语言包玩一局游戏"),
    Achievement.OHAIDER: (
        "哟，你也在啊！", "和 Para 的小号（不是 @para949）同场玩一局"),
    Achievement.SPY_VS_SPY: ("谍中谍", "参与一局暗牌模式的游戏（死亡不公开身份）"),
    Achievement.EXPLORER: ("探险家", "在 10 个不同的群里各玩至少 2 局"),
    Achievement.LINGUIST: (
        "语言学家", "用 10 种不同的语言包各玩至少 2 局"),
    Achievement.NO_IDEA_WHAT: (
        "我完全不知道自己在干嘛", "在暗牌失忆模式下玩一局游戏"),
    Achievement.ENOCHLOPHOBIA: ("人群恐惧症", "参与一局 35 人的游戏"),
    Achievement.INTROVERT: ("社恐", "参与一局 5 人的游戏"),
    Achievement.NAUGHTY: ("不学好！", "使用任意成人语言包玩一局游戏"),
    Achievement.DEDICATED: ("铁杆玩家", "累计参与 100 局游戏"),
    Achievement.OBSESSED: ("走火入魔", "累计参与 1000 局游戏"),
    Achievement.HERE_JOHNNY: ("闪灵附体", "以变态杀人狂身份累计击杀 50 人"),
    Achievement.GOT_YOUR_BACK: (
        "有我罩着你", "以守卫身份累计救下 50 人"),
    Achievement.MASOCHIST: ("受虐狂", "以圣战者身份赢下一局游戏"),
    Achievement.WOBBLE: (
        "走位飘忽", "在至少 10 人的游戏里以酗酒艺术家身份活到终局"),
    Achievement.INCONSPICUOUS: (
        "查无此人",
        "在 20 人及以上的游戏里，一张放逐票都没被投到（并且活到终局）"),
    Achievement.SURVIVALIST: ("生存专家", "累计 100 局活到终局"),
    Achievement.BLACK_SHEEP: ("众矢之的", "连续 3 局都成为第一个被放逐的人"),
    Achievement.PROMISCUOUS: (
        "雨露均沾",
        "以秘密警察身份在 5 夜以上的游戏里活到终局，期间既不留在家中，也不重复查同一个人"),
    Achievement.MASON_BROTHER: (
        "好兄弟", "终局时至少有两名拖延症患者存活，你是其中之一"),
    Achievement.DOUBLE_SHIFTER: (
        "连轴转",
        "在同一局游戏里变换身份两次（邪教转化不算）"),
    Achievement.HEY_MAN_NICE_SHOT: (
        "好枪法！",
        "以猎人身份用临终一枪打死狼人或变态杀人狂"),
    Achievement.DONT_STAY_HOME: (
        "叫你别宅在家里",
        "以狼人或邪教徒身份，杀死或转化一个当晚待在家中的秘密警察"),
    Achievement.DOUBLE_VISION: ("双重视野", "与另一名先知同时在场"),
    Achievement.DOUBLE_KILL: (
        "双杀", "参与变态杀人狂与猎人同归于尽的结局"),
    Achievement.SHOULD_HAVE_KNOWN: (
        "早该想到", "以先知身份查验到无神论者"),
    Achievement.LACK_OF_TRUST: (
        "信任呢？", "以先知身份在第一天就被放逐"),
    Achievement.BLOODY_NIGHT: (
        "血色之夜",
        "同一个夜晚至少死了 4 个人，你是其中之一"),
    Achievement.CHANGING_SIDES: (
        "跳槽真香", "在一局游戏里变换了身份，并最终获胜"),
    Achievement.FORBIDDEN_LOVE: (
        "禁忌之恋",
        "以狼人与普通村民的情侣组合获胜（必须是普通村民，而非村民阵营的其他角色）"),
    Achievement.DEVELOPER: ("开发者", "有一个 Pull Request 被合并进本项目仓库"),
    Achievement.FIRST_STONE: (
        "第一块石头", "在同一局游戏里 5 次第一个投出放逐票"),
    Achievement.SMART_GUNNER: (
        "神枪手",
        "以枪手身份，两颗子弹全部命中狼人、变态杀人狂或邪教徒"),
    Achievement.STREETWISE: (
        "老江湖",
        "以侦探身份连续 4 晚各查出一个不同的狼人、变态杀人狂、纵火狂或邪教徒"),
    Achievement.ONLINE_DATING: (
        "闪电相亲", "爱神没能选人，由机器人把你指定成了恋人"),
    Achievement.BROKEN_CLOCK: (
        "停摆的钟一天也准两回",
        "以冒牌先知身份，到终局时至少有两次查验结果是对的"),
    Achievement.SO_CLOSE: ("就差一点！", "以圣战者身份在放逐投票中打成最高平票"),
    Achievement.CULT_CON: (
        "邪教大会", "终局时存活的邪教徒达到 10 人及以上，你是其中之一"),
    Achievement.SELF_LOVING: (
        "自恋成瘾", "以爱神身份把自己选成恋人之一"),
    Achievement.SHOULDVE_MENTIONED: (
        "早说啊",
        "以狼人身份眼看着狼队吃掉了你的恋人（第一晚不算）"),
    Achievement.TANNER_OVERKILL: (
        "众叛亲离",
        "以圣战者身份，除你之外的所有人都投票放逐你"),
    Achievement.SERIAL_SAMARITAN: (
        "变态活雷锋",
        "以变态杀人狂身份在同一局里至少杀死 3 只狼人"),
    Achievement.CULT_FODDER: (
        "送死的教徒",
        "成为被派去转化异端审判官的那名邪教徒"),
    Achievement.LONE_WOLF: (
        "独狼",
        "在 10 人及以上的混乱模式游戏里，作为唯一一只狼人取胜"),
    Achievement.PACK_HUNTER: ("群狼围猎", "同一时刻场上有 7 只存活的狼人，你是其中之一"),
    Achievement.GUNNER_SAVES: (
        "一颗子弹救全村",
        "身在村民阵营，狼人数量已经和村民持平，但因为枪手还留着子弹，游戏没有结束"),
    Achievement.LONG_HAUL: (
        "持久战", "在同一局游戏里存活至少一个小时"),
    Achievement.OH_SHI: ("卧槽——", "第一晚就杀死了自己的恋人"),
    Achievement.VETERAN: ("老兵", "累计参与 500 局游戏，你现在可以加入 @werewolfvets 了"),
    Achievement.NO_SORCERY: ("不需要法术！", "以狼人身份杀死了自己阵营的黑暗法师"),
    Achievement.CULTIST_TRACKER: (
        "邪教猎手",
        "以异端审判官身份在同一局里至少处决 3 名邪教徒"),
    Achievement.IM_NOT_DRUNK: (
        "我没醉——嗝！",
        "以傻瓜身份，到终局时至少有 3 次放逐投对了人"),
    Achievement.WUFFIE_CULT: (
        "狼族传销",
        "以头狼（源狼）身份成功把至少 3 名受害者变成狼人"),
    Achievement.DID_YOU_GUARD_YOURSELF: (
        "你守的是自己吧？",
        "以守卫身份连续 3 次守护了没被袭击的狼人，还活了下来"),
    Achievement.SPOILED_RICH_BRAT: (
        "被宠坏的富家子",
        "以公主身份亮明了身份，却还是被放逐"),
    Achievement.THREE_LITTLE_WOLVES: (
        "三只小狼和一头大坏猪",
        "以黑暗法师身份活到终局，且场上还有三只及以上存活的狼人"),
    Achievement.PRESIDENT: (
        "总统", "以市长身份亮明身份后成功投出 3 次放逐票"),
    Achievement.I_HELPED: (
        "我也出力了！",
        "以幼狼身份死去后，存活的狼队当晚成功吃掉了两个人"),
    Achievement.IT_WAS_A_BUSY_NIGHT: (
        "今晚家里真热闹！",
        "在同一个夜晚被 3 个及以上不同的夜访角色造访"),
    Achievement.STRONGEST_ALPHA: (
        "最强头狼", "以头狼（源狼）身份成功感染变态杀人狂！"),
    Achievement.AM_I_YOUR_SEER: (
        "我是你的先知吗？", "以冒牌先知身份准确认出无神论者"),
    Achievement.DEMOTED_BY_THE_DEATH: (
        "死后被降级",
        "以猎人身份用最后一枪打死长老，自己沦为普通村民而死"),
    Achievement.WASTED_SILVER: (
        "白撒的银粉",
        "以铁匠身份在睡神哼唱催眠曲的同一天撒出银粉"),
    Achievement.TRUSTWORTHY: (
        "清白之身",
        "以“狼”人身份被先知查验之后，依然活到终局并获胜"),
    Achievement.DEEP_LOVE: (
        "爱得深沉",
        "以模仿者身份把恋人选作模仿对象。真是爱得深沉 <3"),
    Achievement.TIME_TO_RETIRE: (
        "是时候退休了……",
        "以黑暗法师身份成为村里最后一个活人，却输掉了游戏"),
    Achievement.SEEING_BETWEEN_TEAMS: (
        "跨阵营的视线", "成为先知与黑暗法师的情侣组合之一"),
    Achievement.JUST_A_BEARDY_GUY: (
        "只是个大胡子？",
        "以“狼”人身份被头狼（源狼）感染，变成了真正的狼人。嗷呜——！"),
    Achievement.THAT_CAME_UNEXPECTED: (
        "这也太意外了！",
        "以圣战者身份在只剩 3 人时被放逐，并赢下游戏"),
    Achievement.NOW_IM_BLIND: (
        "眼前一黑",
        "以神谕身份因为其他人身份全都一样，而没能得到任何启示。"),
    Achievement.EVERY_MAN_FOR_HIMSELF: (
        "先顾好自己！",
        "以和平演说者身份把自己从放逐中救下来（此时已有至少 50% 的票投向你）"),
    Achievement.MY_SWEETIE_SO_STRONG: (
        "我家那位真给力！",
        "与和平演说者相恋，并被他从放逐中救下（此时已有至少 50% 的票投向你）"),
    Achievement.CULT_LEADER: (
        "教主", "从开局就是邪教徒，一路活到最后并赢下游戏。"),
    Achievement.THANKS_JUNIOR: (
        "多谢了，小老弟！",
        "狼队吃掉酗酒艺术家之后，你变成了狼人，趁其他狼都醉倒时独自出手吃人！"),
    Achievement.DEATH_VILLAGE: (
        "死亡村庄", "参与一局没有任何赢家的游戏。"),
    Achievement.I_LOST_MY_WISDOM: (
        "智慧不翼而飞",
        "以长老身份变换了身份！忽然之间，你也没那么睿智了……"),
    Achievement.AFFECTIONATE: ("情深意切", "以秘密警察身份去查了自己的恋人！"),
    Achievement.LUCKY_DAY: (
        "走运的一天", "以头狼（源狼）身份感染了酗酒艺术家，自己却没跟着醉倒！好险……"),
    Achievement.CONDITION_RED: (
        "一级警报！", "作为场上最后一只活狼，把叛徒吃掉了。糟了！"),
    Achievement.INDESTRUCTIBLE: (
        "金刚不坏",
        "成为模仿者或孤儿，而模仿对象正是你自己！"),
    Achievement.PSYCHOPATH_KILLER: (
        "变态中的变态", "以变态杀人狂身份赢下一局 35 人的游戏！"),
    Achievement.TODAYS_SPECIAL: (
        "今日特供！",
        "参加一次狼人杀特别活动！当前：在 2020 年愚人节被耍一次！"),
    Achievement.ROMEO_AND_JULIET: (
        "罗密欧与朱丽叶",
        "与圣战者相恋，并靠放逐自己的恋人取胜！"),
    Achievement.REALLY_BAD_LUCK: (
        "倒霉透顶",
        "以变态杀人狂身份掉进坟坑，随后随机杀人，又被守卫挡了回来。"),
    Achievement.DOMINO: (
        "多米诺骨牌", "以猎人身份打死另一名猎人，逼得对方也开了枪。"),
    Achievement.DOUBLE_SHOT: (
        "一枪两命",
        "以猎人或枪手身份打死一个坏人，而这个坏人的恋人也是坏人！"),
    Achievement.PLAYING_WITH_THE_FIRE: (
        "玩火", "以纵火狂身份在一晚烧掉 5 栋及以上的房子。"),
    Achievement.FIREWORK: (
        "烟火大会",
        "以纵火狂身份在一晚烧掉 10 栋及以上的房子！多漂亮的烟火 :)"),
    Achievement.COLD_AS_ICE: (
        "冷若冰霜", "以雪狼身份冻住秘密警察。他们的爱意冷得像冰。"),
    Achievement.GOOD_CHOICE_FOR_YOU: (
        "好选择……对你而言",
        "以疯狂化学家身份在同一局里 3 次找人对饮并活了下来。"),
    Achievement.INCREASE_THE_PACK: (
        "狼群扩编！",
        "在幼狼死后，以头狼（源狼）身份感染 2 名玩家！"),
    Achievement.FIREFIGHTER: (
        "消防员", "以守卫身份清理掉三栋房子上的煤油。"),
    Achievement.HELPFUL_PARANOIA: (
        "有用的疑心病", "以猎人身份打死两名袭击者！"),
    Achievement.S_TIER_HUNTER: (
        "S 级猎人",
        "以猎人身份在同一晚干掉一只狼人和一名邪教徒！"),
    Achievement.TRIPLE_KILL: (
        "三杀",
        "以变态杀人狂或狼人身份，在同一晚让至少三个人死在你手上！"),
    Achievement.RESIST_THE_BEAST: (
        "守住人性",
        "孤儿、叛徒、基因缺陷者三人同场，全都没有变成狼，并随村民阵营取胜。"),
    Achievement.AT_LEAST_YOU_TRIED: (
        "好歹你努力过……",
        "以守卫身份在夜里救下一个人，却眼看着他死于疯狂化学家的毒药。"),
    Achievement.LUCKY_NIGHT: (
        "好运之夜",
        "同一个夜晚既在疯狂化学家手下活了下来，又迎来了秘密警察的登门造访！"),
    Achievement.IN_THE_MIDDLE_OF_THE_TROUBLE: (
        "身陷是非之中",
        "以守卫身份从袭击中救下一只狼人，自己还活了下来！"),
    Achievement.AM_I_HALLUCINATING: (
        "我是不是出现幻觉了？！",
        "以冒牌先知身份，看到了一个先知永远查不到的身份"),
}


def achievement_name(item: Achievement) -> str:
    """对应官方 GetName() 扩展方法。"""
    return ACHIEVEMENT_INFO.get(item, (item.name, ""))[0]


def achievement_description(item: Achievement) -> str:
    """对应官方 GetDescription() 扩展方法。"""
    return ACHIEVEMENT_INFO.get(item, ("", ""))[1]


def unlock_text(item: Achievement) -> str:
    """对应 Werewolf.cs:6058 的即时解锁提示。"""
    return (f"成就解锁！\n{achievement_name(item)}\n"
            f"{achievement_description(item)}")
