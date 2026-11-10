# coding=utf-8
"""Batch C 回归：网页动作（装备切换 / 准备法术）+ HTTP 接口。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 本机系统代理不排除回环时会吞掉 127.0.0.1 请求（偶发 502）——测试里显式绕开。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from engine.longrun import build_table_loop  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402
from engine.webui import TableServer, WebBridge  # noqa: E402

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


HUMAN = "布兰"
st, led, loop = build_table_loop(seed=3, names=(HUMAN, "B", "C", "D"))
st.commit([{"op": "insert", "path": f"/角色列表/{HUMAN}/物品/长剑",
            "value": {"类别": "武器", "已装备": True,
                      "伤害公式": "1d8", "伤害类型": "挥砍"}}],
          actor="测试", reason="给测试角色一把剑")
items = st.read(f"/角色列表/{HUMAN}/物品") or {}
weapon = next((k for k, v in items.items()
               if isinstance(v, dict) and "武器" in str(v.get("类别", ""))), "")
ok(bool(weapon), "C-0 测试角色有武器", list(items))


# --------------------------------------------------------------------------- #
section("C-1 装备/卸下（走 commit + schema）")

was = st.read(f"/角色列表/{HUMAN}/物品/{weapon}/已装备")
out = apply_action(st, loop, HUMAN, {"kind": "equip_item", "name": weapon})
now = st.read(f"/角色列表/{HUMAN}/物品/{weapon}/已装备")
ok(out["ok"] is True and now is (not was),
   "C-1a ⭐ 装备开关翻转", (was, now))
audit = st.audit[-1]
ok(audit.actor.startswith("玩家:") and ("装备" in audit.reason or "卸下" in audit.reason),
   "C-1b 记了玩家审计", (audit.actor, audit.reason))

out = apply_action(st, loop, HUMAN, {"kind": "equip_item", "name": weapon})
back = st.read(f"/角色列表/{HUMAN}/物品/{weapon}/已装备")
ok(out["ok"] is True and back is was, "C-1c 再点一次翻回来", back)

try:
    apply_action(st, loop, HUMAN, {"kind": "equip_item", "name": "不存在的剑"})
    ok(False, "C-1d 不存在的物品必须报错")
except ActionError:
    ok(True, "C-1d 不存在的物品 → ActionError")


# --------------------------------------------------------------------------- #
section("C-2 战斗中不许换装 / 改准备")

loop.start_combat(enemies=[("Bandit", 2)], distance=20)
try:
    apply_action(st, loop, HUMAN, {"kind": "equip_item", "name": weapon})
    ok(False, "C-2a 战斗中换装必须被拒")
except ActionError:
    ok(True, "C-2a ⭐ 战斗中换装 → ActionError")
try:
    apply_action(st, loop, HUMAN, {"kind": "prepare_spell", "name": "治疗真言"})
    ok(False, "C-2b 战斗中改准备必须被拒")
except ActionError:
    ok(True, "C-2b 战斗中改准备 → ActionError")


# --------------------------------------------------------------------------- #
section("C-3 准备法术（不在战斗时）")

st2, led2, loop2 = build_table_loop(seed=4, names=(HUMAN, "B"))
spells = st2.read(f"/角色列表/{HUMAN}/施法/法术书") or {}
if not spells:
    spell = "祝福术"
    st2.commit([{"op": "insert", "path": f"/角色列表/{HUMAN}/施法/法术书/{spell}",
                 "value": {"等级": 1, "准备中": False}}],
               actor="测试", reason="造一条法术")
else:
    spell = next(iter(spells))
out = apply_action(st2, loop2, HUMAN, {"kind": "prepare_spell", "name": spell})
ok(out["ok"] is True
   and st2.read(f"/角色列表/{HUMAN}/施法/法术书/{spell}/准备中") is True,
   "C-3a ⭐ 准备法术开关生效")
out = apply_action(st2, loop2, HUMAN, {"kind": "prepare_spell", "name": spell})
ok(st2.read(f"/角色列表/{HUMAN}/施法/法术书/{spell}/准备中") is False,
   "C-3b 再点取消准备")


# --------------------------------------------------------------------------- #
section("C-5 DM 技能走 HTTP（/api/act）")

bridge5 = WebBridge(timeout=5,
                    action_handler=lambda p: apply_action(st2, loop2, HUMAN, p))
srv5 = TableServer(bridge5, port=0).start()
try:
    body = json.dumps({"kind": "dm_create_action", "name": "跃斩",
                       "类型": "attack", "攻击加值": "7",
                       "伤害骰": "2d6+4", "伤害类型": "挥砍"}).encode("utf-8")
    req = urllib.request.Request(srv5.url + "api/act", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        res = json.loads(r.read().decode("utf-8"))
    ok(res.get("ok") is True and "下一轮" in res.get("message", ""),
       "C-5a ⭐ 网页造技能把机械摘要回给人", res)
    ok(st2.read(f"/角色列表/{HUMAN}/动作/跃斩") is not None,
       "C-5b 技能落进当局状态", st2.read(f"/角色列表/{HUMAN}/动作"))

    body = json.dumps({"kind": "dm_create_action", "name": "坏技能",
                       "类型": "attack"}).encode("utf-8")
    req = urllib.request.Request(srv5.url + "api/act", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        code = 200
    except urllib.error.HTTPError as exc:
        code = exc.code
    ok(code == 400, "C-5c 坏技能 → 400（不假装成功）", code)
finally:
    srv5.stop()


# --------------------------------------------------------------------------- #
section("C-4 HTTP /api/act")

bridge = WebBridge(timeout=5, action_handler=lambda p: apply_action(st2, loop2, HUMAN, p))
srv = TableServer(bridge, port=0).start()
try:
    body = json.dumps({"kind": "prepare_spell", "name": spell}).encode("utf-8")
    req = urllib.request.Request(srv.url + "api/act", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        res = json.loads(r.read().decode("utf-8"))
    ok(res.get("ok") is True and "准备" in res.get("message", ""),
       "C-4a ⭐ /api/act 走通", res)
finally:
    srv.stop()


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
