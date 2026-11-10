# coding=utf-8
"""出口闸：**证明"离线"是跑在闸门后面的，不是靠"我记得我桩了"**。

来源：狼人杀交接文档 §4 第 14 条 ——
「自称离线要用『出口闸』证明。历史上有脚本只桩了预检、真调用照发。」

这个闸门做的事很小：把 Python 里"往外连"的四个口子包起来 ——

    socket.getaddrinfo         （DNS 解析）
    socket.create_connection   （TCP 连接，http.client 走它）
    socket.socket.connect      （底层 connect）
    socket.socket.connect_ex   （非阻塞 connect）

**只放行回环地址**（127.0.0.0/8、::1、localhost）；其余一律抛 `NetworkBlocked`，
并把"谁想连哪里"记进 `blocked` 清单，事后能对账。

用法：

    from engine.netguard import install, uninstall
    with install():            # 也可以用 `with NetGuard():`
        ...  # 这里任何外连都会抛

    DND_NETGUARD=1 python ...  # 或设环境变量：engine 一被 import 就自动装上

⚠️ 它**不防** UDP / AF_UNIX / 子进程里另起的解释器（那些要各自装）。这一版的
目标是"测试与跑团主进程不会偷偷连网"，不是内核级沙箱。子进程要装的话，
把 `DND_NETGUARD=1` 传进它的环境即可（`run_all.py` 已经这么做）。
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import threading

__all__ = [
    "NetworkBlocked", "NetGuard", "GUARD", "install", "uninstall",
    "is_active", "blocked_hosts", "maybe_install_from_env",
    "ENV_FLAG", "is_loopback_host",
]

#: 设成 1/on/true 时，`engine` 被 import 就自动装闸（测试子进程靠它）。
ENV_FLAG = "DND_NETGUARD"


class NetworkBlocked(RuntimeError):
    """闸门拦下了一次外连。**这不是业务错误，是"离线证明"的证据。**"""


def _split_address(address) -> tuple[str, int]:
    if isinstance(address, (tuple, list)) and len(address) >= 2:
        return str(address[0]), int(address[1] or 0)
    return str(address), 0


def is_loopback_host(host: str | None) -> bool:
    """只认字面回环：localhost / 127.x / ::1。**主机名一律不放行**（要证离线）。"""
    h = ("" if host is None else str(host)).strip().lower()
    if h in ("", "0.0.0.0", "::"):
        return True                      # 绑定用，不算外连
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


#: 模块级安装状态：**同一时刻只允许一个闸**；同一实例可嵌套（深度计数）。
_LOCK = threading.RLock()
_ACTIVE: "NetGuard | None" = None
_DEPTH = 0


class NetGuard:
    """可安装/卸载的出口闸。**装两次是幂等的。**"""

    def __init__(self, *, allow_loopback: bool = True):
        self.allow_loopback = allow_loopback
        self.active = False
        self.blocked: list[dict] = []
        self._orig: dict = {}

    # ---------- 判定 ----------

    def check(self, host, port=0, via: str = "") -> None:
        if not self.active:
            return
        if self.allow_loopback and is_loopback_host(host):
            return
        self.blocked.append({"host": str(host), "port": int(port or 0),
                             "via": via})
        raise NetworkBlocked(
            f"出口闸拦下外连：{host}:{port}（{via}）—— 只允许回环地址。"
            f"要真联网请显式卸闸（`engine.netguard.uninstall()`）")

    # ---------- 安装 / 卸载 ----------

    def install(self) -> "NetGuard":
        """装闸。**可嵌套**：同一实例装 N 次就要卸 N 次，深度归零才真的还原。

        旧实现里内层 `with install()` 一退出就把外层的闸也卸了（`active` 变 False
        但外层还以为有闸）；这里用模块级深度计数 + 锁，杜绝"静默卸闸"。
        """
        global _ACTIVE, _DEPTH
        with _LOCK:
            if _ACTIVE is self:
                _DEPTH += 1
                self.active = True
                return self
            if _ACTIVE is not None:
                raise RuntimeError(
                    f"已经装了另一个出口闸（{_ACTIVE!r}）—— 先卸它，别叠两个")
            self._orig = {
                "getaddrinfo": socket.getaddrinfo,
                "create_connection": socket.create_connection,
                "connect": socket.socket.connect,
                "connect_ex": socket.socket.connect_ex,
            }
            guard = self

            def getaddrinfo(host, port, *a, **kw):
                guard.check(host, port, "getaddrinfo")
                return guard._orig["getaddrinfo"](host, port, *a, **kw)

            def create_connection(address, *a, **kw):
                h, p = _split_address(address)
                guard.check(h, p, "create_connection")
                return guard._orig["create_connection"](address, *a, **kw)

            def connect(sock_self, address):
                h, p = _split_address(address)
                guard.check(h, p, "connect")
                return guard._orig["connect"](sock_self, address)

            def connect_ex(sock_self, address):
                h, p = _split_address(address)
                guard.check(h, p, "connect_ex")
                return guard._orig["connect_ex"](sock_self, address)

            socket.getaddrinfo = getaddrinfo
            socket.create_connection = create_connection
            socket.socket.connect = connect
            socket.socket.connect_ex = connect_ex
            _ACTIVE = self
            _DEPTH = 1
            self.active = True
            return self

    def uninstall(self) -> None:
        global _ACTIVE, _DEPTH
        with _LOCK:
            if _ACTIVE is not self:
                return
            _DEPTH -= 1
            if _DEPTH > 0:
                return                 # 还有外层没退，闸必须留着
            socket.getaddrinfo = self._orig["getaddrinfo"]
            socket.create_connection = self._orig["create_connection"]
            socket.socket.connect = self._orig["connect"]
            socket.socket.connect_ex = self._orig["connect_ex"]
            self._orig = {}
            self.active = False
            _ACTIVE = None
            _DEPTH = 0

    # ---------- 上下文 ----------

    def __enter__(self) -> "NetGuard":
        return self.install()

    def __exit__(self, *exc) -> None:
        self.uninstall()

    def reset(self) -> None:
        self.blocked.clear()


#: 进程级单例（`engine` import 时可能被环境变量自动装上）
GUARD = NetGuard()


def install() -> NetGuard:
    return GUARD.install()


def uninstall() -> None:
    GUARD.uninstall()


def is_active() -> bool:
    return GUARD.active


def blocked_hosts() -> list[dict]:
    return list(GUARD.blocked)


def maybe_install_from_env() -> bool:
    """`DND_NETGUARD=1/on/true` 时装上。返回是否装了。"""
    flag = str(os.environ.get(ENV_FLAG, "")).strip().lower()
    if flag in ("1", "on", "true", "yes"):
        GUARD.install()
        return True
    return False