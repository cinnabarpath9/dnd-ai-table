# coding=utf-8
"""变异验证：把守卫**改回坏写法**，确认对应的断言真的会红。

来源：狼人杀项目的交接主文档 §6 ——
「新写的修复必须配回归，并做一次变异验证（把修复改回旧写法，确认测试变红）——
否则你不知道那条断言是不是『永远为真』。」

本脚本**不改任何文件**：运行时 monkeypatch，跑完立刻恢复。
九个变异（每个都要求：正常实现下守卫有效；改坏之后**必须**变红）：

    M1 可见性闸门恒真          → 玩家能看到 DM 秘密
    M2 台账折叠失效            → 最早的轮次又回到渲染里
    M3 线索可以重复发现        → 同一人第二次还能撞上同一条
    M4 召回退回"按时间远近"    → 一分钟前的无关事挤掉两年前的欠条
    M5 lint 守卫失效           → 故意的公开泄漏抓不到
    M6 存档版本闸门失效        → 不匹配的旧档被当成能读
    M7 先攻加值不算进总值      → 敏捷高低不影响先攻
    M8 线索的私有可见性失效    → 只有甲的发现被别人"想起来"
    M9 动作经济再校验失效      → 动作已用还能再做一个
    M10 法术位先扣后校验        → 施法失败也扣掉法术位
    M11 濒死不掷死亡豁免        → 被敌人打倒的 PC 永远躺着
    M12 专注检定恒成功          → 受伤也不掉专注
    M13 重试把非 retryable 也重试 → 真 bug 被重试掩盖
    M14 出口闸放行一切        → 测试可以偷偷真联网
    M15 先攻同分不看敏捷      → 同分顺序退化成"按 id"
    M16 存档丢掉审计          → 谁改了什么查不到
    M17 规则检索无视字数预算  → 规则块把上下文撑爆
    M18 engine 档对 DM 放行   → 连 DM 都能看到暗骰明细
    M19 法术位一次扣两格      → 施法一次资源没了
    M20 先攻同分顺序反转      → 同分同敏捷时顺序不稳定
    M21 骰式 keep 失效        → 4d6kh3 变成 4d6，取高规则没了
    M22 治疗只认活目标        → 倒地队友选不到、救不起来
    M23 倒地仍掷专注检定      → 击杀专注者抛异常/吞掉击杀
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

import engine.dice.roller as roller_mod  # noqa: E402
import engine.netguard as netguard_mod  # noqa: E402
from engine.netguard import NetGuard, NetworkBlocked  # noqa: E402
import engine.persist as persist_mod  # noqa: E402
from engine.rules import rules_text as rules_text_mod  # noqa: E402
from engine.rules import narrate_lint  # noqa: E402
from engine.models.client import (  # noqa: E402
    ModelClient, ModelError, ModelReply, ModelRequest, RetryPolicy,
)
import re  # noqa: E402
import engine.rules.bestiary as bestiary_mod  # noqa: E402
from engine.rules.bestiary import build_unit  # noqa: E402
from engine.verify_isolation import build_scenario, collect_prompts  # noqa: E402
import engine.rules.loop as loop_mod  # noqa: E402
import engine.rules.adjudicate as adjudicate_mod  # noqa: E402
import engine.rules.menu as menu_mod  # noqa: E402
import engine.rules.turn as turn_mod  # noqa: E402
import engine.rules.effects as effects_mod  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from engine.rules.spells import Spell, get_spell  # noqa: E402
from engine.dice import AbilityCheck, Random, roll_d20  # noqa: E402
from engine.longrun import build_table_loop  # noqa: E402
from engine.memory import MemoryStore, Query, parse_game_time, recall  # noqa: E402
import sys as _sys  # noqa: E402
recall_mod = _sys.modules["engine.memory.recall"]  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.clues import ClueOutcome, SceneClue  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.rules.turn import execute_intent  # noqa: E402
from engine.rules.unit import Action, Attack, DamagePacket, Unit  # noqa: E402
import engine.rules.item_effects as item_effects_mod  # noqa: E402
import engine.rules.effect_catalog as catalog_mod  # noqa: E402
import engine.rules.armor as armor_mod  # noqa: E402
import engine.state.patch as patch_mod  # noqa: E402
from engine.state.patch import PatchOpError, apply_patch  # noqa: E402
import engine.webui as webui_mod  # noqa: E402
import engine.models.client as client_mod  # noqa: E402
import engine.state.world as world_mod  # noqa: E402
import engine.state.visibility as vis_mod  # noqa: E402
from engine.persist import (  # noqa: E402
    _action_from_dict, _action_to_dict, _spell_from_dict, _spell_to_dict,
)
from engine.rules.menu import build_menu  # noqa: E402
from engine.models.client import Intent  # noqa: E402
from engine.state import Ledger, StateError, StateTree, new_character  # noqa: E402
import engine.state.tree as tree_mod  # noqa: E402
import engine.web_actions as wa_mod  # noqa: E402
import engine.rules.armor as armor_mod  # noqa: E402
import engine.rules.economy as economy_mod  # noqa: E402
import engine.creation as creation_mod  # noqa: E402
import engine.rules.spells as spells_mod  # noqa: E402
import engine.state.visibility as visibility_mod  # noqa: E402
from engine.state.visibility import Visibility, lint_leaks  # noqa: E402
from engine.state.world import World, new_world  # noqa: E402

FAILS: list[str] = []
COUNT = 0

AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def mutate(target_obj, name: str, replacement):
    """替换属性，返回还原函数。**不改文件、进程内生效。**"""
    original = getattr(target_obj, name)
    setattr(target_obj, name, replacement)
    return lambda: setattr(target_obj, name, original)


# --------------------------------------------------------------------------- #
section("M1 可见性闸门：DM 秘密不许进玩家视角")

led = Ledger()
led.secret("公告", "队长是内应，他是双面间谍，今晚要动手。")
led.public("公告", "天亮了，大家集合。")


def m1_probe() -> bool:
    """正常实现下：玩家的可见条目里没有 DM 秘密。"""
    return not any("内应" in e.text for e in led.visible_to("甲"))


ok(m1_probe(), "M1a 正常实现：玩家看不到 DM 秘密")
restore = mutate(Visibility, "allows", lambda self, viewer: True)
try:
    m1_caught = not m1_probe()
finally:
    restore()
ok(m1_caught, "M1b ⭐ 把闸门改成恒真 → 守卫立刻抓到（变异被抓）")
ok(m1_probe(), "M1c 还原之后又正常（没污染后续用例）")


# --------------------------------------------------------------------------- #
section("M2 台账折叠：只渲染最近 6 轮")

led2 = Ledger()
for r in range(1, 21):
    led2.public("公告", f"第 {r} 轮的事，写得长一点。", section="公告", round=r)


def m2_probe() -> bool:
    text = led2.render("甲", last_rounds=6)
    return "第 1 轮" not in text and "已折叠" in text


ok(m2_probe(), "M2a 正常实现：第 1 轮被折叠、并写明折叠了多少")
_orig_render = Ledger.render


def _bad_render(self, viewer, **kw):
    kw.pop("last_rounds", None)          # 坏写法：忽略折叠参数
    return _orig_render(self, viewer, **kw)


restore = mutate(Ledger, "render", _bad_render)
try:
    m2_caught = not m2_probe()
finally:
    restore()
ok(m2_caught, "M2b ⭐ 折叠参数被忽略 → 守卫立刻抓到")


# --------------------------------------------------------------------------- #
section("M3 线索：同一个人不能重复发现同一条")


def _mk_clue_loop():
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=AB,
                             hp_max=20, ac=14)},
        current="甲", world=new_world(date="1492-09-17", period="午后"))
    w = World(st).ensure()
    w.discover("哨塔", description="塔门半开。")
    w.travel("哨塔", actor="DM")
    loop = GameLoop(st, Ledger(), FakeModel(name="mutation"), rng=Random(3))
    loop.set_field([make_hero_unit(st, "甲")])
    u = loop.find_unit("甲")
    u.skill_bonuses["perception"] = 30          # 保证成功，专测"能不能再来一次"
    clue = SceneClue(id="c1", text="墙角有一道新的刮痕，指向楼梯口。",
                     skill="perception", dc=14)
    w.register_clue("c1", place="哨塔", skill="perception", dc=14, text=clue.text)
    loop.set_scene_context(scene_id="s1", clues=(clue,))
    return loop, u


def m3_probe() -> bool:
    loop, u = _mk_clue_loop()
    first = loop._resolve_look_search(u, "look")
    second = loop._resolve_look_search(u, "look")
    return (first.clue is not None and first.clue.found
            and second.clue is None)


ok(m3_probe(), "M3a 正常实现：第一次发现、第二次不再撞上同一条")
_orig_eligible = GameLoop._eligible_clue


def _bad_eligible(self, unit, action):
    for c in self.scene_clues:                  # 坏写法：不管已发现/已试过
        if c.action == action:
            return c
    return None


restore = mutate(GameLoop, "_eligible_clue", _bad_eligible)
try:
    m3_caught = not m3_probe()
finally:
    restore()
ok(m3_caught, "M3b ⭐ 去掉『已发现/已试过』判断 → 守卫立刻抓到")


# --------------------------------------------------------------------------- #
section("M4 召回：按内容挑，不按时间远近")

mem = MemoryStore(label="mutation")
near = mem.record("一分钟前，路人甲打了个哈欠。",
                  start=parse_game_time("1492-09-17", "13:59"))
far = mem.record("两年前，磨坊主把欠条押给了你们。",
                 start=parse_game_time("1490-09-17", "午后"),
                 tags=("p2",), people=("磨坊主",))
q = Query(tags=("p2",), viewer="dm",
          now=parse_game_time("1492-09-17", "午后"))


def m4_probe() -> bool:
    return recall(mem, q, count=6).ids() == [far.id]


ok(m4_probe(), "M4a 正常实现：只有两年前那条入选（近的那条无关）")


def _bad_recall(store, query, *, count: int = 6):
    """坏写法：不筛相关性，直接按"距今由近到远"排。"""
    rows = []
    for e in store.visible_to(query.viewer):
        diff, group, show = recall_mod._when(e, query.now)
        rows.append(recall_mod.Recalled(
            entry=e, score=0.0, tier="FALLBACK", kind="补位召回",
            reason="按时间远近", diff_min=diff, group=group, display=show))
    rows.sort(key=lambda r: (r.diff_min if r.diff_min is not None else 10 ** 9))
    return recall_mod.RecallResult(query=query, items=rows[:count],
                                   available=len(rows), asked=count)


ok(_bad_recall(mem, q, count=6).ids() != [far.id],
   "M4b ⭐ 改回『按时间远近』 → 近的无关事挤进来，守卫（M4a）就会红")
ok(_bad_recall(mem, q, count=6).ids()[:1] == [near.id],
   "M4c 坏写法的第一条正是「一分钟前」那条 —— 变异确实换掉了判据")


# --------------------------------------------------------------------------- #
section("M5 lint：故意的公开泄漏必须被抓到")

poison = Ledger()
poison.private("对话", "这是只有甲知道的秘密内容，长度足够触发检查。", "甲")
poison.public("公告", "有人把「这是只有甲知道的秘密内容」说出去了。")


def m5_probe() -> bool:
    return bool(visibility_mod.lint_leaks(poison))


ok(m5_probe(), "M5a 正常实现：公开条目抄了私有内容 → 抓到")
restore = mutate(visibility_mod, "lint_leaks", lambda *a, **k: [])
try:
    m5_caught = not m5_probe()
finally:
    restore()
ok(m5_caught, "M5b ⭐ 把 lint 改成永远空 → 泄漏漏掉，守卫抓到（说明它真在跑）")


# --------------------------------------------------------------------------- #
section("M6 存档版本：不匹配就拒绝，不猜着读")

st6, led6, loop6 = build_table_loop(seed=21)
doc6 = persist_mod.dump_game(state=st6, ledger=led6, loop=loop6)
bad6 = dict(doc6)
bad6["version"] = 999


def m6_probe() -> bool:
    try:
        persist_mod.load_from_dict(bad6, client=FakeModel(name="m6"))
        return False
    except persist_mod.SaveError:
        return True


ok(m6_probe(), "M6a 正常实现：版本 999 的档被拒")
_orig_load = persist_mod.load_from_dict


def _bad_load(d, *, client=None):
    d = dict(d)
    d["version"] = persist_mod.SAVE_VERSION      # 坏写法：跳过版本校验
    return _orig_load(d, client=client)


restore = mutate(persist_mod, "load_from_dict", _bad_load)
try:
    m6_caught = not m6_probe()
finally:
    restore()
ok(m6_caught, "M6b ⭐ 跳过版本校验 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M7 先攻：敏捷加值要进总值")

class _FixedRoll:
    def __init__(self, total: int) -> None:
        self.total = total


def m7_probe() -> bool:
    st, led, loop = build_table_loop(seed=5, names=("甲",))
    u = loop.find_unit("甲")
    u.abilities.pop("敏捷", None)
    u.abilities["dexterity"] = 20               # +5
    _orig_roll = loop_mod.roll_d20
    loop_mod.roll_d20 = lambda rng, mod=0: _FixedRoll(10)   # 骰子固定 10
    try:
        loop.start_combat(enemies=[])
    finally:
        loop_mod.roll_d20 = _orig_roll
    vals = {x.id: v for x, v in loop.initiative}
    return vals.get("甲") == 15                  # 10 + 5，不是裸 10


ok(m7_probe(), "M7a 正常实现：先攻 = 骰面 10 + 敏捷加值 5")
restore = mutate(Unit, "mod", lambda self, ability: 0)
try:
    m7_caught = not m7_probe()
finally:
    restore()
ok(m7_caught, "M7b ⭐ 加值不算 → 先攻只剩裸骰，守卫抓到")


# --------------------------------------------------------------------------- #
section("M8 线索的私有可见性：只有甲能想起")


def m8_probe() -> bool:
    clue = SceneClue(id="mx", text="墙角有一道新的刮痕，指向楼梯口。", secret=True)
    out = ClueOutcome(clue, True, 15, 14)
    mem = MemoryStore()
    mem.record("在哨塔发现：墙角有一道新的刮痕。",
               visible_to=out.visible_to("甲"), key="mx")
    return not mem.visible_to("乙")


ok(m8_probe(), "M8a 正常实现：乙看不到甲发现的秘密痕迹")
restore = mutate(ClueOutcome, "visible_to", lambda self, who: "public")
try:
    m8_caught = not m8_probe()
finally:
    restore()
ok(m8_caught, "M8b ⭐ 秘密痕迹变公开 → 乙能召回，守卫抓到")


# --------------------------------------------------------------------------- #
section("M9 动作经济：动作已用不许再做通用动作")


def _mk_action_loop():
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=AB,
                             hp_max=20, ac=14)},
        current="甲", world=new_world())
    loop = GameLoop(st, Ledger(), FakeModel(name="m9"), rng=Random(7))
    loop.set_field([make_hero_unit(st, "甲")])
    return loop


def m9_probe() -> bool:
    loop = _mk_action_loop()
    u = loop.find_unit("甲")
    foe = Unit(id="F", name="敌", hp=10, hp_max=10, team="enemy",
               position=(5.0, 0.0))
    menu = build_menu(u, [u, foe])
    dodge = next(o for o in menu.options if o.kind == "dodge")
    u.spend("action")
    out = execute_intent(u, Intent(unit=u.id, option_id=dodge.id),
                         menu, [u, foe], Random(7))
    return (not out.ok) and "已用过" in out.blocked


ok(m9_probe(), "M9a 正常实现：动作已用 → 回避被拒")
restore = mutate(turn_mod, "_require_slot", lambda unit, slot, out: True)
try:
    m9_caught = not m9_probe()
finally:
    restore()
ok(m9_caught, "M9b ⭐ 去掉再校验 → 白嫖第二次，守卫抓到")


# --------------------------------------------------------------------------- #
section("M10 施法：先校验后扣法术位（失败不扣）")


def _mk_caster():
    c = Unit(id="C", name="法师", hp=20, hp_max=20, team="pc")
    c.spell_slots[1] = {"current": 1, "max": 1}
    return c


def m10_probe() -> bool:
    c = _mk_caster()
    sp = get_spell("Magic Missile")
    res = adjudicate_mod.cast_spell(c, sp, [], Random(3))   # 没给目标 → 必须失败
    return (not res.ok) and c.spell_slots[1]["current"] == 1


ok(m10_probe(), "M10a 正常实现：施法失败不扣法术位")
_orig_cast = adjudicate_mod.cast_spell


def _bad_cast(caster, spell, targets, rng, **kw):
    lv = kw.get("slot_level") or getattr(spell, "level", 0) or 0
    if lv and not kw.get("free_cast"):
        caster.use_spell_slot(int(lv))          # 坏写法：先扣再校验
    return _orig_cast(caster, spell, targets, rng, **kw)


restore = mutate(adjudicate_mod, "cast_spell", _bad_cast)
try:
    m10_caught = not m10_probe()
finally:
    restore()
ok(m10_caught, "M10b ⭐ 先扣后校验 → 失败也扣位，守卫抓到")


# --------------------------------------------------------------------------- #
section("M11 濒死：0 HP 单位在自己回合掷死亡豁免")


def m11_probe() -> bool:
    st, led, loop = build_table_loop(seed=11, names=("甲",))
    loop.start_combat(enemies=[("Bandit", 1)], distance=20)
    u = loop.find_unit("甲")
    u.hp = 0                                     # 开战后才被打倒
    loop.death.clear()
    try:
        loop.run_combat_round()
    except Exception:                            # noqa: BLE001
        return False
    return u.id in loop.death


ok(m11_probe(), "M11a 正常实现：被打倒的 PC 会掷死亡豁免（记进 loop.death）")
restore = mutate(GameLoop, "_check_death", lambda self, u, log: None)
try:
    m11_caught = not m11_probe()
finally:
    restore()
ok(m11_caught, "M11b ⭐ 跳过濒死判定 → 永远躺着，守卫抓到")


# --------------------------------------------------------------------------- #
section("M12 专注：受伤后检定失败要打断专注")

stub_ok = SimpleNamespace(check=SimpleNamespace(success=True),
                          summary=lambda: "")


def m12_probe() -> bool:
    t = Unit(id="T", name="受术者", hp=30, hp_max=30, team="pc")
    t.concentrating_on = "祝福术"
    line = adjudicate_mod.damage_concentration_line(t, 100, Random(3))
    return bool(line) and not t.concentrating_on


ok(m12_probe(), "M12a 正常实现：受 100 点伤害（DC 50）→ 专注被打断")
restore = mutate(adjudicate_mod, "concentration_check",
                 lambda unit, amount, rng: stub_ok)
try:
    m12_caught = not m12_probe()
finally:
    restore()
ok(m12_caught, "M12b ⭐ 专注检定恒成功 → 怎么打都不掉专注，守卫抓到")


# --------------------------------------------------------------------------- #
section("M13 重试：非 retryable 的错误不许重试（别掩盖真 bug）")


class _Flaky(ModelClient):
    def __init__(self, outcomes, **kw):
        kw.setdefault("enabled", True)
        super().__init__(name="flaky-m", **kw)
        self.outcomes = list(outcomes)
        self.seen = 0

    def _complete(self, req):
        self.seen += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


_REQ = ModelRequest(role="dm", actor="dm", task="t", prompt="x")


def m13_probe() -> bool:
    c = _Flaky([ModelError("JSON 坏了，重试也没用", retryable=False),
                ModelReply(text="ok")],
               policy=RetryPolicy(max_attempts=3))
    r = c.complete(_REQ)
    return (not r.ok) and c.seen == 1


ok(m13_probe(), "M13a 正常实现：非 retryable 只发 1 次、不重试")
_orig_err_init = ModelError.__init__


def _bad_err_init(self, message="", *, retryable=False):
    _orig_err_init(self, message, retryable=True)      # 坏写法：什么都可重试


restore = mutate(ModelError, "__init__", _bad_err_init)
try:
    m13_caught = not m13_probe()
finally:
    restore()
ok(m13_caught, "M13b ⭐ 什么都当可重试 → 真 bug 被掩盖，守卫抓到")


# --------------------------------------------------------------------------- #
section("M14 出口闸：外连必须被拦，不能放行")


def m14_probe() -> bool:
    g = NetGuard()
    g.active = True
    try:
        g.check("example.com", 80, "connect")
        return False
    except NetworkBlocked:
        return True


ok(m14_probe(), "M14a 正常实现：外连被拦（只放行回环）")
restore = mutate(NetGuard, "check", lambda self, host, port=0, via="": None)
try:
    m14_caught = not m14_probe()
finally:
    restore()
ok(m14_caught, "M14b ⭐ 闸门放行一切 → 离线就成了空话，守卫抓到")


# --------------------------------------------------------------------------- #
section("M15 先攻同分：按敏捷调整值排，不能退化成按 id")


def m15_probe() -> bool:
    st, led, loop = build_table_loop(seed=5, names=("A", "B"))
    a = loop.find_unit("A")
    b = loop.find_unit("B")
    a.abilities.pop("敏捷", None)
    b.abilities.pop("敏捷", None)
    a.abilities["dexterity"] = 10          # +0
    b.abilities["dexterity"] = 20          # +5
    _orig_roll = loop_mod.roll_d20
    loop_mod.roll_d20 = lambda rng, mod=0: _FixedRoll(10)   # 两边骰面相同
    try:
        loop.start_combat(enemies=[])
    finally:
        loop_mod.roll_d20 = _orig_roll
    order = [u.id for u, _v in loop.initiative]
    return order.index("B") < order.index("A")             # 高敏捷先动


ok(m15_probe(), "M15a 正常实现：同分时高敏捷先动")
restore = mutate(Unit, "mod", lambda self, ability: 0)
try:
    m15_caught = not m15_probe()
finally:
    restore()
ok(m15_caught, "M15b ⭐ 敏捷不再影响同分排序 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M16 存档：审计流水要跟着走")

st16, led16, loop16 = build_table_loop(seed=9, names=("甲",))
st16.commit([{"op": "replace", "path": "/角色列表/甲/生命值/当前",
              "value": 1}], actor="测试", reason="审计测试")
_before = len(st16.audit)


def m16_probe() -> bool:
    doc = persist_mod.dump_game(state=st16, ledger=led16, loop=loop16)
    sess = persist_mod.load_from_dict(doc, client=FakeModel(name="m16"))
    reasons = [e.reason for e in sess.state.audit]
    return len(sess.state.audit) == _before and "审计测试" in reasons


ok(m16_probe(), "M16a 正常实现：审计流水随档往返")
_orig_dump = persist_mod.dump_game


def _bad_dump(**kw):
    d = _orig_dump(**kw)
    d["state"].pop("audit", None)          # 坏写法：存档不带审计
    return d


restore = mutate(persist_mod, "dump_game", _bad_dump)
try:
    m16_caught = not m16_probe()
finally:
    restore()
ok(m16_caught, "M16b ⭐ 存档丢审计 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M17 规则检索：字数预算要生效，装不下要写明")


def m17_probe() -> bool:
    text = rules_text_mod.build_rules_text(phase="explore", max_chars=400)
    return len(text) <= 400 and "未收入" in text


ok(m17_probe(), "M17a 正常实现：400 字预算内 + 写明还有几条没收入")
_orig_build = rules_text_mod.build_rules_text


def _bad_build(**kw):
    kw["max_chars"] = 100000               # 坏写法：预算形同虚设
    return _orig_build(**kw)


restore = mutate(rules_text_mod, "build_rules_text", _bad_build)
try:
    m17_caught = not m17_probe()
finally:
    restore()
ok(m17_caught, "M17b ⭐ 无视预算 → 规则块撑爆，守卫抓到")


# --------------------------------------------------------------------------- #
section("M18 engine 档：连 DM 都不给（暗骰明细）")


def m18_probe() -> bool:
    v = Visibility("engine")
    return (not v.allows("dm")) and (not v.allows("甲")) and v.allows("engine")


ok(m18_probe(), "M18a 正常实现：engine 档对 DM 与玩家都不可见")
_orig_allows = Visibility.allows


def _bad_allows(self, viewer):
    if viewer == "dm":                     # 坏写法：DM 也能看 engine 档
        return True
    return _orig_allows(self, viewer)


restore = mutate(Visibility, "allows", _bad_allows)
try:
    m18_caught = not m18_probe()
finally:
    restore()
ok(m18_caught, "M18b ⭐ 闸门对 DM 放行 → 暗骰明细泄露，守卫抓到")


# --------------------------------------------------------------------------- #
section("M19 法术位：成功只扣一格")


def m19_probe() -> bool:
    c = Unit(id="C", name="法师", hp=20, hp_max=20, team="pc")
    c.spell_slots[1] = {"current": 2, "max": 2}
    t = Unit(id="T", name="靶", hp=20, hp_max=20, team="enemy",
             position=(5.0, 0.0))
    res = adjudicate_mod.cast_spell(c, get_spell("Magic Missile"), [t],
                                    Random(3))
    return res.ok and res.slot_consumed and c.spell_slots[1]["current"] == 1


ok(m19_probe(), "M19a 正常实现：施法成功扣 1 格（2 → 1）")
_orig_slot = Unit.use_spell_slot


def _bad_slot(self, level):
    ok_ = _orig_slot(self, level)
    rec = self.spell_slots.get(int(level))
    if ok_ and rec and rec.get("current", 0) > 0:
        rec["current"] -= 1                    # 坏写法：一次扣两格
    return ok_


restore = mutate(Unit, "use_spell_slot", _bad_slot)
try:
    m19_caught = not m19_probe()
finally:
    restore()
ok(m19_caught, "M19b ⭐ 一次扣两格 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M20 先攻同分：顺序必须稳定（按 id）")


def m20_probe() -> bool:
    st, led, loop = build_table_loop(seed=5, names=("B", "A"))
    for n in ("B", "A"):
        u = loop.find_unit(n)
        u.abilities.pop("敏捷", None)
        u.abilities["dexterity"] = 14          # 同分同敏捷
    _orig_roll = loop_mod.roll_d20
    loop_mod.roll_d20 = lambda rng, mod=0: _FixedRoll(10)
    try:
        loop.start_combat(enemies=[])
    finally:
        loop_mod.roll_d20 = _orig_roll
    order = [u.id for u, _v in loop.initiative]
    return order[:2] == ["A", "B"]             # 按 id 升序，不随插入顺序


ok(m20_probe(), "M20a 正常实现：同分同敏捷 → 按 id 稳定排序")
_orig_start = GameLoop.start_combat


def _bad_start(self, **kw):
    log = _orig_start(self, **kw)
    self.initiative.reverse()                  # 坏写法：顺序不稳定
    return log


restore = mutate(GameLoop, "start_combat", _bad_start)
try:
    m20_caught = not m20_probe()
finally:
    restore()
ok(m20_caught, "M20b ⭐ 顺序被反转 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M21 骰式 keep：4d6kh3 要真的丢最小那颗")


class _FixedRng:
    def __init__(self, vals) -> None:
        self.vals = list(vals)
        self.i = 0

    def randint(self, a, b):
        v = self.vals[self.i % len(self.vals)]
        self.i += 1
        return v


def m21_probe() -> bool:
    r = roller_mod.roll("4d6kh3", _FixedRng([6, 1, 1, 1]))
    return (r.total == 8 and r.kept and r.kept[0] == [6, 1, 1]
            and r.dropped and r.dropped[0] == [1])


ok(m21_probe(), "M21a 正常实现：6,1,1,1 取高三颗 = 8，丢掉那颗 1")
_orig_roll = roller_mod.roll


def _bad_roll(notation, rng, *a, **kw):
    if "kh" in notation or "kl" in notation:
        notation = notation.split("k")[0]       # 坏写法：忽略 keep
    return _orig_roll(notation, rng, *a, **kw)


restore = mutate(roller_mod, "roll", _bad_roll)
try:
    m21_caught = not m21_probe()
finally:
    restore()
ok(m21_caught, "M21b ⭐ keep 失效 → 4d6 全算，守卫抓到")


# --------------------------------------------------------------------------- #
section("M22 治疗：0 HP 的队友必须在候选里")


def m22_probe() -> bool:
    caster = Unit(id="C", name="牧师", hp=18, hp_max=18, team="pc")
    caster.spells = [get_spell("Cure Wounds")]
    caster.spell_slots[1] = {"current": 1, "max": 1}
    dying = Unit(id="A", name="艾尔玛", hp=0, hp_max=20, team="pc")
    foe = Unit(id="E", name="敌", hp=10, hp_max=10, team="enemy",
               position=(5.0, 0.0))
    menu = build_menu(caster, [caster, dying, foe], include_friendly_targets=True)
    ops = [o for o in menu.options
           if o.kind == "spell" and "Cure Wounds" in o.name]
    return bool(ops) and "A" in ops[0].target_ids


ok(m22_probe(), "M22a 正常实现：0 HP 队友出现在治疗候选里")
_orig_ou = menu_mod._other_units


def _bad_ou(unit, battlefield, *, enemies_only, include_dying=False):
    return _orig_ou(unit, battlefield, enemies_only=enemies_only,
                    include_dying=False)      # 坏写法：又把濒死排除了


restore = mutate(menu_mod, "_other_units", _bad_ou)
try:
    m22_caught = not m22_probe()
finally:
    restore()
ok(m22_caught, "M22b ⭐ 治疗候选排除濒死 → 救不了人，守卫抓到")


# --------------------------------------------------------------------------- #
section("M23 击杀专注者：倒地后不许再掷豁免")


def m23_probe() -> bool:
    dead = Unit(id="D", name="倒地", hp=0, hp_max=20, team="enemy")
    dead.concentrating_on = "祝福术"
    try:
        line = adjudicate_mod.damage_concentration_line(dead, 20, Random(3))
    except Exception:                            # noqa: BLE001
        return False
    return line != "" and dead.concentrating_on == ""


ok(m23_probe(), "M23a 正常实现：倒地只断专注、不掷骰")
_orig_line = adjudicate_mod.damage_concentration_line


def _bad_line(target, amount, rng):
    if amount <= 0 or not getattr(target, "concentrating_on", ""):
        return ""
    adjudicate_mod.concentration_check(target, amount, rng)   # 坏写法：照掷
    return ""


restore = mutate(adjudicate_mod, "damage_concentration_line", _bad_line)
try:
    m23_caught = not m23_probe()
finally:
    restore()
ok(m23_caught, "M23b ⭐ 倒地仍掷豁免 → 抛异常/吞击杀，守卫抓到")


# --------------------------------------------------------------------------- #
section("M24 天然 20 只在攻击骰上自动成功")


def m24_probe() -> bool:
    r = roll_d20(Random(1), 0)
    r.natural, r.kept, r.detail = 20, [[20]], [[20]]
    chk = AbilityCheck(kind="check", label="检定", a=r, bonus=0, dc=100)
    atk = AbilityCheck(kind="attack", label="攻击", a=r, bonus=0, dc=100)
    return (not chk.success) and atk.success


ok(m24_probe(), "M24a 正常实现：检定天然 20 不自动成功，攻击天然 20 必中")


def _bad_success(self):
    if self.forced is not None:
        return bool(self.forced)
    if self.a.is_crit:
        return True
    if self.a.is_crit_fail:
        return False
    return self.total >= self.dc


restore = mutate(AbilityCheck, "success", property(_bad_success))
try:
    m24_caught = not m24_probe()
finally:
    restore()
ok(m24_caught, "M24b ⭐ 天然 20 又管所有检定 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M25 敌方规划只吃 public（战斗子集）")


def m25_probe() -> bool:
    _s, _l, loop, client = build_scenario()
    caps = collect_prompts(loop, client)
    plan = next(c for c in caps if c.role == "planner")
    return ("队长是内应" not in plan.prompt) and ("门口有脚印" in plan.prompt)


ok(m25_probe(), "M25a 正常实现：planner 包无 DM 秘密、有公共线索")
_orig_planner = loop_mod.PLANNER


def _bad_view():
    loop_mod.PLANNER = loop_mod.DM           # 坏写法：又按 DM 视角建包


try:
    _bad_view()
    m25_caught = not m25_probe()
finally:
    loop_mod.PLANNER = _orig_planner
ok(m25_caught, "M25b ⭐ planner 回到 DM 视角 → 秘密泄漏，守卫抓到")


# --------------------------------------------------------------------------- #
section("M26 敌方规划：第 2 轮菜单不能被上一轮吃空")


def m26_probe() -> bool:
    _st, _led, lp = build_table_loop(seed=7, names=("A", "B", "C", "D"))
    lp.start_combat(enemies=[("Bandit", 2)], distance=20)
    lp.run_combat_round(pc_name="A")
    lp.run_combat_round(pc_name="A")
    plans = [r for r in lp.client.reqs if r.task == "plan_all_enemies"]
    last = plans[-1].allowed["units"]
    return any(o["kind"] == "attack" for u in last for o in u["options"])


ok(m26_probe(), "M26a 正常实现：第 2 轮规划仍有攻击选项")
_orig_reset = Unit.reset_turn


def _no_reset(self):
    pass


restore = mutate(Unit, "reset_turn", _no_reset)
try:
    m26_caught = not m26_probe()
finally:
    restore()
ok(m26_caught, "M26b ⭐ 规划前不重置回合 → 攻击选项消失，守卫抓到")


# --------------------------------------------------------------------------- #
section("M27 范围正则：线状/半径写法")


def m27_probe() -> bool:
    act = bestiary_mod.parse_action({
        "name": "酸吐息", "action_type": "ACTION",
        "desc": "a 60-foot-long, 5-foot-wide Line. DC 18 Dexterity saving throw."})
    return act.area_size == 60 and act.area_shape == "line"


ok(m27_probe(), "M27a 正常实现：60-foot-long, 5-foot-wide Line → 60/line")
_OLD_AREA = re.compile(
    r"(\d+)[\s-]*foot\s+(Cone|Sphere|Cube|Line|Emanation|Cylinder)", re.I)
restore = mutate(bestiary_mod, "_RE_AREA", _OLD_AREA)
try:
    m27_caught = not m27_probe()
finally:
    restore()
ok(m27_caught, "M27b ⭐ 退回旧正则 → 线状吐息变单体，守卫抓到")


# --------------------------------------------------------------------------- #
section("M28 充能：没充上不许出现在菜单里")


def m28_probe() -> bool:
    wyrm = build_unit("Ancient Red Dragon")[0]
    breath = next(a for a in wyrm.actions if a.name == "Fire Breath")
    wyrm.resources[f"recharge:{breath.name}"] = False
    tgt = Unit(id="T", name="木桩", hp=100, hp_max=100, team="pc", ac=10,
               position=(10.0, 0.0))
    menu = build_menu(wyrm, [wyrm, tgt])
    return not any(o.name == "Fire Breath" for o in menu.options)


ok(m28_probe(), "M28a 正常实现：充能失败 → 吐息不进菜单")
restore = mutate(menu_mod, "_recharge_ready", lambda u, a: True)
try:
    m28_caught = not m28_probe()
finally:
    restore()
ok(m28_caught, "M28b ⭐ 无视充能 → 菜单空承诺，守卫抓到")


# --------------------------------------------------------------------------- #
section("M29 AoE 豁免：要打到全部候选")


def m29_probe() -> bool:
    big = Unit(id="D", name="龙", hp=200, hp_max=200, team="enemy", ac=18,
               position=(0.0, 0.0))
    big.actions.append(Action(
        name="吐息", slot="action", save_ability="dexterity", save_dc=12,
        save_success_half=True, damage_notation="4d6", damage_type="fire",
        area_size=30, area_shape="cone", structured=True))
    a = Unit(id="A", name="甲", hp=24, hp_max=24, team="pc", ac=12,
             position=(10.0, 0.0))
    b = Unit(id="B", name="乙", hp=24, hp_max=24, team="pc", ac=12,
             position=(20.0, 0.0))
    menu = build_menu(big, [big, a, b])
    opt = next(o for o in menu.options if o.kind == "save_action")
    out = execute_intent(big, Intent(unit="D", option_id=opt.id, target="A"),
                         menu, [big, a, b], Random(7))
    return bool(out.save_results) and len(out.save_results[0].targets) == 2


ok(m29_probe(), "M29a 正常实现：吐息结算两个目标")
_orig_st = turn_mod._save_action_targets


def _bad_st(opt, act, intent, by_id):
    return _orig_st(opt, act, intent, by_id)[:1]   # 坏写法：只打第一个


restore = mutate(turn_mod, "_save_action_targets", _bad_st)
try:
    m29_caught = not m29_probe()
finally:
    restore()
ok(m29_caught, "M29b ⭐ AoE 退回单目标 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M30 Dash：先声明后移动要有双倍预算")


def m30_probe() -> bool:
    u = Unit(id="P", name="跑者", hp=30, hp_max=30, team="pc", ac=12,
             abilities={"力量": 10, "敏捷": 14, "体质": 10, "智力": 10,
                        "感知": 10, "魅力": 10}, position=(0.0, 0.0))
    u.speeds = {"walk": 30}
    foe = Unit(id="E", name="敌", hp=99, hp_max=99, team="enemy", ac=10,
               position=(55.0, 0.0))
    menu = build_menu(u, [u, foe])
    dash = next(o for o in menu.options if o.kind == "dash")
    execute_intent(u, Intent(unit="P", option_id=dash.id), menu, [u, foe],
                   Random(3))
    mv = next(o for o in menu.options if o.kind == "move")
    out = execute_intent(u, Intent(unit="P", option_id=mv.id, target="E"),
                         menu, [u, foe], Random(3))
    return out.moved > u.speed


ok(m30_probe(), "M30a 正常实现：疾走后移动超过基础速度")


def _bad_dash(unit):
    unit.movement_used = max(0.0, unit.movement_used - unit.speed)


restore = mutate(turn_mod, "_grant_dash", _bad_dash)
try:
    m30_caught = not m30_probe()
finally:
    restore()
ok(m30_caught, "M30b ⭐ 退回 max(0,…) → 疾走白给，守卫抓到")


# --------------------------------------------------------------------------- #
section("M31 技能熟练：卡上的标签要算成加值")


def m31_probe() -> bool:
    st = StateTree.from_template(
        {"游荡者": new_character("游荡者", level=5, job="游荡者",
                                abilities=dict(AB), hp_max=30, ac=14)},
        current="游荡者", world=new_world(date="1492-09-17", period="午后"))
    st.commit([{"op": "replace", "path": "/角色列表/游荡者/熟练配置/技能",
                "value": {"隐匿": "熟练"}}], actor="填表", reason="熟练")
    u = make_hero_unit(st, "游荡者")
    return u.skill_bonuses.get("stealth") == 5


ok(m31_probe(), "M31a 正常实现：隐匿熟练 = +5（敏捷 +2 + 熟练 +3）")


def _bad_skill(raw, val, abilities, level):
    return int(val) if isinstance(val, int) else None


restore = mutate(loop_mod, "_skill_bonus", _bad_skill)
try:
    m31_caught = not m31_probe()
finally:
    restore()
ok(m31_caught, "M31b ⭐ 只认 int → 熟练标签被丢，守卫抓到")


# --------------------------------------------------------------------------- #
section("M32 戏法：5 级要变大")


def m32_probe() -> bool:
    return get_spell("Fire Bolt").damage_at(0, 5) == "2d10"


ok(m32_probe(), "M32a 正常实现：Fire Bolt 5 级 2d10")
_orig_damage_at = Spell.damage_at


def _bad_damage_at(self, slot_level, character_level=0):
    return _orig_damage_at(self, slot_level)      # 坏写法：忽略角色等级


restore = mutate(Spell, "damage_at", _bad_damage_at)
try:
    m32_caught = not m32_probe()
finally:
    restore()
ok(m32_caught, "M32b ⭐ 戏法不随等级成长 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M33 升环：固定加值不能丢")


def m33_probe() -> bool:
    return get_spell("Disintegrate").damage_at(7) == "13d6+40"


ok(m33_probe(), "M33a 正常实现：解离术 7 环 13d6+40")
_orig_keep = Spell._keep_flat


def _bad_keep(self, expr):
    return expr                                    # 坏写法：不加回 +40


restore = mutate(Spell, "_keep_flat", _bad_keep)
try:
    m33_caught = not m33_probe()
finally:
    restore()
ok(m33_caught, "M33b ⭐ 升环丢 +40 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M34 专注 DC：封顶 30")


def m34_probe() -> bool:
    u = Unit(id="C", name="专注者", hp=100, hp_max=100, team="pc")
    return adjudicate_mod.concentration_check(u, 100, Random(3)).check.dc == 30


ok(m34_probe(), "M34a 正常实现：100 伤害 → DC 30")
_orig_cc = adjudicate_mod.concentration_check


def _bad_cc(unit, damage_taken, rng):
    return adjudicate_mod.saving_throw(
        unit, "constitution", max(10, int(damage_taken) // 2), rng)


restore = mutate(adjudicate_mod, "concentration_check", _bad_cc)
try:
    m34_caught = not m34_probe()
finally:
    restore()
ok(m34_caught, "M34b ⭐ 不封顶 → DC 50，守卫抓到")


# --------------------------------------------------------------------------- #
section("M35 patch：缺 value 不许静默写 None")


def m35_probe() -> bool:
    try:
        apply_patch({"a": 1}, [{"op": "replace", "path": "/a"}])
    except PatchOpError:
        return True
    return False


ok(m35_probe(), "M35a 正常实现：缺 value → PatchOpError")
restore = mutate(patch_mod, "NEEDS_VALUE", ())
try:
    m35_caught = not m35_probe()
finally:
    restore()
ok(m35_caught, "M35b ⭐ 不校验 value → 静默写 None，守卫抓到")


# --------------------------------------------------------------------------- #
section("M36 存档：投射物字段不能丢")


def m36_probe() -> bool:
    sp = get_spell("Magic Missile")
    pm = sys.modules["engine.persist"]
    back = pm._spell_from_dict(pm._spell_to_dict(sp))
    return back.projectiles == sp.projectiles and back.projectiles > 1


ok(m36_probe(), "M36a 正常实现：魔法飞弹投射物数随档往返")
_orig_spell_from = _spell_from_dict


def _bad_spell_from(d):
    sp = _orig_spell_from(d)
    sp.projectiles = 1
    sp.upcast_projectiles = 0
    return sp


restore = mutate(sys.modules["engine.persist"], "_spell_from_dict",
                 _bad_spell_from)
try:
    m36_caught = not m36_probe()
finally:
    restore()
ok(m36_caught, "M36b ⭐ 读档把飞弹变 1 发 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M37 存档：动作射程不能丢")


def m37_probe() -> bool:
    act = Action(name="吐息", slot="action", save_ability="dexterity",
                 save_dc=15, damage_notation="4d6", damage_type="fire",
                 area_size=30, range_ft=60, structured=True)
    pm = sys.modules["engine.persist"]
    return pm._action_from_dict(pm._action_to_dict(act)).range_ft == 60


ok(m37_probe(), "M37a 正常实现：动作射程 60 随档往返")
_orig_action_from = _action_from_dict


def _bad_action_from(d):
    act = _orig_action_from(d)
    act.range_ft = 0
    return act


restore = mutate(sys.modules["engine.persist"], "_action_from_dict",
                 _bad_action_from)
try:
    m37_caught = not m37_probe()
finally:
    restore()
ok(m37_caught, "M37b ⭐ 读档射程变 0（= 不限距离）→ 守卫抓到")


# --------------------------------------------------------------------------- #
section("M38 模型：默认必须是关的")


class _Bare(ModelClient):
    def _complete(self, req):
        return ModelReply(text="{}")


def m38_probe() -> bool:
    return _Bare(name="bare").enabled is False


ok(m38_probe(), "M38a 正常实现：ModelClient 默认 enabled=False")
_orig_init = ModelClient.__init__


def _bad_init(self, *a, **kw):
    kw["enabled"] = True
    _orig_init(self, *a, **kw)


restore = mutate(ModelClient, "__init__", _bad_init)
try:
    m38_caught = not m38_probe()
finally:
    restore()
ok(m38_caught, "M38b ⭐ 默认改回 True（接真模型即花钱）→ 守卫抓到")


# --------------------------------------------------------------------------- #
section("M39 网页日志：别人的私有行要挡掉")


def m39_probe() -> bool:
    f = webui_mod.viewer_log_filter("布兰")
    return (not f("【艾尔玛·私有】你发现了钥匙。")) \
        and f("【布兰·私有】你摸到了暗格。") and f("【公告】到哨塔了。")


ok(m39_probe(), "M39a 正常实现：只放行公共行 + 本人私有")
restore = mutate(webui_mod, "viewer_log_filter",
                 lambda viewer: (lambda line: True))
try:
    m39_caught = not m39_probe()
finally:
    restore()
ok(m39_caught, "M39b ⭐ 日志不过滤 → 别人的私有泄漏，守卫抓到")


# --------------------------------------------------------------------------- #
section("M40 页面：必须有台词输入框")


def m40_probe() -> bool:
    return "<textarea" in webui_mod.PAGE_HTML \
        and 'id="say"' in webui_mod.PAGE_HTML


ok(m40_probe(), "M40a 正常实现：页面有 #say 输入框")
_TEXTAREA = ('<textarea id="say" maxlength="500" aria-label="台词（可选）" '
             'placeholder="台词（可选）：先说，再点技能"></textarea>')
restore = mutate(webui_mod, "PAGE_HTML",
                 webui_mod.PAGE_HTML.replace(_TEXTAREA, ""))
try:
    m40_caught = not m40_probe()
finally:
    restore()
ok(m40_caught, "M40b ⭐ 页面又变成发不出台词 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M41 patch：move 不能移进自己的子树")


def m41_probe() -> bool:
    try:
        apply_patch({"a": {"b": 1}},
                    [{"op": "move", "from": "/a", "path": "/a/b"}])
    except PatchOpError:
        return True
    return False


ok(m41_probe(), "M41a 正常实现：移进子树 → PatchOpError")
restore = mutate(patch_mod, "_is_own_subtree", lambda s, d: False)
try:
    m41_caught = not m41_probe()
finally:
    restore()
ok(m41_caught, "M41b ⭐ 不挡子树移动 → 静默丢数据，守卫抓到")


# --------------------------------------------------------------------------- #
section("M42 战斗台词：解析层不能丢 say")


def m42_probe() -> bool:
    u = Unit(id="P", name="甲", hp=10, hp_max=10, team="pc", ac=12)
    u.actions.append(Action(name="砍", slot="action", attacks=[
        Attack(name="剑", attack_bonus=5, damage_notation="1d8",
               damage_type="slashing", reach=5)]))
    foe = Unit(id="E1", name="敌", hp=10, hp_max=10, team="enemy", ac=10,
               position=(5.0, 0.0))
    menu = build_menu(u, [u, foe])
    reply = ModelReply(text="{}", data={
        "actions": [{"id": "a1", "target": "E1", "say": "我劈他"}]})
    intents, _p = client_mod.parse_intent_reply(reply, {"P": menu})
    return bool(intents) and intents[0].extra.get("say") == "我劈他"


ok(m42_probe(), "M42a 正常实现：意图里保留台词")
_orig_parse = client_mod.parse_intent_reply


def _bad_parse(reply, menus):
    intents, probs = _orig_parse(reply, menus)
    for it in intents:
        it.extra.pop("say", None)
    return intents, probs


restore = mutate(client_mod, "parse_intent_reply", _bad_parse)
try:
    m42_caught = not m42_probe()
finally:
    restore()
ok(m42_caught, "M42b ⭐ 解析层丢掉 say → 台词消失，守卫抓到")


# --------------------------------------------------------------------------- #
section("M43 网页轮询：同一问题不许重绘")


def m43_probe() -> bool:
    return "s.pending.seq !== renderedSeq" in webui_mod.PAGE_HTML


ok(m43_probe(), "M43a 正常实现：只在 seq 变化时 render")
restore = mutate(webui_mod, "PAGE_HTML",
                 webui_mod.PAGE_HTML.replace(
                     "s.pending.seq !== renderedSeq", "true"))
try:
    m43_caught = not m43_probe()
finally:
    restore()
ok(m43_caught, "M43b ⭐ 每轮都重绘 → 选中被清，守卫抓到")


# --------------------------------------------------------------------------- #
section("M44 网页轮询：必须有超时")


def m44_probe() -> bool:
    return "AbortController" in webui_mod.PAGE_HTML \
        and "ctrl.abort()" in webui_mod.PAGE_HTML


ok(m44_probe(), "M44a 正常实现：轮询有 5 秒超时")
restore = mutate(webui_mod, "PAGE_HTML",
                 webui_mod.PAGE_HTML.replace("new AbortController()", "null"))
try:
    m44_caught = not m44_probe()
finally:
    restore()
ok(m44_caught, "M44b ⭐ 去掉超时 → 悬挂即卡死，守卫抓到")


# --------------------------------------------------------------------------- #
section("M45 世界清单：超过 20 条要折叠并说明")


def m45_probe() -> bool:
    st = StateTree.from_template({}, world=new_world(current="村口"))
    w = world_mod.World(st).ensure()
    for i in range(25):
        name = f"地点{i:02d}"
        w.register_location(name, description="x")
        w.discover(name)
    return "另有 5 项" in w.render("布兰")


ok(m45_probe(), "M45a 正常实现：25 条地点 → 只列 20 + 折叠说明")
restore = mutate(world_mod, "_trim", lambda names, limit: (list(names), ""))
try:
    m45_caught = not m45_probe()
finally:
    restore()
ok(m45_caught, "M45b ⭐ 不封顶 → 清单无限长，守卫抓到")


# --------------------------------------------------------------------------- #
section("M46 lint：DM 密记抄进玩家私有")


def m46_probe() -> bool:
    led = Ledger()
    led.public("公告", "你们在营地外停下。", section="公告")
    led.secret("公告", "队长是内应，今晚就要动手，别让玩家知道。", section="公告")
    led.private("对话", "队长是内应，今晚就要动手，别让玩家知道。", "甲",
                section="对话")
    return any("DM 密记" in p for p in vis_mod.lint_leaks(led))


ok(m46_probe(), "M46a 正常实现：DM→pc 复制会被抓到")
restore = mutate(vis_mod, "_lcs_len", lambda a, b: 0)
try:
    m46_caught = not m46_probe()
finally:
    restore()
ok(m46_caught, "M46b ⭐ 不查公共子串 → 泄漏漏报，守卫抓到")


# --------------------------------------------------------------------------- #
section("M47 存档：版本号必须跟着字段走")


def m47_probe() -> bool:
    return persist_mod.SAVE_VERSION == 4


ok(m47_probe(), "M47a 正常实现：SAVE_VERSION = 4")
restore = mutate(persist_mod, "SAVE_VERSION", 2)
try:
    m47_caught = not m47_probe()
finally:
    restore()
ok(m47_caught, "M47b ⭐ 版本退回 2 → 同版本不同字段，守卫抓到")


# --------------------------------------------------------------------------- #
section("M48 存档：当前场景/线索不能丢")


def m48_probe() -> bool:
    st, led, lp = build_table_loop(seed=3, names=("A", "B"))
    lp.set_scene_context(scene_id="s9", clues=[
        __import__("engine.rules.clues", fromlist=["SceneClue"]).SceneClue(
            id="c9", text="线索九")])
    doc = persist_mod.dump_game(state=st, ledger=led, loop=lp)
    back = persist_mod.load_from_dict(doc).loop
    return back.scene_id == "s9" and len(back.scene_clues) == 1


ok(m48_probe(), "M48a 正常实现：场景 id/线索随档往返")
_orig_loop_to = persist_mod._loop_to_dict


def _bad_loop_to(loop):
    d = _orig_loop_to(loop)
    d["scene_id"] = ""
    d["scene_clues"] = []
    return d


restore = mutate(persist_mod, "_loop_to_dict", _bad_loop_to)
try:
    m48_caught = not m48_probe()
finally:
    restore()
ok(m48_caught, "M48b ⭐ 不存场景 → 读档线索归零，守卫抓到")


# --------------------------------------------------------------------------- #
section("M49 座位：未知 role 不许静默兜给 DM")


def m49_probe() -> bool:
    t = TableClient(dm=FakeModel(name="dm"))
    try:
        t.seat_for(ModelRequest(role="filler", actor="x", task="t", prompt="p"))
    except ModelError:
        return True
    return False


ok(m49_probe(), "M49a 正常实现：未知 role → ModelError")
_orig_seat = TableClient.seat_for


def _bad_seat(self, req):
    try:
        return _orig_seat(self, req)
    except ModelError:
        return self.default or self.dm


restore = mutate(TableClient, "seat_for", _bad_seat)
try:
    m49_caught = not m49_probe()
finally:
    restore()
ok(m49_caught, "M49b ⭐ 未知 role 兜给 DM → 串味，守卫抓到")


# --------------------------------------------------------------------------- #
section("M50 戏法菜单：摘要也要带角色等级")


def m50_probe() -> bool:
    m = Unit(id="M", name="法师", hp=20, hp_max=20, team="pc", level=5,
             position=(0.0, 0.0))
    m.spells = [get_spell("Fire Bolt")]
    menu = build_menu(m, [m])
    return any("2d10" in o.summary for o in menu.options if o.kind == "spell")


ok(m50_probe(), "M50a 正常实现：5 级戏法菜单显示 2d10")
_orig_menu_line = Spell.menu_line


def _bad_menu_line(self, slot_level=None, character_level=0):
    return _orig_menu_line(self, slot_level)      # 坏写法：忽略角色等级


restore = mutate(Spell, "menu_line", _bad_menu_line)
try:
    m50_caught = not m50_probe()
finally:
    restore()
ok(m50_caught, "M50b ⭐ 菜单仍显示 1d10 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M51 网页选项：必须分组（不能一列平铺）")


def m51_probe() -> bool:
    return "GROUP_ORDER" in webui_mod.PAGE_HTML and "groupOf" in webui_mod.PAGE_HTML


ok(m51_probe(), "M51a 正常实现：选项分组逻辑存在")
restore = mutate(webui_mod, "PAGE_HTML",
                 webui_mod.PAGE_HTML.replace("GROUP_ORDER", "GROUPS_X"))
try:
    m51_caught = not m51_probe()
finally:
    restore()
ok(m51_caught, "M51b ⭐ 退回平铺列表 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M52 网页地图：战斗 SVG 容器")


def m52_probe() -> bool:
    return "drawMap(" in webui_mod.PAGE_HTML and 'id="battlemap"' in webui_mod.PAGE_HTML


ok(m52_probe(), "M52a 正常实现：地图容器与渲染函数")
restore = mutate(webui_mod, "PAGE_HTML",
                 webui_mod.PAGE_HTML.replace("drawMap(", "paintMap("))
try:
    m52_caught = not m52_probe()
finally:
    restore()
ok(m52_caught, "M52b ⭐ 去掉地图渲染 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M53 schema：commit 前必须校验并回滚")


def m53_probe() -> bool:
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=dict(AB),
                             hp_max=28, ac=16)}, current="甲")
    try:
        st.commit([{"op": "replace", "path": "/角色列表/甲/生命值/当前",
                    "value": "坏值"}], actor="测试", reason="坏值")
    except StateError:
        return st.read("/角色列表/甲/生命值/当前") == 28
    return False


ok(m53_probe(), "M53a 正常实现：坏值被拒且回滚")
restore = mutate(tree_mod, "validate_stats", lambda stats: [])
try:
    m53_caught = not m53_probe()
finally:
    restore()
ok(m53_caught, "M53b ⭐ 跳过 schema 校验 → 字符串写进生命值，守卫抓到")


# --------------------------------------------------------------------------- #
section("M54 剧本进度：章节/场景必须随档")


def m54_probe() -> bool:
    _st, _led, lp = build_table_loop(seed=8, names=("A", "B"))
    lp.set_story_position(adventure="剧本X", act="a1", scene="s1",
                          status="running")
    lp.mark_scene_done("s1")
    doc = persist_mod.dump_game(state=_st, ledger=_led, loop=lp)
    back = persist_mod.load_from_dict(doc).loop
    return (back.adventure_title == "剧本X" and back.story_act == "a1"
            and back.story_scene == "s1" and back.scenes_done == ["s1"])


ok(m54_probe(), "M54a 正常实现：剧本进度随档往返")
_orig_loop_to2 = persist_mod._loop_to_dict


def _bad_loop_to2(loop):
    d = _orig_loop_to2(loop)
    for k in ("adventure_title", "story_act", "story_scene",
              "story_status", "scenes_done"):
        d.pop(k, None)
    return d


restore = mutate(persist_mod, "_loop_to_dict", _bad_loop_to2)
try:
    m54_caught = not m54_probe()
finally:
    restore()
ok(m54_caught, "M54b ⭐ 不存章节进度 → 读档回第一场，守卫抓到")


# --------------------------------------------------------------------------- #
section("M55 网页动作：装备必须真的写进状态")


def m55_probe() -> bool:
    st, _led, lp = build_table_loop(seed=3, names=("A", "B"))
    st.commit([{"op": "insert", "path": "/角色列表/A/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="测试", reason="造武器")
    out = wa_mod.apply_action(st, lp, "A", {"kind": "equip_item", "name": "长剑"})
    return (out.get("ok") is True
            and st.read("/角色列表/A/物品/长剑/已装备") is False)


ok(m55_probe(), "M55a 正常实现：装备切换写进状态")
restore = mutate(wa_mod, "_toggle_bool",
                 lambda state, path, **kw: False)
try:
    m55_caught = not m55_probe()
finally:
    restore()
ok(m55_caught, "M55b ⭐ 只改界面不改状态 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M56 网页动作：战斗中不许换装")


def m56_probe() -> bool:
    st, _led, lp = build_table_loop(seed=3, names=("A", "B"))
    st.commit([{"op": "insert", "path": "/角色列表/A/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="测试", reason="造武器")
    lp.start_combat(enemies=[("Bandit", 1)], distance=10)
    try:
        wa_mod.apply_action(st, lp, "A", {"kind": "equip_item", "name": "长剑"})
    except wa_mod.ActionError:
        return True
    return False


ok(m56_probe(), "M56a 正常实现：战斗中换装 → ActionError")
restore = mutate(wa_mod, "_in_combat", lambda loop: False)
try:
    m56_caught = not m56_probe()
finally:
    restore()
ok(m56_caught, "M56b ⭐ 战斗中允许换装 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M57 护甲：轻甲/中甲必须加敏捷（带上限）")


def m57_probe() -> bool:
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士",
                             abilities=dict(AB), hp_max=30, ac=10)},
        current="甲")
    st.commit([{"op": "insert", "path": "/角色列表/甲/物品/链甲衫",
                "value": {"类别": "护甲", "已装备": True}}],
              actor="测试", reason="链甲衫")
    ac, _parts, _warn = armor_mod.armor_class(st, "甲")
    return ac == 15


ok(m57_probe(), "M57a 正常实现：链甲衫 13+2 = 15")
restore = mutate(armor_mod, "armor_class",
                 lambda state, character: (12, [], []))
try:
    m57_caught = not m57_probe()
finally:
    restore()
ok(m57_caught, "M57b ⭐ 不算护甲/敏捷 → AC 掉回 12，守卫抓到")


# --------------------------------------------------------------------------- #
section("M58 法术攻击：Touch 必须算近战")


def m58_probe() -> bool:
    touch = spells_mod.get_spell("Chill Touch")
    ranged = spells_mod.get_spell("Fire Bolt")
    return bool(touch and touch.is_melee_attack
                and ranged and not ranged.is_melee_attack)


ok(m58_probe(), "M58a 正常实现：Touch 近战 / Fire Bolt 远程")
restore = mutate(spells_mod.Spell, "is_melee_attack",
                 property(lambda self: False))
try:
    m58_caught = not m58_probe()
finally:
    restore()
ok(m58_caught, "M58b ⭐ 所有法术攻击都按远程 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M59 世界节点图：玩家图里不许有没听说过的地点")


def _map_state():
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="战士", abilities=dict(AB),
                             hp_max=20, ac=12)}, current="甲")
    return st, world_mod.World(st).ensure()


def m59_probe() -> bool:
    _st, w = _map_state()
    w.discover("村里")
    w.register_location("塔后", secret="强盗不知道有人会从这边上来")
    names = [n["name"] for n in w.graph("甲")["nodes"]]
    return "村里" in names and "塔后" not in names


ok(m59_probe(), "M59a 正常实现：玩家图看不到没听说过的地点")
_orig_graph = world_mod.World.graph
restore = mutate(
    world_mod.World, "graph",
    lambda self, viewer=world_mod.DM: _orig_graph(self, world_mod.DM))
try:
    m59_caught = not m59_probe()
finally:
    restore()
ok(m59_caught, "M59b ⭐ 图不分视角（按 DM 画）→ 守卫抓到")


# --------------------------------------------------------------------------- #
section("M60 世界节点图：玩家不知道的通道不许出现")


def m60_probe() -> bool:
    _st, w = _map_state()
    w.discover("村里")
    w.hear_about("塔后")
    w.add_passage("村里", "塔后", note="绕塔外墙的窄道", known=False)
    return w.graph("甲")["edges"] == []


ok(m60_probe(), "M60a 正常实现：秘密通道不进玩家图")
_orig_add_passage = world_mod.World.add_passage


def _bad_add_passage(self, a, b, **kw):
    kw["known"] = True                       # 把秘密通道当成"玩家已知"
    return _orig_add_passage(self, a, b, **kw)


restore = mutate(world_mod.World, "add_passage", _bad_add_passage)
try:
    m60_caught = not m60_probe()
finally:
    restore()
ok(m60_caught, "M60b ⭐ 通道一律公开 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M61 世界节点图：听说 ≠ 去过")


def m61_probe() -> bool:
    _st, w = _map_state()
    w.discover("村里")
    w.hear_about("塔顶")
    states = {n["name"]: n["state"] for n in w.graph("甲")["nodes"]}
    if states.get("塔顶") != "heard":
        return False
    try:
        w.travel("塔顶")
        return False
    except world_mod.WorldError:
        return True


ok(m61_probe(), "M61a 正常实现：只听说过 → 虚点、且过不去")
_orig_hear = world_mod.World.hear_about


def _bad_hear(self, name, **kw):
    _orig_hear(self, name, **kw)
    self.discover(name)                      # 听说直接当成"去过"


restore = mutate(world_mod.World, "hear_about", _bad_hear)
try:
    m61_caught = not m61_probe()
finally:
    restore()
ok(m61_caught, "M61b ⭐ 听说等于去过 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M62 经济：钱不够必须拒绝，不能赊账")


def _rich_state(hp=20, gold=0, stock=None):
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="牧师", abilities=dict(AB),
                             hp_max=hp, ac=14)}, current="甲")
    if gold:
        st.commit([{"op": "replace", "path": "/角色列表/甲/钱币/金币",
                    "value": int(gold)}], actor="测试", reason="给钱")
    w = world_mod.World(st).ensure()
    w.discover("村里")
    w.set_shop("村里", stock or ["Potion of Healing"], actor="测试")
    return st


def m62_probe() -> bool:
    st = _rich_state(gold=1)
    try:
        economy_mod.buy(st, "甲", "Potion of Healing")
        return False
    except economy_mod.EconomyError:
        return (economy_mod.wallet(st, "甲")["金币"] == 1
                and economy_mod.item_entry(st, "甲", "Potion of Healing") is None)


ok(m62_probe(), "M62a 正常实现：1 金币买不起 50 的药，且不扣钱不给货")
_orig_wallet = economy_mod.wallet
restore = mutate(economy_mod, "wallet",
                 lambda state, who: {"金币": 999, "银币": 0, "铜币": 0})
try:
    m62_caught = not m62_probe()
finally:
    restore()
ok(m62_caught, "M62b ⭐ 钱袋永远返回 999 → 赊账成功，守卫抓到")


# --------------------------------------------------------------------------- #
section("M63 经济：再给同类物品必须累加数量")


def m63_probe() -> bool:
    st = _rich_state()
    economy_mod.give_item(st, "甲", "火把", {"类别": "杂物"}, qty=1,
                          actor="测试")
    economy_mod.give_item(st, "甲", "火把", {}, qty=2, actor="测试")
    return economy_mod.item_qty(economy_mod.item_entry(st, "甲", "火把")) == 3


ok(m63_probe(), "M63a 正常实现：1+2 = 3（累加）")


def _bad_give_item(state, who, name, value, *, qty=1, actor="DM",
                   reason="获得物品"):
    body = {**value, "数量": int(qty)}        # 覆盖成新数量，不累加
    path = (f"/角色列表/{patch_mod.escape_token(who)}"
            f"/物品/{patch_mod.escape_token(name)}")
    op = "replace" if economy_mod.item_entry(state, who, name) is not None \
        else "insert"
    state.commit([{"op": op, "path": path, "value": body}],
                 actor=actor, reason=reason)
    return body


restore = mutate(economy_mod, "give_item", _bad_give_item)
try:
    m63_caught = not m63_probe()
finally:
    restore()
ok(m63_caught, "M63b ⭐ 新数量覆盖旧数量 → 1+2 变 2，守卫抓到")


# --------------------------------------------------------------------------- #
section("M64 经济：SRD 没给价的物品不许当成免费")


def m64_probe() -> bool:
    st = _rich_state(gold=500, stock=["Potion of Greater Healing"])
    try:
        economy_mod.buy(st, "甲", "Potion of Greater Healing")
        return False
    except economy_mod.EconomyError:
        return True


ok(m64_probe(), "M64a 正常实现：强效药水没价 → 拒绝（需 DM 报价）")
restore = mutate(economy_mod, "_price_of", lambda name: 0)
try:
    m64_caught = not m64_probe()
finally:
    restore()
ok(m64_caught, "M64b ⭐ 没价就当 0 金币 → 白拿，守卫抓到")


# --------------------------------------------------------------------------- #
section("M65 建卡：HP 必须按生命骰算（不能套 d10）")


def _wiz_char():
    return creation_mod.create_character(
        "莉安", klass="Wizard", species="Elf", level=3,
        abilities={"力量": 8, "敏捷": 14, "体质": 13, "智力": 15,
                   "感知": 12, "魅力": 10},
        skills=["奥秘", "调查"], spells=["Fire Bolt", "Magic Missile"])


def m65_probe() -> bool:
    ch = _wiz_char()
    return ch["生命值"]["最大"] == 17 and ch["生命值"]["生命骰"]["面数"] == "d6"


ok(m65_probe(), "M65a 正常实现：3 级法师 HP 17、生命骰 d6")
_orig_class_info = creation_mod.class_info
restore = mutate(creation_mod, "class_info",
                 lambda k: {**_orig_class_info(k), "hit_die": 10})
try:
    m65_caught = not m65_probe()
finally:
    restore()
ok(m65_caught, "M65b ⭐ 法师按 d10 算 HP → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M66 建卡：不在本职业法术表里的法术必须拒绝")


def m66_probe() -> bool:
    try:
        creation_mod.create_character(
            "莉安", klass="Wizard", species="Elf", level=1,
            abilities={"力量": 8, "敏捷": 14, "体质": 13, "智力": 15,
                       "感知": 12, "魅力": 10},
            skills=["奥秘", "调查"], spells=["Cure Wounds"])
        return False
    except creation_mod.CreationError:
        return True


ok(m66_probe(), "M66a 正常实现：法师点牧师法术 → 拒绝")
_orig_spells_for = creation_mod.spells_for


def _bad_spells_for(klass, *, max_level=1):
    return [{"name": r.get("name"), "level": int(r.get("level") or 0)}
            for r in creation_mod._store().records("spells")
            if int(r.get("level") or 0) <= int(max_level)]


restore = mutate(creation_mod, "spells_for", _bad_spells_for)
try:
    m66_caught = not m66_probe()
finally:
    restore()
ok(m66_caught, "M66b ⭐ 法术表不按职业过滤 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M67 升级：1~5 级上限必须守住")


def _st_at_five():
    ch = _wiz_char()
    st = StateTree.from_template({"莉安": ch}, current="莉安")
    st.commit([{"op": "replace", "path": "/角色列表/莉安/职业/法师",
                "value": 5}], actor="测试", reason="直接设 5 级")
    return st


def m67_probe() -> bool:
    st = _st_at_five()
    try:
        creation_mod.level_up(st, "莉安")
        return False
    except creation_mod.CreationError:
        return True


ok(m67_probe(), "M67a 正常实现：5 级再升 → 报错")
restore = mutate(creation_mod, "_MAX_LEVEL", 10)
try:
    m67_caught = not m67_probe()
finally:
    restore()
ok(m67_caught, "M67b ⭐ 把上限改成 10 级 → 继续升，守卫抓到")


# --------------------------------------------------------------------------- #
section("M68 网页建卡：开局必须用建出来的角色")


def m68_probe() -> bool:
    import shutil as _shutil
    import tempfile as _tempfile
    from engine.adventure import load_adventure as _load
    from engine.demo_web import DEFAULT_ADV as _ADV, WebSession as _Sess
    from engine.webui import WebBridge as _Bridge
    adv = _load(_ADV)
    tmp = _tempfile.mkdtemp(prefix="dnd-m68-")
    bridge = _Bridge(timeout=0.1)
    sess = _Sess(bridge, adv, tmp)
    try:
        res = sess.create_start({
            "name": "阿瞳", "klass": "Cleric", "species": "Human", "level": 1,
            "ability_mode": "suggest",
            "skills": ["perception", "religion"], "spells": []})
        names = list(sess.state.characters()) if sess.state else []
        return bool(res.get("ok")) and "阿瞳" in names
    finally:
        bridge.abort()
        sess._stop_old()
        _shutil.rmtree(tmp, ignore_errors=True)


ok(m68_probe(), "M68a 正常实现：建卡开局换掉真人座位")
import engine.demo_web as demo_web_mod  # noqa: E402
_orig_bwt = demo_web_mod.build_web_table


def _bad_bwt(bridge, *, narrations=None, human_char=None,
             human_name=demo_web_mod.HUMAN, mode="normal",
             title="哨塔的拖痕", companions=None, companion_chars=None):
    return _orig_bwt(bridge, narrations=narrations, human_char=None,
                     human_name=demo_web_mod.HUMAN, mode=mode, title=title,
                     companions=companions, companion_chars=companion_chars)


restore = mutate(demo_web_mod, "build_web_table", _bad_bwt)
try:
    m68_caught = not m68_probe()
finally:
    restore()
ok(m68_caught, "M68b ⭐ 开局忽略建出来的角色（回默认四人队）→ 守卫抓到")


# --------------------------------------------------------------------------- #
section("M69 战内喝药：必须消耗附赠动作（一回合一瓶）")


def _potion_unit():
    from engine.rules.menu import build_menu as _bm
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="牧师", abilities=dict(AB),
                             hp_max=24, ac=14)}, current="甲")
    st.commit([
        {"op": "replace", "path": "/角色列表/甲/生命值/当前", "value": 8},
        {"op": "insert", "path": "/角色列表/甲/物品/治疗药水",
         "value": {"类别": "消耗品", "数量": 2, "效果": {"治疗": "2d4+2"}}},
    ], actor="测试", reason="备药")
    u = make_hero_unit(st, "甲")
    u.reset_turn()
    menu = _bm(u, [u])
    opt = next(o for o in menu.options if o.kind == "use_item")
    return u, menu, opt


def m69_probe() -> bool:
    u, menu, opt = _potion_unit()
    first = execute_intent(u, Intent(unit=u.id, option_id=opt.id), menu, [u],
                           Random(21))
    second = execute_intent(u, Intent(unit=u.id, option_id=opt.id), menu, [u],
                            Random(22))
    return first.ok and not second.ok


ok(m69_probe(), "M69a 正常实现：同一回合第二瓶被拒")
_orig_spend = Unit.spend
restore = mutate(Unit, "spend", lambda self, slot, *a, **kw: None)
try:
    m69_caught = not m69_probe()
finally:
    restore()
ok(m69_caught, "M69b ⭐ 不扣附赠动作 → 一回合能连喝，守卫抓到")


# --------------------------------------------------------------------------- #
section("M70 战内喝药：stat_data 的数量必须当场回写")


def m70_probe() -> bool:
    from engine.rules.menu import build_menu as _bm
    loop = _potion_loop()
    unit = loop.field[0]
    unit.reset_turn()
    m = _bm(unit, loop.field)
    o = next(x for x in m.options if x.kind == "use_item")
    out = execute_intent(unit, Intent(unit=unit.id, option_id=o.id), m,
                         loop.field, loop.rng,
                         on_use_item=loop._consume_item_state)
    item = loop.state.read("/角色列表/甲/物品/治疗药水")
    return bool(out.ok) and isinstance(item, dict) and item.get("数量") == 1


def _potion_loop():
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="牧师", abilities=dict(AB),
                             hp_max=24, ac=14)}, current="甲")
    st.commit([
        {"op": "replace", "path": "/角色列表/甲/生命值/当前", "value": 8},
        {"op": "insert", "path": "/角色列表/甲/物品/治疗药水",
         "value": {"类别": "消耗品", "数量": 2, "效果": {"治疗": "2d4+2"}}},
    ], actor="测试", reason="备药")
    loop = GameLoop(st, Ledger(),
                    TableClient(dm=FakeModel(), planner=FakeModel(),
                                players={}), rng=Random(23))
    loop.set_field([make_hero_unit(st, "甲")])
    return loop


ok(m70_probe(), "M70a 正常实现：喝一瓶后 stat_data 剩 1")
restore = mutate(GameLoop, "_consume_item_state",
                 lambda self, unit, name, left: None)
try:
    m70_caught = not m70_probe()
finally:
    restore()
ok(m70_caught, "M70b ⭐ 不回写数量 → 角色卡还是 2 瓶，守卫抓到")


# --------------------------------------------------------------------------- #
section("M71 消耗品：动作开销按物品走，不能一律附赠")


def m71_probe() -> bool:
    from engine.rules.menu import build_menu as _bm
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, job="牧师", abilities=dict(AB),
                             hp_max=24, ac=14)}, current="甲")
    st.commit([{"op": "insert", "path": "/角色列表/甲/物品/火油",
                "value": {"类别": "消耗品", "数量": 1, "动作": "action"}}],
              actor="测试", reason="杂项")
    u = loop_mod.make_hero_unit(st, "甲")
    u.reset_turn()
    menu = _bm(u, [u])
    opt = next((o for o in menu.options
                if o.kind == "use_item" and o.item == "火油"), None)
    return opt is not None and opt.slot == "action"


ok(m71_probe(), "M71a 正常实现：标「动作」的物品占动作槽")
_orig_mk = loop_mod.make_hero_unit


def _bad_mk(state, name, **kw):
    u = _orig_mk(state, name, **kw)
    for info in (u.consumables or {}).values():
        info["动作"] = "bonus_action"
    return u


restore = mutate(loop_mod, "make_hero_unit", _bad_mk)
try:
    m71_caught = not m71_probe()
finally:
    restore()
ok(m71_caught, "M71b ⭐ 所有消耗品都当附赠动作 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M72 战役欠账：已兑现的伏笔不能再算欠账")


def m72_probe() -> bool:
    from engine.adventure import CampaignProgress, load_adventure
    adv = load_adventure(os.path.join(ROOT, "adventures", "哨塔的拖痕.json"))
    prog = CampaignProgress()
    prog.close("p2", note="测试里手动兑现", save=False)
    return "p2" not in [p.id for p in prog.unresolved(adv)]


ok(m72_probe(), "M72a 正常实现：兑现过的 p2 不再欠")
from engine.adventure.progress import CampaignProgress as _CP  # noqa: E402
_orig_unresolved = _CP.unresolved


def _bad_unresolved(self, adv):
    return [p for p in adv.promises if p.status == "open"]   # 不看 closed


restore = mutate(_CP, "unresolved", _bad_unresolved)
try:
    m72_caught = not m72_probe()
finally:
    restore()
ok(m72_caught, "M72b ⭐ 不看「已兑现」→ 欠账永远清不掉，守卫抓到")


# --------------------------------------------------------------------------- #
section("M73 世界图：玩家不能走秘密通道")


def m73_probe() -> bool:
    st = StateTree.from_template(
        {"甲": new_character("甲", level=1, job="战士", abilities=dict(AB),
                             hp_max=12, ac=12)}, current="甲")
    w = world_mod.World(st).ensure()
    w.discover("村里")
    w.ear_about = None
    w.hear_about("荒径")
    w.discover("哨塔")
    w.add_passage("村里", "荒径", known=True)
    w.add_passage("荒径", "哨塔", known=False)      # 秘密通道
    w.move_to("荒径")
    try:
        w.move_to("哨塔")
        return False
    except world_mod.WorldError:
        return True


ok(m73_probe(), "M73a 正常实现：秘密通道走不过去")
restore = mutate(world_mod.World, "can_move_to",
                 lambda self, name: (True, ""))
try:
    m73_caught = not m73_probe()
finally:
    restore()
ok(m73_caught, "M73b ⭐ 不检查通道能不能走 → 玩家乱穿，守卫抓到")


# --------------------------------------------------------------------------- #
section("M74 硬预算：超了必须裁，且不能裁角色卡")


def m74_probe() -> bool:
    from engine.state.assembler import Assembler as _A
    st = StateTree.from_template(
        {"甲": new_character("甲", level=1, job="战士", abilities=dict(AB),
                             hp_max=12, ac=12)}, current="甲")
    asm = _A(st, None, memory_provider=lambda v: "旧事。" * 800)
    asm.set_block("角色卡", st.render("甲"))
    asm.set_block("可选行动", "[a1] 攻击")
    out = asm.build("甲", task="行动", max_tokens=200)
    return bool(out.dropped) and out.dropped[0][0] == "记忆" \
        and out.has("角色卡") and out.tokens_est <= 200


ok(m74_probe(), "M74a 正常实现：超预算先裁记忆、保住角色卡")
from engine.state.assembler import Assembler as _Asm  # noqa: E402
restore = mutate(_Asm, "_apply_budget",
                 staticmethod(lambda out, mt: None))
try:
    m74_caught = not m74_probe()
finally:
    restore()
ok(m74_caught, "M74b ⭐ 不执行预算裁剪 → 超限也不裁，守卫抓到")


# --------------------------------------------------------------------------- #
section("M75 事务回滚：失败必须真的把树还原")


def m75_probe() -> bool:
    st = StateTree.from_template(
        {"甲": new_character("甲", level=1, job="战士", abilities=dict(AB),
                             hp_max=12, ac=12)}, current="甲")
    path = "/角色列表/甲/护甲等级"
    old = st.read(path)
    snap = st.snapshot()
    st.commit([{"op": "replace", "path": path, "value": int(old) + 2}],
              actor="测试", reason="改 AC")
    st.restore(snap, actor="测试", reason="回滚")
    return st.read(path) == old


ok(m75_probe(), "M75a 正常实现：restore 真的还原字段")
restore = mutate(StateTree, "restore", lambda self, snap, **kw: False)
try:
    m75_caught = not m75_probe()
finally:
    restore()
ok(m75_caught, "M75b ⭐ restore 什么都不做 → 半截状态留下，守卫抓到")


# --------------------------------------------------------------------------- #
section("M76 输出体检：完整但非法必须纠错一次")


def m76_probe() -> bool:
    from engine.models import FakeModel as _F
    from engine.models.format_check import classify as _cls
    st = StateTree.from_template(
        {"甲": new_character("甲", level=1, job="战士", abilities=dict(AB),
                             hp_max=12, ac=12)}, current="甲")
    unit = make_hero_unit(st, "甲")
    menu = build_menu(unit, [unit])
    from engine.state.assembler import Assembler as _A
    asm = _A(st, Ledger()).build("甲", task="行动")
    oid = next((o.id for o in menu.options if not o.requires_target),
               menu.options[0].id)
    good = __import__("json").dumps(
        [{"unit": unit.id, "actions": [{"id": oid}]}])
    loop = GameLoop(st, Ledger(), _F(script=[
        '{"unit": "甲", "actions": [{"id": "a999"}]}', good]))
    reply = loop._call_checked(role="player", actor="甲", task="combat_turn",
                               assembled=asm, expect="intent",
                               menus={unit.id: menu}, allowed={})
    return loop.client._i == 2 and _cls(reply, {unit.id: menu}).status == "valid"


ok(m76_probe(), "M76a 正常实现：坏一次 → 纠错一次 → 用第二次")
from engine.rules.loop import GameLoop as _GL  # noqa: E402
_orig_checked = _GL._call_checked


def _no_repair(self, *, role, actor, task, assembled, expect, menus,
               allowed=None, repair=True):
    return self._call(role=role, actor=actor, task=task, assembled=assembled,
                      expect=expect, allowed=allowed)


restore = mutate(_GL, "_call_checked", _no_repair)
try:
    m76_caught = not m76_probe()
finally:
    restore()
ok(m76_caught, "M76b ⭐ 不纠错 → 坏输出直接混进来，守卫抓到")


# --------------------------------------------------------------------------- #
section("M77 被动察觉：站着不动也要能发现")


def m77_probe() -> bool:
    from engine.rules.clues import SceneClue as _SC
    st = StateTree.from_template(
        {"甲": new_character("甲", level=1, job="战士", abilities=dict(AB),
                             hp_max=12, ac=12)}, current="甲")
    u = make_hero_unit(st, "甲")
    u.skill_bonuses["perception"] = 5           # 被动 15
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([u])
    w = World(st).ensure()
    w.discover("哨塔")
    w.travel("哨塔")
    c = _SC(id="c1", text="地板上有一串新鲜脚印，一直通向楼梯口。",
            skill="perception", dc=14, secret=True, place="哨塔")
    w.register_clue("c1", scene="s1", place="哨塔", skill="perception",
                    dc=14, text=c.text, secret=True)
    loop.set_scene_context(scene_id="s1", clues=[c])
    return bool(loop._passive_scan([u], None)) and w.clue("c1")["已发现"]


ok(m77_probe(), "M77a 正常实现：被动 15 ≥ DC 14 自动发现")
restore = mutate(GameLoop, "_passive_scan", lambda self, units, log=None: [])
try:
    m77_caught = not m77_probe()
finally:
    restore()
ok(m77_caught, "M77b ⭐ 不扫被动察觉 → 站着不动永远发现不了，守卫抓到")


# --------------------------------------------------------------------------- #
section("M78 线索重试：同一场必须拦住")


def m78_probe() -> bool:
    st = StateTree.from_template(
        {"甲": new_character("甲", level=1, job="战士", abilities=dict(AB),
                             hp_max=12, ac=12)}, current="甲")
    u = make_hero_unit(st, "甲")
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([u])
    w = World(st).ensure()
    w.discover("哨塔")
    w.travel("哨塔")
    from engine.rules.clues import SceneClue as _SC
    c = _SC(id="c2", text="地板上有一串新鲜脚印，一直通向楼梯口。",
            skill="perception", dc=30, secret=True, place="哨塔")
    w.register_clue("c2", scene="s1", place="哨塔", skill="perception",
                    dc=30, text=c.text, secret=True)
    loop.set_scene_context(scene_id="s1", clues=[c])
    loop._resolve_look_search(u, "look")        # 失败 → 记一次尝试
    return loop._eligible_clue(u, "look") is None


ok(m78_probe(), "M78a 正常实现：同一场失败后不再给掷")
_orig_eligible = GameLoop._eligible_clue


def _bad_eligible(self, unit, action):
    for c in self.scene_clues:
        if c.action == action:
            return c
    return None


restore = mutate(GameLoop, "_eligible_clue", _bad_eligible)
try:
    m78_caught = not m78_probe()
finally:
    restore()
ok(m78_caught, "M78b ⭐ 同一场能反复掷到成功 → 守卫抓到")


# --------------------------------------------------------------------------- #
section("M79 别名表：短刀与匕首必须能互认")


def m79_probe() -> bool:
    from engine.memory import MemoryStore as _MS
    from engine.memory import Query as _Q
    from engine.memory import score_entry as _se
    st = _MS()
    st.record("门轴上刻着名字。", tags=("短刀",))
    s, _why = _se(st._entries[0], _Q(tags=("匕首",)))
    return s == 3.0


ok(m79_probe(), "M79a 正常实现：短刀标签能被「匕首」命中")
restore = mutate(recall_mod, "canonical", lambda term, aliases=None: term)
try:
    m79_caught = not m79_probe()
finally:
    restore()
ok(m79_caught, "M79b ⭐ 别名不折叠 → 换个说法就召回不到，守卫抓到")


# --------------------------------------------------------------------------- #
section("M80 IDF：稀有词必须压过常用词")


def m80_probe() -> bool:
    from engine.memory import MemoryStore as _MS
    from engine.memory import build_idf as _bi
    st = _MS()
    st.record("哨塔的门开着。", tags=("哨塔",))
    st.record("石壁上刻着龙纹。", tags=("龙纹",))
    st.record("哨塔二层很暗。", tags=("哨塔",))
    idf = recall_mod.build_idf(st._entries)
    return idf.get("龙纹", 1.0) > idf.get("哨塔", 1.0)


ok(m80_probe(), "M80a 正常实现：出现少的词 IDF 高")
restore = mutate(recall_mod, "build_idf",
                 lambda entries, aliases=None: {})
try:
    m80_caught = not m80_probe()
finally:
    restore()
ok(m80_caught, "M80b ⭐ IDF 恒等 → 稀有词失去权重，守卫抓到")


# --------------------------------------------------------------------------- #
section("M81 回到某一场：前面没跑完必须拒绝")


def m81_probe() -> bool:
    from engine.adventure import AdventureError as _AE
    from engine.adventure import AdventureRunner as _AR
    from engine.adventure import load_adventure as _load
    st, _led, loop = build_table_loop()
    runner = _AR(loop, _load(os.path.join(ROOT, "adventures",
                                         "哨塔的拖痕.json")))
    try:
        runner.run_from("s2")
        return False
    except _AE:
        return True


ok(m81_probe(), "M81a 正常实现：前面没跑完，从 s2 开始被拒")
from engine.adventure.runner import AdventureRunner as _Runner  # noqa: E402
restore = mutate(_Runner, "run_from",
                 lambda self, scene_id, combat_rounds=None:
                 self.run_all(combat_rounds=combat_rounds))
try:
    m81_caught = not m81_probe()
finally:
    restore()
ok(m81_caught, "M81b ⭐ 不检查前置场景 → 从中间硬跑，守卫抓到")


# --------------------------------------------------------------------------- #
section("M82 导出存档：必须是完整的一局")


def _slots_session(tmp):
    from engine.adventure import load_adventure as _la
    from engine.demo_web import WebSession as _WS
    from engine.webui import WebBridge as _WB
    sess = _WS(_WB(timeout=1), _la(os.path.join(ROOT, "adventures",
                                                "哨塔的拖痕.json")), tmp)
    st = StateTree.from_template(
        {"甲": new_character("甲", level=1, job="战士", abilities=dict(AB),
                             hp_max=12, ac=12)}, current="甲")
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    sess.state, sess.loop = st, loop
    sess.human_name = "甲"
    return sess


def m82_probe() -> bool:
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="dnd-m82-")
    try:
        sess = _slots_session(tmp)
        out = sess.action({"kind": "export_save"})
        data = (out.get("value") or {}).get("data") or {}
        return bool(data.get("format") and data.get("state") and data.get("loop"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


ok(m82_probe(), "M82a 正常实现：导出的存档带 format/state/loop")
from engine.demo_web import WebSession as _WS2  # noqa: E402


def _bad_export(self):
    return {"ok": True, "value": {"filename": "x.json",
                                  "data": {"state": self.state.to_dict()}}}


restore = mutate(_WS2, "export_data", _bad_export)
try:
    m82_caught = not m82_probe()
finally:
    restore()
ok(m82_caught, "M82b ⭐ 导出只剩半个 state → 导入会缺档，守卫抓到")


# --------------------------------------------------------------------------- #
section("M83 别名替换：正名不能被子串二次污染")


def m83_probe() -> bool:
    return (recall_mod._canon_text("治疗药水") == "治疗药水"
            and recall_mod._canon_text("喝下药水") == "喝下治疗药水")


ok(m83_probe(), "M83a 正常实现：治疗药水不被二次替换")


def _bad_canon(text, aliases=None):
    s = "".join(str(text or "").split())
    table = recall_mod.ALIASES if aliases is None else aliases
    for a, n in table.items():
        s = s.replace(a, n)
    return s


restore = mutate(recall_mod, "_canon_text", _bad_canon)
try:
    m83_caught = not m83_probe()
finally:
    restore()
ok(m83_caught, "M83b ⭐ 逐个别名串联替换 → 治疗治疗药水，守卫抓到")


# --------------------------------------------------------------------------- #
section("M84 叙事体检：未发现的秘密不能从 DM 正文漏出去")


def m84_probe() -> bool:
    secret = "地板上有一块松动的石板，下面压着一封信。"
    return "secret_leak" in {i.code for i in narrate_lint.check_narration(
        secret, forbidden=[secret])}


ok(m84_probe(), "M84a 正常实现：抄秘密原文会被标记 secret_leak")
_orig_check = narrate_lint.check_narration


def _bad_check(text, *, forbidden=(), min_chars=30, max_chars=1200):
    return _orig_check(text, forbidden=(), min_chars=min_chars,
                       max_chars=max_chars)


restore = mutate(narrate_lint, "check_narration", _bad_check)
try:
    m84_caught = not m84_probe()
finally:
    restore()
ok(m84_caught, "M84b ⭐ 体检无视 forbidden 清单 → 秘密照抄进正文，守卫抓到")


# --------------------------------------------------------------------------- #
section("M85 战斗解析：单位当键的对象要能拆")


def m85_probe() -> bool:
    from engine.models import ModelReply as _MR
    from engine.rules.menu import Menu as _Menu, Option as _Opt
    menu = _Menu(unit_id="E1", unit_name="E1", team="enemy", options=[
        _Opt(id="a1", kind="attack", slot="action", name="弩", summary="",
             target_ids=["布兰"], requires_target=True)])
    intents, _probs = client_mod.parse_intent_reply(
        _MR(data={"E1": {"action": {"id": "a1", "target": "布兰"}}}),
        {"E1": menu})
    return [i.option_id for i in intents] == ["a1"]


ok(m85_probe(), "M85a 正常实现：{E1:{action:...}} 能拆成意图")
_orig_parse = client_mod.parse_intent_reply


def _bad_parse(reply, menus):
    from engine.models import ModelReply as _MR
    data = reply.data
    if isinstance(data, dict) and "unit" not in data and "actions" not in data:
        return _orig_parse(_MR(text=reply.text, data={}), menus)
    return _orig_parse(reply, menus)


restore = mutate(client_mod, "parse_intent_reply", _bad_parse)
try:
    m85_caught = not m85_probe()
finally:
    restore()
ok(m85_caught, "M85b ⭐ 不支持单位当键 → 多单位规划全废，守卫抓到")


# --------------------------------------------------------------------------- #
section("M86 DM 技能写入口：坏字段不许静默落库")


def _bad_coerce(payload):
    """坏写法：把网页字段原样塞进当局（不做类型专属校验）。"""
    return {k: v for k, v in payload.items() if k not in ("name", "kind")}


def m86_probe() -> bool:
    st = StateTree.from_template({"甲": new_character("甲", level=3)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    try:
        wa_mod.apply_action(st, loop, "甲",
                            {"kind": "dm_create_action", "name": "坏技能",
                             "类型": "attack"})
    except wa_mod.ActionError:
        return True
    return False


ok(m86_probe(), "M86a 正常实现：缺伤害骰的攻击技能被拒")
restore = mutate(wa_mod, "coerce_action_spec", _bad_coerce)
try:
    m86_caught = not m86_probe()
finally:
    restore()
ok(m86_caught, "M86b ⭐ 写入口不校验 → 坏技能落库，守卫抓到")


# --------------------------------------------------------------------------- #
section("M87 治疗技能只能落在自己或友方")


def _mk_heal_loop():
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, hp_max=20)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    wa_mod.apply_action(st, loop, "甲",
                        {"kind": "dm_create_action", "name": "回春",
                         "类型": "heal", "治疗骰": "1d4"})
    loop.start_combat(enemies=[("Bandit", 1)], distance=20)
    pc = loop.find_unit("甲")
    foe = next(u for u in loop.field if u.team == "enemy")
    pc.position, foe.position = (5.0, 0.0), (5.0, 0.0)
    return loop, pc, foe


def m87_probe() -> bool:
    loop, pc, foe = _mk_heal_loop()
    menu = build_menu(pc, loop.field)
    opt = next(o for o in menu.options if o.kind == "heal_action")
    res = execute_intent(pc, Intent(unit=pc.id, option_id=opt.id, target=foe.id),
                         menu, loop.field, Random(3))
    return not res.ok


ok(m87_probe(), "M87a 正常实现：治疗技能不能对敌人用")
restore = mutate(turn_mod, "_heal_target_ok", lambda caster, target: True)
try:
    m87_caught = not m87_probe()
finally:
    restore()
ok(m87_caught, "M87b ⭐ 不查阵营 → 敌人被治好，守卫抓到")


# --------------------------------------------------------------------------- #
section("M88 存读档：治疗动作不能丢治疗骰")


def m88_probe() -> bool:
    u = Unit(id="U1", name="甲", team="pc", hp_max=10, hp=10,
             actions=[Action(name="回春", slot="action", heal_notation="2d4+2")])
    back = persist_mod.unit_from_dict(persist_mod.unit_to_dict(u))
    return bool(back.actions) and back.actions[0].heal_notation == "2d4+2"


ok(m88_probe(), "M88a 正常实现：heal_notation 存读档不丢")
_orig_action_to = persist_mod._action_to_dict


def _bad_action_to(a):
    d = _orig_action_to(a)
    d.pop("heal_notation", None)          # 坏写法：序列化漏字段
    return d


restore = mutate(persist_mod, "_action_to_dict", _bad_action_to)
try:
    m88_caught = not m88_probe()
finally:
    restore()
ok(m88_caught, "M88b ⭐ 序列化漏掉治疗骰 → 读档后治不了人，守卫抓到")


# --------------------------------------------------------------------------- #
section("M89 人物数值：单位必须按世界树里的数值造")


def _mk_npc_table():
    st = StateTree.from_template(
        {"甲": new_character("甲", level=3, hp_max=24, ac=15)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    wa_mod.apply_action(st, loop, "甲", {
        "kind": "dm_set_npc_stats", "name": "疤脸", "等级": 3,
        "护甲等级": 14, "生命值": 22, "属性": {"敏捷": 12},
        "动作": {"砍刀": {"类型": "attack", "攻击加值": 5,
                          "伤害骰": "1d8+2"}}})
    return st, loop


def m89_probe() -> bool:
    st, _loop = _mk_npc_table()
    u = loop_mod.build_npc_unit(st, "疤脸", team="enemy", uid="N1")
    return u.ac == 14 and u.hp_max == 22


ok(m89_probe(), "M89a 正常实现：人物 AC/HP 来自数值块")
restore = mutate(loop_mod, "build_npc_unit",
                 lambda st, name, **kw: Unit(id="N1", name=name,
                                             team="enemy", ac=10,
                                             hp_max=10, hp=10))
try:
    m89_caught = not m89_probe()
finally:
    restore()
ok(m89_caught, "M89b ⭐ 改用通用怪数值 → DM 写的 AC/HP 被吞，守卫抓到")


# --------------------------------------------------------------------------- #
section("M90 人物上场：必须插进当前先攻序")


def m90_probe() -> bool:
    _st, loop = _mk_npc_table()
    loop.start_combat(enemies=[("Bandit", 1)], distance=20)
    u = loop.add_npc("疤脸", team="enemy")
    return any(x.id == u.id for x, _i in loop.initiative)


ok(m90_probe(), "M90a 正常实现：战斗中加人进先攻序")
_orig_add_npc = GameLoop.add_npc


def _bad_add_npc(self, name, **kw):
    u = _orig_add_npc(self, name, **kw)
    self.initiative[:] = [(x, i) for x, i in self.initiative if x.id != u.id]
    return u


restore = mutate(GameLoop, "add_npc", _bad_add_npc)
try:
    m90_caught = not m90_probe()
finally:
    restore()
ok(m90_caught, "M90b ⭐ 只塞进 field、不进先攻 → 这个人永远轮不到，守卫抓到")


# --------------------------------------------------------------------------- #
section("M91 临时 AC 修正必须进有效 AC")


def m91_probe() -> bool:
    u = Unit(id="U1", name="甲", team="pc", hp_max=10, hp=10, ac=15,
             mods=[{"name": "盾", "kind": "ac", "value": 2,
                    "scope": "scene", "rounds": 0, "target": ""}])
    return u.ac_total == 17 and u.ac == 15


ok(m91_probe(), "M91a 正常实现：ac_total 加修正、基础 AC 不动")
restore = mutate(Unit, "ac_total", property(lambda self: self.ac))
try:
    m91_caught = not m91_probe()
finally:
    restore()
ok(m91_caught, "M91b ⭐ 修正不进有效 AC → 护盾白挂，守卫抓到")


# --------------------------------------------------------------------------- #
section("M92 换场必须清掉场景修正")


def m92_probe() -> bool:
    st = StateTree.from_template({"甲": new_character("甲", level=3)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    u = loop.find_unit("甲")
    effects_mod.add_mod(u, {"name": "光", "kind": "ac", "value": 1,
                            "scope": "scene", "rounds": 0, "target": ""})
    loop.set_scene_effects([], source="s-next")
    return not any(m["name"] == "光" for m in u.mods)


ok(m92_probe(), "M92a 正常实现：换场把上一场修正清掉")
_orig_set_effects = GameLoop.set_scene_effects


def _bad_set_effects(self, effects, *, source=""):
    self._scene_effects = [dict(e) for e in (effects or [])]
    self._scene_source = str(source or "")
    self._apply_effects(phase="pre")          # 忘记清旧的场景修正


restore = mutate(GameLoop, "set_scene_effects", _bad_set_effects)
try:
    m92_caught = not m92_probe()
finally:
    restore()
ok(m92_caught, "M92b ⭐ 场景修正跨场残留 → 上一场的加成带到下一场，守卫抓到")


# --------------------------------------------------------------------------- #
section("M93 轮次修正必须到期")


def m93_probe() -> bool:
    u = Unit(id="U2", name="甲", team="pc", hp_max=10, hp=10, ac=12,
             mods=[{"name": "鼓", "kind": "attack", "value": 1,
                    "scope": "round", "rounds": 1, "target": ""}])
    u.reset_turn()
    return not u.mods


ok(m93_probe(), "M93a 正常实现：1 轮修正到下一回合掉")
restore = mutate(effects_mod, "tick_round", lambda unit: [])
try:
    m93_caught = not m93_probe()
finally:
    restore()
ok(m93_caught, "M93b ⭐ 轮次不扣 → 临时加值变成永久，守卫抓到")


# --------------------------------------------------------------------------- #
section("M94 免死必须消耗每场次数")


def m94_probe() -> bool:
    u = Unit(id="U94", name="甲", team="pc", hp_max=20, hp=5, ac=10)
    u.item_effects = [{"名称": "余荫", "触发": "致命伤害", "效果": "免死",
                       "数值": 8, "保留生命": 1, "每场": 1, "冷却": 0,
                       "物品": "斗篷"}]
    u.item_charges = {"斗篷:余荫": {"max": 1, "current": 1, "cooldown": 0}}
    u.take_damage([DamagePacket(30, "")])
    first = u.hp > 0
    u.hp = 5
    second = u.take_damage([DamagePacket(30, "")])
    return first and second.dropped_to_zero


ok(m94_probe(), "M94a 正常实现：第一次免死，第二次照常归零")
_orig_available = item_effects_mod.available
restore = mutate(item_effects_mod, "available",
                 lambda unit, eff: True)
try:
    m94_caught = not m94_probe()
finally:
    restore()
ok(m94_caught, "M94b ⭐ 免死不扣次数 → 每刀都免死，守卫抓到")

# --------------------------------------------------------------------------- #
section("M95 头盔的护甲加值必须进 AC")


def m95_probe() -> bool:
    st = StateTree.from_template({"甲": new_character("甲", level=3)})
    st.commit([{"op": "insert", "path": "/角色列表/甲/物品/铁盔",
                "value": {"名称": "铁盔", "类别": "护甲", "护甲类型": "轻甲",
                          "护甲加值": 1, "已装备": True, "装备栏": "头盔"}}],
              actor="test", reason="m95")
    ac, parts, _w = armor_mod.armor_class(st, "甲")
    return ac == 11 and any("铁盔" in p for p in parts)


ok(m95_probe(), "M95a 正常实现：头盔 +1 计入 AC")
_orig_slot = armor_mod.slot_of
restore = mutate(armor_mod, "slot_of",
                 lambda item, name="": "上装"
                 if (item or {}).get("装备栏") == "头盔"
                 else _orig_slot(item, name))
try:
    m95_caught = not m95_probe()
finally:
    restore()
ok(m95_caught, "M95b ⭐ 头盔被当成上装 → AC 算错，守卫抓到")

# --------------------------------------------------------------------------- #
section("M96 开战必须重置装备次数")


def m96_probe() -> bool:
    u = Unit(id="U96", name="甲", team="pc", hp_max=20, hp=20, ac=10)
    u.item_effects = [{"名称": "余荫", "触发": "致命伤害", "效果": "免死",
                       "数值": 0, "保留生命": 1, "每场": 1, "冷却": 0,
                       "物品": "斗篷"}]
    u.item_charges = {"斗篷:余荫": {"max": 1, "current": 0, "cooldown": 0}}
    item_effects_mod.trigger_battle_start(u)
    return u.item_charges["斗篷:余荫"]["current"] == 1


ok(m96_probe(), "M96a 正常实现：开战次数回满")
restore = mutate(item_effects_mod, "reset_charges", lambda unit: None)
try:
    m96_caught = not m96_probe()
finally:
    restore()
ok(m96_caught, "M96b ⭐ 开战不重置 → 免死整场只能用一次，守卫抓到")

# --------------------------------------------------------------------------- #
section("M97 装备预算必须拦等级过高的神装")


def m97_probe() -> bool:
    items = {"传说斗篷": {"已装备": True, "词条": ["ward_grace"]}}
    item = {"词条": ["guard_2"]}
    return bool(catalog_mod.check_equip_budget(items, "守御纹章", item, 3))


ok(m97_probe(), "M97a 正常实现：4+2 > 5 被拦")
restore = mutate(catalog_mod, "check_equip_budget", lambda *a, **k: "")
try:
    m97_caught = not m97_probe()
finally:
    restore()
ok(m97_caught, "M97b ⭐ 预算闸失效 → 低级角色穿上神装，守卫抓到")

# --------------------------------------------------------------------------- #
section("M98 自写词条不能绕过品级下限")


def m98_probe() -> bool:
    try:
        catalog_mod.validate_entry({"名称": "便宜免死", "品级": 1,
                                    "适用": "装备", "触发": "致命伤害",
                                    "效果": "免死", "数值": 0})
        return False
    except ValueError:
        return True


ok(m98_probe(), "M98a 正常实现：常见品级的免死被拒")
restore = mutate(catalog_mod, "required_tier", lambda entry: 1)
try:
    m98_caught = not m98_probe()
finally:
    restore()
ok(m98_caught, "M98b ⭐ 品级下限失效 → 常见免死落库，守卫抓到")

# --------------------------------------------------------------------------- #
section("M99 离开场景必须收场（轮次修正/敌人/先攻）")


def m99_probe() -> bool:
    from engine.adventure import AdventureRunner as _AR
    from engine.adventure import load_adventure as _load
    st = StateTree.from_template({})
    loop = GameLoop(st, Ledger(), FakeModel())
    pc = Unit(id="U99", name="甲", team="pc", hp_max=10, hp=10, ac=10)
    pc.mods = [{"name": "湿滑", "kind": "save", "value": -1,
                "scope": "round", "rounds": 2, "target": "dexterity"}]
    foe = Unit(id="E99", name="鼠", team="enemy", hp_max=5, hp=5, ac=10)
    loop.field = [pc, foe]
    loop.initiative = [pc.id, foe.id]
    _AR(loop, _load(os.path.join(ROOT, "adventures",
                                 "雾渡的第三声钟.json")))._close_combat()
    return (not pc.mods and not loop.initiative
            and not [u for u in loop.field if u.team == "enemy"])


ok(m99_probe(), "M99a 正常实现：离开场景清轮次修正、撤敌人、收先攻")
import engine.adventure.runner as _runner_mod  # noqa: E402
restore = mutate(_runner_mod.AdventureRunner, "_close_combat",
                 lambda self: None)
try:
    m99_caught = not m99_probe()
finally:
    restore()
ok(m99_caught, "M99b ⭐ 不收场 → 轮次修正/敌人跟进下一场，守卫抓到")

# --------------------------------------------------------------------------- #
section("M100 进程级真调用闸门（DND_NO_REAL_CALLS）")


def m100_probe() -> bool:
    """闸门设上后，真客户端必须在**发请求前**失败，attempts=0。"""
    import engine.models.client as _cm
    req = ModelRequest(role="player", actor="甲", task="combat_turn", prompt="x")
    old = os.environ.get("DND_NO_REAL_CALLS")
    os.environ["DND_NO_REAL_CALLS"] = "1"
    hit = {"n": 0}
    orig = _cm.urllib.request.urlopen
    try:
        def _boom(*a, **k):
            hit["n"] += 1
            raise AssertionError("闸门没拦住")
        _cm.urllib.request.urlopen = _boom
        cli = _cm.RealModel(name="x", model="m", base_url="http://x",
                            api_key="k", enabled=True)
        r = cli.complete(req)
        return (not r.ok and "DND_NO_REAL_CALLS" in r.error
                and hit["n"] == 0 and cli.calls[-1].attempts == 0)
    finally:
        _cm.urllib.request.urlopen = orig
        if old is None:
            os.environ.pop("DND_NO_REAL_CALLS", None)
        else:
            os.environ["DND_NO_REAL_CALLS"] = old


ok(m100_probe(), "M100a 正常实现：进程闸门在发请求前拒绝，attempts=0")
import engine.models.client as _client_mod2  # noqa: E402
restore = mutate(_client_mod2, "_no_real_calls", lambda: False)
try:
    m100_caught = not m100_probe()
finally:
    restore()
ok(m100_caught, "M100b ⭐ 闸门失效 → 真请求会发出去，守卫抓到")

# --------------------------------------------------------------------------- #
section("M101 提示缓存命中要记账（各家字段）")


def m101_probe() -> bool:
    import engine.models.client as _cm
    ok1 = _cm._cached_tokens({"prompt_tokens_details": {"cached_tokens": 5}}) == 5
    ok2 = _cm._cached_tokens({"prompt_cache_hit_tokens": 7}) == 7
    ok3 = _cm._cached_tokens({"cache_read_input_tokens": 3}) == 3
    ok4 = _cm._cached_tokens({}) == 0
    return ok1 and ok2 and ok3 and ok4


ok(m101_probe(), "M101a 正常实现：OpenAI / DeepSeek / Anthropic 三种字段都认")
restore = mutate(_client_mod2, "_cached_tokens", lambda usage: 0)
try:
    m101_caught = not m101_probe()
finally:
    restore()
ok(m101_caught, "M101b ⭐ 缓存字段不解析 → 命中率永远 0，守卫抓到")

# --------------------------------------------------------------------------- #
section("M102 留痕容量预算：份数 + 总字节双阈值")


def m102_probe() -> bool:
    """history 裁剪必须**同时**认份数和总字节（Q04），且不把档删光。"""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="dnd-mut-budget-") as td:
        paths = []
        for i in range(4):
            p = os.path.join(td, f"f{i}.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x" * 400)
            os.utime(p, (2000 + i * 10, 2000 + i * 10))
            paths.append(p)
        one = os.path.getsize(paths[0])
        persist_mod.prune_kept(paths, keep=99, max_bytes=one * 2 + one // 4)
        left = [p for p in paths if os.path.exists(p)]
        return 0 < len(left) <= 2


ok(m102_probe(), "M102a 正常实现：字节闸生效，且至少留一份")


def _m102_keep_only(paths, *, keep=20, max_bytes=0):
    """变异版：只按份数删，**故意忽略 max_bytes**。"""
    alive = sorted((os.stat(p).st_mtime, p) for p in paths)
    dead = alive[:max(0, len(alive) - max(0, int(keep)))]
    for _t, p in dead:
        try:
            os.unlink(p)
        except OSError:
            pass
    return [p for _t, p in dead]


restore = mutate(persist_mod, "prune_kept", _m102_keep_only)
try:
    m102_caught = not m102_probe()
finally:
    restore()
ok(m102_caught, "M102b ⭐ 只剩份数闸 → 字节上限失守，守卫抓到")

# --------------------------------------------------------------------------- #
section("M103 剧情简报与世界秘密：DM 该看到，PC 包不许出现")


def _iso_setup():
    from engine.adventure import AdventureRunner as _AR
    from engine.adventure import load_adventure as _load
    adv = _load(os.path.join(ROOT, "adventures", "雾渡的第三声钟.json"))
    scene = adv.scenes[0]
    st = StateTree.from_template({"甲": new_character("甲", level=3)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    runner = _AR(loop, adv)
    world_secret = "墙缝里的税印-77c1"
    loop.world.register_location("地窖", description="潮湿的地窖",
                                secret=world_secret, discovered=True)
    asm = loop._assembler(phase="explore")
    asm.set_block("说明", "隔离", why="测试")
    loop._set_world(asm, "甲")
    return scene, runner, asm.build("甲", task="隔离").text(), world_secret


def m103_brief_probe() -> bool:
    scene, runner, _pc, _ws = _iso_setup()
    hint = runner.dm_hint(scene)
    secret = scene.brief.offstage
    return bool(secret) and secret in hint and secret not in _pc


def m103_world_probe() -> bool:
    _scene, _runner, pc, world_secret = _iso_setup()
    return world_secret not in pc and "🔒秘密" not in pc


ok(m103_brief_probe(), "M103a 正常实现：brief 进 DM 说明，不进 PC 包")
import engine.adventure.runner as _adv_runner  # noqa: E402
restore = mutate(_adv_runner, "dm_brief_text",
                 lambda scene, adv=None: "")
try:
    m103_caught = not m103_brief_probe()
finally:
    restore()
ok(m103_caught, "M103b ⭐ 简报没进 DM 说明 → DM 看不到幕后，守卫抓到")

ok(m103_world_probe(), "M103c 正常实现：世界秘密不进 PC 包")
from engine.state.world import World as _World  # noqa: E402
_orig_render = _World.render


def _leaky_render(self, viewer: str = "dm") -> str:
    return _orig_render(self, "dm")          # 变异：不管谁问都按 DM 渲染


restore = mutate(_World, "render", _leaky_render)
try:
    m103_world_caught = not m103_world_probe()
finally:
    restore()
ok(m103_world_caught, "M103d ⭐ 世界渲染忽略视角 → 秘密漏进 PC 包，守卫抓到")

# --------------------------------------------------------------------------- #
section("M104 跨章欠账：同一存档续跑也要注入")


def m104_probe() -> bool:
    """已跑过上一章的 runner，在第二章第一场仍要把欠账塞进 DM 说明。"""
    from engine.adventure import AdventureRunner as _AR
    from engine.adventure import load_adventure as _load
    adv = _load(os.path.join(ROOT, "adventures", "M2测试本-下-水寨的账.json"))
    st = StateTree.from_template({"甲": new_character("甲", level=3)})
    loop = GameLoop(st, Ledger(), FakeModel())
    loop.set_field([make_hero_unit(st, "甲")])
    loop.scenes_done = ["a1", "a2"]          # 假装上一章已经跑过
    runner = _AR(loop, adv, campaign_debt="【欠账】p3 水寨的灯")
    first = runner.dm_hint(adv.scenes[0])
    second = runner.dm_hint(adv.scenes[1])
    return "p3 水寨的灯" in first and "p3 水寨的灯" not in second


ok(m104_probe(), "M104a 正常实现：续跑时第一场注入欠账，第二场不重复")
import engine.adventure.runner as _runner_m  # noqa: E402
_orig_hint = _runner_m.AdventureRunner.dm_hint


def _only_fresh_session(self, scene):
    text = _orig_hint(self, scene)
    if self.scenes_done:
        text = "\n".join(x for x in text.splitlines()
                         if "p3 水寨的灯" not in x)
    return text


restore = mutate(_runner_m.AdventureRunner, "dm_hint", _only_fresh_session)
try:
    m104_caught = not m104_probe()
finally:
    restore()
ok(m104_caught, "M104b ⭐ 退回『只在全新一局注入』→ 续跑丢欠账，守卫抓到")

# --------------------------------------------------------------------------- #
section("M105 目标必须写在选项行上、且只写 id")


def m105_probe() -> bool:
    from engine.rules.bestiary import build_unit as _bu
    from engine.rules.menu import build_menu as _bm, render_menu as _rm
    bandit, _ = _bu("Bandit")
    bandit.position = (0, 0)
    pc = Unit(id="Q1", name="布兰", team="pc", hp_max=10, hp=10, ac=12)
    pc.position = (25, 0)
    menu = _bm(bandit, [bandit, pc])
    text = _rm(menu, battlefield=[bandit, pc])
    return ("🎯可选目标：Q1" in text
            and "🎯可选目标：Q1 布兰" not in text)


ok(m105_probe(), "M105a 正常实现：选项行带目标、只写 id")
import engine.rules.menu as _menu_m  # noqa: E402
_orig_line = _menu_m.Option.line


def _no_target_line(self, *, targets: str = "") -> str:
    return _orig_line(self, targets="")      # 变异：渲染时丢掉目标


restore = mutate(_menu_m.Option, "line", _no_target_line)
try:
    m105_caught = not m105_probe()
finally:
    restore()
ok(m105_caught, "M105b ⭐ 目标不写进选项 → 模型只能猜/漏填 target，守卫抓到")

# --------------------------------------------------------------------------- #
section("M106 选项 id 唯一：跳过攻击留空号也不能撞（外审 P1-1）")


def m106_probe() -> bool:
    from engine.rules.bestiary import build_unit as _bu
    from engine.rules.menu import build_menu as _bm
    from engine.rules.spells import get_spell as _gs
    from engine.rules.unit import Action as _A, Attack as _At, Unit as _U
    wiz = _U(id="W1", name="法师", team="pc", hp_max=18, hp=18, ac=12, level=3,
             speeds={"walk": 30},
             abilities={"strength": 8, "dexterity": 14, "constitution": 12,
                        "intelligence": 16, "wisdom": 12, "charisma": 10},
             cast_ability="intelligence",
             spell_slots={1: {"current": 2, "max": 2}})
    wiz.position = (0, 0)
    wiz.actions = [_A(name="Dagger", slot="action",
                      attacks=[_At(name="Dagger", is_melee=True, reach=5,
                                   attack_bonus=4, damage_notation="1d4")])]
    wiz.spells = [_gs("Magic Missile"), _gs("Fire Bolt")]
    wiz.reset_turn()
    foe, _ = _bu("Bandit", uid="E1", team="enemy")
    foe.position = (70, 0)          # 近战够不着 → 攻击选项被跳过，留空号
    menu = _bm(wiz, [wiz, foe])
    ids = [o.id for o in menu.options]
    if len(ids) != len(set(ids)):
        return False
    spell = next((o for o in menu.options if o.kind == "spell"), None)
    return spell is not None and menu.by_id(spell.id).kind == "spell"


ok(m106_probe(), "M106a 正常实现：跳过攻击后 id 仍唯一，法术 id 指向法术")
import engine.rules.menu as _menu_i  # noqa: E402
_orig_next = _menu_i.Menu.next_id


def _bad_next(self) -> str:
    return f"a{len(self.options) + 1}"     # 变异：退回"个数+1"


restore = mutate(_menu_i.Menu, "next_id", _bad_next)
try:
    m106_caught = not m106_probe()
finally:
    restore()
ok(m106_caught, "M106b ⭐ 退回『个数+1』发号 → 法术与移动撞号，守卫抓到")

# --------------------------------------------------------------------------- #
section("M107 历史只裁同槽：点号前缀不串槽（外审 P2-2）")


def m107_probe() -> bool:
    import tempfile
    from engine.persist import _archive_previous as _arch
    with tempfile.TemporaryDirectory(prefix="dnd-m107-") as td:
        a = os.path.join(td, "a.json")
        b = os.path.join(td, "a.b.json")
        for p in (a, b):
            with open(p, "w", encoding="utf-8") as f:
                f.write("{}")
        hist = os.path.join(td, "history")
        os.makedirs(hist)
        other = []
        for i in range(3):
            hp = os.path.join(hist, f"a.b.2026110{i}T000000.{i:06d}Z.json")
            with open(hp, "w", encoding="utf-8") as f:
                f.write("{}")
            os.utime(hp, (1000 + i, 1000 + i))
            other.append(hp)
        _arch(a, keep=1, max_bytes=None)
        return all(os.path.exists(p) for p in other)


ok(m107_probe(), "M107a 正常实现：a.json 的裁剪不碰 a.b.* 的历史")
import engine.persist as _persist_m  # noqa: E402
_orig_pat = getattr(_persist_m, "_HIST_RE_CACHE", None)


def _bad_archive(path, *, keep=None, max_bytes=None):
    """变异：退回 `startswith(stem + ".")` 圈同槽。"""
    import shutil
    from datetime import datetime, timezone
    base = os.path.basename(os.fspath(path))
    stem = os.path.splitext(base)[0]
    hist = os.path.join(os.path.dirname(os.path.abspath(path)), "history")
    os.makedirs(hist, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dst = os.path.join(hist, f"{stem}.{stamp}.json")
    shutil.copy2(path, dst)
    olds = sorted(os.path.join(hist, x) for x in os.listdir(hist)
                  if x.startswith(stem + ".") and x.endswith(".json"))
    _persist_m.prune_kept(olds, keep=keep or 0,
                          max_bytes=0 if max_bytes is None else max_bytes)
    return dst


restore = mutate(_persist_m, "_archive_previous", _bad_archive)
try:
    m107_caught = not m107_probe()
finally:
    restore()
ok(m107_caught, "M107b ⭐ 退回前缀圈同槽 → 删掉别人的历史，守卫抓到")

# --------------------------------------------------------------------------- #
section("M108 坏存档不能让回合开始抛异常（外审 P2-4）")


def m108_probe() -> bool:
    from engine.rules.item_effects import trigger_turn_start as _tts
    from engine.rules.unit import Unit as _U
    for charges in ({"x": {"cooldown": "bad"}}, {"x": "not-a-dict"}):
        u = _U(id="P", name="甲", team="pc", hp_max=10, hp=10, ac=12)
        u.item_charges = dict(charges)
        u.item_effects = [{"触发": "回合开始", "效果": "回复", "数值": 1,
                           "物品": "戒指", "名称": "回春"}]
        try:
            _tts(u)
        except Exception:                          # noqa: BLE001
            return False
    return True


ok(m108_probe(), "M108a 正常实现：坏存档的冷却值不打断回合开始")
import engine.rules.item_effects as _ie_m  # noqa: E402
_orig_tick = _ie_m.tick_cooldowns


def _bad_tick(unit) -> None:
    for rec in (unit.item_charges or {}).values():
        if rec.get("cooldown"):
            rec["cooldown"] = max(0, int(rec["cooldown"]) - 1)


restore = mutate(_ie_m, "tick_cooldowns", _bad_tick)
try:
    # 只变异 tick_cooldowns 不够：available 仍会守。这里同时退掉 available 的守。
    _orig_avail = _ie_m.available
    _ie_m.available = lambda unit, eff: True
    try:
        m108_caught = not m108_probe()
    finally:
        _ie_m.available = _orig_avail
finally:
    restore()
ok(m108_caught, "M108b 双变异（available 恒真 + 退回裸 int()）：坏存档打断回合——"
   "只证明这一组合被抓住，单改 tick 仍可能漏")

# --------------------------------------------------------------------------- #
section("M109 叙事 max_tokens 要给推理留位置（M2 上章掉过 2 次）")


def m109_probe() -> bool:
    import json as _json
    import engine.models.client as _cm
    from engine.models.client import ModelRequest as _Req

    class _Resp:
        def __init__(self, payload):
            self._p = _json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._p

    seen = {}
    # run_all 默认开进程闸门；本探针走假 transport，先摘掉、跑完还原
    _gate = os.environ.pop("DND_NO_REAL_CALLS", None)

    def fake(req, **kw):
        seen["cap"] = _json.loads(req.data.decode("utf-8"))["max_tokens"]
        return _Resp({"choices": [{"message": {"content": "旁白"}}],
                      "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

    orig = _cm.urllib.request.urlopen
    _cm.urllib.request.urlopen = fake
    try:
        _cm.RealModel(name="ds", model="m", base_url="http://fake",
                      api_key="k", enabled=True).complete(
            _Req(role="dm", actor="dm", task="narrate",
                 prompt="x", expect="narration"))
    finally:
        _cm.urllib.request.urlopen = orig
        if _gate is not None:
            os.environ["DND_NO_REAL_CALLS"] = _gate
    return seen.get("cap", 0) == 12000


ok(m109_probe(), "M109a 正常实现：叙事请求给满 12000")
import engine.models.client as _cm_m  # noqa: E402
_orig_complete = _cm_m.RealModel._complete


def _bad_complete(self, req):
    import json as _j
    import urllib.request as _u
    cap = self.max_tokens if req.expect != "narration" else min(self.max_tokens, 1200)
    payload = {"model": req.model or self.model,
               "messages": [{"role": "system", "content": self.SYSTEM},
                            {"role": "user", "content": req.prompt}],
               "max_tokens": cap}
    body = _j.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = _u.Request(self.base_url + "/chat/completions", data=body,
                   headers={"Authorization": "Bearer " + self.api_key,
                            "Content-Type": "application/json"})
    with _u.urlopen(r, timeout=self.timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return _cm_m.ModelReply(ok=True, text="旁白", model="m", prompt_tokens=5,
                            completion_tokens=1, raw=raw)


restore = mutate(_cm_m.RealModel, "_complete", _bad_complete)
try:
    m109_caught = not m109_probe()
finally:
    restore()
ok(m109_caught, "M109b ⭐ 退回 1200 上限 → 推理吃掉预算，守卫抓到")

# --------------------------------------------------------------------------- #
section("M110 DM 提示词要压住「地点打架」（Q29）")


def m110_probe() -> bool:
    from engine.rules.loop import GameLoop as _GL
    text = _GL.DM_SYSTEM
    return "当前地点" in text and "之前" in text


ok(m110_probe(), "M110a DM 系统提示写明「地点以当前地点为准，更早地名属于之前」")
import engine.rules.loop as _loop_m  # noqa: E402
_orig_dm = _loop_m.GameLoop.DM_SYSTEM


def _bad_dm(text):
    return (text.replace("6. **地点以「当前地点」为准**：", "")
                .replace("台账/记忆里更早的地名属于**之前**的场景，", "")
                .replace("不是现在的位置；不要为了调和两者花篇幅，直接接着当前地点写。", ""))


restore = mutate(_loop_m.GameLoop, "DM_SYSTEM", _bad_dm(_loop_m.GameLoop.DM_SYSTEM))
try:
    m110_caught = not m110_probe()
finally:
    restore()
ok(m110_caught, "M110b ⭐ 删掉那条地点口径 → 守卫抓到")

# --------------------------------------------------------------------------- #
section("M111 跨章兑现要落到上一章的进度里（Q23）")


def m111_probe() -> bool:
    import json as _json
    import tempfile as _tf
    import engine.adventure.campaign as _camp
    import engine.adventure.progress as _prog
    from engine.adventure import AdventureRunner as _AR, load_campaign as _lc
    from engine.adventure.model import load_adventure as _la
    from engine.models import FakeModel as _FM, TableClient as _TC
    from engine.rules.loop import GameLoop as _GL
    from engine.state import Ledger as _L, StateTree as _ST, new_character as _nc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    camp_path = os.path.join(root, "adventures", "M2测试本.campaign.json")
    tmp = _tf.mkdtemp(prefix="dnd-m111-")
    resolver = (lambda p: os.path.join(
        tmp, os.path.splitext(os.path.basename(p))[0] + ".progress.json"))
    orig_rs = None
    try:
        import tools.real_smoke as _rs
        orig_rs = _rs.progress_path_for
        _rs.progress_path_for = resolver
    except Exception:                                   # noqa: BLE001
        pass
    orig_cp, orig_pp = _camp.progress_path_for, _prog.progress_path_for
    _camp.progress_path_for = resolver
    _prog.progress_path_for = resolver
    try:
        st = _ST.from_template({"甲": _nc("甲", level=3, job="战士")})
        loop = _GL(st, _L(), _TC(dm=_FM(), planner=_FM(), players={"甲": _FM()}))
        camp = _lc(camp_path)
        adv2 = _la(os.path.join(root, "adventures",
                                "M2测试本-下-水寨的账.json"))
        runner = _AR(loop, adv2, campaign=camp, chapter_id="ch2")
        scene = next((s for s in adv2.scenes if s.id == "b5"), None)
        if scene is None:
            return False
        runner._pay_external(scene)
        doc = _json.load(open(os.path.join(
            tmp, "M2测试本-上-钟下的账.progress.json"), encoding="utf-8"))
        return bool(((doc.get("promises") or {}).get("p3") or {}).get("closed"))
    except FileNotFoundError:
        # "没落账" 就体现为上一章进度文件根本没生成 —— 这正是变异生效，
        # 算「被抓到」。和"环境坏了"要区分开（外审 C5）。
        return False
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"M111 探针无法构造：{exc}") from exc
    finally:
        _camp.progress_path_for = orig_cp
        _prog.progress_path_for = orig_pp
        if orig_rs is not None:
            import tools.real_smoke as _rs2
            _rs2.progress_path_for = orig_rs


ok(m111_probe(), "M111a 正常实现：ch2 兑现后 ch1 的 p3 真的关了")
import engine.adventure.runner as _runner_m  # noqa: E402
_orig_pay = _runner_m.AdventureRunner._pay_external


def _no_pay(self, scene):
    return None


restore = mutate(_runner_m.AdventureRunner, "_pay_external", _no_pay)
try:
    m111_caught = not m111_probe()
finally:
    restore()
ok(m111_caught, "M111b ⭐ 不落账到上一章 → 欠账永远收不掉，守卫抓到")

# --------------------------------------------------------------------------- #
section("M112 留痕参数口径：None = 不限，0 = 零预算（结构项 B）")


def m112_probe() -> bool:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="dnd-m112-") as td:
        paths = []
        for i in range(3):
            p = os.path.join(td, f"f{i}.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x" * 100)
            os.utime(p, (3000 + i, 3000 + i))
            paths.append(p)
        # None / None = 两边都不限：一份都不该删
        persist_mod.prune_kept(paths, keep=None, max_bytes=None)
        if any(not os.path.exists(p) for p in paths):
            return False
        # keep=0 = 删光
        persist_mod.prune_kept(paths, keep=0, max_bytes=None)
        if any(os.path.exists(p) for p in paths):
            return False
        # max_bytes=0 = 只留最新一份（外审 C3：这条口径以前从没被跑过）
        for i in range(3):
            p = os.path.join(td, f"g{i}.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x" * 100)
            os.utime(p, (4000 + i, 4000 + i))
        persist_mod.prune_kept([os.path.join(td, f"g{i}.json") for i in range(3)],
                               keep=None, max_bytes=0)
        left = [f"g{i}.json" for i in range(3)
                if os.path.exists(os.path.join(td, f"g{i}.json"))]
        return left == ["g2.json"]


ok(m112_probe(), "M112a 正常实现：None 不限、0 删光")
_orig_norm = persist_mod.prune_kept


def _bad_prune(paths, *, keep=None, max_bytes=None):
    """变异：把 None 当 0（旧口径混用），None 也会把文件删光。"""
    return _orig_norm(paths, keep=0 if keep is None else keep,
                      max_bytes=None)


restore = mutate(persist_mod, "prune_kept", _bad_prune)
try:
    m112_caught = not m112_probe()
finally:
    restore()
ok(m112_caught, "M112b ⭐ 把 None 当 0 → 不限会变成删光，守卫抓到")

# --------------------------------------------------------------------------- #
section("M113 备份失败不能拦存档（结构项 B）")


def m113_probe() -> bool:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="dnd-m113-") as td:
        hp = os.path.join(td, "s.json")
        with open(hp, "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(td, "history"), "w", encoding="utf-8") as f:
            f.write("not a dir")            # makedirs 必然失败
        try:
            persist_mod._archive_guarded(hp, keep=3, max_bytes=None)
        except Exception:                    # noqa: BLE001
            return False
        return os.path.exists(hp)


ok(m113_probe(), "M113a 正常实现：history 写不进去，存档仍成功")


def _unguarded(path, *, keep, max_bytes):
    persist_mod._archive_previous(path, keep=keep, max_bytes=max_bytes)


restore = mutate(persist_mod, "_archive_guarded", _unguarded)
try:
    m113_caught = not m113_probe()
finally:
    restore()
ok(m113_caught, "M113b ⭐ 备份异常直接冒泡 → 存档被判失败，守卫抓到")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
_mut_n = len(re.findall(r'section\("M\d+',
                        open(__file__, encoding="utf-8").read()))
print(f"共 {COUNT} 项：全部通过（{_mut_n} 个变异全部被抓到）")







