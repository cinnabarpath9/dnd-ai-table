# coding=utf-8
"""隔离自检：P1 地基必须是「零模型调用、零网络、零花钱」的。

为什么单独立一个脚本（对应狼人杀项目的血泪教训）：
    那个项目出过"自称全程离线的测试其实一直在真联网真花钱"的事故。
    根因是"我记得我桩了"—— **记忆不是证据**。
    所以要有一个**机械可查**的守卫：只要引擎里出现任何网络/模型调用的痕迹，这里就红。

四条守卫：
    L1 引擎源码里不许出现已知的模型/网络客户端（httpx / requests / openai / aiohttp …）
    L2 引擎源码里不许出现对模型 API 域名的引用
    L3 引擎源码里不许直接 `import random`（随机数只能从 engine.dice 出，否则同种子不可复现）
    L4 真正跑一遍核心链路，确认它在**没有网络**的情况下也能出结果
"""

from __future__ import annotations

import ast
import glob
import os
import random
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


ENGINE_FILES = sorted(
    p for p in glob.glob(os.path.join(ROOT, "engine", "**", "*.py"), recursive=True)
)


def read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
section("L0. 找得到引擎源码")

ok(len(ENGINE_FILES) >= 8, f"找到 {len(ENGINE_FILES)} 个引擎源文件", ENGINE_FILES)


# --------------------------------------------------------------------------- #
section("L1. 引擎里不许有网络/模型客户端")

BANNED_IMPORTS = (
    "httpx", "requests", "aiohttp", "urllib.request", "urllib3", "openai",
    "anthropic", "google.generativeai", "dashscope", "websockets", "socket",
)
#: 例外：**数据采集**脚本必须联网（从 open5e 拉 SRD）。它不调用任何模型，
#: 而且只跑一次、把结果落盘；回归与引擎运行都不碰它。
#: 另一个例外是**出口闸**本身：它要 import socket 才能把 connect 包起来 ——
#: 它只拦不连（下面 L1b 钉住这一点）。
ALLOW_NETWORK = {
    os.path.join(ROOT, "engine", "srd", "fetch.py"),
    os.path.join(ROOT, "engine", "netguard.py"),
    # P5-C 真人网页座位：**只绑 127.0.0.1** 的本地服务（L1c 钉住这一点）；
    # demo_web 的"假浏览器"用 urllib 调同一个回环地址，不连外网。
    os.path.join(ROOT, "engine", "webui.py"),
    os.path.join(ROOT, "engine", "demo_web.py"),
    # P4 唯一真模型出口：只有 RealModel 在这个文件里 import urllib.request。
    # 默认 enabled=False、缺 key/base 直接拒发；回归与 demo 全程不构造它。
    os.path.join(ROOT, "engine", "models", "client.py"),
}
bad: list[str] = []
for p in ENGINE_FILES:
    if p in ALLOW_NETWORK:
        continue
    src = read(p)
    for mod in BANNED_IMPORTS:
        # 只看真实的 import 语句，避免误伤注释里提到名字
        if re.search(rf"^\s*(?:import|from)\s+{re.escape(mod)}\b", src, re.M):
            bad.append(f"{os.path.relpath(p, ROOT)} 导入 {mod}")
ok(not bad, "引擎不导入任何网络/模型客户端（数据采集脚本已明确除外）", bad)

# L1b：出口闸自己只许"拦"，不许"连"——没有 send / sendto / urlopen / http.client
# 用 AST 判，避免文档里提到 "http.client" 就被误伤。
guard_src = read(os.path.join(ROOT, "engine", "netguard.py"))
_gtree = ast.parse(guard_src)
_bad_calls = [n.func.attr for n in ast.walk(_gtree)
              if isinstance(n, ast.Call)
              and getattr(n.func, "attr", "") in ("send", "sendto", "urlopen")]
_bad_mods = []
for n in ast.walk(_gtree):
    if isinstance(n, ast.Import):
        _bad_mods += [a.name for a in n.names
                      if a.name in ("http.client", "urllib.request", "ssl")]
    if isinstance(n, ast.ImportFrom) and n.module in (
            "http.client", "urllib.request", "ssl"):
        _bad_mods.append(n.module)
ok(not _bad_calls and not _bad_mods,
   "L1b ⭐ 出口闸只拦不连（AST：没有 send/sendto/urlopen，也不导入 http/urllib/ssl）",
   (_bad_calls, _bad_mods))

# L1c：网页服务必须只绑回环（没有 0.0.0.0 / 空 host / 对外绑定）
_webui = read(os.path.join(ROOT, "engine", "webui.py"))
ok('"127.0.0.1", int(port)' in _webui and "0.0.0.0" not in _webui,
   "L1c ⭐ 真人网页服务只绑 127.0.0.1（没有 0.0.0.0）")


# --------------------------------------------------------------------------- #
section("L2. 引擎里不许出现模型 API 域名")

BANNED_HOSTS = (
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    "ark.cn-beijing.volces.com", "dashscope.aliyuncs.com", "api.deepseek.com",
    "openrouter.ai",
)
bad2: list[str] = []
for p in ENGINE_FILES:
    src = read(p)
    for h in BANNED_HOSTS:
        if h in src:
            bad2.append(f"{os.path.relpath(p, ROOT)} 提到 {h}")
ok(not bad2, "引擎不引用任何模型 API 域名", bad2)

# 例外说明：srd/fetch.py 要联网拉数据 —— 那是**数据采集**，不是模型调用。
FETCH = os.path.join(ROOT, "engine", "srd", "fetch.py")
ok(os.path.exists(FETCH), "数据采集脚本存在（它是唯一允许联网的文件）")
fetch_src = read(FETCH)
ok("api.open5e.com" in fetch_src,
   "采集脚本只指向 open5e（SRD 数据源），不指向任何模型 API")
ok(not any(h in fetch_src for h in BANNED_HOSTS),
   "采集脚本里没有模型 API 域名")


# --------------------------------------------------------------------------- #
section("L3. 随机数只能从 engine.dice 出（否则同种子不可复现）")

allowed_import_random = {
    os.path.join(ROOT, "engine", "dice", "roller.py"),   # 骰子的底层，唯一允许
}
bad3: list[str] = []
for p in ENGINE_FILES:
    if p in allowed_import_random:
        continue
    src = read(p)
    if re.search(r"^\s*import\s+random\b", src, re.M):
        bad3.append(os.path.relpath(p, ROOT))
ok(not bad3, "除 engine/dice/roller.py 外，引擎不直接 import random", bad3)

roller = read(os.path.join(ROOT, "engine", "dice", "roller.py"))
ok("import random" in roller, "骰子模块自己确实用了 random（这是唯一合法出口）")


# --------------------------------------------------------------------------- #
section("L4. 在没有网络的前提下，核心链路能跑出结果")

from engine.dice import roll  # noqa: E402
from engine.rules.adjudicate import resolve_attack, resolve_save_action  # noqa: E402
from engine.rules.bestiary import build_unit  # noqa: E402
from engine.rules.menu import build_menu, render_menus  # noqa: E402
from engine.rules.unit import Unit  # noqa: E402

rng = random.Random(20261014)

goblin, _ = build_unit("Bandit")
hero = Unit(id="P1", name="Hero", team="pc", is_pc=True, level=3,
            ac=16, hp_max=30, hp=30, speeds={"walk": 30},
            abilities={"strength": 16, "dexterity": 14, "constitution": 14,
                       "intelligence": 10, "wisdom": 12, "charisma": 10},
            position=(5, 0))
goblin.position = (0, 0)
field = [goblin, hero]

m = build_menu(goblin, field)
ok(len(m.options) > 3, "L4a 菜单生成成功（不需要网络）", len(m.options))
txt = render_menus([m], field)
ok("可选行动" in txt, "L4b 菜单渲染成功")

scim = [a for a in goblin.actions if a.name == "Scimitar"][0]
res = resolve_attack(goblin, hero, scim.attacks[0], rng, distance=5)
ok(res.check is not None, "L4c 攻击结算跑通")
ok(isinstance(res.hit, bool), "L4d 命中判定是布尔值")

dragon, _ = build_unit("Adult Red Dragon")
fb = [a for a in dragon.actions if a.name == "Fire Breath"][0]
sr = resolve_save_action(dragon, [hero], fb, rng)
ok(len(sr.targets) == 1, "L4e AoE 结算跑通")

# 同种子可复现：整条链路跑两遍结果必须一模一样
def run_once(seed: int) -> list:
    g, _ = build_unit("Bandit")
    h = Unit(id="P1", name="Hero", team="pc", hp_max=30, hp=30,
             abilities={k: 14 for k in ("strength", "dexterity", "constitution",
                                        "intelligence", "wisdom", "charisma")})
    r = random.Random(seed)
    out = []
    for _ in range(5):
        a = resolve_attack(g, h, [x for x in g.actions if x.name == "Scimitar"][0].attacks[0],
                           r, distance=5, apply_damage=False)
        out.append((a.check.a.natural, a.check.total, a.hit, a.damage_total))
    return out

ok(run_once(99) == run_once(99), "L4f 同种子整条链路可复现（逐颗骰子一致）")
ok(run_once(99) != run_once(100), "L4g 不同种子结果不同（不是伪随机）")


# --------------------------------------------------------------------------- #
section("L5. 数据是本地缓存，回归不联网")

DATA = os.path.join(ROOT, "engine", "srd", "data")
ok(os.path.isdir(DATA), "SRD 数据目录存在")
for res in ("creatures", "spells", "weapons", "armor"):
    ok(os.path.exists(os.path.join(DATA, f"{res}.index.json")),
       f"{res}.index.json 已落盘")
man = os.path.join(DATA, "manifest.json")
ok(os.path.exists(man), "采集清单 manifest.json 存在")
if os.path.exists(man):
    import json
    with open(man, encoding="utf-8") as fh:
        mf = json.load(fh)
    ok(mf.get("document") == "srd-2024", "清单记录的文档是 srd-2024", mf.get("document"))
    ok(len(mf.get("resources", {})) >= 10, "清单里至少有 10 个资源")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：通过 {COUNT}，失败 0")
