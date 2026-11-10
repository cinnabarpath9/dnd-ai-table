# coding=utf-8
"""RealModel 的**离线**接线测试：不联网，用假 transport 验请求/解析/错误口径。"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engine.models.client as client_mod  # noqa: E402
from engine.models import ModelReply, ModelRequest, RealModel  # noqa: E402

# run_all 默认 DND_NO_REAL_CALLS=1（Q08）；本测试的 A/B 段故意用假 transport
# 验“请求长什么样”，所以先摘掉闸门；B6 再单独把它设上验证闸门本身。
os.environ.pop("DND_NO_REAL_CALLS", None)

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


class Resp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._raw


ok(RealModel().enabled is False,
   "A0 RealModel 默认不发请求（enabled=False）")


def model(**kw):
    base = dict(name="ds", model="deepseek-v4.1-flash", base_url="http://fake",
                api_key="k", enabled=True)
    base.update(kw)
    return RealModel(**base)


def request(expect="intent", prompt="只输出 JSON"):
    return ModelRequest(role="player", actor="布兰", task="combat_turn",
                        prompt=prompt, expect=expect)


GOOD = {"model": "deepseek-v4.1-flash",
        "choices": [{"message": {"content": '{"id":"a1","target":"E1"}',
                                  "reasoning_content": "先打最近的 E1"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8,
          "prompt_tokens_details": {"cached_tokens": 5}}}

seen = {}


def fake_ok(req, timeout=0):
    seen["url"] = req.full_url
    seen["auth"] = req.get_header("Authorization")
    seen["body"] = json.loads(req.data.decode())
    return Resp(GOOD)


orig = client_mod.urllib.request.urlopen
try:
    client_mod.urllib.request.urlopen = fake_ok
    m = model()
    r = m.complete(request())
finally:
    client_mod.urllib.request.urlopen = orig

ok(r.ok and r.text == '{"id":"a1","target":"E1"}', "A1 正文原样返回", r)
ok(r.data == {"id": "a1", "target": "E1"}, "A2 自动解析 JSON 进 data", r.data)
ok(r.prompt_tokens == 12 and r.completion_tokens == 8, "A3 用量按上游记账", r)
ok(r.reasoning == "先打最近的 E1", "A3b ⭐ 思考链原样留痕", r.reasoning)
ok(r.cached_tokens == 5 and abs(r.cache_hit_rate - 5 / 12) < 1e-9,
   "A3c ⭐ 提示缓存命中按上游记账", (r.cached_tokens, r.cache_hit_rate))
ok(m.totals()["cached_tokens"] == 5, "A3d 总账含 cached_tokens",
   m.totals())
ok(seen["url"].endswith("/chat/completions"), "A4 走 /chat/completions", seen["url"])
ok(seen["auth"] == "Bearer k", "A5 Authorization 正确（不打印 key）")
ok(seen["body"]["model"] == "deepseek-v4.1-flash"
   and seen["body"]["messages"][1]["content"] == "只输出 JSON",
   "A6 model 与 prompt 都发出去了", seen["body"]["model"])
ok(seen["body"]["max_tokens"] >= 4096, "A7 给足输出预算（推理也占）",
   seen["body"]["max_tokens"])


def fake_empty(req, timeout=0):
    return Resp({"model": "deepseek-v4.1-flash",
                 "choices": [{"message": {"content": "",
                                          "reasoning_content": "想了 512 字"},
                              "finish_reason": "length"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 512}})


try:
    client_mod.urllib.request.urlopen = fake_empty
    r2 = model().complete(request())
finally:
    client_mod.urllib.request.urlopen = orig
ok(not r2.ok and "推理" in r2.error, "A8 推理占满导致空正文 → 明确失败", r2.error)
ok(r2.reasoning == "想了 512 字", "A8b 空正文时也保住思考链", r2.reasoning)


def fake_500(req, timeout=0):
    raise urllib.error.HTTPError("http://fake", 500, "boom", None,
                                 io.BytesIO(b"upstream down"))


m3 = model()
m3._sleep = lambda _s: None
calls = {"n": 0}


def fake_500_count(req, timeout=0):
    calls["n"] += 1
    return fake_500(req, timeout)


try:
    client_mod.urllib.request.urlopen = fake_500_count
    r3 = m3.complete(request())
finally:
    client_mod.urllib.request.urlopen = orig
ok(not r3.ok and "HTTP 500" in r3.error, "B1 HTTP 500 → 失败且带状态", r3.error)
ok(calls["n"] == 2, "B2 5xx 按策略重试一次", calls["n"])

m4 = model(enabled=False)
r4 = m4.complete(request())
ok(not r4.ok and "禁用" in r4.error, "B3 未授权（enabled=False）不发请求", r4.error)
ok(m4.calls[0].attempts == 0, "B4 记账里尝试次数为 0", m4.calls[0].attempts)

m5 = model(api_key="")
r5 = m5.complete(request())
ok(not r5.ok and "api_key" in r5.error,
   "B5 没有 key 直接拒绝（不构造坏请求）", r5.error)

# 进程级真调用闸门：环境变量一设，连 urlopen 都不许进
os.environ["DND_NO_REAL_CALLS"] = "1"
sentinel = {"called": False}


def _must_not_request(req, timeout=0):
    sentinel["called"] = True
    raise AssertionError("DND_NO_REAL_CALLS=1 时仍发出了真请求")


orig_gate = client_mod.urllib.request.urlopen
try:
    client_mod.urllib.request.urlopen = _must_not_request
    m6 = model()
    r6 = m6.complete(request())
finally:
    client_mod.urllib.request.urlopen = orig_gate
    os.environ.pop("DND_NO_REAL_CALLS", None)
ok(not r6.ok and "DND_NO_REAL_CALLS" in r6.error,
   "B6 ⭐ 进程闸门：DND_NO_REAL_CALLS=1 在发请求前拒绝", r6.error)
ok(not sentinel["called"] and m6.calls[0].attempts == 0,
   "B7 ⭐ 闸门没碰网络，尝试数 0", (sentinel, m6.calls[0].attempts))
ok(model(enabled=False).complete(request()).ok is False,
   "B8 闸门恢复后 enabled=False 仍按原逻辑拒绝")

import argparse  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "tools"))
import real_smoke  # noqa: E402

orig_complete = RealModel._complete
RealModel._complete = lambda self, req: ModelReply(text="{}", data={})
try:
    counter = {"n": 0}
    capped = real_smoke.CappedReal(counter, 2, name="x", model="m",
                                   base_url="http://x", api_key="k",
                                   enabled=True)
    capped.complete(request())
    capped.complete(request())
    blocked = capped.complete(request())
finally:
    RealModel._complete = orig_complete
ok(counter["n"] == 2 and not blocked.ok and "上限" in blocked.error,
   "C1 ⭐ smoke 调用硬闸：到上限不再发请求", (counter, blocked.error))

ok(real_smoke._scene_count("all") == "all"
   and real_smoke._scene_count("2") == "2", "C2 --scenes 解析：all / 数字")
try:
    real_smoke._scene_count("4")
    raised = False
except argparse.ArgumentTypeError:
    raised = True
ok(raised, "C3 --scenes 越界在 argparse 层报错（不跑起来才炸）")

logger = real_smoke.LoggedReal(name="log", model="m", base_url="http://x",
                               api_key="k", enabled=True)
logger._complete = lambda req: (_ for _ in ()).throw(RuntimeError("boom"))
failed = logger.complete(request())
ok(not failed.ok and len(logger.records) == 1
   and "boom" in failed.error,
   "C4 ⭐ 失败调用也进 trace（不再漏失败现场）",
   (len(logger.records), failed.error))

logger2 = real_smoke.LoggedReal(name="log", model="m", base_url="http://x",
                                api_key="k", enabled=True)
logger2._complete = lambda req: ModelReply(
    text="{}", data={}, prompt_tokens=10, completion_tokens=2, cached_tokens=4)
logger2.complete(request())
ok(logger2.records[-1]["cached_tokens"] == 4,
   "C5 ⭐ trace 记录带 cached_tokens", logger2.records[-1])

os.environ["DND_NO_REAL_CALLS"] = "1"
try:
    gate_code = real_smoke.main(["--scenes", "1"])
finally:
    os.environ.pop("DND_NO_REAL_CALLS", None)
ok(gate_code == 2, "C6 ⭐ smoke 入口遇到进程闸门直接退出（不读 key/不跑）",
   gate_code)

# --only 的解析：只认剧本里真有的场次，且按剧本顺序
_saved_argv = sys.argv
try:
    pass
finally:
    sys.argv = _saved_argv
import types  # noqa: E402


def _only_result(arg: str):
    """不真跑：只把 --only 解析到“选中哪几场”这一步就返回。

    ⚠️ 同时把 `_read_key` 打桩成常量：否则这个断言会依赖**本机有 key**，
    换台机器/CI 上就假红（外审 P2-5）。
    """
    seen = {}

    def fake_build(*a, **kw):
        raise RuntimeError("stop-before-build")

    orig_build = real_smoke.build
    orig_key = real_smoke._read_key
    real_smoke.build = fake_build
    real_smoke._read_key = lambda: "test-key"
    try:
        code = real_smoke.main(["--adventure",
                                os.path.join(ROOT, "adventures",
                                             "雾渡的第三声钟.json"),
                                "--only", arg, "--max-calls", "1"])
    except RuntimeError as exc:
        seen["code"] = str(exc)
        code = None
    finally:
        real_smoke.build = orig_build
        real_smoke._read_key = orig_key
    return code, seen


_code, _seen = _only_result("s5")
ok(_code is None and _seen.get("code") == "stop-before-build",
   "C11 ⭐ --only 合法场次：能走到建客户端那一步（不报场次错）", (_code, _seen))
_bad_code, _ = _only_result("s99")
ok(_bad_code == 2, "C11b ⭐ --only 里有不存在的场次 → 退出 2（还没读 key/没花钱）",
   _bad_code)

import types  # noqa: E402

_only = types.SimpleNamespace(save="", only="s3", save_dir="saves/real",
                              adventure="adventures/雾渡的第三声钟.json",
                              model="deepseek-v4.1-flash")
ok(real_smoke.save_path_for(_only) == "",
   "C12 ⭐ --only 单场复验不自动写档（不把整本真档覆盖成半截）")
_full = types.SimpleNamespace(save="", only="", save_dir="saves/real",
                              adventure="adventures/雾渡的第三声钟.json",
                              model="deepseek-v4.1-flash")
ok(real_smoke.save_path_for(_full).startswith("saves/real")
   and not os.path.isabs(real_smoke.save_path_for(_full)),
   "C12b 整本跑会写 saves/real 下的自动档（相对路径，不是任意绝对路径）",
   real_smoke.save_path_for(_full))
ok(real_smoke.save_path_for(types.SimpleNamespace(
    save="x.json", only="s3", save_dir="s", adventure="a", model="m")) == "x.json",
   "C12c 显式 --save 优先，--only 也照写")

import tempfile  # noqa: E402
import time  # noqa: E402

with tempfile.TemporaryDirectory(prefix="dnd-trace-") as td:
    for i in range(4):
        real_smoke.write_trace(os.path.join(td, f"t{i}.jsonl"), seed=1,
                               model="m", adventure="a", scenes=[],
                               seats={}, keep=2, max_bytes=0)
        time.sleep(0.02)
    names = sorted(os.listdir(td))
    ok(names == ["t2.jsonl", "t3.jsonl"],
       "C7 ⭐ trace 份数闸：keep=2 只留最近两份（不靠 gitignore 兜底）", names)
    rows = [json.loads(x) for x in open(os.path.join(td, "t3.jsonl"),
                                        encoding="utf-8") if x.strip()]
    ok(rows and rows[0].get("type") == "meta",
       "C7b 裁完的 trace 仍是完整 JSONL（meta 行在）", rows[:1])

with tempfile.TemporaryDirectory(prefix="dnd-trace-mb-") as td2:
    for i in range(3):
        real_smoke.write_trace(os.path.join(td2, f"t{i}.jsonl"), seed=1,
                               model="m", adventure="a", scenes=[],
                               seats={}, keep=99, max_bytes=1)
        time.sleep(0.02)
    sizes = [os.path.getsize(os.path.join(td2, x)) for x in os.listdir(td2)]
    ok(len(sizes) == 1, "C8 ⭐ trace 字节闸：上限 1 字节时只留最新一份", sizes)

saved = real_smoke.default_save_path(
    os.path.join("D:", "saves", "real"), "adventures/雾渡的第三声钟.json",
    "deepseek-v4.1-flash")
ok(os.path.dirname(saved) == os.path.join("D:", "saves", "real")
   and os.path.basename(saved) == "雾渡的第三声钟-deepseek-v4.1-flash.json",
   "C9 ⭐ 真局默认存档：剧本+模型固定一个名字（旧档才进 history 按预算裁）", saved)
ok(real_smoke.default_save_path("x", "a/b:c*.json", "m/n") ==
   os.path.join("x", "b-c-m-n.json"),
   "C9b 路径/非法字符被替换，不会写出子目录",
   real_smoke.default_save_path("x", "a/b:c*.json", "m/n"))

with tempfile.TemporaryDirectory(prefix="dnd-trace-keep-") as td3:
    foreign = os.path.join(td3, "别人的笔记.jsonl")
    with open(foreign, "w", encoding="utf-8") as f:
        f.write('{"note": "这不是 trace"}\n')
    real_smoke.write_trace(os.path.join(td3, "t0.jsonl"), seed=1, model="m",
                           adventure="a", scenes=[], seats={}, keep=1,
                           max_bytes=0)
    time.sleep(0.02)
    real_smoke.write_trace(os.path.join(td3, "t1.jsonl"), seed=1, model="m",
                           adventure="a", scenes=[], seats={}, keep=1,
                           max_bytes=0)
    left = sorted(os.listdir(td3))
    ok(left == ["t1.jsonl", "别人的笔记.jsonl"],
       "C10 ⭐ 裁 trace 不误删别人的 JSONL（只认自己的 meta 行）", left)

with tempfile.TemporaryDirectory(prefix="dnd-trace-older-") as td4:
    real_smoke.write_trace(os.path.join(td4, "before.jsonl"), seed=1, model="m",
                           adventure="a", scenes=[], seats={}, keep=99,
                           max_bytes=0)
    time.sleep(0.05)
    target = os.path.join(td4, "current.jsonl")
    real_smoke.write_trace(target, seed=1, model="m", adventure="a",
                           scenes=[], seats={}, keep=99, max_bytes=0)
    os.utime(target, (1000, 1000))       # 假装它是"最旧的"
    real_smoke._prune_traces(target, keep=1, max_bytes=0)
    left = sorted(os.listdir(td4))
    ok(left == ["current.jsonl"],
       "C10b ⭐ 刚写的那份永远不裁（mtime 变老也不行），其余按份数裁", left)

# --------------------------------------------------------------------------- #
print("\n=== D. 叙事预算：max_tokens 是推理+正文的总和（M2 上章实测掉过 2 次） ===")


def _payload_cap(expect: str) -> int:
    seen = {}

    def fake(req, **kw):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return Resp({"choices": [{"message": {"content": "旁白"}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    orig = client_mod.urllib.request.urlopen
    try:
        client_mod.urllib.request.urlopen = fake
        model().complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                   prompt="说点什么", expect=expect))
    finally:
        client_mod.urllib.request.urlopen = orig
    return int(seen["payload"]["max_tokens"])


_ok = [json.dumps({"id": "a1", "target": "E1"})]
ok(_payload_cap("narration") >= 2400,
   "D1 ⭐ 叙事请求的 max_tokens ≥2400（推理吃掉预算时正文还有位置）",
   _payload_cap("narration"))
ok(_payload_cap("intent") == 12000,
   "D2 意图/规划请求用默认 12000（用户口径：套餐便宜，让它多想想）",
   _payload_cap("intent"))

# D2b：叙事也给到 6000（原来 3000 常被思考链吃光 → 空正文）。
ok(_payload_cap("narration") == 12000,
   "D2b ⭐ 叙事也给满 12000（推理+正文共用；6000 在真模型里仍被吃光）",
   _payload_cap("narration"))

# D3：敌方规划被 max_tokens 截断（长跑批次实测）——能捞出前面完整的那几条，
# 不让整轮所有敌人白站；单个对象的截断**不能**瞎捞（否则会执行半截意图）。
from engine.models.client import _try_json  # noqa: E402
_trunc = ('[{"unit":"E1","actions":[{"id":"a1"}]},'
          '{"unit":"E2","actions":[{"id":"a1"}]},'
          '{"unit":"E4","act')
ok(_try_json(_trunc) == [{"unit": "E1", "actions": [{"id": "a1"}]},
                         {"unit": "E2", "actions": [{"id": "a1"}]}],
   "D3 ⭐ 截断的数组能捞出前面完整的对象", _try_json(_trunc))
ok(_try_json('{"id":"a1","targ') is None,
   "D3b 单个对象被截断时不瞎捞（宁可不行动）")

# --------------------------------------------------------------------------- #
print("\n=== E. 战役模式：两章连跑，第二章开头带上一章欠账 ===")

CAMPAIGN = os.path.join(ROOT, "adventures", "M2测试本.campaign.json")


def _campaign_run(chapters: str, scenes: str = "1"):
    """用假模型跑 campaign 模式，返回 (退出码, 每章的 DM 提示) 。"""
    from engine.dice import Random
    from engine.models import FakeModel, TableClient
    from engine.rules.loop import GameLoop, make_hero_unit
    from engine.state import Ledger, StateTree, new_character

    names = ("布兰", "艾尔玛")
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", hp_max=24, ac=15)
         for n in names}, current=names[0])
    st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}} for n in names],
              actor="填表", reason="装备")
    table = TableClient(dm=FakeModel(name="dm"), planner=FakeModel(name="pl"),
                        players={n: FakeModel(name=n) for n in names})
    loop = GameLoop(st, Ledger(), table, rng=Random(20261108))
    loop.set_field([make_hero_unit(st, n) for n in names])

    prompts = []
    orig_dm = table.seats()["dm"].complete

    def spy(req):
        prompts.append(req.prompt)
        return orig_dm(req)

    table.seats()["dm"].complete = spy

    tmpdir = tempfile.mkdtemp(prefix="dnd-camp-prog-")
    orig_build, orig_key = real_smoke.build, real_smoke._read_key
    import engine.adventure.campaign as _camp_mod
    import engine.adventure.progress as _prog_mod
    orig_prog = real_smoke.progress_path_for
    orig_camp_prog = _camp_mod.progress_path_for
    orig_prog_fn = _prog_mod.progress_path_for
    # 进度文件写到临时目录：测试不许往 adventures/ 里留产物（外审那条的先例）
    def _prog_in_tmp(p):
        return os.path.join(
            tmpdir, os.path.splitext(os.path.basename(p))[0] + ".progress.json")

    # 三个取路径的入口都要指到临时目录，否则测试会写真实的 adventures/*.progress.json
    real_smoke.progress_path_for = _prog_in_tmp
    _camp_mod.progress_path_for = _prog_in_tmp
    _prog_mod.progress_path_for = _prog_in_tmp
    real_smoke.build = lambda *a, **kw: (st, loop, {"n": 0})
    real_smoke._read_key = lambda: "test-key"
    try:
        save_to = os.path.join(tmpdir, "c.json")
        code = real_smoke.main(["--campaign", CAMPAIGN, "--chapters", chapters,
                                "--scenes", scenes, "--save", save_to,
                                "--max-calls", "999"])
    finally:
        real_smoke.build = orig_build
        real_smoke._read_key = orig_key
        real_smoke.progress_path_for = orig_prog
        _camp_mod.progress_path_for = orig_camp_prog
        _prog_mod.progress_path_for = orig_prog_fn
    return code, prompts, tmpdir


_code, _prompts, _ = _campaign_run("ch1,ch2")
ok(_code == 0, "E1 ⭐ 战役模式两章连跑跑通（假模型）", _code)
ok(len(_prompts) >= 2, "E2 两章各至少一次 DM 叙事", len(_prompts))
# 第二章第一场的 DM 提示里应带第一章的欠账（campaign 层从进度文件里读）
ok(any("欠账" in p or "上一章" in p for p in _prompts[1:]),
   "E3 ⭐ 第二章开头的 DM 提示带上一章欠账", _prompts[1][:120] if len(_prompts) > 1 else "")

_code_bad, _, _ = _campaign_run("ch1,chX")
ok(_code_bad == 2, "E4 战役里不存在的章 → 退出 2（还没读 key/没花钱）", _code_bad)

# E5：欠账必须在**轮到该章时**才算。提前算会拿到空进度，
# 把上一章已经兑现的伏笔又列一遍（2026-11-08 同档真跑踩到，事后修）。
_code5, _prompts5, _prog5 = _campaign_run("ch1,ch2", scenes="all")
_debt5 = next((p[p.find("上一章还欠着的伏笔"):] for p in _prompts5
               if "上一章还欠着的伏笔" in p), "")
ok(_code5 == 0, "E5 两章整跑（假模型）跑通", _code5)
ok("p3 水寨的灯" in _debt5,
   "E6 ⭐ 第二章欠账里有 ch1 没兑现的 p3", _debt5[:160])
ok("p1 钟绳上的黑蜡" not in _debt5,
   "E7 ⭐ 第二章欠账里没有 ch1 已在 a7 兑现的 p1（欠账按最新进度算）",
   _debt5[:160])
ok("提过" in _debt5 and "提过 0 次" not in _debt5,
   "E8 ⭐ 欠账带真实“提过几次”（不是空进度；按完整短语比对，避免 10 次被误判成 0 次）",
   _debt5[:120])

# E9：ch2 的 b5 声明 `pays_external: ["ch1:p3"]` —— 兑现要落在**ch1 的进度文件**里，
# 下一章的 debts() 才不会又把 p3 列成欠着（Q23）。
import json as _json  # noqa: E402

_ch1_prog = os.path.join(_prog5, "M2测试本-上-钟下的账.progress.json")
ok(os.path.exists(_ch1_prog), "E9 ch1 的进度文件落在临时目录（测试不碰真实 adventures/）")
try:
    _ch1_doc = _json.load(open(_ch1_prog, encoding="utf-8"))
except (OSError, ValueError):
    _ch1_doc = {}
_p3 = ((_ch1_doc.get("promises") or {}).get("p3") or {})
ok(bool(_p3.get("closed")),
   "E10 ⭐ 跨章兑现：ch2 的 b5 把 ch1 的 p3 在**ch1 进度**里关掉", _p3)
ok("ch2" in str(_p3.get("closed_note") or ""),
   "E11 兑现记录写清是哪一章还的", _p3.get("closed_note"))

# E12：重算欠账，p3 不该再出现（欠账只读进度，不读剧本里“还欠着”的直觉）
import engine.adventure.campaign as _camp2  # noqa: E402
from engine.adventure import debts as _debts, load_campaign as _lc  # noqa: E402

_orig_camp_prog2 = _camp2.progress_path_for
_camp2.progress_path_for = lambda p: os.path.join(
    _prog5, os.path.splitext(os.path.basename(p))[0] + ".progress.json")
try:
    _rows = _debts(_lc(CAMPAIGN), "ch2")
finally:
    _camp2.progress_path_for = _orig_camp_prog2
ok(not any(r["id"] == "p3" for r in _rows),
   "E12 ⭐ 兑现后重算欠账，p3 不再列出", [r["id"] for r in _rows])

# --------------------------------------------------------------------------- #
print("\n=== F. 多座位配置：reasoning_effort 只在显式给值时发送 ===")


def _payload_effort(effort, temperature=0.7):
    seen = {}

    def fake(req, **kw):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return Resp({"choices": [{"message": {"content": "旁白"}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    orig = client_mod.urllib.request.urlopen
    try:
        client_mod.urllib.request.urlopen = fake
        m = model(reasoning_effort=effort, temperature=temperature)
        m.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                prompt="说点什么", expect="narration"))
    finally:
        client_mod.urllib.request.urlopen = orig
    return seen["payload"]


_p = _payload_effort("medium")
ok(_p.get("reasoning_effort") == "medium",
   "F1 ⭐ 显式档位会进请求体", _p.get("reasoning_effort"))
_p = _payload_effort(None)
ok("reasoning_effort" not in _p,
   "F2 ⭐ 不填就不发（Gemini/GLM 走这路）", sorted(_p))
_p = _payload_effort("")
ok("reasoning_effort" not in _p,
   "F3 空串等同不填", _p.get("reasoning_effort"))
_p = _payload_effort("medium", temperature=None)
ok("temperature" not in _p,
   "F4 ⭐ temperature=null 时不发（Claude 这类上游拒绝自定义温度）", sorted(_p))


def _payload_intent_none():
    seen = {}

    def fake(req, **kw):
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return Resp({"choices": [{"message": {"content": "{\"action\":\"look\"}"}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    orig = client_mod.urllib.request.urlopen
    try:
        client_mod.urllib.request.urlopen = fake
        m = model(temperature=None)
        m.complete(ModelRequest(role="player", actor="小翠", task="explore_action",
                                prompt="只输出 JSON", expect="intent"))
    finally:
        client_mod.urllib.request.urlopen = orig
    return seen["payload"]


_p = _payload_intent_none()
ok("temperature" not in _p,
   "F5 ⭐ intent 任务也不能偷偷给 Claude 发 temperature", sorted(_p))

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")











