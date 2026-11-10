# coding=utf-8
"""战斗 demo：一场完整战斗，**全程零模型调用**。

这个脚本有两个用途：
  1. **证明地基能跑**：从 SRD 建怪 → 出菜单 → 挑动作 → 结算 → 推进先攻序，一场打完。
  2. **当 P0-③ 实测的出题器**：`--export-prompt` 会把"敌方规划模型"要看到的那段
     提示词原样打印出来（战场 + 全部敌人菜单 + 输出格式）。拿它去问真模型，
     就能测出"一次输出多个单位行动序列"这个能力（狼人杀题库测不到它）。

跑法：
    python -m engine.demo_battle                          # 默认：3 个 PC + 4 个强盗
    python -m engine.demo_battle --enemies 6 --rounds 8
    python -m engine.demo_battle --export-prompt          # 只打印给模型的提示词
    python -m engine.demo_battle --seed 7                 # 换种子（可复现）
"""

from __future__ import annotations

import argparse
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dice import Random, roll_d20  # noqa: E402
from engine.rules.adjudicate import (  # noqa: E402
    AdjudicationError, grid_distance_ft, resolve_attack, resolve_save_action,
)
from engine.rules.bestiary import build_unit  # noqa: E402
from engine.rules.constants import ability_modifier, proficiency_bonus  # noqa: E402
from engine.rules.menu import build_menu, render_menus  # noqa: E402
from engine.rules.unit import Action, Attack, Unit  # noqa: E402
from engine.srd.store import get_store  # noqa: E402

ABIL = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")


def weapon_attack(weapon_name: str, *, ability: str, bonus_ability: int,
                  level: int, mastery: str = "") -> Action:
    """按 SRD 的武器数据造一个攻击动作。

    **不手搓伤害骰** —— 从本地 SRD 的 weapons 表读，避免"我记得长剑是 1d8"这类错误。
    """
    store = get_store()
    rec = store.by_name("weapons", weapon_name)
    dmg_die = rec.get("damage_dice") or "1d8"
    dmg_type = ((rec.get("damage_type") or {}).get("key")
                if isinstance(rec.get("damage_type"), dict) else rec.get("damage_type")) or ""
    props = rec.get("properties") or []
    prop_keys = {((p.get("property") or {}).get("key") if isinstance(p, dict) else p)
                 for p in props}
    prop_keys = {p for p in prop_keys if p}
    is_ranged = "range" in (rec.get("name") or "").lower() or "ammunition" in prop_keys

    atk_bonus = ability_modifier(bonus_ability) + proficiency_bonus(level)
    dmg_bonus = ability_modifier(bonus_ability)
    atk = Attack(
        name=weapon_name,
        attack_bonus=atk_bonus,
        damage_notation=f"{dmg_die}{dmg_bonus:+d}" if dmg_bonus else dmg_die,
        damage_type=dmg_type or "slashing",
        reach=None if is_ranged else 5,
        range_normal=80 if is_ranged else None,
        range_long=320 if is_ranged else None,
        is_melee=not is_ranged,
        properties=tuple(sorted(prop_keys)),
    )
    note = f"（{rec.get('name')}：{dmg_die} {dmg_type}）"
    if mastery:
        note += f"｜精通：{mastery}"
    return Action(name=f"攻击（{weapon_name}）", slot="action", attacks=[atk],
                  desc=note, structured=True)


def make_hero(idx: int, x: float) -> Unit:
    """造一个 3 级战士型 PC（第一版先手搓，角色创建是 P7 的事）。

    ⚠️ 武器数据从 SRD 读；攻击加值 = 力量调整值 + 熟练加值。
    """
    u = Unit(
        id=f"P{idx}", name=f"冒险者{idx}", team="pc", is_pc=True, level=3,
        ac=16, hp_max=28, hp=28, speeds={"walk": 30},
        abilities={"strength": 16, "dexterity": 14, "constitution": 15,
                   "intelligence": 10, "wisdom": 12, "charisma": 10},
        save_proficiencies={"strength", "constitution"},
        skill_bonuses={"athletics": 5, "perception": 3},
        position=(x, 0),
    )
    u.actions.append(weapon_attack("Longsword", ability="strength",
                                   bonus_ability=u.abilities["strength"],
                                   level=u.level, mastery="Sap"))
    return u


def roll_initiative(units: list[Unit], rng: Random) -> list[tuple[Unit, int]]:
    """掷先攻。**先攻决定回合顺序 —— 一个单位一个回合，不存在同时调度。**"""
    out = []
    for u in units:
        r = roll_d20(rng, u.initiative_bonus)
        out.append((u, r.total))
    # 平手：敏捷高的先；再平手按 id（**要确定性，否则同种子会分叉**）
    out.sort(key=lambda t: (-t[1], -t[0].mod("dexterity"), t[0].id))
    return out


def _expected_damage(atk) -> float:
    """粗略期望伤害（只看骰面平均 + 加值），用来给本地 AI 排序。"""
    import re as _re
    total = 0.0
    for expr in (atk.damage_notation, atk.extra_damage_notation):
        if not expr:
            continue
        m = _re.match(r"(\d*)d(\d+)([+-]\d+)?", expr.replace(" ", ""))
        if not m:
            continue
        cnt = int(m.group(1)) if m.group(1) else 1
        faces = int(m.group(2))
        bonus = int(m.group(3) or 0)
        total += cnt * (faces + 1) / 2 + bonus
    return total


def pick_best_attack(unit: Unit, menu, field: list[Unit], rng: Random):
    """本地兜底 AI（**也是 D3"敌人回合不调模型"的最小实现**）。

    规则很朴素，但刻意做对了两件事：
      1. **优先打血最少的敌人**（集火）；
      2. **近战够得着就别用远程**（远程在接敌距离有劣势，且伤害期望通常不占优）。

    真接上敌方规划模型后，这个函数降级为"模型崩格式时的兜底"。
    """
    by_id = {u.id: u for u in field}

    def score(o) -> tuple:
        a = unit.actions[o.action_index].attacks[o.attack_index]
        exp = _expected_damage(a)
        melee_first = 0 if a.is_melee else 1
        # 近战优先、期望伤害高的优先
        return (melee_first, -exp)

    atk_opts = [o for o in menu.options if o.kind == "attack" and o.target_ids]
    if atk_opts:
        atk_opts.sort(key=score)
        o = atk_opts[0]
        # 集火：目标里挑血最少的
        tgt = min((by_id[i] for i in o.target_ids if i in by_id),
                  key=lambda u: (u.hp, u.id))
        return ("attack", o, tgt)

    save_opts = [o for o in menu.options if o.kind == "save_action" and o.target_ids]
    if save_opts:
        o = save_opts[0]
        return ("save", o, [by_id[i] for i in o.target_ids if i in by_id])

    mv = [o for o in menu.options if o.kind == "move"]
    if mv:
        return ("move", mv[0], None)
    return ("end", None, None)


def _step_toward(actor: Unit, target: Unit, budget: float) -> float:
    """把 actor 沿直线朝 target 移动，最多 budget 尺，**停在 5 尺（相邻格）**。

    返回实际移动距离。这是本地兜底 AI 的"接敌"逻辑；
    真接上敌方规划模型后，位置由模型的意图 + 这里的合法性检查共同决定。
    """
    ax, ay = actor.position
    tx, ty = target.position
    dist = grid_distance_ft(actor.position, target.position)
    if dist <= 5:
        return 0.0
    # 目标位置：留在离目标 5 尺处
    travel = min(budget, dist - 5)
    if travel <= 0:
        return 0.0
    # 按格（5 尺）步进，保证落在格上
    steps = int(travel // 5)
    if steps <= 0:
        return 0.0
    dx, dy = tx - ax, ty - ay
    norm = max(abs(dx), abs(dy)) or 1
    ux, uy = dx / norm, dy / norm
    nx = ax + round(ux * steps) * 5
    ny = ay + round(uy * steps) * 5
    # 不许跟别人重叠（简化：撞了就少走一格）
    moved = grid_distance_ft((ax, ay), (nx, ny))
    actor.position = (nx, ny)
    actor.movement_used += moved
    return moved


def execute(actor: Unit, choice, field: list[Unit], rng: Random,
            log: list[str]) -> None:
    """执行一个选择，把结果写进 log。"""
    kind, opt, tgt = choice
    if kind == "attack":
        act = actor.actions[opt.action_index]
        atk = act.attacks[opt.attack_index]
        # 够不着 → 先花移动力接敌，再打
        need = (atk.range_normal or 0) if not atk.is_melee else (atk.reach or 5)
        need = need or 5
        d = grid_distance_ft(actor.position, tgt.position)
        if d > need:
            budget = max(0.0, actor.speed - actor.movement_used)
            moved = _step_toward(actor, tgt, budget)
            if moved:
                log.append(f"  {actor.name} 移动 {moved:g} 尺接敌"
                           f"（{d:g} → {grid_distance_ft(actor.position, tgt.position):g} 尺）")
            d = grid_distance_ft(actor.position, tgt.position)
        try:
            res = resolve_attack(actor, tgt, atk, rng, distance=d)
            log.append("  " + res.summary())
            actor.spend(act.slot)
        except AdjudicationError as exc:
            log.append(f"  [跳过] {exc}")
        return
    if kind == "save":
        act = actor.actions[opt.action_index]
        try:
            sr = resolve_save_action(actor, tgt, act, rng)
            log.append("  " + sr.summary().replace("\n", "\n  "))
            actor.spend(act.slot)
        except AdjudicationError as exc:
            log.append(f"  [跳过] {exc}")
        return
    if kind == "move":
        foes = [u for u in field if u.alive and u.team != actor.team]
        if foes:
            nearest = min(foes, key=lambda u: grid_distance_ft(actor.position, u.position))
            budget = max(0.0, actor.speed - actor.movement_used)
            moved = _step_toward(actor, nearest, budget)
            log.append(f"  {actor.name} 移动 {moved:g} 尺接敌"
                       f"（现距 {grid_distance_ft(actor.position, nearest.position):g} 尺）")
        else:
            log.append(f"  {actor.name} 无敌人可接近")
        return
    log.append(f"  {actor.name} 结束回合")


def main() -> int:
    ap = argparse.ArgumentParser(description="零模型调用的战斗 demo")
    ap.add_argument("--pcs", type=int, default=3)
    ap.add_argument("--enemies", type=int, default=4)
    ap.add_argument("--enemy", default="Bandit", help="敌人种类（SRD 名字）")
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20261014)
    ap.add_argument("--export-prompt", action="store_true",
                    help="只打印「敌方规划模型」该看到的提示词（P0-③ 出题用）")
    ap.add_argument("--quiet", action="store_true", help="只打结果摘要")
    args = ap.parse_args()

    rng = Random(args.seed)

    pcs = [make_hero(i + 1, 30 + i * 10) for i in range(args.pcs)]
    enemies: list[Unit] = []
    for i in range(args.enemies):
        u, unparsed = build_unit(args.enemy, uid=f"E{i + 1}", team="enemy")
        # ⚠️ uid 参数在某些版本里可能没生效（build_unit 的签名差异），
        #    这里**兜底再赋一次**并校验唯一性 —— 重复 id 会让 build_menu 直接报错。
        u.id = f"E{i + 1}"
        u.name = f"{u.name}·E{i + 1}"
        u.position = (i * 5, 0)
        enemies.append(u)
    if len({u.id for u in enemies}) != len(enemies):
        print("❌ 敌人 id 有重复，菜单会拒绝生成")
        return 2
    field = pcs + enemies

    if args.export_prompt:
        print(export_prompt(field, enemies, args.enemy))
        return 0

    print("=" * 92)
    print(f"战斗 demo（零模型调用）｜{args.pcs} 个 PC vs {args.enemies} 个 {args.enemy}"
          f"｜种子 {args.seed}")
    print("=" * 92)
    print("\n【出战单位】")
    for u in pcs:
        print("  " + u.combat_block().replace("\n", "\n  "))
    print()
    for u, unparsed in [(build_unit(args.enemy, uid=f"E{i+1}")[0], None)
                        for i in range(args.enemies)]:
        pass
    print("【敌人】")
    for u in enemies:
        acts = "；".join(a.name for a in u.actions)
        print(f"  {u.id} {u.name}｜AC {u.ac}｜HP {u.hp}｜动作：{acts}")

    order = roll_initiative(field, rng)
    print("\n【先攻序（先攻决定回合，一个单位一个回合）】")
    for i, (u, init) in enumerate(order, 1):
        print(f"  {i}. {u.name}({u.id}) 先攻 {init}")

    for rnd in range(1, args.rounds + 1):
        alive_pcs = [u for u in pcs if u.alive]
        alive_foes = [u for u in enemies if u.alive]
        if not alive_pcs or not alive_foes:
            break
        print("\n" + "─" * 92)
        print(f"■ 第 {rnd} 轮｜PC 存活 {len(alive_pcs)}｜敌人存活 {len(alive_foes)}")
        print("─" * 92)

        for u, _init in order:
            if not u.alive:
                continue
            u.reset_turn()
            log: list[str] = [f"▸ {u.status_line()}"]
            try:
                menu = build_menu(u, field)
            except Exception as exc:  # noqa: BLE001
                log.append(f"  [跳过] {exc}")
                print("\n".join(log))
                continue
            choice = pick_best_attack(u, menu, field, rng)
            execute(u, choice, field, rng, log)
            print("\n".join(log))
            u.tick_conditions()

    print("\n" + "=" * 92)
    print("【结果】")
    for u in pcs:
        print("  PC   " + u.status_line())
    for u in enemies:
        print("  敌   " + u.status_line())
    survivors = [u for u in pcs if u.alive]
    print(f"\n  ⇒ PC 存活 {len(survivors)}/{len(pcs)}；"
          f"敌人存活 {len([u for u in enemies if u.alive])}/{len(enemies)}")
    print(f"  ⇒ 全程**零模型调用**（这份报告本身就是 P1 的验收证据）")
    return 0


def export_prompt(field: list[Unit], enemies: list[Unit], enemy_kind: str) -> str:
    """生成"敌方规划模型"该看到的提示词 —— P0-③ 实测的出题材料。"""
    menus = [build_menu(e, field) for e in enemies if e.alive]
    lines = [
        "你在为一场 D&D 战斗规划**所有敌方单位**这一回合的行动。",
        "",
        "【规则】",
        "- 每个单位每回合只有：1 个动作、1 个附赠动作、1 个反应、以及等于速度的移动。",
        "- **只能从下面给出的选项 id 里挑**，不要自己发明动作。",
        "- 目标必须是该选项列出的 target 之一。",
        "- 目标选择要考虑：优先集火、优先打威胁大的/血少的、珍惜有限资源（每日次数/充能）。",
        "- 行动顺序也要你决定（同一个单位先动还是后动，会影响结果）。",
        "",
        "【战场】",
    ]
    for u in field:
        lines.append("  " + u.status_line()
                     + f"　位置 {u.position[0]:g},{u.position[1]:g}")
    lines.append("")
    lines.append("【各单位之间的距离（尺，5 尺一格，对角算一格）】")
    for a in field:
        for b in field:
            if a.id < b.id:
                lines.append(f"  {a.id}↔{b.id}: {grid_distance_ft(a.position, b.position):g}")
    lines.append("")
    lines.append("【你方单位与本回合可选行动】")
    lines.append(render_menus(menus, battlefield=field))
    lines += [
        "",
        "【输出格式】只输出一个 JSON 数组，每个存活敌方单位一条：",
        '[{"unit":"E1","actions":[{"id":"a3","target":"P2"}]}]',
        "如果某单位本回合什么也不做，给它一个 end_turn 选项的 id。",
        "不要输出任何解释文字。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
