# coding=utf-8
"""P5-G2 回归：幕后痕迹 / 线索检定 / 防泄漏。

守的四件事（这层最容易"看起来对"的地方）：
  A. **失败不许说"这里有东西"**：玩家看到的失败句与"有没有线索"完全无关；
  B. **公开那句只写"做了什么"**，结果只给行动者（和 DM）；
  C. **同一个人只能试一次**：不许反复掷骰到成功；别人还能试；
  D. **发现后才记记忆**，公开痕迹进公开记忆、秘密痕迹只进发现者。
"""

from __future__ import annotations

import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure.model import (  # noqa: E402
    Adventure, AdventureError, Scene, clue_brief_text, parse_adventure,
    render_skeleton,
)
from engine.adventure.runner import AdventureRunner  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.memory import MemoryStore, parse_game_time  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.persist import dump_game, load_from_dict  # noqa: E402
from engine.rules.clues import (  # noqa: E402
    CLUE_ACTIONS, ClueError, ClueOutcome, SceneClue, check_clue, failure_text,
    parse_clue, success_text,
)
from engine.rules.loop import GameLoop, RoundLog, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402
from engine.state.visibility import lint_leaks  # noqa: E402
from engine.state.world import World, WorldError, new_world  # noqa: E402

FAILS: list[str] = []
COUNT = 0

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
SECRET_TEXT = "楼梯上方传来压低的说话声，两个人正在争那只箱子该归谁。"
PUBLIC_TEXT = "门轴有一道新刮痕，门槛下积着碎木屑：这扇门最近被反复推开过。"


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


def mk_loop(names=("艾尔玛", "罗恩"), *, memory=None, seed=7):
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", abilities=AB,
                          hp_max=24, ac=15) for n in names},
        current=names[0], world=new_world(date="1492-09-17", period="午后"))
    w = World(st).ensure()
    w.discover("废弃哨塔", description="塔门半开。")
    w.travel("废弃哨塔", actor="DM")
    led = Ledger()
    loop = GameLoop(st, led, FakeModel(name="clue-test"), rng=Random(seed),
                    memory=memory)
    loop.set_field([make_hero_unit(st, n) for n in names])
    return st, led, loop


# --------------------------------------------------------------------------- #
section("A. 线索模型：解析与校验")

c = parse_clue({"id": "c9", "text": "地上有一道很新的拖痕通到楼梯口。"})
ok(c.skill == "perception" and c.dc == 12 and c.secret is True,
   "A1 缺省：察觉 / DC 12 / 秘密", (c.skill, c.dc, c.secret))
ok(c.action == "look", "A2 察觉 → 观察（look）")
ok(CLUE_ACTIONS.get("investigation") == "search", "A3 调查 → 搜索（search）")
ok(parse_clue({"id": "x", "text": "很长的一句话用来测试解析。",
               "skill": "Investigation", "dc": "13"}).skill == "investigation"
   and parse_clue({"id": "x", "text": "很长的一句话用来测试解析。",
                   "dc": "13"}).dc == 13,
   "A4 技能大小写归一、dc 字符串转整数")
ok(raises(lambda: parse_clue({"id": "x", "dc": "不是数字"}), ClueError),
   "A5 dc 不是整数 → 解析即报错")
ok(raises(lambda: parse_clue("不是对象"), ClueError), "A6 不是对象 → 报错")

ok(check_clue({"id": "c1", "text": SECRET_TEXT}) == [], "A7 正常线索通过校验")
ok(any("缺少 id" in e for e in check_clue({"text": SECRET_TEXT})),
   "A8 缺 id 报错")
ok(any("太短" in e for e in check_clue({"id": "c1", "text": "有东西"})),
   "A9 ⭐ 文字太短（看不清「看到了什么」）报错")
ok(any("skill" in e for e in check_clue(
    {"id": "c1", "text": SECRET_TEXT, "skill": "arcana"})),
   "A10 ⭐ 这版触发不了的技能（奥秘）直接报错，不假装能用")
ok(any("dc" in e for e in check_clue({"id": "c1", "text": SECRET_TEXT, "dc": 99})),
   "A11 DC 越界报错")
ok(any("dc" in e for e in check_clue({"id": "c1", "text": SECRET_TEXT, "dc": 1})),
   "A12 DC 太小也算错（1 ~ 4）")
ok(raises(lambda: parse_clue({"id": "x", "text": SECRET_TEXT,
                              "secret": "false"}), ClueError),
   "A13 ⭐ secret 只认真布尔：字符串 \"false\" 报错（不静默当 true）")
ok(check_clue({"id": "x", "text": SECRET_TEXT, "secret": 0}) != [],
   "A14 数字 0 也不当 false —— 明说要 true/false")


# --------------------------------------------------------------------------- #
section("B. 剧本：clues 进 JSON、进校验、进骨架")

RAW = {
    "title": "线索测试剧本",
    "scenes": [
        {"id": "s1", "title": "门口",
         "situation": "你们站在哨塔门外，门轴被风推得半开。",
         "brief": {"present": "甲在门口，乙在看门轴", "span": "从村里走了两小时",
                   "offstage": "塔顶的强盗正在把木箱往墙根挪"},
         "world": {"location": "废弃哨塔"},
         "clues": [
             {"id": "c1", "text": SECRET_TEXT, "skill": "perception", "dc": 14},
             {"id": "c2", "text": PUBLIC_TEXT, "skill": "investigation",
              "dc": 12, "secret": False},
         ]},
    ],
}
adv = parse_adventure(RAW)
ok(len(adv.scene("s1").clues) == 2, "B1 两条线索解析出来",
   len(adv.scene("s1").clues))
errs, warns = adv.validate()
ok(errs == [], "B2 合法剧本零错误", errs)
ok(adv.scene("s1").clues[1].secret is False, "B3 secret:false 认得出")

txt = render_skeleton(adv)
ok("痕迹" in txt and "c1" in txt and "察觉 DC 14" in txt,
   "B4 骨架视图里有痕迹与 DC", txt[-200:])

RAW2 = {"title": "重复线索", "scenes": [dict(RAW["scenes"][0])]}
RAW2["scenes"][0]["clues"] = [
    {"id": "dup", "text": SECRET_TEXT},
    {"id": "dup", "text": PUBLIC_TEXT},
]
e2, _ = parse_adventure(RAW2).validate()
ok(any("线索 id 重复" in x for x in e2), "B5 ⭐ 线索 id 必须全局唯一（存世界树按 id）",
   e2)

RAW3 = {"title": "坏线索", "scenes": [dict(RAW["scenes"][0])]}
RAW3["scenes"][0]["clues"] = [{"id": "c1", "text": SECRET_TEXT, "skill": "arcana"}]
e3, _ = parse_adventure(RAW3).validate()
ok(any("skill" in x for x in e3), "B6 坏技能在剧本校验里被拦", e3)

RAW4 = {"title": "坏线索2", "scenes": [dict(RAW["scenes"][0])]}
RAW4["scenes"][0]["clues"] = [{"text": SECRET_TEXT}]
e4, _ = parse_adventure(RAW4).validate()
ok(any("缺少 id" in x for x in e4), "B7 缺 id 在剧本校验里被拦", e4)

RAW5 = {"title": "线索不是数组", "scenes": [dict(RAW["scenes"][0])]}
RAW5["scenes"][0]["clues"] = "c1"
ok(raises(lambda: parse_adventure(RAW5), AdventureError),
   "B8 clues 不是数组 → 解析直接报错")

brief = clue_brief_text(adv.scene("s1"), found=())
ok("只有 DM 看得到" in brief and "c1" in brief and SECRET_TEXT in brief,
   "B9 线索简报把正文给 DM")
ok("未发现" in brief, "B10 没发现时标「未发现」")
ok("已发现" in clue_brief_text(adv.scene("s1"), found={"c1"}),
   "B11 发现过的标「已发现」")
ok(clue_brief_text(Scene(id="s9", title="空场")) == "",
   "B12 没有线索 → 空串（不占 token）")


# --------------------------------------------------------------------------- #
section("C. 世界树：哪条发现了、谁试过")

st, led, loop = mk_loop()
w = loop.world
ok(w.clue("c1") is None, "C1 未登记时 clue() 返回 None")
first = w.register_clue("c1", scene="s1", place="废弃哨塔",
                        skill="perception", dc=14, text=SECRET_TEXT,
                        secret=True, source="塔顶有人")
ok(first and w.clue("c1")["已发现"] is False, "C2 登记成功、初始未发现")
ok(w.register_clue("c1", scene="s1", place="废弃哨塔", skill="perception",
                   dc=14, text=SECRET_TEXT) == [] and
   len(w.clue_tried_by("c1")) == 0,
   "C3 ⭐ 重复登记幂等（重跑同一场不重置状态）")
w.mark_clue_tried("c1", "艾尔玛")
w.mark_clue_tried("c1", "艾尔玛")
ok(w.clue_tried_by("c1") == ["艾尔玛"], "C4 同一个人只记一次尝试")
w.mark_clue_tried("c1", "罗恩")
ok(w.clue_tried_by("c1") == ["艾尔玛", "罗恩"], "C5 别人可以试")
ok(raises(lambda: w.mark_clue_tried("nope", "甲"), WorldError),
   "C6 未登记的线索不能记尝试")
ok(raises(lambda: w.mark_clue_found("nope", by="甲"), WorldError),
   "C7 未登记的线索不能标发现")

w.mark_clue_found("c1", by="艾尔玛", round=3)
node = w.clue("c1")
ok(node["已发现"] is True and node["发现者"] == "艾尔玛" and node["发现轮"] == 3,
   "C8 标发现：发现者与轮次都记下", node)
ok(w.mark_clue_found("c1", by="罗恩", round=4) == [] and
   w.clue("c1")["发现者"] == "艾尔玛",
   "C9 ⭐ 重复标发现不覆盖第一个发现者")
ok(w.found_clues() == ["c1"], "C10 found_clues 列出来")

dm_text = w.render(DM)
ok("线索 · 仅 DM" in dm_text and SECRET_TEXT in dm_text and "已被 艾尔玛 发现" in dm_text,
   "C11 DM 视角看得到痕迹与发现者")
player_text = w.render("罗恩")
ok("线索 · 仅 DM" not in player_text and SECRET_TEXT not in player_text,
   "C12 ⭐ 玩家视角的世界块里根本没有线索块")

# 线索是只加不减的：DM 世界块只列最近 6 条，否则包会随场次线性膨胀（P5-B2）
for i in range(1, 11):
    w.register_clue(f"m{i}", scene="s1", place="废弃哨塔", skill="perception",
                    dc=10, text=f"第 {i} 条补登记痕迹的正文，写得够长以便校验。")
dm2 = w.render(DM)
ok("只列最近 6 条" in dm2, "C13 ⭐ 线索多了以后注明「只列最近 6 条」",
   dm2[:200])
ok("m10" in dm2 and "m5" in dm2, "C14 最近 6 条在（m5–m10）")
ok("m1［" not in dm2 and "m4［" not in dm2, "C15 更早的不再整条列出")


# --------------------------------------------------------------------------- #
section("D. 谁能撞上：动作、地点、已发现、已试过")

st2, led2, loop2 = mk_loop()
w2 = loop2.world
w2.register_clue("c1", scene="s1", place="废弃哨塔", skill="perception",
                 dc=14, text=SECRET_TEXT)
w2.register_clue("c2", scene="s1", place="废弃哨塔", skill="investigation",
                 dc=12, text=PUBLIC_TEXT, secret=False)
w2.register_clue("c3", scene="s1", place="村子", skill="perception",
                 dc=10, text="村口的井沿上有一圈新的绳痕，像是刚有人打过水。")
loop2.set_scene_context(scene_id="s1", clues=(
    SceneClue(id="c1", text=SECRET_TEXT, dc=14),
    SceneClue(id="c2", text=PUBLIC_TEXT, skill="investigation", dc=12,
              secret=False),
    SceneClue(id="c3", text="村口的井沿上有一圈新的绳痕，像是刚有人打过水。",
              dc=10, place="村子"),
))
u = loop2.find_unit("艾尔玛")
ok(loop2._eligible_clue(u, "look").id == "c1", "D1 观察撞上察觉线索")
ok(loop2._eligible_clue(u, "search").id == "c2", "D2 搜索撞上调查线索")
u.name = "艾尔玛"
loop2.world.mark_clue_found("c3", by="罗恩")   # 但地点也不对，双重排除
ok(loop2._eligible_clue(u, "look").id == "c1",
   "D3 别的地点（村子）的线索撞不到 —— 人在废弃哨塔")

loop2.world.mark_clue_tried("c1", "艾尔玛")
ok(loop2._eligible_clue(u, "look") is None,
   "D4 ⭐ 同一个人试过就不能再试（防止掷骰到成功）")
v = loop2.find_unit("罗恩")
ok(loop2._eligible_clue(v, "look").id == "c1",
   "D5 但别人还能再试这条（尝试是按人记的）")

loop2.world.mark_clue_found("c1", by="罗恩")
loop2.scene_clues = (SceneClue(id="c1", text=SECRET_TEXT, dc=14),
                     SceneClue(id="c4", text="c4 的一段足够长的文字。", dc=18),
                     SceneClue(id="c5", text="c5 的一段足够长的文字。", dc=9))
loop2.world.register_clue("c4", place="废弃哨塔", skill="perception", dc=18,
                          text="c4 的一段足够长的文字。")
loop2.world.register_clue("c5", place="废弃哨塔", skill="perception", dc=9,
                          text="c5 的一段足够长的文字。")
ok(loop2._eligible_clue(v, "look").id == "c5",
   "D6 多条可用时先给 DC 最低的那条（最容易看见的）")


# --------------------------------------------------------------------------- #
section("E. 检定结果：成功/失败、秘密/公开")

st3, led3, loop3 = mk_loop()
w3 = loop3.world
su = loop3.find_unit("艾尔玛")
su.skill_bonuses["perception"] = 30          # 保证成功
sc = SceneClue(id="c1", text=SECRET_TEXT, dc=14)
w3.register_clue("c1", place="废弃哨塔", skill="perception", dc=14, text=SECRET_TEXT)
loop3.set_scene_context(scene_id="s1", clues=(sc,))
r = loop3._resolve_look_search(su, "look")
ok(r.public == "艾尔玛观察了四周。", "E1 公开那句只写「做了什么」", r.public)
ok("你发现了" in r.private and SECRET_TEXT in r.private,
   "E2 秘密线索的结果进私有", r.private)
ok(r.clue is not None and r.clue.found, "E3 结果标记为发现")
ok("线索 c1" in r.dm and "发现" in r.dm, "E4 DM 那句带线索编号与结果", r.dm)
ok(SECRET_TEXT not in r.public and SECRET_TEXT not in r.dm,
   "E5 ⭐ 公开句与 DM 句里都不抄线索正文（正文只在私有/世界树）")
ok(w3.clue("c1")["已发现"] and w3.clue("c1")["发现者"] == "艾尔玛",
   "E6 成功当场写进世界树")

# 失败：换成必败
st4, led4, loop4 = mk_loop()
fu = loop4.find_unit("艾尔玛")
fu.skill_bonuses["perception"] = -30
loop4.world.register_clue("c1", place="废弃哨塔", skill="perception",
                          dc=14, text=SECRET_TEXT)
loop4.set_scene_context(scene_id="s1", clues=(SceneClue(id="c1", text=SECRET_TEXT, dc=14),))
rf = loop4._resolve_look_search(fu, "look")
ok(rf.public == "艾尔玛观察了四周。", "E7 失败时公开句一模一样")
ok(rf.private == failure_text(), "E8 ⭐ 失败句不含线索内容", rf.private)
ok(SECRET_TEXT not in rf.public and SECRET_TEXT not in rf.private
   and SECRET_TEXT not in rf.dm, "E9 ⭐ 失败时三句里都没有线索正文")
ok(loop4.world.clue_tried_by("c1") == ["艾尔玛"], "E10 失败记一次尝试")
ok(loop4.world.clue("c1")["已发现"] is False, "E11 失败不标发现")

# 没有线索时：同一套句式，不制造对比
loop4.set_scene_context(scene_id="s1", clues=())
rn = loop4._resolve_look_search(fu, "look")
ok(rn.public == "艾尔玛观察了四周。", "E12 没有线索时公开句仍然一样")
ok(rn.private in ("你注意到了周围的一个细节。", failure_text()),
   "E13 没有线索时的私有结果也是这两种之一", rn.private)
ok(rn.dm == "" and rn.clue is None, "E14 没撞上线索就没有 DM 那条")

# 公开线索
st5, led5, loop5 = mk_loop()
pu = loop5.find_unit("艾尔玛")
pu.skill_bonuses["investigation"] = 30
loop5.world.register_clue("c2", place="废弃哨塔", skill="investigation",
                          dc=12, text=PUBLIC_TEXT, secret=False)
loop5.set_scene_context(scene_id="s1", clues=(
    SceneClue(id="c2", text=PUBLIC_TEXT, skill="investigation", dc=12,
              secret=False),))
rp = loop5._resolve_look_search(pu, "search")
ok(PUBLIC_TEXT in rp.public and "艾尔玛发现了" in rp.public,
   "E15 公开痕迹：一句话进公开", rp.public)
ok(rp.private == "", "E16 公开痕迹没有私有那句")
ok(rp.clue.visible_to("艾尔玛") == "public", "E17 公开痕迹的记忆是 public")
ok(SceneClue(id="x", text=SECRET_TEXT).secret is True
   and ClueOutcome(SceneClue(id="x", text=SECRET_TEXT), True, 15, 14)
   .visible_to("艾尔玛") == "pc:艾尔玛",
   "E18 秘密痕迹的记忆是 pc:<发现者>")


# --------------------------------------------------------------------------- #
section("F. 循环接线：结果不进别人的包")

st6, led6, loop6 = mk_loop(memory=MemoryStore(label="线索记忆"))
lu = loop6.find_unit("艾尔玛")
lu.skill_bonuses["perception"] = 30
loop6.world.register_clue("c1", place="废弃哨塔", skill="perception",
                          dc=14, text=SECRET_TEXT, source="塔顶有人")
loop6.set_scene_context(scene_id="s1", clues=(SceneClue(
    id="c1", text=SECRET_TEXT, dc=14, source="塔顶有人"),))
lg = RoundLog(n=1, phase="explore")
loop6._cur = lg
loop6._explore_turn(lu, lg)
loop6._cur = None

ok(lg.text().count("你发现了") == 1, "F1 终端日志里有发现那句", lg.text()[-160:])
pub = [e for e in led6.entries if e.visible_to.is_public()]
priv = [e for e in led6.entries if "pc:艾尔玛" in e.visible_to.tags]
secret = [e for e in led6.entries if "dm" in e.visible_to.tags]
ok(not any(SECRET_TEXT in e.text for e in pub),
   "F2 ⭐ 公开台账里没有线索正文")
ok(any(SECRET_TEXT in e.text for e in priv),
   "F3 艾尔玛的私有台账里有")
ok(any("线索 c1" in e.text for e in secret),
   "F4 DM 有一条带编号的")
ok(lint_leaks(led6) == [], "F5 ⭐ 整本台账通过 lint_leaks", lint_leaks(led6))

pack = loop6._assembler()
pack.set_block("角色卡", st6.render("罗恩"))
p_ron = pack.build("罗恩", task="轮到你。")
ok(SECRET_TEXT not in p_ron.text(),
   "F6 ⭐ 罗恩的包里没有艾尔玛发现的痕迹")
asm_dm = loop6._assembler()
loop6._set_world(asm_dm, DM)
asm_dm.set_block("角色卡", st6.render(DM) if st6.has_character(DM)
                 else "（DM 无角色卡）")
p_dm = asm_dm.build(DM, task="描述。")
ok("c1" in p_dm.text(), "F7 DM 的世界块里看得到 c1")

# 记忆：成功才记；秘密痕迹只给发现者
ok(len(loop6.memory) == 1, "F8 发现一条 → 记忆多一条", len(loop6.memory))
mem = loop6.memory.entries[0]
ok(mem.tags == ("clue:c1",) and mem.key == "clue:c1", "F9 记忆标签用 clue:<id>",
   mem.tags)
ok(mem.visible_to.tags == frozenset({"pc:艾尔玛"}),
   "F10 秘密痕迹的记忆只给发现者", mem.visible_to.tags)

# 失败不记记忆
lu2 = loop6.find_unit("罗恩")
lu2.skill_bonuses["perception"] = -30
loop6.world.register_clue("c9", place="废弃哨塔", skill="perception",
                          dc=14, text="c9 的一段足够长的线索文字。")
loop6.set_scene_context(scene_id="s1", clues=(SceneClue(
    id="c9", text="c9 的一段足够长的线索文字。", dc=14),))
lg2 = RoundLog(n=2, phase="explore")
loop6._cur = lg2
loop6._explore_turn(lu2, lg2)
loop6._cur = None
ok(len(loop6.memory) == 1, "F11 失败不记记忆", len(loop6.memory))


# --------------------------------------------------------------------------- #
section("G. 剧本推进器：登记、发现、简报")

ADV = parse_adventure({
    "title": "推进器线索",
    "scenes": [
        {"id": "s1", "title": "门口",
         "situation": "你们站在哨塔门外，门轴被风推得半开。",
         "brief": {"present": "甲在门口，乙在看门轴", "span": "走了两小时",
                   "offstage": "塔顶的强盗正在把木箱往墙根挪"},
         "world": {"location": "废弃哨塔"},
         "clues": [{"id": "k1", "text": SECRET_TEXT, "skill": "perception",
                    "dc": 14, "source": "塔顶的强盗正在把木箱往墙根挪"}]},
    ],
})
ADV.validate()
st7 = StateTree.from_template(
    {"艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                             hp_max=24, ac=15)},
    current="艾尔玛", world=new_world(date="1492-09-17", period="午后"))
loop7 = GameLoop(st7, Ledger(), FakeModel(name="runner"), rng=Random(3),
                 memory=MemoryStore(label="推进器记忆"))
loop7.set_field([make_hero_unit(st7, "艾尔玛")])
runner = AdventureRunner(loop7, ADV)
hint = runner.dm_hint(ADV.scene("s1"))
ok("k1" in hint and SECRET_TEXT in hint, "G1 DM 的任务说明里有本场痕迹")
ok("只有 DM 看得到" in hint, "G2 而且写明只有 DM 看")

runner.run_scene(ADV.scene("s1"))
ok(loop7.world.clue("k1") is not None, "G3 进场景就登记线索")
ok(loop7.world.clue("k1")["地点"] == "废弃哨塔", "G4 没写 place 就用本场地点")

# 玩家提示词里不能有痕迹正文
reqs = []
class _Cap(FakeModel):
    def _complete(self, req):
        reqs.append(req)
        return super()._complete(req)
st8 = StateTree.from_template(
    {"艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                             hp_max=24, ac=15)},
    current="艾尔玛", world=new_world(date="1492-09-17", period="午后"))
loop8 = GameLoop(st8, Ledger(), _Cap(name="cap"), rng=Random(4))
loop8.set_field([make_hero_unit(st8, "艾尔玛")])
AdventureRunner(loop8, ADV).run_scene(ADV.scene("s1"))
player_prompts = [r.prompt for r in reqs if r.role == "player"]
ok(player_prompts and all(SECRET_TEXT not in p for p in player_prompts),
   "G5 ⭐ 玩家提示词里没有痕迹正文", len(player_prompts))
ok(any("只有 DM 看得到" in r.prompt for r in reqs if r.role == "dm"),
   "G6 DM 提示词里有痕迹简报")


# --------------------------------------------------------------------------- #
section("H. 存档：发现状态跟着整局走")

doc = dump_game(state=st7, ledger=loop7.ledger, loop=loop7)
sess = load_from_dict(doc)
w9 = sess.loop.world
ok(w9.clue("k1") is not None, "H1 读档后线索还在")
ok(w9.clue("k1")["已发现"] == loop7.world.clue("k1")["已发现"],
   "H2 发现状态还原", (w9.clue("k1")["已发现"],
                       loop7.world.clue("k1")["已发现"]))
ok(w9.clue_tried_by("k1") == loop7.world.clue_tried_by("k1"),
   "H3 谁试过也还原", (w9.clue_tried_by("k1"),
                      loop7.world.clue_tried_by("k1")))
w9.mark_clue_found("k1", by="艾尔玛", round=2)
doc2 = dump_game(state=sess.state, ledger=sess.ledger, loop=sess.loop)
sess2 = load_from_dict(doc2)
ok(sess2.loop.world.clue("k1")["已发现"] is True
   and sess2.loop.world.clue("k1")["发现者"] == "艾尔玛",
   "H4 ⭐ 发现了也读得回来（不会读档后又能「第一次发现」）")


# --------------------------------------------------------------------------- #
section("I. 一处容易错的细节")

ok(failure_text() == "你仔细看过、听过，没发现什么异常。",
   "I1 失败句是固定的一句，与「有没有线索」无关")
c_pub = SceneClue(id="p", text=PUBLIC_TEXT, secret=False)
c_sec = SceneClue(id="s", text=SECRET_TEXT, secret=True)
ok(success_text(ClueOutcome(c_pub, True, 15, 12), "甲") == f"甲发现了：{PUBLIC_TEXT}",
   "I2 公开痕迹用第三人称")
ok(success_text(ClueOutcome(c_sec, True, 15, 14), "甲") == f"你发现了：{SECRET_TEXT}",
   "I3 秘密痕迹用第二人称")
ok(ClueOutcome(c_sec, False, 9, 14).found is False
   and "楼梯" not in failure_text(), "I4 失败结果不含正文")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")