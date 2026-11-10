# coding=utf-8
"""接线自检：菜单产出的东西，引擎真的都接住了吗？

这不是业务测试，是**结构**检查（AST，不跑游戏）：
    W1 菜单 `Option(kind=...)` 产出的每一种，`turn.execute_intent` 都有分支
    W2 引擎里 `set_block("...")` 的名字都在 `BLOCK_ORDER` 里
    W3 引擎里 `Action(slot=...)` 的槽位都在 `SLOTS` 里
    W4 扫描本身不是空跑（产出集与处理集都非空）
    W5 自证：故意塞一个假 kind，检查器必须报出来

为什么要有：这类"菜单生成了、引擎没接"的死分支，跑起来只会走到兜底，
**看起来一切正常**，但那条选项永远不产生效果 —— 正是"静默失效"的典型。
"""

from __future__ import annotations

import ast
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
sys.path.insert(0, ROOT)

from engine.rules.unit import SLOTS  # noqa: E402
from engine.state.assembler import BLOCK_ORDER  # noqa: E402

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


def parse(rel: str) -> ast.AST:
    path = os.path.join(ENGINE, rel)
    return ast.parse(open(path, encoding="utf-8").read(), filename=path)


def call_name(node: ast.Call) -> str:
    fn = node.func
    return getattr(fn, "id", None) or getattr(fn, "attr", None) or ""


def option_kinds(tree) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and call_name(n) == "Option":
            for kw in n.keywords:
                if kw.arg == "kind" and isinstance(kw.value, ast.Constant):
                    out.add(str(kw.value.value))
    return out


def handled_kinds(tree) -> set[str]:
    """`opt.kind == "x"` 与 `opt.kind in ("x","y")` 两种写法都收。"""
    out: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Compare):
            continue
        left = n.left
        if not (isinstance(left, ast.Attribute) and left.attr == "kind"):
            continue
        for op, comp in zip(n.ops, n.comparators):
            if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                out.add(str(comp.value))
            if isinstance(op, ast.In):
                elts = getattr(comp, "elts", None)
                if elts:
                    for e in elts:
                        if isinstance(e, ast.Constant):
                            out.add(str(e.value))
    return out


def set_block_names(tree) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and call_name(n) == "set_block" and n.args:
            a = n.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                out.add(a.value)
    return out


def action_slots(tree) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and call_name(n) == "Action":
            for kw in n.keywords:
                if kw.arg == "slot" and isinstance(kw.value, ast.Constant):
                    out.add(str(kw.value.value))
    return out


# --------------------------------------------------------------------------- #
section("W1/W2/W3 结构接线")

produced = option_kinds(parse("rules/menu.py"))
handled = handled_kinds(parse("rules/turn.py"))
missing = sorted(produced - handled)
ok(not missing, f"W1 菜单产出的 {len(produced)} 种选项都有处理分支", missing)
ok(bool(produced) and bool(handled),
   f"W4 扫描不是空跑（产出 {len(produced)} / 处理 {len(handled)}）",
   (sorted(produced), sorted(handled)))

blocks: set[str] = set()
slots: set[str] = set()
for dirpath, _dirs, files in os.walk(ENGINE):
    for fn in files:
        if not fn.endswith(".py") or fn.startswith("demo_"):
            continue
        path = os.path.join(dirpath, fn)
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        blocks |= set_block_names(tree)
        slots |= action_slots(tree)

unknown_blocks = sorted(blocks - set(BLOCK_ORDER))
ok(not unknown_blocks, f"W2 set_block 的名字都在 BLOCK_ORDER 里（{len(blocks)} 个）",
   unknown_blocks)
unknown_slots = sorted(slots - set(SLOTS))
ok(not unknown_slots, f"W3 Action.slot 都在 SLOTS 里（{len(slots)} 个）",
   unknown_slots)
ok(bool(blocks) and bool(slots), "W5 块名/槽位扫描也非空",
   (sorted(blocks), sorted(slots)))


# --------------------------------------------------------------------------- #
section("W6 自证：检查器能报出故意漏接的 kind")

fake_produced = set(produced) | {"definitely_not_implemented"}
fake_missing = sorted(fake_produced - handled)
ok(bool(fake_missing) and fake_missing == ["definitely_not_implemented"],
   "W6 ⭐ 塞一个假 kind → 立刻被列为缺处理", fake_missing)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")