# coding=utf-8
"""U14 网页建卡向导回归：选项 / 预览 / 开局 / 读档 / 服务端不信任客户端数值。

守的口径：

    · 向导只让玩家选"选择项"，HP/AC/环位一律由服务端重算（客户端塞的数字不算）；
    · 建出来的角色替换真人座位，另外三名同伴不变；
    · 读档要认得"真人是谁"（自定义名字也要坐回同一把椅子）；
    · 页面有建卡浮层；`/api/act` 能取选项、能预览、能开局。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
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

from engine.adventure import load_adventure  # noqa: E402
from engine.creation import CreationError  # noqa: E402
from engine.demo_web import (  # noqa: E402
    DEFAULT_ADV, WebSession, _human_from_save, build_web_table,
)
from engine.persist import save_game  # noqa: E402
from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402
from engine.webui import viewer_log_filter  # noqa: E402

FAILS: list[str] = []
COUNT = 0
ADV = load_adventure(DEFAULT_ADV)


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


def choices(**kw) -> dict:
    base = dict(name="阿瞳", klass="Cleric", species="Human", level=3,
                ability_mode="suggest",
                skills=["perception", "religion"], spells=["Sacred Flame"])
    base.update(kw)
    return base


def tmpdir() -> str:
    return tempfile.mkdtemp(prefix="dnd-webcreate-")


# --------------------------------------------------------------------------- #
section("A. 选项：职业 / 种族 / 技能 / 每个职业的法术表")

tmp = tmpdir()
bridge = WebBridge(timeout=0.1)
sess = WebSession(bridge, ADV, tmp)
opts = sess.create_options()
try:
    ok(len(opts["classes"]) == 12 and len(opts["species"]) == 9,
       "A1 12 职业 / 9 种族", (len(opts["classes"]), len(opts["species"])))
    ok(len(opts["skills"]) == 18, "A2 18 项技能", len(opts["skills"]))
    wz = next(c for c in opts["classes"] if c["en"] == "Wizard")
    ok(wz["hit_die"] == 6 and wz["skill_choices"] == 2,
       "A3 法师条目带生命骰与技能数", wz)
    ok("Fire Bolt" in {s["name"] for s in opts["spells"]["Wizard"]},
       "A4 法师法术表里有 Fire Bolt")
    ok(opts["spells"]["Fighter"] == [],
       "A5 战士法术表为空（不漏别人的法术）")
    ok(all("desc" not in s for s in opts["spells"]["Wizard"]),
       "A6 法术条目不下发长描述（控体积）")

    # ----------------------------------------------------------------------- #
    section("B. 预览：服务端重算，客户端塞的派生值不算")

    prev = sess.create_preview(choices())
    v = prev["value"]
    ok(v["hp"] > 0 and v["ac"] > 0 and v["level"] == 3,
       "B1 返回 HP/AC/等级", v)
    ok(v["saves"] == ["魅力", "感知"], "B2 豁免来自 SRD", v["saves"])
    ok(v["slots"].get("1", {}).get("最大") == 4,
       "B3 3 级牧师 1 环位 4", v["slots"])
    ok(v["spells"] == ["Sacred Flame"], "B4 选的法术进书", v["spells"])

    tampered = sess.create_preview({**choices(), "生命值": 9999,
                                    "护甲等级": 99})
    ok(tampered["value"]["hp"] != 9999 and tampered["value"]["ac"] != 99,
       "B5 ⭐ 客户端塞 HP/AC 不算数（服务端重算）", tampered["value"])

    r1 = sess.create_preview(choices(ability_mode="roll", seed=7))["value"]
    r2 = sess.create_preview(choices(ability_mode="roll", seed=7))["value"]
    ok(r1["hp"] == r2["hp"] and r1["ac"] == r2["ac"],
       "B6 同一个种子预览结果一致", (r1["hp"], r2["hp"]))
    sug = sess.create_preview(choices(klass="Wizard", spells=[]))["value"]
    ok(sug["ac"] >= 10, "B7 推荐标准数组可用", sug)

    ok(raises(lambda: sess.create_preview(choices(klass="Champion")),
              CreationError),
       "B8 子职业不能当基础职业")
    ok(raises(lambda: sess.create_preview(choices(skills=["perception"])),
              CreationError),
       "B9 技能数量不对 → 拒")
    ok(raises(lambda: sess.create_preview(choices(spells=["Fire Bolt"])),
              CreationError),
       "B10 不在本职业法术表 → 拒")
    ok(raises(lambda: sess.create_preview(choices(level=9)), CreationError),
       "B11 等级超范围 → 拒")

    # ----------------------------------------------------------------------- #
    section("C. 开局：换掉真人座位，同伴不变")

    # 建卡前故意把会话留在托管：建卡是“真人坐进来”，必须回到 normal
    sess.mode = "hosted"
    bridge.mode = "hosted"
    res = sess.create_start(choices())
    ok(res["ok"], "C1 开局返回 ok", res.get("message"))
    ok(sess.mode == "normal" and bridge.mode == "normal",
       "C1b ⭐ 建卡开局明确回到 normal（不会界面托管、桌子等真人）",
       (sess.mode, bridge.mode))
    ok(sess.loop.client.human_seat._auto is None,
       "C1c ⭐ 建卡后的真人座位没有托管 auto")
    names = list(sess.state.characters())
    ok(set(names) == {"艾尔玛", "罗恩", "小翠", "阿瞳"},
       "C2 三名同伴 + 新建的真人", names)
    ok(sess.human_name == "阿瞳", "C3 真人座位换成新角色", sess.human_name)
    ok(sess.state.read("/角色列表/阿瞳/职业") == {"牧师": 3},
       "C4 建出来的角色写进了状态树")
    ok(sess.loop is not None and len(sess.loop.field) == 4,
       "C5 上桌单位 4 个", len(sess.loop.field) if sess.loop else 0)
    unit = next((u for u in sess.loop.field if u.name == "阿瞳"), None)
    ok(unit is not None and unit.team == "pc", "C6 新角色在战场上")

    extra = sess.snapshot_extra()
    ok((extra.get("sheet") or {}).get("name") == "阿瞳",
       "C7 ⭐ 状态快照用新真人名取卡，而不是布兰",
       (extra.get("sheet") or {}).get("name"))
    ok(extra.get("scene", {}).get("story", {}).get("total") == len(ADV.scenes),
       "C8 状态快照的剧本总场数还在")

    # 思考提示：真人自己不算“在想”，同伴算
    sess.loop.client.on_call("阿瞳", "explore_action", "start")
    ok(bridge.snapshot()["thinking"] is None,
       "C9 ⭐ 新真人出手不显示“在想”（不再按布兰过滤）")
    sess.loop.client.on_call("艾尔玛", "explore_action", "start")
    ok((bridge.snapshot()["thinking"] or {}).get("seat") == "艾尔玛",
       "C10 同伴出手仍显示“在想”")
    sess.loop.client.on_call("艾尔玛", "explore_action", "end")

    keep = viewer_log_filter(lambda: "阿瞳")
    ok(keep("【阿瞳·私有】你看到了") and not keep("【布兰·私有】别人看到了"),
       "C11 ⭐ 日志过滤跟着新真人名走")

    # ----------------------------------------------------------------------- #
    section("D. 页面与接口")
finally:
    bridge.abort()
    sess._stop_old()
    shutil.rmtree(tmp, ignore_errors=True)

ok('data-panel="create"' in PAGE_HTML and "loadCreateOptions" in PAGE_HTML,
   "D1 页面有建卡浮层与入口")
ok("createPayload" in PAGE_HTML and "create_start" in PAGE_HTML
   and "create_preview" in PAGE_HTML,
   "D2 页面会调预览/开局接口")
ok("技能熟练" in PAGE_HTML and "cSkills" in PAGE_HTML,
   "D3 技能选择区在（数量提示也在）")

tmp = tmpdir()
bridge = WebBridge(timeout=0.1)
sess = WebSession(bridge, ADV, tmp)
bridge.action_handler = lambda p: sess.action(p)
srv = TableServer(bridge, port=0).start()


def post(payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        srv.url + "api/act", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


try:
    code, data = post({"kind": "create_options"})
    ok(code == 200 and data["ok"] and len(data["value"]["classes"]) == 12,
       "D4 ⭐ /api/act 能取建卡选项", code)
    code, data = post({"kind": "create_preview", **choices()})
    ok(code == 200 and data["value"]["hp"] > 0, "D5 /api/act 能预览", code)
    code, data = post({"kind": "create_preview", **choices(klass="Champion")})
    ok(code == 400 and not data["ok"], "D6 坏选择 → 400 且明确失败", (code, data))
    code, data = post({"kind": "create_start", **choices(name="阿瞳")})
    ok(data["ok"] and sess.human_name == "阿瞳",
       "D7 ⭐ /api/act 能建卡开局", data.get("message"))
finally:
    bridge.abort()
    sess._stop_old()
    srv.stop()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
section("E. 读档：自定义真人也要坐回同一把椅子")

tmp = tmpdir()
try:
    bridge = WebBridge(timeout=0.1)
    # 用同一套创建逻辑拿完整角色再存（预览只给摘要，不冒充角色数据）
    from engine.creation import create_character  # noqa: E402
    full = create_character("阿瞳", klass="Cleric", species="Human", level=3,
                            abilities={"力量": 12, "敏捷": 10, "体质": 14,
                                       "智力": 10, "感知": 16, "魅力": 12},
                            skills=["察觉", "宗教"], spells=["Sacred Flame"])
    st, loop = build_web_table(WebBridge(timeout=0.1), human_char=full,
                              human_name="阿瞳")
    path = os.path.join(tmp, "latest.json")
    save_game(path, state=st, ledger=loop.ledger, loop=loop)
    raw = json.load(open(path, encoding="utf-8"))
    ok(_human_from_save(raw) == "阿瞳",
       "E1 ⭐ 从存档能认出真人是谁", _human_from_save(raw))

    bridge2 = WebBridge(timeout=0.1)
    sess2 = WebSession(bridge2, ADV, tmp)
    res = sess2.load_latest()
    ok(res["ok"] and sess2.human_name == "阿瞳",
       "E2 ⭐ 读档后真人座位仍是自定义角色",
       (res.get("message"), sess2.human_name))
    ok("阿瞳" in list(sess2.state.characters()),
       "E3 自定义角色随档回来")
    bridge2.abort()
    sess2._stop_old()
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
