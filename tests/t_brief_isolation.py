# coding=utf-8
"""跑一场真实剧本，抓每个座位拿到的提示词，验**剧情简报不外泄**。

`t_prompt_isolation.py` 只测“装配器按视角过滤世界与台账”；这一条补上
`AdventureRunner.dm_hint()` 那条路：`brief.offstage`（幕后）、未发现线索、
跨局欠账**只拼进 DM 的任务说明**，PC/planner 的包里一个字都不许有。

用 M1 剧本真跑一场（假模型，零花费），比断言单个函数更接近真桌。
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import AdventureRunner, load_adventure  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

FAILS: list[str] = []
COUNT = 0

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
PCS = [("布兰", "战士"), ("艾尔玛", "盗贼"), ("罗恩", "牧师"), ("小翠", "法师")]


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


class Recorder(FakeModel):
    """记下每次请求的 prompt；行为仍是假模型的本地战术 AI。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompts: list[tuple[str, str]] = []     # (task, prompt)

    def _complete(self, req):
        self.prompts.append((req.task, req.prompt))
        return super()._complete(req)


def main() -> int:
    adv = load_adventure(os.path.join(ROOT, "adventures",
                                      "雾渡的第三声钟.json"))
    scene = adv.scenes[0]
    # 只有 DM 该看到的三个标记，直接取自剧本本身（不手写假数据）
    secret_bits = {
        "offstage": scene.brief.offstage,
        "present": scene.brief.present,
        "span": scene.brief.span,
    }
    hidden_clues = [c.text for c in scene.clues if c.secret]

    st = StateTree.from_template(
        {n: new_character(n, level=3, job=j, abilities=dict(AB),
                          hp_max=24, ac=15) for n, j in PCS})
    units = [make_hero_unit(st, n) for n, _j in PCS]
    rec = {seat: Recorder() for seat in ["dm", "planner"]
           + [f"pc:{n}" for n, _j in PCS]}
    table = TableClient(dm=rec["dm"], planner=rec["planner"],
                        players={n: rec[f"pc:{n}"] for n, _j in PCS},
                        name="iso")
    loop = GameLoop(st, Ledger(), table, rng=Random(20261107), prompt_budget=6000)
    loop.set_field(units)
    runner = AdventureRunner(loop, adv)
    runner.seed_map()
    runner.run_scene(scene, combat_rounds=1)
    # 再跑一场带战斗的：planner 只在战斗轮出现，不跑它就等于没验 planner 隔离
    fight = next((s for s in adv.scenes if s.combat and s.id != scene.id), None)
    if fight is not None:
        runner.run_scene(fight, combat_rounds=1)

    def prompts_of(seat: str) -> str:
        return "\n".join(p for _t, p in rec[seat].prompts)

    def check(name: str, bits: dict, expect_in: bool) -> None:
        for seat, rows in rec.items():
            body = "\n".join(p for _t, p in rows.prompts)
            if not body:
                continue
            for label, text in bits.items():
                hit = text and text in body
                want = expect_in and seat == "dm"
                ok(hit == want if want else not hit,
                   f"{name} {seat} {'该看到' if want else '不许看到'} {label}"
                   + ("" if hit == want else f"（命中={hit}）"),
                   text[:40])

    print(f"\n=== A. 剧情简报（{scene.id}）按座位隔离 ===")
    check("A1", {"在场与主动性": secret_bits["present"]}, expect_in=True)
    check("A2", {"幕后": secret_bits["offstage"]}, expect_in=True)
    check("A3", {"时间跨度": secret_bits["span"]}, expect_in=True)

    print("\n=== B. 未发现线索不进 PC/planner 包 ===")
    if hidden_clues:
        check("B1", {"未发现线索": hidden_clues[0]}, expect_in=True)
    else:
        ok(False, "B1 剧本第一场应有 hidden clue（夹具变了？）")

    print("\n=== C. 座位真的收到过包（不是空跑通过） ===")
    for seat in ("dm", "planner", "pc:布兰"):
        ok(bool(rec[seat].prompts), f"C {seat} 收到过提示词",
           len(rec[seat].prompts))

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