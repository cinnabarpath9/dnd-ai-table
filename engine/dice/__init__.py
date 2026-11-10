# coding=utf-8
"""骰子与检定。

**所有随机性只从这个包出。** 引擎别处不许直接 `import random`（回归套件会守这一条），
这样"同种子同一局"才成立，复盘才能逐颗骰子对账。
"""

from __future__ import annotations

#: 复出 `random.Random` 这个名字，让引擎别处**不必 import random**。
#: 随机数的唯一出口是 `roll()` / `roll_d20()`；引擎只借用这个类型标注。
#: 回归 `check_isolation.py` 的 L3 会守这一条——如果哪处又直接 import random，
#: "同种子同一局"就不再成立了。
from random import Random

from .roller import (
    AbilityCheck,
    DiceTerm,
    Roll,
    RollError,
    RollMods,
    advantage_of,
    parse,
    roll,
    roll_d20,
)

__all__ = [
    "Roll", "RollMods", "RollError", "DiceTerm", "AbilityCheck", "Random",
    "roll", "roll_d20", "parse", "advantage_of",
]
