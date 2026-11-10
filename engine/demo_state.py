# coding=utf-8
"""状态层 demo：一场战斗后的状态变更，**全程零模型调用**。

演示四件事：
  1. **MVU 那条 JSONPatch 增量**怎么用（模型只写变化，不重写整份档案）；
  2. **"代码算 → 代码写 patch"** 与 MVU 的区别（同一个 patch，谁来算）；
  3. **审计**：每次改动都有"谁改的、为什么、改了多少"；
  4. **可见性**：三个视角（DM / 艾尔玛 / 罗恩）各自看到的台账不一样。

跑法：
    python -m engine.demo_state
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random, roll_d20  # noqa: E402
from engine.rules.adjudicate import (  # noqa: E402
    grid_distance_ft, resolve_attack, resolve_damage_expression,
)
from engine.rules.bestiary import build_unit  # noqa: E402
from engine.rules.constants import ability_modifier, proficiency_bonus  # noqa: E402
from engine.rules.menu import build_menu, render_menu  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.srd.store import get_store  # noqa: E402
from engine.state import (  # noqa: E402
    DM, Ledger, StateTree, new_character,
)
from engine.state.assembler import Assembler  # noqa: E402


def hero_state(name: str, *, job: str, level: int, abilities: dict,
               hp_max: int, ac: int) -> dict:
    return new_character(name, level=level, job=job, abilities=abilities,
                         hp_max=hp_max, ac=ac)


def main() -> int:
    rng = Random(20261014)

    # ===================================================================== #
    print("=" * 92)
    print("P2 状态层 demo —— 零模型调用")
    print("=" * 92)

    # --------------------------------------------------------------------- #
    print("\n【1】建局：从模板初始化 stat_data")
    print("-" * 92)
    st = StateTree.from_template({
        "艾尔玛": hero_state("艾尔玛", job="战士", level=3,
                            abilities={"力量": 16, "敏捷": 14, "体质": 15,
                                       "智力": 10, "感知": 12, "魅力": 10},
                            hp_max=28, ac=16),
        "罗恩": hero_state("罗恩", job="法师", level=3,
                           abilities={"力量": 8, "敏捷": 14, "体质": 12,
                                      "智力": 16, "感知": 12, "魅力": 10},
                           hp_max=18, ac=12),
    }, current="艾尔玛", label="哨塔遭遇战")

    # 法师的法术位（低频"填表"场景：这种 patch 可以来自填表模型）
    st.commit([
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术位/1",
         "value": {"当前": 4, "最大": 4}},
        {"op": "insert", "path": "/角色列表/罗恩/施法/法术书/魔法飞弹",
         "value": {"等级": 1, "准备中": True, "学派": "塑能",
                   "成分": "V、S", "描述": "3 枚飞弹，每枚 1d4+1 力场伤害"}},
    ], actor="填表", reason="新建法师：建立法术位与法术书")

    print("  校验：", "通过" if not st.validate() else st.validate())
    print("\n  艾尔玛的状态块（给模型看的就是这个，不是整棵树）：")
    for line in st.render("艾尔玛").splitlines():
        print("    " + line)

    # --------------------------------------------------------------------- #
    print("\n【2】开局：写台账（三档可见性）")
    print("-" * 92)
    led = Ledger()
    led.public("公告", "秋日午后，你们推开废弃哨塔的木门，霉味扑面而来。",
               section="公告", round=0)
    led.private("检定", "罗恩发现地板上有一块松动的石板，下面似乎有东西。", "罗恩",
                section="你的私有", round=0)
    led.secret("战斗", "石板下是一枚警报符咒；塔顶的哥布林已经听见了开门声。",
               section="战斗", round=0)
    print(f"  写入了 {len(led)} 条台账：1 条公开 + 1 条罗恩私有 + 1 条 DM 秘密")

    # --------------------------------------------------------------------- #
    print("\n【3】战斗：掷先攻，按先攻序推进")
    print("-" * 92)
    pcs = []
    for nm, x in (("艾尔玛", 30.0), ("罗恩", 40.0)):
        c = st.read(f"/角色列表/{nm}")
        u = Unit(id=nm, name=nm, team="pc", is_pc=True, level=3,
                 ac=int(c["护甲等级"]), hp_max=c["生命值"]["最大"],
                 hp=c["生命值"]["当前"], speeds={"步行": c["速度"]["步行"]},
                 abilities=dict(c["属性"]),
                 save_proficiencies={"力量", "体质"} if nm == "艾尔玛" else {"智力", "感知"},
                 position=(x, 0.0))
        # 武器从 SRD 读，别手搓伤害骰
        w = get_store().by_name("weapons", "Longsword" if nm == "艾尔玛" else "Dagger")
        die = w.get("damage_dice") or "1d8"
        dmg_bonus = ability_modifier(u.abilities["力量"])
        u.actions.append(Action(
            name=f"攻击（{w['name']}）", slot="action",
            attacks=[Attack(name=w["name"],
                            attack_bonus=ability_modifier(u.abilities["力量"]) + proficiency_bonus(3),
                            damage_notation=f"{die}{dmg_bonus:+d}",
                            damage_type="slashing", reach=5, is_melee=True)]))
        pcs.append(u)

    goblins = []
    for i in range(3):
        g, _ = build_unit("Bandit", uid=f"G{i+1}", team="enemy")
        g.id = f"G{i+1}"
        g.name = f"哥布林·G{i+1}"
        g.position = (float(i * 5), 0.0)
        goblins.append(g)
    field = pcs + goblins

    order = []
    for u in field:
        order.append((u, roll_d20(rng, u.initiative_bonus).total))
    order.sort(key=lambda t: (-t[1], -t[0].mod("dexterity"), t[0].id))
    led.public("公告", "先攻序：" + " > ".join(f"{u.name}({i})" for u, i in order),
               section="战斗", round=1)
    for u, i in order:
        print(f"    {u.name:<12} 先攻 {i}")

    # --------------------------------------------------------------------- #
    print("\n【4】一次攻击 → 代码算 → 代码写 patch → 落库")
    print("-" * 92)
    elma = pcs[0]
    gob = goblins[0]
    # 接敌：移动过去（引擎会按格距判够不够得着；这里直接把位置摆到相邻格）
    elma.position = (gob.position[0] + 5, gob.position[1])
    led.public("战斗", f"艾尔玛冲到 {gob.name} 身前（相距 5 尺）。",
               section="战斗", actor="艾尔玛", round=1)
    elma.reset_turn()
    atk = elma.actions[0].attacks[0]
    res = resolve_attack(elma, gob, atk, rng,
                         distance=grid_distance_ft(elma.position, gob.position))
    print("  结算：" + res.summary())

    if res.hit:
        # ⭐ 关键：**伤害是引擎掷的**，patch 是引擎按结算结果生成的
        dmg = res.damage_total
        led.public("战斗", f"艾尔玛用长剑击中 {gob.name}，造成 {dmg} 点挥砍伤害。",
                   section="战斗", actor="艾尔玛", round=1,
                   data={"target": gob.id, "damage": dmg,
                         "roll": res.check.total, "ac": gob.ac})
        # 敌人不在 stat_data 的"角色列表"里（它们是临时单位），所以这里演示
        # 把 PC 自己的资源变化落库 —— 这才是 MVU 那棵树管的东西。
        changes = st.commit([
            {"op": "delta", "path": "/角色列表/艾尔玛/生命值/当前", "value": 0},
        ], actor="引擎", reason="（示例）本回合无自身血量变化")
    else:
        led.public("战斗", f"艾尔玛挥剑落空。", section="战斗", actor="艾尔玛", round=1)

    # 让哥布林打一下，演示 PC 掉血 + 状态
    gob.reset_turn()
    gres = resolve_attack(gob, elma, [a for a in gob.actions
                                      if a.name == "Scimitar"][0].attacks[0],
                          rng, distance=grid_distance_ft(gob.position, elma.position))
    print("  结算：" + gres.summary())
    if gres.hit:
        changes = st.commit([
            {"op": "delta", "path": "/角色列表/艾尔玛/生命值/当前",
             "value": -gres.damage_total},
        ], actor="引擎", reason=f"{gob.name} 的弯刀命中")
        print(f"  → 状态变更：{'；'.join(changes)}")
        led.public("战斗", f"{gob.name} 用弯刀砍中艾尔玛，造成 {gres.damage_total} 点穿刺伤害。",
                   section="战斗", actor=gob.name, round=1)
        # 半血 → 加个状态（演示状态用文字描述，不建对象）
        cur, mx, _t = st.hp("艾尔玛")
        if cur * 2 <= mx:
            ch2 = st.commit([
                {"op": "replace", "path": "/角色列表/艾尔玛/当前状态",
                 "value": "被砍中的上臂在流血（Bloodied），呼吸急促"},
            ], actor="引擎", reason="血量过半 → 记见红")
            print(f"  → 状态变更：{'；'.join(ch2)}")

    # --------------------------------------------------------------------- #
    print("\n【5】审计：每一次改动都有据可查")
    print("-" * 92)
    for e in st.audit:
        print("  " + e.line())
    print(f"\n  当前艾尔玛：HP {st.hp('艾尔玛')[0]}/{st.hp('艾尔玛')[1]}"
          f"｜状态：{st.read('/角色列表/艾尔玛/当前状态') or '（无）'}")

    # --------------------------------------------------------------------- #
    print("\n【6】⭐ 可见性：三个视角看到的台账不一样")
    print("-" * 92)
    for viewer in ("艾尔玛", "罗恩", DM):
        text = led.render(viewer)
        n = len(led.visible_to(viewer))
        print(f"\n  ── 视角：{viewer}（可见 {n} 条）──")
        for line in text.splitlines():
            print("    " + line)

    leaks = None
    from engine.state import lint_leaks  # noqa: E402
    leaks = lint_leaks(led)
    print(f"\n  泄漏检查：{'发现 ' + str(len(leaks)) + ' 处' if leaks else '未发现私有内容泄漏到公开条目'}")
    for l in leaks:
        print("    ✗ " + l)

    # --------------------------------------------------------------------- #
    print("\n【7】上下文装配：同一份状态，三个视角各自的包")
    print("-" * 92)
    asm = Assembler(st, led)
    asm.set_block("说明", "你在一场 D&D 战斗中。只输出你的行动意图。", why="系统提示")
    asm.set_block("规则", "攻击：d20 + 攻击加值 vs AC。命中后掷伤害骰。"
                          "每回合 1 动作 / 1 附赠动作 / 1 反应 / 移动。", why="本场规则")

    for viewer in ("艾尔玛", "罗恩"):
        # 战斗轮用"战斗必需块"，不用整张角色卡（对应决策记录 §4 的两视图纪律）
        unit = next((u for u in field if u.id == viewer), None)
        asm.set_block("角色卡",
                      unit.combat_block() if unit else st.render(viewer),
                      why="战斗轮只发战斗必需块（日常轮才发完整角色卡）")
        menu_text = render_menu(build_menu(unit, field), battlefield=field) if unit else ""
        asm.set_block("可选行动", menu_text, why="合法动作菜单")
        a = asm.build(viewer, task=f"现在轮到你（{viewer}）行动。")
        print(f"\n  ── {viewer} 的包：{a.chars} 字 ≈ {a.tokens_est} tok ──")
        print("     " + a.summary().replace("\n", "\n     "))

    a_dm = asm.build(DM, task="描述回合末发生了什么。")
    print(f"\n  ── DM 的包：{a_dm.chars} 字 ≈ {a_dm.tokens_est} tok ──")
    print("     " + a_dm.summary().replace("\n", "\n     "))

    # --------------------------------------------------------------------- #
    print("\n【8】把整棵树存下来（可复算）")
    print("-" * 92)
    js = st.to_json(indent=1)
    print(f"  stat_data 序列化：{len(js)} 字符")
    st2 = StateTree.from_dict(__import__("json").loads(js))
    print(f"  往返一致：{st2.snapshot() == st.snapshot()}")
    print("\n" + "=" * 92)
    print("全程**零模型调用**。这份输出本身就是 P2 的验收证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
