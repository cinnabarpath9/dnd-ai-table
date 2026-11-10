# coding=utf-8
"""观战 / 托管 / BGM：模式开关、座位替换、节奏回调、页面接线。"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure.model import load_adventure  # noqa: E402
from engine.demo_table import build_table  # noqa: E402
from engine.demo_web import (  # noqa: E402
    COMPANION_NAMES, WebSession, _auto_option, _pilot_callable,
    build_web_table,
)
from engine.lifepath import QUESTIONS, build as build_lifepath  # noqa: E402
from engine.models import FakeModel, ModelReply, ModelRequest, TableClient  # noqa: E402
from engine.models.client import ModelError  # noqa: E402
from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402

FAILS: list[str] = []
COUNT = 0
ADV = load_adventure(os.path.join(ROOT, "adventures", "哨塔的拖痕.json"))


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(label)
        print(f"  ✗ {label}  extra={extra!r}")


# ---- A. 兜底选动作 ----
req = ModelRequest(role="player", actor="布兰", task="explore_action",
                   prompt="", expect="intent",
                   allowed={"options": [
                       {"id": "look", "kind": "look"},
                       {"id": "a1", "kind": "attack", "requires_target": True,
                        "targets": ["E1"]},
                   ]})
ok(_auto_option(req) == "look", "A1 优先挑不需要目标的合法项", _auto_option(req))
req2 = ModelRequest(role="player", actor="布兰", task="combat_action",
                    prompt="", expect="intent",
                    allowed={"options": [
                        {"id": "a1", "kind": "attack", "requires_target": True,
                         "targets": ["E1"]}]})
ok(_auto_option(req2) == "a1 E1", "A2 需要目标时带上第一个合法目标", _auto_option(req2))
ok(_auto_option(ModelRequest(role="player", actor="x", task="t", prompt="",
                             expect="intent", allowed={"options": []})) == "",
   "A3 空菜单返回空（交给座位报错/兜底）")


def run(mode, pace=0.0):
    bridge = WebBridge(timeout=8)
    server = TableServer(bridge, port=0).start()
    sess = WebSession(bridge, ADV, os.path.join(os.environ["TEMP"], "mode-test"))
    sess.pace = pace
    sess.new_game(mode)
    return bridge, server, sess


def wait_until(fn, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.05)
    return False


# ---- B. 观战：没有真人座位 ----
bridge, server, sess = run("spectate")
try:
    ok(wait_until(lambda: bridge.done, 20), "B1 ⭐ 观战局自动跑完（不等输入）")
    snap = bridge.snapshot()
    ok(snap["mode"] == "spectate", "B2 snapshot 带 mode=spectate", snap["mode"])
    table = sess.loop.client
    ok(getattr(table, "human_seat", "x") is None, "B3 ⭐ 观战没有真人座位")
    ok(table.players.get("布兰") is not None
       and not table.players["布兰"].__class__.__name__.startswith("WebHumanSeat"),
       "B4 布兰座位换成了 AI")
    ok(len(snap["log"]) > 10 and not snap["error"], "B5 跑出日志、没有错误",
       snap["error"])
finally:
    bridge.abort()
    server.stop()

# ---- C. 托管：座位还在，但 auto 代打 ----
bridge, server, sess = run("hosted", pace=0.3)
try:
    seat = sess.loop.client.human_seat
    ok(seat._auto is not None, "C1 ⭐ 托管给真人座位装了 auto")
    out = sess.set_hosted(False)
    ok(out["ok"] and seat._auto is None and sess.mode == "normal",
       "C2 ⭐ 接管后 auto 撤掉、模式回 normal", out)
    out = sess.set_hosted(True)
    ok(out["ok"] and seat._auto is not None and sess.mode == "hosted",
       "C3 再次托管能恢复 auto")
    # 这次真的接管，让下一轮等网页
    out = sess.set_hosted(False)
    ok(out["ok"] and seat._auto is None, "C4 第二次接管也生效")
    sess.pace = 0.0
    ok(wait_until(lambda: bridge.snapshot()["pending"] is not None, 10),
       "C5 ⭐ 接管后轮到自己时会等人（不再自动）",
       (bridge.snapshot().get("pending") or {}).get("actor"))
    snap = bridge.snapshot()
    p = snap["pending"] or {}
    ok(bool(p.get("options")), "C6 待答里带着合法菜单", p.get("actor"))
finally:
    bridge.abort()
    server.stop()

# ---- D. 观战局不能"接管"（没有真人座位） ----
bridge, server, sess = run("spectate")
try:
    out = sess.set_hosted(False)
    ok(out["ok"] is False, "D1 观战局点接管要明确拒绝", out)
finally:
    bridge.abort()
    server.stop()

# ---- E. 轮末回调 ----
state, _ledger, loop = build_table(True, False)
calls = {"n": 0}
loop.on_round = lambda: calls.__setitem__("n", calls["n"] + 1)
loop.run_explore_round_all(situation="测试")
ok(calls["n"] == 1, "E1 探索轮结束调一次 on_round", calls["n"])
bad = {"n": 0}


def boom():
    bad["n"] += 1
    raise RuntimeError("故意的")


loop.on_round = boom
log = loop.run_explore_round_all(situation="测试")
ok(bad["n"] == 1, "E2 回调确实被调用")
ok(any("轮末回调出错" in p for p in log.problems),
   "E3 ⭐ 回调抛错只记问题、不崩这一局", log.problems[-2:])

# ---- G. 会话代次：旧座位的 ask/日志/思考不能进新局 ----
_b = WebBridge(timeout=0.1)
_req = ModelRequest(role="player", actor="布兰", task="explore_action",
                    prompt="", expect="intent",
                    allowed={"options": [{"id": "look"}]})
_b.set_generation(7)
ok(raises(lambda: _b.ask(_req, generation=6), ModelError),
   "G1 ⭐ 旧代次的 ask 直接拒绝，不投进新局")
ok(_b.generation() == 7, "G2 bridge 记住当前代次")

_sess = WebSession(_b, ADV, os.path.join(os.environ["TEMP"], "mode-test"))
_sess.gen = 7
_b.set_generation(7)
_sess._log_if(6, "【旧局】不该出现")
ok(not any("旧局" in x["text"] for x in _b.snapshot()["log"]),
   "G3 ⭐ 旧代次的日志被丢弃")
_sess._log_if(7, "【新局】应该出现")
ok(any("新局" in x["text"] for x in _b.snapshot()["log"]),
   "G4 当前代次的日志照常进")
_sess._think_if(6, "艾尔玛", "explore_action", "start")
ok(_b.snapshot()["thinking"] is None, "G5 ⭐ 旧代次的思考不覆盖新局")
_sess._think_if(7, "布兰", "explore_action", "start")
ok(_b.snapshot()["thinking"] is None, "G6 真人自己不算在想")
_sess._think_if(7, "艾尔玛", "explore_action", "start")
ok((_b.snapshot()["thinking"] or {}).get("seat") == "艾尔玛",
   "G7 非真人座位的思考照常显示")
_b.abort()

# ---- H. 托管 pilot 要进牌桌总账、失败要明说 ----
_p_table = TableClient(players={"布兰": FakeModel(name="布兰（假）")},
                       pilot=FakeModel(name="pilot（假）"))
_preq = ModelRequest(role="player", actor="布兰", task="explore_action",
                     prompt="", expect="intent",
                     allowed={"options": [{"id": "look"}]})
_prep = _p_table.complete_pilot(_preq)
ok(_prep.ok, "H1 托管座位能答")
ok(_p_table.totals()["calls"] == 1,
   "H2 ⭐ pilot 调用进牌桌总账（自检/复盘不少算）", _p_table.totals()["calls"])
ok("pilot" in _p_table.seat_summary(), "H3 seat_summary 里看得到 pilot")

class _BadPilot(FakeModel):
    def _complete(self, req):
        raise ModelError("上游 500")


_p_table.pilot = _BadPilot(name="pilot（坏）")
_pilot = _pilot_callable(_p_table, lambda s: None)
ok(raises(lambda: _pilot(_preq), ModelError),
   "H4 ⭐ 托管模型失败明确报错，不静默挑第一个选项")

# ---- I. 开局大厅：先讲战役 + 四人确认预生成卡 ----
_lb = WebBridge(timeout=0.1)
_ls = WebSession(_lb, ADV, tempfile.mkdtemp(), session_zero=True)
try:
    _r = _ls.new_game("spectate")
    _snap = _ls.snapshot_extra()
    ok(_r.get("ok") and _snap.get("lobby", {}).get("active"),
       "I1 ⭐ session_zero 新开先停在开局大厅")
    ok(len(_snap["lobby"]["characters"]) == 4,
       "I2 大厅列出四张预生成角色卡", len(_snap["lobby"]["characters"]))
    ok("最高属性" in (_snap["lobby"].get("audit") or ""),
       "I2b ⭐ 大厅有 DM 审卡（最高属性一句）", _snap["lobby"].get("audit"))
    ok(all(c.get("top") for c in _snap["lobby"]["characters"]),
       "I2c 四张卡都带最高属性")
    ok((_snap["lobby"].get("stage") or {}).get("title") == ADV.title,
       "I2d 大厅显示本桌剧本", _snap["lobby"].get("stage"))
    _st = _snap["lobby"]["stage"]
    ok(len(_st.get("options") or []) >= 2,
       "I2d2 ⭐ 剧本列表列出多个已校验 JSON", len(_st.get("options") or []))
    _other = next((x["id"] for x in _st["options"]
                   if x["id"] != _st.get("id")), "")
    _sw = _ls.select_stage(_other)
    ok(_sw.get("ok") and _ls.adv.title != ADV.title
       and _ls.snapshot_extra()["lobby"]["stage"]["title"] == _ls.adv.title,
       "I2d3 ⭐ 大厅能换剧本并重建大厅", (_sw, _ls.adv.title))
    _gc = _ls.generate_companions()
    _gz = _ls.snapshot_extra()["lobby"]
    ok(_gc.get("ok") and _gc.get("fallback")
       and _gz.get("companion_source") == "pregen"
       and any("假模型" in v for v in (_gc.get("errors") or {}).values()),
       "I2e0 假模型下 AI 自建退预生成卡（带原因）", _gc)
    _ii = _ls.lobby_intros()
    _intro = _ls.snapshot_extra()["lobby"].get("intros") or []
    ok(_ii.get("ok") and len(_intro) == 3,
       "I2e ⭐ 三个 AI 队友各说一句", _intro)
    ok({x["name"] for x in _intro} == {"艾尔玛", "罗恩", "小翠"},
       "I2e2 介绍来自三个 AI 座位，不含真人", [x["name"] for x in _intro])
    ok(_snap["lobby"].get("guided_step") == "motivation",
       "I2f 大厅先问动机")
    _g1 = _ls.lobby_answer("我想查清哨塔的旧账。")
    ok(_g1.get("ok") and _ls.snapshot_extra()["lobby"]["guided_step"] == "bonds",
       "I2g ⭐ 答完动机才问羁绊", _g1)
    _g2 = _ls.lobby_answer("罗恩是我必须带上的旧友。")
    ok(_g2.get("ok") and _ls.snapshot_extra()["lobby"]["guided_step"] == "done"
       and _ls.snapshot_extra()["lobby"]["notes"]["bonds"].startswith("罗恩"),
       "I2h 两问答完进确认；答案只进 notes")
    ok(ADV.title in _snap["lobby"]["pitch"],
       "I3 战役介绍带剧本标题")
    _q1 = _ls.lobby_ask("这地方安全吗？")
    ok(_q1.get("ok") and len(_ls.snapshot_extra()["lobby"]["qa"]) == 1,
       "I3b ⭐ 大厅能问 DM，回答进 transcript", _q1)
    _ls.lobby_ask("第二个问题")
    _ls.lobby_ask("第三个问题")
    _q4 = _ls.lobby_ask("第四个问题")
    ok(not _q4.get("ok"),
       "I3c 大厅最多问 3 个（防开局烧预算）", _q4.get("error"))
    ok(_ls.start_game().get("ok") and _ls.phase == "playing",
       "I4 ⭐ 四人确认后才启动 AdventureRunner")
    ok(_ls.snapshot_extra().get("lobby") is None,
       "I5 开始后大厅快照清空")
    _logs = [x.get("text") or "" for x in _lb.snapshot().get("log") or []]
    ok(any("现在，你要做什么？" in x for x in _logs),
       "I5b ⭐ 确认后有启航旁白（含固定收尾）", _logs[-2:])
    ok(any("哨塔的旧账" in x for x in _logs),
       "I5c ⭐ 动机/羁绊答案进了启航旁白", _logs[-2:])
    ok(any("同桌已就位" in x for x in _logs),
       "I5d ⭐ 三句队友介绍进了启航旁白", _logs[-2:])
    _sv = _ls.save()
    _raw = json.load(open(os.path.join(_ls.save_dir, "latest.json"), encoding="utf-8"))
    _z = (_raw.get("extra") or {}).get("session_zero") or {}
    ok(_sv.get("ok") and _z.get("notes", {}).get("motivation", "").startswith("我想")
       and len(_z.get("intros") or []) == 3,
       "I5e ⭐ 开局记录（审卡/两问/队友）进存档", _z)
    _load = _ls.load_latest()
    ok((_ls.snapshot_extra().get("session_zero") or {}).get("stage") == _z["stage"],
       "I5f ⭐ 读档后开局记录仍能取到")
    _again = _ls.start_game()
    ok(not _again.get("ok"), "I6 重复确认不重开桌", _again.get("error"))
finally:
    _ls._stop_old()
    _lb.abort()

# ---- I-b. 动态桌规模：真人 1 + AI 0~3 ---- 
_lb0 = WebBridge(timeout=0.1)
_ls0 = WebSession(_lb0, ADV, tempfile.mkdtemp(), session_zero=True)
try:
    _ls0.new_game("normal")
    _r0 = _ls0.set_table_size(0)
    _s0 = _ls0.snapshot_extra()["lobby"]
    ok(_r0.get("ok") and len(_s0["characters"]) == 1
       and _s0["table"]["total"] == 1,
       "I7 ⭐ 单人桌（1 真人 + 0 AI）能建", _s0.get("table"))
    ok(_ls0.lobby_intros().get("value") == [],
       "I7b 单人桌没有 AI 队友介绍")
    ok(_ls0.start_game().get("ok") and _ls0.phase == "playing",
       "I7c ⭐ 单人桌能开局")
finally:
    _ls0._stop_old()
    _lb0.abort()

_lb2 = WebBridge(timeout=0.1)
_ls2 = WebSession(_lb2, ADV, tempfile.mkdtemp(), session_zero=True)
try:
    _ls2.new_game("normal")
    _r2 = _ls2.set_table_size(2)
    _s2 = _ls2.snapshot_extra()["lobby"]
    ok(_r2.get("ok") and len(_s2["characters"]) == 3
       and _s2["table"]["total"] == 3,
       "I7d ⭐ 三人桌（1 真人 + 2 AI）能建", _s2.get("table"))
    _i2 = _ls2.lobby_intros()
    ok(_i2.get("ok") and len(_s2.get("intros") or _ls2.snapshot_extra()["lobby"]["intros"]) == 2,
       "I7e 只让 2 个 AI 队友介绍")
    ok(_ls2.start_game().get("ok")
       and any("3 张角色卡" in (x.get("text") or "")
               for x in _lb2.snapshot().get("log") or []),
       "I7f ⭐ 三人桌开局旁白按实际人数说")
    _sv2 = _ls2.save()
    _raw2 = json.load(open(os.path.join(_ls2.save_dir, "latest.json"), encoding="utf-8"))
    ok(_sv2.get("ok")
       and ((_raw2.get("extra") or {}).get("table") or {}).get("ai") == 2,
       "I7g 桌规模进存档", _raw2.get("extra"))
    _l2 = _ls2.load_latest()
    ok(_l2.get("ok") and _ls2.ai_count == 2
       and _ls2.snapshot_extra().get("session_zero", {}).get("table", {}).get("ai") == 2,
       "I7h ⭐ 三人桌读档仍按 2 AI 建座位", (_l2, _ls2.ai_count))
finally:
    _ls2._stop_old()
    _lb2.abort()

_lb3 = WebBridge(timeout=0.1)
_ls3 = WebSession(_lb3, ADV, tempfile.mkdtemp())
try:
    _ls3.new_game("spectate")
    ok(_ls3.snapshot_extra().get("lobby") is None,
       "I7 不开 session_zero 时仍是直接开局")
finally:
    _ls3._stop_old()
    _lb3.abort()

# ---- J. 角色卡库 + 人生路径 8 问 ---- 
_lb4 = WebBridge(timeout=0.1)
_ls4 = WebSession(_lb4, ADV, tempfile.mkdtemp(), characters_dir=tempfile.mkdtemp())
try:
    _answers = {q["id"]: q["options"][0]["id"] for q in QUESTIONS}
    _built = build_lifepath(_answers, name="问卷人")
    ok(len(QUESTIONS) == 8 and set(_built["payload"]) >= {"klass", "species", "skills"},
       "J1 ⭐ 人生路径 8 问能生成建卡 payload", _built["payload"])
    _pv = _ls4.create_preview(_built["payload"])
    ok(_pv.get("ok") and _pv["value"]["name"] == "问卷人",
       "J2 问卷 payload 能过 create_character", _pv.get("value"))
    _sv4 = _ls4.save_character(dict(_built["payload"], lifepath=_built["summary"]))
    _lib = _ls4.list_characters()
    ok(_sv4.get("ok") and len(_lib) == 1 and _lib[0]["name"] == "问卷人",
       "J3 ⭐ 角色卡能存进卡库并列出", _lib)
    _ld = _ls4.load_character(_sv4["value"]["id"])
    ok(_ld.get("ok") and _ld["value"]["choices"]["klass"] == _built["payload"]["klass"],
       "J4 ⭐ 卡库能读回建卡选择", _ld.get("value", {}).get("choices"))
    _dl = _ls4.delete_character(_sv4["value"]["id"])
    ok(_dl.get("ok") and _ls4.list_characters() == [],
       "J5 卡库能删除")
finally:
    _ls4._stop_old()
    _lb4.abort()

# ---- K. AI 自建卡的成功路径（用假座位返回 JSON，不调网络） ----
class _GenSeat:
    real = True
    def __init__(self, obj):
        self.obj = obj
    def complete(self, req):
        return ModelReply(text=json.dumps(self.obj, ensure_ascii=False), data=self.obj)

_lb5 = WebBridge(timeout=0.1)
_ls5 = WebSession(_lb5, ADV, tempfile.mkdtemp(), session_zero=True)
try:
    _ls5.new_game("normal")
    _ls5.set_table_size(2)
    _names5 = [n for n in _ls5.state.characters() if n != "布兰"]
    _ls5.loop.client.players[_names5[0]] = _GenSeat(
        {"klass": "Ranger", "species": "Elf",
         "skills": ["perception", "survival", "nature"], "spells": []})
    _ls5.loop.client.players[_names5[1]] = _GenSeat(
        {"klass": "Bard", "species": "Human",
         "skills": ["performance", "persuasion", "deception"], "spells": []})
    _g5 = _ls5.generate_companions()
    _c5 = {x["name"]: x["job"] for x in _ls5.snapshot_extra()["lobby"]["characters"]}
    ok(_g5.get("ok") and _g5.get("value")
       and not _g5.get("errors")
       and _ls5.snapshot_extra()["lobby"]["companion_source"] == "ai",
       "K1 ⭐ AI 返回合法 JSON 时能自建卡", (_g5, _c5))
    ok("游侠" in _c5.get(_names5[0], "") and "吟游诗人" in _c5.get(_names5[1], ""),
       "K2 ⭐ 自建卡真的进了本桌卡列", (_g5, _c5))
finally:
    _ls5._stop_old()
    _lb5.abort()

# ---- L. B 方案：DM 提案同伴（假 DM 座位返回 JSON） ----
_lb6 = WebBridge(timeout=0.1)
_ls6 = WebSession(_lb6, ADV, tempfile.mkdtemp(), session_zero=True)
try:
    _ls6.new_game("normal")
    _ls6.set_table_size(2)
    _ls6.loop.client.dm = _GenSeat([
        {"name": "灰羽", "klass": "Ranger", "species": "Elf",
         "skills": ["perception", "survival", "nature"]},
        {"name": "铜锤", "klass": "Fighter", "species": "Dwarf",
         "skills": ["athletics", "intimidation"]},
    ])
    _d6 = _ls6.dm_companions("我想要一个能看路、一个能顶前排的")
    _cards6 = {x["name"]: x["job"] for x in _ls6.snapshot_extra()["lobby"]["characters"]}
    ok(_d6.get("ok") and _ls6.snapshot_extra()["lobby"]["companion_source"] == "dm",
       "L1 ⭐ DM 能按玩家描述提案同伴", _d6)
    ok("游侠" in _cards6.get("灰羽", "") and "战士" in _cards6.get("铜锤", ""),
       "L2 ⭐ DM 提案的卡进了本桌", _cards6)
finally:
    _ls6._stop_old()
    _lb6.abort()

_lb7 = WebBridge(timeout=0.1)
_ls7 = WebSession(_lb7, ADV, tempfile.mkdtemp(), session_zero=True)
try:
    _ls7.new_game("normal")
    _ls7.set_table_size(1)
    _d7 = _ls7.dm_companions("随便来一个")
    ok(not _d7.get("ok") and "假模型" in (_d7.get("error") or ""),
       "L3 假 DM 不假装能提案", _d7)
finally:
    _ls7._stop_old()
    _lb7.abort()

# ---- M. A 方案：玩家手搓同伴卡 ----
_lb8 = WebBridge(timeout=0.1)
_ls8 = WebSession(_lb8, ADV, tempfile.mkdtemp(), session_zero=True,
                  characters_dir=tempfile.mkdtemp())
try:
    _ls8.new_game("normal")
    _ls8.set_table_size(2)
    _a1 = build_lifepath({q["id"]: q["options"][0]["id"] for q in QUESTIONS},
                         name="灰羽")["payload"]
    _a2 = build_lifepath({q["id"]: q["options"][1]["id"] for q in QUESTIONS},
                         name="铜锤")["payload"]
    _s1 = _ls8.save_character(dict(_a1, role="companion"))
    _s2 = _ls8.save_character(dict(_a2, role="companion"))
    _comp = _ls8.list_characters(role="companion")
    ok(len(_comp) == 2 and all(x["role"] == "companion" for x in _comp),
       "M1 ⭐ 同伴卡按 role 存进卡库", _comp)
    _u8 = _ls8.use_companion_cards([_s1["value"]["id"], _s2["value"]["id"]])
    _c8 = {x["name"]: x["job"] for x in _ls8.snapshot_extra()["lobby"]["characters"]}
    ok(_u8.get("ok") and _ls8.snapshot_extra()["lobby"]["companion_source"] == "manual",
       "M2 ⭐ 手搓同伴卡能上桌", _u8)
    ok("灰羽" in _c8 and "铜锤" in _c8, "M3 同伴卡名字进了卡列", _c8)
    _bad8 = _ls8.use_companion_cards([_s1["value"]["id"]])
    ok(not _bad8.get("ok") and "要选 2 张" in (_bad8.get("error") or ""),
       "M4 人数不够明确报错", _bad8)
finally:
    _ls8._stop_old()
    _lb8.abort()

# ---- N. 从卡库选真人卡 ----
_lb9 = WebBridge(timeout=0.1)
_ls9 = WebSession(_lb9, ADV, tempfile.mkdtemp(), session_zero=True,
                  characters_dir=tempfile.mkdtemp())
try:
    _ls9.new_game("normal")
    _ls9.set_table_size(2)
    _h9 = _ls9.save_character(dict(
        build_lifepath({q["id"]: q["options"][0]["id"] for q in QUESTIONS},
                       name="卡库真人")["payload"], role="human"))
    _u9 = _ls9.use_human_card(_h9["value"]["id"])
    _c9 = {x["name"]: x["job"] for x in _ls9.snapshot_extra()["lobby"]["characters"]}
    ok(_u9.get("ok") and "卡库真人" in _c9,
       "N1 ⭐ 卡库真人卡能替换真人座位", _c9)
    ok(len(_ls9.snapshot_extra()["lobby"]["characters"]) == 3,
       "N2 换真人卡不动已有 AI 人数")
    _cp9 = _ls9.save_character(dict(
        build_lifepath({q["id"]: q["options"][1]["id"] for q in QUESTIONS},
                       name="同伴卡")["payload"], role="companion"))
    _bad9 = _ls9.use_human_card(_cp9["value"]["id"])
    ok(not _bad9.get("ok") and "不是真人卡" in (_bad9.get("error") or ""),
       "N3 同伴卡不能当真人卡用", _bad9)
finally:
    _ls9._stop_old()
    _lb9.abort()

# ---- O. 5 人桌：1 真人 + 4 AI ----
_lb10 = WebBridge(timeout=0.1)
_ls10 = WebSession(_lb10, ADV, tempfile.mkdtemp(), session_zero=True)
try:
    _ls10.new_game("normal")
    _r10 = _ls10.set_table_size(4)
    _s10 = _ls10.snapshot_extra()["lobby"]
    ok(_r10.get("ok") and len(_s10["characters"]) == 5
       and _s10["table"]["total"] == 5,
       "O1 ⭐ 1 真人 + 4 AI 能建 5 人桌", _s10.get("table"))
    _i10 = _ls10.lobby_intros()
    ok(_i10.get("ok") and len(_ls10.snapshot_extra()["lobby"]["intros"]) == 4,
       "O2 ⭐ 4 个 AI 队友各说一句")
    _ls10.start_game()
finally:
    _ls10._stop_old()
    _lb10.abort()

_lb11 = WebBridge(timeout=0.1)
try:
    _s11, _loop11 = build_web_table(_lb11, human_char=None,
                                    companions=list(COMPANION_NAMES))
    ok(len(_s11.characters()) == 5,
       "O3 ⭐ 默认真人卡 + 4 AI 不会在重建时把队友清掉",
       _s11.characters())
finally:
    _lb11.abort()

# ---- F. 页面接线（静态检查，不跑浏览器） ----
ok('id="gameMode"' in PAGE_HTML and 'value="spectate"' in PAGE_HTML,
   "F1 页面有模式选择（真人/托管/观战）")
ok('id="bgmBtn"' in PAGE_HTML and "AudioContext" in PAGE_HTML,
   "F2 页面有 BGM 开关与合成器")
ok("applyMode" in PAGE_HTML and 'id="modebanner"' in PAGE_HTML,
   "F3 观战/托管横幅与输入框切换接了")
ok("new Audio(" not in PAGE_HTML, "F4 BGM 不依赖外部音频文件")
ok('id="modeStart"' in PAGE_HTML, "F5 按当前模式新开的入口在")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for x in FAILS:
        print(f"  ✗ {x}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")