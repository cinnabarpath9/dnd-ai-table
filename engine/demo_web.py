# coding=utf-8
"""P5-C 验收 demo：**真人网页座位**（本地起服务，浏览器里打一局）。

两种跑法：

    python -m engine.demo_web                 # 真浏览器：打印地址，你去点
    python -m engine.demo_web --auto          # 自带"假浏览器"（走同一套 HTTP），无人值守

`--auto` 不是绕过前端：它用 `urllib` 调 `/api/state` 和 `/api/answer`，
和真浏览器走的是同一条协议；所以它能证明"网页这条路是通的"，零花费、可进回归。

只绑 127.0.0.1，没有鉴权 —— 本机自己玩的东西，不该暴露到局域网。
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys
import threading
import time
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.adventure import (  # noqa: E402
    AdventureRunner, build_journal, campaign_guide, load_adventure,
)
from engine.demo_table import HUMAN, KIT, PARTY, _DemoDM  # noqa: E402
from engine.lifepath import LifepathError, QUESTIONS as LIFEPATH_QUESTIONS  # noqa: E402
from engine.lifepath import build as lifepath_build  # noqa: E402
from engine.lifepath import questions as lifepath_questions  # noqa: E402
from engine.creation import (  # noqa: E402
    CreationError, CLASS_CN, SPECIES_CN, class_info, create_character,
    list_options, roll_abilities, spells_for, suggest_abilities,
)
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, ModelClient, RealModel, TableClient  # noqa: E402
from engine.models.client import ModelError, ModelRequest, read_api_key  # noqa: E402
from engine.persist import dump_game, load_from_dict, save_game  # noqa: E402
from engine.rules.constants import CONDITIONS  # noqa: E402
from engine.rules.equipment import slot_map  # noqa: E402
from engine.rules.spells import SpellParseError, get_spell  # noqa: E402
from engine.srd.store import NotFound  # noqa: E402
from engine.rules.economy import (  # noqa: E402
    EconomyError, POTION_CN, consumable_spec, shop_stock,
)
from engine.rules.loop import (  # noqa: E402
    GameLoop, _SPELL_CN, make_hero_unit,
)
from engine.state import Ledger, StateTree, WorldError, new_character  # noqa: E402
from engine.web_actions import apply_action  # noqa: E402
from engine.webui import (  # noqa: E402
    TableServer, WebBridge, WebHumanSeat, viewer_log_filter,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ADV = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")


#: 非真人座位的工厂：默认 None（假模型）；`--real` 时由 main 换成真模型。
#: 页面/HTTP 代码一行都不用改 —— 桌位换人，前端不变。
SEAT_FACTORY = None
COMPANION_NAMES = ("艾尔玛", "罗恩", "小翠", "塔莉亚")


def list_adventures() -> list[dict]:
    """列出本机能通过校验的剧本；包含本机 imported/，但不入库、不分发。"""
    out: list[dict] = []
    root = os.path.join(ROOT, "adventures")
    paths = sorted(glob.glob(os.path.join(root, "*.json")))
    paths += sorted(glob.glob(os.path.join(root, "imported", "*.json")))
    for path in paths:
        try:
            adv = load_adventure(path)
        except Exception:                            # noqa: BLE001
            continue
        rid = os.path.relpath(path, root).replace(os.sep, "/")
        out.append({"id": rid, "title": adv.title, "path": path})
    return out


def load_seat_config(path: str) -> dict:
    """读每座位模型配置（JSON）。**只放 env 名，不把 key 写进仓库。**"""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("座位配置顶层必须是对象")
    seats = cfg.get("seats")
    if not isinstance(seats, dict):
        raise ValueError("座位配置缺少 seats 对象")
    defaults = cfg.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("座位配置 defaults 必须是对象")
    return cfg


def seat_spec(label: str, cfg: dict | None) -> dict:
    """按标签取座位配置：精确 → pilot/pc:* → default；defaults 先铺底。"""
    cfg = cfg or {}
    seats = cfg.get("seats") or {}
    spec = dict(cfg.get("defaults") or {})
    candidates = [label]
    if label.startswith("pc:pilot:"):
        candidates.append("pilot")
    if label.startswith("pc:"):
        candidates.append("pc:*")
    candidates.append("default")
    for key in candidates:
        if isinstance(seats.get(key), dict):
            spec.update(seats[key])
            break
    return spec


def seat_api_key(spec: dict, fallback: str = "") -> str:
    """座位 key：优先 api_key_env，其次全局 --api-key 读到的 key。"""
    env = str(spec.get("api_key_env") or "").strip()
    if env:
        return os.environ.get(env, "")
    return fallback or ""


def missing_seat_envs(cfg: dict | None) -> list[str]:
    cfg = cfg or {}
    missing: list[str] = []
    groups = [cfg.get("defaults") or {}, cfg.get("seats") or {}]
    for group in groups:
        for label, spec in group.items():
            if not isinstance(spec, dict):
                continue
            env = str(spec.get("api_key_env") or "").strip()
            if env and not os.environ.get(env):
                missing.append(f"{label}:{env}")
    return missing


class CappedReal(RealModel):
    """网页演示用的真模型：全程共享一个计数器，到上限就**拒绝发请求**。

    网页会话会自己往下跑（DM/planner/3 个同伴），没有上限就可能一直烧钱；
    超过上限的调用返回失败 `ModelReply`，由循环记成“本回合没动”，不崩。
    """

    def __init__(self, counter: dict, *, cap: int, **kw):
        super().__init__(**kw)
        self.counter = counter
        self.cap = int(cap)

    def _complete(self, req):
        if self.counter.get("n", 0) >= self.cap:
            raise ModelError(f"网页演示已到调用上限 {self.cap}（不再发请求）",
                             retryable=False)
        self.counter["n"] = self.counter.get("n", 0) + 1
        return super()._complete(req)


def _auto_option(req) -> str:
    """假模型/兜底托管：从菜单里挑一个合法动作（零花费、确定性）。"""
    opts = list((req.allowed or {}).get("options") or [])
    if not opts:
        return ""
    for o in opts:
        oid = str(o.get("id") or "")
        if not oid:
            continue
        targets = list(o.get("targets") or [])
        if o.get("requires_target") and targets:
            return f"{oid} {targets[0]}"
        if not o.get("requires_target"):
            return oid
    return ""


def _pilot_callable(table, log):
    """真模型托管：走牌桌的 `complete_pilot`（记账+思考），失败**明说**、不静默兜底。"""
    def pilot(req) -> str:
        try:
            rep = table.complete_pilot(req)
        except Exception as exc:                     # noqa: BLE001
            raise ModelError(f"托管模型调用失败：{type(exc).__name__}: {exc}") from exc
        if not rep.ok:
            raise ModelError(f"托管模型返回失败：{rep.error or '空回复'}")
        try:
            obj = json.loads(rep.text)
        except (TypeError, ValueError) as exc:
            log(f"⚠️ 托管模型输出不是 JSON：{(rep.text or '')[:80]}")
            raise ModelError("托管模型输出不是 JSON") from exc
        if not isinstance(obj, dict):
            raise ModelError("托管模型输出不是对象")
        oid = str(obj.get("id") or "")
        if not oid:
            raise ModelError("托管模型没有给出选项 id")
        target = str(obj.get("target") or "")
        say = str(obj.get("say") or "")
        return f"{oid} {target} {say}".strip()
    return pilot


def make_web_table(bridge: WebBridge, *, narrations: list[str] | None = None,
                   human_name: str = HUMAN, mode: str = "normal",
                   companions: list[str] | None = None) -> TableClient:
    """只造"座位表"（读档时也要用同一条真人座位）。

    mode：
      · `normal`   —— 一个真人座位（等网页回答）
      · `hosted`   —— 真人座位仍在，但 `auto` 让 AI 代打（人可随时"接管"）
      · `spectate` —— **没有真人座位**，四个 PC 全是 AI（观战/测试）
    """
    mode = mode if mode in ("normal", "hosted", "spectate") else "normal"
    mates_names = list(COMPANION_NAMES if companions is None else companions)[:4]
    if SEAT_FACTORY is None:
        dm = _DemoDM(narrations=narrations or [])
        planner = FakeModel(name="敌方规划（假模型）")
        mates = {n: FakeModel(name=f"{n}的模型（假）") for n in mates_names}
        ai_seat = lambda: FakeModel(name=f"{human_name} 的模型（观战）")  # noqa: E731
        pilot_seat = None
    else:
        dm = SEAT_FACTORY("dm")
        planner = SEAT_FACTORY("planner")
        mates = {n: SEAT_FACTORY(f"pc:{n}") for n in mates_names}
        ai_seat = lambda: SEAT_FACTORY(f"pc:{human_name}")  # noqa: E731
        pilot_seat = SEAT_FACTORY(f"pc:pilot:{human_name}")

    n_people = len(mates_names) + 1
    if mode == "spectate":
        human = ai_seat()
        players = {**mates, human_name: human}
        table = TableClient(dm=dm, planner=planner, players=players,
                            name=f"{n_people}人桌（观战·全 AI）")
        table.human_seat = None
    else:
        seat = WebHumanSeat(bridge, name=f"真人·{human_name}")
        table = TableClient(dm=dm, planner=planner,
                            players={**mates, human_name: seat},
                            pilot=pilot_seat,
                            name=f"{n_people}人桌（网页版）")
        table.human_seat = seat
        auto = (_pilot_callable(table, bridge.log)
                if (mode == "hosted" and pilot_seat)
                else (_auto_option if mode == "hosted" else None))
        seat._auto = auto
        table.host_pilot = auto if mode == "hosted" else None
    table.web_mode = mode
    return table


def build_web_table(bridge: WebBridge, *, narrations: list[str] | None = None,
                    human_char: dict | None = None,
                    human_name: str = HUMAN, mode: str = "normal",
                    title: str = "哨塔的拖痕",
                    companions: list[str] | None = None,
                    companion_chars: dict[str, dict] | None = None):
    """和 demo_table 同一张桌子，只把真人座位换成网页版。

    `human_char` 给了就用**建卡向导产出的角色**替换默认真人（其余三名同伴不变）；
    没给就走默认四人队。数值已经在 `create_character` 里由 SRD + 规则算过，
    这里只负责放进状态树、发开局装备。
    """
    mates_names = list(COMPANION_NAMES if companions is None else companions)[:4]
    custom = companion_chars or {}
    party = {n: (custom.get(n) or new_character(n, **PARTY[n]))
             for n in mates_names}
    kit = [row for row in KIT if row[0] in party]
    name = human_name or HUMAN
    if human_char is not None:
        party[name] = human_char
    else:
        party[HUMAN] = new_character(HUMAN, **PARTY[HUMAN])
        name = HUMAN
    #: `human_char=None` 时不能把队友清掉：这里是“加真人”，不是“只留真人”。
    kit = [row for row in KIT if row[0] in party]
    state = StateTree.from_template(party, current=name, label=title)
    if kit:
        state.commit([
            {"op": "insert", "path": path, "value": value}
            for _who, path, value in kit
        ], actor="填表", reason="开局装备与法术准备")

    table = make_web_table(bridge, narrations=narrations, human_name=name,
                           mode=mode, companions=mates_names)
    loop = GameLoop(state, Ledger(), table, rng=Random(20261104),
                    label=f"{title}（网页）", on_line=bridge.log)
    loop.set_field([make_hero_unit(state, n) for n in state.characters()])
    return state, loop


def campaign_pitch(adv) -> str:
    """开局大厅的战役介绍。优先用剧本 meta.pitch；没有就按标题/幕/第一场拼。

    这里是**脚本化 DM 介绍**，不调模型：开局大厅不该先烧一次调用，
    也不该让模型临时编出与剧本不符的世界观。要更丰富的介绍，写进 `meta.pitch`。
    """
    meta = getattr(adv, "meta", {}) or {}
    custom = str(meta.get("pitch") or meta.get("序幕") or "").strip()
    if custom:
        lines = [custom]
        guide = campaign_guide(adv)
        if guide:
            lines.extend(["", guide])
        return "\n".join(lines)
    rules = str(meta.get("规则") or "").strip()
    span = str(meta.get("时长") or "").strip()
    acts = " / ".join(str(getattr(a, "title", "") or "") for a in adv.acts)
    first = adv.scenes[0] if adv.scenes else None
    first_title = str(getattr(first, "title", "") or "") if first else ""
    present = str(getattr(getattr(first, "brief", None), "present", "") or "") if first else ""
    lines = [f"《{adv.title}》"]
    if rules or span:
        lines.append("　".join(x for x in (rules, span) if x))
    if acts:
        lines.append(f"第一幕：{acts}" + (f"｜开场：{first_title}" if first_title else ""))
    if present:
        lines.append(present)
    guide = campaign_guide(adv)
    if guide:
        lines.extend(["", guide])
    lines.append("DM：今晚先确认角色卡；确认后我们从第一场开始。")
    return "\n".join(lines)


def character_cards(state) -> list[dict]:
    """四张预生成角色卡。数值来自状态树，不经过模型。"""
    out = []
    for name in state.characters():
        c = state.read(f"/角色列表/{name}") or {}
        jobs = c.get("职业") or {}
        hp = c.get("生命值") or {}
        attrs = {str(k): int(v) for k, v in (c.get("属性") or {}).items()}
        top = max(attrs.items(), key=lambda kv: kv[1]) if attrs else ("—", 0)
        out.append({
            "name": name,
            "job": "、".join(f"{k} {v}" for k, v in jobs.items()) or "—",
            "level": sum(int(v) for v in jobs.values()) or 1,
            "hp": int(hp.get("当前") or 0),
            "hp_max": int(hp.get("最大") or 0),
            "ac": int(c.get("护甲等级") or 10),
            "top": {"name": top[0], "value": top[1]},
        })
    return out


INTRO_FALLBACK = {
    "艾尔玛": "艾尔玛把剑柄往桌上一放：「战士，艾尔玛。站我后面，我替你挡。」",
    "罗恩": "罗恩合上法术书：「法师，罗恩。别碰我的书，其他都好商量。」",
    "小翠": "小翠把短剑在指间一转：「游荡者，小翠。门轴响不响，我先去听。」",
}

GUIDED_PROMPTS = {
    "motivation": "是什么核心动机或执念，驱使你的角色踏上这段旅途？",
    "bonds": "有没有一个重要的羁绊角色？可以是 NPC、队友，"
             "也可以是你喜欢的作品原型；没有也可以说没有。",
}


def audit_card(state, name: str) -> str:
    """DM 审卡：只看最高属性，给一句人话评价；不改数值。"""
    c = state.read(f"/角色列表/{name}") if state is not None else None
    attrs = {str(k): int(v) for k, v in ((c or {}).get("属性") or {}).items()}
    if not attrs:
        return f"{name}：还没读到属性。"
    k, v = max(attrs.items(), key=lambda kv: kv[1])
    if v >= 20:
        tail = "这是被幸运女神眷顾的开局——很高，但别忘了骰子仍会翻脸。"
    elif v >= 17:
        tail = "这是很扎实的开局，够你应付前几场硬仗。"
    elif v >= 16:
        tail = "够用，但不是压倒性的强；打起来要靠站位和队友。"
    else:
        tail = "略低于 16，旅途会更坎坷；想稳一点可以回建卡面板调一调。"
    return f"{name}：最高属性是{k} {v}。{tail}"


def lobby_opening(adv, notes: dict | None = None,
                  intros: list[dict] | None = None,
                  party_size: int = 4) -> str:
    """确认后的一句启航旁白；把玩家刚答的动机/羁绊带进开场。"""
    first = adv.scenes[0] if adv.scenes else None
    scene = str(getattr(first, "title", "") or "").strip()
    where = f"第一场「{scene}」" if scene else "第一场"
    extra = []
    notes = notes or {}
    if str(notes.get("motivation") or "").strip():
        extra.append(f"你带着「{str(notes['motivation']).strip()}」的动机上路")
    if str(notes.get("bonds") or "").strip():
        extra.append(f"羁绊：{str(notes['bonds']).strip()}")
    tail = "；".join(extra)
    if tail:
        tail += "。"
    crew = ""
    if intros:
        short = "；".join(
            f"{str(x.get('name') or '')}：{str(x.get('text') or '')[:60]}"
            for x in intros if str(x.get("text") or "").strip())
        if short:
            crew = f"同桌已就位——{short}。"
    return (f"《{adv.title}》的帷幕拉开。{party_size} 张角色卡都已确认，我们从{where}开始。"
            f"{crew}{tail}现在，你要做什么？")


def map_snapshot(loop) -> dict:
    """战斗地图快照（给网页 SVG）。**探索阶段没有坐标就不画地理。**"""
    if loop is None or not getattr(loop, "initiative", None):
        return {"battle": False, "round": getattr(loop, "round", 0)}
    order = [u.id for u, _v in loop.initiative]
    score = {u.id: int(v) for u, v in loop.initiative}
    units = []
    for u in loop.field:
        if u.team not in ("pc", "ally", "enemy"):
            continue
        units.append({
            "id": u.id, "name": u.name, "team": u.team,
            "hp": int(u.hp), "hp_max": int(u.hp_max),
            "temp_hp": int(getattr(u, "temp_hp", 0)),
            "pos": [float(u.position[0]), float(u.position[1])],
            "init": order.index(u.id) if u.id in order else -1,
            "init_score": score.get(u.id, 0),
            "conditions": [k for k, v in (u.conditions or {}).items() if v != 0],
            "alive": bool(u.alive),
        })
    return {"battle": True, "round": int(loop.round),
            "units": units, "order": order}


def graph_snapshot(loop) -> dict | None:
    """探索期世界节点图（U13）：点 + 通道 + 迷雾。**按玩家视角过滤。**"""
    if loop is None:
        return None
    g = loop.world.graph(HUMAN)
    region = getattr(loop, "region_map", None)
    if isinstance(g, dict) and isinstance(region, dict):
        g = {**g, "hexmap": region}
    return g


def _human_from_save(raw: dict) -> str:
    """存档里"真人是谁"：四个座位里不在固定同伴中的那个。"""
    state_d = (raw or {}).get("state") or {}
    stats = state_d.get("stat_data") or state_d
    chars = stats.get("角色列表") or {}
    others = [n for n in chars
          if n not in ("艾尔玛", "罗恩", "小翠", "塔莉亚")]
    if len(others) == 1:
        return str(others[0])
    if HUMAN in chars:
        return HUMAN
    return str(others[0]) if others else HUMAN


def shop_snapshot(loop) -> dict:
    """当前地点的货架（U12）。**没有商店就是空货架**，不凭空进货。"""
    if loop is None:
        return {"place": "", "items": []}
    if getattr(loop, "initiative", None):
        return {"place": "", "items": [], "note": "战斗中不能交易"}
    try:
        stock = shop_stock(loop.state)
    except EconomyError as exc:
        return {"place": "", "items": [], "note": str(exc)}
    items = []
    for name in stock:
        label, price = POTION_CN.get(name, name), None
        try:
            spec = consumable_spec(name)
            label = spec.get("中文名") or label
            price = spec.get("价格")
        except EconomyError:
            pass
        items.append({"name": name, "label": label, "price": price})
    return {"place": str(loop.state.read("/世界/当前地点") or ""),
            "items": items}


def _mod_lines(unit) -> list[str]:
    """角色身上的临时修正（H2b）——只给人话，不把内部结构倒给前端。"""
    if unit is None:
        return []
    from .rules.effects import describe
    return describe(unit)


def save_dir_writable(path: str) -> bool:
    """真写一个临时文件再删（不是只看目录存在）。自检要这个证据。"""
    import tempfile
    try:
        os.makedirs(path, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dnd-diag-", suffix=".tmp", dir=path)
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


def _key_present() -> bool:
    """只回答"有没有钥匙"，**绝不返回内容**。"""
    try:
        from engine.models.client import read_api_key
        return bool(read_api_key())
    except Exception:                                    # noqa: BLE001
        return bool(os.environ.get("QIANFAN_API_KEY"))


def _catalog_rows(extra=None) -> list[dict]:
    from .rules.effect_catalog import catalog_payload
    return catalog_payload(extra)


def _entry_rows(ids, extra=None) -> list[dict]:
    """词条 id → 卡面用的短行（名称/品级名）。"""
    from .rules.effect_catalog import TIER_NAMES, get_entry
    out = []
    for eid in ids or []:
        e = get_entry(str(eid), extra)
        if e is None:
            out.append({"id": str(eid), "名称": str(eid), "品级名": "未知"})
            continue
        out.append({"id": str(eid), "名称": str(e.get("名称")),
                    "品级名": TIER_NAMES.get(int(e.get("品级") or 1), "")})
    return out


def _tier_budget(c: dict, extra=None) -> dict:
    """角色已装备词条的品级预算（H3）。"""
    from .rules.effect_catalog import character_budget, equipped_tier_total
    try:
        level = sum(int(v) for v in (c.get("职业") or {}).values()) or 1
    except (TypeError, ValueError):
        level = 1
    return {"level": level,
            "used": equipped_tier_total(c.get("物品") or {}, extra=extra),
            "max": character_budget(level)}


def npc_snapshot(loop) -> list[dict]:
    """已登记人物 + 数值块（DM 面板用）。只读，不写状态。"""
    if loop is None:
        return []
    out = []
    for name in loop.world.npcs():
        node = loop.world.npc(name) or {}
        unit = next((u for u in loop.field
                     if u.name == name or u.id == name), None)
        out.append({
            "name": str(name),
            "role": str(node.get("身份") or ""),
            "stats": dict(node.get("数值") or {}),
            "entries": _entry_rows(node.get("词条") or [],
                                    loop.world.entries()),
            "in_field": unit is not None,
            "hp": unit.hp if unit else None,
            "hp_max": unit.hp_max if unit else None,
            "team": unit.team if unit else "",
        })
    return out


def sheet_snapshot(loop, name: str = HUMAN) -> dict:
    """角色卡快照（状态栏/背包浮层用）。只读，不写状态。"""
    if loop is None:
        return {"sheet": None}
    c = loop.state.read(f"/角色列表/{name}") or {}
    u = next((x for x in loop.field if x.name == name or x.id == name), None)
    hp = c.get("生命值") or {}
    prof = c.get("熟练配置") or {}
    cast = c.get("施法") or {}
    extra_entries = loop.world.entries()
    items = [{"name": k, **v, "lore": loop.world.lore("物品", k),
              "词条信息": _entry_rows(v.get("词条") or [], extra_entries)}
             for k, v in (c.get("物品") or {}).items()
             if isinstance(v, dict)]
    conds = []
    if u is not None:
        for key, rounds in (u.conditions or {}).items():
            label = CONDITIONS[key].name_cn if key in CONDITIONS else key
            conds.append({"key": key, "name": label, "rounds": int(rounds)})
    book = {}
    for sname, v in (cast.get("法术书") or {}).items():
        rec = dict(v) if isinstance(v, dict) else {}
        rec["lore"] = loop.world.lore("法术", str(sname))
        try:
            sp = get_spell(_SPELL_CN.get(str(sname), str(sname)))
        except (NotFound, SpellParseError):
            sp = None
        if sp is not None:
            rec.setdefault("施法时间", sp.casting_time)
            rec.setdefault("射程", sp.range_text)
            rec.setdefault("持续时间", sp.duration)
            rec.setdefault("专注", bool(sp.concentration))
            rec.setdefault("仪式", bool(sp.ritual))
            rec.setdefault("规则描述", sp.desc)
        book[str(sname)] = rec
    skill_lore = {str(k): loop.world.lore("技能", str(k))
                  for k in (prof.get("技能") or {})}
    # DM 造的技能（H1）：机械字段 + 技能志文字，卡面展示/编辑用
    dm_actions = []
    for aname, av in (c.get("动作") or {}).items():
        if not isinstance(av, dict):
            continue
        row = {"name": str(aname), **av,
               "lore": loop.world.lore("技能", str(aname))}
        dm_actions.append(row)
    if not conds and c.get("当前状态"):
        conds.append({"key": "", "name": str(c["当前状态"]), "rounds": 0})
    return {"sheet": {
        "name": name,
        "hp": int(hp.get("当前", 0)), "hp_max": int(hp.get("最大", 0)),
        "temp_hp": int(hp.get("临时", 0)),
        "hit_dice": hp.get("生命骰") or {},
        "ac": int(c.get("护甲等级", 10)),
        "speed": c.get("速度") or {},
        "init": int(c.get("先攻", 0)),
        "jobs": c.get("职业") or {},
        "abilities": c.get("属性") or {},
        "saves": list(prof.get("豁免") or []),
        "skills": dict(prof.get("技能") or {}),
        "skill_lore": skill_lore,
        "dm_actions": dm_actions,
        "slots": {str(k): dict(v) for k, v in (cast.get("法术位") or {}).items()},
        "spellbook": book,
        "resources": dict(c.get("资源") or {}),
        "coins": {k: int(v) for k, v in
                  (c.get("钱币") if isinstance(c.get("钱币"), dict) else {}).items()
                  if k in ("金币", "银币", "铜币") and isinstance(v, int)},
        "items": items,
        "equipment": slot_map(c.get("物品") or {}),
        "conditions": conds,
        "concentrating": str(getattr(u, "concentrating_on", "") or ""),
        "exhaustion": int(getattr(u, "exhaustion", 0) or 0),
        "mod_lines": _mod_lines(u),
        "tier_budget": _tier_budget(c, extra_entries),
    }}


# --------------------------------------------------------------------------- #
# 网页会话：新开 / 存档 / 读档（同一进程内换 loop + 重启推进线程）
# --------------------------------------------------------------------------- #

class WebSession:
    """一局网页牌桌。**存档/读档/重开**都在这里换 loop，HTTP 那边只看 bridge。"""

    def __init__(self, bridge: WebBridge, adv, save_dir: str,
                 *, session_zero: bool = False, adv_path: str = "",
                 ai_count: int = 3, characters_dir: str | None = None):
        self.bridge = bridge
        self.adv = adv
        self.adv_path = os.path.abspath(adv_path) if adv_path else ""
        self.stages = list_adventures()
        self._human_char: dict | None = None
        self.ai_count: int = max(0, min(4, int(ai_count)))
        self._companion_chars: dict[str, dict] = {}
        self._companion_source: str = "pregen"
        self._companion_errors: dict[str, str] = {}
        self.save_dir = os.path.abspath(save_dir)
        self.characters_dir = os.path.abspath(
            characters_dir or os.path.join(ROOT, "characters"))
        os.makedirs(self.characters_dir, exist_ok=True)
        os.makedirs(self.save_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.gen = 0
        self.thread: threading.Thread | None = None
        #: 自动读档重试次数：同一检查点只救一次，避免死循环。
        self._auto_restores = 0
        self.state: StateTree | None = None
        self.loop: GameLoop | None = None
        #: 真人座位用的是谁（默认布兰；建卡开局时换成新角色）
        self.human_name: str = HUMAN
        #: normal / hosted / spectate —— 观战与托管会**自动往下跑**，不等网页。
        self.mode: str = "normal"
        #: `lobby` = 已搭好桌、等四人确认；`playing` = AdventureRunner 已启动。
        self.phase: str = "idle"
        self.lobby: dict | None = None
        #: 确认上桌后保留的开局记录（进存档，读档后仍能看）
        self._session_zero: dict | None = None
        #: 真桌开局大厅：先讲战役 + 确认四张预生成卡，再开第一场。
        self.session_zero: bool = bool(session_zero)
        #: 自动模式的轮间隔（秒）：假模型一局不到 1 秒，不放慢就只剩结算页。
        self.pace: float = 1.2
        self._narr = [s.narration for s in adv.scenes if s.narration]

    def _companion_names(self) -> list[str]:
        out = list(self._companion_chars)
        for n in COMPANION_NAMES:
            if len(out) >= self.ai_count:
                break
            if n not in out:
                out.append(n)
        return out[:self.ai_count]

    def snapshot_extra(self) -> dict:
        """网页状态快照。**真人名是可变字段**，卡/视野/剧情都不能硬编码 HUMAN。"""
        loop = self.loop
        if loop is None or self.state is None:
            return {"map": {"battle": False}, "sheet": None, "graph": None,
                    "npcs": [], "catalog": _catalog_rows(),
                    "shop": {"place": "", "items": []}, "slots": [],
                    "session_zero": self._session_zero,
                    "lobby": self.lobby if self.phase == "lobby" else None,
                    "quests": build_journal(self.adv, None),
                    "scene": {"text": "", "round": 0,
                              "story": {"total": len(self.adv.scenes)}}}
        human = self.human_name or HUMAN
        return {
            "map": map_snapshot(loop),
            "graph": graph_snapshot(loop),
            "shop": shop_snapshot(loop),
            "sheet": sheet_snapshot(loop, name=human).get("sheet"),
            "npcs": npc_snapshot(loop),
            "catalog": _catalog_rows(loop.world.entries()),
            "quests": build_journal(self.adv, loop),
            "slots": self.list_slots(),
            "session_zero": self._session_zero,
            "lobby": self.lobby if self.phase == "lobby" else None,
            "scene": {
                "text": loop.world.render(human),
                "round": loop.round,
                "story": {
                    "adventure": loop.adventure_title or self.adv.title,
                    "act": loop.story_act, "scene": loop.story_scene,
                    "status": loop.story_status,
                    "done": list(loop.scenes_done),
                    "total": len(self.adv.scenes),
                    "act_total": len(self.adv.acts)}}}

    def _latest(self) -> str:
        return os.path.join(self.save_dir, "latest.json")

    def _spawn(self, state: StateTree, loop: GameLoop) -> None:
        self.state, self.loop = state, loop
        self.phase = "playing"
        self.lobby = None
        self.gen += 1
        gen = self.gen
        self.bridge.set_generation(gen)
        loop.on_line = lambda line, g=gen: self._log_if(g, line)
        self.bridge.mode = self.mode
        if self.mode != "normal":
            loop.on_round = lambda g=gen: self._pace_if(g)
        # 「思考提示」：只显示**非真人**座位的思考（真人座位在等操作，不算"在想"）。
        loop.client.on_call = (
            lambda actor, task, phase, g=gen:
            self._think_if(g, actor, task, phase))
        seat = getattr(loop.client, "human_seat", None)
        if seat is not None:
            seat.generation = gen

        def worker():
            runner = AdventureRunner(
                loop, self.adv,
                on_scene_end=lambda sc, st=state, lp=loop, g=gen:
                    self._checkpoint_scene(sc, state=st, loop=lp, gen=g))
            try:
                runner.run_all(resume=True)
            except Exception as exc:                 # noqa: BLE001
                if gen == self.gen:
                    self.bridge.log(f"⚠️ {type(exc).__name__}: {exc}")
                    self.bridge.finish(error=f"{type(exc).__name__}: {exc}")
                return
            if gen != self.gen:
                return
            if runner.failed:
                cpath = self._checkpoint_path()
                if not os.path.exists(cpath) or self._auto_restores >= 1:
                    self.bridge.log(f"【系统】{runner.failure_reason or '全团倒下'}；没有可用检查点。")
                    self.bridge.finish(error=runner.failure_reason or "全团倒下")
                    return
                self._auto_restores += 1
                self.bridge.log("【系统】全团倒下，自动读档到上一个检查点。")
                def restore():
                    try:
                        res = self._load_path(cpath, "已自动读档到上一个检查点")
                    except Exception as exc:         # noqa: BLE001
                        self.bridge.finish(error=f"自动读档失败：{type(exc).__name__}: {exc}")
                        return
                    if not res.get("ok"):
                        self.bridge.finish(error=res.get("error") or "自动读档失败")
                threading.Thread(target=restore, daemon=True,
                                 name=f"auto-restore-{gen}").start()
                return
            self.bridge.finish()

        self.thread = threading.Thread(target=worker, daemon=True,
                                       name=f"web-session-{gen}")
        self.thread.start()

    def _pace(self) -> None:
        """观战/托管：每轮结束歇一下，让人看得见过程（不影响规则与存档）。"""
        if self.pace > 0:
            time.sleep(self.pace)

    def _log_if(self, gen: int, line: str) -> None:
        if gen == self.gen:
            self.bridge.log(line)

    def _think_if(self, gen: int, actor: str, task: str, phase: str) -> None:
        if gen != self.gen:
            return
        if str(actor or "") == (self.human_name or HUMAN):
            return
        self.bridge.think(actor, task, phase)

    def _pace_if(self, gen: int) -> None:
        if gen == self.gen:
            self._pace()

    def _checkpoint_path(self) -> str:
        return os.path.join(self.save_dir, "checkpoint.json")

    def _checkpoint_scene(self, scene, *, state=None, loop=None,
                          gen: int | None = None) -> None:
        """每场结束写一个可回滚检查点；不占用户 1~3 号槽。

        旧代次的 worker 可能在新局开始后才走到回调：必须用**创建时的**
        state/loop，且代次不符就直接丢弃，不能拿新局字段写旧场检查点。
        """
        if gen is not None and gen != self.gen:
            return
        st = state if state is not None else self.state
        lp = loop if loop is not None else self.loop
        with self._lock:
            if st is None or lp is None:
                return
            save_game(self._checkpoint_path(), state=st,
                      ledger=lp.ledger, loop=lp,
                      extra={"session_zero": self.session_zero_record(),
                             "table": {"ai": self.ai_count, "max": 5},
                             "checkpoint": {"scene": scene.id,
                                            "title": scene.title}},
                      note=f"检查点 @ {scene.id} {scene.title}")

    def _stop_old(self) -> None:
        # 先把代次推到新值：旧 loop 的 on_line/on_call/ask 立刻失效，
        # 不再靠 join(3s) 去等一个可能跑 30 秒的真模型调用。
        self.gen += 1
        self.bridge.set_generation(self.gen)
        self.bridge.abort()
        t = self.thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def _enter_lobby(self, state: StateTree, loop: GameLoop, mode: str) -> None:
        self.state, self.loop = state, loop
        self.phase = "lobby"
        self._session_zero = None
        self.lobby = {
            "active": True,
            "pitch": campaign_pitch(self.adv),
            "characters": character_cards(state),
            "audit": audit_card(state, self.human_name or HUMAN),
            "table": {"ai": self.ai_count,
                      "total": len(state.characters()),
                      "max": 5},
            "companion_source": self._companion_source,
            "companion_errors": dict(self._companion_errors),
            "presets": self.preset_cards(),
            "mode": mode,
            "qa": [],
            "qa_limit": 3,
            "stage": {
                "id": (os.path.relpath(self.adv_path,
                                        os.path.join(ROOT, "adventures")).replace(os.sep, "/")
                       if self.adv_path else ""),
                "title": self.adv.title,
                "options": [{"id": x["id"], "title": x["title"]}
                            for x in self.stages],
                "note": ("可切换已校验剧本；换剧本会重开大厅。" if len(self.stages) > 1
                         else "当前只有这一张剧本；多剧本选择待接。"),
            },
            "notes": {"motivation": "", "bonds": ""},
            "guided_step": "motivation",
            "guided_prompt": GUIDED_PROMPTS["motivation"],
        }
        n_cards = len(state.characters())
        self.bridge.log(f"【DM】先确认 {n_cards} 张角色卡；确认后我们从第一场开始。")

    def generate_companions(self) -> dict:
        """C 方案：每个 AI 座位一次短请求，按世界简介生成自己的卡；失败退预置。"""
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            names = [n for n in (self.state.characters() if self.state else [])
                     if n != (self.human_name or HUMAN)]
            if not names:
                return {"ok": False, "error": "本桌没有 AI 队友"}
            if self._companion_chars and self._companion_source == "ai":
                return {"ok": True, "message": "AI 已经建过卡了",
                        "value": [{"name": n, "klass": self._companion_chars[n].get("职业")}
                                  for n in names if n in self._companion_chars]}
            opts = list_options()
            classes = [f'{c["en"]}（{c["cn"]}）' for c in opts["classes"]]
            species = [f'{x["en"]}（{x["cn"]}）' for x in opts["species"]]
            skills = [x["en"] for x in opts["skills"]]
            pitch = str(self.lobby.get("pitch") or campaign_pitch(self.adv))
            players = getattr(self.loop.client, "players", {}) if self.loop else {}
            made, fallback = [], []
            errors: dict[str, str] = {}
            for name in names:
                seat = players.get(name)
                reason = ""
                if seat is None or not getattr(seat, "real", False):
                    reason = "假模型或没有座位"
                    text = ""
                else:
                    text = ""
                if seat is not None and getattr(seat, "real", False):
                    prompt = (
                        f"你在《{self.adv.title}》中扮演玩家角色「{name}」。\n"
                        f"世界简介：\n{pitch}\n"
                        "你只需要决定自己的战斗身份，不要替 DM 叙事。\n"
                        f"职业只能从这些里选：{', '.join(classes)}。\n"
                        f"种族只能从这些里选：{', '.join(species)}。\n"
                        f"技能只能从这些里选：{', '.join(skills)}。\n"
                        "技能选 2 项（吟游诗人 3 项、游荡者 4 项、游侠 3 项）。\n"
                        "只输出一个 JSON 对象，不要 Markdown、不要解释：\n"
                        '{"klass":"Fighter","species":"Human",'
                        '"skills":["athletics","perception"]}')
                    req = ModelRequest(role="player", actor=name,
                                       task="lobby_companion", prompt=prompt,
                                       expect="free",
                                       allowed={"classes": classes, "species": species,
                                                "skills": skills})
                    try:
                        rep = seat.complete(req)
                        obj = None
                        if not rep.ok:
                            reason = f"调用失败：{rep.error or '空回复'}"
                        else:
                            obj = rep.data if isinstance(rep.data, dict) else None
                            if obj is None:
                                raw = (rep.text or "").strip()
                                if raw.startswith("```"):
                                    raw = raw.split("```")[1].lstrip("json").strip()
                                i, j = raw.find("{"), raw.rfind("}")
                                if i >= 0 and j > i:
                                    try:
                                        obj = json.loads(raw[i:j + 1])
                                    except ValueError as exc:
                                        reason = f"JSON 解析失败：{exc}"
                                else:
                                    reason = "没有 JSON 对象"
                        if isinstance(obj, dict):
                            payload = {
                                "name": name,
                                "klass": str(obj.get("klass") or "Fighter"),
                                "species": str(obj.get("species") or "Human"),
                                "level": 3, "ability_mode": "suggest", "seed": 7,
                                "skills": [str(x) for x in (obj.get("skills") or [])],
                                "spells": [],
                            }
                            try:
                                char, _choices = self._build_char(payload)
                            except Exception as exc:  # noqa: BLE001
                                reason = f"建卡校验失败：{exc}"
                            else:
                                self._companion_chars[name] = char
                                made.append({"name": name,
                                             "klass": str(char.get("职业") or "")})
                                continue
                        elif not reason:
                            reason = "模型回复不是 JSON 对象"
                    except Exception as exc:         # noqa: BLE001
                        reason = f"调用异常：{exc}"
                # 假模型 / 调用失败 / 校验失败：保留预生成卡
                self._companion_chars.pop(name, None)
                fallback.append(name)
                errors[name] = reason or "未知原因"
            if made:
                self._companion_source = "ai"
            state, loop = build_web_table(
                self.bridge, narrations=self._narr,
                human_char=self._human_char,
                human_name=self.human_name or HUMAN, mode=self.mode,
                title=self.adv.title, companions=names,
                companion_chars=self._companion_chars)
            self.bridge.reset(clear_log=True)
            self._companion_errors = errors
            self._enter_lobby(state, loop, self.mode)
            self.lobby["companion_errors"] = dict(errors)
            if made:
                self.bridge.log("【DM】AI 队友自己写好了角色卡：" +
                                "、".join(x["name"] for x in made))
            if fallback:
                self.bridge.log("【DM】这些座位先用了预生成卡：" + "、".join(fallback))
                for nm in fallback:
                    self.bridge.log(f"【DM】{nm}：{errors.get(nm, '未知原因')}")
        return {"ok": True, "value": made, "fallback": fallback,
                "errors": errors, "message": "AI 建卡完成"}

    def dm_companions(self, brief) -> dict:
        """B 方案：玩家说想要怎样的同伴，DM 一次提案，服务器校验后上桌。"""
        text = str(brief or "").strip()[:300]
        if not text:
            return {"ok": False, "error": "先写一句你想要怎样的同伴"}
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            n = self.ai_count
            if n <= 0:
                return {"ok": False, "error": "本桌没有 AI 队友"}
            dm = getattr(self.loop.client, "dm", None) if self.loop else None
            if dm is None or not getattr(dm, "real", False):
                return {"ok": False, "error": "DM 现在是假模型，不能提案同伴"}
            opts = list_options()
            classes = [f'{c["en"]}（{c["cn"]}）' for c in opts["classes"]]
            species = [f'{x["en"]}（{x["cn"]}）' for x in opts["species"]]
            skills = [x["en"] for x in opts["skills"]]
            pitch = str(self.lobby.get("pitch") or campaign_pitch(self.adv))
            prompt = (
                f"你在主持《{self.adv.title}》，现在是开局大厅。\n"
                f"世界简介：\n{pitch}\n"
                f"玩家想要这样的同伴：{text}\n"
                f"请提出 {n} 位同伴，名字不要和真人「{self.human_name or HUMAN}」重复。\n"
                f"职业只能从：{', '.join(classes)}；种族只能从：{', '.join(species)}；"
                f"技能只能从：{', '.join(skills)}。\n"
                "技能数：吟游诗人 3、游荡者 4、游侠 3，其余 2。\n"
                "只输出 JSON 数组，不要 Markdown、不要解释：\n"
                '[{"name":"灰羽","klass":"Ranger","species":"Elf",'
                '"skills":["perception","survival","nature"]}]')
            req = ModelRequest(role="dm", actor="dm", task="lobby_companion_dm",
                               prompt=prompt, expect="free",
                               allowed={"classes": classes, "species": species,
                                        "skills": skills})
            try:
                rep = dm.complete(req)
                raw = (rep.text or "").strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1].lstrip("json").strip()
                i, j = raw.find("["), raw.rfind("]")
                arr = json.loads(raw[i:j + 1]) if i >= 0 and j > i else (rep.data or [])
                if isinstance(arr, dict):
                    arr = arr.get("companions") or []
            except Exception as exc:             # noqa: BLE001
                return {"ok": False, "error": f"DM 提案解析失败：{exc}"}
            if not isinstance(arr, list) or len(arr) < n:
                return {"ok": False, "error": f"DM 没有给出 {n} 位合法同伴"}
            chars: dict[str, dict] = {}
            for k, item in enumerate(arr[:n]):
                if not isinstance(item, dict):
                    return {"ok": False, "error": f"第 {k + 1} 位同伴不是对象"}
                name = str(item.get("name") or "").strip()[:20]
                if not name or name == (self.human_name or HUMAN) or name in chars:
                    return {"ok": False, "error": f"第 {k + 1} 位同伴名字不合法或重复"}
                payload = {
                    "name": name, "klass": str(item.get("klass") or ""),
                    "species": str(item.get("species") or ""),
                    "level": 3, "ability_mode": "suggest", "seed": 7,
                    "skills": [str(x) for x in (item.get("skills") or [])],
                    "spells": [],
                }
                try:
                    char, _choices = self._build_char(payload)
                except Exception as exc:         # noqa: BLE001
                    return {"ok": False,
                            "error": f"第 {k + 1} 位「{name}」建卡失败：{exc}"}
                chars[name] = char
            self._companion_chars = chars
            self._companion_source = "dm"
            self._companion_errors = {}
            state, loop = build_web_table(
                self.bridge, narrations=self._narr,
                human_char=self._human_char,
                human_name=self.human_name or HUMAN, mode=self.mode,
                title=self.adv.title, companions=list(chars),
                companion_chars=chars)
            self.bridge.reset(clear_log=True)
            self._enter_lobby(state, loop, self.mode)
            self.bridge.log("【DM】同伴提案已上桌：" +
                            "、".join(f"{x}（{chars[x].get('职业')}）" for x in chars))
        return {"ok": True, "value": [{"name": x, "klass": chars[x].get("职业")}
                                      for x in chars],
                "message": "DM 同伴已上桌"}

    def use_human_card(self, cid) -> dict:
        """从卡库选一张真人卡上桌；只换真人，不动已有 AI 同伴。"""
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            if self.mode == "spectate":
                return {"ok": False, "error": "观战模式没有真人座位"}
            try:
                path = self._lib_path(cid)
            except Exception as exc:             # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            if not os.path.exists(path):
                return {"ok": False, "error": "卡库里没有这张卡"}
            try:
                raw = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"读卡失败：{exc}"}
            if str(raw.get("role") or "human") != "human":
                return {"ok": False, "error": f"「{raw.get('name')}」不是真人卡"}
            try:
                char, choices = self._build_char(raw.get("choices") or {})
            except Exception as exc:             # noqa: BLE001
                return {"ok": False, "error": f"「{raw.get('name')}」建卡失败：{exc}"}
            name = choices["name"] or "无名冒险者"
            self._human_char = char
            self.human_name = name
            state, loop = build_web_table(
                self.bridge, narrations=self._narr,
                human_char=char, human_name=name, mode="normal",
                title=self.adv.title, companions=self._companion_names(),
                companion_chars=self._companion_chars)
            self.mode = "normal"
            self.bridge.reset(clear_log=True)
            self._enter_lobby(state, loop, "normal")
            self.bridge.log(f"【DM】真人卡已换成「{name}」。")
        return {"ok": True, "value": self._preview_of(char, choices),
                "message": f"已换成 {name}"}

    def use_companion_cards(self, ids) -> dict:
        """A 方案：玩家从卡库挑已经建好的同伴卡，按实际 AI 人数上桌。"""
        if not isinstance(ids, list):
            return {"ok": False, "error": "同伴卡列表格式不对"}
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            n = self.ai_count
            if n <= 0:
                return {"ok": False, "error": "本桌没有 AI 队友"}
            ids = [str(x) for x in ids]
            if len(ids) != n:
                return {"ok": False, "error": f"本桌要选 {n} 张同伴卡，拿到 {len(ids)} 张"}
            chars: dict[str, dict] = {}
            for cid in ids:
                try:
                    path = self._lib_path(cid)
                except Exception as exc:         # noqa: BLE001
                    return {"ok": False, "error": str(exc)}
                if not os.path.exists(path):
                    return {"ok": False, "error": f"卡库里没有 {cid}"}
                try:
                    raw = json.load(open(path, encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    return {"ok": False, "error": f"读卡失败：{exc}"}
                if str(raw.get("role") or "human") != "companion":
                    return {"ok": False, "error": f"「{raw.get('name')}」不是同伴卡"}
                name = str(raw.get("name") or "").strip()[:20]
                if not name or name == (self.human_name or HUMAN) or name in chars:
                    return {"ok": False, "error": f"同伴名字不合法或重复：{name!r}"}
                try:
                    char, _choices = self._build_char(raw.get("choices") or {})
                except Exception as exc:         # noqa: BLE001
                    return {"ok": False, "error": f"「{name}」建卡失败：{exc}"}
                chars[name] = char
            self._companion_chars = chars
            self._companion_source = "manual"
            self._companion_errors = {}
            state, loop = build_web_table(
                self.bridge, narrations=self._narr,
                human_char=self._human_char,
                human_name=self.human_name or HUMAN, mode=self.mode,
                title=self.adv.title, companions=list(chars),
                companion_chars=chars)
            self.bridge.reset(clear_log=True)
            self._enter_lobby(state, loop, self.mode)
            self.bridge.log("【DM】手搓同伴卡已上桌：" + "、".join(chars))
        return {"ok": True, "value": [{"name": x, "klass": chars[x].get("职业")}
                                      for x in chars],
                "message": "手搓同伴已上桌"}

    def lobby_intros(self) -> dict:
        """让三个 AI 队友各说一句。真模型每个座位一次短调用；假模型用预置台词。"""
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            if self.lobby.get("intros"):
                return {"ok": True, "message": "队友已经介绍过了",
                        "value": self.lobby["intros"]}
            names = [n for n in (self.state.characters() if self.state else [])
                     if n != (self.human_name or HUMAN)]
            if not names:
                self.lobby["intros"] = []
                return {"ok": True, "message": "本桌没有 AI 队友", "value": []}
            players = getattr(self.loop.client, "players", {}) if self.loop else {}
            intros = []
            for name in names:
                seat = players.get(name)
                job = ""
                if self.state is not None:
                    jobs = (self.state.read(f"/角色列表/{name}") or {}).get("职业") or {}
                    job = "、".join(f"{k} {v}" for k, v in jobs.items())
                text = ""
                if seat is not None and getattr(seat, "real", False):
                    prompt = (f"你是《{self.adv.title}》里的{name}（{job or '冒险者'}）。"
                              "用一句 20~40 字的中文，向同桌介绍自己：名字、职业/外观、"
                              "此刻的一个小动作。只输出这一句，不要 JSON、不要规则解释。")
                    req = ModelRequest(role="player", actor=name, task="lobby_intro",
                                       prompt=prompt, expect="narration")
                    try:
                        rep = seat.complete(req)
                        if rep.ok and (rep.text or "").strip():
                            text = rep.text.strip().splitlines()[0][:120]
                    except Exception:                    # noqa: BLE001
                        text = ""
                if not text:
                    text = INTRO_FALLBACK.get(name, f"{name}点了点头，没有说话。")
                intros.append({"name": name, "text": text})
                self.bridge.log(f"【{name}】{text}")
            self.lobby["intros"] = intros
        return {"ok": True, "message": "队友介绍完毕", "value": intros}

    def lobby_answer(self, answer, *, skip: bool = False) -> dict:
        """序章引导问答：动机 → 羁绊，一次只问一个；答案只进 notes，不改数值。"""
        text = str(answer or "").strip()[:300]
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            step = str(self.lobby.get("guided_step") or "")
            if step not in GUIDED_PROMPTS:
                return {"ok": False, "error": "引导问答已经完成了"}
            if not text and not skip:
                return {"ok": False, "error": "先写一句回答，或点跳过"}
            notes = self.lobby.setdefault("notes", {})
            notes[step] = "" if skip else text
            nxt = "bonds" if step == "motivation" else "done"
            self.lobby["guided_step"] = nxt
            if nxt == "done":
                self.lobby["guided_prompt"] = "灵魂问答完成；点「四人确认上桌」开始。"
            else:
                self.lobby["guided_prompt"] = GUIDED_PROMPTS[nxt]
            if text:
                self.bridge.log(f"【玩家】{text}")
                self.bridge.log("【DM】记下了。")
        return {"ok": True, "message": "记下了", "value": nxt}

    def lobby_ask(self, question) -> dict:
        """开局大厅问 DM：只回答战役设定，不推进场景、不掷骰。

        最多 3 问，避免在大厅先把真模型预算烧光；假模型给确定性占位回答。
        """
        q = str(question or "").strip()[:200]
        if not q:
            return {"ok": False, "error": "问题不能为空"}
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            qa = self.lobby.setdefault("qa", [])
            limit = int(self.lobby.get("qa_limit") or 3)
            if len(qa) >= limit:
                return {"ok": False, "error": f"大厅最多问 {limit} 个问题"}
            dm = getattr(self.loop.client, "dm", None)
            if dm is None:
                return {"ok": False, "error": "大厅没有 DM 座位"}
            if not getattr(dm, "real", False):
                answer = ("（假模型）这里只验证开局流程；真桌会由 DM 按剧本回答。"
                          f"你问的是：{q}")
            else:
                prompt = (
                    "现在是跑团开局大厅，还没进入第一场。\n"
                    f"战役资料：\n{self.lobby.get('pitch') or ''}\n"
                    f"玩家问：{q}\n"
                    "只回答与这个战役设定/角色/开局有关的问题；80 字以内；"
                    "不推进剧情、不掷骰、不替玩家做决定；不知道就说不知道。")
                req = ModelRequest(role="dm", actor="dm", task="lobby_qa",
                                   prompt=prompt, expect="narration")
                try:
                    rep = dm.complete(req)
                except Exception as exc:             # noqa: BLE001
                    return {"ok": False, "error": f"DM 回答失败：{exc}"}
                if not rep.ok or not (rep.text or "").strip():
                    return {"ok": False, "error": f"DM 没有回答：{rep.error or '空回复'}"}
                answer = rep.text.strip()
            qa.append({"q": q, "a": answer})
            self.bridge.log(f"【玩家】{q}")
            self.bridge.log(f"【DM】{answer}")
        return {"ok": True, "message": "DM 答了", "value": answer}

    def set_table_size(self, count) -> dict:
        """定桌规模：真人固定 1 人，AI 0~3 人，总玩家 1~4。"""
        try:
            n = int(count)
        except (TypeError, ValueError):
            return {"ok": False, "error": "人数必须是 0~3 的整数"}
        if not (0 <= n <= 4):
            return {"ok": False, "error": "AI 队友只能是 0~4 人（总玩家上限 5）"}
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            self._stop_old()
            self.ai_count = n
            self._companion_chars = {}
            self._companion_source = "pregen"
            self._companion_errors = {}
            state, loop = build_web_table(
                self.bridge, narrations=self._narr,
                human_char=self._human_char,
                human_name=self.human_name or HUMAN, mode=self.mode,
                title=self.adv.title, companions=self._companion_names(),
                companion_chars=self._companion_chars)
            self.bridge.reset(clear_log=True)
            self._enter_lobby(state, loop, self.mode)
        return {"ok": True, "message": f"本桌 {n + 1} 人（真人 1 + AI {n}）"}

    def select_stage(self, stage_id) -> dict:
        """大厅换剧本：只允许已校验的 adventures/*.json；换完回大厅，不开局。"""
        sid = str(stage_id or "").strip()
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            opt = next((x for x in self.stages if x["id"] == sid), None)
            if opt is None:
                return {"ok": False, "error": f"没有这个剧本：{sid!r}"}
            try:
                adv = load_adventure(opt["path"])
            except Exception as exc:                 # noqa: BLE001
                return {"ok": False, "error": f"剧本读不了：{exc}"}
            self._stop_old()
            self.adv = adv
            self.adv_path = opt["path"]
            self._narr = [s.narration for s in adv.scenes if s.narration]
            state, loop = build_web_table(
                self.bridge, narrations=self._narr,
                human_char=self._human_char,
                human_name=self.human_name or HUMAN, mode=self.mode,
                title=self.adv.title, companions=self._companion_names(),
                companion_chars=self._companion_chars)
            self.bridge.reset(clear_log=True)
            self._enter_lobby(state, loop, self.mode)
        return {"ok": True, "message": f"已切到《{self.adv.title}》"}

    def start_game(self) -> dict:
        """大厅确认：四人上桌，启动 AdventureRunner。"""
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            # 先清掉大厅标记，再交给 _spawn；旧线程在上一次 _stop_old 已作废。
            state, loop = self.state, self.loop
            if state is None or loop is None:
                return {"ok": False, "error": "大厅里没有可开的桌"}
            self.bridge.log("【DM】四人已确认。我们开始。")
            _notes = dict(self.lobby.get("notes") or {})
            _intros = list(self.lobby.get("intros") or [])
            self._session_zero = {
                "stage": (self.lobby.get("stage") or {}).get("title") or self.adv.title,
                "audit": self.lobby.get("audit") or "",
                "notes": _notes,
                "intros": [dict(x) for x in _intros],
                "table": {"ai": self.ai_count, "max": 5},
                "companion_source": self._companion_source,
                "companion_errors": dict(self._companion_errors),
                "confirmed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            _size = len(state.characters())
            self.bridge.log("【DM】" + lobby_opening(self.adv, _notes, _intros, _size))
            self._spawn(state, loop)
        return {"ok": True, "message": "四人已确认，开始第一场"}

    def new_game(self, mode: str = "normal") -> dict:
        if mode not in ("normal", "hosted", "spectate"):
            return {"ok": False, "error": f"不认识的模式：{mode!r}"}
        with self._lock:
            self._stop_old()
            self._auto_restores = 0
            self._human_char = None
            state, loop = build_web_table(self.bridge, narrations=self._narr,
                                          mode=mode, title=self.adv.title,
                                          companions=self._companion_names(),
                                          companion_chars=self._companion_chars)
            self.human_name = HUMAN
            self.mode = mode
            self.bridge.reset(clear_log=True)
            if self.session_zero:
                self._enter_lobby(state, loop, mode)
                return {"ok": True, "message": "战役介绍已就绪；确认四人后开始"}
            self._spawn(state, loop)
            label = {"normal": "新的一局开始了",
                     "hosted": "新的一局开始了（托管：AI 替你行动）",
                     "spectate": "新的一局开始了（观战：全 AI）"}[mode]
            return {"ok": True, "message": label}

    # ---------- 建卡向导（U14 网页） ----------

    def _lib_path(self, cid: str) -> str:
        sid = str(cid or "")
        if not sid or any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz-"
                          for ch in sid):
            raise CreationError(f"角色卡 id 不合法：{cid!r}")
        return os.path.join(self.characters_dir, f"{sid}.json")

    def list_characters(self, role=None) -> list[dict]:
        """卡库列表：只读头部元数据，不整份读卡。"""
        rows = []
        for fn in sorted(os.listdir(self.characters_dir)):
            if not fn.endswith(".json"):
                continue
            try:
                raw = json.load(open(os.path.join(self.characters_dir, fn),
                                     encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                if role and str(raw.get("role") or "human") != str(role):
                    continue
                ch = raw.get("choices") or {}
                rows.append({
                    "id": fn[:-5], "name": raw.get("name") or ch.get("name") or "无名",
                    "role": str(raw.get("role") or "human"),
                    "klass": ch.get("klass") or "", "species": ch.get("species") or "",
                    "level": ch.get("level") or 1,
                    "updated_at": raw.get("updated_at") or "",
                })
            except (OSError, ValueError):
                continue
        return rows

    def save_character(self, payload: dict) -> dict:
        """把当前建卡选择存进卡库；派生数值仍由服务器重算。"""
        char, choices = self._build_char(payload)
        if not choices["name"]:
            return {"ok": False, "error": "先给角色起个名字"}
        cid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + \
            str(int(time.time_ns()) % 1000000)
        role = str(payload.get("role") or "human")
        if role not in ("human", "companion"):
            return {"ok": False, "error": "role 只能是 human/companion"}
        doc = {
            "format": "dnd-character-v1", "id": cid,
            "name": choices["name"], "role": role, "choices": choices,
            "preview": self._preview_of(char, choices),
            "lifepath": payload.get("lifepath") or "",
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        path = self._lib_path(cid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return {"ok": True, "value": {"id": cid, "name": choices["name"]},
                "message": f"已存入卡库：{choices['name']}"}

    def load_character(self, cid) -> dict:
        path = self._lib_path(cid)
        if not os.path.exists(path):
            return {"ok": False, "error": "卡库里没有这张卡"}
        try:
            raw = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"读卡失败：{exc}"}
        return {"ok": True, "value": raw,
                "message": f"读入：{raw.get('name') or '无名'}"}

    def delete_character(self, cid) -> dict:
        path = self._lib_path(cid)
        if not os.path.exists(path):
            return {"ok": False, "error": "卡库里没有这张卡"}
        os.remove(path)
        return {"ok": True, "message": "已从卡库删除"}

    def create_options(self) -> dict:
        """向导要的选项：职业（含生命骰/豁免/技能数）/ 种族 / 技能 / 各职业法术表。"""
        opts = list_options()
        classes = []
        spells: dict[str, list[dict]] = {}
        for c in opts["classes"]:
            info = class_info(c["en"])
            classes.append({**c, "hit_die": info["hit_die"],
                            "saves": info["saves"],
                            "skill_choices": info["skill_choices"],
                            "caster": info["caster"]})
            spells[c["en"]] = [
                {"name": s["name"], "level": s["level"],
                 "school": s["school"]}
                for s in spells_for(c["en"], max_level=5)]
        return {"classes": classes, "species": opts["species"],
                "skills": opts["skills"], "spells": spells,
                "lifepath": lifepath_questions()}

    @staticmethod
    def _choices(payload: dict) -> dict:
        """只取玩家的**选择**；HP/AC/环位这类派生值一律不信客户端。"""
        try:
            level = int(payload.get("level") or 1)
            seed = int(payload.get("seed") or 1)
        except (TypeError, ValueError) as exc:
            raise CreationError("等级/种子必须是整数") from exc
        klass = str(payload.get("klass") or "Wizard")
        mode = str(payload.get("ability_mode") or "suggest")
        if mode == "roll":
            abilities = roll_abilities(Random(seed))
        elif mode == "manual":
            abilities = payload.get("abilities") or {}
        else:
            abilities = suggest_abilities(klass)
        return {
            "name": str(payload.get("name") or "").strip(),
            "klass": klass,
            "species": str(payload.get("species") or "Human"),
            "level": level,
            "abilities": abilities,
            "skills": [str(x) for x in (payload.get("skills") or [])],
            "spells": [str(x) for x in (payload.get("spells") or [])],
        }

    @staticmethod
    def _preview_of(char: dict, choices: dict) -> dict:
        hp = char.get("生命值") or {}
        prof = char.get("熟练配置") or {}
        cast = char.get("施法") or {}
        return {
            "name": choices["name"] or "无名冒险者",
            "klass": choices["klass"], "species": choices["species"],
            "level": choices["level"],
            "hp": int(hp.get("最大") or 0),
            "ac": int(char.get("护甲等级") or 10),
            "saves": list(prof.get("豁免") or []),
            "skills": dict(prof.get("技能") or {}),
            "slots": {str(k): {"当前": int(v.get("当前") or 0),
                               "最大": int(v.get("最大") or 0)}
                      for k, v in (cast.get("法术位") or {}).items()},
            "spells": list(cast.get("法术书") or {}),
        }

    def preset_cards(self) -> list[dict]:
        """本模块 meta 里声明的预生成卡；只读卡库头部，不偷偷造卡。"""
        ids = []
        meta = getattr(self.adv, "meta", {}) or {}
        raw = meta.get("预生成卡") or []
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            for x in raw:
                cid = str((x.get("id") if isinstance(x, dict) else x) or "").strip()
                if cid and cid not in ids:
                    ids.append(cid)
        rows = []
        for cid in ids:
            try:
                path = self._lib_path(cid)
                if not os.path.exists(path):
                    continue
                card = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ch = card.get("choices") or {}
            klass = str(ch.get("klass") or "")
            species = str(ch.get("species") or "")
            hp = hp_max = 0
            try:
                built, _choices = self._build_char(ch)
                hp = int((built.get("生命值") or {}).get("当前") or 0)
                hp_max = int((built.get("生命值") or {}).get("最大") or 0)
            except (CreationError, ValueError, TypeError):
                pass
            rows.append({"id": cid, "name": str(card.get("name") or cid),
                         "role": str(card.get("role") or ""),
                         "klass": klass, "klass_cn": CLASS_CN.get(klass, klass),
                         "species": species,
                         "species_cn": SPECIES_CN.get(species, species),
                         "level": int(ch.get("level") or 1),
                         "hp": hp, "hp_max": hp_max})
        return rows

    def use_preset_cards(self, ids) -> dict:
        """按本模块预生成卡开一桌：第一张给真人位，其余给 AI。"""
        if not isinstance(ids, list) or not ids:
            return {"ok": False, "error": "先选预设卡"}
        with self._lock:
            if self.phase != "lobby" or self.lobby is None:
                return {"ok": False, "error": "现在不在开局大厅"}
            ids = [str(x) for x in ids]
            total = self.ai_count + 1
            if len(ids) != total:
                return {"ok": False, "error": f"本桌要选 {total} 张卡，拿到 {len(ids)} 张"}
            allowed = {x["id"] for x in self.preset_cards()}
            bad = [x for x in ids if x not in allowed]
            if bad:
                return {"ok": False, "error": f"不是本模块预设卡：{bad}"}
            chars: dict[str, dict] = {}
            for cid in ids:
                try:
                    raw = json.load(open(self._lib_path(cid), encoding="utf-8"))
                    char, _choices = self._build_char(raw.get("choices") or {})
                except Exception as exc:             # noqa: BLE001
                    return {"ok": False, "error": f"读卡 {cid} 失败：{exc}"}
                name = str(char.get("name") or raw.get("name") or cid)
                if name in chars:
                    return {"ok": False, "error": f"角色名重复：{name}"}
                chars[name] = char
            names = list(chars)
            human, mates = names[0], names[1:]
            state, loop = build_web_table(
                self.bridge, narrations=self._narr,
                human_char=chars[human], human_name=human,
                mode=self.mode, title=self.adv.title,
                companions=mates, companion_chars={n: chars[n] for n in mates})
            self.human_name = human
            self._human_char = chars[human]
            self._companion_chars = {n: chars[n] for n in mates}
            self._companion_source = "preset"
            self._companion_errors = {}
            self.bridge.reset(clear_log=True)
            self._enter_lobby(state, loop, self.mode)
            self.bridge.log("【DM】模块预设卡上桌：" + "、".join(names))
        return {"ok": True, "value": [{"name": n, "klass": chars[n].get("职业")}
                                      for n in names],
                "message": "预设卡已上桌"}

    def _build_char(self, payload: dict) -> tuple[dict, dict]:
        choices = self._choices(payload)
        try:
            char = create_character(**choices)
        except (CreationError, ValueError, TypeError) as exc:
            raise CreationError(str(exc) or type(exc).__name__) from exc
        return char, choices

    def lifepath_generate(self, payload: dict) -> dict:
        """8 问 → 建卡 payload；不建卡，只回填表单。"""
        try:
            built = lifepath_build(payload.get("answers") or {},
                                   name=str(payload.get("name") or ""))
        except LifepathError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "value": built}

    def create_preview(self, payload: dict) -> dict:
        """只算一遍给玩家看，**不改任何状态、不开局**。"""
        char, choices = self._build_char(payload)
        return {"ok": True, "value": self._preview_of(char, choices),
                "message": f"{choices['name'] or '无名冒险者'}："
                           f"{choices['klass']} {choices['level']} 级"}

    def create_start(self, payload: dict) -> dict:
        char, choices = self._build_char(payload)
        name = choices["name"] or "无名冒险者"
        with self._lock:
            self._stop_old()
            # 建卡是“真人坐进来”，明确按 normal 建桌：否则托管/观战里建卡，
            # 界面会隐藏输入框、桌子却在等一个永远不来的答案。
            self._human_char = char
            state, loop = build_web_table(self.bridge, narrations=self._narr,
                                          human_char=char, human_name=name,
                                          mode="normal", title=self.adv.title,
                                          companions=self._companion_names(),
                                          companion_chars=self._companion_chars)
            self.human_name = name
            self.mode = "normal"
            self.bridge.reset(clear_log=True)
            if self.session_zero:
                self._enter_lobby(state, loop, "normal")
            else:
                self._spawn(state, loop)
        return {"ok": True, "value": self._preview_of(char, choices),
                "message": f"{name} 上桌了（{choices['klass']} "
                           f"{choices['level']} 级）"}

    def session_zero_record(self) -> dict:
        """这桌怎么开起来的：剧本、审卡、两问、队友介绍。只做记录，不改数值。"""
        if self._session_zero:
            return dict(self._session_zero)
        if self.phase != "lobby" or not self.lobby:
            return {}
        return {
            "stage": (self.lobby.get("stage") or {}).get("title") or self.adv.title,
            "audit": self.lobby.get("audit") or "",
            "notes": dict(self.lobby.get("notes") or {}),
            "intros": [dict(x) for x in (self.lobby.get("intros") or [])],
            "table": {"ai": self.ai_count, "max": 5},
            "companion_source": self._companion_source,
            "companion_errors": dict(self._companion_errors),
            "confirmed_at": "",
        }

    def save(self, slot: int | None = None) -> dict:
        with self._lock:
            if self.state is None or self.loop is None:
                return {"ok": False, "error": "现在没有可存档的一局"}
            if slot is None:
                path = self._latest()
                label = "最近存档"
            else:
                n = self._slot_no(slot)
                path = self._slot_path(n)
                label = f"槽 {n}"
            save_game(path, state=self.state, ledger=self.loop.ledger,
                      loop=self.loop, extra={
                          "session_zero": self.session_zero_record(),
                          "table": {"ai": self.ai_count, "max": 5}},
                      note=f"网页座位 @ {datetime.datetime.now():%F %T}")
            return {"ok": True, "message": f"已存档到{label}（{os.path.basename(path)}）"}

    SLOT_MAX = 3

    def _slot_no(self, slot) -> int:
        try:
            n = int(slot)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"槽位号必须是 1~{self.SLOT_MAX} 的整数") from exc
        if not (1 <= n <= self.SLOT_MAX):
            raise ValueError(f"槽位号必须是 1~{self.SLOT_MAX}，拿到 {n}")
        return n

    def _slot_path(self, n: int) -> str:
        return os.path.join(self.save_dir, f"slot-{n}.json")

    def list_slots(self) -> list[dict]:
        """三个槽位的状态：有没有、什么时候存的。**不读整份存档**（只看头部字段）。"""
        rows = []
        for n in range(1, self.SLOT_MAX + 1):
            row = {"slot": n, "exists": False, "saved_at": "", "note": ""}
            path = self._slot_path(n)
            if os.path.exists(path):
                try:
                    raw = json.load(open(path, encoding="utf-8"))
                    row.update(exists=True, saved_at=str(raw.get("saved_at") or ""),
                               note=str(raw.get("note") or ""))
                except (OSError, ValueError):
                    row.update(exists=True, note="（存档头读不出来）")
            rows.append(row)
        return rows

    def load_slot(self, slot: int) -> dict:
        try:
            n = self._slot_no(slot)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        path = self._slot_path(n)
        if not os.path.exists(path):
            return {"ok": False, "error": f"槽 {n} 还没有存档"}
        self._auto_restores = 0
        return self._load_path(path, f"已读槽 {n}，从当前场景开头继续")

    def load_latest(self) -> dict:
        path = self._latest()
        if not os.path.exists(path):
            return {"ok": False, "error": "还没有存档（先点一次「存档」）"}
        self._auto_restores = 0
        return self._load_path(path, "已读最近存档，从当前场景开头继续")

    def _load_path(self, path: str, message: str) -> dict:
        with self._lock:
            try:
                raw = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"读档失败：{exc}"}
        return self._adopt(raw, message, what="读档")

    def _adopt(self, raw: dict, message: str, *, what: str = "读档") -> dict:
        """把一份存档接到当前牌桌上（读档/导入共用）。"""
        with self._lock:
            self._stop_old()
            human = _human_from_save(raw)
            _chars = (((raw.get("state") or {}).get("stat_data") or {})
                      .get("角色列表") or {})
            _mates = [n for n in _chars if n != human]
            self.ai_count = len(_mates)
            table = make_web_table(self.bridge, narrations=self._narr,
                                   human_name=human, mode=self.mode,
                                   companions=_mates)
            try:
                sess = load_from_dict(raw, client=table)
            except Exception as exc:                 # noqa: BLE001
                return {"ok": False, "error": f"{what}失败：{exc}"}
            self.human_name = human
            self._session_zero = ((sess.meta.get("extra") or {})
                                  .get("session_zero") or None)
            self.bridge.reset(clear_log=True)
            self.bridge.log(f"▸ {message}")
            self._spawn(sess.state, sess.loop)
            return {"ok": True, "message": message}

    def export_data(self) -> dict:
        """导出成一份可下载的 JSON（不落盘，由浏览器保存）。"""
        with self._lock:
            if self.state is None or self.loop is None:
                return {"ok": False, "error": "现在没有可导出的局"}
            raw = dump_game(state=self.state, ledger=self.loop.ledger,
                            loop=self.loop, extra={
                                "session_zero": self.session_zero_record(),
                                "table": {"ai": self.ai_count, "max": 5}},
                            note=f"网页导出 @ {datetime.datetime.now():%F %T}")
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            return {"ok": True, "value": {
                "filename": f"dnd-save-{stamp}.json", "data": raw}}

    def import_data(self, raw) -> dict:
        """导入一份存档 dict（也接受导出的 `{filename, data}` 包）。"""
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            raw = raw["data"]
        if not isinstance(raw, dict):
            return {"ok": False, "error": "导入失败：不是一份存档 JSON"}
        return self._adopt(raw, "已导入存档，从当前场景开头继续", what="导入")

    def action(self, payload: dict) -> dict:
        kind = str(payload.get("kind") or "")
        if kind == "save_game":
            try:
                return self.save(payload.get("slot"))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        if kind == "load_game":
            slot = payload.get("slot")
            return self.load_latest() if slot in (None, "") else self.load_slot(slot)
        if kind == "export_save":
            return self.export_data()
        if kind == "import_save":
            return self.import_data(payload.get("data"))
        if kind == "new_game":
            return self.new_game(str(payload.get("mode") or "normal"))
        if kind == "start_game":
            return self.start_game()
        if kind == "select_stage":
            return self.select_stage(payload.get("stage"))
        if kind == "set_table_size":
            return self.set_table_size(payload.get("ai"))
        if kind == "lobby_ask":
            return self.lobby_ask(payload.get("question"))
        if kind == "lobby_answer":
            return self.lobby_answer(payload.get("answer"),
                                     skip=bool(payload.get("skip")))
        if kind == "lobby_intros":
            return self.lobby_intros()
        if kind == "generate_companions":
            return self.generate_companions()
        if kind == "dm_companions":
            return self.dm_companions(payload.get("brief"))
        if kind == "use_companion_cards":
            return self.use_companion_cards(payload.get("ids"))
        if kind == "use_preset_cards":
            return self.use_preset_cards(payload.get("ids"))
        if kind == "use_human_card":
            return self.use_human_card(payload.get("id"))
        if kind == "set_hosted":
            return self.set_hosted(bool(payload.get("on")))
        if kind == "create_options":
            return {"ok": True, "value": self.create_options()}
        if kind == "list_characters":
            return {"ok": True, "value": self.list_characters(payload.get("role"))}
        if kind == "save_character":
            try:
                return self.save_character(payload)
            except (CreationError, ValueError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
        if kind == "load_character":
            return self.load_character(payload.get("id"))
        if kind == "delete_character":
            return self.delete_character(payload.get("id"))
        if kind == "lifepath_build":
            return self.lifepath_generate(payload)
        if kind == "create_preview":
            return self.create_preview(payload)
        if kind == "create_start":
            return self.create_start(payload)
        if kind == "set_lore":
            loop = self.loop
            if loop is None:
                return {"ok": False, "error": "现在没有开局"}
            fields = payload.get("fields")
            if not isinstance(fields, dict):
                fields = {"描述": payload.get("text") or ""}
            try:
                changes = loop.world.set_lore_fields(
                    payload.get("lore_kind"), payload.get("name"), fields,
                    actor="DM", reason="DM 设定")
            except WorldError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "changes": changes,
                    "message": "已记下 DM 设定" if changes else "没有变化"}
        loop = self.loop
        if loop is None:
            return {"ok": False, "error": "现在没有开局"}
        return apply_action(loop.state, loop, self.human_name, payload)

    def set_hosted(self, on: bool) -> dict:
        """托管 ↔ 接管：只换真人座位的"谁在答"，不重建这一局。"""
        loop = self.loop
        table = getattr(loop, "client", None)
        seat = getattr(table, "human_seat", None)
        if seat is None:
            return {"ok": False, "error": "观战局没有真人座位，不能接管"}
        if on:
            seat._auto = getattr(table, "host_pilot", None) or _auto_option
            self.mode = "hosted"
            self.bridge.mode = "hosted"
            return {"ok": True, "message": "已托管：AI 会替你选，直到你点「接管」"}
        seat._auto = None
        self.mode = "normal"
        self.bridge.mode = "normal"
        return {"ok": True, "message": "已接管：下一个人物回合会等你输入"}

    def wait(self) -> None:
        while not self.bridge.done:
            time.sleep(0.2)


# --------------------------------------------------------------------------- #
# 无人值守：一个"假浏览器"，走的是同一套 HTTP
# --------------------------------------------------------------------------- #

#: 回环请求**绕开系统代理**：Windows 的代理不排除 127.0.0.1 时，
#: 本地网页请求会被代理吞掉（实测偶发 502）。这里固定用"无代理"的 opener。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with _LOCAL_OPENER.open(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick(opts: list[dict]) -> tuple[str, str] | None:
    """假浏览器策略：优先能直接做的「观察」，否则第一个不需要目标的；
    实在都要目标，就挑第一个有合法目标的。"""
    def usable(o: dict) -> bool:
        return not o.get("requires_target") or bool(o.get("targets"))

    usable_opts = [o for o in opts if usable(o)]
    if not usable_opts:
        return None
    opt = next((o for o in usable_opts if str(o.get("id")) == "look"),
               usable_opts[0])
    target = ""
    if opt.get("requires_target"):
        targets = opt.get("targets") or []
        target = str(targets[0]) if targets else ""
    return str(opt.get("id") or ""), target


def auto_driver(server: TableServer, bridge: WebBridge) -> None:
    base = server.url
    since = 0
    answered: set[int] = set()
    while not bridge.done:
        try:
            st = _http_json(f"{base}api/state?since={since}")
        except OSError:
            time.sleep(0.1)
            continue
        for item in st.get("log") or []:
            since = max(since, int(item["seq"]))
        lob = st.get("lobby")
        if lob and lob.get("active"):
            try:
                _http_json(f"{base}api/act", {"kind": "start_game"})
            except Exception:                    # noqa: BLE001
                time.sleep(0.2)
            continue
        p = st.get("pending")
        if not p or int(p["seq"]) in answered:
            time.sleep(0.05)
            continue
        opts = p.get("options") or []
        picked = _pick(opts)
        if picked is None:
            time.sleep(0.05)
            continue
        oid, target = picked
        try:
            r = _http_json(f"{base}api/answer",
                           {"seq": int(p["seq"]), "id": oid,
                            "target": target, "say": "随便看看"})
        except Exception:                        # noqa: BLE001
            time.sleep(0.05)
            continue
        if r.get("ok"):
            answered.add(int(p["seq"]))


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="真人网页座位（本地、零花费）")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--auto", action="store_true",
                    help="自带假浏览器跑完（无人值守；仍走 HTTP）")
    ap.add_argument("--adventure", default=DEFAULT_ADV)
    ap.add_argument("--save-dir", default=None,
                    help="存档目录（默认 saves/mock；--real 时默认 saves/real）")
    ap.add_argument("--characters-dir", default=None,
                    help="角色卡库目录（默认 characters/）")
    ap.add_argument("--ai-count", type=int, default=3, choices=(0, 1, 2, 3, 4),
                    help="开局默认 AI 队友数（0~3；总玩家上限 4）")
    ap.add_argument("--real", action="store_true",
                    help="非真人座位改用真模型（DM/规划/3 名同伴）；真人座位仍是网页")
    ap.add_argument("--session-zero", action="store_true",
                    help="先开战役介绍大厅，四人确认预生成角色后再进第一场")
    ap.add_argument("--model", default="deepseek-v4.1-flash")
    ap.add_argument("--base-url",
                    default="https://qianfan.baidubce.com/v2/tokenplan/personal")
    ap.add_argument("--max-calls", type=int, default=40,
                    help="--real 的硬闸：整局最多发几次请求（默认 40）")
    ap.add_argument("--seat-config", default=None,
                    help="每座位模型 JSON（dm/planner/pc:名字 各自 model/base_url/api_key_env）")
    ap.add_argument("--reasoning-effort", default=None,
                    help="单模型默认档；座位配置里的 reasoning_effort 优先（null=不发送）")
    ap.add_argument("--save-on-finish", action="store_true",
                    help="跑完自动存进 1 号槽（网页里点『存档』走的是同一条）")
    args = ap.parse_args()

    counter: dict = {"n": 0}
    if args.save_dir is None:
        args.save_dir = os.path.join(ROOT, "saves",
                                     "real" if args.real else "mock")
    seat_cfg = None
    if args.seat_config:
        try:
            seat_cfg = load_seat_config(args.seat_config)
        except Exception as exc:                     # noqa: BLE001
            print(f"❌ 座位配置读不了：{type(exc).__name__}: {exc}")
            return 2
        missing = missing_seat_envs(seat_cfg)
        if missing:
            print("❌ 座位配置里的环境变量未设置：" + "，".join(missing))
            return 2

    if args.real:
        from engine.models.client import _no_real_calls
        if _no_real_calls():
            print("❌ DND_NO_REAL_CALLS=1：真调用被进程闸门禁用（先取消该环境变量）")
            return 2
        key = read_api_key()
        if not key and seat_cfg is None:
            print("❌ 没有 QIANFAN_API_KEY（环境变量或 ~/.dsh/.credentials.yaml）")
            return 2

        def _factory(label: str):
            spec = seat_spec(label, seat_cfg)
            model = str(spec.get("model") or args.model)
            base_url = str(spec.get("base_url") or args.base_url)
            if not model or not base_url:
                raise ModelError(f"座位 {label} 没有 model/base_url")
            effort = (spec["reasoning_effort"]
                      if "reasoning_effort" in spec
                      else args.reasoning_effort)
            return CappedReal(counter, cap=args.max_calls, name=label,
                              model=model, base_url=base_url,
                              api_key=seat_api_key(spec, key),
                              max_tokens=int(spec.get("max_tokens", 12000)),
                              temperature=(None if spec.get("temperature") is None
                     else float(spec.get("temperature", 0.7))),
                              timeout=float(spec.get("timeout", 180.0)),
                              reasoning_effort=effort, enabled=True)

        globals()["SEAT_FACTORY"] = _factory

    adv = load_adventure(args.adventure)
    holder: dict = {}

    def _snap() -> dict:
        sess = holder.get("session")
        if sess is None:
            return {"map": {"battle": False}, "sheet": None, "graph": None, "npcs": [],
                    "catalog": _catalog_rows(),
                    "shop": {"place": "", "items": []}, "slots": [],
                    "quests": build_journal(adv, None),
                    "scene": {"text": "", "round": 0,
                              "story": {"total": len(adv.scenes)}}}
        return sess.snapshot_extra()

    def _diag() -> dict:
        """自检：只读事实，**不含密钥**（狼人杀那个「这台手机支持什么」的对应物）。"""
        sess = holder.get("session")
        loop = sess.loop if sess is not None else None
        calls = 0
        pt = ct = 0
        if loop is not None:
            t = loop.client.totals()
            calls, pt, ct = t["calls"], t["prompt_tokens"], t["completion_tokens"]
        from engine.persist import SAVE_VERSION
        from engine.models.client import _no_real_calls
        return {
            "version": f"存档 v{SAVE_VERSION}",
            "python": sys.version.split()[0],
            "save_dir": args.save_dir,
            "save_dir_writable": save_dir_writable(args.save_dir),
            "real": bool(args.real),
            "model": args.model if args.real else "",
            "key_present": _key_present() if args.real else False,
            "no_real_calls": bool(_no_real_calls()),
            "max_calls": args.max_calls if args.real else 0,
            "calls_used": counter.get("n", 0),
            "adventure": adv.title,
            "scenes_total": len(adv.scenes),
            "round": getattr(loop, "round", 0) if loop else 0,
            "scene": getattr(loop, "story_scene", "") if loop else "",
            "status": getattr(loop, "story_status", "") if loop else "",
            "calls": calls, "prompt_tokens": pt, "completion_tokens": ct,
        }

    bridge = WebBridge(timeout=900,
                       log_filter=viewer_log_filter(
                           lambda: (holder["session"].human_name
                                    if holder.get("session") else HUMAN)),
                       snapshot_extra=_snap, print_log=True,
                       action_handler=lambda p: holder["session"].action(p))
    session = WebSession(bridge, adv, save_dir=args.save_dir,
                         session_zero=args.session_zero,
                         adv_path=args.adventure,
                         ai_count=args.ai_count,
                         characters_dir=args.characters_dir)
    holder["session"] = session
    session.new_game()
    server = TableServer(bridge, port=args.port, diag=_diag,
                         review_dir=lambda: args.save_dir).start()
    print("=" * 78)
    print(f"P5-C 真人网页座位：{server.url}")
    print(f"  （只绑 127.0.0.1；浏览器打开，点选项行动。"
          f"Ctrl+C 退出）")
    print(f"  剧本：{adv.title}｜你的角色：{session.human_name}｜存档目录：{session.save_dir}")
    if args.real:
        print(f"  座位：真模型 {args.model}（上限 {args.max_calls} 次调用，"
              f"真人座位仍是网页）")
    print("=" * 78)

    driver = None
    if args.auto:
        driver = threading.Thread(target=auto_driver, args=(server, bridge),
                                  daemon=True)
        driver.start()

    error = ""
    reported = False

    def report() -> None:
        """一局结束时打一次结算；**服务不关**，网页还能点「新的一局」。"""
        nonlocal reported
        loop = session.loop
        err = bridge.error or error
        print("\n" + "=" * 78)
        print(f"【结算】{len(loop.logs)} 轮｜"
              f"{'异常：' + err if err else '正常结束'}")
        print(f"  台账条数：{len(loop.ledger)}｜可见性："
              f"布兰 {len(loop.ledger.visible_to(HUMAN))} 条 / DM "
              f"{len(loop.ledger.visible_to('dm'))} 条")
        for u in loop.field:
            if u.team in ("pc", "ally"):
                print(f"  {u.status_line()}")
        if args.real:
            print(f"  真模型调用：{counter.get('n', 0)} 次（上限 {args.max_calls}）")
        if args.save_on_finish:
            try:
                out = session.save(1)        # 和网页点「存档」同一条路径
                print(f"  存档：{out}")
            except Exception as exc:         # noqa: BLE001
                print(f"  ⚠️ 存档失败：{exc}")
        print(f"  服务仍在：{server.url}（网页可「按此模式新开」；Ctrl+C 退出）")
        print("=" * 78)
        reported = True

    try:
        while True:
            time.sleep(0.2)
            if bridge.done and not reported:
                report()
                if args.auto:                # 无人值守仍是一次性进程
                    break
            if not bridge.done and reported:
                reported = False             # 页面开了新的一局
    except KeyboardInterrupt:
        error = "你中断了这一局"
        bridge.finish(error=error)
    finally:
        if session.thread is not None:
            session.thread.join(timeout=10)
        server.stop()

    error = bridge.error or error
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())


