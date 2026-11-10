# coding=utf-8
"""U12 回归：钱包 / 物品数量 / 消耗品 / 买卖 / 网页动作 / 商店播种。

守的口径（写在 `engine/rules/economy.py` 顶部）：

    · 钱和数量都是状态的一部分，改动全走 commit（有审计）；
    · 钱不够就报错，不扣成负数；买东西扣钱+给货在**同一事务**里；
    · 治疗量只从 **SRD 原文的表格**抽，抽不到就说"需 DM 裁定"；
    · SRD 没给价格的（强效/高等/至强药水）**不猜**，要买必须 DM 报价；
    · 战斗中喝药还没接进动作菜单 → 明确拒绝，不假装成功。
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import AdventureRunner, load_adventure  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.persist import load_game, save_game  # noqa: E402
from engine.rules import economy  # noqa: E402
from engine.rules.economy import EconomyError  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, World, new_character  # noqa: E402
from engine.web_actions import ActionError, apply_action  # noqa: E402

FAILS: list[str] = []
COUNT = 0
SAMPLE = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
WHO = "甲"


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


def make_state(hp=20, cur=None) -> StateTree:
    st = StateTree.from_template(
        {WHO: new_character(WHO, level=3, job="牧师", abilities=AB,
                            hp_max=hp, ac=14)},
        current=WHO)
    if cur is not None:
        st.commit([{"op": "replace", "path": f"/角色列表/{WHO}/生命值/当前",
                    "value": int(cur)}], actor="测试", reason="set hp")
    return st


def make_loop(st: StateTree) -> GameLoop:
    led = Ledger()
    table = TableClient(dm=FakeModel(), planner=FakeModel(),
                        players={WHO: FakeModel()})
    loop = GameLoop(st, led, table, rng=Random(20261106))
    loop.set_field([make_hero_unit(st, WHO)])
    return loop


def give_potion(st: StateTree, qty: int = 2) -> None:
    economy.give_item(st, WHO, "治疗药水",
                      {"类别": "消耗品", "已装备": False,
                       "英文名": "Potion of Healing"},
                      qty=qty, actor="测试", reason="发药")


def open_shop(st: StateTree, stock=None) -> None:
    w = World(st).ensure()
    if w.location("村里") is None:
        w.discover("村里")
    w.set_shop("村里", stock or ["Potion of Healing"], actor="测试")


# --------------------------------------------------------------------------- #
section("A. 钱包：加/扣/不赊账")

st = make_state()
ok(economy.wallet(st, WHO) == {"金币": 0, "银币": 0, "铜币": 0},
   "A1 新角色钱袋是三个零", economy.wallet(st, WHO))
economy.add_coins(st, WHO, 60, actor="测试", reason="开局")
ok(economy.wallet(st, WHO)["金币"] == 60, "A2 加钱进状态",
   economy.wallet(st, WHO))
ok(any("开局" in e.reason for e in st.audit), "A3 加钱留审计",
   [e.line() for e in st.audit][-2:])

economy.spend_coins(st, WHO, 10, actor="测试")
ok(economy.wallet(st, WHO)["金币"] == 50, "A4 扣钱后余额对",
   economy.wallet(st, WHO))
before = economy.wallet(st, WHO)
ok(raises(lambda: economy.spend_coins(st, WHO, 999, actor="测试"), EconomyError),
   "A5 钱不够 → 报错")
ok(economy.wallet(st, WHO) == before, "A5b ⭐ 失败不扣钱（不赊账）",
   economy.wallet(st, WHO))
ok(raises(lambda: economy.add_coins(st, WHO, -5), EconomyError),
   "A6 加负数是错的（扣钱要走 spend）")
ok(raises(lambda: economy.add_coins(st, WHO, 5, coin="钻石"), EconomyError),
   "A7 不认识的币种 → 报错")
ok(raises(lambda: economy.add_coins(st, WHO, 5.5), EconomyError),
   "A8 金额不是整数 → 报错（不 round）")

st_bad = make_state()
st_bad.commit([{"op": "replace", "path": f"/角色列表/{WHO}/钱币/金币",
                "value": "10"}], actor="测试", reason="写坏")
ok(raises(lambda: economy.wallet(st_bad, WHO), EconomyError),
   "A9 钱袋里是字符串 → 报错（不当成 10）")


# --------------------------------------------------------------------------- #
section("B. 物品数量：增减到 0 就删")

st = make_state()
economy.give_item(st, WHO, "火把", {"类别": "杂物"}, qty=3, actor="测试")
ok(economy.item_qty(economy.item_entry(st, WHO, "火把")) == 3,
   "B1 新建物品带数量")
economy.give_item(st, WHO, "火把", {}, qty=2, actor="测试")
ok(economy.item_qty(economy.item_entry(st, WHO, "火把")) == 5,
   "B2 ⭐ 再给同类物品是累加数量，不是覆盖")
economy.take_item(st, WHO, "火把", qty=2, actor="测试")
ok(economy.item_qty(economy.item_entry(st, WHO, "火把")) == 3,
   "B3 取走 2 个还剩 3 个")
economy.take_item(st, WHO, "火把", qty=3, actor="测试")
ok(economy.item_entry(st, WHO, "火把") is None,
   "B4 ⭐ 数量归零 → 整条移出背包（不留数量 0 的幽灵）")
ok(raises(lambda: economy.take_item(st, WHO, "火把", qty=1), EconomyError),
   "B5 背包没有的不能拿走")
economy.give_item(st, WHO, "绳子", {"类别": "杂物"}, qty=1, actor="测试")
economy.adjust_item(st, WHO, "绳子", 2, actor="测试")
ok(economy.item_qty(economy.item_entry(st, WHO, "绳子")) == 3,
   "B6 调整数量 +2")
ok(raises(lambda: economy.adjust_item(st, WHO, "不存在的", 1), EconomyError),
   "B7 给不存在的物品加数量 → 报错")
ok(raises(lambda: economy.give_item(st, WHO, "x", {}, qty=0), EconomyError),
   "B8 数量必须正整数")


# --------------------------------------------------------------------------- #
section("C. 消耗品：治疗量只从 SRD 原文抽")

potions = economy.healing_potions()
ok(bool(potions), "C1 ⭐ SRD 治疗药水表解析成功", list(potions))
ok(potions.get("Potion of Healing", {}).get("治疗") == "2d4+2",
   "C2 治疗药水 = 2d4+2", potions.get("Potion of Healing"))
ok(potions.get("Potion of Greater Healing", {}).get("治疗") == "4d4+4",
   "C3 强效治疗药水 = 4d4+4")
ok(potions.get("Potion of Healing", {}).get("价格") == 50,
   "C4 SRD 给了价格：50 金币", potions.get("Potion of Healing"))
ok(potions.get("Potion of Greater Healing", {}).get("价格") is None,
   "C5 ⭐ SRD 没给价的（0.00）不许当成免费")
ok(raises(lambda: economy.consumable_spec("超级回血水"), EconomyError),
   "C6 不认识的消耗品 → 明说需 DM 裁定")

st = make_state(hp=20, cur=10)
give_potion(st, 2)
out = economy.use_consumable(st, WHO, "治疗药水", rng=Random(3), actor="测试")
ok(4 <= out["healed"] <= 10, "C7 2d4+2 的治疗量在 4~10 之间", out)
ok(st.read(f"/角色列表/{WHO}/生命值/当前") == 10 + out["healed"],
   "C8 血量真的加上去了", st.read(f"/角色列表/{WHO}/生命值/当前"))
ok(out["left"] == 1, "C9 药水数量 -1", out)
economy.use_consumable(st, WHO, "治疗药水", rng=Random(4), actor="测试")
ok(economy.item_entry(st, WHO, "治疗药水") is None,
   "C10 最后一瓶喝完 → 移出背包")

st = make_state(hp=20, cur=19)
give_potion(st, 1)
out = economy.use_consumable(st, WHO, "治疗药水", rng=Random(5), actor="测试")
ok(out["hp"] == 20 and out["浪费"] >= 1,
   "C11 回血封顶最大 HP，多出来的记成浪费", out)
ok(raises(lambda: economy.use_consumable(
        make_state(), WHO, "x", rng=Random(1)), EconomyError),
   "C12 没有的东西不能喝")

st = make_state()
economy.give_item(st, WHO, "治疗药水",
                  {"类别": "消耗品", "英文名": "Potion of Healing"},
                  qty=1, actor="测试")
out = economy.use_consumable(st, WHO, "治疗药水", rng=Random(6), actor="测试")
ok(out["formula"] == "2d4+2",
   "C13 ⭐ 物品没写效果时按中文名回查 SRD（不静默 0 回血）", out)


# --------------------------------------------------------------------------- #
section("D. 买卖：钱货一个事务，价格不猜")

st = make_state()
make_loop(st)
economy.add_coins(st, WHO, 60, actor="测试")
ok(raises(lambda: economy.buy(st, WHO, "Potion of Healing"), EconomyError),
   "D1 没有商店不能凭空买")

open_shop(st)
ok(raises(lambda: economy.buy(st, WHO, "Potion of Greater Healing"),
          EconomyError),
   "D2 货架上没有的不能买")
ok(raises(lambda: economy.buy(st, WHO, "Potion of Healing", qty=2), EconomyError),
   "D3 钱不够 → 报错")
ok(economy.item_entry(st, WHO, "Potion of Healing") is None,
   "D3b ⭐ 失败不会给货（钱货同一事务）")
buy = economy.buy(st, WHO, "Potion of Healing", qty=1, actor="测试")
ok(buy["paid"] == 50 and buy["left_gold"] == 10, "D4 买成，余额对", buy)
item = economy.item_entry(st, WHO, "Potion of Healing") or {}
ok(item.get("类别") == "消耗品" and item.get("效果", {}).get("治疗") == "2d4+2",
   "D5 买到的药水带 SRD 效果", item)
ok(economy.item_qty(item) == 1, "D5b 数量 1")
economy.add_coins(st, WHO, 50, actor="测试")
economy.buy(st, WHO, "Potion of Healing", qty=1, actor="测试")
ok(economy.item_qty(economy.item_entry(st, WHO, "Potion of Healing")) == 2,
   "D6 ⭐ 买第二瓶是累加，不是覆盖")

open_shop(st, ["Potion of Healing", "Potion of Greater Healing"])
ok(raises(lambda: economy.buy(st, WHO, "Potion of Greater Healing",
                              unit_price=None), EconomyError),
   "D7 SRD 没给价的要 DM 报价")
economy.add_coins(st, WHO, 200, actor="测试")
b2 = economy.buy(st, WHO, "Potion of Greater Healing", unit_price=150,
                 actor="测试")
ok(b2["paid"] == 150, "D8 显式报价可以买", b2)

sell = economy.sell(st, WHO, "Potion of Healing", qty=1, actor="测试")
ok(sell["earned"] == 25, "D9 ⭐ 卖出默认半价（50→25）", sell)
ok(economy.item_qty(economy.item_entry(st, WHO, "Potion of Healing")) == 1,
   "D9b 卖出后数量减 1")
ok(raises(lambda: economy.sell(st, WHO, "不存在的"), EconomyError),
   "D10 卖背包里没有的 → 报错")
ok(any(("购买" in e.reason or "买入" in e.reason) for e in st.audit),
   "D11 ⭐ 买卖有审计", [e.line() for e in st.audit][-2:])
ok(raises(lambda: economy.sell(
        st, WHO, "Potion of Healing", qty=1, unit_price="3"), EconomyError),
   "D12 单价是字符串 → 报错（不当成 3）")


# --------------------------------------------------------------------------- #
section("E. 网页动作：喝药 / 买卖（战斗里拒绝）")

st = make_state(hp=20, cur=8)
loop = make_loop(st)
give_potion(st, 1)
res = apply_action(st, loop, WHO,
                   {"kind": "use_item", "name": "治疗药水"})
ok(res["ok"] and "喝下" in res["message"], "E1 网页喝药成功", res)
unit = next(u for u in loop.field if u.name == WHO)
ok(unit.hp == int(st.read(f"/角色列表/{WHO}/生命值/当前")),
   "E2 ⭐ 单位 HP 与 stat_data 同步", (unit.hp,
                                     st.read(f"/角色列表/{WHO}/生命值/当前")))

ok(raises(lambda: apply_action(st, loop, WHO,
                               {"kind": "use_item", "name": "不存在"}),
          ActionError),
   "E3 背包没有的药 → ActionError")

loop.start_combat(enemies=[("Bandit", 1)], distance=20)
ok(raises(lambda: apply_action(st, loop, WHO,
                               {"kind": "use_item", "name": "治疗药水"}),
          ActionError),
   "E4 ⭐ 战斗中喝药明确拒绝（还没接动作菜单）")
lc = int(st.read(f"/角色列表/{WHO}/生命值/当前"))
ok(raises(lambda: apply_action(st, loop, WHO,
                               {"kind": "buy_item",
                                "name": "Potion of Healing"}), ActionError),
   "E5 战斗中不能交易")
ok(int(st.read(f"/角色列表/{WHO}/生命值/当前")) == lc,
   "E6 被拒绝的动作没有改状态")

st = make_state()
loop = make_loop(st)
economy.add_coins(st, WHO, 60, actor="测试")
open_shop(st)
res = apply_action(st, loop, WHO, {"kind": "buy_item",
                                   "name": "Potion of Healing", "qty": 1})
ok(res["ok"] and economy.wallet(st, WHO)["金币"] == 10,
   "E7 网页买药成功", res)
res = apply_action(st, loop, WHO, {"kind": "sell_item",
                                   "name": "Potion of Healing", "qty": 1})
ok(res["ok"] and economy.wallet(st, WHO)["金币"] == 35,
   "E8 网页卖药成功（半价）", res)


# --------------------------------------------------------------------------- #
section("F. 剧本播种：商店挂在世界树上")

adv = load_adventure(SAMPLE)
village = next(p for p in adv.map["places"] if p["name"] == "村里")
ok(village.get("shop") == ["Potion of Healing"],
   "F1 示例剧本把药水货架挂在村里", village.get("shop"))

st = make_state()
loop = make_loop(st)
runner = AdventureRunner(loop, adv)
runner.seed_map()
ok(World(st).shop_items("村里") == ["Potion of Healing"],
   "F2 ⭐ 播种后世界树上有货架", World(st).shop_items("村里"))
runner.seed_map()
ok(World(st).shop_items("村里") == ["Potion of Healing"],
   "F3 重复播种不叠货架")
ok(economy.shop_stock(st) == ["Potion of Healing"],
   "F4 ⭐ 当前地点在村里 → 货架可读（列表来自世界树）",
   economy.shop_stock(st))
loop.world.discover("废弃哨塔", actor="测试")
loop.world.travel("废弃哨塔", actor="测试")
ok(economy.shop_stock(st) == [],
   "F5 走到没有货架的地点 → 买不了（不凭空进货）",
   economy.shop_stock(st))


# --------------------------------------------------------------------------- #
section("G. 存档往返")

st = make_state(hp=20, cur=12)
loop = make_loop(st)
economy.add_coins(st, WHO, 65, actor="测试")
economy.give_item(st, WHO, "治疗药水",
                  {"类别": "消耗品", "已装备": False}, qty=2, actor="测试")
open_shop(st)
economy.buy(st, WHO, "Potion of Healing", qty=1, actor="测试")
tmp = tempfile.mkdtemp(prefix="dnd-eco-")
try:
    path = os.path.join(tmp, "eco.json")
    save_game(path, state=st, ledger=loop.ledger, loop=loop)
    sess = load_game(path)
    ok(economy.wallet(sess.state, WHO)["金币"] == 15,
       "G1 钱袋随档往返", economy.wallet(sess.state, WHO))
    ok(economy.item_qty(economy.item_entry(sess.state, WHO, "治疗药水")) == 2,
       "G2 药水数量随档往返")
    ok(World(sess.state).shop_items("村里") == ["Potion of Healing"],
       "G3 货架随档往返")
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")