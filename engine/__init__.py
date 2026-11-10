# coding=utf-8
"""D&D AI 跑团引擎（P1 地基）。

设计纪律（见 `决策记录.md` §5.12.2）：
    骰子和判定永远由代码做 —— 模型只出意图、只写叙事。
    所以这个包里**不许出现任何模型调用**，全部可在离线状态下跑完整场。
"""

__all__ = ["srd"]

#: 出口闸：设了 `DND_NETGUARD=1` 的进程一 import 引擎就装上。
#: 测试/回归靠它把"离线"变成**被证明**的，而不是"我记得没联网"。
#: 默认不装（正常使用不受影响）；装了以后只放行回环地址。
from . import netguard as _netguard  # noqa: E402

_netguard.maybe_install_from_env()
