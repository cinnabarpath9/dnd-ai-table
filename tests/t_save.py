# coding=utf-8
"""U3 回归：存档 / 读档 / 断点续跑。"""

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

from engine.dice import Random, roll_d20  # noqa: E402
from engine.models import FakeModel  # noqa: E402
from engine.persist import (  # noqa: E402
    SAVE_FORMAT, SAVE_VERSION, GameSession, SaveError, dump_game, load_from_dict,
    load_game, save_game, unit_from_dict, unit_to_dict,
)
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

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


AB = {"力量": 8, "敏捷": 14, "体质": 12,
      "智力": 16, "感知": 12, "魅力": 10}


def rich_state() -> StateTree:
    st = StateTree.from_template({
        "罗恩": new_character("罗恩", level=3, job="法师",
                              abilities=AB, hp_max=18, ac=12),
    }, current="罗恩", label="存档测试", ui={"_主题": "dark"})
    st.commit([
        {"op": "insert", "path": "/角色列表/罗恩/物品/匕首",
         "value": {"类别": "武器", "已装备": True, "伤害公式": "1d4",
                   "伤害类型": "穿刺"}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术位/1",
         "value": {"当前": 4, "最大": 4}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术位/3",
         "value": {"当前": 1, "最大": 1}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术书/魔法飞弹",
         "value": {"等级": 1, "准备中": True, "学派": "塑能", "成分": "V、S"}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术书/火球术",
         "value": {"等级": 3, "准备中": True, "学派": "塑能", "成分": "V、S、M"}},
        {"op": "delta", "path": "/角色列表/罗恩/生命值/当前", "value": -6},
    ], actor="填表", reason="开局装备与法术")
    return st


def build_loop(st: StateTree | None = None) -> tuple[StateTree, Ledger, GameLoop]:
    state = st or rich_state()
    ledger = Ledger()
    client = FakeModel(name="fake-save-test")
    loop = GameLoop(state, ledger, client, rng=Random(20261023), label="存档测试")
    loop.set_field([make_hero_unit(state, "罗恩")])
    loop.run_explore_round(pc_name="罗恩", situation="哨塔的门半开着。")
    loop.start_combat(enemies=[("Bandit", 2)], distance=20)
    loop.run_combat_round(pc_name="罗恩")
    return state, ledger, loop


# --------------------------------------------------------------------------- #
section("A. 战斗单位往返（法术书/法术位/状态/动作都带上）")

state0 = rich_state()
unit = make_hero_unit(state0, "罗恩")
unit.add_condition("poisoned", 3)
unit.resources["action:测试"] = {"current": 2, "max": 2}
unit.resistances.add("fire")
unit.movement_used = 10.0
unit.reaction_used = True
unit.concentrating_on = "Magic Missile"
unit.notes.append("测试备注")
d0 = unit_to_dict(unit)
js = json.dumps(d0, ensure_ascii=False)
unit2 = unit_from_dict(json.loads(js))
ok(unit_to_dict(unit2) == d0, "A1 ⭐ 单位 JSON 往返完全一致")
ok(unit2.position == unit.position and isinstance(unit2.position, tuple),
   "A2 位置是 tuple", unit2.position)
ok(unit2.spells and unit2.spells[0].name == "Magic Missile",
   "A3 法术书恢复了", [s.name for s in unit2.spells])
ok({1, 3} <= set(unit2.spell_slots) and all(
    isinstance(k, int) for k in unit2.spell_slots),
   "A4 ⭐ 法术位键恢复成 int", list(unit2.spell_slots))
ok(unit2.actions[0].attacks[0].attack_bonus == unit.actions[0].attacks[0].attack_bonus,
   "A5 攻击加值恢复")
ok(unit2.conditions == {"poisoned": 3}, "A6 状态恢复", unit2.conditions)
ok("fire" in unit2.resistances, "A7 抗性恢复")
ok(unit2.concentrating_on == "Magic Missile" and unit2.reaction_used,
   "A8 专注/反应标记恢复")
ok(unit2.notes == unit.notes, "A9 备注恢复", unit2.notes)

# 缺 id → 明确报错，不造半个单位
ok(raises(lambda: unit_from_dict({"name": "没有 id"}), SaveError),
   "A10 缺关键字段 → SaveError")


# --------------------------------------------------------------------------- #
section("B. 状态树与台账往返")

st = rich_state()
d = st.to_dict(include_audit=True)
st2 = StateTree.from_dict(json.loads(json.dumps(d, ensure_ascii=False)))
ok(st2.snapshot() == st.snapshot(), "B1 stat_data 一致")
ok(len(st2.audit) == len(st.audit) == 1, "B2 ⭐ 审计流水恢复",
   (len(st.audit), len(st2.audit)))
ok(st2.audit[0].actor == "填表" and st2.audit[0].changes == st.audit[0].changes,
   "B3 审计内容一致")
ok(st2.audit[0].at == st.audit[0].at, "B4 审计时间戳保留")
st3 = StateTree.from_dict(st.to_dict(include_audit=True))
changes = st3.commit([{"op": "replace", "path": "/当前角色ID",
                       "value": "罗恩"}],
                     actor="引擎", reason="读档后继续写审计")
ok(changes and len(st3.audit) == 2 and st3.audit[1].seq == 2,
   "B4b 读档后新增审计从正确序号继续", (changes, [e.seq for e in st3.audit]))
ok("audit" not in st.to_dict(), "B5 默认 to_dict 不带审计（老口径不变）")

led = Ledger()
led.public("公告", "公开的话", section="公告", actor="DM", round=1)
led.private("对话", "只说给罗恩", "罗恩", section="对话", actor="DM", round=1)
led.engine_only("检定", "暗骰 d20=13", section="检定", actor="引擎", round=1,
                data={"roll": 13})
led2 = Ledger.from_dict(json.loads(json.dumps(led.to_dict(), ensure_ascii=False)))
ok(len(led2) == 3, "B6 台账条目数一致")
ok(len(led2.visible_to("罗恩")) == 2 and len(led2.visible_to("其他人")) == 1,
   "B7 ⭐ 可见性标签原样保留（私有没变公开）",
   (len(led2.visible_to("罗恩")), len(led2.visible_to("其他人"))))
ok(any(e.text == "暗骰 d20=13" for e in led2.visible_to("engine")),
   "B8 engine 档恢复")
ok(led2.render_omniscient() == led.render_omniscient(), "B9 全知渲染一致")
ok([e.seq for e in led2.entries] == [1, 2, 3], "B10 序号保留")
ok(led2.entries[0].round == 1 and led2.entries[0].at == led.entries[0].at,
   "B11 轮次/时间戳保留")
ok(led2.entries[2].data == {"roll": 13}, "B12 结构化 data 保留")
ok(Ledger.from_dict({"seq": 0, "entries": []}).entries == [], "B13 空台账能读")
ok(raises(lambda: Ledger.from_dict({"entries": [{"kind": "x", "text": "y"}]}),
          ValueError), "B14 缺可见性的条目 → 报错（不默认公开）")


# --------------------------------------------------------------------------- #
section("C. 整局存档 / 读档")

s1, l1, lp1 = build_loop()
before = {
    "round": lp1.round,
    "hp": {u.id: u.hp for u in lp1.field},
    "slots": {u.id: {k: v["current"] for k, v in u.spell_slots.items()}
              for u in lp1.field},
    "init": [u.id for u, _ in lp1.initiative],
    "ledger": len(l1),
    "audit": len(s1.audit),
    "rng": lp1.rng.getstate(),
    "calls": lp1.client.totals()["calls"],
    "state": s1.snapshot(),
}

with tempfile.TemporaryDirectory(prefix="dnd-save-test-") as tmp:
    path = os.path.join(tmp, "nested", "session.json")
    save_game(path, state=s1, ledger=l1, loop=lp1, note="回归测试")
    ok(os.path.isfile(path), "C1 ⭐ 自动建目录并写出文件")
    ok(not [x for x in os.listdir(os.path.dirname(path)) if x.endswith(".tmp")],
       "C2 ⭐ 原子写：没有残留临时文件")
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    ok(doc.get("format") == SAVE_FORMAT and doc.get("version") == SAVE_VERSION,
       "C3 存档带格式与版本")
    ok(doc.get("note") == "回归测试", "C4 备注保留")

    sess = load_game(path)
    ok(isinstance(sess, GameSession), "C5 读回 GameSession")
    ok(isinstance(sess.loop.client, FakeModel) and sess.loop.client.enabled,
       "C6 ⭐ 默认客户端是假模型（零花费）")
    ok(sess.loop.client.totals()["calls"] == before["calls"],
       "C6b ⭐ 调用记账随档恢复（report 不归零）",
       (sess.loop.client.totals()["calls"], before["calls"]))
    ok(sess.loop.round == before["round"], "C7 轮次一致")
    ok({u.id: u.hp for u in sess.loop.field} == before["hp"],
       "C8 ⭐ 战场 HP 一致（不是回到初始值）")
    ok({u.id: {k: v["current"] for k, v in u.spell_slots.items()}
        for u in sess.loop.field} == before["slots"], "C9 法术位一致")
    ok([u.id for u, _ in sess.loop.initiative] == before["init"],
       "C10 ⭐ 先攻序一致")
    ok(len(sess.ledger) == before["ledger"], "C11 台账条数一致")
    ok(sess.ledger.render_omniscient() == l1.render_omniscient(),
       "C12 台账内容一致")
    ok(len(sess.state.audit) == before["audit"], "C13 审计条数一致")
    ok(sess.state.snapshot() == before["state"], "C14 状态树一致")
    ok(sess.loop.rng.getstate() == before["rng"], "C15 ⭐ 骰子状态一致")
    ok(sess.warnings == [], "C16 读档后结构自检无问题")

    # 刚读完就再导出：除 saved_at 外结构应与原档一致
    doc2 = dump_game(state=sess.state, ledger=sess.ledger, loop=sess.loop)
    ok(doc2["loop"] == doc["loop"], "C17 ⭐ 再导出：回合结构完全一致")
    ok(doc2["state"] == doc["state"], "C18 再导出：状态树完全一致")
    ok(doc2["ledger"] == doc["ledger"], "C19 再导出：台账完全一致")
    ok(doc2["client"]["calls"] == doc["client"]["calls"],
       "C19b 再导出：调用记账完全一致")

    # 读档后继续跑
    calls = sess.loop.client.totals()["calls"]
    sess.loop.run_combat_round(pc_name="罗恩")
    ok(sess.loop.round == before["round"] + 1, "C20 ⭐ 读档后能继续跑下一轮")
    ok(len(sess.ledger) >= before["ledger"], "C21 续跑后台账没倒退")
    ok(sess.loop.client.totals()["calls"] > calls, "C22 续跑确实调用了（假）模型")
    ok(len(sess.ledger) >= len(doc["ledger"]["entries"]),
       "C23 续跑后原有台账没被清掉")

    # 战斗前的存档：没有先攻序，也要能读、能继续日常轮
    sx = rich_state()
    lx = Ledger()
    lpx = GameLoop(sx, lx, FakeModel(), rng=Random(31), label="日常存档")
    lpx.set_field([make_hero_unit(sx, "罗恩")])
    lpx.run_explore_round(pc_name="罗恩", situation="还在地下城门口。")
    path2 = os.path.join(tmp, "explore.json")
    save_game(path2, state=sx, ledger=lx, loop=lpx)
    sess2 = load_game(path2)
    ok(not hasattr(sess2.loop, "initiative"),
       "C24 ⭐ 战斗前存档没有先攻序（不伪造一个）")
    r2 = sess2.loop.run_explore_round(pc_name="罗恩", situation="推门进去。")
    ok(r2.calls >= 2 and sess2.loop.round == lpx.round + 1,
       "C25 ⭐ 读档后能继续日常轮", (r2.calls, sess2.loop.round))


# --------------------------------------------------------------------------- #
section("D. 坏档：每一种失败都要说清是哪种")

s2, l2, lp2 = build_loop()
good = dump_game(state=s2, ledger=l2, loop=lp2)


def broken(mutator) -> dict:
    doc = json.loads(json.dumps(good))
    mutator(doc)
    return doc


ok(raises(lambda: load_from_dict({}), SaveError), "D1 空对象 → SaveError")
try:
    load_from_dict({})
except SaveError as exc:
    ok("不是本项目" in str(exc), "D2 错误信息说「不是本项目」", str(exc))
ok(raises(lambda: load_from_dict(broken(lambda d: d.update(version=99))), SaveError),
   "D3 版本不匹配 → SaveError")
try:
    load_from_dict(broken(lambda d: d.update(version=99)))
except SaveError as exc:
    ok("版本" in str(exc) and "99" in str(exc), "D4 版本错误信息带版本号", str(exc))
ok(raises(lambda: load_from_dict(broken(lambda d: d.pop("loop"))), SaveError),
   "D5 缺 loop → SaveError")
empty = load_from_dict(broken(
    lambda d: d["loop"].update(units=[], initiative=[])))
ok(empty.loop.field == [] and empty.loop.round == lp2.round,
   "D6 空战场（无先攻）可以读")
ok(raises(lambda: load_from_dict(broken(
    lambda d: d["loop"]["units"].append(dict(d["loop"]["units"][0])))), SaveError),
   "D7 ⭐ 战场重复 id → SaveError")
ok(raises(lambda: load_from_dict(broken(
    lambda d: d["loop"]["initiative"].append(
        {"unit": "不存在", "init": 3}))), SaveError),
   "D8 先攻序里有不存在单位 → SaveError")
ok(raises(lambda: load_from_dict(broken(
    lambda d: d["loop"].update(rng={"version": 3, "internal": [1, 2]}))), SaveError),
   "D9 坏骰子状态 → SaveError")
ok(raises(lambda: load_from_dict(broken(
    lambda d: d["state"].update(stat_data="不是对象"))), SaveError),
   "D10 坏状态树 → SaveError")
ok(raises(lambda: load_from_dict(broken(
    lambda d: d["ledger"]["entries"].append({"kind": "x", "text": "y"}))), SaveError),
   "D11 坏台账条目 → SaveError")

with tempfile.TemporaryDirectory(prefix="dnd-save-bad-") as tmp:
    p = os.path.join(tmp, "bad.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ 这不是 JSON")
    try:
        load_game(p)
        ok(False, "D12 坏 JSON → SaveError")
    except SaveError as exc:
        ok("不是合法 JSON" in str(exc), "D12 坏 JSON → SaveError", str(exc))
    try:
        load_game(os.path.join(tmp, "不存在.json"))
        ok(False, "D13 文件不存在 → SaveError")
    except SaveError as exc:
        ok("读不到存档" in str(exc), "D13 文件不存在 → SaveError", str(exc))

    # 不能序列化的 data：写盘前就报，不写半个文件
    s3, l3, lp3 = build_loop()
    l3.public("公告", "坏数据", section="公告", data={"obj": object()})
    try:
        save_game(os.path.join(tmp, "x.json"), state=s3, ledger=l3, loop=lp3)
        ok(False, "D14 不可序列化 → SaveError")
    except SaveError as exc:
        ok("JSON" in str(exc), "D14 不可序列化 → SaveError", str(exc))
    ok(not os.path.exists(os.path.join(tmp, "x.json")),
       "D15 ⭐ 写失败不留下半个存档")

    ok(raises(lambda: load_from_dict(broken(
        lambda d: d["client"]["calls"].append({"seq": "不是数字"}))), SaveError),
       "D16 坏调用记账 → SaveError")
    doc_no_client = broken(lambda d: d.pop("client"))
    ok(load_from_dict(doc_no_client).loop.client.totals()["calls"] == 0,
       "D17 老档没有调用记账也能读（从 0 开始记）")


# --------------------------------------------------------------------------- #
section("E. RNG：读档后还是同一颗骰子")

rng = Random(7)
for _ in range(3):
    roll_d20(rng, 0)
d = json.loads(json.dumps({"rng": {"version": rng.getstate()[0],
                                  "internal": list(rng.getstate()[1]),
                                  "gauss": rng.getstate()[2]}}))
from engine.persist import _rng_from_dict, _rng_to_dict  # noqa: E402
r2 = _rng_from_dict(d["rng"])
ok(r2.getstate() == rng.getstate(), "E1 RNG 状态往返一致")
ok(roll_d20(r2, 0).detail == roll_d20(rng, 0).detail,
   "E2 ⭐ 读档后掷出的骰子与原来一致")
ok(_rng_from_dict(None).getstate() == Random(20261014).getstate(),
   "E3 没有 RNG 存档时用默认种子（可复现）")
ok(set(_rng_to_dict(Random(1))) == {"version", "internal", "gauss"},
   "E4 导出的 RNG 形状固定")


# --------------------------------------------------------------------------- #
print("\n=== F. 存档留痕：save_log.jsonl + history/ ===")
with tempfile.TemporaryDirectory(prefix="dnd-save-log-") as tmp:
    s4, l4, lp4 = build_loop()
    p4 = os.path.join(tmp, "slot.json")
    save_game(p4, state=s4, ledger=l4, loop=lp4, note="第一次")
    save_game(p4, state=s4, ledger=l4, loop=lp4, note="第二次")
    logp = os.path.join(tmp, "save_log.jsonl")
    ok(os.path.isfile(logp), "F1 save_log.jsonl 生成了")
    lines = [json.loads(x) for x in open(logp, encoding="utf-8") if x.strip()]
    ok(len(lines) == 2 and lines[-1]["note"] == "第二次",
       "F2 ⭐ 每次存档追加一行（含 note）", len(lines))
    with open(p4, "rb") as f:
        import hashlib
        digest = hashlib.sha256(f.read()).hexdigest()
    ok(lines[-1]["sha256"] == digest and lines[-1]["bytes"] == os.path.getsize(p4),
       "F3 ⭐ 日志记了文件指纹（能查是不是同一份）")
    hist = os.path.join(tmp, "history")
    ok(os.path.isdir(hist) and len(os.listdir(hist)) == 1,
       "F4 ⭐ 覆盖前旧档进了 history/", os.listdir(hist) if os.path.isdir(hist) else [])

    # v3 旧档可读（新字段按缺省），v2 没有迁移器必须拒
    d3 = dump_game(state=s4, ledger=l4, loop=lp4)
    d3["version"] = 3
    ok(load_from_dict(d3).state is not None, "F5 ⭐ v3 旧档仍可读（新字段按缺省）")
    d2 = dump_game(state=s4, ledger=l4, loop=lp4)
    d2["version"] = 2
    ok(raises(lambda: load_from_dict(d2), SaveError), "F6 v2 老档明确拒绝")


# --------------------------------------------------------------------------- #
print("\n=== F2. Q04 留痕容量预算：份数 + 总字节 ===")


def _hist_files(folder: str, stem: str) -> list[str]:
    h = os.path.join(folder, "history")
    if not os.path.isdir(h):
        return []
    return sorted(os.path.join(h, x) for x in os.listdir(h)
                  if x.startswith(stem + ".") and x.endswith(".json"))


with tempfile.TemporaryDirectory(prefix="dnd-budget-") as btmp:
    s5, l5, lp5 = build_loop()
    slot = os.path.join(btmp, "slot.json")
    for i in range(6):
        save_game(slot, state=s5, ledger=l5, loop=lp5, note=f"第{i}次",
                  history_keep=3, history_max_bytes=None)
    kept = _hist_files(btmp, "slot")
    ok(len(kept) == 3, "F7 ⭐ history 份数闸：keep=3 只留 3 份", len(kept))

    one = os.path.getsize(kept[-1]) if kept else 0
    cap = one * 2 + one // 4
    for i in range(4):
        save_game(slot, state=s5, ledger=l5, loop=lp5, note=f"字节{i}",
                  history_keep=99, history_max_bytes=cap)
    kept = _hist_files(btmp, "slot")
    total = sum(os.path.getsize(x) for x in kept)
    ok(one > 0 and total <= cap, "F8 ⭐ history 字节闸：总量不超上限",
       (total, cap))
    ok(0 < len(kept) <= 2, "F9 ⭐ 字节闸生效，且至少留一份（不删光）", len(kept))

    other = os.path.join(btmp, "slot-b.json")
    save_game(other, state=s5, ledger=l5, loop=lp5, note="别的槽")
    save_game(other, state=s5, ledger=l5, loop=lp5, note="别的槽2")
    ok(len(_hist_files(btmp, "slot-b")) == 1,
       "F10 ⭐ 裁剪只碰同一存档名，别的槽历史不动",
       _hist_files(btmp, "slot-b"))
    ok(len(_hist_files(btmp, "slot")) <= 2,
       "F10b 另一个槽的存档没有把 slot.json 的历史挤掉",
       len(_hist_files(btmp, "slot")))

    logdir = os.path.join(btmp, "log")
    os.makedirs(logdir, exist_ok=True)
    slot2 = os.path.join(logdir, "s.json")
    log_cap = 1500
    for i in range(8):
        save_game(slot2, state=s5, ledger=l5, loop=lp5, note=f"log{i}",
                  save_log_max_bytes=log_cap, history_keep=1)
    logp2 = os.path.join(logdir, "save_log.jsonl")
    raw = open(logp2, "rb").read()
    rows = [json.loads(x) for x in raw.decode("utf-8").splitlines() if x.strip()]
    ok(len(raw) <= log_cap, "F11 ⭐ save_log 超上限自动裁最旧的行",
       (len(raw), log_cap))
    ok(bool(rows) and rows[-1]["note"] == "log7",
       "F12 ⭐ 裁完最新一条还在", rows[-1]["note"] if rows else "")
    ok(len(rows) < 8, "F13 确实裁过（不是没触发）", len(rows))

from engine.persist import prune_kept  # noqa: E402

with tempfile.TemporaryDirectory(prefix="dnd-prune-") as ptmp:
    paths = []
    for i in range(5):
        p = os.path.join(ptmp, f"f{i}.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x" * 200)
        os.utime(p, (1000 + i * 10, 1000 + i * 10))
        paths.append(p)
    gone = prune_kept(paths, keep=2, max_bytes=None)
    left = sorted(os.path.basename(x) for x in paths if os.path.exists(x))
    ok(len(gone) == 3 and left == ["f3.json", "f4.json"],
       "F14 ⭐ prune_kept：按旧到新删、留最新（keep=2）", (len(gone), left))
    gone_all = prune_kept(paths, keep=0, max_bytes=None)
    ok(len(gone_all) == 2 and not [x for x in paths if os.path.exists(x)],
       "F15 keep=0 仍然全删（保持旧口径，不偷偷改语义）", len(gone_all))

# 外审 P2-2：历史只认「完全同槽」（stem + 时间戳），不能删掉点号前缀的别人
with tempfile.TemporaryDirectory(prefix="dnd-hist-prefix-") as htmp:
    a = os.path.join(htmp, "a.json")
    b = os.path.join(htmp, "a.b.json")
    for p in (a, b):
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}")
    hist = os.path.join(htmp, "history")
    os.makedirs(hist)
    other_hist = []
    for i in range(3):
        hp = os.path.join(hist, f"a.b.2026110{i}T000000.{i:06d}Z.json")
        with open(hp, "w", encoding="utf-8") as f:
            f.write("{}")
        os.utime(hp, (1000 + i, 1000 + i))
        other_hist.append(hp)
    save_game(a, state=s5, ledger=l5, loop=lp5, note="prefix-a",
              history_keep=1)
    left = [p for p in other_hist if os.path.exists(p)]
    ok(len(left) == len(other_hist),
       "F16 ⭐ 给 a.json 存档不会删掉 a.b.json 的历史（点号前缀不串槽）",
       (len(left), len(other_hist)))

# 外审 P2-1：日志里有坏字节时，裁剪不能把「已经存好的档」报成失败
with tempfile.TemporaryDirectory(prefix="dnd-log-bad-") as ltmp:
    lp = os.path.join(ltmp, "s.json")
    with open(lp, "w", encoding="utf-8") as f:
        f.write("{}")
    logp = os.path.join(ltmp, "save_log.jsonl")
    with open(logp, "wb") as f:
        f.write(b'{"a":1}\n' * 20)
        f.write(b"\xff\xfe broken\n")
    try:
        save_game(lp, state=s5, ledger=l5, loop=lp5, note="bad-log",
                  save_log_max_bytes=200, history_keep=1)
        crashed = ""
    except Exception as exc:                        # noqa: BLE001
        crashed = f"{type(exc).__name__}: {exc}"
    ok(not crashed and os.path.exists(lp),
       "F17 ⭐ 日志含坏字节时仍能存档成功（不抛 UnicodeDecodeError）",
       crashed)

# 外审结构项 B：备份失败不能把「已经存好」的档判失败
with tempfile.TemporaryDirectory(prefix="dnd-hist-broken-") as htmp:
    hp = os.path.join(htmp, "s.json")
    with open(hp, "w", encoding="utf-8") as f:
        f.write("{}")
    # 把 history 做成**文件**：makedirs 必然失败 → 备份失败
    with open(os.path.join(htmp, "history"), "w", encoding="utf-8") as f:
        f.write("not a dir")
    try:
        save_game(hp, state=s5, ledger=l5, loop=lp5, note="no-history",
                  history_keep=3)
        broke = ""
    except Exception as exc:                        # noqa: BLE001
        broke = f"{type(exc).__name__}: {exc}"
    ok(not broke and os.path.exists(hp),
       "F18 ⭐ history 写不进去时，存档本身仍然成功（留痕失败≠存档失败）",
       broke)

    # ---------------------------------------------------------------- #
    print("\n" + "=" * 70)
    if FAILS:
        print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
        for f in FAILS:
            print(f"  ✗ {f}")
        raise SystemExit(1)
    print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")

