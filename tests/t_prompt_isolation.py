# coding=utf-8
"""P9：提示包信息隔离 —— DM 秘密、别人的私有、引擎暗骰都不许进 PC/planner 包。

这是「把可见性做成架构」在本项目里的对应闸门：
不靠模型自觉，靠 `World.render(viewer)` + `Ledger.render(viewer)` 的过滤，
再用真实装配出来的 prompt 断言。
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import FakeModel  # noqa: E402
from engine.rules.loop import GameLoop  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
from engine.state.visibility import DM, PLANNER  # noqa: E402

FAILS: list[str] = []
COUNT = 0

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
SECRET = "税印-7f3a"          # 世界地点秘密
LEDGER_SECRET = "铜扣-9c1d"   # 只有 DM 的台账
PRIVATE_B = "布兰私-2b8e"     # 布兰的私有
PRIVATE_A = "艾尔玛私-4d6f"   # 艾尔玛的私有
ENGINE_ONLY = "暗骰-5a0c"     # 连 DM 都不给


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def make_state() -> StateTree:
    return StateTree.from_template({
        "艾尔玛": new_character("艾尔玛", level=3, job="战士", abilities=AB,
                                hp_max=28, ac=16),
        "布兰": new_character("布兰", level=3, job="盗贼", abilities=AB,
                              hp_max=24, ac=15),
    })


def main() -> int:
    st = make_state()
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.world.register_location(
        "地窖", description="潮湿的地窖", secret=f"墙缝里藏着{SECRET}",
        discovered=True)
    loop.ledger.public("公告", "渡口的灯还亮着")
    loop.ledger.secret("DM秘密", f"账本夹层里有{LEDGER_SECRET}")
    loop.ledger.private("私有", PRIVATE_B, "布兰")
    loop.ledger.private("私有", PRIVATE_A, "艾尔玛")
    loop.ledger.engine_only("暗骰", ENGINE_ONLY)

    def packet(viewer: str) -> str:
        asm = loop._assembler(phase="explore")
        asm.set_block("说明", "隔离测试", why="测试")
        loop._set_world(asm, viewer)
        return asm.build(viewer, task="隔离测试").text()

    section("A. DM 包：该看到的都在")
    dm = packet(DM)
    ok("潮湿的地窖" in dm, "A1 DM 看得到公开地点描述")
    ok(SECRET in dm, "A2 ⭐ DM 看得到地点秘密", SECRET)
    ok(LEDGER_SECRET in dm, "A3 ⭐ DM 看得到 secret 台账")
    ok(PRIVATE_B in dm and PRIVATE_A in dm, "A4 ⭐ DM 看得到所有人的私有（主持人）")
    ok(ENGINE_ONLY not in dm, "A5 ⭐ engine_only 连 DM 都不给")

    section("B. PC 包：只看到自己的")
    a = packet("艾尔玛")
    ok("潮湿的地窖" in a, "B1 PC 看得到已发现地点描述")
    ok("渡口的灯还亮着" in a, "B2 PC 看得到 public 台账")
    ok(SECRET not in a and "🔒秘密" not in a, "B3 ⭐ 地点秘密不进 PC 包", SECRET)
    ok(LEDGER_SECRET not in a, "B4 ⭐ DM 的 secret 台账不进 PC 包")
    ok(PRIVATE_B not in a, "B5 ⭐ 别人的私有不进 PC 包")
    ok(PRIVATE_A in a, "B6 自己的私有在 PC 包")
    ok(ENGINE_ONLY not in a, "B7 engine_only 不进 PC 包")

    b = packet("布兰")
    ok(PRIVATE_B in b and PRIVATE_A not in b, "B8 ⭐ 私有按座位分开")

    section("C. planner 包：只拿公共战场信息")
    p = packet(PLANNER)
    ok("渡口的灯还亮着" in p, "C1 planner 看得到 public")
    ok(SECRET not in p and LEDGER_SECRET not in p, "C2 ⭐ planner 不拿 DM 秘密")
    ok(PRIVATE_A not in p and PRIVATE_B not in p, "C3 ⭐ planner 不拿 PC 私有")
    ok(ENGINE_ONLY not in p, "C4 planner 不拿暗骰")

    print("\n" + "=" * 70)
    if FAILS:
        print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
        for f in FAILS:
            print(f"  ✗ {f}")
        return 1
    print(f"共 {COUNT} 项：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())