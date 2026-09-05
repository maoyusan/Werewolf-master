# -*- coding: utf-8 -*-
"""Build the concise werewolf player Word guide via officecli batch."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FILE = Path(r"d:\Project\ScriptProjects\Werewolf-master\werewolf-python\docs\狼人杀局内说明.docx")
BATCH = Path(r"d:\Project\ScriptProjects\Werewolf-master\werewolf-python\docs\_guide_batch.json")

EA = "微软雅黑"
LATIN = "Calibri"
GREEN = "1B4332"
MUTED = "475569"
BODY = "1F2937"
HEADER_FILL = "1B4332"
ROW_ALT = "F0F7F4"
BORDER = "single;4;CBD5E1"


def run(args: list[str]) -> None:
    print("+", " ".join(args[:6]), "...")
    r = subprocess.run(args, check=False)
    if r.returncode != 0:
        sys.exit(r.returncode)


def para(text: str, **props) -> dict:
    p = {
        "font.ea": EA,
        "font.latin": LATIN,
        "color": BODY,
        "size": "11pt",
        "spaceAfter": "8pt",
        "lineSpacing": "1.15x",
        "text": text,
    }
    p.update(props)
    return {"command": "add", "parent": "/body", "type": "paragraph", "props": p}


def h1(text: str) -> dict:
    return para(
        text,
        style="Heading1",
        size="18pt",
        bold="true",
        color=GREEN,
        spaceBefore="16pt",
        spaceAfter="8pt",
    )


def h2(text: str) -> dict:
    return para(
        text,
        style="Heading2",
        size="14pt",
        bold="true",
        color=GREEN,
        spaceBefore="12pt",
        spaceAfter="6pt",
    )


def add_table(rows: int, cols: int, widths: str) -> dict:
    return {
        "command": "add",
        "parent": "/body",
        "type": "table",
        "props": {"rows": str(rows), "cols": str(cols), "width": "100%", "colWidths": widths},
    }


def fill_table(tbl: int, header: list[str], data: list[list[str]]) -> list[dict]:
    ops: list[dict] = []
    for i, cell in enumerate(header, 1):
        ops.append(
            {
                "command": "set",
                "path": f"/body/tbl[{tbl}]/tr[1]/tc[{i}]",
                "props": {
                    "text": cell,
                    "bold": "true",
                    "color": "FFFFFF",
                    "fill": HEADER_FILL,
                    "size": "10.5pt",
                    "font": EA,
                    "valign": "center",
                },
            }
        )
    ops.append({"command": "set", "path": f"/body/tbl[{tbl}]/tr[1]", "props": {"header": "true"}})
    for r, row in enumerate(data, 2):
        fill = ROW_ALT if r % 2 == 0 else "FFFFFF"
        for c, val in enumerate(row, 1):
            ops.append(
                {
                    "command": "set",
                    "path": f"/body/tbl[{tbl}]/tr[{r}]/tc[{c}]",
                    "props": {
                        "text": val,
                        "size": "10.5pt",
                        "font": EA,
                        "fill": fill,
                        "color": BODY,
                        "valign": "center",
                    },
                }
            )
    return ops


def main() -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["officecli", "close", str(FILE)], check=False)
    if FILE.exists():
        FILE.unlink()

    run(["officecli", "create", str(FILE)])
    run(["officecli", "open", str(FILE)])
    run(
        [
            "officecli",
            "set",
            str(FILE),
            "/",
            "--prop",
            "docDefaults.font=Calibri",
            "--prop",
            "docDefaults.fontSize=11pt",
        ]
    )

    ops: list[dict] = []

    ops.append(
        para(
            "狼人杀局内说明",
            size="28pt",
            bold="true",
            color=GREEN,
            spaceAfter="8pt",
            spaceBefore="48pt",
        )
    )
    ops.append(para("QQ 群文字指令，给第一次进群的人看。", size="14pt", color=MUTED, spaceAfter="18pt"))
    ops.append(
        para(
            "按这个仓库现在的规则写。夜里私聊动手，白天在群里说话和投票。斜杠可加可不加。",
            size="12pt",
            spaceAfter="16pt",
        )
    )

    ops.append(add_table(4, 2, "2200,6800"))
    ops.extend(
        fill_table(
            1,
            ["这块", "看什么"],
            [
                ["怎么玩", "从加好友到一局走完，卡了先看哪"],
                ["指令", "群里发什么，私聊发什么"],
                ["角色", "谁夜里动手，谁白天动手，谁死了才动手"],
            ],
        )
    )
    ops.append(para("2026年9月", size="11pt", color=MUTED, spaceBefore="18pt", spaceAfter="0pt"))
    ops.append({"command": "add", "parent": "/body/p[last()]", "type": "pagebreak", "props": {"type": "page"}})

    ops.append(
        {
            "command": "add",
            "parent": "/",
            "type": "toc",
            "props": {"title": "目录", "levels": "1-2", "hyperlinks": "true", "pageNumbers": "true"},
        }
    )

    ops.append(h1("怎么玩"))
    ops.append(
        para(
            "先加机器人为好友。不加好友，夜里收不到身份牌。加过一次就一直有效。"
        )
    )
    ops.append(
        para(
            "群里办事：建房、加入、开局、投票。私聊办事：看身份、夜里动手、部分白天技能。"
            "发错地方机器人会回你。夜里技能发到群里，等于当着所有人报身份。"
        )
    )
    ops.append(
        para(
            "斜杠可加可不加。join、/join、加入效果一样。不用 @ 机器人。"
            "目标写座位号或昵称，例如 狼人 3。只发指令不写人，机器人会列出能选的人，你回座位号。"
            "拖到阶段结束，这次选择作废。分步选人时可以回 确认、重选、取消。"
            "默认不能选自己，也不能选已经出局的人。丘比特连线时允许把自己算进去。"
        )
    )
    ops.append(
        para(
            "机器人在群里是管理员的话，开局会把名片改成 1号、2号，出局改成 3号（已出局）。"
            "不是管理员也能玩，号码自己记。"
        )
    )

    ops.append(h2("一局怎么走"))
    ops.append(
        para(
            "有人在群里发 startgame 建房。别人发 join。够人数后，房主发 go，或再发一次 startgame。"
            "入场默认 180 秒。到点人数够就自动开，不够就散。一个群同时只能有一局。"
        )
    )
    ops.append(
        para(
            "开局后每人私聊一张身份。夜里默认 90 秒。有技能的人私聊动手，每人每夜只能交一次。"
            "不想动就发 跳过。丘比特、分身、野孩子、盗贼首夜必须选人。"
            "人都交完或时间到，夜里结算，群里报死讯。"
        )
    )
    ops.append(
        para(
            "白天在群里聊天。机器人不解析闲聊。讨论结束进投票，群里发 投票 3 或 弃票。"
            "投完不能改。全员投完立刻出结果，不用等满 90 秒。平票或全员弃票，本轮没人出局。"
        )
    )
    ops.append(
        para(
            "猎人死亡后有 30 秒开枪窗口，这期间整局停着等他。"
            "情侣一方死，另一方跟着死。然后又是夜，循环到有阵营赢。"
        )
    )

    ops.append(h2("人数和时限"))
    ops.append(add_table(8, 2, "2800,6200"))
    ops.extend(
        fill_table(
            2,
            ["项目", "默认"],
            [
                ["最少人数", "5 人，部署时可改，本机测试常见 3 人"],
                ["最多人数", "35 人"],
                ["入场", "180 秒"],
                ["夜里", "90 秒；首夜有丘比特、分身、野孩子、盗贼时至少 120 秒"],
                ["投票", "90 秒，全员投完提前结算"],
                ["白天讨论", "人越多越长"],
                ["猎人开枪", "死亡后 30 秒"],
            ],
        )
    )
    ops.append(para("群里发 config，能看到本群实际数字。", spaceBefore="8pt"))

    ops.append(h2("怎么算赢"))
    ops.append(para("狼人数不少于场上其他人，狼赢。场上没狼也没教徒，村赢。"))
    ops.append(para("替罪羊被投出去，他自己赢。只剩两个人时，先看情侣，再看连环杀手、纵火犯、教会。"))
    ops.append(para("只剩一个人，且他是替罪羊、巫师、盗贼或分身，判无人获胜。"))

    ops.append(h2("常见卡点"))
    ops.append(
        para(
            "没回，先在群里发 status。这条任何阶段都有反应。"
            "status 有反应、你的指令没有，多半是发错地方、你不在局里，或当前阶段不让这个动作。"
        )
    )
    ops.append(para("夜里没私聊，先确认加过好友。已经是好友却没有菜单，多半是你这身份夜里没技能，等天亮。"))
    ops.append(para("想改票，改不了。群里已经有一局，先让房主或管理员发 cancel，或等这局结束。"))

    ops.append(h1("指令"))
    ops.append(h2("群里发"))
    ops.append(add_table(15, 3, "2600,2400,4000"))
    ops.extend(
        fill_table(
            3,
            ["指令", "也能这么写", "干什么"],
            [
                ["startgame", "开局、建局、开始游戏", "建房；人数够时房主再发一次等于立刻开"],
                ["startchaos", "混乱开始", "建混乱局"],
                ["join", "加入、参加", "入场加入"],
                ["go", "强制开始、立即开始", "房主开局，不用管理员"],
                ["leave", "退出、离开", "入场阶段退出"],
                ["cancel", "取消", "房主解散本局"],
                ["status", "状态、玩家、players", "看阶段、人数、剩余时间"],
                ["vote 3", "投票 3", "投票阶段处决"],
                ["弃票", "abstain；投票阶段发跳过也行", "放弃这一票"],
                ["flee", "逃跑", "入场是退出；开局后算死亡"],
                ["结算", "结果、历史", "看本局结算"],
                ["rolelist", "", "看身份目录"],
                ["config", "getconfig", "看本群规则"],
                ["nextgame", "", "预约下一局提醒"],
            ],
        )
    )

    ops.append(h2("私聊发"))
    ops.append(add_table(19, 3, "2600,2800,3600"))
    ops.extend(
        fill_table(
            4,
            ["指令", "谁用", "什么时候"],
            [
                ["身份", "所有人", "任何时候看自己的牌"],
                ["狼人 3", "狼人、狼崽、阿尔法狼", "夜里袭击"],
                ["转化 3", "教徒、阿尔法狼", "夜里发展或转化"],
                ["查验 3", "预言家、愚者、神谕者、巫师", "夜里查人"],
                ["守护 3", "守卫", "夜里保人"],
                ["访问 3", "妓女", "夜里拜访"],
                ["冻结 3", "雪狼", "夜里冻结"],
                ["连环杀 3", "连环杀手", "夜里独自杀人"],
                ["猎杀教徒 3", "教徒猎人", "夜里猎教徒，猜错自己死"],
                ["化学 3", "化学家", "夜里服药"],
                ["盗取 3", "盗贼", "夜里盗身份"],
                ["纵火 3 / 引燃", "纵火犯", "夜里先浇油，再点火"],
                ["恋人 2 5 / 模仿 3 / 偶像 3", "丘比特、分身、野孩子", "仅首夜，必须选"],
                ["猎杀 3", "猎人", "死亡后 30 秒内"],
                ["跳过", "有夜间菜单的人", "放弃本次行动"],
                ["侦查 3 / 开枪 3", "侦探、枪手、南瓜", "白天"],
                ["市长 / 和平", "市长、和平主义者", "仅第一天"],
                ["催眠 是 / 撒银 是 / 捣乱", "沙人、铁匠、捣乱者", "白天一次性"],
            ],
        )
    )
    ops.append(para("分步选人时回 确认、重选、取消。统计看个人战绩。群里发 help、ping、version 也能用。", spaceBefore="8pt"))

    ops.append(h1("角色"))
    ops.append(para("开局私聊发身份。狼人会看到同伴，教徒看到教徒，石匠看到另一个石匠，守望者看到预言家。愚者会被当成预言家通知，查验结果是假的。"))

    ops.append(h2("夜里动手"))
    ops.append(add_table(21, 3, "2200,2400,4400"))
    ops.extend(
        fill_table(
            5,
            ["身份", "私聊发", "备注"],
            [
                ["狼人 / 狼崽", "狼人 3", "多只狼一起决定杀谁"],
                ["阿尔法狼", "狼人 3 或 转化 3", "转化有概率失败"],
                ["雪狼", "冻结 3", "不参与袭击投票，计入狼人数"],
                ["预言家 / 学徒预言家", "查验 3", "看对方是不是狼"],
                ["愚者", "查验 3", "以为自己是预言家，结果不准"],
                ["神谕者", "查验 3", "看到对方不是某个身份"],
                ["占卜师", "不用发", "每夜自动看到一个未公开身份"],
                ["守卫", "守护 3", "保人不被狼杀死"],
                ["妓女", "访问 3", "访到狼人会死；被访的人当夜被杀你也死"],
                ["教徒", "转化 3", "发展教徒"],
                ["教徒猎人", "猎杀教徒 3", "猜错自己死"],
                ["连环杀手", "连环杀 3", "每夜独自杀一人"],
                ["纵火犯", "纵火 3 / 引燃", "先浇油，再一次点燃"],
                ["丘比特", "恋人 2 5", "仅首夜；一方死另一方殉情"],
                ["分身", "模仿 3", "仅首夜；模板死后继承身份"],
                ["野孩子", "偶像 3", "仅首夜；偶像死后变狼"],
                ["盗贼", "盗取 3", "有概率偷到身份"],
                ["化学家", "化学 3", "有概率毒死对方"],
                ["掘墓人", "不用发", "自动挖昨夜新坟"],
                ["巫师", "查验 3", "只能分辨是不是狼或预言家"],
            ],
        )
    )
    ops.append(para("普通村民、酒鬼、王子、市长、和平主义者夜里没有菜单，等天亮。", spaceBefore="8pt"))

    ops.append(h2("白天动手"))
    ops.append(add_table(9, 3, "2400,2400,4200"))
    ops.extend(
        fill_table(
            6,
            ["身份", "私聊发", "备注"],
            [
                ["侦探", "侦查 3", "讨论结束后告诉你身份，有概率暴露自己"],
                ["市长", "市长", "仅第一天；公开后票算两票"],
                ["和平主义者", "和平", "仅第一天；本轮不处决"],
                ["沙人", "催眠 是", "一次性；当晚夜间行动全停"],
                ["铁匠", "撒银 是", "一次性；当夜狼不能袭击"],
                ["枪手", "开枪 3", "有子弹时当场打死"],
                ["南瓜", "开枪 3", "和目标一起死"],
                ["捣乱者", "捣乱", "一次性；本轮投两次，出局两人"],
            ],
        )
    )

    ops.append(h2("死了才变"))
    ops.append(add_table(9, 2, "2800,6200"))
    ops.extend(
        fill_table(
            7,
            ["身份", "触发"],
            [
                ["猎人", "死亡后 30 秒内发 猎杀 3，放弃发 跳过"],
                ["情侣", "一方死，另一方立刻殉情"],
                ["诅咒者", "被狼袭击时不死，变成狼"],
                ["叛徒", "所有狼出局后变成狼"],
                ["学徒预言家", "预言家出局后接班"],
                ["狼崽", "死后，狼当夜可多杀一个"],
                ["智者", "第一次被狼袭击能活"],
                ["王子", "第一次被投出去时公开身份并免疫"],
            ],
        )
    )

    BATCH.write_text(json.dumps(ops, ensure_ascii=False, indent=2), encoding="utf-8")
    run(["officecli", "batch", str(FILE), "--input", str(BATCH), "--json"])

    run(
        [
            "officecli",
            "add",
            str(FILE),
            "/",
            "--type",
            "header",
            "--prop",
            "type=default",
            "--prop",
            "text=狼人杀局内说明",
            "--prop",
            f"font.ea={EA}",
            "--prop",
            "size=9pt",
            "--prop",
            f"color={MUTED}",
        ]
    )
    run(
        [
            "officecli",
            "add",
            str(FILE),
            "/",
            "--type",
            "footer",
            "--prop",
            "type=default",
            "--prop",
            "size=9pt",
            "--prop",
            "text=QQ 群 · ",
            "--prop",
            "field=page",
        ]
    )
    run(["officecli", "set", str(FILE), "/header[1]/p[1]", "--prop", "align=left"])
    run(["officecli", "set", str(FILE), "/footer[1]/p[1]", "--prop", "align=center"])
    run(["officecli", "refresh", str(FILE)])
    run(["officecli", "save", str(FILE)])
    print("wrote", FILE)


if __name__ == "__main__":
    main()
