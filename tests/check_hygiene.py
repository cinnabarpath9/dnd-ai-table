# coding=utf-8
"""代码卫生闸门：防"静默吞异常"和"永远为真的测试"回潮。

来源：狼人杀交接文档 §4 的坑（"以为关了思考，其实满血在想"、"测试通过了但线上还是坏的"）。
那类问题的共同形状是：**出错被吞掉，看起来一切正常**。
这个脚本不评判业务对错，只锁三条底线：

    H1 引擎核心不许有「broad except → 只 pass/continue」的静默吞（函数体内，`_main` 除外）
    H2 全仓库不许有裸 `except:`
    H3 测试里不许有 `or True` / `if False:` 这种永远为真的断言

外加一条信息项：TODO / FIXME 计数（只报告，不判失败）。
"""

from __future__ import annotations

import ast
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
TESTS = os.path.join(ROOT, "tests")

BROAD = {"Exception", "BaseException"}
#: H1 不看的文件：演示脚本、自检脚本、命令行入口（它们只做 import 路径与 stdout 兜底）
H1_SKIP_FILES = {"verify_isolation.py", "__main__.py"}

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


def py_files(root: str):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def parents_of(tree: ast.AST) -> dict:
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def enclosing_func(node, parents) -> str | None:
    p = parents.get(node)
    while p is not None:
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return p.name
        p = parents.get(p)
    return None


def handler_kind(h: ast.ExceptHandler) -> str:
    if h.type is None:
        return "bare"
    t = ast.unparse(h.type).strip()
    return "broad" if t in BROAD else "narrow"


def is_silent(h: ast.ExceptHandler) -> bool:
    return len(h.body) == 1 and isinstance(h.body[0], (ast.Pass, ast.Continue))


# --------------------------------------------------------------------------- #
section("H1 引擎核心：不许「broad except → 只 pass/continue」")

h1_bad: list[str] = []
scanned = 0
for path in py_files(ENGINE):
    fn = os.path.basename(path)
    if fn.startswith("demo_") or fn in H1_SKIP_FILES:
        continue
    scanned += 1
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    pmap = parents_of(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if handler_kind(node) != "broad" or not is_silent(node):
            continue
        func = enclosing_func(node, pmap)
        if func is None or func == "_main":
            continue                    # import 期 stdout 兜底 / 命令行入口：不算逻辑吞
        h1_bad.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}"
                      f"（{func}）")
ok(not h1_bad, "H1a 引擎函数里没有静默吞异常", h1_bad[:5])
ok(scanned >= 20, f"H1b 扫描覆盖了 {scanned} 个引擎文件（不是空跑）", scanned)

# 变异自证：故意喂一段"坏写法"，扫描器必须能抓出来（否则这个闸门是摆设）
_bad_src = (
    "def f():\n"
    "    try:\n"
    "        g()\n"
    "    except Exception:\n"
    "        pass\n"
)
_bt = ast.parse(_bad_src)
_bp = parents_of(_bt)
_caught = [n for n in ast.walk(_bt)
           if isinstance(n, ast.ExceptHandler)
           and handler_kind(n) == "broad" and is_silent(n)
           and (enclosing_func(n, _bp) not in (None, "_main"))]
ok(bool(_caught), "H1c ⭐ 扫描器能抓到故意的坏写法（自证有效）")


# --------------------------------------------------------------------------- #
section("H2 全仓库：不许裸 `except:`")

h2_bad: list[str] = []
for base in (ENGINE, TESTS):
    for path in py_files(base):
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and handler_kind(node) == "bare":
                h2_bad.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}")
ok(not h2_bad, "H2 没有任何裸 except（它连 SystemExit/KeyboardInterrupt 都吞）",
   h2_bad[:5])


# --------------------------------------------------------------------------- #
section("H3 测试：不许永远为真的断言")

h3_bad: list[str] = []
for path in py_files(TESTS):
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    for node in ast.walk(tree):
        # `x or True`：整个布尔式恒真
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(isinstance(v, ast.Constant) and v.value is True
                   for v in node.values):
                h3_bad.append(f"{os.path.basename(path)}:{node.lineno}（or True）")
        # `if False:`：永远不会执行的分支
        if isinstance(node, ast.If):
            t = node.test
            if isinstance(t, ast.Constant) and t.value is False:
                h3_bad.append(f"{os.path.basename(path)}:{node.lineno}（if False）")
ok(not h3_bad, "H3 测试里没有 `or True` / `if False:`（AST 判，不看注释与字符串）",
   h3_bad[:5])


# --------------------------------------------------------------------------- #
section("H4 信息项：TODO / FIXME（只报告，不判失败）")

todo = 0
for base in (ENGINE, TESTS):
    for path in py_files(base):
        for line in open(path, encoding="utf-8"):
            if re.search(r"\b(TODO|FIXME|XXX)\b", line):
                todo += 1
print(f"  ℹ️ TODO/FIXME/XXX 共 {todo} 处（引擎 + 测试）")
ok(True, "H4 信息项已统计")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")