# coding=utf-8
"""U4 回归：世界状态（地点 / 派系 / 任务 / 时间）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import FakeModel  # noqa: E402
from engine.persist import load_game, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateError, StateTree, new_character  # noqa: E402
from engine.state.assembler import BLOCK_ORDER, Assembler  # noqa: E402
from engine.state.world import (  # noqa: E402
    FACTION_ATTITUDES, QUEST_STATUSES, World, WorldError, new_world,
)

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


AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def make_state(world=None) -> StateTree:
    return StateTree.from_template({
        "罗恩": new_character("罗恩", level=3, job="战士", abilities=AB,
                              hp_max=28, ac=16),
    }, current="罗恩", label="世界测试", world=world)


def demo_world(w: World) -> None:
    w.discover("旧哨塔", description="塔门半开，里面很暗。", region="荒原",
               secret="塔底有一座献祭祭坛。")
    w.discover("地下墓穴", description="潮湿的石阶向下延伸。", region="荒原")
    w.add_faction("灰狼帮", attitude="敌对", description="盘踞荒原的强盗。",
                  known=True, secret="头目其实是变形怪。")
    w.add_faction("银月议会", attitude="中立", description="遥远的施法者组织。",
                  known=False, secret="他们在找同一件东西。")
    w.open_quest("找回货物", description="商队被劫。", objective="找回三箱丝绸",
                 public=True, secret="货物里藏着一枚徽记。", round=2)
    w.open_quest("调查失踪者", description="有人夜里失踪。",
                 objective="查明失踪原因", public=False,
                 secret="失踪者被灰狼帮献祭了。", round=3)
    w.advance_time(date="第 3 日", period="傍晚", weather="阴")


# --------------------------------------------------------------------------- #
section("A. 世界树的建/补")

st = make_state()
w = World(st)
ok(not w.exists(), "A1 默认模板里没有世界树")
before = len(st.audit)
w.ensure()
ok(w.exists(), "A2 ensure() 建出世界树")
ok(len(st.audit) == before + 1, "A3 建树也走 commit（有审计）", len(st.audit))
w.ensure()
ok(len(st.audit) == before + 1, "A4 再 ensure() 不重复写审计")

st2 = make_state(new_world(current="村口", date="第 1 日"))
ok(World(st2).exists() and World(st2).current_location == "村口",
   "A5 from_template(world=...) 直接带世界树")
ok(World(st2).clock["日期"] == "第 1 日", "A6 时间字段读得到")
ok(raises(lambda: StateTree.from_template({}, world="不是对象"), StateError),
   "A7 world 不是对象 → StateError")

# 缺子字段只补，不覆盖已有值
st3 = StateTree({"角色列表": {}, "当前角色ID": "",
                 "世界": {"当前地点": "已有地点"}})
w3 = World(st3)
w3.ensure()
ok(w3.current_location == "已有地点", "A8 补子字段时不动已有值")
ok(w3.clock == {"日期": "", "时段": "", "天气": ""}, "A9 缺的时间字段补成空")

ok(make_state(new_world()).validate() == [], "A10 合法世界树自检通过")
bad_loc = StateTree({"角色列表": {}, "当前角色ID": "",
                     "世界": {"当前地点": "龙巢", "地点": {}}})
ok(any("不在地点表" in p for p in bad_loc.validate()),
   "A11 ⭐ 当前地点不存在 → 自检报出", bad_loc.validate())
bad_disc = StateTree({
    "角色列表": {}, "当前角色ID": "",
    "世界": {"当前地点": "旧哨塔",
             "地点": {"旧哨塔": {"已发现": False}}}})
ok(any("还没发现" in p for p in bad_disc.validate()),
   "A12 ⭐ 当前地点未发现 → 自检报出", bad_disc.validate())
bad_root = StateTree({"角色列表": {}, "当前角色ID": "", "世界": "不是对象"})
ok(any("世界」不是对象" in p for p in bad_root.validate()),
   "A13 世界根不是对象 → 自检报出", bad_root.validate())


# --------------------------------------------------------------------------- #
section("B. 地点：发现、切换、秘密")

st = make_state()
w = World(st)
w.ensure()
w.discover("旧哨塔", description="塔门半开。", region="荒原",
           secret="塔底有祭坛。")
ok(w.current_location == "旧哨塔", "B1 第一个发现的地点自动成为当前地点")
ok(w.location("旧哨塔")["已发现"] is True, "B2 标记已发现")
ok(w.discovered_locations() == ["旧哨塔"], "B3 已发现列表")

w.discover("地下墓穴", description="潮湿的石阶。", region="荒原")
ok(w.current_location == "旧哨塔", "B4 再发现一个不会改当前地点")
ok(set(w.discovered_locations()) == {"旧哨塔", "地下墓穴"}, "B5 两个都已发现")

w.travel("地下墓穴")
ok(w.current_location == "地下墓穴", "B6 travel 切换当前地点")
ok(w.travel("地下墓穴") == [], "B7 去同一个地点是空操作（不写审计）")
ok(raises(lambda: w.travel("龙巢"), WorldError), "B8 ⭐ 没发现的地点不能直接去")
try:
    w.travel("龙巢")
except WorldError as exc:
    ok("还没发现" in str(exc), "B9 错误信息说清原因", str(exc))

# 未发现但登记过的地点也不能去
w.register_location("王都", description="远处的城。", region="平原",
                    secret="国王病重。")
ok(raises(lambda: w.travel("王都"), WorldError), "B10 ⭐ 登记了但没发现 → 不能去")
ok(w.location("王都")["已发现"] is False, "B10b 预登记默认未发现")
ok(raises(lambda: w.register_location("王都"), WorldError),
   "B10c 重复登记 → 报错")

n = len(st.audit)
w.discover("旧哨塔", description="塔门已经被撞开。")
ok(w.location("旧哨塔")["描述"] == "塔门已经被撞开。", "B11 再发现会更新描述")
ok(len(st.audit) == n + 1, "B12 更新描述也留审计")

# 名字里带斜杠也不能把路径切坏
w.discover("A/B 洞窟", description="名字里有斜杠。")
ok(w.location("A/B 洞窟") is not None, "B13 ⭐ 地点名带 / 也能存取（路径转义）")
ok("A/B 洞窟" in w.discovered_locations(), "B14 带斜杠的名字在列表里")

# 秘密只给 DM
player_text = w.render("罗恩")
dm_text = w.render(DM)
ok("祭坛" in dm_text, "B15 DM 看得到地点秘密")
ok("祭坛" not in player_text, "B16 ⭐ 玩家看不到地点秘密")
ok("王都" not in player_text, "B17 ⭐ 玩家看不到未发现地点")
ok("（未发现）" in dm_text, "B18 DM 看得到未发现标记")
ok("旧哨塔" in player_text and "地下墓穴" in player_text,
   "B19 玩家看得到已发现地点")


# --------------------------------------------------------------------------- #
section("C. 派系：态度、已知、秘密")

st = make_state()
w = World(st)
w.ensure()
w.add_faction("灰狼帮", attitude="敌对", description="强盗。", known=True,
              secret="头目是变形怪。")
w.add_faction("银月议会", attitude="中立", description="施法者组织。",
              known=False, secret="他们在找徽记。")
ok(set(w.factions()) == {"灰狼帮", "银月议会"}, "C1 派系列表")
ok(raises(lambda: w.add_faction("灰狼帮"), WorldError), "C2 重复登记 → 报错")
ok(raises(lambda: w.add_faction("新帮", attitude="很坏"), WorldError),
   "C3 非法态度 → 报错")
ok(set(FACTION_ATTITUDES) == {"友好", "中立", "敌对", "未知"}, "C4 态度枚举固定")

pt = w.render("罗恩")
dt = w.render(DM)
ok("灰狼帮" in pt and "银月议会" not in pt, "C5 ⭐ 玩家只知道已听说的派系")
ok("变形怪" in dt and "变形怪" not in pt, "C6 ⭐ 派系秘密只给 DM")

w.know_faction("银月议会")
ok("银月议会" in w.render("罗恩"), "C7 know_faction 后玩家能看到")
ok(w.know_faction("银月议会") == [], "C8 已经知道就空操作")
w.set_attitude("灰狼帮", "友好")
ok(w.faction("灰狼帮")["态度"] == "友好", "C9 态度可以改变")
ok(raises(lambda: w.set_attitude("灰狼帮", "很好"), WorldError),
   "C10 非法态度 → 报错")
ok(raises(lambda: w.set_attitude("不存在", "敌对"), WorldError),
   "C11 不存在派系 → 报错")


# --------------------------------------------------------------------------- #
section("D. 任务：状态、公开/秘密、备注")

st = make_state()
w = World(st)
w.ensure()
w.open_quest("找回货物", description="商队被劫。", objective="找回三箱丝绸",
             public=True, secret="货物里有徽记。", round=2)
w.open_quest("调查失踪者", description="有人夜里失踪。",
             objective="查明原因", public=False,
             secret="被献祭了。", round=3)
ok(set(w.quests()) == {"找回货物", "调查失踪者"}, "D1 任务列表")
ok(w.quests(status="进行中") == ["找回货物", "调查失踪者"], "D2 按状态筛选")
ok(raises(lambda: w.open_quest("找回货物"), WorldError), "D3 重名任务 → 报错")
ok(raises(lambda: w.open_quest("  "), WorldError), "D4 空任务名 → 报错")

w.update_quest("找回货物", status="已完成", note="在灰狼帮营地找到。", round=4)
q = w.quest("找回货物")
ok(q["状态"] == "已完成", "D5 状态推进")
ok("灰狼帮营地" in q["备注"], "D6 备注追加")
ok(q["更新轮"] == 4, "D7 更新轮记录")
ok(raises(lambda: w.update_quest("找回货物", status="差不多了"), WorldError),
   "D8 非法状态 → 报错")
ok(raises(lambda: w.update_quest("不存在", note="x"), WorldError),
   "D9 不存在任务 → 报错")
ok(set(QUEST_STATUSES) == {"未开始", "进行中", "已完成", "失败", "搁置"},
   "D10 状态枚举固定")

pt = w.render("罗恩")
dt = w.render(DM)
ok("找回货物" in pt, "D11 公开任务玩家看得到")
ok("调查失踪者" not in pt, "D12 ⭐ 不公开任务玩家看不到")
ok("调查失踪者" in dt and "被献祭" in dt, "D13 DM 看得到隐藏任务与秘密")
ok("徽记" not in pt, "D14 ⭐ 任务秘密不进玩家视角")


# --------------------------------------------------------------------------- #
section("E. 时间")

st = make_state()
w = World(st)
w.ensure()
n = len(st.audit)
w.advance_time(date="第 3 日", period="傍晚", weather="阴")
ok(w.clock == {"日期": "第 3 日", "时段": "傍晚", "天气": "阴"}, "E1 时间写入")
ok(len(st.audit) == n + 1, "E2 推进时间留审计")
w.advance_time(date="第 3 日", period="傍晚", weather="阴")
ok(len(st.audit) == n + 1, "E3 值没变不写审计")
w.advance_time(period="夜里")
ok(w.clock["时段"] == "夜里" and w.clock["日期"] == "第 3 日",
   "E4 只改一个字段不影响别的")
ok("第 3 日" in w.render(DM) and "阴" in w.render(DM), "E5 渲染带时间")


# --------------------------------------------------------------------------- #
section("F. 渲染与装配器")

st = make_state()
w = World(st)
w.ensure()
demo_world(w)
pt = w.render("罗恩")
dt = w.render(DM)
for secret in ("祭坛", "变形怪", "徽记", "献祭"):
    ok(secret not in pt, f"F1 ⭐ 玩家视角不含秘密「{secret}」")
ok("祭坛" in dt and "变形怪" in dt and "徽记" in dt,
   "F2 DM 视角含全部秘密")
ok("【当前地点】" in dt and "【地点】" in dt and "【派系】" in dt and "【任务】" in dt,
   "F3 渲染有四个分区")

empty = StateTree.from_template({})
ok(World(empty).render(DM) == "", "F4 没有世界树 → 渲空串（不塞空块）")

asm = Assembler(st)
asm.set_block("说明", "你是 DM")
asm.set_block("规则", "规则片段")
asm.set_block("世界", w.render(DM))
asm.set_block("角色卡", st.render("罗恩"))
a = asm.build(DM, task="描述场景")
ok("世界" in BLOCK_ORDER, "F5 BLOCK_ORDER 里有「世界」")
ok(BLOCK_ORDER.index("规则") < BLOCK_ORDER.index("世界")
   < BLOCK_ORDER.index("角色卡"), "F6 ⭐ 世界块在规则之后、角色卡之前")
ok(a.has("世界") and "祭坛" in a.text(), "F7 装配进去的是 DM 视角")

asm2 = Assembler(st)
asm2.set_block("说明", "你是玩家")
asm2.set_block("世界", w.render("罗恩"))
asm2.set_block("角色卡", st.render("罗恩"))
a2 = asm2.build("罗恩")
ok("祭坛" not in a2.text(), "F8 ⭐ 玩家装配包里没有秘密")


# --------------------------------------------------------------------------- #
section("G. 审计：每一次世界改动都可查")

st = make_state()
w = World(st)
w.ensure()
n0 = len(st.audit)
w.discover("码头", description="湿滑的石阶。")
w.add_faction("盐帮", attitude="中立", known=True)
w.open_quest("护送商队", objective="安全送到北门", round=2)
w.update_quest("护送商队", note="出发了。", round=3)
w.advance_time(period="清晨")
ok(len(st.audit) == n0 + 5, "G1 五次写操作 = 五条审计", len(st.audit) - n0)
actors = {e.actor for e in st.audit[n0:]}
reasons = [e.reason for e in st.audit[n0:]]
ok(actors <= {"DM", "引擎"}, "G2 审计记了 actor", actors)
ok(any("地点" in r for r in reasons) and any("任务" in r for r in reasons),
   "G3 审计记了 reason", reasons)


# --------------------------------------------------------------------------- #
section("H. 存档：世界树跟着状态树一起走")

st = make_state()
w = World(st)
w.ensure()
demo_world(w)
led = Ledger()
led.public("公告", "你们在荒原上。", section="公告", round=1)
loop = GameLoop(st, led, FakeModel(), rng=None, label="世界存档")
loop.set_field([make_hero_unit(st, "罗恩")])

dm_before = w.render(DM)
pt_before = w.render("罗恩")
with tempfile.TemporaryDirectory(prefix="dnd-world-save-") as tmp:
    path = os.path.join(tmp, "w.json")
    save_game(path, state=st, ledger=led, loop=loop)
    sess = load_game(path)
    w2 = World(sess.state)
    ok(w2.exists(), "H1 读档后世界树还在")
    ok(w2.render(DM) == dm_before, "H2 ⭐ DM 视角世界渲染一致")
    ok(w2.render("罗恩") == pt_before, "H3 玩家视角世界渲染一致")
    ok(w2.quest("找回货物")["状态"] == "进行中", "H4 任务状态保留")
    ok(w2.current_location == "旧哨塔", "H5 当前地点保留")
    ok(w2.clock["日期"] == "第 3 日", "H6 时间保留")
    ok("祭坛" in w2.render(DM) and "祭坛" not in w2.render("罗恩"),
       "H7 ⭐ 读档后秘密仍然只给 DM")

    # 读档后继续推进世界，再存再读
    w2.update_quest("找回货物", status="已完成", note="找到了。", round=5)
    path2 = os.path.join(tmp, "w2.json")
    save_game(path2, state=sess.state, ledger=sess.ledger, loop=sess.loop)
    sess2 = load_game(path2)
    ok(World(sess2.state).quest("找回货物")["状态"] == "已完成",
       "H8 读档后改世界、再存再读仍正确")

# 世界树也进 StateTree 的 JSON 往返
d = json.loads(json.dumps(st.to_dict(include_audit=True), ensure_ascii=False))
st3 = StateTree.from_dict(d)
ok(World(st3).render(DM) == dm_before, "H9 StateTree JSON 往返世界一致")


# --------------------------------------------------------------------------- #
section("I. 接进 GameLoop：DM 的包有秘密，玩家的包没有")

class CaptureModel(FakeModel):
    """记录每次请求，但不改行为（零花费）。"""

    def __init__(self) -> None:
        super().__init__(name="capture")
        self.reqs = []

    def _complete(self, req):
        self.reqs.append(req)
        return super()._complete(req)


st = make_state()
w = World(st)
w.ensure()
demo_world(w)
cm = CaptureModel()
loop = GameLoop(st, Ledger(), cm, rng=None, label="世界接入")
loop.set_field([make_hero_unit(st, "罗恩")])
loop.run_explore_round(pc_name="罗恩", situation="你们在旧哨塔前。")
dm_prompt = next(r.prompt for r in reversed(cm.reqs) if r.role == "dm")
pc_prompt = next(r.prompt for r in reversed(cm.reqs) if r.role == "player")
ok("祭坛" in dm_prompt, "I1 ⭐ DM 的上下文里有地点秘密")
ok("祭坛" not in pc_prompt, "I2 ⭐ 玩家的上下文里没有地点秘密")
ok("变形怪" not in pc_prompt, "I3 ⭐ 玩家的上下文里没有派系秘密")
ok("旧哨塔" in pc_prompt, "I4 玩家的上下文里有当前地点")
ok(hasattr(loop, "world") and loop.world.exists(), "I5 GameLoop 持有一层 World")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
