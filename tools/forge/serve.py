# coding=utf-8
"""锻造台的独立服务：本地开一个页面，上传素材 → 出草稿 → 下载剧本。

    python -m tools.forge serve --port 8790

它**独立运行**：不依赖游戏服、不读写游戏的存档目录；只做文件转换。
游戏那边的「锻造台」按钮只是打开这个地址（默认 `http://127.0.0.1:8790`）。

只绑 127.0.0.1，没有鉴权 —— 本机工具，不该暴露到局域网；
上传的文件只在内存里过一遍，不落盘（产物由用户点「下载」自己存）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import zlib
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.forge.cards import (  # noqa: E402
    draft_from_card, load_card_bytes, merge_entries, suggest_clues,
    worldbook_entries)

from tools.forge.contract import doctor  # noqa: E402
from tools.forge.pipeline import convert as pipeline_convert  # noqa: E402
from tools.forge.from_markdown import parse_markdown  # noqa: E402
from tools.forge.from_novel import draft_from_text  # noqa: E402

MAX_BODY = 8 * 1024 * 1024

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>剧本锻造台</title>
<style>
 body{font:15px/1.6 system-ui;max-width:820px;margin:40px auto;padding:0 20px;color:#222}
 h1{font-size:20px} small{color:#666}
 fieldset{border:1px solid #ddd;border-radius:8px;padding:16px;margin:18px 0}
 pre{background:#f6f6f6;padding:12px;border-radius:6px;overflow:auto;max-height:340px}
 button{padding:8px 16px;font-size:15px;border-radius:6px;border:1px solid #888;background:#fff;cursor:pointer}
 .ok{color:#176} .bad{color:#a12} textarea{width:100%;height:130px}
</style>
<h1>剧本锻造台</h1>
<small>本机工具：上传你自己的素材，转成本项目能跑的剧本 JSON。文件只在内存里过一遍。</small>

<fieldset>
  <b>1. 酒馆角色卡（.json / .png）</b><br>
  <input type="file" id="card" accept=".json,.png">
  <button onclick="send('card')">转换</button>
</fieldset>

<fieldset>
  <b>2. 固定模板（.md）</b> <small>模板见 tools/forge/template.md</small><br>
  <input type="file" id="md" accept=".md,.txt">
  <button onclick="send('md')">转换</button>
</fieldset>

<fieldset>
  <b>3. 小说 / 自由文本（.txt / .md）</b> <small>只切场次，简报线索要你补</small><br>
  <input type="file" id="novel" accept=".txt,.md">
  <button onclick="send('novel')">转粗稿</button>
</fieldset>

<div id="out"></div>

<script>
const out = document.getElementById('out');
function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function send(kind){
  const input = document.getElementById(kind);
  const file = input.files && input.files[0];
  if(!file){ out.innerHTML = '<p class="bad">先选一个文件。</p>'; return; }
  try{
    const buf = new Uint8Array(await file.arrayBuffer());
    let bin = ''; const CH = 0x8000;
    for(let i=0;i<buf.length;i+=CH){ bin += String.fromCharCode.apply(null, buf.subarray(i,i+CH)); }
    out.innerHTML = '<p>转换中…</p>';
    const r = await fetch('/api/convert', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind, name:file.name, data: btoa(bin)})});
    let d;
    try{ d = await r.json(); }
    catch(e){ throw new Error('服务端回了非 JSON（' + r.status + '）'); }
    if(!d.ok){ out.innerHTML = '<p class="bad">'+esc(d.error||'失败')+'</p>'; return; }
    const rep = d.report || {};
    const cls = rep.ok ? 'ok' : 'bad';
    let html = '<p class="'+cls+'">'+(rep.ok?'可以跑':'还缺东西（粗稿）')+'</p>';
    html += '<p>'+esc(d.summary) +'</p>';
    html += '<pre>' + (rep.errors||[]).map(e=>'❌ '+esc(e)).join('\n') +
                      (rep.warnings||[]).map(e=>'⚠️ '+esc(e)).join('\n') + '</pre>';
    if((d.suggestions||[]).length){
      html += '<p><b>候选线索</b>（没写进剧本，确认后再加）</p><ul>' +
        d.suggestions.map(c => '<li>['+esc(c.skill)+' DC '+esc(c.suggested_dc)+'] '+
          esc(c.text)+' <small>（'+esc(c.why)+'｜'+esc(c.from)+'）</small></li>').join('') + '</ul>';
    }
    html += '<button onclick="download()">下载剧本 JSON</button>';
    out.innerHTML = html;
    window.__doc = d.document;
  }catch(e){
    out.innerHTML = '<p class="bad">转换失败：'+esc(e && e.message || e)+'</p>';
  }
}
function download(){
  const blob = new Blob([JSON.stringify(window.__doc,null,2)+'\\n'],
                        {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'adventure.json'; a.click();
}
</script></html>"""


def convert(kind: str, name: str, data: bytes) -> dict:
    """网页上传 → 草稿。**实现已统一到 `pipeline.convert`**（外审 D）。"""
    try:
        out = pipeline_convert(kind, name, data)
    except ValueError:
        raise
    return {"ok": True, "draft": out["draft"], "report": out["report"],
            "summary": out["summary"], "suggestions": out.get("suggestions") or []}


def _api_convert(payload: Any) -> dict:
    """POST /api/convert 的纯函数部分（便于测试：不碰 socket）。

    只接受**对象**；数组/字符串/None 都给可读错误（外审 B2）。
    """
    if not isinstance(payload, dict):
        raise TypeError(f"请求体必须是对象，拿到 {type(payload).__name__}")
    kind = str(payload.get("kind") or "")
    name = str(payload.get("name") or "")
    try:
        data = base64.b64decode(payload.get("data") or "", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"data 不是合法 base64：{exc}") from exc
    out = convert(kind, name, data)
    return {"ok": True, "report": out["report"],
            "document": out["draft"], "summary": out["summary"],
            "suggestions": out.get("suggestions") or []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        """访问日志只留非 200（外审 B9：旧实现两个分支都是空操作，等于全吞）。"""
        try:
            code = str(args[1]) if len(args) > 1 else ""
        except (IndexError, TypeError):
            code = ""
        if code and not code.startswith("2"):
            print(f"[forge] {code} {fmt % args}")

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                          # noqa: N802
        if urllib.parse.urlparse(self.path).path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"ok": False, "error": "没这个页面"}, 404)

    def do_POST(self):                         # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/api/convert":
            self._json({"ok": False, "error": "没这个接口"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._json({"ok": False, "error": "文件为空或太大（>8MB）"}, 400)
            return
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._json({"ok": False, "error": f"请求不是合法 JSON：{exc}"}, 400)
            return
        try:
            out = _api_convert(payload)
        except (ValueError, TypeError, KeyError, AttributeError,
                zlib.error, MemoryError, OSError) as exc:
            # 外审 B2：payload 是数组（AttributeError）、坏压缩块（zlib.error）、
            # 解压炸弹（MemoryError）都不能让连接被掐——统一回一个可读错误。
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400)
            return
        self._json(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="剧本锻造台（本地独立服务）")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"锻造台： http://127.0.0.1:{args.port}  （Ctrl+C 退出）")
    print("游戏里的「锻造台」按钮默认也开这个地址。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())