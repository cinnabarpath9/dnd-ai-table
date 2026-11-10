# coding=utf-8
"""多槽位存档（P5-I 补完）：三个槽 + 最近存档，互不覆盖、能分别读回。

守的口径：

    · 槽位号只能是 1~3；非法号直接报错（不静默落到最近存档）；
    · 三个槽互不覆盖；`latest.json` 仍是"最近"通道；
    · 读档从"当前场景开头"继续（沿用 U3 口径）；
    · 网页设置页给每个槽一个"存/读"按钮，并显示"有档/空"。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import load_adventure  # noqa: E402
from engine.demo_web import WebSession  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.webui import PAGE_HTML, WebBridge  # noqa: E402

FAILS: list[str] = []
COUNT = 0
SAMPLE = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")
AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
WHO = "甲"


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def new_session(tmp: str) -> tuple[WebSession, StateTree, GameLoop]:
    bridge = WebBridge(timeout=1)
    adv = load_adventure(SAMPLE)
    sess = WebSession(bridge, adv, tmp)
    st = StateTree.from_template(
        {WHO: new_character(WHO, level=3, job="战士", abilities=dict(AB),
                            hp_max=24, ac=15)}, current=WHO)
    st.commit([{"op": "replace", "path": f"/角色列表/{WHO}/钱币/金币",
                "value": 60}], actor="填表", reason="开局钱")
    loop = GameLoop(st, Ledger(),
                    TableClient(dm=FakeModel(), planner=FakeModel(), players={}),
                    rng=Random(20261112))
    loop.set_field([make_hero_unit(st, WHO)])
    sess.state, sess.loop = st, loop
    sess.human_name = WHO
    return sess, st, loop


tmp = tempfile.mkdtemp(prefix="dnd-slots-")
try:
    sess, st, loop = new_session(tmp)
    ok(sess.save(1)["ok"], "A1 存槽 1 成功")
    ok(os.path.exists(sess._slot_path(1)), "A2 槽 1 文件在")

    st.commit([{"op": "replace", "path": f"/角色列表/{WHO}/钱币/金币",
                "value": 65}], actor="玩家:甲", reason="捡到 5 金币")
    ok(sess.save(2)["ok"], "A3 存槽 2 成功")
    sess.save(3)
    ok(sess.save()["ok"], "A4 「最近存档」仍可用")

    rows = sess.list_slots()
    ok([r["slot"] for r in rows] == [1, 2, 3], "A5 列出三个槽", rows)
    ok(all(r["exists"] for r in rows), "A6 三个槽都有档")
    ok(bool(rows[0]["saved_at"]), "A7 槽里有时间戳", rows[0])

    captured: dict = {}
    sess._spawn = lambda state, loop2: captured.update(state=state, loop=loop2)
    r = sess.load_slot(1)
    ok(r["ok"] and "槽 1" in r.get("message", ""), "A8 ⭐ 读槽 1 成功", r)
    ok(int(captured["state"].read(f"/角色列表/{WHO}/钱币/金币")) == 60,
       "A9 ⭐ 槽 1 是 60 金币（没被槽 2 覆盖）",
       captured["state"].read(f"/角色列表/{WHO}/钱币/金币"))

    captured.clear()
    sess.load_slot(2)
    ok(int(captured["state"].read(f"/角色列表/{WHO}/钱币/金币")) == 65,
       "A10 ⭐ 槽 2 是 65 金币（两槽各自独立）",
       captured["state"].read(f"/角色列表/{WHO}/钱币/金币"))

    ok(sess.load_slot(3)["ok"], "A11 读槽 3")
    ok(sess.load_latest()["ok"], "A12 读「最近」仍走另一条通道")

    ok(sess.load_slot(2)["ok"], "A13 覆盖槽 2（新存的新档）")

    r = sess.load_slot(0)
    ok(not r["ok"] and "1~3" in r.get("error", ""), "A14 槽 0 → 报错", r)
    ok(not sess.load_slot(4)["ok"], "A15 槽 4 → 报错")
    try:
        sess.save("x")
        raised = False
    except ValueError:
        raised = True
    ok(raised, "A16 ⭐ 槽位号不是数字 → 报错，不落到最近存档")

    saved_paths = {n: os.path.exists(sess._slot_path(n)) for n in (1, 2, 3)}
    ok(all(saved_paths.values()) and os.path.exists(sess._latest()),
       "A17 三个槽与 latest 同时存在", saved_paths)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("B. 网页入口：每个槽一个存/读按钮")

ok(all(f'id="saveSlot{n}"' in PAGE_HTML for n in (1, 2, 3)),
   "B1 三个「存」按钮都在页面里")
ok(all(f'id="loadSlot{n}"' in PAGE_HTML for n in (1, 2, 3)),
   "B2 三个「读」按钮都在页面里")
ok('id="save-slots"' in PAGE_HTML, "B3 有槽位状态行")
ok('renderSaveSlots' in PAGE_HTML, "B4 页面会渲染槽位状态")
ok('kind:"save_game", slot:n' in PAGE_HTML or "kind:'save_game', slot:n" in PAGE_HTML,
   "B5 按钮带槽位号调 save_game")
ok('id="exportSave"' in PAGE_HTML and 'id="importFile"' in PAGE_HTML,
   "B6 导出/导入入口在页面里")
ok("kind:'export_save'" in PAGE_HTML and "kind:'import_save'" in PAGE_HTML,
   "B7 导出/导入调对了动作")


# --------------------------------------------------------------------------- #
section("C. 导出 / 导入单文件")

sess, st, loop = new_session(tmp)
sess.save(1)
exp = sess.action({"kind": "export_save"})
ok(exp["ok"] and exp["value"]["filename"].endswith(".json"),
   "C1 导出返回文件名", exp.get("value", {}).get("filename"))
raw = exp["value"]["data"]
ok(raw.get("format") and raw.get("state") and raw.get("loop"),
   "C2 ⭐ 导出的是完整存档（state/loop/format 都在）", list(raw))

sess2, st2, loop2 = new_session(tmp)
sess2._spawn = lambda state, loop3: None
r = sess2.action({"kind": "import_save", "data": raw})
ok(r["ok"] and "导入" in r.get("message", ""), "C3 导入存档成功", r)

wrapped = {"filename": "x.json", "data": raw}
ok(sess2.action({"kind": "import_save", "data": wrapped})["ok"],
   "C4 导出的包（{filename,data}）也能导入")

bad = sess2.action({"kind": "import_save", "data": {"foo": 1}})
ok(not bad["ok"] and "导入失败" in bad.get("error", ""),
   "C5 ⭐ 不是存档 → 导入失败，不假装成功", bad)

sess._spawn = lambda state, loop3: None
routed = sess.action({"kind": "save_game", "slot": 2})
ok(routed["ok"], "C6 action 的 save_game 带 slot 能存", routed)
ok(sess.action({"kind": "load_game", "slot": 2})["ok"],
   "C7 action 的 load_game 带 slot 能读")

bad_slot = sess.action({"kind": "save_game", "slot": 9})
ok(not bad_slot["ok"] and "1~3" in bad_slot.get("error", ""),
   "C8 ⭐ 网页动作传非法槽位 → 返回错误，不抛异常", bad_slot)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")