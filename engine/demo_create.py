# coding=utf-8
"""U14 demo：角色创建 / 升级向导（**终端里的交互，零模型调用**）。

    python -m engine.demo_create            # 交互：一步步选职业/种族/属性/技能/法术
    python -m engine.demo_create --auto     # 无人值守：自动建一个 3 级精灵法师并升到 4 级

这不是"第二个真相"：数值全由 `engine/creation.py` 按 SRD 算，
这里只负责问与展示。
"""

from __future__ import annotations

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

from engine.creation import (  # noqa: E402
    CLASS_CN, CreationError, SKILL_CHOICES, class_info, create_character,
    level_up, list_options, roll_abilities, spells_for, species_info,
    standard_array,
)
from engine.dice import Random  # noqa: E402
from engine.state import StateTree  # noqa: E402
from engine.state.patch import escape_token  # noqa: E402

ABILITIES = ("力量", "敏捷", "体质", "智力", "感知", "魅力")


def _pick(prompt: str, items: list[tuple[str, str]]) -> str:
    print(f"\n{prompt}")
    for i, (_en, cn) in enumerate(items, 1):
        print(f"  {i}. {cn}（{_en}）")
    while True:
        raw = input("输入编号（回车=1）：").strip() or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            en, cn = items[int(raw) - 1]
            print(f"  → 选了 {cn}")
            return en
        print("  编号不对，再来一次。")


def _auto_build() -> dict:
    opts = list_options()
    klass = "Wizard"
    species = "Elf"
    base = standard_array()
    # 法师：智力 15、敏捷 14、体质 13、感知 12、魅力 10、力量 8
    order = {"智力": 0, "敏捷": 1, "体质": 2, "感知": 3, "魅力": 4, "力量": 5}
    ab = {k: base[i] for k, i in order.items()}
    return create_character(
        "莉安", klass=klass, species=species, level=3, abilities=ab,
        skills=["奥秘", "调查"],
        spells=["Fire Bolt", "Magic Missile", "Mage Armor"])


def _print_sheet(char: dict) -> None:
    hp = char.get("生命值") or {}
    print(f"\n=== {next(iter(char.get('职业') or {}), '?')} ===")
    print(f"职业：{'、'.join(f'{k} {v}' for k, v in (char.get('职业') or {}).items())}")
    print(f"属性：" + " ".join(f"{k}{char.get('属性', {}).get(k, '?')}"
                              for k in ABILITIES))
    print(f"HP：{hp.get('当前')}/{hp.get('最大')}｜AC {char.get('护甲等级')}"
          f"｜速度 {char.get('速度')}")
    prof = char.get("熟练配置") or {}
    print(f"技能熟练：{prof.get('技能')}｜豁免：{prof.get('豁免')}")
    slots = (char.get("施法") or {}).get("法术位") or {}
    if slots:
        print("法术位：" + "／".join(f"{k}环 {v.get('当前')}/{v.get('最大')}"
                                 for k, v in sorted(slots.items())))
    book = (char.get("施法") or {}).get("法术书") or {}
    if book:
        print("法术书：" + "、".join(book))
    traits = char.get("特性") or {}
    for src, names in traits.items():
        print(f"种族特性（{src}）：" + "、".join(names))


def auto() -> int:
    char = _auto_build()
    _print_sheet(char)
    st = StateTree.from_template({"莉安": char}, current="莉安")
    out = level_up(st, "莉安", abilities={"智力": 16})
    print(f"\n升级：3 → {out['level']} 级，HP +{out['hp_gain']}"
          f"（最大 {out['hp_max']}），法术位 {out['slots']}")
    _print_sheet(st.read("/角色列表/莉安"))
    print("\n✅ 零模型调用、零花费。")
    return 0


def interactive() -> int:
    opts = list_options()
    print("== 角色创建向导（数值全部由 SRD + 规则算）==")
    name = input("\n角色名（回车=无名冒险者）：").strip() or "无名冒险者"
    klass = _pick("选职业：", [(c["en"], c["cn"]) for c in opts["classes"]])
    species = _pick("选种族：", [(s["en"], s["cn"]) for s in opts["species"]])
    level = int(input("\n等级（1~5，回车=1）：").strip() or "1")
    print("\n属性怎么来？")
    print("  1. 标准数组 15/14/13/12/10/8（你自己分配）")
    print("  2. 掷 4d6 取高 3（可复现）")
    mode = input("编号（回车=1）：").strip() or "1"
    if mode == "2":
        ab = roll_abilities(Random(int(input("随机种子（回车=1）：").strip() or "1")))
        print("掷出：" + " ".join(f"{k}{v}" for k, v in ab.items()))
    else:
        base = standard_array()
        ab = {}
        for i, key in enumerate(ABILITIES):
            while True:
                raw = input(f"{key}（可选 {base}，回车=下一个）：").strip()
                if not raw:
                    val = base[i]
                    break
                if raw.isdigit() and int(raw) in base:
                    val = int(raw)
                    break
                print("  只能从剩下的数里挑。")
            ab[key] = val
    info = class_info(klass)
    need = info["skill_choices"]
    print(f"\n技能熟练：{info['cn']} 选 {need} 项")
    for i, s in enumerate(opts["skills"], 1):
        print(f"  {i}. {s['cn']}（{s['en']}）")
    raw = input(f"输入 {need} 个编号（逗号分隔）：").strip()
    picks = [int(x) for x in raw.replace("，", ",").split(",") if x.strip().isdigit()]
    skills = [opts["skills"][i - 1]["en"] for i in picks if 1 <= i <= len(opts["skills"])]
    spells: list[str] = []
    if info["caster"] == "FULL":
        avail = spells_for(klass, max_level=min(level, 5))
        print("\n法术（可留空；从下面挑，输入名字，逗号分隔）")
        print("  可选：" + "、".join(s["name"] for s in avail))
        spells = [x.strip() for x in input("法术：").split(",") if x.strip()]
    try:
        char = create_character(name, klass=klass, species=species, level=level,
                                abilities=ab, skills=skills, spells=spells)
    except CreationError as exc:
        print(f"\n❌ 建卡失败：{exc}")
        return 1
    _print_sheet(char)
    st = StateTree.from_template({name: char}, current=name)
    answer = input("\n现在升一级试试？（y/N）：").strip().lower()
    if answer == "y":
        out = level_up(st, name)
        print(f"升级到 {out['level']} 级，HP +{out['hp_gain']}")
        _print_sheet(st.read(f"/角色列表/{escape_token(name)}"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true")
    args = ap.parse_args()
    return auto() if args.auto else interactive()


if __name__ == "__main__":
    raise SystemExit(main())