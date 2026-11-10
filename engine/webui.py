# coding=utf-8
"""P5-C：真人前端（本地网页版座位）。

把 `HumanSeat` 的"终端一行输入"换成"浏览器点按钮"，**不花钱、不联网**：

    浏览器  ←HTTP轮询→  TableServer  ←→  WebBridge  ←→  WebHumanSeat（座位）
                                                        ↑
                                                  GameLoop（后台线程）

三条边界（刻意）：
1. **只绑 127.0.0.1**：没有鉴权，就不该暴露到局域网/公网；
2. **只做座位，不改引擎**：`WebHumanSeat` 继承 `HumanSeat`，输入校验、三次失败兜底
   全部复用现有逻辑 —— 网页只是换了个"怎么把菜单给人看、怎么收人的输入"；
3. **零依赖**：`http.server` + 内联 HTML/JS，不引前端框架、不引静态资源。

轮询而不是 WebSocket：少一个依赖、少一类断线重连的坑；本地轮询 0.8 秒，
对"人想好了再点"的场景完全够。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .models.client import ModelError, ModelRequest
from .models.seats import HumanSeat

__all__ = [
    "Pending", "WebBridge", "WebHumanSeat", "TableServer", "serve", "PAGE_HTML",
    "viewer_log_filter",
]

#: 和 HumanSeat 用同一个判据（战斗菜单的选项带 slot / requires_target）
_COMBAT_KEYS = ("slot", "requires_target")

#: 单条答案的最大长度（防误贴整篇文章）
MAX_SAY = 500
MAX_BODY = 16 * 1024


def viewer_log_filter(viewer):
    """只放行"公共行 + 本人私有行"。

    `viewer` 可以是字符串，也可以是"返回当前真人名"的 callable ——
    建卡/读档会把真人从布兰换成别的角色，过滤不能还盯着旧名字。

    循环的私有行格式是 `【名字·私有】…`；别人的私有发现不该出现在这个座位的
    页面/终端里（`lint_leaks` 与 `verify_isolation` 都只查提示词，查不到这条通路）。
    """
    def name() -> str:
        return str(viewer() if callable(viewer) else viewer)

    def keep(line: str) -> bool:
        if "·私有】" not in line:
            return True
        return line.lstrip().startswith(f"【{name()}·私有】")

    return keep


@dataclass
class Pending:
    """一次等真人回答的问题。"""
    seq: int
    actor: str
    task: str
    options: list[dict]
    created: float = field(default_factory=time.time)
    combat: bool = False


class WebBridge:
    """座位 ↔ HTTP 的桥：**阻塞等一个答案**，带超时与幂等序号。"""

    def __init__(self, *, timeout: float = 900.0,
                 log_filter=None, snapshot_extra=None, action_handler=None,
                 print_log: bool = False):
        self.timeout = float(timeout)
        #: 是否把日志同时打到终端。网页/测试默认关：daemon 线程在解释器退出后
        #: 写 stdout 会触发 "could not acquire lock" 的致命错误。
        self.print_log = bool(print_log)
        #: 只给这个座位看的日志过滤（默认全收）。网页真人座位用它挡掉别人的私有行。
        self.log_filter = log_filter
        #: 额外状态（战斗地图等）。每轮轮询调一次，返回 dict 合进 snapshot。
        self.snapshot_extra = snapshot_extra
        #: 网页动作（装备/准备法术）。收到 POST /api/act 时调，返回 dict。
        self.action_handler = action_handler
        self._act_lock = threading.Lock()
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._pending: Pending | None = None
        self._answer: dict | None = None
        self._seq = 0
        self._log: list[dict] = []
        self._log_seq = 0
        #: 重开/读档时置真：正在等答案的 ask 立刻返回空串，让旧线程退出。
        self._aborted = False
        self.done = False
        self.error = ""
        #: 当前正在思考的座位（"XX 在想…"用）。只做展示，不参与判定。
        self._think: dict | None = None
        #: normal / hosted / spectate（由 WebSession 每次开局设置；前端只读）。
        self.mode = "normal"
        #: 会话代次。换局时 +1；旧代次的座位 ask 直接拒绝，不往新局投问题。
        self._generation = 0

    # ---------- 给引擎侧 ----------

    def think(self, actor: str, task: str, phase: str) -> None:
        """模型调用开始/结束：更新"谁在想"。`phase` = start / end。"""
        with self._lock:
            if phase == "start":
                self._think = {"seat": str(actor or ""),
                               "task": str(task or ""), "t0": time.time()}
            else:
                self._think = None

    def log(self, line: str) -> None:
        """座位/循环的每一行人话都进网页日志（也留给终端）。"""
        text = str(line)
        if self.log_filter is not None and not self.log_filter(text):
            return
        with self._lock:
            self._log_seq += 1
            self._log.append({"seq": self._log_seq, "text": text})
            if len(self._log) > 400:
                self._log = self._log[-400:]
        if self.print_log:
            print(text, flush=True)

    def generation(self) -> int:
        with self._lock:
            return int(self._generation)

    def set_generation(self, gen: int) -> None:
        """换局：旧代次的 ask 立刻失效（不靠 join 超时兜底）。"""
        with self._lock:
            self._generation = int(gen)

    def ask(self, req: ModelRequest, *, generation: int | None = None) -> str:
        """发布问题并**阻塞**等人回答；超时返回空串（交给座位按"空输入"处理）。"""
        if generation is not None and int(generation) != self.generation():
            raise ModelError("这一局已经被新的一局替换（旧座位不再作答）")
        opts = list((req.allowed or {}).get("options") or [])
        combat = any(k in o for k in _COMBAT_KEYS for o in opts)
        with self._lock:
            self._seq += 1
            self._pending = Pending(seq=self._seq, actor=req.actor or "你",
                                    task=req.task, options=opts, combat=combat)
            self._answer = None
            self._event.clear()
            seq = self._seq
        got = self._event.wait(self.timeout)
        with self._lock:
            ans = dict(self._answer or {})
            self._pending = None
            aborted = self._aborted
            if aborted:
                raise ModelError("这一局已中止（重开/读档/结束）")
            if not got or not ans:
                return ""
            oid = str(ans.get("id") or "")
            target = str(ans.get("target") or "")
            say = str(ans.get("say") or "")
            if combat:
                return f"{oid} {target} {say}".strip()
            return f"{oid} {say}".strip()

    def finish(self, *, error: str = "") -> None:
        with self._lock:
            self.done = True
            self.error = str(error)
            self._event.set()          # 解除可能还在等的座位

    def abort(self) -> None:
        """重开/读档：让正在等的 `ask` 立刻返回，旧线程自行退出。"""
        with self._lock:
            self._aborted = True
            self._pending = None
            self._answer = None
            self._event.set()

    def reset(self, *, clear_log: bool = False) -> None:
        """开始新一局：清掉结束标记与旧问题。

        `clear_log=True` 只清**显示列表**，序号继续递增（前端的 `since` 不会错位）。
        """
        with self._lock:
            self.done = False
            self.error = ""
            self._pending = None
            self._answer = None
            self._aborted = False
            self._event.clear()
            if clear_log:
                self._log = []

    # ---------- 给 HTTP 侧 ----------

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            p = self._pending
            snap = {
                "done": self.done,
                "error": self.error,
                "pending": None if p is None else {
                    "seq": p.seq, "actor": p.actor, "task": p.task,
                    "combat": p.combat,
                    "options": [_public_option(o) for o in p.options],
                },
                "log": [x for x in self._log if x["seq"] > int(since)],
                "thinking": dict(self._think) if self._think else None,
                #: normal / hosted / spectate —— 前端据此隐藏输入框并显示横幅。
                "mode": getattr(self, "mode", "normal"),
            }
        if self.snapshot_extra is not None:
            try:
                extra = self.snapshot_extra()
                if isinstance(extra, dict):
                    snap.update(extra)
                else:
                    snap["extra"] = extra
            except Exception as exc:                  # noqa: BLE001
                snap["map"] = {"battle": False, "error": str(exc)}
        return snap

    def submit(self, *, seq: int, oid: str, target: str = "", say: str = "") -> tuple[bool, str]:
        """浏览器交答案。**只做浅校验**：选项必须在菜单里；其余交给座位逐条校验。"""
        oid = str(oid or "").strip()[:40]
        target = str(target or "").strip()[:60]
        say = str(say or "").strip()[:MAX_SAY]
        with self._lock:
            p = self._pending
            if p is None or int(seq) != p.seq:
                return False, "这个问题已经过期（页面自动刷新一下）"
            by_id = {str(o.get("id")): o for o in p.options}
            if oid not in by_id:
                return False, f"没有这个选项：{oid!r}"
            opt = by_id[oid]
            if opt.get("requires_target") and not target:
                return False, "这个选项需要先选一个目标"
            self._answer = {"id": oid, "target": target, "say": say}
            self._event.set()
        return True, "已收到，引擎开始结算"

    def act(self, payload: dict) -> dict:
        """网页动作（背包/法术）。没接 handler 就明确拒绝，不假装成功。"""
        if self.action_handler is None:
            return {"ok": False, "error": "这个页面没有接动作接口"}
        with self._act_lock:
            try:
                out = self.action_handler(payload)
            except Exception as exc:                  # noqa: BLE001
                return {"ok": False, "error": str(exc) or type(exc).__name__}
        if not isinstance(out, dict):
            return {"ok": False, "error": "动作返回格式不对"}
        return out


def _public_option(o: dict) -> dict:
    """只把网页需要的字段发出去（别把整个菜单对象倒给前端）。"""
    return {
        "id": str(o.get("id") or ""),
        "kind": str(o.get("kind") or ""),
        "name": str(o.get("name") or o.get("kind") or ""),
        "summary": str(o.get("summary") or ""),
        "note": str(o.get("note") or ""),
        "requires_target": bool(o.get("requires_target")),
        "targets": [str(x) for x in (o.get("targets") or [])],
    }


class WebHumanSeat(HumanSeat):
    """把 `HumanSeat` 的 `input()` 换成"等网页回答"。**其余逻辑一行不改。**

    `auto` 非空时 = **托管**：不等人，直接让回调给答案（"接管"就是把 auto 设回 None）。
    """

    def __init__(self, bridge: WebBridge, auto=None, **kw):
        # 菜单由页面画；日志只留"输入有问题"这类提示，免得整张菜单淹掉正文。
        def out(line: str) -> None:
            text = str(line)
            if text.startswith("  ⚠️"):
                bridge.log(text)

        super().__init__(auto=auto, out=out, **kw)
        self.bridge = bridge
        #: 由 WebSession._spawn 写入；旧代次的座位不能往新局投 pending。
        self.generation: int | None = None

    def _answer(self, req: ModelRequest, prompt: str) -> str:
        if self._auto is not None:
            return super()._answer(req, prompt)
        return self.bridge.ask(req, generation=self.generation)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    bridge: WebBridge = None                     # 由 TableServer 注入
    #: 自检数据（零风险：由 demo_web 注入，只读本机事实，不含密钥）
    diag = None
    #: 复盘要读的存档目录（callable → str；由 demo_web 注入）
    review_dir = None
    #: 单连接超时：防止"声明 Content-Length 却不发 body"把线程永久挂住
    timeout = 10

    def log_message(self, fmt, *args):           # 静音：不要每个请求都刷屏
        pass

    def _same_origin(self) -> bool:
        """Host / Origin 必须是本机回环 —— 挡 DNS rebinding 与跨站提交。"""
        port = str(self.server.server_address[1])

        def ok_netloc(netloc: str) -> bool:
            name, _, p = str(netloc or "").partition(":")
            if name not in ("127.0.0.1", "localhost"):
                return False
            return (not p) or p == port

        if not ok_netloc(self.headers.get("Host", "")):
            return False
        origin = self.headers.get("Origin")
        if origin:
            return ok_netloc(urllib.parse.urlparse(origin).netloc)
        return True

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _games(self) -> list:
        from . import review as _rv
        if self.review_dir is None:
            return []
        try:
            return _rv.list_games(str(self.review_dir()))
        except Exception:                            # noqa: BLE001
            return []

    def _review(self, parsed) -> None:
        """两路口径：`?lite=1` 列表；`/api/game/call/<i>` 单条全文。"""
        from . import review as _rv
        if self.review_dir is None:
            self._json({"ok": False, "error": "这个服务没配复盘"}, code=404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        name = (qs.get("name") or [""])[0]
        path = _rv.resolve(str(self.review_dir()), name)
        if not path:
            self._json({"ok": False, "error": "没这份存档"}, code=404)
            return
        rows = _rv.calls_for(path)
        m = urllib.parse.urlparse(parsed.path).path
        if "/call/" in m:
            tail = m.rsplit("/call/", 1)[1]
            try:
                idx = int(tail)
            except ValueError:
                self._json({"ok": False, "error": "第几条不是数字"}, code=400)
                return
            row = _rv.full_row(rows, idx)
            if row is None:
                self._json({"ok": False, "error": "没有这一条"}, code=404)
                return
            self._json({"ok": True, "call": row, "name": name,
                        "spoiler": True})
            return
        lite = (qs.get("lite") or [""])[0] in ("1", "true", "yes", "on")
        self._json({"ok": True, "name": name,
                    "lite": lite,
                    "calls": _rv.lite_rows(rows) if lite else rows,
                    "spoiler": not lite})

    def _drain_body(self) -> None:
        """把请求体读掉再关连接。

        ⚠️ 2026-11-06 实测的偶发红：拒绝跨站 POST 时直接回 403 并断连接，
        客户端还在写 body → Windows 抛 `ConnectionAbortedError 10053`。
        先读完（有上限）再回，连接才是干净关闭的。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= MAX_BODY:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _deny_origin(self) -> bool:
        if self._same_origin():
            return False
        self._drain_body()
        self._json({"ok": False, "error": "只接受本机回环来源"}, code=403)
        return True

    def do_GET(self):                            # noqa: N802
        if self._deny_origin():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/diag":
            if self.diag is None:
                self._json({"ok": False, "error": "这个服务没配自检"}, code=404)
                return
            try:
                data = self.diag() or {}
            except Exception as exc:              # noqa: BLE001
                self._json({"ok": False,
                            "error": f"{type(exc).__name__}: {exc}"}, code=500)
                return
            self._json({"ok": True, **data})
            return
        if parsed.path == "/api/games":
            self._json({"ok": True, "games": self._games()})
            return
        if parsed.path == "/api/game" or parsed.path.startswith("/api/game/"):
            self._review(parsed)
            return
        if parsed.path == "/api/state":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                since = int((qs.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            self._json(self.bridge.snapshot(since))
            return
        self._json({"ok": False, "error": "没这个页面"}, code=404)

    def do_POST(self):                           # noqa: N802
        if self._deny_origin():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/api/answer", "/api/act"):
            self._drain_body()
            self._json({"ok": False, "error": "没这个接口"}, code=404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._json({"ok": False, "error": "请求体为空或过大"}, code=400)
            return
        try:
            raw = self.rfile.read(n)
        except OSError:
            self._json({"ok": False, "error": "读取请求体失败"}, code=400)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("必须是对象")
            if parsed.path == "/api/act":
                out = self.bridge.act(payload)
                self._json(out, code=200 if out.get("ok") else 400)
                return
            ok, msg = self.bridge.submit(
                seq=int(payload.get("seq") or 0),
                oid=str(payload.get("id") or ""),
                target=str(payload.get("target") or ""),
                say=str(payload.get("say") or ""))
        except (ValueError, TypeError) as exc:
            self._json({"ok": False, "error": f"格式不对：{exc}"}, code=400)
            return
        self._json({"ok": ok, "message": msg}, code=200 if ok else 409)


class TableServer:
    """本地网页服务。**只绑 127.0.0.1**。"""

    def __init__(self, bridge: WebBridge, *, port: int = 8770, diag=None,
                 review_dir=None):
        self.bridge = bridge
        # diag / review_dir 用 staticmethod 包一下：否则会被当成方法、把 self 传进去
        self._handler = type("_H", (_Handler,),
                             {"bridge": bridge,
                              "diag": (staticmethod(diag) if diag else None),
                              "review_dir": (staticmethod(review_dir)
                                             if review_dir else None)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), self._handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> "TableServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def serve(*, port: int = 8770, run: Callable[[WebBridge], None],
          timeout: float = 900.0) -> int:
    """起服务 → 后台线程跑 `run(bridge)` → 结束后停服务。返回退出码。"""
    bridge = WebBridge(timeout=timeout, print_log=True)
    server = TableServer(bridge, port=port).start()
    print("=" * 70)
    print(f"真人前端：{server.url}（只绑 127.0.0.1；Ctrl+C 退出）")
    print("=" * 70)
    error = ""

    def worker():
        nonlocal error
        try:
            run(bridge)
        except ModelError as exc:
            error = f"引擎侧错误：{exc}"
            bridge.log(f"⚠️ {error}")
        except Exception as exc:                 # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            bridge.log(f"⚠️ {error}")
        finally:
            bridge.finish(error=error)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        while not bridge.done:
            time.sleep(0.2)
    except KeyboardInterrupt:
        bridge.finish(error="你中断了这一局")
    finally:
        t.join(timeout=5)
        server.stop()
    return 1 if (error or bridge.error) else 0


# --------------------------------------------------------------------------- #
# 页面（内联，零外部资源）
# --------------------------------------------------------------------------- #

PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>D&amp;D 牌桌 · 你的座位</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.55 system-ui,"Segoe UI",sans-serif;
  background:#14161a;color:#e8e8ea;display:flex;flex-direction:column;height:100vh}
header{padding:8px 14px;border-bottom:1px solid #2a2d34;display:flex;
  align-items:center;gap:14px;flex-wrap:wrap}
header .brand{color:#8ab4ff;font-weight:700;letter-spacing:.04em}
#hud{display:flex;align-items:center;gap:10px;flex:1;min-width:220px;flex-wrap:wrap}
#hud .nm{font-weight:600}
.hpbar{position:relative;width:150px;height:14px;background:#2a2d34;border-radius:7px;overflow:hidden}
.hpbar i{display:block;height:100%;background:linear-gradient(90deg,#2f9e6e,#4ade80);width:0}
.hpbar span{position:absolute;inset:0;text-align:center;font-size:11px;line-height:14px;color:#fff;text-shadow:0 0 2px #000}
.chip{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;
  background:#3a2a55;border:1px solid #6b46c1;color:#e9d8fd;margin-right:4px}
.chip.bloodied{background:#4a1f22;border-color:#ff7b72;color:#ffb3ad}
#status{color:#8b93a3;font-size:12px;text-align:right;min-width:90px}
#gameMode{background:#12151b;color:#cfd3dc;border:1px solid #333a46;border-radius:7px;
  padding:4px 6px;font-size:12px}
#modebanner{display:flex;align-items:center;gap:8px;padding:7px 10px;margin:0 0 6px;
  border:1px solid #4a3b22;background:#1d180f;border-radius:8px;color:#e8b86d;font-size:13px}
#modebanner[hidden]{display:none}
#takeOver{margin-left:auto}
.toolbar{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}
.tool{background:#1e222a;border:1px solid #333a46;color:#cfd3dc;border-radius:7px;
  padding:5px 10px;cursor:pointer;font:inherit;font-size:13px}
.tool:hover{border-color:#8ab4ff}
main{flex:1;min-height:0;display:flex;min-width:0}
#log{padding:12px 16px;overflow:auto;white-space:pre-wrap;font-size:14px;
  color:#cfd3dc;flex:1;min-width:0;overflow-wrap:anywhere}
#dock{flex:0 0 auto;border-top:1px solid #2a2d34;padding:8px 12px 10px;min-width:0;
  display:flex;flex-direction:column;gap:6px;background:#14161a}
#turnline{font-size:13px;color:#8b93a3;min-height:18px}
#thinkline{font-size:12px;color:#6f7a8c;min-height:16px}
#rvList .rvrow{display:block;width:100%;text-align:left;margin:4px 0;padding:6px 8px;
  border:1px solid #2a3445;border-radius:6px;background:#151b26;color:#c9d1e0;cursor:pointer}
#rvList .rvrow:hover{border-color:#3d4f6b}
#rvList .rvrow b{color:#9bb3d8}
#rvList .rvrow .meta{color:#7c8798;font-size:12px;margin-left:6px}
#rvDetail pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto;
  background:#0f141d;border:1px solid #2a3445;border-radius:6px;padding:8px;color:#d7dee9;
  font-size:12px;line-height:1.5}
#thinkline b{color:#9bb3d8;font-weight:600}
#turnline b{color:#8ab4ff}
#cats{display:flex;gap:4px;flex-wrap:wrap}
#cats button{background:#1e222a;border:1px solid #333a46;color:#8b93a3;
  border-radius:6px;padding:3px 9px;cursor:pointer;font-size:12px}
#cats button.on{color:#e8e8ea;border-color:#8ab4ff}
#slots{display:flex;gap:6px;overflow-x:auto;padding:2px 0;min-height:64px;min-width:0;max-width:100%}
#initiative{display:none;gap:6px;overflow-x:auto;padding:2px 0 8px;min-width:0;max-width:100%}
.init-card{flex:0 0 auto;width:132px;background:#1e222a;border:1px solid #333a46;
  border-left:3px solid #8b93a3;border-radius:8px;padding:5px 7px;font-size:11px;
  color:#cfd3dc}
.init-card.pc{border-left-color:#6ea8ff}
.init-card.ally{border-left-color:#6ee7a8}
.init-card.enemy{border-left-color:#ff7b72}
.init-card.active{background:#20293a;box-shadow:0 0 0 1px #ffd166 inset}
.init-card.down{opacity:.45}
.init-card .n{display:block;font-weight:700;font-size:12px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.init-card .meta{display:block;color:#8b93a3;font-size:10px;line-height:1.35;
  margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.quest-head{padding:2px 0 8px;border-bottom:1px solid #2a2d34;margin-bottom:8px}
.quest-head .qtitle{font-size:15px;font-weight:700;color:#ffd166}
.quest-head .qmeta{font-size:12px;color:#8b93a3;margin-top:2px}
.quest-card{background:#1e222a;border:1px solid #333a46;border-left:3px solid #6ea8ff;
  border-radius:8px;padding:8px 10px;margin:8px 0;font-size:13px;line-height:1.55}
.quest-card .qtitle{font-size:14px;font-weight:700}
.qbadge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:999px;
  margin-left:6px;vertical-align:2px;background:#2b3550;color:#9ec1ff;
  border:1px solid #3b5b96}
.qbadge.done{background:#173a2b;color:#8ff0bc;border-color:#2f6b4f}
.receipt{background:#161b22;border:1px solid #2a2d34;border-left:3px solid #6ee7a8;
  border-radius:8px;padding:7px 9px;margin:7px 0;font-size:12px;line-height:1.5}
.receipt .qtitle{font-size:13px;font-weight:700}
.qline{color:#cfd3dc;margin-top:3px;overflow-wrap:anywhere}
.qline b{color:#8b93a3;font-weight:500}
.slot{flex:0 0 auto;width:112px;text-align:left;background:#1e222a;
  border:1px solid #333a46;border-radius:8px;color:#e8e8ea;cursor:pointer;
  padding:5px 7px;position:relative;font:inherit}
.slot:hover{border-color:#8ab4ff}
.slot.sel{border-color:#8ab4ff;box-shadow:0 0 0 1px #8ab4ff inset;background:#20293a}
.slot .k{position:absolute;right:5px;top:4px;font-size:10px;color:#8b93a3}
.slot .n{font-size:13px;font-weight:600;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.slot small{display:block;color:#8b93a3;font-size:10px;line-height:1.3;margin-top:2px;
  height:26px;overflow:hidden}
.slot .warn{color:#ffb86b}
#composer{display:flex;gap:8px;align-items:stretch;flex-wrap:wrap}
#say{flex:1;min-width:0;min-height:40px;max-height:80px;background:#1e222a;color:#e8e8ea;
  border:1px solid #333a46;border-radius:8px;padding:8px;resize:vertical;font:inherit}
#submit{flex:0 0 auto;padding:0 18px;border-radius:8px;border:0;
  background:#3b6ef6;color:#fff;cursor:pointer;font-weight:600}
#submit:disabled{background:#2b3550;color:#9aa4bb;cursor:default}
#allActions{flex:0 0 auto;padding:0 12px;border-radius:8px;border:1px solid #333a46;
  background:#1e222a;color:#cfd3dc;cursor:pointer}
#targetbox select{background:#1e222a;color:#e8e8ea;border:1px solid #333a46;
  border-radius:7px;padding:5px 8px}
#error{color:#ff7b72;font-size:13px;min-height:16px;white-space:pre-wrap}
.overlay{position:fixed;inset:0;background:rgba(6,8,12,.72);display:none;
  align-items:center;justify-content:center;z-index:50;padding:20px}
.overlay.open{display:flex}
.modal{background:#181b21;border:1px solid #333a46;border-radius:12px;
  width:min(880px,96vw);max-height:88vh;display:flex;flex-direction:column;
  box-shadow:0 18px 50px rgba(0,0,0,.5)}
.modal > header{padding:10px 14px;border-bottom:1px solid #2a2d34;
  display:flex;justify-content:space-between;align-items:center}
.modal > header b{color:#8ab4ff;font-size:15px}
.x{background:none;border:0;color:#8b93a3;font-size:20px;cursor:pointer}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
@media (prefers-reduced-motion: reduce){
  *{animation-duration:.01ms !important;animation-iteration-count:1 !important;
    transition-duration:.01ms !important;scroll-behavior:auto !important}
}
.modal .body{padding:12px 14px;overflow:auto}
.session0 .modal{width:min(760px,96vw)}
.s0-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}
.s0-card{background:#1e222a;border:1px solid #333a46;border-radius:9px;
  padding:8px 10px;font-size:13px;line-height:1.55}
.s0-card b{display:block;color:#8ab4ff;font-size:13px;margin-bottom:3px}
.s0-pitch{white-space:pre-line;background:#10151d;border:1px solid #2a3546;
  border-radius:9px;padding:10px 12px;font-size:13px;line-height:1.65;margin:8px 0}
.s0-party{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}
.s0-stage{background:#10151d;border:1px solid #2a3546;border-radius:9px;
  padding:9px 11px;margin:8px 0;font-size:13px;line-height:1.6}
.s0-stage b{color:#d8b46a}
.s0-stage button{margin-top:7px;padding:6px 9px;border:1px solid #3a4558;border-radius:7px;
  background:#1e222a;color:#e9e4d7;font:inherit;font-size:13px;cursor:pointer}
.s0-stage select{max-width:100%;padding:6px 8px;border-radius:7px;border:1px solid #333a46;
  background:#0d1014;color:#e9e4d7;font:inherit;font-size:13px}
.s0-stage .row2{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap}
.s0-intros{background:#0d1014;border:1px solid #2a2d34;border-radius:9px;
  padding:8px 10px;margin:8px 0;font-size:13px;line-height:1.6}
.s0-intros .who{color:#ffd166}
#s0Intros{margin-top:6px;padding:6px 9px;border:1px solid #3a4558;border-radius:7px;
  background:#1e222a;color:#e9e4d7;font:inherit;font-size:13px;cursor:pointer}
#s0Intros:disabled{color:#758094;cursor:default}
.s0-guided{background:#10151d;border:1px solid #2a3546;border-radius:9px;
  padding:8px 10px;margin:8px 0}
.s0-gq{font-size:13px;line-height:1.6;color:#e9e4d7;margin-bottom:6px}
.s0-audit{background:#111820;border:1px solid #3a4558;border-left:3px solid #d8b46a;
  border-radius:7px;padding:8px 10px;font-size:13px;line-height:1.6;margin:8px 0;color:#e9e4d7}
.s0-qa{max-height:150px;overflow:auto;background:#0d1014;border:1px solid #2a2d34;
  border-radius:9px;padding:8px 10px;font-size:13px;line-height:1.6;margin:8px 0}
.s0-qa .q{color:#9aa4bb}
.s0-qa .a{color:#e9e4d7;margin:0 0 6px 0}
.s0-qrow{display:flex;gap:6px;align-items:center;margin:6px 0}
.s0-qrow input{flex:1;min-width:0;padding:7px 9px;border-radius:7px;border:1px solid #333a46;
  background:#0d1014;color:#e9e4d7;font:inherit;font-size:13px}
.s0-qrow button{padding:7px 10px;border:1px solid #3a4558;border-radius:7px;
  background:#1e222a;color:#e9e4d7;font:inherit;font-size:13px;cursor:pointer}
.s0-qrow button:disabled{color:#758094;cursor:default}
.s0-hint{color:#9aa4bb;font-size:12px;margin:2px 0 8px}
.s0-pc{background:#1e222a;border:1px solid #333a46;border-radius:9px;
  padding:8px 10px;font-size:13px;line-height:1.55}
.s0-pc b{display:block;color:#ffd166;font-size:14px;margin-bottom:2px}
.s0-pc .muted{color:#9aa4bb;font-size:12px}
.s0-review{background:#0d1014;border:1px solid #2a2d34;border-radius:9px;
  padding:9px 11px;font-size:13px;line-height:1.6}
.s0-review b{color:#ffd166}
.s0-ack{display:flex;gap:8px;align-items:flex-start;margin:10px 0;
  font-size:13px;line-height:1.5}
.s0-ack input{margin-top:3px}
#s0Start{width:100%;padding:9px;border:0;border-radius:8px;background:#3b6ef6;
  color:#fff;font-weight:600;cursor:pointer;font:inherit}
#s0Start:disabled{background:#2b3550;color:#9aa4bb;cursor:default}
@media (max-width:520px){.s0-grid,.s0-party{grid-template-columns:1fr}}
.tabs{display:flex;gap:4px;padding:8px 14px 0;border-bottom:1px solid #2a2d34}
.tabs button{background:none;border:0;color:#8b93a3;padding:6px 10px;cursor:pointer;
  border-bottom:2px solid transparent;font:inherit;font-size:13px}
.tabs button.on{color:#e8e8ea;border-bottom-color:#8ab4ff}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px}
.card{background:#1e222a;border:1px solid #333a46;border-radius:9px;padding:8px 10px}
.card h5{margin:0 0 4px;color:#8b93a3;font-size:11px;font-weight:600;letter-spacing:.06em}
.card .v{font-size:17px;font-weight:700}
.card .s{color:#8b93a3;font-size:12px}
.row{display:flex;justify-content:space-between;gap:10px;padding:4px 0;
  border-bottom:1px dashed #232831;font-size:13px}
.row:last-child{border-bottom:0}
.row .l{color:#8b93a3}
.abil{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:8px}
.abil div{background:#1e222a;border:1px solid #333a46;border-radius:8px;
  text-align:center;padding:6px}
.abil .a{font-size:11px;color:#8b93a3}
.abil .n{font-size:17px;font-weight:700}
.abil .m{font-size:11px;color:#8ab4ff}
.pips{display:flex;gap:3px;flex-wrap:wrap}
.pip{width:14px;height:14px;border-radius:3px;background:#2a2d34;border:1px solid #3a4150}
.pip.on{background:#8ab4ff;border-color:#8ab4ff}
.item{display:flex;justify-content:space-between;gap:10px;padding:7px 9px;
  background:#1e222a;border:1px solid #333a46;border-radius:8px;margin:5px 0}
.item .eq{font-size:10px;color:#0d1014;background:#6ee7a8;border-radius:4px;
  padding:0 5px;margin-left:6px}
.item small{color:#8b93a3;display:block}
.item .mini, .spellrow .mini, .row .mini{background:#2b3550;border:1px solid #3a4150;
  color:#cfd3dc;border-radius:6px;padding:2px 8px;cursor:pointer;font-size:11px}
.item .mini:disabled, .spellrow .mini:disabled{opacity:.5;cursor:default}
.spellrow{display:flex;justify-content:space-between;gap:10px;padding:7px 9px;
  background:#1e222a;border:1px solid #333a46;border-radius:8px;margin:5px 0;
  align-items:center}
.loot-card{display:block;background:#1e222a;border:1px solid #333a46;
  border-left:3px solid #8ab4ff;border-radius:9px;padding:9px 11px;margin:7px 0;
  font-size:13px;line-height:1.55}
.loot-card.equipped{border-left-color:#6ee7a8}
.loot-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.loot-head b{font-size:14px}
.loot-kind{flex:0 0 auto;font-size:10px;color:#9ec1ff;background:#16233a;
  border:1px solid #2f4d78;border-radius:999px;padding:1px 7px}
.loot-meta{color:#8b93a3;font-size:11px;margin-top:3px}
.loot-desc{color:#cfd3dc;margin-top:5px;overflow-wrap:anywhere}
.loot-desc b{color:#8b93a3;font-weight:500}
.loot-src{color:#8b93a3;font-size:11px;margin-top:2px}
.loot-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
.loot-rule{margin-top:6px;color:#9aa4bb;font-size:11px}
.loot-rule summary{cursor:pointer;color:#8b93a3}
#lore-text,#lore-mech,#lore-bg{width:100%;min-height:90px;background:#151a22;color:#e6e9ef;
  border:1px solid #2a2d34;border-radius:7px;padding:8px;font:inherit;resize:vertical}
#lore-mech{min-height:80px}
.dmform input,.dmform select,.dmform textarea{width:100%;background:#151a22;
  color:#e6e9ef;border:1px solid #2a2d34;border-radius:6px;padding:6px;font:inherit;
  margin:2px 0 6px}
.dmform textarea{resize:vertical}
.dmform .row{display:flex;gap:8px;align-items:center}
.dmform .row .l{flex:1 1 45%;font-size:12px;color:#9aa4bb}
.dmform .row input,.dmform .row select{margin:2px 0}
.skill-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:6px 0}
.skill-card{background:#1e222a;border:1px solid #333a46;border-radius:8px;
  padding:7px 9px;font-size:13px;line-height:1.5}
.skill-card .loot-desc{margin-top:3px;font-size:12px}
.equip-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:6px 0}
.equip-slot{background:#161b22;border:1px solid #2a2d34;border-radius:8px;
  padding:7px 9px;min-height:48px;font-size:12px}
.equip-slot .sn{color:#8b93a3;font-size:10px;letter-spacing:.05em}
.equip-slot .si{display:block;font-weight:600;color:#cfd3dc;margin-top:2px;
  overflow-wrap:anywhere}
.equip-slot.filled{border-left:3px solid #6ee7a8}
.equip-slot.filled.hand{border-left-color:#ffd166}
@media (max-width:520px){.skill-grid{grid-template-columns:1fr}}
.empty{color:#8b93a3;font-size:13px;padding:10px 0}
#battlemap{background:#0d1014;border:1px solid #2a2d34;border-radius:8px;padding:6px}
#battlemap svg{width:100%;height:auto;max-height:60vh;display:block}
#worldgraph{background:#0d1014;border:1px solid #2a2d34;border-radius:8px;padding:6px;margin-bottom:8px}
#worldgraph svg{width:100%;height:auto;max-height:52vh;display:block}
#create-body .clist{max-height:150px;overflow:auto;border:1px solid #2a2d34;border-radius:6px;padding:6px;margin-top:4px}
#create-body .clist label{display:inline-block;margin:2px 10px 2px 0;white-space:nowrap}
#create-body input[type=text],#create-body input[type=number],#create-body select{background:#151a22;color:#e6e9ef;border:1px solid #2a2d34;border-radius:6px;padding:4px 6px}
#create-body .row{margin:6px 0}
#create-body .hint{color:#8b93a3;font-size:12px}
#create-preview{margin-top:8px;padding:8px;border:1px solid #2a2d34;border-radius:6px;background:#0d1014;white-space:pre-wrap}
#cLife{border:1px solid #3a4558;border-radius:8px;background:#0d1014;padding:10px;margin:8px 0}
#cLife .lq{font-size:14px;color:#e9e4d7;margin-bottom:8px}
#cLife .lopt{display:block;width:100%;text-align:left;margin:5px 0;padding:7px 9px;
  border:1px solid #333a46;border-radius:7px;background:#151a22;color:#e6e9ef;font:inherit;cursor:pointer}
#cLife .lopt:hover{border-color:#d8b46a}
#cLife .lbar{color:#8b93a3;font-size:12px;margin-bottom:6px}
#scenetext{white-space:pre-wrap;font-size:13px;color:#cfd3dc;line-height:1.7}
.optsheet h4{margin:10px 0 6px;font-size:12px;color:#8b93a3;letter-spacing:.06em}
.optsheet .g{display:grid;grid-template-columns:1fr 1fr;gap:6px}
@media (max-width:900px){
  #hud{order:3;flex-basis:100%}
  #slots .slot{width:96px}
  .grid2{grid-template-columns:1fr 1fr}
  .optsheet .g{grid-template-columns:1fr}
}
@media (max-width:520px){
  header{padding:6px 8px;gap:8px}
  header .brand{font-size:14px}
  .tool{padding:5px 8px;font-size:12px}
  #turnline{overflow-wrap:anywhere}
  #say{flex:1 1 100%}
  #submit,#allActions{flex:1 1 auto}
}

/* ================= 视觉主题 v2（2026-11-09）=================
   只改观感，不动结构/ID/语义；无外部字体与图片。
   调色：墨蓝底 + 羊皮纸金 + 苔绿生命条。 */
:root{
  --bg:#0d1016; --bg2:#11151d; --panel:#1a202b; --panel2:#151a23;
  --line:#2c3544; --line2:#3a4558;
  --ink:#e9e4d7; --muted:#9aa5b6; --dim:#778397;
  --gold:#d8b46a; --gold2:#f2d79b; --gold-dim:rgba(216,180,106,.18);
  --teal:#66c7a5; --blue:#8db4f7; --danger:#f08a82;
  --serif:ui-serif,Georgia,"Noto Serif SC","Songti SC",serif;
}
html,body{height:100%}
body{
  background:
    radial-gradient(1100px 560px at 12% -8%, rgba(216,180,106,.10), transparent 62%),
    radial-gradient(900px 480px at 92% -4%, rgba(102,199,165,.07), transparent 58%),
    linear-gradient(180deg,#0e1117 0%,#0b0d13 55%,#090b10 100%);
  color:var(--ink);
  font:15px/1.62 "Segoe UI","Noto Sans SC",system-ui,sans-serif;
}
body::before{
  content:"";position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.045;
  background-image:repeating-linear-gradient(0deg,rgba(255,255,255,.35) 0 1px,transparent 1px 3px);
  mix-blend-mode:overlay;
}
header{
  position:relative;z-index:3;padding:10px 18px;gap:12px;
  background:linear-gradient(180deg,rgba(22,27,37,.97),rgba(14,18,25,.94));
  border-bottom:1px solid rgba(216,180,106,.22);
  box-shadow:0 10px 30px rgba(0,0,0,.35);
  backdrop-filter:blur(10px);
}
header .brand{
  color:var(--gold2);font-family:var(--serif);font-weight:700;font-size:17px;
  letter-spacing:.14em;text-shadow:0 1px 0 rgba(0,0,0,.6);
  display:flex;align-items:center;gap:9px;white-space:nowrap
}
header .brand::before{
  content:"◆";color:var(--gold);font-size:13px;letter-spacing:0;
  text-shadow:0 0 12px rgba(216,180,106,.55)
}
#hud{color:#d9d3c5}
#hud .nm{font-family:var(--serif);font-size:15px;color:#f3ecdc;letter-spacing:.03em}
.hpbar{height:12px;background:#232a36;border:1px solid #313b4c;border-radius:999px;
  box-shadow:inset 0 1px 3px rgba(0,0,0,.45)}
.hpbar i{background:linear-gradient(90deg,#3f9e7d,#7fe0b7);
  box-shadow:0 0 10px rgba(102,199,165,.35)}
.hpbar span{line-height:12px;font-size:10.5px;font-weight:600;letter-spacing:.03em}
.chip{border-radius:999px;background:#2f2744;border-color:#6d55a8;color:#e6dcff}
.chip.bloodied{background:#4a2024;border-color:#c96a62;color:#ffc0ba}
#status{color:#9aa5b6;font-size:12px;letter-spacing:.02em}
#gameMode{
  appearance:none;-webkit-appearance:none;
  background:#121722;color:#e6dfd0;border:1px solid #394354;border-radius:999px;
  padding:5px 26px 5px 11px;font-size:12px;
  background-image:linear-gradient(45deg,transparent 50%,#9aa5b6 50%),
    linear-gradient(135deg,#9aa5b6 50%,transparent 50%);
  background-position:calc(100% - 14px) 50%,calc(100% - 9px) 50%;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat;
}
.toolbar{gap:5px}
.tool{
  background:linear-gradient(180deg,#1e2430,#171c26);
  border:1px solid #333d4d;border-radius:999px;color:#cfd8e6;
  padding:6px 12px;font-size:12.5px;letter-spacing:.02em;
  transition:border-color .18s,color .18s,transform .18s,box-shadow .18s;
}
.tool:hover{border-color:rgba(216,180,106,.7);color:#f6efdf;
  transform:translateY(-1px);box-shadow:0 5px 14px rgba(0,0,0,.35)}
.tool:active{transform:translateY(0)}
.tool[aria-pressed="true"]{border-color:rgba(216,180,106,.55);color:var(--gold2)}
#modebanner{
  background:linear-gradient(90deg,rgba(216,180,106,.14),rgba(216,180,106,.03));
  border:1px solid rgba(216,180,106,.42);border-radius:12px;color:#f0d18d;
  padding:9px 12px;box-shadow:0 8px 22px rgba(0,0,0,.25)
}
#modebanner::before{
  content:"";width:8px;height:8px;border-radius:50%;background:#e8b86d;
  box-shadow:0 0 10px #e8b86d;animation:bgmpulse 1.8s ease-in-out infinite;flex:0 0 auto
}
@keyframes bgmpulse{0%,100%{opacity:.55;transform:scale(.9)}50%{opacity:1;transform:scale(1.1)}}
#modebanner #takeOver{
  background:linear-gradient(180deg,#d8b46a,#b9924a);color:#1a1408;
  border:0;border-radius:999px;padding:5px 12px;font-weight:700;cursor:pointer
}
main{position:relative;z-index:1;justify-content:center}
#log{
  width:100%;max-width:1040px;margin:0 auto;
  padding:22px 28px 12px;font-family:var(--serif);font-size:16px;line-height:1.9;
  color:#ddd6c8;letter-spacing:.01em;
  background:radial-gradient(900px 400px at 50% 0%,rgba(216,180,106,.03),transparent 70%);
}
#log .ln{display:block;padding:1px 0}
#log .ln + .ln{margin-top:1px}
#log .ln-dm{color:#f1d79b;text-shadow:0 0 18px rgba(216,180,106,.10)}
#log .ln-pc{color:#b9cbe4}
#log .ln-private{color:#c5b3e6}
#log .ln-eng{color:#8793a6;font-family:"Segoe UI","Noto Sans SC",sans-serif;font-size:13px}
#log .ln-bad{color:#f08a82}
#log .ln-detail{color:#7f8b9d;font-family:"Segoe UI","Noto Sans SC",sans-serif;
  font-size:12.5px;line-height:1.6;padding-left:18px}
#dock{
  position:relative;z-index:2;padding:10px 16px 14px;gap:7px;
  align-items:center;
  background:linear-gradient(180deg,rgba(18,22,30,.92),rgba(11,14,20,.98));
  border-top:1px solid rgba(216,180,106,.18);
  box-shadow:0 -18px 40px rgba(0,0,0,.32);backdrop-filter:blur(8px)
}
#dock > *{width:100%;max-width:1040px}
#turnline{font-size:13px;color:#9aa5b6}
#turnline b{color:var(--gold2)}
#thinkline{color:#7f8b9d}
#thinkline b{color:#c0cddd}
#cats button{border-radius:999px;background:#161c26;border-color:#333d4d;
  padding:4px 11px;transition:border-color .18s,color .18s}
#cats button:hover{color:#e6dfd0;border-color:#4b586c}
#cats button.on{color:#f6efdf;border-color:rgba(216,180,106,.75);background:rgba(216,180,106,.10)}
#slots{gap:8px;min-height:78px}
.slot{
  width:126px;background:linear-gradient(160deg,#202633,#181d27);
  border:1px solid #333d4d;border-radius:12px;padding:9px 10px;
  transition:transform .18s,border-color .18s,box-shadow .18s;
}
.slot:hover{transform:translateY(-2px);border-color:rgba(216,180,106,.65);
  box-shadow:0 8px 20px rgba(0,0,0,.4)}
.slot.sel{border-color:var(--gold);background:linear-gradient(160deg,#262a33,#1c212b);
  box-shadow:0 0 0 1px var(--gold) inset,0 8px 22px rgba(216,180,106,.16)}
.slot .k{right:7px;top:6px;font-size:10px;color:#c9b98f;background:#121722;
  border:1px solid #3a4558;border-radius:6px;padding:0 5px;line-height:16px}
.slot .n{font-size:13.5px;font-weight:650;color:#f4eee0}
.slot small{color:#94a0b2;font-size:10.5px;height:30px}
.slot .warn{color:#f0b566}
#composer{gap:9px}
#say{
  min-height:46px;background:#11161f;border:1px solid #34404f;border-radius:12px;
  padding:11px 13px;color:#e9e4d7;transition:border-color .18s,box-shadow .18s
}
#say::placeholder{color:#6f7a8b}
#say:focus{border-color:rgba(216,180,106,.75);box-shadow:none}
#submit{
  border-radius:12px;background:linear-gradient(180deg,#e2bf77,#b9924a);
  color:#1b1407;font-weight:700;letter-spacing:.04em;padding:0 22px;
  box-shadow:0 6px 18px rgba(216,180,106,.18)
}
#submit:hover:not(:disabled){filter:brightness(1.06)}
#submit:disabled{background:#242b37;color:#758094;box-shadow:none}
#allActions{border-radius:12px;background:#161c26;border-color:#333d4d;padding:0 14px}
#allActions:hover{border-color:rgba(216,180,106,.55);color:#f6efdf}
#targetbox select{background:#121722;border-color:#3a4558;border-radius:9px}
#error{color:#f08a82}
.overlay{background:rgba(5,7,11,.76);backdrop-filter:blur(3px)}
.modal{
  background:linear-gradient(180deg,#1b212c,#12161e);
  border:1px solid #3a4558;border-radius:16px;
  box-shadow:0 30px 80px rgba(0,0,0,.6),0 0 0 1px rgba(216,180,106,.08) inset
}
.modal > header{padding:12px 16px;border-bottom:1px solid rgba(216,180,106,.18);
  background:linear-gradient(180deg,rgba(216,180,106,.06),transparent)}
.modal > header b{color:var(--gold2);font-family:var(--serif);font-size:16px;letter-spacing:.05em}
.x{color:#9aa5b6;border-radius:8px;width:30px;height:30px;line-height:1}
.x:hover{color:#f4eee0;background:#242b37}
.tabs{padding:10px 16px 0}
.tabs button{border-radius:8px 8px 0 0}
.tabs button.on{color:#f4eee0;border-bottom-color:var(--gold)}
.card,.s0-card,.quest-card,.item,.spellrow,.skill-card,.equip-slot,.loot-card,.init-card{
  background:linear-gradient(160deg,#1f2532,#171c26);border-color:#313b4b
}
.card h5{color:#9aa5b6}
.card .v{color:#f4eee0}
.card .s{color:#94a0b2}
.abil div{background:linear-gradient(160deg,#1e2431,#171c26);border-color:#313b4b}
.abil .m{color:var(--gold)}
.pip{background:#232a36;border-color:#3a4558}
.pip.on{background:var(--gold);border-color:var(--gold);box-shadow:0 0 8px rgba(216,180,106,.4)}
.item .mini,.spellrow .mini,.row .mini{background:#232c3c;border-color:#3a4558;color:#dfe6f0}
.item .mini:hover,.spellrow .mini:hover{background:#2b3546;border-color:#4b586c}
.qbadge{background:#2b3550;border-color:#3b5b96;color:#b9d0ff}
.qbadge.done{background:#173a2b;border-color:#2f6b4f;color:#9cf0c7}
#s0Start{background:linear-gradient(180deg,#e2bf77,#b9924a);color:#1b1407;border-radius:10px}
#s0Start:disabled{background:#242b37;color:#758094}
.s0-review{background:#0f141c;border-color:#2c3544}
.s0-review b{color:var(--gold2)}
#rvList .rvrow{background:#141a25;border-color:#2e3a4c;border-radius:8px}
#rvList .rvrow:hover{border-color:#4b5a70}
#rvDetail pre{background:#0d121a;border-color:#2e3a4c;border-radius:8px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2b3342;border-radius:999px;border:2px solid transparent;
  background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:#3d485b;background-clip:content-box}
@media (max-width:900px){
  #log{padding:16px 16px 8px;font-size:15.5px;line-height:1.8}
  header{padding:8px 12px;gap:9px}
}
@media (max-width:640px){
  /* 手机：三行网格（标题/状态、血条、工具横滑），别让工具把首屏吃掉 */
  header{
    display:grid;grid-template-columns:1fr auto;
    grid-template-areas:"brand status" "hud hud" "tools tools";
    gap:6px 10px;padding:8px 10px
  }
  header .brand{grid-area:brand;font-size:15px;letter-spacing:.08em}
  header .brand::before{font-size:11px}
  #status{grid-area:status;min-width:0;text-align:right;font-size:11px}
  #hud{grid-area:hud;gap:8px;min-width:0;flex-wrap:nowrap}
  .hpbar{width:118px}
  #hud .nm{font-size:13px}
  .toolbar{
    grid-area:tools;flex-wrap:nowrap;justify-content:flex-start;
    overflow-x:auto;max-width:100%;padding:2px 0 3px;
    scrollbar-width:none;-ms-overflow-style:none
  }
  .toolbar::-webkit-scrollbar{display:none}
  .tool{flex:0 0 auto;padding:5px 9px;font-size:12px}
  #gameMode{flex:0 0 auto}
  #forgeLink{display:none}
  #dock{padding:8px 10px 11px;gap:5px}
  #say{min-height:40px;max-height:64px;font-size:14px;padding:9px 11px}
  #submit,#allActions{padding:8px 12px;font-size:13px}
  .slot{width:108px;padding:7px 8px}
  .slot small{height:28px}
  #log{padding:12px 12px 6px;font-size:15px;line-height:1.75}
}
@media (max-width:420px){
  #hud{flex-wrap:wrap}
  .hpbar{width:104px}
  #slots{min-height:70px}
  .slot{width:102px}
}

/* ================= 视觉主题 v3：桌面 + 纸页（2026-11-11）=================
   只改观感，不动结构/ID/交互。深色桌壳 + 中间羊皮纸日志 + 卡面化大厅。 */
:root{
  --desk:#0b0f14; --desk2:#111720; --leather:#1a1713;
  --paper:#efe3cb; --paper2:#e8d9bb; --paper-line:#cdbb99;
  --ink-paper:#332a20; --ink-soft:#6c5d4a;
  --brass:#c9a45c; --brass2:#e8c989;
  --pc-ink:#17384a; --private-ink:#5b3f6e; --engine-ink:#756a58;
  --danger-paper:#9c3f37;
}
body{background:
  radial-gradient(1100px 600px at 18% -10%,rgba(201,164,92,.10),transparent 62%),
  radial-gradient(900px 500px at 88% 0%,rgba(92,138,132,.08),transparent 58%),
  linear-gradient(180deg,#0b0f14,#090c10 65%,#070a0e);}
header{padding:11px 20px;border-bottom-color:rgba(201,164,92,.28);
  background:linear-gradient(180deg,rgba(22,20,17,.98),rgba(11,15,20,.96));
  box-shadow:0 14px 38px rgba(0,0,0,.45)}
header .brand{color:#f0d7a6;font-size:18px;letter-spacing:.18em}
header .brand::before{color:#c9a45c}
.tool{border-color:#3b3930;background:linear-gradient(180deg,#1d211f,#141816)}
.tool:hover{border-color:rgba(201,164,92,.8);color:#fff4dc}
#gameMode{background:#141816;border-color:#3b3930;color:#e7ddc8}
#status{color:#9b9485}

/* 中间：真正的跑团纸页 */
main{justify-content:center;padding:0 14px;background:
  radial-gradient(1200px 700px at 50% 0%,rgba(201,164,92,.035),transparent 70%);}
#log{
  width:min(860px,100%);max-width:none;height:auto;flex:0 1 860px;align-self:stretch;
  margin:14px 0;padding:34px 42px 30px;
  background:
    linear-gradient(90deg,rgba(120,90,48,.07),transparent 16%,transparent 84%,rgba(120,90,48,.06)),
    radial-gradient(700px 280px at 50% -8%,rgba(255,255,255,.42),transparent 62%),
    repeating-linear-gradient(0deg,rgba(96,72,38,.025) 0 1px,transparent 1px 4px),
    var(--paper);
  color:var(--ink-paper);border:1px solid #c9b795;border-radius:16px;
  box-shadow:0 26px 60px rgba(0,0,0,.38),inset 0 0 0 1px rgba(255,250,235,.55),
    inset 0 0 70px rgba(112,78,34,.07);
  font-family:var(--serif);font-size:16.5px;line-height:1.92;
  letter-spacing:.015em;overflow:auto;
}
#log .ln{padding:1px 0;border-radius:2px}
#log .ln + .ln{margin-top:2px}
#log .ln-dm{color:#3a2d1f;font-weight:600;text-shadow:none}
#log .ln-pc{color:var(--pc-ink);padding-left:8px;border-left:2px solid #b8a47e}
#log .ln-private{color:var(--private-ink);background:rgba(91,63,110,.055);
  padding-left:8px;border-left:2px solid #b79acb}
#log .ln-eng{color:var(--engine-ink);font-family:"Segoe UI","Noto Sans SC",sans-serif;
  font-size:13px;line-height:1.7;background:rgba(83,69,48,.045);
  border:1px solid rgba(83,69,48,.08);border-radius:5px;padding:2px 7px}
#log .ln-bad{color:var(--danger-paper);background:rgba(156,63,55,.07);
  border-left:3px solid #b86157;padding-left:9px}
#log .ln-detail{color:#7a6a56;font-family:"Segoe UI","Noto Sans SC",sans-serif;
  font-size:12.5px;line-height:1.65;padding-left:20px}
#log .ln-say{color:#1c4253;font-weight:600;padding-left:8px;
  border-left:2px solid #7fa3ae}
#log .ln-dm::first-letter{font-size:1.2em}

/* 底部动作托盘 */
#dock{padding:11px 15px 15px;background:linear-gradient(180deg,rgba(18,16,13,.96),rgba(8,10,12,.99));
  border-top-color:rgba(201,164,92,.28)}
#dock > *{max-width:960px}
#turnline{color:#b9b09e}
#turnline b{color:var(--brass2)}
#thinkline{color:#948d7f}
#cats button{background:#171b1d;border-color:#3b3930}
#cats button.on{background:rgba(201,164,92,.13);border-color:var(--brass)}
#slots{gap:10px;min-height:82px}
.slot{background:linear-gradient(160deg,#20211d,#151817);border-color:#3b3930;
  border-radius:12px;width:134px}
.slot:hover{transform:translateY(-3px);border-color:rgba(201,164,92,.8)}
.slot.sel{border-color:var(--brass);background:linear-gradient(160deg,#2a2820,#1b1d19);
  box-shadow:0 0 0 1px var(--brass) inset,0 10px 24px rgba(201,164,92,.14)}
.slot .n{color:#f5ead2}
.slot small{color:#a39a89}
#say{background:#0e1215;border-color:#3b3930;color:#efe2c7}
#say:focus{border-color:var(--brass)}
#submit{background:linear-gradient(180deg,#dfbd7c,#b98e45);color:#1b1408}
#allActions{background:#161a1a;border-color:#3b3930;color:#d4c8b2}
#allActions:hover{border-color:var(--brass)}

/* 大厅：卡面化 */
.session0 .modal{width:min(980px,96vw);background:linear-gradient(180deg,#1b1d1b,#111515)}
.s0-review{background:linear-gradient(180deg,#f1e5ce,#dfcfae);color:#332a20;
  border-color:#c9b795;box-shadow:inset 0 0 40px rgba(112,78,34,.06)}
.s0-review b{color:#5a3f20}
.s0-pc{background:linear-gradient(160deg,#f0e3c8,#dfcda8);color:#332a20;
  border-color:#c9b795}
.s0-pc b{color:#5a3f20}.s0-pc .muted{color:#74634d}
.s0-stage{background:#f1e5ce;color:#332a20;border-color:#c9b795}
.s0-stage b{color:#6b4a22}
.s0-stage button{background:#e5d2ac;color:#332a20;border-color:#bda77f}
.s0-stage select{background:#fbf3df;color:#332a20;border-color:#bda77f}
.s0-hint{color:#a59a86}
.s0-preset-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0}
.s0-preset-card{position:relative;text-align:left;background:linear-gradient(160deg,#f4e7cd,#dfcda9);
  color:#332a20;border:1px solid #c9b795;border-radius:10px;padding:9px 10px;
  font:inherit;font-size:13px;line-height:1.45;cursor:pointer;min-height:92px;
  transition:transform .15s,border-color .15s,box-shadow .15s}
.s0-preset-card:hover{transform:translateY(-2px);border-color:#a98445}
.s0-preset-card.sel{border:2px solid #8a6327;box-shadow:0 0 0 2px rgba(143,106,43,.28);
  background:linear-gradient(160deg,#fff8e8,#e8d0a4);transform:translateY(-2px)}
.s0-preset-card .pc-mark{position:absolute;right:8px;top:7px;font-size:10px;
  color:#7a5a24;font-weight:700}
.s0-preset-card.sel .pc-mark{color:#5a3f12}
.s0-preset-card .pc-name{display:block;font-weight:700;font-size:14px;color:#4b331a}
.s0-preset-card .pc-meta{display:block;color:#6c5a42;font-size:12px;margin-top:3px}
.s0-preset-card .pc-mark{position:absolute;right:8px;top:7px;font-size:11px;color:#7d633b}
.s0-preset-card .pc-hp{display:block;color:#756247;font-size:11px;margin-top:4px}
.s0-advanced{margin:8px 0;padding:8px 10px;border:1px solid #bda77f;border-radius:9px;
  background:rgba(255,250,238,.35)}
.s0-advanced summary{cursor:pointer;color:#5a452a;font-weight:600;font-size:13px;
  list-style:none;outline:none}
.s0-advanced summary::-webkit-details-marker{display:none}
.s0-advanced summary::before{content:"▸";display:inline-block;margin-right:6px;
  color:#8a6327;transition:transform .15s}
.s0-advanced[open] summary::before{transform:rotate(90deg)}
.s0-advanced .row2{margin:6px 0 0}
#s0PresetCards{display:none}
#s0PresetUse{background:linear-gradient(180deg,#dfbd7c,#b98e45);color:#1b1408;
  border:0;border-radius:9px;font-weight:700;padding:8px 12px}
#s0PresetUse:disabled{opacity:.5;filter:grayscale(.4)}
#s0Start{background:linear-gradient(180deg,#dfbd7c,#b98e45);color:#1b1408}
#s0Start:disabled{background:#24282a;color:#7f837c}

/* 地图弹窗：给六边格足够空间 */
.overlay[data-panel="map"] .modal{width:min(1180px,96vw);height:min(88vh,900px)}
.overlay[data-panel="map"] .modal .body{flex:1;min-height:0;display:flex;flex-direction:column}
#worldgraph{flex:1;min-height:0;display:flex;flex-direction:column}
#worldgraph svg{flex:1;min-height:0;max-height:none;height:100%}
#battlemap{flex:1;min-height:0}
#battlemap svg{max-height:none;height:100%}
#worldgraph,#battlemap{background:#0b1216;border-color:#39433f}

@media (max-width:900px){
  #log{margin:8px 0;padding:24px 22px;border-radius:12px;font-size:15.5px;line-height:1.82}
  .s0-preset-grid{grid-template-columns:1fr 1fr}
}
@media (max-width:520px){
  main{padding:0 8px}
  #log{padding:18px 16px;margin:6px 0;font-size:15px;line-height:1.78}
  #log .ln-eng,#log .ln-detail{font-size:12px}
  .s0-preset-grid{grid-template-columns:1fr 1fr;gap:6px}
  .s0-preset-card{min-height:84px;padding:8px}
  .overlay[data-panel="map"] .modal{height:92vh;width:96vw}
}

</style></head>
<body>
<header>
  <div class="brand">D&amp;D 牌桌</div>
  <div id="hud">
    <span class="nm" id="hud-name">布兰</span>
    <div class="hpbar"><i id="hpfill"></i><span id="hptext">--/--</span></div>
    <span id="hud-ac">AC --</span>
    <span id="chips"></span>
  </div>
  <div class="toolbar">
    <button class="tool" data-panel="sheet" aria-haspopup="dialog" aria-expanded="false">状态</button>
    <button class="tool" data-panel="inventory" aria-haspopup="dialog" aria-expanded="false">背包</button>
    <button class="tool" data-panel="map" aria-haspopup="dialog" aria-expanded="false">地图</button>
    <button class="tool" data-panel="quests" aria-haspopup="dialog" aria-expanded="false">任务</button>
    <button class="tool" data-panel="npc" aria-haspopup="dialog" aria-expanded="false">人物</button>
    <button class="tool" data-panel="zero" id="zeroTool" hidden
            aria-haspopup="dialog" aria-expanded="false"
            title="这桌是怎么开起来的（读档后仍保留）">开局</button>
    <button class="tool" data-panel="create" aria-haspopup="dialog" aria-expanded="false">建卡</button>
    <button class="tool" data-panel="settings" aria-haspopup="dialog" aria-expanded="false">设置</button>
    <button class="tool" data-panel="diag" aria-haspopup="dialog" aria-expanded="false">自检</button>
    <button class="tool" id="thinkToggle" type="button" aria-pressed="true"
            title="关掉之后连「谁在想」都不显示">思考提示</button>
    <button class="tool" data-panel="review" aria-haspopup="dialog" aria-expanded="false"
            title="看每次模型调用（含思考链，剧透）">复盘</button>
    <select id="gameMode" aria-label="开局模式" title="开局模式：真人 / 托管 / 观战">
      <option value="normal">真人</option>
      <option value="hosted">托管</option>
      <option value="spectate">观战</option>
    </select>
    <button class="tool" id="modeStart" title="按左侧模式重开一局（会丢当前进度）">按此模式新开</button>
    <button class="tool" id="bgmBtn" aria-pressed="false"
            title="背景音乐（浏览器合成，无外部音频；点一下开/关）">BGM</button>
    <a class="tool" id="forgeLink" target="_blank" rel="noopener"
       title="配套工具（独立运行）：python -m tools.forge serve --port 8790"
       href="http://127.0.0.1:8790">锻造台</a>
  </div>
  <span id="status" role="status" aria-live="polite">连接中…</span>
</header>
<main><div id="log" role="log" aria-live="polite" aria-relevant="additions"></div></main>
<div id="dock">
  <div id="modebanner" hidden><span id="modeText"></span><button id="takeOver" hidden>接管</button></div>
  <div id="turnline">等待轮到你…</div>
<div id="thinkline" role="status" aria-live="polite"></div>
  <div id="cats"></div>
  <div id="slots"></div>
  <div id="targetbox"></div>
  <div id="composer">
    <textarea id="say" maxlength="500" aria-label="台词（可选）" placeholder="台词（可选）：先说，再点技能"></textarea>
    <button id="submit" disabled>提交</button>
    <button id="allActions">全部动作</button>
  </div>
  <div id="error" role="alert" aria-live="assertive"></div>
</div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="角色状态" data-panel="sheet"><div class="modal">
  <header><b>角色状态</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="tabs" id="sheet-tabs">
    <button data-stab="overview" class="on">概览</button>
    <button data-stab="status">状态</button>
    <button data-stab="equip">装备</button>
    <button data-stab="spells">法术</button>
  </div>
  <div class="body" id="sheet-body"></div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="背包" data-panel="inventory"><div class="modal">
  <header><b>背包</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body" id="inv-body"></div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="地图" data-panel="map"><div class="modal">
  <header><b>地图</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="tabs" id="map-tabs">
    <button data-mtab="battle" class="on">战斗</button>
    <button data-mtab="scene">场景</button>
  </div>
  <div class="body">
    <div id="initiative"></div>
    <div id="battlemap"></div>
    <div id="worldgraph" style="display:none"></div>
    <div id="travel" style="display:none"></div>
    <div id="scenetext" style="display:none"></div>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="任务日志" data-panel="quests"><div class="modal">
  <header><b>任务日志</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body" id="quests-body"></div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="开局记录" data-panel="zero"><div class="modal">
  <header><b>开局记录</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body" id="zero-body"></div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="人物" data-panel="npc"><div class="modal">
  <header><b>人物（当局）</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body" id="npc-body"></div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="人物数值" data-panel="dm-npc"><div class="modal">
  <header><b>人物数值 / 造人物</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body dmform">
    <p class="empty" id="dmn-hint">给人物写结构化数值后，他才能用自己的 AC / 生命 / 攻击 / 豁免参与引擎结算；只写文字的人物仍是叙事对象。</p>
    <label class="empty" for="dmn-name">名字</label>
    <input type="text" id="dmn-name" aria-label="人物名字">
    <label class="empty" for="dmn-role">身份（如：哨塔守卫）</label>
    <input type="text" id="dmn-role" aria-label="人物身份">
    <div class="row"><span class="l">等级</span><input type="number" id="dmn-level" aria-label="等级"></div>
    <div class="row"><span class="l">护甲等级</span><input type="number" id="dmn-ac" aria-label="护甲等级"></div>
    <div class="row"><span class="l">生命值上限</span><input type="number" id="dmn-hp" aria-label="生命值上限"></div>
    <div class="row"><span class="l">先攻加值（总值）</span><input type="number" id="dmn-init" aria-label="先攻加值"></div>
    <div class="row"><span class="l">速度（尺）</span><input type="number" id="dmn-speed" aria-label="速度"></div>
    <div class="row"><span class="l">力量</span><input type="number" id="dmn-str" aria-label="力量"></div>
    <div class="row"><span class="l">敏捷</span><input type="number" id="dmn-dex" aria-label="敏捷"></div>
    <div class="row"><span class="l">体质</span><input type="number" id="dmn-con" aria-label="体质"></div>
    <div class="row"><span class="l">智力</span><input type="number" id="dmn-int" aria-label="智力"></div>
    <div class="row"><span class="l">感知</span><input type="number" id="dmn-wis" aria-label="感知"></div>
    <div class="row"><span class="l">魅力</span><input type="number" id="dmn-cha" aria-label="魅力"></div>
    <label class="empty" for="dmn-saves">豁免熟练（用逗号分隔，如：敏捷,体质）</label>
    <input type="text" id="dmn-saves" aria-label="豁免熟练">
    <label class="empty" for="dmn-perc">察觉总加值（留空按感知调整值）</label>
    <input type="number" id="dmn-perc" aria-label="察觉加值">
    <label class="empty" for="dmn-atkname">攻击名称（留空 = 这个人物不会攻击）</label>
    <input type="text" id="dmn-atkname" aria-label="攻击名称">
    <div class="row"><span class="l">攻击加值</span><input type="number" id="dmn-atk" aria-label="攻击加值"></div>
    <div class="row"><span class="l">伤害骰（如 1d8+2）</span><input type="text" id="dmn-dmg" aria-label="伤害骰"></div>
    <div class="row"><span class="l">伤害类型</span><input type="text" id="dmn-dmgtype" aria-label="伤害类型"></div>
    <div class="row"><span class="l">射程（尺，留空 = 近战）</span><input type="number" id="dmn-range" aria-label="射程"></div>
    <div class="row"><button class="mini" id="dmn-save">写入当局</button><button class="mini" data-close>取消</button></div>
    <p class="empty">填了攻击字段会替换该人物的全部动作；多动作/豁免型技能只走接口。</p>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="DM 补述" data-panel="lore"><div class="modal">
  <header><b>DM 补述</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body">
    <p class="empty" id="lore-target"></p>
    <label class="empty" for="lore-text">描述：外观、材质、手感、传闻。</label>
    <textarea id="lore-text" rows="4" aria-label="描述"></textarea>
    <label class="empty" for="lore-mech">机制 / 数值 / 效果：DM 按桌面情况补；这里只作裁定记录，引擎不自动验算。</label>
    <textarea id="lore-mech" rows="3" aria-label="机制与数值"></textarea>
    <label class="empty" for="lore-bg">背景：来历、制造者、世界里的位置。</label>
    <textarea id="lore-bg" rows="3" aria-label="背景"></textarea>
    <div class="row"><button class="mini" id="loreSave">保存</button><button class="mini" id="loreClear">清除设定</button><button class="mini" data-close>取消</button></div>
    <p class="empty">文字设定会写入世界树并跟存档；结构化数值（造物/改数值）单独进当局引擎，不写进世界书。</p>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="DM 造物" data-panel="dm-item"><div class="modal">
  <header><b>DM 造物 / 改数值</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body dmform">
    <p class="empty" id="dm-item-hint">世界书只给框架；这里填的数值会进当局引擎，新开一局不保留。</p>
    <label class="empty" for="dm-name">名称</label>
    <input type="text" id="dm-name" aria-label="物品名称">
    <label class="empty" for="dm-category">类别</label>
    <select id="dm-category" aria-label="物品类别">
      <option value="武器">武器</option><option value="护甲">护甲</option>
      <option value="盾牌">盾牌</option><option value="饰品">饰品</option>
      <option value="日常服装">日常服装</option>
      <option value="消耗品">消耗品</option>
      <option value="物品">物品</option>
    </select>
    <label class="empty" for="dm-type">装备类型</label>
    <select id="dm-type" aria-label="装备类型">
      <option value="战斗">战斗装备</option><option value="日常">日常穿着</option>
    </select>
    <label class="s0-ack"><input type="checkbox" id="dm-two">
      <span>双手武器（占左右手两格，不能配盾）</span></label>
    <label class="empty">词条（从词条库选；装备最多 3 条、品级合计 ≤6）</label>
    <select id="dm-entry1" aria-label="词条 1"><option value="">— 无 —</option></select>
    <select id="dm-entry2" aria-label="词条 2"><option value="">— 无 —</option></select>
    <select id="dm-entry3" aria-label="词条 3"><option value="">— 无 —</option></select>
    <div class="row"><button class="mini" id="dm-open-entries">词条库 / 写新词条</button></div>
    <div class="row"><span class="l">攻击加值（命中总加值）</span><input type="number" id="dm-atk" aria-label="攻击加值"></div>
    <div class="row"><span class="l">伤害骰（如 2d6+5）</span><input type="text" id="dm-dmg" aria-label="伤害骰"></div>
    <div class="row"><span class="l">伤害类型</span><input type="text" id="dm-dmgtype" placeholder="挥砍 / 火焰…" aria-label="伤害类型"></div>
    <div class="row"><span class="l">护甲加值（基础 AC）</span><input type="number" id="dm-ac" aria-label="护甲加值"></div>
    <div class="row"><span class="l">护甲类型</span><select id="dm-actype" aria-label="护甲类型">
      <option value="">—</option><option value="轻甲">轻甲</option><option value="中甲">中甲</option>
      <option value="重甲">重甲</option><option value="盾牌">盾牌</option></select></div>
    <label class="empty" for="dm-desc">描述</label>
    <textarea id="dm-desc" rows="3" aria-label="物品描述"></textarea>
    <label class="empty" for="dm-mech">机制 / 数值 / 效果</label>
    <textarea id="dm-mech" rows="3" aria-label="物品机制"></textarea>
    <label class="empty" for="dm-bg">背景</label>
    <textarea id="dm-bg" rows="3" aria-label="物品背景"></textarea>
    <div class="row"><button class="mini" id="dm-save">写入当局</button><button class="mini" data-close>取消</button></div>
    <p class="empty">写入的是**当局状态**：跟存档，不进下一局，也不进世界书。</p>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="词条库" data-panel="entries"><div class="modal">
  <header><b>词条库 / 写词条</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body dmform">
    <p class="empty">引擎只认这里的词汇：免死、临时生命、回合回复，以及 AC/攻击/豁免/速度/生命等常驻属性。自写词条只在本战役有效。</p>
    <div class="row"><span class="l">id（小写字母/数字/_，如 x_life_blessing）</span><input type="text" id="de-id" aria-label="词条 id"></div>
    <div class="row"><span class="l">名称</span><input type="text" id="de-name" aria-label="词条名称"></div>
    <div class="row"><span class="l">品级</span><select id="de-tier" aria-label="品级">
      <option value="1">常见</option><option value="2">精良</option>
      <option value="3">稀有</option><option value="4">传说</option></select></div>
    <div class="row"><span class="l">适用</span><select id="de-applies" aria-label="适用">
      <option value="通用">通用</option><option value="装备">装备</option><option value="生物">生物</option></select></div>
    <div class="row"><span class="l">触发</span><select id="de-trigger" aria-label="触发">
      <option value="常驻">常驻</option><option value="战斗开始">战斗开始</option>
      <option value="回合开始">回合开始</option><option value="致命伤害">致命伤害</option></select></div>
    <div class="row"><span class="l">属性 / 效果</span><select id="de-kind" aria-label="属性或效果"></select></div>
    <div class="row"><span class="l">数值</span><input type="number" id="de-value" aria-label="数值"></div>
    <div class="row"><span class="l">保留生命（免死）</span><input type="number" id="de-keep" aria-label="保留生命"></div>
    <div class="row"><span class="l">每场次数（0=不限）</span><input type="number" id="de-per" aria-label="每场次数"></div>
    <div class="row"><span class="l">冷却回合</span><input type="number" id="de-cd" aria-label="冷却回合"></div>
    <label class="empty" for="de-note">说明（给 DM 和玩家看，不进计算）</label>
    <textarea id="de-note" rows="2" aria-label="词条说明"></textarea>
    <div class="row"><button class="mini" id="de-save">写入本战役</button><button class="mini" data-close>关闭</button></div>
    <h4>现有词条</h4>
    <div id="entries-list"></div>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="DM 技能" data-panel="dm-action"><div class="modal">
  <header><b>DM 技能 / 造动作</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body dmform">
    <p class="empty" id="dma-hint">写进当局引擎；数值写清楚，引擎照常掷骰。新开一局不保留。</p>
    <label class="empty" for="dma-name">名称</label>
    <input type="text" id="dma-name" aria-label="技能名称">
    <div class="row"><span class="l">类型</span><select id="dma-type" aria-label="技能类型">
      <option value="attack">攻击型（命中 + 伤害）</option>
      <option value="save">豁免型（目标豁免 / 范围伤害）</option>
      <option value="heal">治疗型（回复生命）</option>
      <option value="utility">描述型（附加效果交 DM 裁定）</option>
    </select></div>
    <div class="row"><span class="l">动作槽</span><select id="dma-slot" aria-label="动作槽">
      <option value="action">动作</option><option value="bonus_action">附赠动作</option>
    </select></div>
    <div class="row"><span class="l">攻击加值（留空按力量/敏捷+熟练）</span><input type="number" id="dma-atk" aria-label="攻击加值"></div>
    <div class="row"><span class="l">伤害骰（如 2d6+4）</span><input type="text" id="dma-dmg" aria-label="伤害骰"></div>
    <div class="row"><span class="l">伤害类型（挥砍/火焰…）</span><input type="text" id="dma-dmgtype" aria-label="伤害类型"></div>
    <div class="row"><span class="l">豁免属性</span><select id="dma-ability" aria-label="豁免属性">
      <option value="">—</option><option value="力量">力量</option><option value="敏捷">敏捷</option>
      <option value="体质">体质</option><option value="智力">智力</option>
      <option value="感知">感知</option><option value="魅力">魅力</option></select></div>
    <div class="row"><span class="l">豁免 DC</span><input type="number" id="dma-dc" aria-label="豁免 DC"></div>
    <label class="s0-ack"><input type="checkbox" id="dma-half" checked>
      <span>豁免成功只受半伤</span></label>
    <div class="row"><span class="l">范围（尺，0 = 单体）</span><input type="number" id="dma-area" aria-label="范围"></div>
    <div class="row"><span class="l">范围形状</span><select id="dma-shape" aria-label="范围形状">
      <option value="sphere">球形</option><option value="cone">锥形</option>
      <option value="cube">立方</option><option value="line">线形</option>
      <option value="emanation">光环</option></select></div>
    <div class="row"><span class="l">射程 / 治疗距离（尺）</span><input type="number" id="dma-range" aria-label="射程"></div>
    <div class="row"><span class="l">治疗骰（如 2d4+2）</span><input type="text" id="dma-heal" aria-label="治疗骰"></div>
    <div class="row"><span class="l">每日次数（0 = 不限）</span><input type="number" id="dma-uses" aria-label="每日次数"></div>
    <label class="empty" for="dma-desc">描述：动作怎么做、看起来什么样</label>
    <textarea id="dma-desc" rows="3" aria-label="技能描述"></textarea>
    <label class="empty" for="dma-mech">机制 / 背景（只作说明，不进引擎）</label>
    <textarea id="dma-mech" rows="2" aria-label="技能机制与背景"></textarea>
    <div class="row"><button class="mini" id="dma-write">写入当局</button><button class="mini" data-close>取消</button></div>
    <p class="empty">攻击/豁免/治疗由引擎结算；控制、召唤、变形等附加效果仍交 DM 裁定。</p>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="全部动作" data-panel="all"><div class="modal">
  <header><b>全部动作</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body optsheet" id="all-body"></div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="建卡" data-panel="create"><div class="modal">
  <header><b>建卡（真人座位）</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body" id="create-body"></div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="设置" data-panel="settings"><div class="modal">
  <header><b>设置</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body">
    <div class="row"><span class="l">模型</span><span>默认假模型；真模型需显式授权启动</span></div>
    <div class="row"><span class="l">轮询</span><span>0.8 秒 / 5 秒超时</span></div>
    <div class="row"><span class="l">开桌说明</span><button class="mini" id="openSession0">再看一遍</button></div>
    <div class="row"><span class="l">存档槽</span><button class="mini" id="saveSlot1">存1</button><button class="mini" id="saveSlot2">存2</button><button class="mini" id="saveSlot3">存3</button><button class="mini" id="saveGame">存最近</button></div>
    <div class="row"><span class="l">读档槽</span><button class="mini" id="loadSlot1">读1</button><button class="mini" id="loadSlot2">读2</button><button class="mini" id="loadSlot3">读3</button><button class="mini" id="loadGame">读最近</button></div>
    <div class="row"><span class="l">槽位状态</span><span id="save-slots">—</span></div>
    <div class="row"><span class="l">导出/导入</span><button class="mini" id="exportSave">下载存档</button><button class="mini" id="importSave">导入文件</button><input type="file" id="importFile" aria-label="选择存档文件" accept=".json,application/json" style="display:none"></div>
    <div class="row"><span class="l">BGM 音量</span><input type="range" id="bgmVol" min="0" max="100" value="35" aria-label="背景音乐音量"></div>
    <div class="row"><span class="l">重开</span><button class="mini" id="newGame">新的一局</button></div>
    <div class="row"><span class="l">版本</span><span id="set-version">—</span></div>
    <p class="empty">设置页先占位；P4 接真模型后在这里填端点、模型名和 key。</p>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="复盘" data-panel="review"><div class="modal">
  <header><b>复盘：每次调用看到了什么、想了什么</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body">
    <p class="empty" style="color:#e8b86d">⚠️ 含剧透（DM 私有信息 / 思考链）；别给玩家座位看。</p>
    <div class="row"><span class="l">存档</span><select id="rvGame" aria-label="选择存档"></select></div>
    <div id="rvList"></div>
    <div id="rvDetail"></div>
  </div>
</div></div>

<div class="overlay" role="dialog" aria-modal="true" aria-label="自检" data-panel="diag"><div class="modal">
  <header><b>自检：这台机器/浏览器能跑吗</b><button class="x" data-close aria-label="关闭">×</button></header>
  <div class="body">
    <p class="empty">下面是**只读**的本机事实：浏览器能力 + 服务端配置。不含密钥。</p>
    <div id="diagRows"></div>
    <div class="row"><span class="l">操作</span>
      <button class="mini" id="diagRefresh">重新检测</button>
      <button class="mini" id="diagCopy">复制自检信息</button>
    </div>
  </div>
</div></div>

<div class="overlay open session0" id="session0" role="dialog" aria-modal="true" aria-label="开桌">
  <div class="modal">
    <header><b>开桌：先对一下桌规</b><button class="x" id="s0Close" aria-label="关闭">×</button></header>
    <div class="body">
      <p class="empty">这不是教程关；是这张桌的四条共识。规则判定仍由引擎执行，DM 只负责把世界讲清楚。</p>
      <div class="s0-grid">
        <div class="s0-card"><b>共同叙事</b>DM 描述世界，你决定角色。世界不会为剧情让路，但会为你的选择回应。</div>
        <div class="s0-card"><b>DM 不是敌人，也不是保姆</b>敌人会动脑、规则会执行；DM 不会凭空放水，也不会故意刁难。</div>
        <div class="s0-card"><b>失败也是故事</b>0 HP 走死亡豁免；团灭可以读档/重开。输掉一场战斗，不等于输掉整段旅程。</div>
        <div class="s0-card"><b>代价与权衡</b>法术位、药水、火把、短休都要算。你不需要“最优解”，只需要承担选择的后果。</div>
      </div>
      <div class="s0-pitch" id="s0-pitch"></div>
      <div class="s0-party" id="s0-party"></div>
      <div class="s0-audit" id="s0-audit"></div>
      <div class="s0-stage" id="s0-stage">
        <div><b>本桌剧本</b>：<span id="s0-stage-title"></span></div>
        <div class="row2">
          <select id="s0-stage-select" aria-label="选择本桌剧本"></select>
          <button id="s0StageGo" type="button">切换剧本</button>
        </div>
        <div class="row2">
          <select id="s0-size" aria-label="本桌总玩家数">
            <option value="1">1 人（单人）</option>
            <option value="2">2 人（真人 + 1 AI）</option>
            <option value="3">3 人（真人 + 2 AI）</option>
            <option value="4">4 人（真人 + 3 AI）</option>
            <option value="5">5 人（真人 + 4 AI）</option>
          </select>
          <button id="s0SizeGo" type="button">定桌</button>
        </div>
        <details class="s0-advanced">
          <summary>自定义卡 / 高级开桌</summary>
          <div class="row2">
            <button id="s0GenCompanions" type="button">让 AI 自行建卡</button>
            <span class="s0-hint" id="s0-companion-note"></span>
          </div>
          <div class="row2">
            <input id="s0DmBrief" maxlength="300" placeholder="或告诉 DM：你想要怎样的同伴？">
            <button id="s0DmGo" type="button">和 DM 说同伴</button>
          </div>
          <div class="row2">
            <select id="s0CompCards" multiple size="3" aria-label="已建好的同伴卡"
                    style="min-width:220px"></select>
            <button id="s0CompUse" type="button">使用手搓同伴</button>
          </div>
          <div class="row2">
            <select id="s0HumanCard" aria-label="卡库里的真人卡"></select>
            <button id="s0HumanUse" type="button">使用真人卡</button>
          </div>
        </details>
        <div class="s0-stage" id="s0-preset-box">
          <div><b>本模块预设卡</b>：<span id="s0-preset-note"></span></div>
          <div id="s0PresetGrid" class="s0-preset-grid" role="group" aria-label="本模块预设卡"></div>
          <select id="s0PresetCards" multiple size="5" aria-label="本模块预设卡（兼容）"></select>
          <button id="s0PresetUse" type="button">按预设卡开桌</button>
        </div>
        <div class="s0-hint" id="s0-stage-note"></div>
        <button id="s0JustStart" type="button">直接上桌（跳过问答）</button>
      </div>
      <div class="s0-intros" id="s0-intros"></div>
      <button id="s0Intros" type="button">让三位队友说一句</button>
      <div class="s0-guided" id="s0-guided">
        <div class="s0-gq" id="s0-gq"></div>
        <div class="s0-qrow">
          <input id="s0-ga" maxlength="300" placeholder="用一两句话回答">
          <button id="s0Gbtn" type="button">回答</button>
          <button id="s0Gskip" type="button">跳过</button>
        </div>
        <div class="s0-hint" id="s0-ghint"></div>
      </div>
      <div class="s0-qa" id="s0-qa"></div>
      <div class="s0-qrow" id="s0-qrow">
        <input id="s0-q" maxlength="200" placeholder="问 DM：战役/角色/开局，最多 3 个问题">
        <button id="s0Ask" type="button">问 DM</button>
        <button id="s0Create" type="button">调整角色卡</button>
      </div>
      <div class="s0-hint" id="s0-qhint"></div>
      <div class="s0-review" id="s0-review">角色卡读取中…</div>
      <label class="s0-ack"><input type="checkbox" id="s0-ack">
        <span>我明白这四条；可以开始了。</span></label>
      <button id="s0Start" disabled>开始冒险</button>
    </div>
  </div>
</div>

<script>
let since = 0, seq = 0, renderedSeq = 0, busy = false, selected = null;
let lastSheet = null, lastLobby = null, lastMap = null, lastScene = null, lastGraph = null, lastShop = null;
let lastQuests = null, lastZero = null;
let loreTarget = null;
let itemMode = {mode:'new', name:''};
let actionMode = {mode:'new', name:''};
let npcMode = {mode:'new', name:''};
let lastNpcs = [];
let hotkeys = [], activeCat = 'all', allOpts = [], sheetTab = 'overview';

const $ = s => document.querySelector(s);
//: 拼 innerHTML 时的转义（自检面板等用；页面其它地方走 textContent，不需要它）
function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
const logEl = $('#log'), statusEl = $('#status'), hpfill = $('#hpfill');
const hptext = $('#hptext'), hudac = $('#hud-ac'), chipsEl = $('#chips');
const slotsEl = $('#slots'), catsEl = $('#cats'), turnEl = $('#turnline');
const errEl = $('#error'), sayEl = $('#say'), submitEl = $('#submit');
const tgtEl = $('#targetbox');

function el(tag, cls, text){const e=document.createElement(tag);
  if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e;}
function svg(tag, attrs){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(const k in attrs){ if(k==='text'){e.textContent=attrs[k];} else {e.setAttribute(k,attrs[k]);} }
  return e;}

const GROUP_ORDER = [
  ['attack','攻击 / 动作'], ['spell','法术'], ['save_action','范围动作'],
  ['move','移动'], ['defend','防御'], ['team','协作'], ['other','其他']
];
const GRP = {attack:'attack',spell:'spell',save_action:'save_action',special:'attack',
  move:'move',dash:'move',disengage:'defend',dodge:'defend',hide:'defend',
  help:'team',ready_attack:'team',ready:'team',end_turn:'other'};
function groupOf(kind){ return GRP[kind] || 'other'; }
function catOf(kind){
  if(kind === 'spell') return 'spell';
  if(kind === 'move' || kind === 'dash') return 'move';
  if(kind === 'attack' || kind === 'save_action' || kind === 'special') return 'action';
  return 'other';
}
function unavailable(o){
  if(o.requires_target && !(o.targets && o.targets.length)) return true;
  return /没有合法目标|需先移动|不可用|没有目标/.test(o.note || '');
}

function hpBand(hp, max){
  if(!max) return '';
  if(hp <= 0) return '倒地';
  if(hp <= max / 2) return '见血';
  return '状态尚好';
}

let humanCardsLoaded = false;
async function loadHumanCards(){
  const sel = $('#s0HumanCard'); if(!sel || humanCardsLoaded) return;
  humanCardsLoaded = true;
  try{
    const d = await actJson({kind:'list_characters', role:'human'});
    if(!d.ok) return;
    sel.textContent = '';
    const rows = d.value || [];
    if(!rows.length){
      const op = el('option','','（卡库没有真人卡）'); op.value=''; op.disabled=true; sel.append(op);
      const b = $('#s0HumanUse'); if(b) b.disabled = true;
      return;
    }
    for(const c of rows){
      const op = el('option','',`${c.name}｜${c.klass} ${c.level}｜${c.species}`);
      op.value = c.id; sel.append(op);
    }
  }catch(e){ /* 不挡大厅 */ }
}

let compCardsKey = '';
let presetCardsKey = '';
const PRESET_CLASS_CN = {Cleric:'牧师',Paladin:'圣武士',Fighter:'战士',Rogue:'游荡者',Wizard:'法师',Ranger:'游侠',Barbarian:'野蛮人',Bard:'吟游诗人',Druid:'德鲁伊',Monk:'武僧',Sorcerer:'术士',Warlock:'邪术师'};
const PRESET_SPECIES_CN = {Dwarf:'矮人',Human:'人类',Elf:'精灵',Halfling:'半身人',Dragonborn:'龙裔',Gnome:'侏儒',Goliath:'歌利亚',Orc:'兽人',Tiefling:'提夫林'};
let presetSelected = new Set();
async function loadCompanionCards(){
  const sel = $('#s0CompCards'); if(!sel || !lastLobby) return;
  const key = String((lastLobby.table && lastLobby.table.ai) || 0);
  if(compCardsKey === key) return;
  compCardsKey = key;
  try{
    const d = await actJson({kind:'list_characters', role:'companion'});
    if(!d.ok) return;
    sel.textContent = '';
    const rows = d.value || [];
    if(!rows.length){
      const op = el('option','','（卡库没有同伴卡）'); op.value=''; op.disabled=true; sel.append(op); return;
    }
    for(const c of rows){
      const op = el('option','',`${c.name}｜${c.klass} ${c.level}｜${c.species}`);
      op.value = c.id; sel.append(op);
    }
  }catch(e){ /* 不挡大厅 */ }
}

function renderPresetSelection(total){
  const note = $('#s0-preset-note'), use = $('#s0PresetUse');
  const grid = $('#s0PresetGrid');
  if(grid){
    for(const tile of grid.querySelectorAll('.s0-preset-card')){
      const on = presetSelected.has(tile.dataset.id);
      tile.classList.toggle('sel', on);
      tile.setAttribute('aria-pressed', on ? 'true' : 'false');
      const mark = tile.querySelector('.pc-mark');
      if(mark) mark.textContent = on ? '✓ 已选' : '';
    }
  }
  if(note) note.textContent = `已选 ${presetSelected.size}/${total} 张（第一张给真人位；观战则全给 AI）`;
  if(use) use.disabled = presetSelected.size !== total;
}

function renderSession0(){
  const box = $('#s0-review'), pitch = $('#s0-pitch'), party = $('#s0-party');
  const start = $('#s0Start');
  if(!box || !pitch || !party) return;
  const lob = lastLobby;
  if(lob && lob.active){
    pitch.style.display = 'block';
    pitch.textContent = lob.pitch || '';
    party.style.display = 'grid';
    party.textContent = '';
    for(const c of (lob.characters || [])){
      const card = el('div', 's0-pc');
      card.append(el('b', '', c.name || '冒险者'));
      card.append(el('div', '', `${c.job || '—'}｜${c.level || '?'} 级`));
      card.append(el('div', 'muted', `HP ${c.hp}/${c.hp_max} · AC ${c.ac}`));
      if(c.top && c.top.name) card.append(el('div', 'muted',
        `最高：${c.top.name} ${c.top.value}`));
      party.append(card);
    }
    const audit = $('#s0-audit');
    if(audit){ audit.style.display = 'block'; audit.textContent = lob.audit || ''; }
    const st = $('#s0-stage'), stTitle = $('#s0-stage-title'), stNote = $('#s0-stage-note');
    const stSel = $('#s0-stage-select'), stGo = $('#s0StageGo');
    const szSel = $('#s0-size'), szGo = $('#s0SizeGo');
    if(szSel && lob.table){
      szSel.value = String(lob.table.total || 1);
      if(szGo) szGo.disabled = false;
    }
    const gen = $('#s0GenCompanions'), genNote = $('#s0-companion-note');
    const dmBrief = $('#s0DmBrief'), dmGo = $('#s0DmGo');
    if(gen){
      const hasAI = !!(lob.table && lob.table.ai);
      gen.style.display = hasAI ? 'inline-block' : 'none';
      gen.disabled = !hasAI || lob.companion_source !== 'pregen';
      gen.textContent = lob.companion_source === 'ai' ? 'AI 已自建角色卡'
        : (lob.companion_source === 'dm' ? 'DM 已提案同伴' : '让 AI 自行建卡');
    }
    if(dmBrief){ dmBrief.style.display = (lob.table && lob.table.ai) ? 'inline-block' : 'none'; dmBrief.disabled = lob.companion_source !== 'pregen'; }
    if(dmGo){ dmGo.style.display = (lob.table && lob.table.ai) ? 'inline-block' : 'none'; dmGo.disabled = lob.companion_source !== 'pregen'; }
    const compSel = $('#s0CompCards'), compUse = $('#s0CompUse');
    if(compSel){ compSel.style.display = (lob.table && lob.table.ai) ? 'inline-block' : 'none'; compSel.disabled = lob.companion_source !== 'pregen'; }
    if(compUse){ compUse.style.display = (lob.table && lob.table.ai) ? 'inline-block' : 'none'; compUse.disabled = lob.companion_source !== 'pregen'; }
    const hSel = $('#s0HumanCard'), hUse = $('#s0HumanUse');
    if(hSel){
      hSel.style.display = (lob.mode === 'spectate') ? 'none' : 'inline-block';
      hSel.disabled = lob.mode === 'spectate';
    }
    if(hUse){ hUse.style.display = (lob.mode === 'spectate') ? 'none' : 'inline-block'; hUse.disabled = lob.mode === 'spectate'; }
    const presetBox = $('#s0-preset-box'), presetSel = $('#s0PresetCards'),
          presetUse = $('#s0PresetUse'), presetNote = $('#s0-preset-note');
    const presets = lob.presets || [];
    if(presetBox) presetBox.style.display = presets.length ? 'block' : 'none';
    const presetGrid = $('#s0PresetGrid');
    if(presetSel){
      const total = (lob.table && lob.table.total) || 3;
      const key = presets.map(x => x.id).join(',') + '|' + total;
      // 只保留本模块仍存在的卡；换剧本后不拿旧 ID 上桌
      presetSelected = new Set([...presetSelected].filter(id =>
        presets.some(x => x.id === id)));
      if(presetCardsKey !== key){
        presetCardsKey = key;
        presetSel.textContent = ''; if(presetGrid) presetGrid.textContent = '';
        for(const c of presets){
          const op = el('option','',`${c.name}｜${c.klass} ${c.level}｜${c.species}`);
          op.value = c.id; presetSel.append(op);
          if(presetGrid){
            const tile = el('button','s0-preset-card');
            tile.type = 'button'; tile.dataset.id = c.id;
            tile.setAttribute('aria-pressed', 'false');
            tile.append(el('span','pc-name',c.name || c.id));
            tile.append(el('span','pc-mark',''));
            const kcn = PRESET_CLASS_CN[c.klass] || c.klass || '—';
            const scn = PRESET_SPECIES_CN[c.species] || c.species || '—';
            tile.append(el('span','pc-meta',`${kcn} ${c.level || '?'} 级｜${scn}`));
            tile.append(el('span','pc-hp',c.hp ? `HP ${c.hp}/${c.hp_max} · ${c.role === 'human' ? '真人卡' : '同伴卡'}` : (c.role === 'human' ? '真人卡' : '同伴卡')));
            tile.onclick = () => {
              const id = c.id;
              if(presetSelected.has(id)) presetSelected.delete(id);
              else {
                if(presetSelected.size >= total){
                  const first = [...presetSelected][0];
                  presetSelected.delete(first);
                }
                presetSelected.add(id);
              }
              for(const opt of presetSel.options)
                opt.selected = presetSelected.has(opt.value);
              renderPresetSelection(total);
            };
            presetGrid.append(tile);
          }
        }
      }
      for(const tile of (presetGrid ? presetGrid.querySelectorAll('.s0-preset-card') : [])){
        const on = presetSelected.has(tile.dataset.id);
        tile.classList.toggle('sel', on);
        tile.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      if(presetNote) presetNote.textContent = `已选 ${presetSelected.size}/${total} 张（第一张给真人位；观战则全给 AI）`;
      if(presetUse) presetUse.disabled = presetSelected.size !== total;
    }
    if(lob.mode !== 'spectate') loadHumanCards();
    if(lob.table && lob.table.ai && lob.companion_source !== 'manual') loadCompanionCards();
    if(genNote){
      const errs = lob.companion_errors || {};
      const errText = Object.entries(errs).map(([k,v]) => `${k}（${v}）`).join('；');
      genNote.textContent = (lob.companion_source === 'ai')
        ? '本桌 AI 已用自己的卡' : (lob.companion_source === 'dm'
          ? '本桌同伴由 DM 提案' : (lob.table && lob.table.ai
            ? (errText ? ('fallback：' + errText) : '先让 AI 按世界简介写卡，失败会自动用预生成卡。')
            : ''));
    }
    if(st){
      st.style.display = 'block';
      if(stTitle) stTitle.textContent = (lob.stage && lob.stage.title) || '—';
      if(stNote) stNote.textContent = (lob.stage && lob.stage.note) || '';
      if(stSel){
        stSel.textContent = '';
        const opts = (lob.stage && lob.stage.options) || [];
        for(const o of opts){
          const op = document.createElement('option');
          op.value = o.id; op.textContent = o.title;
          stSel.append(op);
        }
        if(lob.stage && lob.stage.id) stSel.value = lob.stage.id;
        stSel.disabled = opts.length < 2;
      }
      if(stGo) stGo.disabled = !(lob.stage && lob.stage.options && lob.stage.options.length > 1);
    }
    const ib = $('#s0-intros'), ibtn = $('#s0Intros');
    if(ib){
      ib.style.display = 'block';
      ib.textContent = '';
      const intros = lob.intros || [];
      for(const x of intros){
        const d = el('div');
        d.append(el('b', 'who', x.name + '：'), document.createTextNode(x.text || ''));
        ib.append(d);
      }
      if(!intros.length) ib.append(el('div', 'empty', '还没让队友开口；可以先听他们各说一句。'));
    }
    if(ibtn){
      const hasAI = !!(lob.table && lob.table.ai);
      ibtn.style.display = hasAI ? 'inline-block' : 'none';
      ibtn.disabled = !hasAI || !!(lob.intros && lob.intros.length);
      ibtn.textContent = (lob.intros && lob.intros.length) ? '队友已介绍'
        : `让 ${lob.table ? lob.table.ai : 0} 位 AI 队友说一句`;
    }
    const gd = $('#s0-guided'), gq = $('#s0-gq'), ga = $('#s0-ga');
    const gb = $('#s0Gbtn'), gs = $('#s0Gskip'), gh = $('#s0-ghint');
    const step = String(lob.guided_step || 'done');
    if(gd){
      gd.style.display = 'block';
      gq.textContent = lob.guided_prompt || '';
      if(step === 'done'){
        if(ga){ ga.disabled = true; ga.value = ''; }
        if(gb) gb.disabled = true;
        if(gs) gs.disabled = true;
        const n = lob.notes || {};
        if(gh) gh.textContent = `动机：${n.motivation || '（跳过）'}｜羁绊：${n.bonds || '（跳过）'}`;
      }else{
        if(ga){ ga.disabled = false; }
        if(gb) gb.disabled = false;
        if(gs) gs.disabled = false;
        if(gh) gh.textContent = step === 'motivation' ? '第 1/2 题' : '第 2/2 题';
      }
    }
    const qaBox = $('#s0-qa'), qrow = $('#s0-qrow'), qin = $('#s0-q'), qbtn = $('#s0Ask');
    const qhint = $('#s0-qhint');
    if(qaBox){
      qaBox.style.display = 'block';
      qaBox.textContent = '';
      for(const x of (lob.qa || [])){
        qaBox.append(el('div', 'q', '你：' + (x.q || '')));
        qaBox.append(el('div', 'a', 'DM：' + (x.a || '')));
      }
      if(!(lob.qa || []).length) qaBox.append(el('div', 'q', '还没提问。点“问 DM”问一句战役/角色相关的事。'));
    }
    const used = (lob.qa || []).length, limit = Number(lob.qa_limit || 3);
    if(qrow) qrow.style.display = 'flex';
    if(qin){ qin.disabled = used >= limit; qin.placeholder = used >= limit
      ? '问满了；确认上桌吧' : '问 DM：战役/角色/开局，最多 ' + limit + ' 个问题'; }
    if(qbtn) qbtn.disabled = used >= limit;
    if(qhint) qhint.textContent = `已问 ${used}/${limit}；只回答战役设定，不推进剧情。`;
    box.style.display = 'none';
    if(start) start.textContent = `${(lob.table && lob.table.total) || 4} 人确认上桌`;
    return;
  }
  pitch.style.display = 'none';
  party.style.display = 'none';
  party.textContent = '';
  const qaBoxH = $('#s0-qa'), qrowH = $('#s0-qrow'), qhintH = $('#s0-qhint');
  const auditH = $('#s0-audit'), guidedH = $('#s0-guided'), stageH = $('#s0-stage');
  const introsH = $('#s0-intros'), introsBtnH = $('#s0Intros');
  if(auditH) auditH.style.display = 'none';
  if(guidedH) guidedH.style.display = 'none';
  if(stageH) stageH.style.display = 'none';
  if(introsH) introsH.style.display = 'none';
  if(introsBtnH) introsBtnH.style.display = 'none';
  if(qaBoxH) qaBoxH.style.display = 'none';
  if(qrowH) qrowH.style.display = 'none';
  if(qhintH) qhintH.textContent = '';
  box.style.display = '';
  if(start) start.textContent = '开始冒险';
  if(!lastSheet){ box.textContent = '角色卡读取中…'; return; }
  const s = lastSheet;
  box.textContent = '';
  const line = el('div');
  line.append(el('b', '', s.name || '冒险者'),
    document.createTextNode('　' + (Object.keys(s.jobs || {}).join(' / ') || '—')));
  box.append(line);
  const ab = Object.entries(s.abilities || {})
    .sort((a, b) => Number(b[1]) - Number(a[1]));
  box.append(el('div', '', ab.length ? `最高属性：${ab[0][0]} ${ab[0][1]}` : '属性未记录'));
  box.append(el('div', '', `HP ${s.hp}/${s.hp_max}（${hpBand(s.hp, s.hp_max)}） · AC ${s.ac} · 先攻 ${s.init}`));
  box.append(el('div', '', 'DM 会按 2024 规则核对这张卡；属性低也能玩，桌面不替你做构筑决定。'));
}

function applyLobby(s){
  lastLobby = (s && s.lobby) ? s.lobby : null;
  renderSession0();
  const s0 = $('#session0');
  const other = document.querySelector('.overlay.open:not(#session0)');
  if(lastLobby && lastLobby.active && s0 && !s0.classList.contains('open') && !other){
    openSession0();
  }
}

function openSession0(){
  const s0 = $('#session0'); if(!s0) return;
  const ack = $('#s0-ack');
  if(ack) ack.checked = false;
  const start = $('#s0Start'); if(start) start.disabled = true;
  renderSession0(); s0.classList.add('open');
  if(ack) ack.focus();
}

function updateHud(sheet, p){
  if(!sheet) return;
  lastSheet = sheet;
  $('#hud-name').textContent = sheet.name || '—';
  const max = sheet.hp_max || 1, hp = sheet.hp || 0;
  hpfill.style.width = Math.max(0, Math.min(100, hp / max * 100)) + '%';
  hptext.textContent = `${hp}/${sheet.hp_max}${sheet.temp_hp ? ' +'+sheet.temp_hp : ''}`;
  hudac.textContent = 'AC ' + (sheet.ac ?? '—');
  chipsEl.textContent = '';
  for(const c of (sheet.conditions || [])){
    chipsEl.append(el('span','chip', c.name + (c.rounds > 0 ? `(${c.rounds})` : '')));
  }
  if(sheet.concentrating) chipsEl.append(el('span','chip','专注：'+sheet.concentrating));
  if(sheet.exhaustion) chipsEl.append(el('span','chip','力竭 '+sheet.exhaustion));
  if(hp > 0 && hp <= max / 2) chipsEl.append(el('span','chip bloodied','见血'));
  renderSession0();
  if($('[data-panel="sheet"]').classList.contains('open')) renderSheet();
}

function qLine(label, text){
  const d = el('div', 'qline');
  d.append(el('b', '', label), document.createTextNode(text));
  return d;
}

function renderZero(){
  const box = $('#zero-body'); if(!box) return;
  box.textContent = '';
  const z = lastZero;
  if(!z || !z.stage){
    box.append(el('div', 'empty', '这一局没有开局记录（旧存档或直接开局）。'));
    return;
  }
  const head = el('div', 'quest-head');
  head.append(el('div', 'qtitle', z.stage || '—'));
  head.append(el('div', 'qmeta', z.confirmed_at
    ? ('确认上桌：' + z.confirmed_at) : '开局记录'));
  box.append(head);
  box.append(qLine('审卡：', z.audit || '—'));
  const n = z.notes || {};
  box.append(qLine('动机：', n.motivation || '（跳过）'));
  box.append(qLine('羁绊：', n.bonds || '（跳过）'));
  const intros = z.intros || [];
  if(intros.length){
    box.append(el('h4', '', '队友介绍'));
    for(const x of intros){
      const d = el('div', 'qline');
      d.append(el('b', '', x.name + '：'), document.createTextNode(x.text || ''));
      box.append(d);
    }
  }
}

function renderQuests(){
  const box = $('#quests-body'); if(!box) return;
  box.textContent = '';
  const q = lastQuests;
  if(!q || !q.title){
    box.append(el('div', 'empty', '任务日志加载中…'));
    return;
  }
  const head = el('div', 'quest-head');
  head.append(el('div', 'qtitle', q.title));
  head.append(el('div', 'qmeta',
    `已完成 ${q.completed || 0} 场` + (q.current ? ' · 当前任务进行中' : ' · 这一段已跑完')));
  box.append(head);

  if(q.current){
    const c = el('div', 'quest-card');
    c.append(el('span', 'qbadge', '当前目标'));
    c.append(el('div', 'qtitle', q.current.title));
    c.append(qLine('目标：', q.current.objective || '（本场没有写简报）'));
    if((q.current.clues || []).length){
      c.append(qLine('已发现线索：', (q.current.clues || [])
        .map(x => x.text).filter(Boolean).join('；')));
    }
    box.append(c);
  } else {
    box.append(el('div', 'empty', '这一段剧本已经跑完。'));
  }

  const receipts = q.receipts || [];
  if(receipts.length){
    box.append(el('h4', '', '结算回执'));
    for(const r of receipts){
      const card = el('div', 'receipt');
      const title = el('div', 'qtitle', `第 ${r.index} 场 · ${r.title || r.scene}`);
      title.append(el('span', 'qbadge done', '已完成'));
      card.append(title);
      if((r.clues || []).length)
        card.append(qLine('线索：', (r.clues || []).map(x => x.text)
          .filter(Boolean).join('；')));
      if((r.closed || []).length)
        card.append(qLine('收线：', (r.closed || []).map(x => x.title)
          .filter(Boolean).join('；')));
      if(!(r.clues || []).length && !(r.closed || []).length)
        card.append(el('div', 'qline', '本场没有登记线索或收线。'));
      box.append(card);
    }
  } else {
    box.append(el('div', 'empty', '还没有结算回执；跑完一场后这里会出现。'));
  }
  if(q.note) box.append(el('p', 'empty', q.note));
}

function renderInitiative(m, p){
  const box = $('#initiative'); if(!box) return;
  box.textContent = '';
  if(!m || !m.battle){ box.style.display = 'none'; return; }
  const byId = {};
  for(const u of (m.units || [])) byId[u.id] = u;
  const order = (m.order || []).map(id => byId[id]).filter(Boolean);
  if(!order.length){ box.style.display = 'none'; return; }
  box.style.display = 'flex';
  order.forEach((u, i) => {
    const active = p && (p.actor === u.id || p.actor === u.name);
    const card = el('div', 'init-card ' + (u.team || '') +
      (active ? ' active' : '') + (u.alive ? '' : ' down'));
    card.append(el('span', 'n', `${i + 1}. ${u.name}${active ? ' ▶' : ''}`));
    card.append(el('span', 'meta',
      `HP ${u.hp}/${u.hp_max} · 先攻 ${u.init_score != null ? u.init_score : i + 1}`));
    if((u.conditions || []).length)
      card.append(el('span', 'meta', '状态：' + u.conditions.join('、')));
    box.append(card);
  });
}

function drawMap(box, m, p){
  if(!m || !m.battle){ box.textContent = ''; box.append(el('div','empty','探索阶段没有战斗地图（没有坐标就不伪造）。')); return; }
  box.textContent = '';
  const units = (m.units || []).filter(u => u.alive || u.hp > 0);
  if(!units.length){ box.append(el('div','empty','（战场为空）')); return; }
  const xs = units.map(u => u.pos[0]), ys = units.map(u => u.pos[1]);
  const pad = 10, cell = 30;
  const minx = Math.min(...xs)-pad, maxx = Math.max(...xs)+pad;
  const miny = Math.min(...ys)-pad, maxy = Math.max(...ys)+pad;
  const W = Math.max(140, (maxx-minx)/5*cell + cell);
  const H = Math.max(100, (maxy-miny)/5*cell + cell);
  const root = svg('svg', {viewBox:`0 0 ${W} ${H}`, preserveAspectRatio:'xMidYMid meet'});
  root.append(svg('rect',{x:0,y:0,width:W,height:H,fill:'#0d1014'}));
  for(let gx=Math.floor(minx/5)*5; gx<=maxx; gx+=5){
    const x=(gx-minx)/5*cell+cell/2;
    root.append(svg('line',{x1:x,y1:0,x2:x,y2:H,stroke:'#1e242e','stroke-width':1}));
  }
  for(let gy=Math.floor(miny/5)*5; gy<=maxy; gy+=5){
    const y=(gy-miny)/5*cell+cell/2;
    root.append(svg('line',{x1:0,y1:y,x2:W,y2:y,stroke:'#1e242e','stroke-width':1}));
  }
  const colors = {pc:'#6ea8ff',ally:'#6ee7a8',enemy:'#ff7b72'};
  for(const u of units){
    const cx=(u.pos[0]-minx)/5*cell+cell/2, cy=(u.pos[1]-miny)/5*cell+cell/2, r=11;
    const active = p && (p.actor === u.id || p.actor === u.name);
    const circle = svg('circle',{cx,cy,r,fill:colors[u.team]||'#8b93a3',
      stroke: active ? '#ffd166' : '#0d1014','stroke-width': active?3:1});
    circle.append(svg('title',{text:`${u.name}  HP ${u.hp}/${u.hp_max}`}));
    root.append(circle);
    root.append(svg('text',{x:cx,y:cy+4,fill:'#0d1014','font-size':11,
      'font-weight':700,'text-anchor':'middle',text:(u.name||'?').slice(0,1)}));
    const ratio=Math.max(0,Math.min(1,u.hp_max?u.hp/u.hp_max:0));
    root.append(svg('rect',{x:cx-r,y:cy+r+3,width:2*r,height:3,fill:'#2a2d34',rx:1}));
    root.append(svg('rect',{x:cx-r,y:cy+r+3,width:2*r*ratio,height:3,
      fill:colors[u.team]||'#8b93a3',rx:1}));
    if(active) root.append(svg('text',{x:cx,y:cy-r-5,fill:'#ffd166',
      'font-size':10,'text-anchor':'middle',text:'▶'}));
  }
  box.append(root);
}

function hexPoint(q, r, size){
  // pointy-top hex：轴向坐标 -> 像素
  return [size*Math.sqrt(3)*(q + r/2), size*1.5*r];
}
function hexPath(cx, cy, size){
  const pts = [];
  for(let i=0;i<6;i++){
    const a = Math.PI/180 * (60*i - 30);
    pts.push(`${(cx + size*Math.cos(a)).toFixed(1)},${(cy + size*Math.sin(a)).toFixed(1)}`);
  }
  return 'M' + pts.join('L') + 'Z';
}
function drawRegionMap(box, g, m){
  box.textContent = '';
  const size = Number(m.size || 24);
  const rows = Array.isArray(m.terrain) ? m.terrain : [];
  const cells = [];
  const byName = {};
  const locs = m.locations || [];
  for(const l of locs){ if(l && l.name) byName[l.name] = [Number(l.q)||0, Number(l.r)||0]; }
  let minQ=0,maxQ=0,minR=0,maxR=0,any=false;
  if(rows.length){
    rows.forEach((row, r) => {
      const chars = [...String(row)];
      chars.forEach((ch, col) => {
        const q = col - Math.floor(r/2);
        cells.push({q, r, t: ch});
        if(!any){minQ=maxQ=q;minR=maxR=r;any=true;}
        else {minQ=Math.min(minQ,q);maxQ=Math.max(maxQ,q);minR=Math.min(minR,r);maxR=Math.max(maxR,r);}
      });
    });
  } else if(Array.isArray(m.cells)){
    for(const c of m.cells){
      const q=Number(c.q)||0, r=Number(c.r)||0;
      cells.push({q,r,t:String(c.t||'.')});
      if(!any){minQ=maxQ=q;minR=maxR=r;any=true;}
      else {minQ=Math.min(minQ,q);maxQ=Math.max(maxQ,q);minR=Math.min(minR,r);maxR=Math.max(maxR,r);}
    }
  }
  const spanX = (maxQ-minQ+1)*Math.sqrt(3)*size + size*2;
  const spanY = (maxR-minR+1)*1.5*size + size*2;
  const pad=40;
  const W=Math.max(560, spanX+pad*2), H=Math.max(380, spanY+pad*2);
  const ox = pad + (W-pad*2-spanX)/2;
  const oy = pad + (H-pad*2-spanY)/2;
  const pt = (q,r) => [ox + Math.sqrt(3)*size*(q-minQ) + Math.sqrt(3)*size*((r-minR)/2),
                       oy + 1.5*size*(r-minR)];
  const root = svg('svg',{viewBox:`0 0 ${W} ${H}`,preserveAspectRatio:'xMidYMid meet'});
  root.append(svg('rect',{x:0,y:0,width:W,height:H,fill:'#0b151d'}));
  const colors = {'~':'#16364a','.':'#2d3a2a','f':'#1f3024','^':'#4a4136','s':'#6b5a3c','v':'#4b2222','#':'#22303a'};
  for(const c of cells){
    const p=pt(c.q,c.r);
    root.append(svg('path',{d:hexPath(p[0],p[1],size-1),
      fill: colors[c.t] || '#24313b', stroke:'#0b151d','stroke-width':1}));
  }
  // 道路/已知连接
  const seen = new Set();
  const drawEdge = (a,b,known,label) => {
    const pa=byName[a], pb=byName[b];
    if(!pa || !pb) return;
    const k=[a,b].sort().join('|'); if(seen.has(k)) return; seen.add(k);
    const p1=pt(pa[0],pa[1]), p2=pt(pb[0],pb[1]);
    const line=svg('line',{x1:p1[0],y1:p1[1],x2:p2[0],y2:p2[1],
      stroke:known?'#b99b63':'#53606e','stroke-width':known?2:1.5,
      'stroke-dasharray':known?'':'5 5'});
    if(label) line.append(svg('title',{text:label}));
    root.append(line);
  };
  for(const r of (m.roads||[])){
    if(Array.isArray(r)) drawEdge(r[0],r[1],true,'道路');
    else if(r && (r.a||r.from) && (r.b||r.to))
      drawEdge(r.a||r.from,r.b||r.to,true,r.note||'道路');
  }
  for(const e of (g.edges||[])) drawEdge(e.a,e.b,!!e.known,e.note||'');
  const style={current:{fill:'#ffd166',ring:'#ffe9a8',r:20},
               discovered:{fill:'#202836',ring:'#6ea8ff',r:16},
               heard:{fill:'#12161d',ring:'#5a6478',r:13}};
  const nodeNames = new Set((g.nodes||[]).map(n => n.name));
  // 地图上未被玩家发现的地点：只画地标轮廓，不替它编“已发现”
  for(const l of locs){
    if(!l || !l.name || nodeNames.has(l.name)) continue;
    const qr = byName[l.name]; if(!qr) continue;
    const p = pt(qr[0],qr[1]);
    const _unk = svg('path',{d:hexPath(p[0],p[1],8),fill:'#0e171c',
      stroke:'#495565','stroke-width':1,'stroke-dasharray':'3 3'});
    _unk.append(svg('title',{text:l.name + '（未发现）'}));
    root.append(_unk);
  }
  for(const n of (g.nodes||[])){
    const qr=byName[n.name] || [0,0]; const p=pt(qr[0],qr[1]);
    const st=style[n.state]||style.discovered;
    const poly=svg('path',{d:hexPath(p[0],p[1],st.r),fill:st.fill,
      stroke:st.ring,'stroke-width':n.state==='current'?3:2});
    if(n.state==='heard') poly.setAttribute('stroke-dasharray','4 4');
    poly.append(svg('title',{text:n.name+(n.region?`（${n.region}）`:'')+' ｜ '+(n.description||'')}));
    root.append(poly);
    root.append(svg('text',{x:p[0],y:p[1]+4,fill:n.state==='heard'?'#8b93a3':'#0d1014',
      'font-size':11,'font-weight':700,'text-anchor':'middle',text:(n.name||'?').slice(0,1)}));
    root.append(svg('text',{x:p[0],y:p[1]+st.r+14,fill:'#d7dfec','font-size':10,
      'text-anchor':'middle',text:n.name.length>8?n.name.slice(0,7)+'…':n.name}));
  }
  box.append(root);
  const legend=m.legend||{'~':'海','.':'草地','f':'林','^':'山','s':'滩','v':'火山'};
  const bits=['区域六边格（1 格≈1/2 英里）',
    Object.entries(legend).map(([k,v])=>`${k}=${v}`).join('　')];
  box.append(el('div','empty',bits.join('　｜　')));
}

function drawGraph(box, g){
  box.textContent = '';
  if(!g || !((g.nodes || []).length || (g.hexmap && (g.hexmap.terrain || g.hexmap.cells)))){
    box.append(el('div','empty','还没有地点可画（本局没登记地图）。'));
    return;
  }
  if(g.hexmap && (g.hexmap.terrain || g.hexmap.cells)){
    drawRegionMap(box,g,g.hexmap); return;
  }
  const nodes = g.nodes, edges = g.edges || [];
  const spiral = [[0,0],[1,0],[0,1],[-1,1],[-1,0],[0,-1],[1,-1],
                  [2,0],[1,1],[0,2],[-1,2],[-2,2],[-2,1],[-2,0],
                  [-1,-1],[0,-2],[1,-2],[2,-2],[2,-1],[3,0]];
  const raw = {};
  nodes.forEach((n,i) => {
    if(Array.isArray(n.hex) && n.hex.length === 2){
      raw[n.name] = [Number(n.hex[0]) || 0, Number(n.hex[1]) || 0];
    } else {
      raw[n.name] = spiral[i] || [i, 0];
    }
  });
  const size = 28;
  const pts = {};
  for(const n of nodes){
    const qr = raw[n.name] || [0,0];
    pts[n.name] = hexPoint(qr[0], qr[1], size);
  }
  let minx = 0, maxx = 0, miny = 0, maxy = 0;
  for(const p of Object.values(pts)){
    minx = Math.min(minx, p[0]); maxx = Math.max(maxx, p[0]);
    miny = Math.min(miny, p[1]); maxy = Math.max(maxy, p[1]);
  }
  const pad = 56;
  const ox = -minx + pad, oy = -miny + pad;
  const W = Math.max(560, maxx - minx + pad*2);
  const H = Math.max(380, maxy - miny + pad*2);
  const pos = {};
  for(const n of nodes){
    const p = pts[n.name];
    pos[n.name] = [p[0] + ox, p[1] + oy];
  }
  const root = svg('svg',{viewBox:`0 0 ${W} ${H}`, preserveAspectRatio:'xMidYMid meet'});
  root.append(svg('rect',{x:0,y:0,width:W,height:H,fill:'#0d1014'}));
  // 六边格背景：旅行层是 1 格≈1/2 英里，不拿它当 5 尺战斗格。
  const qs = Object.values(raw).map(x=>x[0]), rs = Object.values(raw).map(x=>x[1]);
  const q0 = Math.min(...qs)-2, q1 = Math.max(...qs)+2;
  const r0 = Math.min(...rs)-2, r1 = Math.max(...rs)+2;
  for(let q=q0;q<=q1;q++){
    for(let r=r0;r<=r1;r++){
      const p = hexPoint(q,r,size);
      const hx = p[0]+ox, hy = p[1]+oy;
      root.append(svg('path',{d:hexPath(hx,hy,size-2),
        fill:'none',stroke:'#222b38','stroke-width':1}));
    }
  }
  for(const e of edges){
    const p1 = pos[e.a], p2 = pos[e.b];
    if(!p1 || !p2) continue;
    const line = svg('line',{x1:p1[0],y1:p1[1],x2:p2[0],y2:p2[1],
      stroke: e.known ? '#3b4658' : '#262c36','stroke-width': e.known ? 2 : 1.5});
    if(!e.known) line.setAttribute('stroke-dasharray','5 5');
    if(e.note) line.append(svg('title',{text:e.note}));
    root.append(line);
  }
  const style = {
    current:   {fill:'#ffd166', ring:'#ffe9a8', r:22},
    discovered:{fill:'#202836', ring:'#6ea8ff', r:18},
    heard:     {fill:'#12161d', ring:'#5a6478', r:15}
  };
  for(const n of nodes){
    const p = pos[n.name]; if(!p) continue;
    const st = style[n.state] || style.discovered;
    const poly = svg('path',{d:hexPath(p[0],p[1],st.r),
      fill:st.fill, stroke:st.ring, 'stroke-width': n.state === 'current' ? 3 : 2});
    if(n.state === 'heard') poly.setAttribute('stroke-dasharray','4 4');
    poly.append(svg('title',{text: n.name + (n.region ? `（${n.region}）` : '')
      + ' ｜ ' + (n.description || '')}));
    root.append(poly);
    root.append(svg('text',{x:p[0], y:p[1]+4,
      fill: n.state === 'heard' ? '#8b93a3' : '#0d1014',
      'font-size':11,'font-weight':700,'text-anchor':'middle',
      text:(n.name || '?').slice(0,1)}));
    const label = n.name.length > 8 ? n.name.slice(0,7)+'…' : n.name;
    root.append(svg('text',{x:p[0], y:p[1]+st.r+14, fill:'#c9d1e0',
      'font-size':10,'text-anchor':'middle', text:label}));
  }
  box.append(root);
  const fog = g.fog || {};
  const bits = ['六边格：旅行层（1 格≈1/2 英里，不用于 5 尺战斗）',
                '● 当前 ○ 去过 ◌ 只听说过', '实线＝已知的路 虚线＝只听说过'];
  if(fog.hidden) bits.push(`另有 ${fog.hidden} 处没听说过`);
  box.append(el('div','empty', bits.join('　｜　')));
}

function slotCard(o, idx){
  const b = el('button','slot');
  b.title = `${o.id}｜${o.summary||''}${o.note?'｜⚠️ '+o.note:''}`;
  b.append(el('span','k', idx != null ? String(idx) : ''));
  b.append(el('span','n', o.name || o.id));
  b.append(el('small','', o.summary || ''));
  if(o.note) b.append(el('span','warn', '⚠️ '+o.note));
  b.onclick = () => pick(o);
  b.dataset.id = o.id;
  return b;
}

function renderCats(p){
  catsEl.textContent = '';
  const avail = (p.options||[]).filter(o => !unavailable(o));
  const counts = {all: avail.length, action:0, spell:0, move:0, other:0};
  for(const o of avail) counts[catOf(o.kind)]++;
  const defs = [['all','全部'],['action','动作'],['spell','法术'],['move','移动'],['other','其他']];
  for(const [k,label] of defs){
    const b = el('button', activeCat===k ? 'on' : '', `${label} ${counts[k]}`);
    b.onclick = () => { activeCat = k; renderSlots(lastPending); };
    catsEl.append(b);
  }
}

function renderSlots(p){
  slotsEl.textContent = ''; hotkeys = [];
  if(!p) return;
  const avail = (p.options||[]).filter(o => !unavailable(o));
  const list = activeCat === 'all' ? avail : avail.filter(o => catOf(o.kind) === activeCat);
  let i = 0;
  for(const o of list.slice(0, 10)){
    i++;
    const c = slotCard(o, i);
    if(i <= 9) hotkeys[i] = o; else hotkeys[0] = o;
    slotsEl.append(c);
  }
  if(!list.length) slotsEl.append(el('div','empty','这一类现在没有可用动作。'));
}

function renderAllActions(p){
  const body = $('#all-body'); body.textContent = '';
  if(!p){ return; }
  const groups = {}, unavail = [];
  for(const o of (p.options||[])){
    if(unavailable(o)) unavail.push(o);
    else { const g = groupOf(o.kind); (groups[g]=groups[g]||[]).push(o); }
  }
  for(const [g,title] of GROUP_ORDER){
    const arr = groups[g]; if(!arr || !arr.length) continue;
    body.append(el('h4','', `${title}（${arr.length}）`));
    const grid = el('div','g');
    for(const o of arr){ const c = slotCard(o, null); grid.append(c); }
    body.append(grid);
  }
  if(unavail.length){
    const d = el('details','unavail');
    d.append(el('summary','', `暂不可用（${unavail.length}）`));
    const grid = el('div','g');
    for(const o of unavail){ const c = slotCard(o, null); c.disabled = true; grid.append(c); }
    d.append(grid); body.append(d);
  }
}

function pick(o){
  if(o.requires_target){
    const tg = o.targets || [];
    if(!tg.length){ errEl.textContent = '这个选项此刻没有合法目标'; return; }
    tgtEl.textContent = '';
    const sel = el('select'); sel.setAttribute('aria-label','选择目标');
    for(const t of tg){ const op = el('option','',t); op.value=t; sel.append(op); }
    tgtEl.append(el('span','', '目标：'), sel);
    tgtEl._sel = sel;
    selected = {id:o.id, target:tg[0]};
    turnEl.textContent = `已选 [${o.id}] ${o.name} → ${tg[0]}，可以提交`;
  } else {
    tgtEl.textContent = ''; tgtEl._sel = null;
    selected = {id:o.id, target:''};
    turnEl.textContent = `已选 [${o.id}] ${o.name}，可以提交`;
  }
  for(const b of document.querySelectorAll('.slot')) b.classList.remove('sel');
  const card = document.querySelector(`.slot[data-id="${o.id}"]`);
  if(card) card.classList.add('sel');
  submitEl.disabled = false;
  closePanel('all');
}

function pipRow(current, max){
  const box = el('div','pips');
  for(let i=0;i<max;i++){
    const p = el('span', i<current ? 'pip on' : 'pip');
    box.append(p);
  }
  return box;
}

function isDailyItem(it){
  return !!(it && (it.装备类型 === '日常'
    || String(it.类别 || '').startsWith('日常')));
}

function loreText(lore){ return (lore && lore.描述) ? String(lore.描述) : ''; }

function loreButton(kind, name, lore){
  const has = !!(lore && (lore.描述 || lore.机制 || lore.数值 || lore.效果 || lore.背景));
  const b = el('button', 'mini', has ? '改设定' : 'DM 设定');
  b.title = '按世界设定补描述/机制/数值/效果/背景；会写进存档，引擎不自动验算';
  b.onclick = () => openLore(kind, name, lore || {});
  return b;
}

function descBlock(kind, name, own, lore, ruleDesc, engineCoupled){
  const box = el('div', '');
  const dm = loreText(lore);
  const text = dm || own || '';
  if(text){
    const d = el('div', 'loot-desc');
    d.append(el('b', '', dm ? 'DM 补述：' : '设定描述：'),
      document.createTextNode(text));
    box.append(d);
    if(dm && own && dm !== own)
      box.append(el('div', 'loot-src', '原描述：' + own));
  }
  const mech = [lore && lore.机制, lore && lore.数值, lore && lore.效果]
    .filter(Boolean);
  if(mech.length){
    const d = el('div', 'loot-desc');
    d.append(el('b', '', 'DM 设定：'),
      document.createTextNode(mech.join('；')));
    box.append(d);
    box.append(el('div', 'loot-src', engineCoupled
      ? '结构化数值已进引擎结算；这段文字只作说明。'
      : '桌面裁定，引擎不自动验算。'));
  }
  if(lore && lore.背景){
    const d = el('div', 'loot-desc');
    d.append(el('b', '', '背景：'), document.createTextNode(String(lore.背景)));
    box.append(d);
  }
  if(!text && !mech.length && !(lore && lore.背景)){
    box.append(el('div', 'loot-desc',
      'DM 尚未设定——世界书只给框架，缺的描述/数值/机制由 DM 按桌面补。'));
  }
  if(ruleDesc && ruleDesc !== text){
    const det = el('details', 'loot-rule');
    det.append(el('summary', '', '规则原文'));
    det.append(el('div', '', String(ruleDesc)));
    box.append(det);
  }
  return box;
}

function skillCard(name, val, lore){
  const c = el('div', 'skill-card');
  const h = el('div', 'loot-head');
  const left = el('div', '');
  left.append(el('b', '', name));
  h.append(left, el('span', 'loot-kind', '技能'));
  c.append(h);
  if(val) c.append(el('div', 'loot-meta', String(val)));
  c.append(descBlock('技能', name, '', lore, ''));
  const acts = el('div', 'loot-actions');
  acts.append(loreButton('技能', name, lore));
  c.append(acts);
  return c;
}

function cnMeta(s){
  return String(s || '')
    .replace(/bonus action/gi, '附赠动作').replace(/action/gi, '动作')
    .replace(/instantaneous/gi, '瞬间').replace(/Touch/gi, '触及')
    .replace(/(\\d+)\\s*feet/gi, '$1 尺')
    .replace(/(\\d+)\\s*minutes?/gi, '$1 分钟')
    .replace(/(\\d+)\\s*hours?/gi, '$1 小时');
}

function spellCard(name, v){
  const c = el('div', 'loot-card');
  const h = el('div', 'loot-head');
  const left = el('div', '');
  left.append(el('b', '', name));
  h.append(left);
  const lv = Number(v.等级 ?? 0);
  const badges = el('div', '');
  badges.append(el('span', 'loot-kind', lv ? `${lv} 环` : '戏法'));
  if(v.准备中) badges.append(el('span', 'loot-kind', '已准备'));
  h.append(badges);
  c.append(h);
  const meta = [
    v.学派, v.成分 ? '成分 ' + v.成分 : '',
    v.施法时间 ? '施法 ' + cnMeta(v.施法时间) : '',
    v.射程 ? '射程 ' + cnMeta(v.射程) : '',
    v.持续时间 ? '持续 ' + cnMeta(v.持续时间) : '',
    v.专注 ? '专注' : '', v.仪式 ? '仪式' : ''
  ].filter(Boolean);
  if(meta.length) c.append(el('div', 'loot-meta', meta.join('｜')));
  c.append(descBlock('法术', name, '', v.lore, v.规则描述));
  const acts = el('div', 'loot-actions');
  const b = el('button', 'mini', v.准备中 ? '取消准备' : '准备');
  b.disabled = !!(lastMap && lastMap.battle);
  b.title = b.disabled ? '战斗中不能改准备法术' : '';
  b.onclick = () => doAct({kind:'prepare_spell', name});
  acts.append(b, loreButton('法术', name, v.lore));
  c.append(acts);
  return c;
}

function openItemEditor(mode, it){
  itemMode = {mode, name: it ? it.name : ''};
  $('#dm-name').value = it ? (it.name || '') : '';
  $('#dm-name').disabled = mode === 'edit';
  $('#dm-category').value = (it && it.类别) || '武器';
  $('#dm-atk').value = it && it.攻击加值 != null ? it.攻击加值 : '';
  $('#dm-dmg').value = it ? (it.伤害骰 || it.伤害公式 || '') : '';
  $('#dm-dmgtype').value = it ? (it.伤害类型 || '') : '';
  $('#dm-ac').value = it && it.护甲加值 != null ? it.护甲加值 : '';
  $('#dm-actype').value = (it && it.护甲类型) || '';
  $('#dm-type').value = (it && it.装备类型) || '战斗';
  $('#dm-two').checked = !!(it && it.双手);
  const ents = (it && it.词条) || [];
  ['#dm-entry1','#dm-entry2','#dm-entry3'].forEach((s, i) => {
    if($(s)) $(s).value = ents[i] || '';
  });
  const lore = (it && it.lore) || {};
  $('#dm-desc').value = (it && it.描述) || lore.描述 || '';
  $('#dm-mech').value = (it && it.机制) || lore.机制 || lore.数值 || lore.效果 || '';
  $('#dm-bg').value = (it && it.背景) || lore.背景 || '';
  $('#dm-item-hint').textContent = mode === 'edit'
    ? '改的是当局物品数值；改完动作/AC 会按引擎重算。'
    : '世界书只给框架；这里填的数值会进当局引擎，新开一局不保留。';
  openPanel('dm-item');
  setTimeout(() => { const n = $('#dm-name'); if(n) n.focus(); }, 0);
}

function specSummary(a){
  const labels = {attack:'攻击型', save:'豁免型', heal:'治疗型', utility:'描述型'};
  const abCN = {strength:'力量', dexterity:'敏捷', constitution:'体质',
                intelligence:'智力', wisdom:'感知', charisma:'魅力'};
  const dtCN = {slashing:'挥砍', piercing:'穿刺', bludgeoning:'钝击', fire:'火焰',
                cold:'寒冷', lightning:'闪电', acid:'强酸', poison:'毒素',
                radiant:'光耀', necrotic:'死灵', force:'力场', psychic:'心灵',
                thunder:'雷鸣'};
  const bits = [labels[a.类型] || a.类型 || '?'];
  if(a.动作槽 === 'bonus_action') bits.push('附赠动作');
  if(a.攻击加值 != null) bits.push('命中 ' + (a.攻击加值 >= 0 ? '+' : '') + a.攻击加值);
  if(a.伤害骰) bits.push(a.伤害骰 + ' ' + (dtCN[a.伤害类型] || a.伤害类型 || ''));
  if(a.豁免) bits.push((abCN[a.豁免] || a.豁免) + '豁免' + (a.DC ? ' DC ' + a.DC : ''));
  if(a.治疗骰) bits.push('回复 ' + a.治疗骰);
  if(a.范围) bits.push(a.范围 + ' 尺' + (a.范围形状 || ''));
  if(a.次数) bits.push(a.次数 + '/日');
  return bits.join('｜');
}

function openActionEditor(name, a){
  actionMode = {mode: name ? 'edit' : 'new', name: name || ''};
  $('#dma-name').value = name || '';
  $('#dma-name').disabled = !!name;
  $('#dma-type').value = (a && a.类型) || 'attack';
  $('#dma-slot').value = (a && a.动作槽) || 'action';
  $('#dma-atk').value = a && a.攻击加值 != null ? a.攻击加值 : '';
  $('#dma-dmg').value = (a && a.伤害骰) || '';
  $('#dma-dmgtype').value = (a && a.伤害类型) || '';
  $('#dma-ability').value = (a && a.豁免) || '';
  $('#dma-dc').value = a && a.DC != null ? a.DC : '';
  $('#dma-half').checked = !(a && a.半伤 === false);
  $('#dma-area').value = a && a.范围 != null ? a.范围 : '';
  $('#dma-shape').value = (a && a.范围形状) || 'sphere';
  $('#dma-range').value = a && a.射程 != null ? a.射程 : '';
  $('#dma-heal').value = (a && a.治疗骰) || '';
  $('#dma-uses').value = a && a.次数 != null ? a.次数 : '';
  $('#dma-desc').value = (a && a.描述) || '';
  $('#dma-mech').value = ((a && a.lore) || {}).机制 || '';
  $('#dma-hint').textContent = name
    ? '改的是当局技能；保存后立刻生效（战斗中也可以）。'
    : '写进当局引擎；数值写清楚，引擎照常掷骰。新开一局不保留。';
  openPanel('dm-action');
  setTimeout(() => { const n = $('#dma-name'); if(n && !n.disabled) n.focus(); }, 0);
}

function npcSummary(st){
  st = st || {};
  const bits = ['AC ' + (st.护甲等级 ?? '—'), 'HP ' + (st.生命值 ?? '—')];
  if(st.等级) bits.push(st.等级 + ' 级');
  for(const [name, spec] of Object.entries(st.动作 || {}).slice(0, 2)){
    bits.push(name + '：' + specSummary(spec));
  }
  return bits.join('｜');
}

function renderNpcPanel(){
  const body = $('#npc-body'); if(!body) return; body.textContent = '';
  const list = lastNpcs || [];
  if(!list.length) body.append(el('div','empty','还没有登记人物；可以新造一个。'));
  for(const n of list){
    const c = el('div','card');
    const head = el('div','row');
    head.append(el('b','', n.name), el('span','s', n.role || ''));
    c.append(head);
    const st = n.stats || {};
    const stateTxt = n.in_field
      ? `在场（${n.team === 'enemy' ? '敌人' : '盟友'}）｜HP ${n.hp}/${n.hp_max}`
      : '未上场';
    c.append(el('div','s', stateTxt));
    c.append(el('div','s', npcSummary(st)));
    for(const en of (n.entries || [])){
      c.append(el('div','s', `词条：${en.名称}（${en.品级名}）`));
    }
    const acts = el('div','loot-actions');
    const edit = el('button','mini','改数值');
    edit.onclick = () => openNpcEditor(n.name, n);
    acts.append(edit);
    for(const [label, team] of [['作为敌人上场','enemy'],['作为盟友上场','ally']]){
      const b = el('button','mini',label);
      b.disabled = !(lastMap && lastMap.battle) || !!n.in_field;
      b.title = n.in_field ? '已经在场上了' : (b.disabled ? '先进入战斗' : '');
      b.onclick = () => doAct({kind:'dm_add_npc_to_combat', name:n.name, team});
      acts.append(b);
    }
    c.append(acts);
    body.append(c);
  }
  const add = el('button','mini','新增人物');
  add.onclick = () => openNpcEditor('', null);
  body.append(add);
}

const AB_ENCN = {strength:'力量', dexterity:'敏捷', constitution:'体质',
                 intelligence:'智力', wisdom:'感知', charisma:'魅力'};

function openNpcEditor(name, n){
  npcMode = {mode: name ? 'edit' : 'new', name: name || ''};
  const st = (n && n.stats) || {};
  const ab = st.属性 || {};
  $('#dmn-name').value = name || '';
  $('#dmn-name').disabled = !!name;
  $('#dmn-role').value = (n && n.role) || '';
  $('#dmn-level').value = st.等级 ?? 1;
  $('#dmn-ac').value = st.护甲等级 ?? 12;
  $('#dmn-hp').value = st.生命值 ?? 10;
  $('#dmn-init').value = st.先攻加值 != null ? st.先攻加值 : '';
  $('#dmn-speed').value = st.速度 ?? 30;
  $('#dmn-str').value = ab['力量'] ?? 10;
  $('#dmn-dex').value = ab['敏捷'] ?? 10;
  $('#dmn-con').value = ab['体质'] ?? 10;
  $('#dmn-int').value = ab['智力'] ?? 10;
  $('#dmn-wis').value = ab['感知'] ?? 10;
  $('#dmn-cha').value = ab['魅力'] ?? 10;
  const skills = st.技能加值 || {};
  $('#dmn-perc').value = skills['perception'] != null ? skills['perception'] : '';
  $('#dmn-saves').value = ((st.豁免熟练 || []).map(x => AB_ENCN[x] || x)).join(',');
  const atkEntry = Object.entries(st.动作 || {}).find(([, spec]) => spec && spec.类型 === 'attack');
  $('#dmn-atkname').value = atkEntry ? atkEntry[0] : '';
  $('#dmn-atk').value = atkEntry && atkEntry[1].攻击加值 != null ? atkEntry[1].攻击加值 : '';
  $('#dmn-dmg').value = atkEntry ? (atkEntry[1].伤害骰 || '') : '';
  $('#dmn-dmgtype').value = atkEntry ? (atkEntry[1].伤害类型 || '') : '';
  $('#dmn-range').value = atkEntry && atkEntry[1].射程 != null ? atkEntry[1].射程 : '';
  $('#dmn-hint').textContent = name
    ? '改的是当局人物数值；保存后按新数值进下一场战斗（已在场上的单位不重算）。'
    : '给人物写结构化数值后，他才能用自己的 AC / 生命 / 攻击 / 豁免参与引擎结算。';
  openPanel('dm-npc');
  setTimeout(() => { const t = $('#dmn-name'); if(t && !t.disabled) t.focus(); }, 0);
}

const PASSIVE_KINDS = {ac:'AC', attack:'攻击', save:'豁免', speed:'速度', hp:'生命上限'};
const TRIGGER_EFFECTS = {致命伤害:['免死'], 战斗开始:['临时生命'], 回合开始:['回复']};
let lastCatalog = [];

function entryLabel(e){
  return `${e.名称}（${e.品级名 || ''}·${e.适用 || '通用'}${e.自定义 ? '·自定义' : ''}）`;
}

function fillEntryOptions(){
  const sel = ['#dm-entry1','#dm-entry2','#dm-entry3'];
  for(const s of sel){
    const elSel = $(s); if(!elSel) continue;
    const cur = elSel.value;
    elSel.textContent = '';
    elSel.append(new Option('— 无 —', ''));
    for(const e of (lastCatalog || [])){
      if(e.适用 === '生物') continue;   // 生物词条不能装物品
      elSel.append(new Option(entryLabel(e), e.id));
    }
    elSel.value = cur && Array.from(elSel.options).some(o => o.value === cur) ? cur : '';
  }
}

function syncEntryKind(){
  const trig = $('#de-trigger').value;
  const sel = $('#de-kind'); sel.textContent = '';
  if(trig === '常驻'){
    for(const [k, v] of Object.entries(PASSIVE_KINDS)) sel.append(new Option(v, k));
  } else {
    for(const eff of (TRIGGER_EFFECTS[trig] || [])) sel.append(new Option(eff, eff));
  }
}

function renderEntries(){
  const box = $('#entries-list'); if(!box) return;
  box.textContent = '';
  for(const e of (lastCatalog || [])){
    const c = el('div','card');
    const head = el('div','row');
    head.append(el('b','', e.名称), el('span','s', `${e.品级名 || ''}·${e.适用 || '通用'}${e.自定义 ? '·自定义' : ''}`));
    c.append(head);
    const spec = e.触发 === '常驻'
      ? `${PASSIVE_KINDS[e.kind] || e.kind} ${e.数值 >= 0 ? '+' : ''}${e.数值}`
      : `${e.触发} → ${e.效果}｜数值 ${e.数值}｜${e.每场 ? e.每场 + '/场' : '不限次数'}${e.冷却 ? '｜冷却 ' + e.冷却 : ''}`;
    c.append(el('div','s', spec));
    if(e.说明) c.append(el('div','s', e.说明));
    if(e.自定义){
      const b = el('button','mini','编辑');
      b.onclick = () => fillEntryForm(e);
      const acts = el('div','loot-actions');
      acts.append(b);
      c.append(acts);
    }
    box.append(c);
  }
}

function fillEntryForm(e){
  $('#de-id').value = e.id || '';
  $('#de-id').disabled = true;
  $('#de-name').value = e.名称 || '';
  $('#de-tier').value = String(e.品级 || 1);
  $('#de-applies').value = e.适用 || '通用';
  $('#de-trigger').value = e.触发 || '常驻';
  syncEntryKind();
  $('#de-kind').value = e.触发 === '常驻' ? (e.kind || 'ac') : (e.效果 || '回复');
  $('#de-value').value = e.数值 ?? 0;
  $('#de-keep').value = e.保留生命 ?? 1;
  $('#de-per').value = e.每场 ?? 1;
  $('#de-cd').value = e.冷却 ?? 0;
  $('#de-note').value = e.说明 || '';
  $('#de-id').scrollIntoView({block:'nearest'});
}

function openLore(kind, name, lore){
  loreTarget = {kind, name};
  $('#lore-target').textContent = `${kind}：${name}`;
  $('#lore-text').value = (lore && lore.描述) || '';
  $('#lore-mech').value = [lore && lore.机制, lore && lore.数值,
    lore && lore.效果].filter(Boolean).join('；');
  $('#lore-bg').value = (lore && lore.背景) || '';
  openPanel('lore');
  setTimeout(() => { const t = $('#lore-text'); if(t) t.focus(); }, 0);
}

function renderSheet(){
  const body = $('#sheet-body'); body.textContent = '';
  const s = lastSheet; if(!s) return;
  if(sheetTab === 'overview'){
    const g = el('div','grid2');
    const jobs = Object.entries(s.jobs||{}).map(([k,v])=>`${k} ${v}`).join(' / ') || '—';
    const cards = [
      ['职业', jobs, ''],
      ['生命值', `${s.hp}/${s.hp_max}`, s.temp_hp?`临时 ${s.temp_hp}`:''],
      ['护甲等级', String(s.ac ?? '—'), ''],
      ['先攻', String(s.init ?? '—'), ''],
    ];
    for(const [k,v,sub] of cards){
      const c = el('div','card'); c.append(el('h5','',k), el('div','v','' + v));
      if(sub) c.append(el('div','s',sub));
      g.append(c);
    }
    body.append(g);
    const ab = el('div','abil');
    const names = {力量:'力量',敏捷:'敏捷',体质:'体质',智力:'智力',感知:'感知',魅力:'魅力'};
    for(const k of ['力量','敏捷','体质','智力','感知','魅力']){
      const v = (s.abilities||{})[k] ?? 10;
      const d = el('div');
      d.append(el('div','a',k), el('div','n',String(v)),
               el('div','m', (v>=10?'+':'') + Math.floor((v-10)/2)));
      ab.append(d);
    }
    body.append(ab);
    body.append(el('h4','', '状态效果'));
    const chips = el('div','');
    if(!(s.conditions||[]).length) chips.append(el('div','empty','无'));
    for(const c of (s.conditions||[])){
      chips.append(el('span','chip', c.name + (c.rounds>0?`(${c.rounds})`:'')));
    }
    if(s.concentrating) chips.append(el('span','chip','专注：'+s.concentrating));
    body.append(chips);
    body.append(el('h4','', '临时修正（场景 / 事件）'));
    const mods = s.mod_lines || [];
    if(!mods.length) body.append(el('div','empty','无'));
    for(const m of mods) body.append(el('div','s', m));
  } else if(sheetTab === 'status'){
    body.append(el('h4','', '豁免熟练'));
    const saves = el('div','');
    for(const x of (s.saves||[])){
      const r = el('div','row');
      r.append(el('span','l', x), el('span','', '熟练'));
      saves.append(r);
    }
    if(!(s.saves||[]).length) saves.append(el('div','empty','无'));
    body.append(saves);
    body.append(el('h4','', '技能熟练'));
    const skillGrid = el('div','skill-grid');
    for(const [k,v] of Object.entries(s.skills||{})){
      skillGrid.append(skillCard(k, v, (s.skill_lore||{})[k]));
    }
    if(!Object.keys(s.skills||{}).length) body.append(el('div','empty','无'));
    else body.append(skillGrid);
    // DM 技能（H1）：机械字段进引擎，卡面只展示摘要
    body.append(el('h4','', 'DM 技能（当局）'));
    const dmActs = s.dm_actions || [];
    if(!dmActs.length) body.append(el('div','empty','没有当局技能'));
    for(const a of dmActs){
      const c = el('div','card');
      const head = el('div','row');
      head.append(el('b','', a.name), el('span','s', specSummary(a)));
      c.append(head);
      const text = a.描述 || ((a.lore||{}).描述) || '';
      if(text) c.append(el('div','s', text));
      const acts = el('div','loot-actions');
      const b = el('button','mini','改技能');
      b.onclick = () => openActionEditor(a.name, a);
      acts.append(b, loreButton('技能', a.name, a.lore));
      c.append(acts);
      body.append(c);
    }
    const addAct = el('button','mini','新增技能');
    addAct.onclick = () => openActionEditor('', null);
    body.append(addAct);
    body.append(el('h4','', '法术位'));
    for(const [lv, rec] of Object.entries(s.slots||{})){
      const r = el('div','row'); r.append(el('span','l', `${lv} 环 ${rec.current}/${rec.max}`));
      r.append(pipRow(rec.current, rec.max)); body.append(r);
    }
    if(!Object.keys(s.slots||{}).length) body.append(el('div','empty','无'));
    body.append(el('h4','', '资源'));
    for(const [k,v] of Object.entries(s.resources||{})){
      const r = el('div','row');
      r.append(el('span','l',k), el('span','', typeof v === 'object' ? `${v.current}/${v.max}` : String(v)));
      body.append(r);
    }
    if(!Object.keys(s.resources||{}).length) body.append(el('div','empty','无'));
  } else if(sheetTab === 'equip'){
    const tb = s.tier_budget || null;
    if(tb) body.append(el('div','s',
      `词条品级预算：已用 ${tb.used} / 上限 ${tb.max}（等级 ${tb.level}；一件最多 3 条、合计 ≤6）`));
    const slots = ['头盔','上装','下装','靴子','左手','右手','饰品1','饰品2'];
    const eq = s.equipment || {};
    const grid = el('div','equip-grid');
    for(const sn of slots){
      const name = eq[sn];
      const c = el('div','equip-slot' + (name ? ' filled' : '') +
        (sn === '左手' || sn === '右手' ? ' hand' : ''));
      c.append(el('div','sn',sn));
      if(name){
        c.append(el('span','si',name));
        const b = el('button','mini','卸下');
        b.onclick = () => doAct({kind:'equip_item', name});
        c.append(b);
      } else c.append(el('div','empty','空'));
      grid.append(c);
    }
    body.append(grid);
    const daily = eq['日常'] || [];
    body.append(el('h4','', `日常穿着（${daily.length}，不占战斗栏）`));
    if(!daily.length) body.append(el('div','empty','没有日常服装/饰品'));
    for(const nm of daily){
      const r = el('div','row');
      r.append(el('span','l',nm));
      const b = el('button','mini','卸下');
      b.onclick = () => doAct({kind:'equip_item', name:nm});
      r.append(b);
      body.append(r);
    }
  } else if(sheetTab === 'spells'){
    const sb = s.spellbook || {};
    if(!Object.keys(sb).length) body.append(el('div','empty','法术书为空'));
    for(const [name, v] of Object.entries(sb)){
      body.append(spellCard(name, v));
    }
  }
}

function itemActions(it){
  const out = [];
  const cat = String(it.类别 || '');
  if(/武器|护甲|盾牌/.test(cat)){
    const b = el('button','mini', it.已装备 ? '卸下' : '装备');
    b.disabled = !!(lastMap && lastMap.battle);
    b.title = b.disabled ? '战斗中不能换装备' : '会重算 AC（武器在下一场战斗生效）';
    b.onclick = () => doAct({kind:'equip_item', name:it.name});
    out.push(b);
  }
  if(/消耗品/.test(cat)){
    const b = el('button','mini','使用');
    b.disabled = !!(lastMap && lastMap.battle);
    b.title = b.disabled ? '战斗中喝药要走附赠动作（还没接进动作菜单）' : '喝下（按 SRD 掷骰回血）';
    b.onclick = () => doAct({kind:'use_item', name:it.name});
    out.push(b);
  }
  if(lastShop && (lastShop.items||[]).length && !it.已装备 && it.价格){
    const hp = Math.floor(Number(it.价格) / 2);
    const b = el('button','mini', `卖 ${hp}`);
    b.title = '半价卖给这里的商人';
    b.onclick = () => doAct({kind:'sell_item', name:it.name, qty:1});
    out.push(b);
  }
  return out;
}

function itemRow(it){
  const card = el('div', 'loot-card' + (it.已装备 ? ' equipped' : ''));
  const head = el('div', 'loot-head');
  const left = el('div', '');
  const b = el('b', '', it.name || '');
  if(it.已装备) b.append(el('span','eq','装备'));
  left.append(b);
  head.append(left, el('span', 'loot-kind', it.类别 || '物品'));
  card.append(head);
  const ents = it.词条信息 || [];
  if(ents.length){
    const row = el('div','loot-actions');
    for(const e of ents) row.append(el('span','chip', `${e.名称}·${e.品级名}`));
    card.append(row);
  }
  const meta = [
    it.攻击加值 != null ? `攻击 ${it.攻击加值 >= 0 ? '+' : ''}${it.攻击加值}` : '',
    it.伤害骰 ? `伤害 ${it.伤害骰}` : (it.伤害公式 ? `伤害 ${it.伤害公式}` : ''),
    it.伤害类型,
    it.护甲加值 != null ? `AC ${it.护甲加值}` : '',
    it.护甲类型,
    it.数量 && Number(it.数量) > 1 ? `×${it.数量}` : '',
    it.价格 ? `${it.价格} 金币` : ''
  ].filter(Boolean);
  if(meta.length) card.append(el('div', 'loot-meta', meta.join('｜')));
  card.append(descBlock('物品', it.name, it.描述, it.lore, '',
    it.攻击加值 != null || !!it.伤害骰 || it.护甲加值 != null));
  const acts = el('div', 'loot-actions');
  acts.append(loreButton('物品', it.name,
    it.lore || (it.描述 ? {描述: it.描述} : {})));
  const eb = el('button', 'mini', '改数值');
  eb.title = 'DM 改攻击/伤害/AC；会立即按引擎重算';
  eb.onclick = () => openItemEditor('edit', it);
  acts.append(eb);
  for(const act of itemActions(it)) acts.append(act);
  card.append(acts);
  return card;
}

function renderInventory(){
  const body = $('#inv-body'); body.textContent = '';
  const s = lastSheet; if(!s) return;
  const mk = el('button', 'mini', 'DM 造物');
  mk.title = '世界书没有的东西，由 DM 按当局写数值；新开一局不保留';
  mk.onclick = () => openItemEditor('new', null);
  body.append(mk);
  const coins = s.coins || {};
  body.append(el('h4','', `钱袋（${coins['金币']||0} 金币 / ${coins['银币']||0} 银 / ${coins['铜币']||0} 铜）`));
  const allItems = s.items || [];
  const daily = allItems.filter(x => x.已装备 && isDailyItem(x));
  const eq = allItems.filter(x => x.已装备 && !isDailyItem(x));
  const other = allItems.filter(x => !x.已装备);
  body.append(el('h4','', `战斗装备（${eq.length}）`));
  if(!eq.length) body.append(el('div','empty','未记录已装备物品'));
  for(const it of eq) body.append(itemRow(it));
  body.append(el('h4','', `日常穿着（${daily.length}，不占战斗栏）`));
  if(!daily.length) body.append(el('div','empty','没有日常服装/饰品'));
  for(const it of daily) body.append(itemRow(it));
  body.append(el('h4','', `携带（${other.length}）`));
  if(!other.length) body.append(el('div','empty','背包空空如也'));
  for(const it of other) body.append(itemRow(it));
  if(lastShop && (lastShop.items||[]).length){
    body.append(el('h4','', `补给（${lastShop.place}）`));
    for(const it of lastShop.items){
      const r = el('div','item');
      const l = el('div','');
      l.append(el('b','', it.label || it.name));
      l.append(el('small','', it.price != null ? `${it.price} 金币` : '价格需 DM 定'));
      r.append(l);
      const b = el('button','mini','买');
      b.disabled = it.price == null;
      b.title = b.disabled ? 'SRD 没给价格，需 DM 报价' : '买一个';
      b.onclick = () => doAct({kind:'buy_item', name:it.name, qty:1});
      r.append(b);
      body.append(r);
    }
  } else {
    body.append(el('p','empty','这里没有可交易的地方（走到有货架的地点才能买）。'));
  }
}

let createOpts = null;
let createLifeSummary = '';

async function actJson(payload){
  const r = await fetch('/api/act',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  return await r.json();
}

function rowOf(label, node){
  const r = el('div','row');
  r.append(el('span','l',label));
  r.append(node);
  return r;
}

async function loadCreateOptions(){
  const body = $('#create-body'); if(!body) return;
  body.textContent = '正在读取 SRD 选项…';
  try{
    const d = await actJson({kind:'create_options'});
    if(!d.ok){ body.textContent = d.error || '读取失败'; return; }
    createOpts = d.value;
    renderCreateForm();
  }catch(e){ body.textContent = '网络错误：' + e; }
}

function renderCreateForm(){
  const body = $('#create-body'); body.textContent = '';
  const o = createOpts; if(!o) return;
  const name = el('input'); name.id='cName'; name.setAttribute('aria-label','角色名'); name.type='text'; name.value='布兰'; name.maxLength=20;
  const cls = el('select'); cls.id='cClass'; cls.setAttribute('aria-label','职业');
  for(const c of o.classes){ const op=el('option','',`${c.cn}（${c.en}）`); op.value=c.en; cls.append(op); }
  const sp = el('select'); sp.id='cSpecies'; sp.setAttribute('aria-label','种族');
  for(const s of o.species){ const op=el('option','',`${s.cn}（${s.en}）`); op.value=s.en; sp.append(op); }
  const lv = el('select'); lv.id='cLevel'; lv.setAttribute('aria-label','等级');
  for(let i=1;i<=5;i++){ const op=el('option','',String(i)); op.value=String(i); lv.append(op); }
  lv.value='3';
  const mode = el('select'); mode.id='cMode'; mode.setAttribute('aria-label','属性生成方式');
  const m1=el('option','','推荐标准数组'); m1.value='suggest'; mode.append(m1);
  const m2=el('option','','掷 4d6 取高 3'); m2.value='roll'; mode.append(m2);
  const seed = el('input'); seed.id='cSeed'; seed.setAttribute('aria-label','随机种子'); seed.type='number'; seed.value='7'; seed.min='1';
  const hint = el('div','hint'); hint.id='cHint';
  const skills = el('div','clist'); skills.id='cSkills';
  for(const sk of o.skills){
    const lab=el('label','');
    const cb=el('input'); cb.type='checkbox'; cb.value=sk.en;
    lab.append(cb, el('span','', sk.cn));
    skills.append(lab);
  }
  const spells = el('div','clist'); spells.id='cSpells';
  const err = el('div','hint'); err.id='create-err';
  const preview = el('div'); preview.id='create-preview';
  const btnPrev = el('button','mini','预览');
  const btnStart = el('button','mini','开始这一局');
  const btns = el('div','row'); btns.append(btnPrev, btnStart);

  const role = el('select'); role.id='cRole'; role.setAttribute('aria-label','卡用途');
  const r1=el('option','','真人卡'); r1.value='human'; role.append(r1);
  const r2=el('option','','同伴卡'); r2.value='companion'; role.append(r2);
  const libSel = el('select'); libSel.id='cLib'; libSel.setAttribute('aria-label','角色卡库');
  const libLoad = el('button','mini','读入');
  const libSave = el('button','mini','存入卡库');
  const libDel = el('button','mini','删除');
  const libRow = el('div','row');
  libRow.append(libSel, libLoad, libSave, libDel);
  const lifeBtn = el('button','mini','问答生成');
  const lifeBox = el('div'); lifeBox.id='cLife'; lifeBox.style.display='none';

  body.append(el('div','hint','角色卡库（存的是建卡选择，数值仍由服务器重算）'));
  body.append(libRow, rowOf('卡用途', role), lifeBtn, lifeBox);
  body.append(rowOf('名字', name), rowOf('职业', cls), rowOf('种族', sp),
              rowOf('等级', lv), rowOf('属性', mode), rowOf('种子', seed));
  body.append(el('div','hint','技能熟练（数量由职业决定；服务器会复核）'));
  body.append(skills, hint);
  body.append(el('div','hint','法术（只有施法职业能选；等级不超过最高环阶）'));
  body.append(spells);
  body.append(btns, err, preview);

  function refresh(){
    const info = o.classes.find(c => c.en === cls.value) || {};
    hint.textContent = `这个职业要选 ${info.skill_choices} 项技能熟练；生命骰 d${info.hit_die}。`;
    spells.textContent='';
    const list = (o.spells && o.spells[cls.value]) || [];
    if(!list.length){ spells.append(el('div','hint','这个职业没有法术表。')); return; }
    for(const s of list){
      const lab=el('label','');
      const cb=el('input'); cb.type='checkbox'; cb.value=s.name;
      lab.append(cb, el('span','', `${s.name}（${s.level} 环）`));
      spells.append(lab);
    }
  }
  cls.onchange = refresh;
  refresh();
  refreshLibrary();

  let life = null;
  function applyChoices(ch){
    if(!ch) return;
    name.value = ch.name || name.value;
    if(ch.klass) cls.value = ch.klass;
    if(ch.species) sp.value = ch.species;
    if(ch.level) lv.value = String(ch.level);
    if(ch.ability_mode) mode.value = ch.ability_mode;
    if(ch.seed) seed.value = String(ch.seed);
    refresh();
    for(const x of document.querySelectorAll('#cSkills input')) x.checked = false;
    for(const sk of (ch.skills || [])){
      const cb = document.querySelector(`#cSkills input[value="${sk}"]`);
      if(cb) cb.checked = true;
    }
    for(const x of document.querySelectorAll('#cSpells input')) x.checked = false;
    for(const spn of (ch.spells || [])){
      const cb = document.querySelector(`#cSpells input[value="${spn}"]`);
      if(cb) cb.checked = true;
    }
  }
  async function refreshLibrary(selectId){
    try{
      const d = await actJson({kind:'list_characters'});
      if(!d.ok) return;
      libSel.textContent = '';
      if(!(d.value || []).length){
        const op = el('option','','（卡库为空）'); op.value=''; libSel.append(op); return;
      }
      for(const c of d.value){
        const op = el('option','',`${c.name}｜${c.klass} ${c.level}｜${c.species}`);
        op.value = c.id; libSel.append(op);
      }
      if(selectId) libSel.value = selectId;
    }catch(e){ /* 卡库读失败不挡建卡 */ }
  }
  libLoad.onclick = async () => {
    if(!libSel.value) return;
    const d = await actJson({kind:'load_character', id:libSel.value});
    if(!d.ok){ err.textContent = d.error || '读卡失败'; return; }
    applyChoices(d.value.choices || {});
    if(d.value.role) role.value = d.value.role;
    createLifeSummary = d.value.lifepath || '';
    err.textContent = '已读入：' + (d.value.name || '');
  };
  libSave.onclick = async () => {
    const d = await actJson(Object.assign(createPayload(), {kind:'save_character'}));
    if(!d.ok){ err.textContent = d.error || '存卡失败'; return; }
    await refreshLibrary(d.value && d.value.id);
    err.textContent = d.message || '已存入卡库';
  };
  libDel.onclick = async () => {
    if(!libSel.value) return;
    const d = await actJson({kind:'delete_character', id:libSel.value});
    if(!d.ok){ err.textContent = d.error || '删除失败'; return; }
    await refreshLibrary();
    err.textContent = d.message || '已删除';
  };

  function renderLife(){
    lifeBox.textContent = '';
    if(!life || !o.lifepath || !o.lifepath.length){ lifeBox.style.display='none'; return; }
    lifeBox.style.display = 'block';
    if(life.i >= o.lifepath.length){
      lifeBox.append(el('div','lq','八问已完成；正在生成角色卡…'));
      return;
    }
    const q = o.lifepath[life.i];
    lifeBox.append(el('div','lbar',`第 ${life.i + 1} / ${o.lifepath.length} 问`));
    lifeBox.append(el('div','lq',q.q));
    for(const opt of q.options){
      const b = el('button','lopt',opt.label);
      b.type='button';
      b.onclick = () => {
        life.answers[q.id] = opt.id;
        life.i += 1;
        if(life.i >= o.lifepath.length) finishLife();
        else renderLife();
      };
      lifeBox.append(b);
    }
    const cancel = el('button','mini','取消'); cancel.type='button';
    cancel.onclick = () => { life = null; lifeBox.style.display='none'; };
    lifeBox.append(cancel);
  }
  async function finishLife(){
    const d = await actJson({kind:'lifepath_build', answers:life.answers, name:(name.value||'').trim()});
    if(!d.ok){ err.textContent = d.error || '生成失败'; lifeBox.style.display='none'; return; }
    applyChoices(d.value.payload || {});
    createLifeSummary = d.value.summary || '';
    life = null; lifeBox.style.display='none';
    err.textContent = '问答生成完毕；可继续改，再预览/开局';
  }
  lifeBtn.onclick = () => {
    if(!o.lifepath || !o.lifepath.length) return;
    life = {i:0, answers:{}};
    renderLife();
    lifeBox.scrollIntoView({block:'nearest'});
  };

  btnPrev.onclick = async () => {
    err.textContent='';
    try{
      const d = await actJson(Object.assign(createPayload(), {kind:'create_preview'}));
      if(!d.ok){ err.textContent = d.error || '预览失败'; return; }
      const v = d.value;
      preview.textContent = `${v.name}｜${v.klass} ${v.level} 级｜${v.species}
HP ${v.hp}｜AC ${v.ac}｜豁免 ${(v.saves||[]).join('、')}
技能 ${Object.keys(v.skills||{}).join('、') || '—'}
法术位 ${Object.entries(v.slots||{}).map(([k,x])=>k+'环 '+x['最大']).join('／') || '—'}
法术 ${(v.spells||[]).join('、') || '—'}`;
    }catch(e){ err.textContent='网络错误：'+e; }
  };
  btnStart.onclick = async () => {
    err.textContent='';
    try{
      const d = await actJson(Object.assign(createPayload(), {kind:'create_start'}));
      if(!d.ok){ err.textContent = d.error || '开局失败'; return; }
      closePanel('create'); renderedSeq = 0;
    }catch(e){ err.textContent='网络错误：'+e; }
  };
}

function createPayload(){
  const pick = sel => [...document.querySelectorAll(`${sel} input:checked`)].map(x=>x.value);
  return {name: ($('#cName').value || '').trim(), klass: $('#cClass').value,
          species: $('#cSpecies').value, level: parseInt($('#cLevel').value,10),
          ability_mode: $('#cMode').value, seed: parseInt($('#cSeed').value||'1',10),
          skills: pick('#cSkills'), spells: pick('#cSpells'),
          role: ($('#cRole') ? $('#cRole').value : 'human'),
          lifepath: createLifeSummary};
}

//: 自检：浏览器能力 + 服务端事实（只读、无密钥）。对应狼人杀那个「这台手机支持什么」。
function diagRows(){
  const rows = [];
  const add = (k, v, ok) => rows.push({k, v: String(v == null ? '' : v), ok});
  add('浏览器标识（UA）', navigator.userAgent);
  add('安全上下文', window.isSecureContext ? '是（https 或 localhost）' : '否', !!window.isSecureContext);
  add('屏幕 / 视口', `${screen.width}×${screen.height}　${innerWidth}×${innerHeight}　${devicePixelRatio}x`);
  add('触摸', (navigator.maxTouchPoints || 0) > 0 ? '支持' : '不支持（鼠标）');
  add('时区', Intl.DateTimeFormat().resolvedOptions().timeZone || '未知');
  return rows;
}
function renderDiagRow(r){
  const color = r.ok === false ? '#ff8a8a' : (r.ok === true ? '#8fe08f' : '#c9d1e0');
  return `<div class="row"><span class="l">${esc(r.k)}</span>`
       + `<span style="color:${color};overflow-wrap:anywhere">${esc(r.v)}</span></div>`;
}
async function renderDiag(){
  const box = $('#diagRows');
  if(!box) return;
  box.innerHTML = diagRows().map(renderDiagRow).join('')
                + '<div class="row"><span class="l">服务端</span><span>读取中…</span></div>';
  let data = {};
  try{
    const r = await fetch('/api/diag');
    data = await r.json();
    if(!data.ok) throw new Error(data.error || ('HTTP ' + r.status));
  }catch(e){
    box.innerHTML = diagRows().map(renderDiagRow).join('')
      + `<div class="row"><span class="l">服务端</span><span style="color:#ff8a8a">`
      + `取不到：${esc(e && e.message || e)}</span></div>`;
    return;
  }
  const srv = [];
  const add = (k, v, ok) => srv.push({k, v, ok});
  add('服务版本', data.version || '—');
  add('Python', data.python || '—');
  add('存档目录', data.save_dir || '—');
  add('存档目录可写', data.save_dir_writable ? '是' : '否（自检写入失败）',
      !!data.save_dir_writable);
  add('模型', data.real ? `真模型：${data.model || '—'}` : '假模型（0 花费）',
      data.real ? null : true);
  if(data.real){
    add('API key', data.key_present ? '已配置（不显示内容）' : '没有配置',
        !!data.key_present);
  }else{
    add('API key', '不需要（假模型）', true);
  }
  add('真调用闸门', data.no_real_calls ? '开着（DND_NO_REAL_CALLS=1）' : '关着',
      !data.no_real_calls);
  if(data.max_calls) add('调用上限', `${data.calls_used || 0} / ${data.max_calls}`);
  add('剧本', `${data.adventure || '—'}（${data.scenes_total || 0} 场）`);
  add('当前进度', `第 ${data.round || 0} 轮 · 场 ${data.scene || '—'} · ${data.status || '—'}`);
  add('调用记账', `${data.calls || 0} 次 / 入 ${data.prompt_tokens || 0} / 出 ${data.completion_tokens || 0}`);
  box.innerHTML = diagRows().map(renderDiagRow).join('')
                + '<div class="row"><span class="l"><b>服务端</b></span><span></span></div>'
                + srv.map(renderDiagRow).join('');
  window.__diagText = box.innerText;
}
$('#diagRefresh') && ($('#diagRefresh').onclick = () => renderDiag());
$('#diagCopy') && ($('#diagCopy').onclick = async () => {
  try{ await navigator.clipboard.writeText(window.__diagText || ''); }
  catch(e){ /* 剪贴板被拒就算了 */ }
});

//: 复盘：一级列表（lite，30KB 级）+ 点开单条取全文（含思考链，剧透）。
async function rvJson(url){
  const r = await fetch(url);
  const d = await r.json();
  if(!d.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}
async function rvLoadGame(name){
  const box = $('#rvList');
  box.textContent = '读取中…';
  try{
    const d = await rvJson('/api/game?name=' + encodeURIComponent(name) + '&lite=1');
    box.innerHTML = '';
    if(!d.calls.length){ box.textContent = '这份存档没有调用记录。'; return; }
    d.calls.forEach((c, i) => {
      const b = document.createElement('button');
      b.className = 'rvrow';
      b.type = 'button';
      const len = Number(c.reasoning_len || 0);
      b.innerHTML = `<b>${esc(c.actor || c.role || '?')}</b>`
        + `<span class="meta">${esc(c.task || '')}</span>`
        + `<span class="meta">${esc((c.elapsed || 0) + 's')}</span>`
        + `<span class="meta">入 ${esc(c.prompt_tokens)} / 出 ${esc(c.completion_tokens)}</span>`
        + (c.ok === false ? `<span class="meta" style="color:#ff8a8a">失败</span>` : '')
        + `<span class="meta">思维链 ${len ? len + ' 字（点开看全文）' : '无'}</span>`;
      b.onclick = () => rvShowCall(name, i);
      box.appendChild(b);
    });
  }catch(e){
    box.textContent = '读不到：' + (e && e.message || e);
  }
}
async function rvShowCall(name, i){
  const box = $('#rvDetail');
  box.textContent = '读取中…';
  try{
    const d = await rvJson('/api/game/call/' + i + '?name=' + encodeURIComponent(name));
    const c = d.call;
    box.innerHTML = `<div class="row"><span class="l">第 ${i + 1} 条 · ${esc(c.actor || c.role)}</span>`
      + `<span>${esc(c.task || '')} · ${esc(c.elapsed || 0)}s · 入 ${esc(c.prompt_tokens)} / 出 ${esc(c.completion_tokens)}</span></div>`
      + `<div class="row"><span class="l">思考链</span><span>${c.reasoning ? esc(String(c.reasoning.length)) + ' 字' : '没有'}</span></div>`
      + (c.reasoning ? `<pre>${esc(c.reasoning)}</pre>`
                     : '<p class="empty">这次调用没有返回思考链。</p>')
      + (c.prompt ? `<details><summary>它当时看到的上下文（${esc(String(c.prompt.length))} 字）</summary><pre>${esc(c.prompt)}</pre></details>` : '');
  }catch(e){
    box.textContent = '读不到：' + (e && e.message || e);
  }
}
async function renderReview(){
  const sel = $('#rvGame');
  if(!sel) return;
  try{
    const d = await fetch('/api/games').then(r => r.json());
    const games = (d && d.games) || [];
    sel.innerHTML = '';
    if(!games.length){ $('#rvList').textContent = '这个目录里没有存档。'; return; }
    games.forEach(g => {
      const o = document.createElement('option');
      o.value = g.name;
      o.textContent = `${g.name}（${g.calls} 次调用${g.has_calls ? '，含思考链' : '，只有元数据'}）`;
      sel.appendChild(o);
    });
    sel.onchange = () => { $('#rvDetail').textContent = ''; rvLoadGame(sel.value); };
    $('#rvDetail').textContent = '';
    rvLoadGame(sel.value);
  }catch(e){
    $('#rvList').textContent = '读不到存档列表：' + (e && e.message || e);
  }
}

let lastFocus = null;
function openPanel(name){
  lastFocus = document.activeElement;
  for(const x of document.querySelectorAll('.overlay')) x.classList.remove('open');
  for(const b of document.querySelectorAll('.tool')) b.setAttribute('aria-expanded','false');
  const panel = document.querySelector(`.overlay[data-panel="${name}"]`);
  if(!panel) return;
  panel.classList.add('open');
  const tb = document.querySelector(`.tool[data-panel="${name}"]`);
  if(tb) tb.setAttribute('aria-expanded','true');
  if(name === 'sheet') renderSheet();
  if(name === 'inventory') renderInventory();
  if(name === 'all') renderAllActions(lastPending);
  if(name === 'map'){ renderMapPanel(); }
  if(name === 'quests'){ renderQuests(); }
  if(name === 'zero'){ renderZero(); }
  if(name === 'npc'){ renderNpcPanel(); }
  if(name === 'entries'){ syncEntryKind(); renderEntries(); }
  if(name === 'diag'){ renderDiag(); }
  if(name === 'review'){ renderReview(); }
  if(name === 'create'){ loadCreateOptions(); }
  const first = panel.querySelector('.modal .tabs button, .modal .body button, ' +
    '.modal .body input, .modal .body textarea, .modal .body select, .modal .x');
  if(first) first.focus();
}
function closeAny(){
  const panel = document.querySelector('.overlay.open');
  if(panel) panel.classList.remove('open');
  for(const b of document.querySelectorAll('.tool')) b.setAttribute('aria-expanded','false');
  if(lastFocus && document.contains(lastFocus) && lastFocus.focus) lastFocus.focus();
}
function closePanel(name){
  const panel = document.querySelector(`.overlay[data-panel="${name}"]`);
  if(panel && panel.classList.contains('open')) closeAny();
}
for(const b of document.querySelectorAll('.tool')) b.onclick = () => openPanel(b.dataset.panel);
for(const b of document.querySelectorAll('[data-close]')) b.onclick = () => closeAny();
for(const o of document.querySelectorAll('.overlay')) o.onclick = e => { if(e.target === o) closeAny(); };
document.addEventListener('keydown', e => {
  if(e.key === 'Escape'){ closeAny(); return; }
  if(e.key === 'Tab'){
    const open = document.querySelector('.overlay.open'); if(!open) return;
    const f = [...open.querySelectorAll('button, [href], input, select, textarea')]
      .filter(x => !x.disabled && x.offsetParent !== null);
    if(!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
  }
  if($('#session0') && $('#session0').classList.contains('open') &&
     e.key !== 'Tab') return;
  if(e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); submitEl.click(); return; }
  const n = parseInt(e.key,10);
  if(!isNaN(n) && n>=0 && n<=9 && hotkeys[n]){ e.preventDefault(); pick(hotkeys[n]); }
});
for(const b of document.querySelectorAll('#sheet-tabs button')){
  b.onclick = () => {
    sheetTab = b.dataset.stab;
    for(const x of document.querySelectorAll('#sheet-tabs button')) x.classList.toggle('on', x === b);
    renderSheet();
  };
}
function renderTravel(){
  const box = $('#travel'); if(!box) return;
  box.textContent = '';
  if(!lastGraph || !lastGraph.current) return;
  const states = {};
  for(const n of (lastGraph.nodes||[])) states[n.name] = n.state;
  const adj = [];
  for(const e of (lastGraph.edges||[])){
    if(e.a === lastGraph.current) adj.push(e.b);
    else if(e.b === lastGraph.current) adj.push(e.a);
  }
  const battle = !!(lastMap && lastMap.battle);
  if(!adj.length){ box.append(el('div','empty','这里没有已知的出口。')); return; }
  box.append(el('div','hint','可前往（沿已知通道）：'));
  for(const name of adj){
    const b = el('button','mini', name + (states[name] === 'heard' ? '（只听说过）' : ''));
    b.disabled = battle;
    b.title = battle ? '战斗中不能赶路' : '沿已知的路走过去';
    b.onclick = () => doAct({kind:'travel', to:name});
    box.append(b);
  }
}

function renderMapPanel(){
  const battleTab = document.querySelector('#map-tabs button[data-mtab="battle"]');
  const sceneTab = document.querySelector('#map-tabs button[data-mtab="scene"]');
  const onBattle = battleTab.classList.contains('on');
  renderInitiative(lastMap, lastPending);
  $('#battlemap').style.display = onBattle ? 'block' : 'none';
  $('#worldgraph').style.display = onBattle ? 'none' : 'block';
  $('#travel').style.display = onBattle ? 'none' : 'block';
  $('#scenetext').style.display = onBattle ? 'none' : 'block';
  if(onBattle) drawMap($('#battlemap'), lastMap, lastPending);
  else {
    drawGraph($('#worldgraph'), lastGraph);
    renderTravel();
    const st = (lastScene && lastScene.story) || {};
    const done = (st.done || []).length;
    const head = st.adventure
      ? `《${st.adventure}》 进度 ${done}/${st.total || '?'} 场`
        + (st.status === 'done' ? '（已跑完）' : '')
        + `\n当前：${st.act || '—'} / ${st.scene || '—'}\n\n`
      : '';
    $('#scenetext').textContent = head + ((lastScene && lastScene.text) || '（没有场景信息）');
  }
}
for(const b of document.querySelectorAll('#map-tabs button')){
  b.onclick = () => { for(const x of document.querySelectorAll('#map-tabs button')) x.classList.toggle('on', x === b); renderMapPanel(); };
}
$('#allActions').onclick = () => openPanel('all');
$('#saveGame').onclick = () => doAct({kind:'save_game'});
    for(const n of [1,2,3]){
      const sb = $('#saveSlot' + n), lb = $('#loadSlot' + n);
      if(sb) sb.onclick = () => doAct({kind:'save_game', slot:n});
      if(lb) lb.onclick = () => { renderedSeq = 0; doAct({kind:'load_game', slot:n}); };
    }
    function renderSaveSlots(rows){
      const box = $('#save-slots'); if(!box) return;
      if(!rows || !rows.length){ box.textContent = '—'; return; }
      box.textContent = rows.map(r =>
        r.exists ? `${r.slot}·有档` : `${r.slot}·空`).join(' / ');
    }
    async function actRaw(payload){
      const r = await fetch('/api/act', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
      return await r.json();
    }
    $('#exportSave').onclick = async () => {
      try{
        const j = await actRaw({kind:'export_save'});
        if(!j.ok){ $('#error').textContent = j.error || '导出失败'; return; }
        const blob = new Blob([JSON.stringify(j.value.data, null, 2)],
                              {type:'application/json'});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob); a.download = j.value.filename;
        a.click(); URL.revokeObjectURL(a.href);
      }catch(e){ $('#error').textContent = '导出失败：' + e; }
    };
    $('#importSave').onclick = () => $('#importFile').click();
    $('#importFile').onchange = async ev => {
      const f = ev.target.files && ev.target.files[0]; if(!f) return;
      try{
        const raw = JSON.parse(await f.text());
        const j = await actRaw({kind:'import_save', data: raw});
        if(!j.ok){ $('#error').textContent = j.error || '导入失败'; return; }
        renderedSeq = 0; await refreshState();
      }catch(e){ $('#error').textContent = '导入失败：' + e; }
      ev.target.value = '';
    };
$('#loadGame').onclick = () => { renderedSeq = 0; doAct({kind:'load_game'}); };
$('#loreSave').onclick = async () => {
  if(!loreTarget) return;
  const fields = {
    描述: ($('#lore-text').value || '').trim(),
    机制: ($('#lore-mech').value || '').trim(),
    背景: ($('#lore-bg').value || '').trim()
  };
  try{
    const r = await fetch('/api/act', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind:'set_lore', lore_kind:loreTarget.kind,
                            name:loreTarget.name, fields})});
    const d = await r.json();
    if(!d.ok){ errEl.textContent = d.error || '保存失败'; return; }
    errEl.textContent = d.message || '已记下 DM 设定';
    closeAny(); await refreshState();
  }catch(e){ errEl.textContent = '网络错误：' + e; }
};
$('#loreClear').onclick = async () => {
  if(!loreTarget) return;
  try{
    const r = await fetch('/api/act', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({kind:'set_lore', lore_kind:loreTarget.kind,
                            name:loreTarget.name, fields:{}})});
    const d = await r.json();
    if(!d.ok){ errEl.textContent = d.error || '清除失败'; return; }
    errEl.textContent = d.message || '已清除 DM 设定';
    closeAny(); await refreshState();
  }catch(e){ errEl.textContent = '网络错误：' + e; }
};
$('#dma-write').onclick = async () => {
  const name = (actionMode.mode === 'edit' ? actionMode.name
                : ($('#dma-name').value || '').trim());
  if(!name){ errEl.textContent = '技能要有名字'; return; }
  const num = v => (v == null ? '' : String(v).trim());
  const typ = $('#dma-type').value;
  const body = {
    kind: actionMode.mode === 'edit' ? 'set_action_stats' : 'dm_create_action',
    name, 类型: typ, 动作槽: $('#dma-slot').value, 次数: num($('#dma-uses').value),
    描述: num($('#dma-desc').value), 机制: num($('#dma-mech').value),
    攻击加值: num($('#dma-atk').value), 伤害骰: num($('#dma-dmg').value),
    伤害类型: num($('#dma-dmgtype').value), 豁免: $('#dma-ability').value,
    DC: num($('#dma-dc').value), 范围: num($('#dma-area').value),
    范围形状: $('#dma-shape').value, 射程: num($('#dma-range').value),
    治疗骰: num($('#dma-heal').value)
  };
  if(typ === 'save') body.半伤 = $('#dma-half').checked;
  // 只发本类型用得上的字段：别的类型专属字段填了就报错，空着不发
  const keep = {attack:['攻击加值','伤害骰','伤害类型','射程','次数'],
                save:['豁免','DC','半伤','范围','范围形状','伤害骰','伤害类型','射程','次数'],
                heal:['治疗骰','射程','次数'], utility:['次数']}[typ] || [];
  for(const k of ['攻击加值','伤害骰','伤害类型','豁免','DC','半伤','范围',
                  '范围形状','射程','治疗骰','次数']){
    if(!keep.includes(k)) delete body[k];
  }
  try{
    const d = await (await fetch('/api/act', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)})).json();
    if(!d.ok){ errEl.textContent = d.error || '写入失败'; return; }
    errEl.textContent = d.message || '已写入当局';
    closeAny(); await refreshState();
  }catch(e){ errEl.textContent = '网络错误：' + e; }
};

$('#dm-open-entries').onclick = () => openPanel('entries');
$('#de-trigger').onchange = syncEntryKind;
$('#de-save').onclick = async () => {
  const id = ($('#de-id').value || '').trim();
  if(!id){ errEl.textContent = '词条要有 id（小写字母/数字/_）'; return; }
  const name = ($('#de-name').value || '').trim();
  if(!name){ errEl.textContent = '词条要有名称'; return; }
  const trig = $('#de-trigger').value;
  const body = {
    kind: 'dm_create_entry', id, 名称: name,
    品级: $('#de-tier').value, 适用: $('#de-applies').value, 触发: trig,
    数值: $('#de-value').value, 保留生命: $('#de-keep').value,
    每场: $('#de-per').value, 冷却: $('#de-cd').value,
    说明: ($('#de-note').value || '').trim()
  };
  if(trig === '常驻') body['属性'] = $('#de-kind').value;
  else body['效果'] = $('#de-kind').value;
  try{
    const d = await (await fetch('/api/act', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)})).json();
    if(!d.ok){ errEl.textContent = d.error || '写入失败'; return; }
    errEl.textContent = d.message || '已写入本战役';
    await refreshState(); renderEntries();
  }catch(e){ errEl.textContent = '网络错误：' + e; }
};

$('#dmn-save').onclick = async () => {
  const name = (npcMode.mode === 'edit' ? npcMode.name
                : ($('#dmn-name').value || '').trim());
  if(!name){ errEl.textContent = '人物要有名字'; return; }
  const n = v => (v == null ? '' : String(v).trim());
  const body = {
    kind: 'dm_set_npc_stats', name, 身份: n($('#dmn-role').value),
    等级: n($('#dmn-level').value), 护甲等级: n($('#dmn-ac').value),
    生命值: n($('#dmn-hp').value), 先攻加值: n($('#dmn-init').value),
    速度: n($('#dmn-speed').value),
    属性: {力量: n($('#dmn-str').value), 敏捷: n($('#dmn-dex').value),
          体质: n($('#dmn-con').value), 智力: n($('#dmn-int').value),
          感知: n($('#dmn-wis').value), 魅力: n($('#dmn-cha').value)},
    豁免熟练: n($('#dmn-saves').value)
  };
  const perc = n($('#dmn-perc').value);
  if(perc !== '') body['技能加值'] = {察觉: perc};
  const atkName = n($('#dmn-atkname').value);
  if(atkName){
    body['动作'] = {};
    body['动作'][atkName] = {
      类型: 'attack', 攻击加值: n($('#dmn-atk').value),
      伤害骰: n($('#dmn-dmg').value), 伤害类型: n($('#dmn-dmgtype').value),
      射程: n($('#dmn-range').value)
    };
  }
  try{
    const d = await (await fetch('/api/act', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)})).json();
    if(!d.ok){ errEl.textContent = d.error || '写入失败'; return; }
    errEl.textContent = d.message || '已写入当局';
    closeAny(); await refreshState();
  }catch(e){ errEl.textContent = '网络错误：' + e; }
};

$('#dm-save').onclick = async () => {
  const name = (itemMode.mode === 'edit' ? itemMode.name
                : ($('#dm-name').value || '').trim());
  if(!name){ errEl.textContent = '造物要有名字'; return; }
  const stats = {
    类别: $('#dm-category').value,
    攻击加值: $('#dm-atk').value.trim(),
    伤害骰: $('#dm-dmg').value.trim(),
    伤害类型: $('#dm-dmgtype').value.trim(),
    护甲加值: $('#dm-ac').value.trim(),
    护甲类型: $('#dm-actype').value,
    装备类型: $('#dm-type').value,
    双手: $('#dm-two').checked
  };
  const text = {
    描述: ($('#dm-desc').value || '').trim(),
    机制: ($('#dm-mech').value || '').trim(),
    背景: ($('#dm-bg').value || '').trim()
  };
  const entryIds = ['#dm-entry1','#dm-entry2','#dm-entry3']
    .map(s => ($(s) ? ($(s).value || '') : '')).filter(Boolean);
  try{
    let d;
    if(itemMode.mode === 'new'){
      const body = {kind:'dm_create_item', name,
        category: stats.类别, ...stats, ...text, 词条: entryIds};
      delete body.类别;
      d = await (await fetch('/api/act', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body)})).json();
    } else {
      d = await (await fetch('/api/act', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({kind:'set_item_stats', name, ...stats,
                              词条: entryIds})})).json();
      if(d.ok){
        d = await (await fetch('/api/act', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({kind:'set_lore', lore_kind:'物品',
                                name, fields:text})})).json();
      }
    }
    if(!d.ok){ errEl.textContent = d.error || '写入失败'; return; }
    errEl.textContent = d.message || '已写入当局';
    closeAny(); await refreshState();
  }catch(e){ errEl.textContent = '网络错误：' + e; }
};
$('#s0-ack').onchange = e => { $('#s0Start').disabled = !e.target.checked; };
$('#s0Start').onclick = async () => {
  // 首屏点击可能早于第一次 poll（lastLobby 还是 null），所以无条件尝试 start_game：
  // 非大厅模式下后端只回“不在开局大厅”，不会重开桌；大厅模式下这才是确认上桌。
  try{ await doAct({kind:'start_game'}); }catch(e){}
  try{ sessionStorage.setItem('dnd-session0','1'); }catch(e){}
  $('#session0').classList.remove('open');
  if(sayEl) sayEl.focus();
};
async function askLobby(){
  const qin = $('#s0-q'); if(!qin) return;
  const q = (qin.value || '').trim();
  if(!q) return;
  const d = await doAct({kind:'lobby_ask', question:q});
  if(d && d.ok){ qin.value = ''; }
  else if(d && d.error){ const hint = $('#s0-qhint'); if(hint) hint.textContent = d.error; }
}
async function answerGuided(skip){
  const step = lastLobby && lastLobby.guided_step;
  if(!step || step === 'done') return;
  const ga = $('#s0-ga');
  const a = skip ? '' : ((ga && ga.value) || '').trim();
  const d = await doAct({kind:'lobby_answer', answer:a, skip:!!skip});
  if(d && d.ok){ if(ga) ga.value = ''; }
  else if(d && d.error){ const h = $('#s0-ghint'); if(h) h.textContent = d.error; }
}
$('#s0Ask').onclick = askLobby;
$('#s0Intros').onclick = async () => {
  const b = $('#s0Intros');
  if(b) b.disabled = true;
  const d = await doAct({kind:'lobby_intros'});
  if(!(d && d.ok)){
    const ib = $('#s0-intros');
    if(ib && d && d.error) ib.textContent = d.error;
  }
};
$('#s0GenCompanions').onclick = async () => {
  const b = $('#s0GenCompanions');
  if(b) b.disabled = true;
  const d = await doAct({kind:'generate_companions'});
  if(!(d && d.ok)){
    const n = $('#s0-companion-note');
    if(n && d && d.error) n.textContent = d.error;
    if(b) b.disabled = false;
  }
};
$('#s0HumanUse').onclick = async () => {
  const sel = $('#s0HumanCard');
  if(!sel || !sel.value) return;
  const d = await doAct({kind:'use_human_card', id:sel.value});
  if(!(d && d.ok)){
    const n = $('#s0-stage-note');
    if(n) n.textContent = (d && d.error) || '换真人卡失败';
  }
};
$('#s0CompUse').onclick = async () => {
  const sel = $('#s0CompCards');
  if(!sel) return;
  const ids = [...sel.selectedOptions].map(x => x.value).filter(Boolean);
  if(!ids.length) return;
  const d = await doAct({kind:'use_companion_cards', ids});
  if(!(d && d.ok)){
    const n = $('#s0-companion-note');
    if(n) n.textContent = (d && d.error) || '同伴卡上桌失败';
  }
};
$('#s0PresetUse').onclick = async () => {
  const ids = [...presetSelected];
  if(!ids.length) return;
  const d = await doAct({kind:'use_preset_cards', ids});
  if(!(d && d.ok)){
    const n = $('#s0-preset-note');
    if(n) n.textContent = (d && d.error) || '预设卡上桌失败';
  }
};
$('#s0DmGo').onclick = async () => {
  const b = $('#s0DmGo');
  const brief = ($('#s0DmBrief').value || '').trim();
  if(!brief) return;
  if(b) b.disabled = true;
  const d = await doAct({kind:'dm_companions', brief});
  if(!(d && d.ok)){
    const n = $('#s0-companion-note');
    if(n && d && d.error) n.textContent = d.error;
    if(b) b.disabled = false;
  }
};
$('#s0SizeGo').onclick = async () => {
  const sel = $('#s0-size');
  if(!sel) return;
  const d = await doAct({kind:'set_table_size', ai:Math.max(0, Math.min(4, Number(sel.value) - 1))});
  if(!(d && d.ok)){
    const n = $('#s0-stage-note');
    if(n && d && d.error) n.textContent = d.error;
  }
};
$('#s0StageGo').onclick = async () => {
  const sel = $('#s0-stage-select');
  if(!sel || !sel.value) return;
  const d = await doAct({kind:'select_stage', stage:sel.value});
  if(!(d && d.ok)){
    const n = $('#s0-stage-note');
    if(n && d && d.error) n.textContent = d.error;
  }
};
$('#s0JustStart').onclick = async () => {
  const d = await doAct({kind:'start_game'});
  if(d && d.ok){
    try{ sessionStorage.setItem('dnd-session0','1'); }catch(e){}
    $('#session0').classList.remove('open');
  }
};
$('#s0Gbtn').onclick = () => answerGuided(false);
$('#s0Gskip').onclick = () => answerGuided(true);
$('#s0-ga').onkeydown = e => { if(e.key === 'Enter'){ e.preventDefault(); answerGuided(false); } };
$('#s0Create').onclick = () => openPanel('create');
$('#s0-q').onkeydown = e => { if(e.key === 'Enter'){ e.preventDefault(); askLobby(); } };
$('#s0Close').onclick = () => {
  $('#session0').classList.remove('open');
  if($('#openSession0')) $('#openSession0').focus();
};
$('#openSession0').onclick = openSession0;
try{
  if(sessionStorage.getItem('dnd-session0') === '1')
    $('#session0').classList.remove('open');
}catch(e){}
$('#newGame').onclick = () => {
  const mode = $('#gameMode') ? $('#gameMode').value : 'normal';
  if(confirm('重开会丢掉当前进度，确定？')){ renderedSeq = 0; doAct({kind:'new_game', mode}); }
};
$('#gameMode').onchange = () => { MODE_DIRTY = true; };
$('#modeStart').onclick = () => {
  const mode = $('#gameMode').value;
  const tip = mode === 'spectate' ? '观战（全 AI，自动推进）'
            : mode === 'hosted' ? '托管（AI 替你行动，可随时接管）' : '真人';
  if(!confirm(`按「${tip}」重开一局？会丢掉当前进度。`)) return;
  renderedSeq = 0;
  doAct({kind:'new_game', mode}).then((d) => {
    if(d && d.ok) MODE_DIRTY = false;
    applyMode(mode);
  });
};

submitEl.onclick = () => {
  if(!selected) return;
  const t = tgtEl._sel ? tgtEl._sel.value : selected.target;
  send(selected.id, t);
};

async function doAct(payload){
  try{
    const r = await fetch('/api/act', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)});
    const d = await r.json();
    if(!d.ok){ errEl.textContent = d.error || '操作失败'; return; }
    errEl.textContent = d.message || '已更新';
    await refreshState();
    return d;
  }catch(e){ errEl.textContent = '网络错误：' + e; }
  return null;
}

async function refreshState(){
  try{
    const r = await fetch('/api/state?since=' + since, {cache:'no-store'});
    const s = await r.json();
    lastPending = s.pending || lastPending;
    if(s.pending){ seq = s.pending.seq; }   // 同 poll()：提交要带对 seq
    lastMap = s.map; lastScene = s.scene; lastGraph = s.graph; lastShop = s.shop; lastNpcs = s.npcs || []; lastCatalog = s.catalog || []; fillEntryOptions();
    lastZero = s.session_zero || null;
    const zt = $('#zeroTool'); if(zt) zt.hidden = !lastZero;
    applyLobby(s);
    applyMode(s.mode || 'normal');
        renderSaveSlots(s.slots || []);
    updateHud(s.sheet, lastPending);
    if(document.querySelector('.overlay[data-panel="inventory"]').classList.contains('open')) renderInventory();
    if(document.querySelector('.overlay[data-panel="sheet"]').classList.contains('open')) renderSheet();
    if(document.querySelector('.overlay[data-panel="map"]').classList.contains('open')) renderMapPanel();
    if(document.querySelector('.overlay[data-panel="quests"]').classList.contains('open')) renderQuests();
    if(document.querySelector('.overlay[data-panel="zero"]').classList.contains('open')) renderZero();
    if(document.querySelector('.overlay[data-panel="npc"]').classList.contains('open')) renderNpcPanel();
    if(document.querySelector('.overlay[data-panel="entries"]').classList.contains('open')) renderEntries();
  }catch(e){ /* 下一轮轮询会补上 */ }
}

async function send(id, target){
  if(busy) return;
  const say = sayEl ? sayEl.value : '';
  busy = true; submitEl.disabled = true;
  try{
    const r = await fetch('/api/answer', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({seq, id, target, say})});
    const d = await r.json();
    if(!d.ok){ errEl.textContent = d.error || d.message || '提交失败'; }
    else { if(sayEl) sayEl.value=''; selected=null; tgtEl.textContent=''; turnEl.textContent='已提交，等待引擎结算…'; }
  }catch(e){ errEl.textContent = '网络错误：' + e; }
  finally{ busy = false; }
}

let lastPending = null;
//: 「思考提示」：关掉之后连"谁在想"都不显示（狼人杀那个开关的对应物）。
//  存 localStorage，切页面/刷新都记得；**不显示座位号以外的任何东西**（不泄私密）。
let SHOW_THINK = localStorage.getItem('dnd_show_think') !== '0';
let THINK = null;
let thinkTimer = null;
function fmtThink(){
  if(!THINK) return '';
  const sec = Math.max(0, Math.round((Date.now() - (THINK.t0 || 0) * 1000) / 1000));
  const who = THINK.seat || '有人';
  const task = THINK.task ? `（${THINK.task}）` : '';
  return `${who} 在想${task}… ${sec}s`;
}
function paintThink(){
  const el = document.getElementById('thinkline');
  if(!el) return;
  el.textContent = SHOW_THINK ? fmtThink() : '';
  if(thinkTimer){ clearInterval(thinkTimer); thinkTimer = null; }
  if(SHOW_THINK && THINK){
    thinkTimer = setInterval(() => { el.textContent = fmtThink(); }, 1000);
  }
}
function setShowThink(on){
  SHOW_THINK = !!on;
  localStorage.setItem('dnd_show_think', on ? '1' : '0');
  const b = document.getElementById('thinkToggle');
  if(b){
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.textContent = on ? '思考提示' : '思考提示(关)';
    b.style.opacity = on ? '' : '0.55';
  }
  paintThink();
}
function applyThink(data){
  THINK = data && data.seat ? data : null;
  paintThink();
}
document.addEventListener('DOMContentLoaded', () => {
  setShowThink(SHOW_THINK);
  const b = document.getElementById('thinkToggle');
  if(b) b.onclick = () => setShowThink(!SHOW_THINK);
});

// ---------- 观战 / 托管 ----------
let GAME_MODE = 'normal';
//: 用户手动选过模式、还没点“按此模式新开”时，轮询不许把 select 改回去。
let MODE_DIRTY = false;
function applyMode(mode){
  GAME_MODE = (mode === 'hosted' || mode === 'spectate') ? mode : 'normal';
  const composer = document.getElementById('composer');
  const banner = document.getElementById('modebanner');
  const text = document.getElementById('modeText');
  const take = document.getElementById('takeOver');
  if(!composer || !banner || !text || !take) return;
  const sel = document.getElementById('gameMode');
  if(!MODE_DIRTY && sel && sel.value !== GAME_MODE) sel.value = GAME_MODE;
  const slots = document.getElementById('slots');
  const cats = document.getElementById('cats');
  const tbox = document.getElementById('targetbox');
  if(GAME_MODE === 'normal'){
    banner.hidden = true; take.hidden = true;
    composer.style.display = '';
    if(slots) slots.style.display = '';
    if(cats) cats.style.display = '';
    if(tbox) tbox.style.display = '';
  }else{
    banner.hidden = false; composer.style.display = 'none';
    text.textContent = GAME_MODE === 'spectate'
      ? '观战：四个座位都是 AI，自动推进（你可以关掉页面）'
      : '托管：AI 正在替你行动';
    take.hidden = GAME_MODE !== 'hosted';
    if(slots) slots.style.display = 'none';
    if(cats) cats.style.display = 'none';
    if(tbox) tbox.style.display = 'none';
    turnEl.textContent = GAME_MODE === 'spectate' ? 'AI 正在行动…' : '托管中：AI 替你选…';
    statusEl.textContent = '自动推进中';
  }
}
const takeBtn = document.getElementById('takeOver');
if(takeBtn) takeBtn.onclick = async () => {
  const d = await doAct({kind:'set_hosted', on:false});
  if(d && d.ok){
    MODE_DIRTY = false;
    $('#gameMode').value = 'normal';   // 只有真的接管成功才同步
  }
};

// ---------- BGM：浏览器合成（不加载任何外部音频） ----------
const BGM = (() => {
  let ctx = null, master = null, oscs = [], lfo = null, running = false;
  let mood = 'explore', builtMood = null;
  let cfg = {on:false, vol:0.35};
  try{ cfg = Object.assign(cfg, JSON.parse(localStorage.getItem('dnd_bgm') || '{}')); }catch(e){}
  function save(){ try{ localStorage.setItem('dnd_bgm', JSON.stringify(cfg)); }catch(e){} }
  function clear(){
    for(const o of oscs){ try{ o.stop(); }catch(e){} try{ o.disconnect(); }catch(e){} }
    oscs = [];
    if(lfo){ try{ lfo.stop(); }catch(e){} try{ lfo.disconnect(); }catch(e){} lfo = null; }
    if(master){ try{ master.disconnect(); }catch(e){} master = null; }
    if(ctx){ try{ ctx.close(); }catch(e){} ctx = null; }
    running = false;
  }
  function build(){
    clear(); // 重建
    const AC = window.AudioContext || window.webkitAudioContext;
    ctx = new AC();
    master = ctx.createGain();
    master.gain.value = Math.max(0.0001, cfg.vol * 0.10);
    master.connect(ctx.destination);
    const base = mood === 'combat' ? 55 : 110;
    const third = mood === 'combat' ? 82.4 : 164.8;
    const types = mood === 'combat' ? ['sawtooth', 'triangle'] : ['sine', 'sine'];
    for(const [f, t, g] of [[base, types[0], 0.5], [third, types[1], 0.28]]){
      const o = ctx.createOscillator(), gn = ctx.createGain();
      o.type = t; o.frequency.value = f; gn.gain.value = g;
      o.connect(gn); gn.connect(master); o.start(); oscs.push(o);
    }
    // 缓慢呼吸：战斗 2Hz 的紧张感，探索 0.07Hz 的起伏
    lfo = ctx.createOscillator(); const lg = ctx.createGain();
    lfo.frequency.value = mood === 'combat' ? 2.0 : 0.07;
    lg.gain.value = mood === 'combat' ? 0.035 : 0.018;
    lfo.connect(lg); lg.connect(master.gain); lfo.start();
    running = true;
    builtMood = mood;
  }
  function start(){
    if(running) return true;
    if(!(window.AudioContext || window.webkitAudioContext)) return false;
    build();
    cfg.on = true; save(); syncBtn();
    return true;
  }
  function stop(){
    clear(); cfg.on = false; save(); syncBtn();
  }
  function toggle(){ running ? stop() : start(); }
  function setMood(m){ mood = (m === 'combat') ? 'combat' : 'explore';
    // 只在**情绪变了**时重建；每 0.8 秒 poll 都重建会把声音切碎。
    if(running && mood !== builtMood) build();
  }
  function setVol(v){ cfg.vol = Math.max(0, Math.min(1, Number(v) || 0));
    if(master) master.gain.value = Math.max(0.0001, cfg.vol * 0.10);
    save();
  }
  function syncBtn(){
    const b = document.getElementById('bgmBtn');
    if(!b) return;
    b.setAttribute('aria-pressed', running ? 'true' : 'false');
    b.textContent = running ? 'BGM 开' : 'BGM';
    b.style.opacity = running ? '' : '0.65';
    const vol = document.getElementById('bgmVol');
    if(vol) vol.value = String(Math.round(cfg.vol * 100));
  }
  document.addEventListener('DOMContentLoaded', () => {
    const b = document.getElementById('bgmBtn');
    if(b) b.onclick = () => { toggle(); };
    const vol = document.getElementById('bgmVol');
    if(vol) vol.oninput = () => setVol(Number(vol.value) / 100);
    syncBtn();
  });
  // 上次开着 BGM：等用户第一次点页面再起（浏览器自动播放策略）
  if(cfg.on){
    const kick = (e) => {
      document.removeEventListener('pointerdown', kick);
      // 点 BGM 按钮本身交给 onclick 的 toggle：kick 先开、click 再关会“点了没开”
      if(e && e.target && e.target.closest && e.target.closest('#bgmBtn')) return;
      start();
    };
    document.addEventListener('pointerdown', kick, {once:true});
  }
  return {start, stop, toggle, setMood, setVol, get running(){ return running; }};
})();

function logClass(text){
  if(text.startsWith('【DM】')) return 'ln-dm';
  if(text.startsWith('【引擎】')) return 'ln-eng';
  if(/^【[^】]+·私有】/.test(text)) return 'ln-private';
  if(/^【[^】]+】/.test(text)) return 'ln-pc';
  if(/^[⚠❌]/.test(text)) return 'ln-bad';
  if(text.startsWith('▸') || text.startsWith('·')) return 'ln-eng';
  if(/^\\s+\\S/.test(text)) return 'ln-detail';
  return '';
}
function appendLog(text){
  const sp = document.createElement('span');
  sp.className = 'ln ' + logClass(text);
  sp.textContent = text;
  logEl.appendChild(sp);
}
async function poll(){
  try{
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5000);
    let r;
    try{ r = await fetch('/api/state?since=' + since, {cache:'no-store', signal:ctrl.signal}); }
    finally{ clearTimeout(timer); }
    const s = await r.json();
    if(s.log && s.log.length){
      for(const x of s.log){ appendLog(x.text); }
      since = s.log[s.log.length-1].seq;
      logEl.scrollTop = logEl.scrollHeight;
    }
    lastPending = s.pending || null; lastMap = s.map; lastScene = s.scene; lastGraph = s.graph; lastShop = s.shop; lastNpcs = s.npcs || []; lastCatalog = s.catalog || []; fillEntryOptions();
    lastZero = s.session_zero || null;
    const zt = $('#zeroTool'); if(zt) zt.hidden = !lastZero;
    applyLobby(s);
    applyThink(s.thinking);
    applyMode(s.mode || 'normal');
    BGM.setMood(s.map && s.map.battle ? 'combat' : 'explore');
    if(s.done) BGM.setMood('explore');
    lastQuests = s.quests || null;
        renderSaveSlots(s.slots || []);
    updateHud(s.sheet, lastPending);
    if(document.querySelector('.overlay[data-panel="inventory"]').classList.contains('open')) renderInventory();
    if(document.querySelector('.overlay[data-panel="sheet"]').classList.contains('open')) renderSheet();
    if(document.querySelector('.overlay[data-panel="map"]').classList.contains('open')) renderMapPanel();
    if(document.querySelector('.overlay[data-panel="quests"]').classList.contains('open')) renderQuests();
    if(document.querySelector('.overlay[data-panel="zero"]').classList.contains('open')) renderZero();
    if(document.querySelector('.overlay[data-panel="npc"]').classList.contains('open')) renderNpcPanel();
    if(document.querySelector('.overlay[data-panel="entries"]').classList.contains('open')) renderEntries();
    if(s.done){ statusEl.textContent = s.error ? ('已结束：'+s.error) : '这一局结束了';
      slotsEl.textContent=''; turnEl.textContent=''; THINK=null; paintThink();
      if(BGM.running) BGM.stop();
      setTimeout(poll, 800);      // 关键：别停轮询，否则新开一局页面不再更新
      return; }
    if(s.pending){
      // ⚠️ 必须把当前问题的 seq 记下来：提交时 `send()` 带的就是这个值。
      // 旧代码只更新 renderedSeq（管“要不要重画选项”），seq 一直是 0，
      // 于是任何一次真人提交都会 409「这个问题已经过期」（真浏览器点一次就复现）。
      seq = s.pending.seq;
      if(s.pending.seq !== renderedSeq){
        renderedSeq = s.pending.seq;
        renderCats(s.pending); renderSlots(s.pending);
        turnEl.textContent = `轮到 ${s.pending.actor}（${s.pending.task}）`;
        errEl.textContent = ''; selected = null; submitEl.disabled = true; tgtEl.textContent = '';
      }
      statusEl.textContent = '轮到你行动';
    } else if(renderedSeq){
      renderedSeq = 0; renderCats(null); renderSlots(null);
      turnEl.textContent = '等待 DM / 其他人…'; statusEl.textContent = '等待中';
    } else statusEl.textContent = '等待 DM / 其他人…';
    if(GAME_MODE !== 'normal' && !s.pending){
      turnEl.textContent = GAME_MODE === 'spectate'
        ? 'AI 正在行动…' : '托管中：AI 替你选…';
    }
  }catch(e){ statusEl.textContent = '连接异常，重试中…'; }
  setTimeout(poll, 800);
}
poll();
</script>
</body></html>"""




