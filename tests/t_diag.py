# coding=utf-8
"""自检（/api/diag + 页面面板）：只读事实、不泄漏密钥。"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.webui import PAGE_HTML, TableServer, WebBridge  # noqa: E402
from engine.demo_web import save_dir_writable, _key_present  # noqa: E402

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


# ---- 页面里有入口 ----
ok('data-panel="diag"' in PAGE_HTML, "A1 顶栏有「自检」按钮")
ok('id="diagRows"' in PAGE_HTML, "A2 有自检面板")
ok("/api/diag" in PAGE_HTML, "A3 面板会请求 /api/diag")
ok("key_present" in PAGE_HTML, "A4 面板显示 key 是否配置（不显示内容）")
ok("function esc(" in PAGE_HTML, "A5 页面有转义 helper（自检拼 HTML 要用）")

# ---- 服务端：正常 / 未配置 / 报错 ----
facts = {"version": "存档 v4", "save_dir": "saves/mock",
         "save_dir_writable": True, "real": False, "key_present": True,
         "model": "", "calls": 3}
bridge = WebBridge(print_log=False)
server = TableServer(bridge, port=0, diag=lambda: facts).start()
try:
    code, body = get(server.url + "api/diag")
    data = json.loads(body)
    ok(code == 200 and data["ok"] is True, "B1 /api/diag 返回 200 + ok",
       (code, body[:80]))
    ok(data.get("save_dir_writable") is True, "B2 带存档目录可写事实")
    ok("key_present" in data and "api_key" not in data,
       "B3 ⭐ 只给 key 是否存在，不给内容", sorted(data))
finally:
    server.stop()

server2 = TableServer(bridge, port=0).start()
try:
    code, body = get(server2.url + "api/diag")
    ok(code == 404 and "没配自检" in body, "B4 没配自检时 404（不假装成功）")
finally:
    server2.stop()


def boom():
    raise RuntimeError("模拟自检崩了")


server3 = TableServer(bridge, port=0, diag=boom).start()
try:
    code, body = get(server3.url + "api/diag")
    ok(code == 500 and "模拟自检崩了" in body, "B5 自检报错时 500 + 原因")
finally:
    server3.stop()

# ---- 可写性检测 ----
import tempfile  # noqa: E402

td = tempfile.mkdtemp(prefix="diag-")
ok(save_dir_writable(td) is True, "C1 临时目录可写")
ok(save_dir_writable(os.path.join(td, "sub", "dir")) is True, "C2 会顺手建目录")
f = os.path.join(td, "a-file")
open(f, "w").close()
ok(save_dir_writable(f) is False, "C3 ⭐ 路径是文件时判不可写（不是只看存在）")

# ---- key 检测不泄漏 ----
old = os.environ.get("QIANFAN_API_KEY")
os.environ["QIANFAN_API_KEY"] = "sk-placeholder-not-a-real-key"
try:
    present = _key_present()
finally:
    if old is None:
        os.environ.pop("QIANFAN_API_KEY", None)
    else:
        os.environ["QIANFAN_API_KEY"] = old
ok(present is True, "D1 设了环境变量时 key_present 为真")
ok("sk-placeholder-not-a-real-key" not in json.dumps(facts, ensure_ascii=False),
   "D2 ⭐ 自检数据里没有密钥内容")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for x in FAILS:
        print(f"  ✗ {x}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")