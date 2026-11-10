# coding=utf-8
"""出口闸回归：**"离线"要被证明，不是被记得。**

守四件事：
    G1 判定只放行回环（localhost / 127.x / ::1），主机名一律拦
    G2 装上后四种外连口子全都抛 `NetworkBlocked`，并记进 blocked 清单
    G3 回环照常放行（连不上是 OSError，不是"被闸门拦"）
    G4 幂等、可卸载、可当上下文管理器；环境变量能让子进程自动装
    G5 真跑一轮跑团（假模型）在闸门后**零外连**
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.longrun import LongRun, build_table_loop  # noqa: E402
from engine.netguard import (  # noqa: E402
    ENV_FLAG, GUARD, NetGuard, NetworkBlocked, install, is_active,
    is_loopback_host, uninstall,
)

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


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


# --------------------------------------------------------------------------- #
section("G1 判定：只放行回环")

for host, want in (("localhost", True), ("LOCALHOST", True),
                   ("127.0.0.1", True), ("127.8.9.10", True),
                   ("::1", True), ("0.0.0.0", True), ("", True),
                   ("example.com", False), ("10.0.0.1", False),
                   ("192.168.1.1", False), ("8.8.8.8", False),
                   (None, True)):
    ok(is_loopback_host(host) is want, f"G1 {host!r} → {'放行' if want else '拦'}")


# --------------------------------------------------------------------------- #
section("G2 装上后：外连一律抛，并记账")

# 回归 runner 会用 DND_NETGUARD=1 预装；先卸掉，让本测试从"未装"开始。
uninstall()
ok(not is_active(), "G2a 未显式安装时闸门不生效（runner 会通过环境变量预装）")
g = install()
ok(is_active(), "G2b install() 之后生效")
try:
    ok(raises(lambda: socket.getaddrinfo("example.com", 80), NetworkBlocked),
       "G2c DNS 解析被拦")
    ok(raises(lambda: socket.create_connection(("example.com", 80), 0.2),
              NetworkBlocked), "G2d create_connection 被拦")
    s = socket.socket()
    try:
        ok(raises(lambda: s.connect(("8.8.8.8", 53)), NetworkBlocked),
           "G2e 底层 connect 被拦")
        ok(raises(lambda: s.connect_ex(("8.8.8.8", 53)), NetworkBlocked),
           "G2f connect_ex 被拦")
    finally:
        s.close()
    hosts = {(b["host"], b["via"]) for b in GUARD.blocked}
    ok(("example.com", "getaddrinfo") in hosts
       and ("8.8.8.8", "connect") in hosts,
       f"G2g blocked 清单记下了 {len(GUARD.blocked)} 次尝试", sorted(hosts))
finally:
    uninstall()


# --------------------------------------------------------------------------- #
section("G3 回环照常放行（失败是 OSError，不是闸门）")

install()
try:
    ok(len(socket.getaddrinfo("localhost", 80)) >= 1, "G3a localhost 能解析")
    blocked_before = len(GUARD.blocked)
    try:
        socket.create_connection(("127.0.0.1", 9), 0.2)
        refused = False
    except NetworkBlocked:
        refused = "blocked"
    except OSError:
        refused = True
    ok(refused is True, "G3b 127.0.0.1:9 是连接被拒（不是被闸门拦）", refused)
    ok(len(GUARD.blocked) == blocked_before, "G3c 回环尝试不记进 blocked")
finally:
    uninstall()


# --------------------------------------------------------------------------- #
section("G4 幂等 / 卸载 / 上下文 / 环境变量")

orig = socket.getaddrinfo
g1 = install()
g2 = install()
ok(g1 is g2 and is_active(), "G4a 装两次幂等（同一个 guard）")
uninstall()
uninstall()
ok(socket.getaddrinfo is orig and not is_active(), "G4b 卸载两次安全，且真的还原")

try:
    with NetGuard():
        socket.getaddrinfo("example.com", 80)
except NetworkBlocked:
    pass
ok(socket.getaddrinfo is orig, "G4c 上下文退出后自动还原（异常路径也算）")

code = ("import sys; sys.path.insert(0, r'%s'); import engine; "
        "from engine.netguard import is_active; print(is_active())" % ROOT)
env_on = {**os.environ, ENV_FLAG: "1", "PYTHONIOENCODING": "utf-8"}
env_off = {k: v for k, v in os.environ.items() if k != ENV_FLAG}
env_off["PYTHONIOENCODING"] = "utf-8"
a = subprocess.run([sys.executable, "-c", code], env=env_on,
                   capture_output=True, timeout=60)
b = subprocess.run([sys.executable, "-c", code], env=env_off,
                   capture_output=True, timeout=60)
ok(a.stdout.decode().strip() == "True", "G4d ⭐ 环境变量让子进程自动装闸",
   a.stdout.decode(errors="replace"))
ok(b.stdout.decode().strip() == "False", "G4e 不设环境变量就不装",
   b.stdout.decode(errors="replace"))


# --------------------------------------------------------------------------- #
section("G5 真跑一轮跑团：闸门后零外连")

install()
try:
    GUARD.reset()
    st, led, loop = build_table_loop(seed=31)
    LongRun(loop).run(1)
    ok(not GUARD.blocked, "G5a ⭐ 一轮四人桌（假模型）没有任何外连尝试",
       GUARD.blocked[:3])
    ok(len(loop.client.reqs) >= 1, "G5b 这一轮真的调了模型（不是空跑）",
       len(loop.client.reqs))
finally:
    uninstall()


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")