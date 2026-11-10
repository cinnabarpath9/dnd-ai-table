# coding=utf-8
"""网页复盘：lite 列表 / 单条全文 / 路径安全。"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import review as rv  # noqa: E402
from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(label)
        print(f"  ✗ {label}  extra={extra!r}")


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


td = tempfile.mkdtemp(prefix="rv-")
save = os.path.join(td, "a1.json")
json.dump({"format": "x", "client": {"calls": []}},
          open(save, "w", encoding="utf-8"))
rows = [
    {"seq": 1, "actor": "dm", "role": "dm", "task": "narrate", "model": "m",
     "prompt_tokens": 100, "completion_tokens": 50, "cached_tokens": 0,
     "elapsed": 5.5, "ok": True, "error": "",
     "reasoning_len": 9, "reasoning": "我在想渡口",
     "prompt_chars": 6, "prompt": "上下文内容"},
    {"seq": 2, "actor": "布兰", "role": "player", "task": "explore_action",
     "model": "m", "prompt_tokens": 80, "completion_tokens": 20,
     "cached_tokens": 0, "elapsed": 2.0, "ok": False, "error": "超时",
     "reasoning_len": 0, "reasoning": "", "prompt_chars": 0, "prompt": ""},
]
with open(os.path.join(td, "a1.calls.jsonl"), "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
# 干扰文件：旁挂、坏 JSON、无旁挂存档
json.dump({"client": {"calls": [{"seq": 9, "actor": "x"}]}},
          open(os.path.join(td, "b.json"), "w", encoding="utf-8"))
open(os.path.join(td, "c.json"), "w").write("{坏")

# ---- A. 数据层 ----
games = rv.list_games(td)
names = [g["name"] for g in games]
ok("a1.calls.jsonl" not in names, "A1 旁挂文件不当作存档列出")
ok(set(names) >= {"a1.json", "b.json"}, "A2 列出主存档", names)
ok(len(rv.calls_for(save)) == 2, "A3 读出旁挂两行")
ok(len(rv.calls_for(os.path.join(td, "b.json"))) == 1,
   "A4 没旁挂时退回主存档元数据")

lite = rv.lite_rows(rows)
ok(all("reasoning" not in x and "prompt" not in x for x in lite),
   "A5 ⭐ lite 不含思考链/prompt（只有长度）", sorted(lite[0]))
ok(lite[0]["reasoning_len"] == 9 and lite[0]["prompt_chars"] == 6,
   "A6 lite 带长度")
ok(lite[0]["error"] == "" and lite[1]["error"] == "超时", "A7 失败信息保留")

full = rv.full_row(rows, 0)
ok(full["reasoning"] == "我在想渡口" and full["prompt"] == "上下文内容",
   "A8 ⭐ 单条全文能取回思考链与 prompt")
ok(rv.full_row(rows, 2) is None and rv.full_row(rows, -1) is None,
   "A9 越界返回 None（不猜）")

ok(rv.resolve(td, "a1.json") == save, "A10 合法名字能解析")
for bad in ("../a1.json", "..\\a1.json", "a1.calls.jsonl", "c.json.bak", "", "a1.txt"):
    ok(rv.resolve(td, bad) == "", f"A11 拒绝非法名字 {bad!r}")

# ---- B. HTTP ----
bridge = WebBridge(print_log=False)
server = TableServer(bridge, port=0, review_dir=lambda: td).start()
try:
    code, body = get(server.url + "api/games")
    d = json.loads(body)
    ok(code == 200 and d["ok"] and len(d["games"]) >= 2, "B1 /api/games 列出存档")

    code, body = get(server.url + "api/game?name=a1.json&lite=1")
    d = json.loads(body)
    ok(code == 200 and len(d["calls"]) == 2, "B2 lite 列表 2 条")
    ok("reasoning" not in d["calls"][0], "B3 ⭐ lite 不带思考链内容", d["calls"][0])
    ok(d["calls"][0]["reasoning_len"] == 9, "B4 lite 带长度")

    code, body = get(server.url + "api/game/call/0?name=a1.json")
    d = json.loads(body)
    ok(code == 200 and d["call"]["reasoning"] == "我在想渡口",
       "B5 ⭐ 单条接口返回思考链全文", d.get("call", {}).get("reasoning"))
    ok(d.get("spoiler") is True, "B6 单条标 spoiler")

    code, body = get(server.url + "api/game/call/99?name=a1.json")
    ok(code == 404, "B7 越界 404")

    code, body = get(server.url + "api/game?name=../a1.json")
    ok(code == 404, "B8 ⭐ 路径穿越被拒", code)

    code, body = get(server.url + "api/game/call/x?name=a1.json")
    ok(code == 400, "B9 非数字序号 400", code)
finally:
    server.stop()

server2 = TableServer(bridge, port=0).start()
try:
    code, body = get(server2.url + "api/games")
    ok(code == 200 and json.loads(body)["games"] == [],
       "B10 没配复盘目录时给空列表（不假装有）")
finally:
    server2.stop()

# ---- C. 页面 ----
ok('data-panel="review"' in PAGE_HTML, "C1 顶栏有「复盘」")
ok('id="rvList"' in PAGE_HTML, "C2 有列表容器")
ok("renderReview" in PAGE_HTML, "C3 打开面板会加载")
ok("剧透" in PAGE_HTML, "C4 ⭐ 面板里有剧透警告")
ok("async function poll(){" in PAGE_HTML, "C5 poll 仍是 async")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for x in FAILS:
        print(f"  ✗ {x}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")