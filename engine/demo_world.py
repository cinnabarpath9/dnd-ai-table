# coding=utf-8
"""世界状态 demo：地点 / 派系 / 任务 / 时间，按视角渲染，跟着存档走。

跑法：
    python -m engine.demo_world

证明四件事：
  1. 世界树在 `stat_data` 里，写操作全走 commit（有审计）；
  2. 同一棵树，DM 看得到秘密，玩家只看到"已发现 / 已听说 / 公开"；
  3. 世界块能进装配器，且位置在规则之后、角色卡之前（缓存友好）；
  4. 存档 → 读档后，地点/派系/任务/时间与视角过滤都原样保留。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import FakeModel  # noqa: E402
from engine.persist import load_game, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import DM, Ledger, StateTree, new_character  # noqa: E402
from engine.state.assembler import Assembler  # noqa: E402
from engine.state.world import World, new_world  # noqa: E402

AB = {"力量": 16, "敏捷": 14, "体质": 15,
      "智力": 10, "感知": 12, "魅力": 10}


def build() -> tuple[StateTree, Ledger, GameLoop]:
    state = StateTree.from_template({
        "艾尔玛": new_character("艾尔玛", level=3, job="战士",
                                abilities=AB, hp_max=28, ac=16),
    }, current="艾尔玛", label="世界状态 demo",
        world=new_world(date="第 3 日", period="傍晚", weather="阴"))

    w = World(state).ensure()
    w.register_location("王都", description="远处的都城。", region="平原",
                        secret="国王病重。")
    w.discover("旧哨塔", description="塔门半开，里面很暗。", region="荒原",
               secret="塔底有一座献祭祭坛。")
    w.discover("地下墓穴", description="潮湿的石阶向下延伸。", region="荒原")
    w.add_faction("灰狼帮", attitude="敌对", description="盘踞荒原的强盗。",
                  known=True, secret="头目其实是变形怪。")
    w.add_faction("银月议会", attitude="中立", description="遥远的施法者组织。",
                  known=False, secret="他们在找同一枚徽记。")
    w.open_quest("找回货物", description="商队被劫。", objective="找回三箱丝绸",
                 public=True, secret="货物里藏着一枚徽记。", round=2)
    w.open_quest("调查失踪者", description="有人夜里失踪。",
                 objective="查明失踪原因", public=False,
                 secret="失踪者被灰狼帮献祭了。", round=3)
    w.travel("旧哨塔")
    w.advance_time(period="夜里")

    ledger = Ledger()
    ledger.public("公告", "你们在荒原上找到了旧哨塔。", section="公告",
                  actor="DM", round=3)
    client = FakeModel(name="fake-world-demo")
    loop = GameLoop(state, ledger, client, rng=None, label="世界状态 demo")
    loop.set_field([make_hero_unit(state, "艾尔玛")])
    return state, ledger, loop


def main() -> int:
    state, ledger, loop = build()
    w = loop.world
    dm_text = w.render(DM)
    pc_text = w.render("艾尔玛")

    print("=" * 82)
    print("世界状态 demo（零模型调用）")
    print("=" * 82)

    print("\n【1】DM 视角（能看到秘密）")
    print("-" * 82)
    for line in dm_text.splitlines():
        print("  " + line)

    print("\n【2】玩家「艾尔玛」视角（秘密被过滤掉）")
    print("-" * 82)
    for line in pc_text.splitlines():
        print("  " + line)

    checks = {
        "DM 看得到地点秘密": "祭坛" in dm_text,
        "玩家看不到地点秘密": "祭坛" not in pc_text,
        "DM 看得到派系秘密": "变形怪" in dm_text,
        "玩家看不到派系秘密": "变形怪" not in pc_text,
        "玩家看不到未发现地点": "王都" not in pc_text,
        "玩家看得到公开任务": "找回货物" in pc_text,
        "玩家看不到隐藏任务": "调查失踪者" not in pc_text,
        "DM 看得到隐藏任务": "调查失踪者" in dm_text,
    }
    print("\n【3】视角过滤检查")
    print("-" * 82)
    for name, good in checks.items():
        print(f"  {'✅' if good else '❌'} {name}")

    # 世界块进装配器
    asm = Assembler(state, ledger)
    asm.set_block("说明", "你是 DM。")
    asm.set_block("规则", "d20 + 调整值 vs DC。")
    asm.set_block("世界", dm_text)
    asm.set_block("角色卡", state.render("艾尔玛"))
    packed = asm.build(DM, task="描述玩家看到的场景。")
    order = [b.name for b in packed.blocks]
    print("\n【4】装配器里的块顺序")
    print("-" * 82)
    print("  " + " → ".join(order))
    print(f"  （{packed.chars} 字 ≈ {packed.tokens_est} tok）")

    # 存档往返
    outdir = tempfile.mkdtemp(prefix="dnd-world-demo-")
    path = os.path.join(outdir, "world.json")
    save_game(path, state=state, ledger=ledger, loop=loop, note="世界状态 demo")
    sess = load_game(path)
    w2 = World(sess.state)
    print("\n【5】存档 → 读档")
    print("-" * 82)
    print(f"  存档：{path}（{os.path.getsize(path):,} 字节）")
    print(f"  读档：第 {sess.loop.round} 轮｜台账 {len(sess.ledger)} 条｜"
          f"当前地点 {w2.current_location}")
    same = {
        "DM 渲染一致": w2.render(DM) == dm_text,
        "玩家渲染一致": w2.render("艾尔玛") == pc_text,
        "任务状态保留": w2.quest("找回货物")["状态"] == "进行中",
        "时间保留": w2.clock["日期"] == "第 3 日",
        "秘密仍只给 DM": "祭坛" in w2.render(DM)
                        and "祭坛" not in w2.render("艾尔玛"),
    }
    for name, good in same.items():
        print(f"  {'✅' if good else '❌'} {name}")

    ok = all(checks.values()) and all(same.values()) and order.index(
        "世界") == order.index("规则") + 1
    print("\n" + "=" * 82)
    print("✅ 世界状态层：全部通过" if ok else "❌ 有检查项失败")
    print("=" * 82)
    shutil.rmtree(outdir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
