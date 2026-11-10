# coding=utf-8
"""P5-G 回归：游戏内时钟 / 记忆条目 / 按内容召回 / 时间分组 / 接线。

守的三件核心事（也是这一层最容易"看起来对"的地方）：
  A. **边界含边界**：120 是 now、121 是 today；1440 是 today、1441 是 days；
  B. **先按内容挑、再算时间**：一分钟前的无关小事不许进，两年前的欠条该进；
  C. **按视角过滤**：DM 专属记忆不许出现在玩家包里（与台账同一条纪律）。
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

import re  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure.model import Adventure, Scene  # noqa: E402
from engine.adventure.runner import AdventureRunner  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.memory import (  # noqa: E402
    BUCKET_ROWS, ClockError, GROUP_LABELS, GROUP_ORDER, GameTime, MemoryEntry,
    MemoryError, MemoryStore, Query, TIME_WORDS, bucket_of, diff_minutes,
    display_of, known_periods, match_text, parse_clock, parse_date,
    parse_game_time, recall, recall_text, render, score_entry, time_words,
)
from engine.memory.clock import PERIOD_HOURS  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402
from engine.state.assembler import BLOCK_ORDER, Assembler  # noqa: E402
from engine.state.world import World, new_world  # noqa: E402

FAILS: list[str] = []
COUNT = 0


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


def _tier_of(score: float) -> str:
    from engine.memory.recall import _tier
    return _tier(score)


# --------------------------------------------------------------------------- #
section("A. 游戏内时钟的解析")

ok(parse_date("1492-09-17").isoformat() == "1492-09-17", "A1 标准日期认得出")
ok(parse_date("1492/9/17").isoformat() == "1492-09-17", "A2 斜杠写法也认")
ok(parse_date("第 3 日") is None, "A3 ⭐ 叙事日期（第 3 日）认不出 → None，不猜")
ok(parse_date("1492-13-40") is None, "A4 非法日期 → None")
ok(parse_clock("14:30") == 870, "A5 具体时刻 14:30 → 870 分钟", parse_clock("14:30"))
ok(parse_clock("午后") == 14 * 60, "A6 时段「午后」→ 14:00", parse_clock("午后"))
ok(parse_clock("半夜三点") is None, "A7 ⭐ 表外时段 → None（不猜大概是凌晨）")
ok(parse_clock("") is None, "A8 空串 → None")

gt = parse_game_time("1492-09-17", "午后")
ok(gt.known and gt.clock_text() == "14:00", "A9 日期+时段拼成一个时刻", gt.iso())
ok(parse_game_time("", "午后").known is False, "A10 缺日期 → 不知道（不做部分推算）")
ok(raises(lambda: GameTime(minutes=1440), ClockError), "A11 分钟越界直接报错")
ok(len(known_periods()) >= 10, "A12 时段表是显式常量，不少于 10 条",
   len(known_periods()))

ok(parse_game_time("1492-09-17", "14:30").minutes == 870,
   "A13 具体时刻可以直接给（不必依赖时段表）")


# --------------------------------------------------------------------------- #
section("B. 分档边界（含边界，逐条钉住）")

CASES = [
    (0, True, "now"), (120, True, "now"),
    (121, True, "today"), (1440, True, "today"),
    (1441, True, "days"),
    (0, False, "days"), (5, False, "days"), (1440, False, "days"),
    (7200, False, "days"), (7201, False, "weeks"), (30240, False, "weeks"),
    (30241, False, "months"), (86400, False, "months"),
    (86401, False, "seasons"), (259200, False, "seasons"),
    (259201, False, "years"), (2628000, False, "years"),
    (2628001, False, "old"),
]
for diff, same, want in CASES:
    got = bucket_of(diff, same_date=same)
    ok(got == want, f"B·{diff}min·同天={same} → {want}", got)

ok(bucket_of(None, same_date=True) == "unknown", "B·None → unknown")
ok(bucket_of(-5, same_date=True) == "unknown", "B·负数（记忆比现在还晚）→ unknown")
ok(len(BUCKET_ROWS) == 9, "B·边界表就是 9 行（与预设梯形表同构）", len(BUCKET_ROWS))


# --------------------------------------------------------------------------- #
section("C. 展示串（0 也要写出来）")

ok(display_of(45, "now") == "45m", "C1 now → 45m", display_of(45, "now"))
ok(display_of(0, "today") == "0h0m", "C2 ⭐ today 的 0 分也要写 0h0m")
ok(display_of(200, "today") == "3h20m", "C3 today → 3h20m")
ok(display_of(60, "today") == "1h0m", "C4 today → 1h0m（不省略 0 分）")
ok(display_of(4820, "days") == "3d8h20m", "C5 days → 3d8h20m",
   display_of(4820, "days"))
ok(display_of(1440, "days") == "1d0h0m", "C6 days 整天 → 1d0h0m")
ok(display_of(None, "unknown") == "记不清时间", "C7 unknown → 记不清时间")


# --------------------------------------------------------------------------- #
section("D. 时间措辞表")

ok(all(g in TIME_WORDS for g in GROUP_ORDER),
   "D1 每一档都有时间词", [g for g in GROUP_ORDER if g not in TIME_WORDS])
ok("刚刚" in time_words("now"), "D2 now 那组含「刚刚」")
ok(time_words("不存在的档") == TIME_WORDS["unknown"],
   "D3 不认识的档退回 unknown 那组（不 KeyError）")
ok(all(g in GROUP_LABELS for g in GROUP_ORDER), "D4 每一档都有中文明细名")
ok(PERIOD_HOURS.get("午后") == 14, "D5 时段表可读（不是藏在函数里的魔法数）")


# --------------------------------------------------------------------------- #
section("E. 记忆条目与登记")

store = MemoryStore(label="测试记忆")
e1 = store.record("在哨塔门口发现了新鲜脚印。",
                  start=parse_game_time("1492-09-17", "午后"),
                  people=("艾尔玛", "罗恩"), places=("废弃哨塔",), tags=("t1",))
ok(e1.id == "AM0001", "E1 第一条编号 AM0001", e1.id)
ok(e1.end == e1.start, "E2 没给结束时间就与开始时间相同")
ok(store.record("第二条。").id == "AM0002", "E3 编号递增")
before = len(store)
again = store.record("重复登记同一场（应被幂等键挡住）。", key="s1")
store.record("第一次用 s1 这个键。", key="s1")
dup = store.record("又跑了一次同一场。", key="s1")
ok(len(store) == before + 1, "E4 ⭐ 同一幂等键不重复登记", len(store))
ok(dup.id == store.by_key("s1").id, "E5 幂等命中返回原来那条")
ok(again.id == "AM0003", "E6 空键不参与幂等（照常新增）")

ok(raises(lambda: store.record(""), MemoryError), "E7 空摘要报错")
ok(raises(lambda: store.record("推进关系"), MemoryError),
   "E8 ⭐ 空泛摘要（推进关系）等于没写，报错")
ok(raises(lambda: store.record("，。、"), MemoryError),
   "E9 只有标点的摘要报错")
ok(raises(lambda: store.record("好摘要", visible_to="随便谁"), ValueError),
   "E10 非法可见性报错")
ok(store.record("同一个人写两遍", people=("艾尔玛", "艾尔玛")).people
   == ("艾尔玛",), "E11 人物自动去重")


# --------------------------------------------------------------------------- #
section("F. 打分与层级（不看时间）")

NOW = parse_game_time("1492-09-17", "午后")


def mk(summary, **kw):
    return MemoryEntry(id=f"AM{kw.pop('n', 1):04d}", summary=summary, **kw)


tagged = mk("欠条还在磨盘底下。", tags=("p2",), people=("艾尔玛",),
            places=("村子",))
s, why = score_entry(tagged, Query(tags=("p2",), viewer=DM, now=NOW))
ok(s == 3.0, "F1 共享线索 = 3 分", (s, why))
ok("共享线索" in why, "F2 理由里写清是共享线索", why)

clue = mk("门轴上刻着一个名字。", tags=("clue:门轴",))
s2, _ = score_entry(clue, Query(tags=("clue:门轴",), viewer=DM))
ok(_tier_of(s2) == "REAL", "F3 物证线索也是 REAL", s2)

crowd = mk("你们一起从村口出发。", people=("艾尔玛", "罗恩"))
s3, _ = score_entry(crowd, Query(people=("艾尔玛", "罗恩"), viewer=DM))
ok(s3 == 4.0 and _tier_of(s3) == "REAL", "F4 同场两个人 = 4 分 REAL", s3)

one = mk("布兰替你们挡了一箭。", people=("布兰",))
s4, _ = score_entry(one, Query(people=("布兰",), viewer=DM))
ok(s4 == 2.0 and _tier_of(s4) == "WEAK", "F5 只共享一个人 = 2 分 WEAK", s4)

only_place = mk("废弃哨塔的门是半开的。", places=("废弃哨塔",))
s5, _ = score_entry(only_place, Query(places=("废弃哨塔",), viewer=DM))
ok(s5 == 1.0 and _tier_of(s5) == "FALLBACK", "F6 只共享地点 = 1 分 补位", s5)

none = mk("完全无关的往事：北境的雪。", people=("某人",))
s6, _ = score_entry(none, Query(people=("艾尔玛",), viewer=DM))
ok(s6 == 0.0, "F7 毫无重合 = 0 分（不进召回）", s6)

by_text = mk("布兰在门口抽着烟。", people=("布兰",))
s7, _ = score_entry(by_text, Query(text="布兰在门口", viewer=DM))
ok(s7 == 1.0, "F8 只在情境文本里点到名 = 1 分（弱证据）", s7)

ok(_tier_of(3.0) == "REAL" and _tier_of(2.0) == "WEAK"
   and _tier_of(1.0) == "FALLBACK", "F9 阈值 3/2/1 分档")


def _tier_of(score: float) -> str:
    from engine.memory.recall import _tier
    return _tier(score)


# 召回：数量、排序、可复现
store2 = MemoryStore(label="召回测试")
a = store2.record("线索强的往事。", tags=("t1",), people=("艾尔玛",))
b = store2.record("只有一个人。", people=("艾尔玛",))
c = store2.record("只有地点。", places=("哨塔",))
d = store2.record("毫无关系。", people=("陌生人",))
r = recall(store2, Query(tags=("t1",), people=("艾尔玛",), places=("哨塔",),
                         viewer=DM, now=NOW), count=6)
ok(r.ids() == ["AM0001", "AM0002", "AM0003"],
   "F10 ⭐ 只有真有重合的才入选，0 分那条不硬凑", r.ids())
ok(not r.has("AM0004"), "F11 0 分的记忆不在结果里")
ok(r.available == 4, "F12 available 记录了这个视角总共几条可选", r.available)
r1 = recall(store2, Query(tags=("t1",), people=("艾尔玛",),
                          places=("哨塔",), viewer=DM, now=NOW), count=2)
ok(len(r1) == 2, "F13 count 真的截断了", len(r1))
ok(recall(store2, Query(tags=("t1",), viewer=DM), count=0).ids() == [],
   "F14 count=0 → 空结果（不报错）")
ok(raises(lambda: recall(store2, Query(viewer=DM), count=-1), ValueError),
   "F15 count 负数报错")


# --------------------------------------------------------------------------- #
section("G. ⭐ 只按内容挑，绝不按时间远近")

gs = MemoryStore(label="时间无关性")
near_irrelevant = gs.record("一分钟前：某人打了个哈欠。",
                            start=parse_game_time("1492-09-17", "13:59"),
                            people=("路人甲",))
far_relevant = gs.record("两年前：磨坊主把欠条押给了你们。",
                         start=parse_game_time("1490-09-17", "午后"),
                         tags=("p2",), people=("磨坊主",))
q = Query(tags=("p2",), text="欠条在哪", viewer=DM, now=NOW)
res = recall(gs, q, count=6)
ok(res.ids() == [far_relevant.id],
   "G1 ⭐ 一分钟前的无关小事落选，两年前的欠条入选", res.ids())
ok(not res.has(near_irrelevant.id),
   "G2 ⭐ 「近」本身不加分（不是从最近往前数）")
ok(res.items[0].group == "years",
   "G3 选完之后才算时间：两年前归 years 档（不是「刚刚」）",
   res.items[0].group)
ok(re.fullmatch(r"\d+d\d+h\d+m", res.items[0].display) is not None,
   "G4 展示串算得出来", res.items[0].display)

# 分数本身与 now 无关
s_a, _ = score_entry(far_relevant, Query(tags=("p2",), viewer=DM, now=NOW))
s_b, _ = score_entry(far_relevant, Query(tags=("p2",), viewer=DM, now=None))
ok(s_a == s_b, "G5 同一个问题、不同 now，分数不变（时间不参与打分）", (s_a, s_b))

# 同分时按登记顺序，可复现
same1 = gs.record("同分甲。", places=("哨塔",))
same2 = gs.record("同分乙。", places=("哨塔",))
r2 = recall(gs, Query(places=("哨塔",), viewer=DM, now=NOW), count=6)
ok(r2.ids() == [same1.id, same2.id], "G6 同分按登记顺序（稳定可复现）", r2.ids())
r3 = recall(gs, Query(places=("哨塔",), viewer=DM, now=NOW), count=6)
ok(r3.ids() == r2.ids(), "G7 两次召回完全一致")


# --------------------------------------------------------------------------- #
section("H. 视角过滤（与台账同一条纪律）")

vs = MemoryStore(label="可见性")
pub = vs.record("公开的脚印。", places=("哨塔",), visible_to="public")
sec = vs.record("DM 才知道：队长是内应。", places=("哨塔",), visible_to="dm")
eng = vs.record("引擎暗骰：d20=3。", places=("哨塔",), visible_to="engine")
priv = vs.record("只有罗恩记得：他见过那封信。", places=("哨塔",),
                 visible_to="pc:罗恩")

rp = recall(vs, Query(places=("哨塔",), viewer="艾尔玛", now=NOW), count=9)
rd = recall(vs, Query(places=("哨塔",), viewer=DM, now=NOW), count=9)
rr = recall(vs, Query(places=("哨塔",), viewer="罗恩", now=NOW), count=9)
ok(rp.has(pub.id), "H1 玩家能看到公开记忆")
ok(not rp.has(sec.id), "H2 ⭐ DM 专属记忆不在玩家召回里")
ok(not rp.has(eng.id) and not rd.has(eng.id),
   "H3 ⭐ engine 档连 DM 都不给")
ok(not rp.has(priv.id), "H4 ⭐ 别人的私有记忆不在艾尔玛召回里")
ok(rr.has(priv.id), "H5 罗恩自己那条能看到")
ok(rd.has(sec.id) and rd.has(pub.id), "H6 DM 看得到秘密")
ok(len(vs.visible_to("艾尔玛")) == 1 and len(vs.visible_to(DM)) == 3,
   "H7 visible_to 计数对得上",
   (len(vs.visible_to("艾尔玛")), len(vs.visible_to(DM))))

txt = recall_text(vs, Query(places=("哨塔",), viewer="艾尔玛", now=NOW))
ok("内应" not in txt and "暗骰" not in txt,
   "H8 ⭐ 渲染出来的文本里根本没有 DM 秘密")

txt_dm = recall_text(vs, Query(places=("哨塔",), viewer=DM, now=NOW))
ok("内应" in txt_dm, "H9 DM 的渲染里有秘密（它是主持人）")


# --------------------------------------------------------------------------- #
section("I. 渲染块")

rs = MemoryStore(label="渲染")
rs.record("刚刚发生：门轴响了。", start=parse_game_time("1492-09-17", "13:59"),
          tags=("t1",))
rs.record("很久以前：雪夜丢了剑。", start=None, tags=("t1",))
res = recall(rs, Query(tags=("t1",), viewer=DM, now=NOW), count=6)
text = render(res)
ok("【记忆 · 时间召回】" in text, "I1 有标题（且说明是按时内容挑的）")
ok("—— 刚刚" in text, "I2 按时间档分组，组名是中文", text[:80])
ok("AM0001｜1m｜" in text, "I3 每行是 编号｜距今｜摘要", text)
ok("REAL" not in text and "WEAK" not in text,
   "I4 ⭐ 层级/类型这种复盘信息不进提示词")
ok("不要念出 AM 编号" in text, "I5 有「别念编号」的约束")
ok("【时间措辞】" in text, "I6 给了可用的时间词")
ok(render(recall(MemoryStore(), Query(viewer=DM), count=3)) == "",
   "I7 没有相关记忆 → 空串（装配器标「没有可召回」）")
long_text = render(res, max_chars=30)
ok("已截断" in long_text, "I8 超预算会截断并明说", long_text)
ok("类型" in match_text(res) and "AM0001" in match_text(res),
   "I9 复盘对账表（match_text）才有类型与理由")


# --------------------------------------------------------------------------- #
section("J. 存档往返")

st = MemoryStore(label="往返")
st.record("门口有脚印。", start=parse_game_time("1492-09-17", "午后"),
          people=("艾尔玛",), places=("哨塔",), tags=("t1", "p2"),
          visible_to="dm", source="s1", key="s1", offstage=True)
d = st.to_dict()
st2 = MemoryStore.from_dict(d)
ok(len(st2) == 1 and st2.entries[0].id == "AM0001", "J1 往返条数/编号不变")
e = st2.entries[0]
ok(e.summary == "门口有脚印。" and e.tags == ("t1", "p2")
   and e.visible_to.tags == frozenset({"dm"}) and e.key == "s1"
   and e.start.iso() == "1492-09-17 14:00" and e.offstage is True,
   "J2 字段逐项还原", e.to_dict())
ok(st2.record("新的一条。").id == "AM0002", "J3 往返后编号接着涨")

with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "memory.json")
    st.save(p)
    st3 = MemoryStore.load(p)
    ok(len(st3) == 1 and st3.entries[0].summary == "门口有脚印。",
       "J4 原子落盘 + 读回")
    ok(not [f for f in os.listdir(td) if f.endswith(".tmp")],
       "J5 不留临时文件", os.listdir(td))
    bad = os.path.join(td, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ not json")
    ok(raises(lambda: MemoryStore.load(bad, missing_ok=False), MemoryError),
       "J6 坏档说清是坏档")
    ok(raises(lambda: MemoryStore.load(os.path.join(td, "nope.json"),
                                      missing_ok=False), MemoryError),
       "J7 缺文件且不许缺 → 报错")
    ok(len(MemoryStore.load(os.path.join(td, "nope.json"))) == 0,
       "J8 缺文件默认给空库（新开局）")

ok(raises(lambda: MemoryStore.from_dict({"format": "别的", "version": 1}),
          MemoryError), "J9 不是本项目的记忆库 → 拒绝")
ok(raises(lambda: MemoryStore.from_dict({"format": st.to_dict()["format"],
                                        "version": 99}), MemoryError),
   "J10 版本不对 → 拒绝，不猜")


# --------------------------------------------------------------------------- #
section("K. 装配器接线（不许绕过）")

ok("记忆" in BLOCK_ORDER, "K1 BLOCK_ORDER 里有「记忆」")
ok(BLOCK_ORDER.index("记忆") == BLOCK_ORDER.index("台账") + 1,
   "K2 紧跟在台账之后")
ok(BLOCK_ORDER.index("记忆") < BLOCK_ORDER.index("可选行动"),
   "K3 在菜单之前（过去的账在前，当下能做什么在后）")

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
stree = StateTree.from_template({
    "艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                            hp_max=28, ac=16),
}, current="艾尔玛", world=new_world(date="1492-09-17", period="午后"))
led = Ledger()
led.public("公告", "你们站在哨塔前。", section="公告", round=1)

a0 = Assembler(stree, led)
ok(raises(lambda: a0.set_block("记忆", "手动塞记忆"), ValueError),
   "K4 ⭐ 记忆块不许手动塞（防全知的落点）")
blocked = a0.build("艾尔玛")
ok(("记忆", "没提供记忆库") in blocked.excluded,
   "K5 没提供记忆库 → excluded 里说明", blocked.excluded)

a1 = Assembler(stree, led, memory_provider=lambda v: (
    "【记忆】只有艾尔玛能看到的一条" if v == "艾尔玛" else ""))
b_elma = a1.build("艾尔玛")
b_dm = a1.build(DM)
ok(b_elma.has("记忆") and "只有艾尔玛" in b_elma.text(),
   "K6 provider 给的文本进了包")
ok(not b_dm.has("记忆"), "K7 provider 对别的视角返回空 → 不挂块")
ok(("记忆", "这个视角没有需要召回的往事") in b_dm.excluded,
   "K8 excluded 写清为什么没有")

a2 = Assembler(stree, led, memory_provider=lambda v: "记忆块")
a2.set_block("可选行动", "菜单")
a2.set_block("角色卡", stree.render("艾尔玛"))
order = [x.name for x in a2.build("艾尔玛").blocks]
ok(order.index("记忆") < order.index("可选行动"), "K9 真实装配顺序正确", order)


# --------------------------------------------------------------------------- #
section("L. 循环接线（记忆块真的进了提示词）")

client = FakeModel(name="memory")
loop0 = GameLoop(stree, led, client, rng=Random(11))
ok(loop0._memory_text(DM) == "", "L1 没挂记忆库 → 空串")

mem = MemoryStore(label="循环记忆")
mem.record("哨塔门口有新鲜脚印。", start=parse_game_time("1492-09-17", "13:00"),
           places=("哨塔",), people=("艾尔玛",), tags=("t1",))
loop = GameLoop(stree, led, client, rng=Random(11), memory=mem, recall_count=4)
World(stree).ensure()
World(stree).discover("哨塔")
World(stree).travel("哨塔", actor="DM")
loop.set_field([make_hero_unit(stree, "艾尔玛", uid="艾尔玛")])
loop.memory_tags = ("t1",)
loop._situation = "你们走到哨塔门口。"

text = loop._memory_text("艾尔玛")
ok("新鲜脚印" in text, "L2 循环的 provider 能把记忆召回出来", text[:60])
q = loop.memory_query("艾尔玛")
ok(q.viewer == "艾尔玛" and "哨塔" in q.places and "t1" in q.tags,
   "L3 问句带上了地点与线索", (q.places, q.tags))
ok(q.now is not None and q.now.known, "L4 问句带上了游戏内时刻", q.now)

asm = loop._assembler()
asm.set_block("角色卡", stree.render("艾尔玛"))
pack = asm.build("艾尔玛", task="轮到你。")
ok(pack.has("记忆") and "脚印" in pack.text(), "L5 装配出来的包里有记忆块")
ok(pack.has("台账") and pack.has("记忆") and pack.has("任务"),
   "L6 台账/记忆/任务都在", pack.included)

class _Capture(FakeModel):
    """记下每次请求（假模型，零花费）。"""

    def __init__(self) -> None:
        super().__init__(name="capture-memory")
        self.reqs = []

    def _complete(self, req):
        self.reqs.append(req)
        return super()._complete(req)


capture = _Capture()
loop_c = GameLoop(stree, led, capture, rng=Random(11), memory=mem,
                  recall_count=4)
loop_c.set_field([make_hero_unit(stree, "艾尔玛", uid="艾尔玛")])
loop_c.run_explore_round(pc_name="艾尔玛", situation="你们走到哨塔门口。")
player_prompts = [r.prompt for r in capture.reqs if r.role == "player"]
ok(player_prompts and "新鲜脚印" in player_prompts[-1],
   "L7 ⭐ 真实发出的提示词里带上了记忆")


# --------------------------------------------------------------------------- #
section("M. 剧本接线（一场一条，幂等）")

adv = Adventure(title="测试剧本", scenes=[Scene(
    id="s1", title="荒径与哨塔",
    situation="你们沿着荒径走到废弃哨塔前。",
    threads=["t1"], promises=["p2"],
    world={"location": "废弃哨塔", "date": "1492-09-17", "period": "午后"},
)])
stree2 = StateTree.from_template({
    "艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                            hp_max=28, ac=16),
}, current="艾尔玛", world=new_world())
loop2 = GameLoop(stree2, Ledger(), FakeModel(name="adv"), rng=Random(12),
                 memory=MemoryStore(label="剧本记忆"))
loop2.set_field([make_hero_unit(stree2, "艾尔玛", uid="艾尔玛")])
runner = AdventureRunner(loop2, adv)
runner.run_scene(adv.scene("s1"))
store2b = loop2.memory
ok(len(store2b) == 1, "M1 跑完一场记了一条", len(store2b))
m = store2b.entries[0]
ok(m.key == "s1" and m.source == "s1", "M2 幂等键/来源都是场景 id")
ok(m.summary.startswith("《荒径与哨塔》"), "M3 摘要以场景标题打头", m.summary)
ok("t1" in m.tags and "p2" in m.tags, "M4 线索标签带上了 thread/promise", m.tags)
ok(m.start.known and m.start.iso() == "1492-09-17 14:00",
   "M5 时间来自剧本的 world.date/period", m.start.iso())
ok(m.visible_to.tags == frozenset({"public"}), "M6 亲历的一场是公开记忆")
ok(loop2.memory_tags == ("t1", "p2"), "M7 循环记住了当前线索", loop2.memory_tags)

runner.run_scene(adv.scene("s1"))
ok(len(store2b) == 1, "M8 ⭐ 同一场重跑不重复记（幂等）", len(store2b))

adv_no_time = Adventure(title="无时间剧本", scenes=[Scene(
    id="s9", title="无时间的场", situation="某处。", world={})])
runner2 = AdventureRunner(loop2, adv_no_time)
runner2.run_scene(adv_no_time.scene("s9"))
m2 = store2b.by_key("s9")
ok(m2 is not None and m2.has_time,
   "M9 剧本没写时间 → 退回世界树当前时间（世界树是唯一真相）",
   m2.start.iso() if m2 else "")

# 世界树也没有时间 → 才是"时间不明"
stree3 = StateTree.from_template({
    "艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                            hp_max=28, ac=16),
}, current="艾尔玛", world=new_world())
loop3 = GameLoop(stree3, Ledger(), FakeModel(name="notime"), rng=Random(13),
                 memory=MemoryStore(label="无时记忆"))
loop3.set_field([make_hero_unit(stree3, "艾尔玛", uid="艾尔玛")])
AdventureRunner(loop3, adv_no_time).run_scene(adv_no_time.scene("s9"))
ok(not loop3.memory.by_key("s9").has_time,
   "M10 剧本与世界都没有时间 → 记成「时间不明」，不猜")


# --------------------------------------------------------------------------- #
section("N. 存档：记忆库跟着整局走")

import copy  # noqa: E402

from engine.persist import SAVE_VERSION, SaveError, dump_game, load_from_dict  # noqa: E402

doc = dump_game(state=stree, ledger=led, loop=loop_c)
ok(doc["version"] == SAVE_VERSION and SAVE_VERSION == 4,
   "N1 存档版本涨到 4（H3 新字段；v3 仍可读）", doc["version"])
sess = load_from_dict(doc)
ok(sess.loop.memory is not None and len(sess.loop.memory) == len(mem),
   "N2 记忆库随档往返", (len(mem), None if sess.loop.memory is None
                        else len(sess.loop.memory)))
ok(sess.loop.memory.entries[0].summary == mem.entries[0].summary,
   "N3 内容逐条还原")
ok(sess.loop.recall_count == loop_c.recall_count, "N4 召回数一并存")
ok(sess.loop._memory_text("艾尔玛") == loop_c._memory_text("艾尔玛"),
   "N5 读档后召回结果一致（含时间分组）")

bad_ver = copy.deepcopy(doc)
bad_ver["version"] = 1
ok(raises(lambda: load_from_dict(bad_ver), SaveError),
   "N6 ⭐ v1 旧档直接拒绝（不猜着读）")

loop_nomem = GameLoop(stree, Ledger(), FakeModel(name="nomem"), rng=Random(14))
doc2 = dump_game(state=stree, ledger=led, loop=loop_nomem)
ok(load_from_dict(doc2).loop.memory is None,
   "N7 没挂记忆库的存档读回来还是没有（不假装有一本空记忆）")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
