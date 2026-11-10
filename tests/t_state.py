# coding=utf-8
"""状态层回归：JSONPatch / stat_data 树 / 可见性过滤。"""

from __future__ import annotations

import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")     # 控制台可能是 GBK，中文/符号会炸
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.state import (  # noqa: E402
    DM, PUBLIC, Ledger, PatchError, StateTree, Visibility, apply_patch, diff,
    lint_leaks, new_character, parse_path, pc,
)

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra: str = "") -> None:
    global COUNT
    COUNT += 1
    print(f"  {'PASS' if cond else 'FAIL'} {label}"
          f"{('  |  ' + str(extra)) if extra and not cond else ''}")
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


# --------------------------------------------------------------------------- #
section("A. JSON Pointer 解析与转义")

ok(parse_path("/角色列表/艾尔玛/生命值/当前") == ["角色列表", "艾尔玛", "生命值", "当前"],
   "A1 中文路径解析")
ok(parse_path("") == [], "A2 空路径 = 根")
ok(raises(lambda: parse_path("角色列表/艾尔玛"), PatchError),
   "A3 不以 / 开头应报错")
# RFC 6901 转义：~ 先转成 ~0，/ 转成 ~1
ok(parse_path("/a~1b") == ["a/b"], "A4 ~1 解码成 /")
ok(parse_path("/a~0b") == ["a~b"], "A5 ~0 解码成 ~")
ok(parse_path("/a~01") == ["a~1"], "A6 ~01 解码成 ~1（顺序不能反）")


# --------------------------------------------------------------------------- #
section("B. JSONPatch 基本 op")

doc = {"a": {"b": 1, "c": [10, 20, 30]}, "x": "hi"}

r = apply_patch(doc, [{"op": "replace", "path": "/a/b", "value": 5}])
ok(r["a"]["b"] == 5 and doc["a"]["b"] == 1, "B1 replace 生效且不改原对象（深拷贝）")

r = apply_patch(doc, [{"op": "add", "path": "/a/d", "value": 9}])
ok(r["a"]["d"] == 9, "B2 add 新增键")

r = apply_patch(doc, [{"op": "insert", "path": "/a/e", "value": 1}])
ok(r["a"]["e"] == 1, "B3 insert 是 add 的别名（MVU 用 insert）")

r = apply_patch(doc, [{"op": "delta", "path": "/a/b", "value": -3}])
ok(r["a"]["b"] == -2, "B4 delta 做增减")

r = apply_patch(doc, [{"op": "delta", "path": "/a/b", "value": 3}])
ok(isinstance(r["a"]["b"], int) and r["a"]["b"] == 4,
   "B5 delta 结果保持整数（不是 4.0）")

r = apply_patch(doc, [{"op": "remove", "path": "/a/b"}])
ok("b" not in r["a"], "B6 remove 删键")

r = apply_patch(doc, [{"op": "replace", "path": "/a/c/1", "value": 99}])
ok(r["a"]["c"] == [10, 99, 30], "B7 数组按下标替换")

r = apply_patch(doc, [{"op": "add", "path": "/a/c/-", "value": 40}])
ok(r["a"]["c"] == [10, 20, 30, 40], "B8 数组用 '-' 追加")

r = apply_patch(doc, [{"op": "copy", "path": "/a/f", "from": "/x"}])
ok(r["a"]["f"] == "hi", "B9 copy")

r = apply_patch(doc, [{"op": "move", "path": "/a/g", "from": "/x"}])
ok(r["a"]["g"] == "hi" and "x" not in r, "B10 move")

r = apply_patch(doc, [{"op": "test", "path": "/a/b", "value": 1}])
ok(r == doc, "B11 test 通过时状态不变")
ok(raises(lambda: apply_patch(doc, [{"op": "test", "path": "/a/b", "value": 999}]),
          PatchError), "B12 test 失败时报错")


# --------------------------------------------------------------------------- #
section("C. 事务语义（一条失败 → 整份回滚）")

d0 = {"hp": 10, "slots": {"1": 2}}
ok(raises(lambda: apply_patch(d0, [
    {"op": "delta", "path": "/hp", "value": -5},
    {"op": "delta", "path": "/slots/1", "value": 999},   # 这条是合法的
    {"op": "delta", "path": "/不存在", "value": -1},      # 这条会失败
]), PatchError), "C1 含非法 op 的 patch 报错")
ok(d0 == {"hp": 10, "slots": {"1": 2}}, "C2 原对象未被改动（不 in_place）")

d1 = {"hp": 10}
ok(raises(lambda: apply_patch(d1, [{"op": "delta", "path": "/hp", "value": -5},
                                   {"op": "delta", "path": "/nope", "value": 1}],
                              in_place=True), PatchError),
   "C3 in_place 时非法 op 报错")
ok(d1 == {"hp": 10}, "C4 ⭐ in_place 失败后**已回滚**（hp 没被扣掉）")


# --------------------------------------------------------------------------- #
section("D. 三条硬口径")

# delta 只能改数字
ok(raises(lambda: apply_patch({"name": "艾尔玛"},
                              [{"op": "delta", "path": "/name", "value": 1}],
                              in_place=True), PatchError),
   "D1 delta 不能用于字符串")
ok(raises(lambda: apply_patch({"n": "3"},
                              [{"op": "delta", "path": "/n", "value": 1}],
                              in_place=True), PatchError),
   "D2 delta 不能用于'数字的字符串'")

# 缺父对象不自动造
ok(raises(lambda: apply_patch({"a": {}},
                              [{"op": "delta", "path": "/a/b/c", "value": 1}],
                              in_place=True), PatchError),
   "D3 父对象不存在 → 报错（不自动造父对象）")
r = apply_patch({"a": {}}, [{"op": "add", "path": "/a/b", "value": {"c": 0}},
                            {"op": "delta", "path": "/a/b/c", "value": 5}])
ok(r["a"]["b"]["c"] == 5, "D4 显式先 insert 父对象再 delta → 可以")

# 下划线开头只读
ok(raises(lambda: apply_patch({"_cfg": {"x": 1}},
                              [{"op": "replace", "path": "/_cfg/x", "value": 2}],
                              in_place=True), PatchError),
   "D5 下划线开头的字段是只读，patch 不许碰")
ok(raises(lambda: apply_patch({"_cfg": 1},
                              [{"op": "replace", "path": "/_cfg", "value": 2}],
                              in_place=True), PatchError),
   "D6 只读字段本身也不许改")

# 不认识的 op
ok(raises(lambda: apply_patch({}, [{"op": "destroy", "path": "/a"}], in_place=True),
          PatchError), "D7 不认识的 op 报错（strict 模式，不静默跳过）")
r = apply_patch({}, [{"op": "destroy", "path": "/a"}], strict=False)
ok(r == {}, "D8 strict=False 时才跳过")


# --------------------------------------------------------------------------- #
section("E. diff（生成最小 patch）")

before = {"hp": 20, "name": "艾尔玛", "slots": {"1": 2}, "tags": ["a"]}
after = {"hp": 12, "name": "艾尔玛", "slots": {"1": 1}, "tags": ["a"], "new": 7}
p = diff(before, after)
ops = {(o["op"], o["path"]) for o in p}
ok(("delta", "/hp") in ops, "E1 数字变化用 delta 表达", p)
ok(("delta", "/slots/1") in ops, "E2 嵌套数字也用 delta", p)
ok(("insert", "/new") in ops, "E3 新增字段用 insert", p)
ok(all(not (o["op"] == "replace" and o["path"] == "/name") for o in p),
   "E4 没变的字段不出现在 patch 里")
ok(apply_patch(before, p) == after, "E5 diff 出来的 patch 能还原状态")

p2 = diff({"a": 1, "b": 2}, {"a": 1})
ok(p2 == [{"op": "remove", "path": "/b"}], "E6 删字段用 remove", p2)

# 引擎改完一堆，用 commit_diff 落库，审计里仍是"具体变了哪些字段"
ok(all("delta" in json.dumps(o, ensure_ascii=False) or "insert" in json.dumps(o, ensure_ascii=False)
       for o in p[:2]), "E7 diff 结果里没有整树覆盖")


# --------------------------------------------------------------------------- #
section("F. stat_data 状态树")

st = StateTree.from_template({
    "艾尔玛": new_character("艾尔玛", level=3, job="战士",
                            abilities={"力量": 16, "敏捷": 14, "体质": 15,
                                       "智力": 10, "感知": 12, "魅力": 10},
                            hp_max=28, ac=16),
    "罗恩": new_character("罗恩", level=3, job="法师",
                          abilities={"力量": 8, "敏捷": 14, "体质": 12,
                                     "智力": 16, "感知": 12, "魅力": 10},
                          hp_max=18, ac=12),
}, current="艾尔玛")

ok(st.characters() == ["艾尔玛", "罗恩"], "F1 角色列表正确", st.characters())
ok(st.hp("艾尔玛") == (28, 28, 0), "F2 读生命值", st.hp("艾尔玛"))
ok(st.read("/当前角色ID") == "艾尔玛", "F3 读当前角色ID")
ok(st.is_alive("艾尔玛"), "F4 存活判定")
ok(st.read("/角色列表/罗恩/职业") == {"法师": 3}, "F5 嵌套读职业")

# 匿名读不存在的路径 → 默认值（不是抛异常；读是宽容的）
ok(st.read("/角色列表/不存在/生命值", "default") == "default", "F6 读不存在给默认值")
ok(st.hp("罗恩")[1] == 18, "F7 罗恩最大生命 18")

# 施法位
st.commit([{"op": "insert", "path": "/角色列表/罗恩/施法/法术位/1",
            "value": {"当前": 4, "最大": 4}}], actor="填表", reason="建立法师法术位")
ok(st.spell_slots("罗恩") == {1: (4, 4)}, "F8 法术位读取", st.spell_slots("罗恩"))

# commit + 审计
changes = st.commit([{"op": "delta", "path": "/角色列表/艾尔玛/生命值/当前",
                      "value": -8}], actor="引擎", reason="地精弯刀命中")
ok(st.hp("艾尔玛") == (20, 28, 0), "F9 commit 改了状态", st.hp("艾尔玛"))
ok(len(changes) == 1 and "28 → 20" in changes[0], "F10 变化摘要写了改前改后", changes)
ok(len(st.audit) == 2, "F11 审计记了 2 条")
ok("地精弯刀" in st.audit[-1].line() and "引擎" in st.audit[-1].line(),
   "F12 审计含理由与执行者", st.audit[-1].line())

# commit 失败要回滚且不留审计
before_audit = len(st.audit)
ok(raises(lambda: st.commit([{"op": "delta", "path": "/角色列表/艾尔玛/生命值/当前",
                              "value": -100},
                             {"op": "delta", "path": "/不存在", "value": 1}],
                            actor="引擎", reason="故意失败"), PatchError),
   "F13 非法 patch 报错")
ok(st.hp("艾尔玛") == (20, 28, 0), "F14 ⭐ commit 失败后状态回滚")
ok(len(st.audit) == before_audit, "F15 失败的 commit **不留审计**（不假装改过）")

# commit_diff
new_snap = st.snapshot()
new_snap["角色列表"]["艾尔玛"]["生命值"]["当前"] = 25
new_snap["角色列表"]["艾尔玛"]["物品"]["长剑"] = {"类别": "武器", "已装备": True,
                                                  "伤害公式": "1d8", "伤害类型": "挥砍"}
st.commit_diff(new_snap, actor="引擎", reason="治疗+捡到长剑")
ok(st.hp("艾尔玛")[0] == 25, "F16 commit_diff 生效")
last = st.audit[-1]
paths = {o["path"] for o in last.patch}
ok(any("生命值/当前" in p for p in paths) and any("物品/长剑" in p for p in paths),
   "F17 commit_diff 生成的是**字段级** patch（不是整树覆盖）", paths)

# 状态字段用文字（MVU 口径：状态用文字描述，不建对象）
st.commit([{"op": "replace", "path": "/角色列表/艾尔玛/当前状态",
            "value": "中毒，持续到下回合结束；正在专注于祝福术"}],
          actor="引擎", reason="中了毒")
ok("中毒" in st.read("/角色列表/艾尔玛/当前状态"), "F18 状态字段是文字描述")

# 只读字段
ok(raises(lambda: st.commit([{"op": "replace", "path": "/_ui/theme", "value": "dark"}],
                            actor="UI", reason="换主题"), PatchError),
   "F19 状态树里也拦只读字段")

# 校验
problems = st.validate()
ok(problems == [], "F20 合法状态树校验通过", problems)

bad = StateTree({"角色列表": {"A": {"生命值": {"当前": 1}}}, "当前角色ID": "不存在"})
bp = bad.validate()
ok(any("不存在的角色" in x for x in bp), "F21 校验抓出当前角色ID 悬空", bp)
ok(any("缺" in x for x in bp), "F22 校验抓出缺字段", bp)

# 渲染
txt = st.render("艾尔玛")
ok("艾尔玛" in txt and "生命值 25/28" in txt, "F23 渲染含名字与生命值", txt)
ok("属性" in txt and "力量 16(+3)" in txt, "F24 渲染属性带调整值")
ok("长剑" in txt, "F25 渲染物品")
ok("中毒" in txt, "F26 渲染当前状态")
ok("罗恩" not in txt, "F27 ⭐ 只渲染这个人自己的（不含别人）")

rtxt = st.render("罗恩")
ok("法术位" in rtxt and "1 环 4/4" in rtxt, "F28 法师渲染出法术位", rtxt)
ok("长剑" not in rtxt, "F29 ⭐ 罗恩看不到艾尔玛的物品")

# 序列化往返
js = st.to_json()
st2 = StateTree.from_dict(json.loads(js))
ok(st2.snapshot() == st.snapshot(), "F30 JSON 序列化往返无损")
ok(raises(lambda: st.render("不存在"), Exception), "F31 渲染不存在的角色报错")


# --------------------------------------------------------------------------- #
section("G. 可见性三档")

led = Ledger()
e1 = led.public("公告", "你们走进了废弃的哨塔。", section="公告", round=1)
e2 = led.add("战斗", "哥布林袭击了艾尔玛，造成 7 点伤害。", PUBLIC,
             section="战斗", actor="哥布林", round=1)
e3 = led.private("检定", "你注意到地板上有一块松动的石板。", "艾尔玛",
                 section="你的私有", round=1)
e4 = led.secret("战斗", "哥布林头目其实是个变形怪，它想引你们深入。",
                section="战斗", round=1)

ok(e1.visible("艾尔玛") and e1.visible("罗恩") and e1.visible(DM), "G1 public 人人可见")
ok(e2.visible("罗恩"), "G2 战斗结果对全场可见")
ok(e3.visible("艾尔玛"), "G3 私有条目本人可见")
ok(not e3.visible("罗恩"), "G4 ⭐ 私有条目别人**看不到**")
ok(e3.visible(DM), "G5 ⭐ 私有条目 **DM 看得到**（它是主持人，必须知道玩家知道什么）")
ok(e4.visible(DM), "G6 dm 条目 DM 可见")
ok(not e4.visible("艾尔玛") and not e4.visible("罗恩"), "G7 ⭐ dm 条目玩家看不到")

ok(len(led.visible_to("艾尔玛")) == 3, "G8 艾尔玛能看到 3 条", len(led.visible_to("艾尔玛")))
ok(len(led.visible_to("罗恩")) == 2, "G9 罗恩能看到 2 条", len(led.visible_to("罗恩")))
ok(len(led.visible_to(DM)) == 4, "G10 ⭐ DM 能看到全部 4 条（含角色私有）",
   len(led.visible_to(DM)))

ok(raises(lambda: Visibility("秘密"), ValueError), "G11 不认识的可见性报错")
ok(raises(lambda: Visibility(), ValueError), "G12 空可见性报错（必须想清谁能看）")
ok(Visibility.of(PUBLIC, pc("艾尔玛")).allows("艾尔玛"), "G13 多标签：public 覆盖全部")


# --------------------------------------------------------------------------- #
section("H. 台账渲染（按视角）")

t_elma = led.render("艾尔玛")
ok("哨塔" in t_elma and "石板" in t_elma, "H1 艾尔玛看到公告+自己的私有")
ok("变形怪" not in t_elma, "H2 ⭐ 艾尔玛看不到 DM 的秘密")
ok("—— 公告 ——" in t_elma and "—— 你的私有 ——" in t_elma, "H3 渲染按 section 分组")

t_lynn = led.render("罗恩")
ok("石板" not in t_lynn, "H4 ⭐ 罗恩看不到艾尔玛的私有")
ok("变形怪" not in t_lynn, "H5 罗恩也看不到 DM 的秘密")

t_dm = led.render(DM)
ok("变形怪" in t_dm, "H6 DM 能看到自己的秘密")
ok("石板" in t_dm, "H7 ⭐ DM **也看得到**角色的私有（主持人视角）")
ok(len(led.visible_to(DM)) == 4, "H8 DM 可见条数 = 全部（除 engine 档）",
   len(led.visible_to(DM)))

# 截断
led2 = Ledger()
for r in range(1, 11):
    led2.public("战斗", f"第 {r} 轮的某件事发生了，写长一点避免太短。",
                section="战斗", round=r)
t = led2.render("艾尔玛", last_rounds=3)
ok("第 10 轮" in t and "第 8 轮" in t, "H8 last_rounds 保留最近 N 轮")
ok("第 7 轮" not in t, "H9 更早的被截断")

# 排除
t = led2.render("艾尔玛", exclude_seqs=[10])
ok("第 10 轮" not in t and "第 9 轮" in t, "H10 exclude_seqs 生效")

# 全知视图（只给人看）
omni = led.render_omniscient()
ok("变形怪" in omni and "石板" in omni, "H11 全知视图含全部（复盘用，不许进模型包）")


# engine 档：只有引擎/复盘看得到
e5 = led.engine_only("检定", "暗骰明细：哥布林的隐匿是 14+3=17。", section="检定")
ok(not e5.visible(DM), "H9 ⭐ engine 档连 DM 都不给（暗骰明细是给复盘对账的）")
ok(not e5.visible("艾尔玛"), "H10 engine 档玩家也看不到")
ok(e5.visible("engine"), "H11 engine 视角看得到")


# --------------------------------------------------------------------------- #
section("I. 泄漏检查（狼人杀那个 P0 的守）")

good = Ledger()
good.public("战斗", "哥布林用弯刀攻击艾尔玛，造成了 7 点伤害。", section="战斗")
good.secret("战斗", "哥布林头目其实是个变形怪，它想引你们深入哨塔深处。", section="战斗")
ok(lint_leaks(good) == [], "I1 正常的公开/私有条目不报泄漏", lint_leaks(good))

bad = Ledger()
bad.secret("战斗", "哥布林头目其实是个变形怪，它想引你们深入哨塔深处。", section="战斗")
bad.public("对话", "哥布林头目其实是个变形怪，它想引你们深入哨塔深处。",
           section="对话", actor="哥布林")
leaks = lint_leaks(bad)
ok(len(leaks) >= 1, "I2 ⭐ 私有原文被抄进公开条目 → 报泄漏", leaks)
ok("公共子串" in leaks[0], "I3 报错信息说明了判据")

# 短文本不该误报
short = Ledger()
short.secret("检定", "石板上有个陷阱。", section="检定")
short.public("公告", "石板上有个陷阱。", section="公告")
ok(lint_leaks(short) == [], "I4 短文本（<12 字）不误报", lint_leaks(short))

# 部分重合：默认阈值 12 是**偏保守**的（宁可漏报几字，也不要满屏误报）。
# 下面这段实际最长公共子串是 11 字，所以默认阈值下抓不到，调低一档才抓得到。
part = Ledger()
part.secret("检定", "你注意到地板上有一块松动的石板，下面藏着一把钥匙。", section="检定")
part.public("对话", "他把「地板上有一块松动的石板」这句话告诉了所有人。", section="对话")
ok(lint_leaks(part) == [],
   "I5 默认阈值 12 时，11 字的偶合**不误报**（保守口径）", lint_leaks(part))
ok(len(lint_leaks(part, min_len=10)) >= 1,
   "I6 ⭐ 阈值调到 10，同样两段就抓出来了（阈值可调）",
   lint_leaks(part, min_len=10))

# 归一化：加标点/改语气不能绕过检查
punc = Ledger()
punc.secret("检定", "祭坛下面压着一本用精灵语写成的法术书，封皮已经腐烂。", section="检定")
punc.public("对话", "祭坛下面压着一本用精灵语写成的法术书，封皮已经腐烂！", section="对话")
ok(len(lint_leaks(punc)) >= 1, "I7 只改标点/语气也照样抓出来（先归一化再比较）",
   lint_leaks(punc))


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
