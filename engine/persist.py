# coding=utf-8
"""存档 / 读档 / 断点续跑。

对应决策记录 §5.16（U3）。**纯本地文件，零模型调用、零花费。**

一份存档要能回答"关掉进程前世界上有什么"：

    stat_data 状态树（含审计流水）   谁改了什么、为什么
    台账（含可见性标签）             说过什么、谁能看到
    战场单位（含法术书/法术位/状态） 每个单位的完整机械状态
    轮次 / 先攻序 / 濒死进度         断点续跑要用的回合结构
    骰子 RNG 状态                   读档后"同一颗骰子"还能接着掷

**默认客户端是 `FakeModel`** —— 读档永远不会因为"忘了传客户端"而真花钱。
真模型必须显式传进来（P4 之后）。

格式带 `format` + `version`：版本对不上就**明确拒绝**，不试着猜。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .dice import Random
from .memory import MemoryError, MemoryStore
from .models.client import FakeModel, ModelClient
from .rules.loop import GameLoop, RoundLog
from .rules.clues import SceneClue
from .rules.reactions import ReadyTrigger
from .rules.spells import Spell
from .rules.turn import DeathState
from .rules.unit import Action, Attack, Unit
from .state.schema import SCHEMA_VERSION
from .state.tree import StateError, StateTree
from .state.visibility import Ledger

__all__ = [
    "SaveError", "GameSession", "SAVE_FORMAT", "SAVE_VERSION",
    "SUPPORTED_SAVE_VERSIONS",
    "HISTORY_KEEP", "HISTORY_MAX_BYTES", "SAVE_LOG_MAX_BYTES",
    "prune_kept",
    "dump_game", "load_from_dict", "save_game", "load_game",
    "calls_path_for", "write_calls_log", "CALLS_SUFFIX",
]

SAVE_FORMAT = "dnd-campaign-save"
#: v2（2026-11-04，P5-G）：`loop` 里多了 `memory` / `recall_count` / `recall_chars`。
#: **没有做迁移器**：v1 档直接拒绝（照实说，不猜着读）。理由见 §4.6 第 2 条。
SAVE_VERSION = 4
#: 可读的旧版本：v4 只加了带缺省的新字段（mods/item_effects/item_charges…），
#: 所以 v3 旧档按缺省读；更老的档没有迁移器，明确拒绝。
SUPPORTED_SAVE_VERSIONS = (3, SAVE_VERSION)

#: 留痕容量预算（Q04）：份数和总字节**两个闸都要过**，取更严的那个。
#: 只按份数留会让一份大档把磁盘吃满；只按字节留会让小档无限攒。
HISTORY_KEEP = 20                                   # history/ 每个存档名最多留几份
HISTORY_MAX_BYTES = 64 * 1024 * 1024                # history/ 每个存档名总量上限
SAVE_LOG_MAX_BYTES = 4 * 1024 * 1024                # save_log.jsonl 上限（超了裁最旧的行）


class SaveError(Exception):
    """存档读不了/写不了/版本不对。**说清楚是哪一种**，不静默当成空局。"""


@dataclass
class GameSession:
    """读回来的一局：状态树 + 台账 + 已经接好线的 GameLoop。"""
    state: StateTree
    ledger: Ledger
    loop: GameLoop
    meta: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        n_pc = len([u for u in self.loop.field if u.team in ("pc", "ally")])
        n_enemy = len([u for u in self.loop.field if u.team == "enemy"])
        return (f"第 {self.loop.round} 轮｜PC {n_pc} / 敌人 {n_enemy}｜"
                f"台账 {len(self.ledger)} 条｜审计 {len(self.state.audit)} 条")


# --------------------------------------------------------------------------- #
# 单位 / 动作 / 法术
# --------------------------------------------------------------------------- #

def _attack_to_dict(a: Attack) -> dict:
    return {
        "name": a.name,
        "attack_bonus": a.attack_bonus,
        "damage_notation": a.damage_notation,
        "damage_type": a.damage_type,
        "extra_damage_notation": a.extra_damage_notation,
        "extra_damage_type": a.extra_damage_type,
        "reach": a.reach,
        "range_normal": a.range_normal,
        "range_long": a.range_long,
        "is_melee": bool(a.is_melee),
        "properties": list(a.properties),
    }


def _attack_from_dict(d: dict) -> Attack:
    try:
        return Attack(
            name=str(d["name"]),
            attack_bonus=int(d.get("attack_bonus", 0)),
            damage_notation=str(d.get("damage_notation", "")),
            damage_type=str(d.get("damage_type", "")),
            extra_damage_notation=str(d.get("extra_damage_notation", "")),
            extra_damage_type=str(d.get("extra_damage_type", "")),
            reach=(None if d.get("reach") is None else int(d["reach"])),
            range_normal=(None if d.get("range_normal") is None
                          else int(d["range_normal"])),
            range_long=(None if d.get("range_long") is None
                        else int(d["range_long"])),
            is_melee=bool(d.get("is_melee", True)),
            properties=tuple(str(x) for x in (d.get("properties") or ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"攻击存档格式不对：{exc}") from exc


def _action_to_dict(a: Action) -> dict:
    return {
        "name": a.name,
        "slot": a.slot,
        "attacks": [_attack_to_dict(x) for x in a.attacks],
        "save_ability": a.save_ability,
        "save_dc": a.save_dc,
        "save_success_half": bool(a.save_success_half),
        "damage_notation": a.damage_notation,
        "damage_type": a.damage_type,
        "heal_notation": str(getattr(a, "heal_notation", "")),
        "area_size": a.area_size,
        "area_shape": a.area_shape,
        "recharge_on_roll": a.recharge_on_roll,
        "uses_per_day": a.uses_per_day,
        "legendary_cost": a.legendary_cost,
        "range_ft": int(getattr(a, "range_ft", 0) or 0),
        "desc": a.desc,
        "structured": bool(a.structured),
    }


def _action_from_dict(d: dict) -> Action:
    try:
        return Action(
            name=str(d["name"]),
            slot=str(d.get("slot", "action")),
            attacks=[_attack_from_dict(x) for x in (d.get("attacks") or [])],
            save_ability=str(d.get("save_ability", "")),
            save_dc=int(d.get("save_dc", 0)),
            save_success_half=bool(d.get("save_success_half", True)),
            damage_notation=str(d.get("damage_notation", "")),
            damage_type=str(d.get("damage_type", "")),
            heal_notation=str(d.get("heal_notation", "")),
            area_size=int(d.get("area_size", 0)),
            area_shape=str(d.get("area_shape", "")),
            recharge_on_roll=int(d.get("recharge_on_roll", 0)),
            uses_per_day=int(d.get("uses_per_day", 0)),
            legendary_cost=int(d.get("legendary_cost", 0)),
            range_ft=int(d.get("range_ft", 0)),
            desc=str(d.get("desc", "")),
            structured=bool(d.get("structured", True)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"动作存档格式不对：{exc}") from exc


def _spell_to_dict(sp: Spell) -> dict:
    return {
        "key": sp.key,
        "name": sp.name,
        "level": sp.level,
        "school": sp.school,
        "casting_time": sp.casting_time,
        "slot": sp.slot,
        "range_ft": sp.range_ft,
        "range_text": sp.range_text,
        "duration": sp.duration,
        "concentration": bool(sp.concentration),
        "ritual": bool(sp.ritual),
        "attack_roll": bool(sp.attack_roll),
        "save_ability": sp.save_ability,
        "save_success_half": bool(sp.save_success_half),
        "damage_roll": sp.damage_roll,
        "damage_type": sp.damage_type,
        "heal_roll": sp.heal_roll,
        "heal_adds_modifier": bool(sp.heal_adds_modifier),
        "temp_hp_roll": sp.temp_hp_roll,
        "shape_type": sp.shape_type,
        "shape_size": sp.shape_size,
        "target_count": sp.target_count,
        "upcast_damage": sp.upcast_damage,
        "upcast_desc": sp.upcast_desc,
        "projectiles": int(sp.projectiles),
        "upcast_projectiles": int(sp.upcast_projectiles),
        "level_options": {str(lv): dict(opt)
                          for lv, opt in sp.level_options.items()},
        "slot_options": {str(lv): dict(opt)
                         for lv, opt in sp.slot_options.items()},
        "components": sp.components,
        "material_cost": sp.material_cost,
        "material_consumed": bool(sp.material_consumed),
        "desc": sp.desc,
        "structured": bool(sp.structured),
        "unstructured_reason": sp.unstructured_reason,
    }


def _spell_from_dict(d: dict) -> Spell:
    try:
        slot_options: dict[int, dict] = {}
        for lv, opt in (d.get("slot_options") or {}).items():
            slot_options[int(lv)] = {
                "damage_roll": str((opt or {}).get("damage_roll", "")),
                "heal_roll": str((opt or {}).get("heal_roll", "")),
            }
        return Spell(
            key=str(d.get("key", "")),
            name=str(d["name"]),
            level=int(d.get("level", 0)),
            school=str(d.get("school", "")),
            casting_time=str(d.get("casting_time", "")),
            slot=str(d.get("slot", "action")),
            range_ft=int(d.get("range_ft", 0)),
            range_text=str(d.get("range_text", "")),
            duration=str(d.get("duration", "instantaneous")),
            concentration=bool(d.get("concentration", False)),
            ritual=bool(d.get("ritual", False)),
            attack_roll=bool(d.get("attack_roll", False)),
            save_ability=str(d.get("save_ability", "")),
            save_success_half=bool(d.get("save_success_half", False)),
            damage_roll=str(d.get("damage_roll", "")),
            damage_type=str(d.get("damage_type", "")),
            heal_roll=str(d.get("heal_roll", "")),
            heal_adds_modifier=bool(d.get("heal_adds_modifier", False)),
            temp_hp_roll=str(d.get("temp_hp_roll", "")),
            shape_type=str(d.get("shape_type", "")),
            shape_size=int(d.get("shape_size", 0)),
            target_count=int(d.get("target_count", 1)),
            upcast_damage=str(d.get("upcast_damage", "")),
            upcast_desc=str(d.get("upcast_desc", "")),
            projectiles=int(d.get("projectiles", 1)),
            upcast_projectiles=int(d.get("upcast_projectiles", 0)),
            level_options={int(lv): dict(opt)
                           for lv, opt in (d.get("level_options") or {}).items()},
            slot_options=slot_options,
            components=str(d.get("components", "")),
            material_cost=str(d.get("material_cost", "")),
            material_consumed=bool(d.get("material_consumed", False)),
            desc=str(d.get("desc", "")),
            structured=bool(d.get("structured", True)),
            unstructured_reason=str(d.get("unstructured_reason", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"法术存档格式不对：{exc}") from exc


def unit_to_dict(u: Unit) -> dict:
    """把一个战斗单位完整导出（含法术书、法术位、状态、资源）。"""
    return {
        "id": u.id, "name": u.name, "team": u.team,
        "is_pc": bool(u.is_pc), "level": u.level,
        "ac": u.ac, "ac_note": u.ac_note,
        "hp_max": u.hp_max, "hp": u.hp, "temp_hp": u.temp_hp,
        "abilities": dict(u.abilities),
        "save_proficiencies": sorted(u.save_proficiencies),
        "skill_bonuses": dict(u.skill_bonuses),
        "save_bonuses": dict(u.save_bonuses),
        "speeds": dict(u.speeds),
        "size": u.size,
        "senses": dict(u.senses),
        "passive_perception": u.passive_perception,
        "position": list(u.position),
        "conditions": dict(u.conditions),
        "exhaustion": u.exhaustion,
        "actions": [_action_to_dict(a) for a in u.actions],
        "spells": [_spell_to_dict(sp) for sp in u.spells],
        "reactions": [r.to_dict() for r in u.reactions
                      if isinstance(r, ReadyTrigger)],
        "cast_ability": u.cast_ability,
        "spell_slots": {str(lv): dict(v) for lv, v in u.spell_slots.items()},
        "consumables": {str(k): dict(v) for k, v in u.consumables.items()},
        "resources": {k: (dict(v) if isinstance(v, dict) else v)
                      for k, v in u.resources.items()},
        "resistances": sorted(u.resistances),
        "immunities": sorted(u.immunities),
        "vulnerabilities": sorted(u.vulnerabilities),
        "condition_immunities": sorted(u.condition_immunities),
        "used": dict(u.used),
        "movement_used": u.movement_used,
        "legendary_actions_max": u.legendary_actions_max,
        "legendary_actions_used": u.legendary_actions_used,
        "lair_actions_max": u.lair_actions_max,
        "reaction_used": bool(u.reaction_used),
        "concentrating_on": u.concentrating_on,
        "source_key": u.source_key,
        "init_override": u.init_override,
        "mods": [dict(m) for m in u.mods],
        "item_effects": [dict(e) for e in u.item_effects],
        "item_charges": {str(k): dict(v) for k, v in u.item_charges.items()},
        "notes": list(u.notes),
    }


def unit_from_dict(d: dict) -> Unit:
    """从存档造回一个 `Unit`。缺关键字段就抛 `SaveError`，不造半个单位。"""
    if not isinstance(d, dict):
        raise SaveError(f"单位存档必须是对象，拿到的是 {type(d).__name__}")
    try:
        pos = tuple(float(x) for x in (d.get("position") or (0.0, 0.0)))
        if len(pos) != 2:
            raise ValueError(f"position 应该是两个数，拿到 {pos!r}")
        u = Unit(
            id=str(d["id"]), name=str(d["name"]),
            team=str(d.get("team", "neutral")),
            is_pc=bool(d.get("is_pc", False)),
            level=int(d.get("level", 1)),
            ac=int(d.get("ac", 10)),
            ac_note=str(d.get("ac_note", "")),
            hp_max=int(d.get("hp_max", 1)),
            hp=int(d.get("hp", 0)),
            temp_hp=int(d.get("temp_hp", 0)),
            abilities={str(k): int(v) for k, v in (d.get("abilities") or {}).items()},
            save_proficiencies={str(x) for x in (d.get("save_proficiencies") or [])},
            skill_bonuses={str(k): int(v)
                           for k, v in (d.get("skill_bonuses") or {}).items()},
            save_bonuses={str(k): int(v)
                          for k, v in (d.get("save_bonuses") or {}).items()},
            speeds={str(k): int(v) for k, v in (d.get("speeds") or {}).items()},
            size=str(d.get("size", "medium")),
            senses={str(k): int(v) for k, v in (d.get("senses") or {}).items()},
            passive_perception=int(d.get("passive_perception", 10)),
            position=pos,
            conditions={str(k): int(v) for k, v in (d.get("conditions") or {}).items()},
            exhaustion=int(d.get("exhaustion", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"单位存档格式不对：{exc}") from exc

    u.actions = [_action_from_dict(x) for x in (d.get("actions") or [])]
    u.mods = [dict(m) for m in (d.get("mods") or []) if isinstance(m, dict)]
    u.item_effects = [dict(e) for e in (d.get("item_effects") or [])
                      if isinstance(e, dict)]
    u.item_charges = {str(k): dict(v)
                      for k, v in (d.get("item_charges") or {}).items()
                      if isinstance(v, dict)}
    if d.get("init_override") is not None:
        try:
            u.init_override = int(d["init_override"])
        except (TypeError, ValueError) as exc:
            raise SaveError(f"先攻覆盖值不是整数：{d.get('init_override')!r}") from exc
    u.spells = [_spell_from_dict(x) for x in (d.get("spells") or [])]
    try:
        u.reactions = [ReadyTrigger.from_dict(x)
                       for x in (d.get("reactions") or [])]
    except (TypeError, ValueError) as exc:
        raise SaveError(f"准备动作存档格式不对：{exc}") from exc
    u.cast_ability = str(d.get("cast_ability", ""))
    u.spell_slots = {
        int(lv): {"current": int(v.get("current", 0)),
                  "max": int(v.get("max", 0))}
        for lv, v in (d.get("spell_slots") or {}).items()}
    u.resources = {str(k): (dict(v) if isinstance(v, dict) else v)
                   for k, v in (d.get("resources") or {}).items()}
    u.consumables = {str(k): {"数量": int(v.get("数量", 0)),
                              "治疗": str(v.get("治疗", "")),
                              "动作": str(v.get("动作", "bonus_action"))}
                     for k, v in (d.get("consumables") or {}).items()
                     if isinstance(v, dict)}
    u.resistances = {str(x) for x in (d.get("resistances") or [])}
    u.immunities = {str(x) for x in (d.get("immunities") or [])}
    u.vulnerabilities = {str(x) for x in (d.get("vulnerabilities") or [])}
    u.condition_immunities = {str(x) for x in (d.get("condition_immunities") or [])}
    u.used = {str(k): int(v) for k, v in (d.get("used") or {}).items()}
    u.movement_used = float(d.get("movement_used", 0.0))
    u.legendary_actions_max = int(d.get("legendary_actions_max", 0))
    u.legendary_actions_used = int(d.get("legendary_actions_used", 0))
    u.lair_actions_max = int(d.get("lair_actions_max", 0))
    u.reaction_used = bool(d.get("reaction_used", False))
    u.concentrating_on = str(d.get("concentrating_on", ""))
    u.source_key = str(d.get("source_key", ""))
    u.notes = [str(x) for x in (d.get("notes") or [])]
    return u


# --------------------------------------------------------------------------- #
# 轮次 / 濒死 / RNG
# --------------------------------------------------------------------------- #

def _log_to_dict(lg: RoundLog) -> dict:
    return {"n": lg.n, "phase": lg.phase, "lines": list(lg.lines),
            "calls": lg.calls, "prompt_tokens": lg.prompt_tokens,
            "completion_tokens": lg.completion_tokens,
            "problems": list(lg.problems)}


def _log_from_dict(d: dict) -> RoundLog:
    try:
        return RoundLog(
            n=int(d["n"]),
            phase=str(d.get("phase", "explore")),
            lines=[str(x) for x in (d.get("lines") or [])],
            calls=int(d.get("calls", 0)),
            prompt_tokens=int(d.get("prompt_tokens", 0)),
            completion_tokens=int(d.get("completion_tokens", 0)),
            problems=[str(x) for x in (d.get("problems") or [])],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"轮次日志格式不对：{exc}") from exc


def _death_to_dict(st: DeathState) -> dict:
    return {"successes": st.successes, "failures": st.failures,
            "stable": bool(st.stable), "dead": bool(st.dead)}


def _death_from_dict(d: dict) -> DeathState:
    try:
        return DeathState(successes=int(d.get("successes", 0)),
                          failures=int(d.get("failures", 0)),
                          stable=bool(d.get("stable", False)),
                          dead=bool(d.get("dead", False)))
    except (TypeError, ValueError) as exc:
        raise SaveError(f"濒死进度格式不对：{exc}") from exc


def _rng_to_dict(rng: Random) -> dict:
    state = rng.getstate()
    return {"version": int(state[0]), "internal": list(state[1]),
            "gauss": state[2]}


def _rng_from_dict(d: dict | None) -> Random:
    """恢复骰子状态；没存过就用默认种子（读档后仍可复现，只是重新起一条序列）。"""
    rng = Random(20261014)
    if d is None:
        return rng
    try:
        rng.setstate((int(d["version"]),
                      tuple(int(x) for x in d["internal"]),
                      d.get("gauss")))
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"骰子状态格式不对：{exc}") from exc
    return rng


# --------------------------------------------------------------------------- #
# 导出 / 导入
# --------------------------------------------------------------------------- #

def _loop_to_dict(loop: GameLoop) -> dict:
    d = {
        "round": loop.round,
        "label": loop.label,
        "units": [unit_to_dict(u) for u in loop.field],
        "logs": [_log_to_dict(lg) for lg in loop.logs],
        "death": {uid: _death_to_dict(st) for uid, st in loop.death.items()},
        "rng": _rng_to_dict(loop.rng),
        #: 记忆库（P5-G）。没有就写 null —— 读档时恢复成"没有记忆库"，
        #: 不假装有一本空记忆（两者的装配行为不同：空库会挂"没有可召回"的说明）。
        "memory": loop.memory.to_dict() if loop.memory is not None else None,
        "recall_count": getattr(loop, "recall_count", 6),
        "recall_chars": getattr(loop, "recall_chars", 900),
        #: 台账折叠口径（P5-B2）。读档后保持同一口径，否则同一局前后包体差一倍。
        "ledger_rounds": getattr(loop, "ledger_rounds", 6),
        "combat_ledger_rounds": getattr(loop, "combat_ledger_rounds", 4),
        #: 当前剧本场景（P5-B2 之后的字段）：中途存档续跑时线索不能归零。
        "scene_id": getattr(loop, "scene_id", ""),
        "adventure_title": getattr(loop, "adventure_title", ""),
        "story_act": getattr(loop, "story_act", ""),
        "story_scene": getattr(loop, "story_scene", ""),
        "story_status": getattr(loop, "story_status", ""),
        "scenes_done": list(getattr(loop, "scenes_done", [])),
        "scene_clues": [
            {"id": c.id, "text": c.text, "skill": c.skill, "dc": c.dc,
             "secret": c.secret, "place": c.place, "source": c.source}
            for c in getattr(loop, "scene_clues", ())
        ],
    }
    init = getattr(loop, "initiative", None)
    if init is not None:
        d["initiative"] = [{"unit": u.id, "init": int(v)} for u, v in init]
    return d


def _loop_from_dict(state: StateTree, ledger: Ledger, client: ModelClient,
                    d: dict) -> GameLoop:
    if not isinstance(d, dict):
        raise SaveError(f"回合存档必须是对象，拿到的是 {type(d).__name__}")
    loop = GameLoop(state, ledger, client, rng=_rng_from_dict(d.get("rng")),
                    label=str(d.get("label", "")))
    units = [unit_from_dict(x) for x in (d.get("units") or [])]
    try:
        loop.set_field(units)
    except ValueError as exc:
        raise SaveError(f"战场单位存档有问题：{exc}") from exc
    loop.round = int(d.get("round", 0))
    loop.logs = [_log_from_dict(x) for x in (d.get("logs") or [])]
    mem_doc = d.get("memory")
    if mem_doc:
        try:
            loop.memory = MemoryStore.from_dict(mem_doc)
        except (MemoryError, ValueError, TypeError) as exc:
            raise SaveError(f"记忆库存档有问题：{exc}") from exc
    if d.get("recall_count") is not None:
        loop.recall_count = int(d["recall_count"])
    if d.get("recall_chars") is not None:
        loop.recall_chars = int(d["recall_chars"])
    if d.get("ledger_rounds") is not None:
        loop.ledger_rounds = max(1, int(d["ledger_rounds"]))
    if d.get("combat_ledger_rounds") is not None:
        loop.combat_ledger_rounds = max(1, int(d["combat_ledger_rounds"]))
    loop.scene_id = str(d.get("scene_id", ""))
    loop.scene_clues = tuple(
        SceneClue(id=str(x["id"]), text=str(x["text"]),
                  skill=str(x.get("skill", "perception")),
                  dc=int(x.get("dc", 12)), secret=bool(x.get("secret", True)),
                  place=str(x.get("place", "")), source=str(x.get("source", "")))
        for x in (d.get("scene_clues") or []))
    loop.adventure_title = str(d.get("adventure_title", ""))
    loop.story_act = str(d.get("story_act", ""))
    loop.story_scene = str(d.get("story_scene", ""))
    loop.story_status = str(d.get("story_status", ""))
    loop.scenes_done = [str(x) for x in (d.get("scenes_done") or [])]
    loop.death = {str(uid): _death_from_dict(x)
                  for uid, x in (d.get("death") or {}).items()}
    init_rows = d.get("initiative")
    if init_rows is not None:
        by_id = {u.id: u for u in units}
        restored = []
        for row in init_rows:
            try:
                uid, val = str(row["unit"]), int(row["init"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SaveError(f"先攻序格式不对：{row!r}") from exc
            u = by_id.get(uid) or loop.find_unit(uid)
            if u is None:
                raise SaveError(f"先攻序里的单位 {uid!r} 不在战场上")
            restored.append((u, val))
        loop.initiative = restored
    loop._cur = None
    return loop


def _client_to_dict(client: ModelClient) -> dict:
    """调用记账（token/延迟/成本）也进存档 —— 读档后 `report()` 接着算，不归零。"""
    calls = []
    for c in getattr(client, "calls", []):
        calls.append({
            "seq": c.seq, "role": c.role, "actor": c.actor, "task": c.task,
            "model": c.model, "prompt_tokens": c.prompt_tokens,
            "completion_tokens": c.completion_tokens, "elapsed": c.elapsed,
            "cached_tokens": int(getattr(c, "cached_tokens", 0)),
            "ok": bool(c.ok), "error": c.error,
            "attempts": int(getattr(c, "attempts", 1)),
            "refused": bool(getattr(c, "refused", False)),
        })
    return {"name": getattr(client, "name", ""), "calls": calls}


def _restore_client_calls(client: ModelClient, d: dict | None) -> None:
    if not d:
        return
    if not isinstance(d, dict):
        raise SaveError(f"调用记账存档必须是对象，拿到的是 {type(d).__name__}")
    try:
        client.load_calls(list(d.get("calls") or []))
    except (TypeError, ValueError) as exc:
        raise SaveError(f"调用记账存档格式不对：{exc}") from exc


def dump_game(*, state: StateTree, ledger: Ledger, loop: GameLoop,
              note: str = "", extra: dict | None = None) -> dict:
    """把一局导出成可 JSON 序列化的 dict（落盘前先在这里集中一次）。"""
    if not isinstance(state, StateTree):
        raise SaveError("dump_game 需要 StateTree")
    if not isinstance(ledger, Ledger):
        raise SaveError("dump_game 需要 Ledger")
    if not isinstance(loop, GameLoop):
        raise SaveError("dump_game 需要 GameLoop")
    if not isinstance(loop.client, ModelClient):
        raise SaveError(f"GameLoop 的客户端不是 ModelClient：{type(loop.client).__name__}")
    return {
        "format": SAVE_FORMAT,
        "version": SAVE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": str(note),
        "state": state.to_dict(include_audit=True),
        "ledger": ledger.to_dict(),
        "client": _client_to_dict(loop.client),
        "loop": _loop_to_dict(loop),
        "extra": dict(extra or {}),
    }


def load_from_dict(d: dict, *, client: ModelClient | None = None) -> GameSession:
    """从 dict 恢复一局。`client` 不传时用 `FakeModel`（零花费）。

    ⚠️ 存档里的**调用记账会覆盖**传入客户端的 `calls`（若有），
    这样读档后 `report()` 的账才是连续的；上游连接/缓存不在此列。
    """
    if not isinstance(d, dict):
        raise SaveError(f"存档根必须是对象，拿到的是 {type(d).__name__}")
    fmt = d.get("format")
    if fmt != SAVE_FORMAT:
        raise SaveError(f"不是本项目的存档（format={fmt!r}，需要 {SAVE_FORMAT!r}）")
    ver = d.get("version")
    sv = d.get("schema_version")
    if sv is not None and int(sv) != SCHEMA_VERSION:
        raise SaveError(
            f"存档 schema 版本 {sv} 与引擎 {SCHEMA_VERSION} 不匹配 —— "
            f"字段语义可能已变，请用同一版本引擎读，或迁移旧档")
    if ver not in SUPPORTED_SAVE_VERSIONS:
        raise SaveError(
            f"存档版本 {ver!r} 与引擎 {SAVE_VERSION} 不匹配 —— "
            f"请用同一版本的引擎读，或迁移旧档")
    missing = [k for k in ("state", "ledger", "loop") if k not in d]
    if missing:
        raise SaveError(f"存档缺少字段：{', '.join(missing)}")

    try:
        state = StateTree.from_dict(d["state"])
        ledger = Ledger.from_dict(d["ledger"])
        cli = client if client is not None else FakeModel(name="fake-loaded")
        _restore_client_calls(cli, d.get("client"))
        loop = _loop_from_dict(state, ledger, cli, d["loop"])
    except SaveError:
        raise
    except (StateError, ValueError, KeyError, TypeError) as exc:
        raise SaveError(f"存档内容损坏：{exc}") from exc

    return GameSession(
        state=state, ledger=ledger, loop=loop,
        meta={"format": fmt, "version": ver,
              "saved_at": d.get("saved_at", ""), "note": d.get("note", ""),
              "extra": d.get("extra") if isinstance(d.get("extra"), dict) else {}},
        warnings=state.validate(),
    )


def prune_kept(paths, *, keep: int | None = HISTORY_KEEP,
               max_bytes: int | None = HISTORY_MAX_BYTES) -> list[str]:
    """按「份数 + 总字节」双阈值从旧到新删，返回实际删掉的路径。

    - 两个闸**同时**生效：`keep` 管份数，`max_bytes` 管总量，谁先到谁停；
    - **`None` 表示那一边不限**；传 `0` 是“零预算”，不是“不限”：
      `keep=0` 删光，`max_bytes=0` 删到只剩最新一份（2026-11-08 统一口径）；
    - 单个文件就超过 `max_bytes` 时，最多删到只剩它一份，不会把档删光；
    - **读不出 `stat` 的条目直接跳过**：不删它、也不把它算进总量（与旧注释说的
      “当作 0 字节、排最旧”相反——旧注释是错的，2026-11-08 外审 P2-3）；
    - 删不掉的跳过，不在这里抛。
    """
    target = None if keep is None else max(0, int(keep))
    limit = None if max_bytes is None else max(0, int(max_bytes))
    alive: list[tuple[float, str, int, str]] = []
    for p in paths:
        try:
            st = os.stat(p)
            alive.append((st.st_mtime, os.path.basename(p), int(st.st_size), p))
        except OSError:
            continue
    alive.sort()
    total = sum(x[2] for x in alive)
    removed: list[str] = []
    while alive and ((target is not None and len(alive) > target)
                     or (limit is not None and total > limit and len(alive) > 1)):
        _mtime, _name, size, path = alive[0]
        try:
            os.unlink(path)
        except OSError:
            alive.pop(0)                 # 删不掉就别死循环，换下一个
            continue
        alive.pop(0)
        total -= size
        removed.append(path)
    return removed


def _archive_previous(path: str, *, keep: int | None = None,
                      max_bytes: int | None = None) -> str | None:
    """覆盖前把旧档复制进 `history/`，再按「份数 + 总字节」裁掉最旧的。

    存档本身是"当前局面"，历史是"当时局面"；复盘/追责看历史，不靠覆盖。
    裁剪只碰同一个存档名的历史（`<stem>.*.json`），不会误删别的槽。
    """
    if not os.path.isfile(path):
        return None
    keep = HISTORY_KEEP if keep is None else int(keep)
    max_bytes = HISTORY_MAX_BYTES if max_bytes is None else int(max_bytes)
    parent = os.path.dirname(os.path.abspath(path))
    hist = os.path.join(parent, "history")
    os.makedirs(hist, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dst = os.path.join(hist, f"{stem}.{stamp}.json")
    shutil.copy2(path, dst)
    # 只认**完全同槽**的历史：`<stem>.<时间戳>.json`。
    # 以前用 `startswith(stem + ".")`，`a.json` 会把 `a.b.*.json`（另一个槽的历史）
    # 算进自家并删掉 —— 存档名带点号前缀时（如 `<剧本>-<模型>.json`）就会误删
    #（2026-11-08 外审 P2-2）。
    pattern = re.compile(rf"^{re.escape(stem)}\.\d{{8}}T\d+Z\.json$")
    olds = sorted(
        os.path.join(hist, x) for x in os.listdir(hist)
        if pattern.match(x)
    )
    prune_kept(olds, keep=keep, max_bytes=max_bytes)
    return dst


def _trim_save_log(log_path: str, *, max_bytes: int | None = None) -> int:
    """`save_log.jsonl` 超上限时按行裁掉最旧的，返回删了几行。

    先用「追加」把这次记录写进去，再裁；裁完仍是合法 JSONL（整行为单位）。
    没超上限 / 读不出来都不动文件（留痕坏了不能把"已经存好"判失败）。

    ⚠️ 读日志用 `errors="replace"`：日志被外部改写/掉电截断时会有坏字节，
    以前 `readlines()` 抛 `UnicodeDecodeError`，穿透 `save_game` 的 `except OSError`，
    把**已经写成功**的存档报成失败（2026-11-08 外审 P2-1）。
    """
    limit = SAVE_LOG_MAX_BYTES if max_bytes is None else int(max_bytes)
    if limit <= 0:
        return 0
    try:
        if os.path.getsize(log_path) <= limit:
            return 0
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except (OSError, ValueError):
        return 0
    sizes = [len(x.encode("utf-8")) for x in lines]
    total = sum(sizes)
    cut = 0
    while cut < len(lines) - 1 and total > limit:
        total -= sizes[cut]
        cut += 1
    if cut <= 0:
        return 0
    kept = lines[cut:]
    fd, tmp = tempfile.mkstemp(prefix=".dnd-log-", suffix=".tmp",
                               dir=os.path.dirname(os.path.abspath(log_path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(kept)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, log_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 0
    return cut


def _append_save_log(path: str, *, state: StateTree, ledger: Ledger,
                     loop: GameLoop, note: str,
                     max_bytes: int | None = None) -> str:
    """追加一行 `save_log.jsonl`：哪一局、几点、多少条台账/审计、文件指纹。"""
    parent = os.path.dirname(os.path.abspath(path))
    log_path = os.path.join(parent, "save_log.jsonl")
    size = os.path.getsize(path)
    rec = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file": os.path.basename(path),
        "note": str(note),
        "scene": str(getattr(loop, "story_scene", "")),
        "status": str(getattr(loop, "story_status", "")),
        "scenes_done": list(getattr(loop, "scenes_done", []) or []),
        "ledger": len(ledger),
        "audit": len(state.audit),
        "bytes": size,
        "sha256": _sha256(path),
        "save_version": SAVE_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _trim_save_log(log_path, max_bytes=max_bytes)
    return log_path


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive_guarded(path: str, *, keep: int | None,
                     max_bytes: int | None) -> None:
    """备份旧档，**失败只当没备份**（留痕失败 ≠ 存档失败）。"""
    try:
        _archive_previous(path, keep=keep, max_bytes=max_bytes)
    except OSError:
        # 磁盘满/权限/history 被占：宁可没有历史副本，也不能把刚打完的结果丢掉
        pass


#: 旁挂调用留痕：`<存档名>.calls.jsonl`（一行一调用，含思考链与 prompt）。
#: 不进主存档 —— 主存档要小、history 每次保存一份，塞进去会翻倍（Q36 讨论口径）。
CALLS_SUFFIX = ".calls.jsonl"


def calls_path_for(save_path: os.PathLike | str) -> str:
    """主存档旁边的调用留痕文件路径（`foo.json` → `foo.calls.jsonl`）。"""
    base, _ext = os.path.splitext(os.fspath(save_path))
    return base + CALLS_SUFFIX


def write_calls_log(save_path: os.PathLike | str, client) -> str:
    """把这次会话的调用（含思考链/prompt）写成旁挂 jsonl；没有调用就不写。

    返回写出的路径（没写则空串）。**原子写**：临时文件 + `os.replace`。
    """
    calls = list(getattr(client, "calls", []) or [])
    if not calls:
        return ""
    target = calls_path_for(save_path)
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dnd-calls-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for c in calls:
                reasoning = str(getattr(c, "reasoning", "") or "")
                prompt = str(getattr(c, "prompt", "") or "")
                f.write(json.dumps({
                    "seq": c.seq, "role": c.role, "actor": c.actor,
                    "task": c.task, "model": c.model,
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                    "cached_tokens": int(getattr(c, "cached_tokens", 0) or 0),
                    "elapsed": c.elapsed, "ok": bool(c.ok),
                    "error": c.error, "attempts": c.attempts,
                    "reasoning_len": len(reasoning),
                    "reasoning": reasoning,
                    "prompt_chars": len(prompt),
                    "prompt": prompt,
                }, ensure_ascii=False) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SaveError(f"写调用留痕失败：{target!r}（{exc}）") from exc
    return target


def save_game(path, *, state: StateTree, ledger: Ledger, loop: GameLoop,
              note: str = "", extra: dict | None = None,
              history_keep: int | None = None,
              history_max_bytes: int | None = None,
              save_log_max_bytes: int | None = None) -> str:
    """原子写盘：先写同目录临时文件，成功后再 `os.replace`。

    ⚠️ 不用"直接覆盖"——写到一半停电会留下半个 JSON，读档时才发现。

    留痕有容量预算（Q04）：`history/` 按「份数 + 总字节」裁最旧的，
    `save_log.jsonl` 超上限裁最旧的行；三个参数只给测试/特殊场景收紧用，
    正常调用走模块常量。
    """
    doc = dump_game(state=state, ledger=ledger, loop=loop, note=note, extra=extra)
    try:
        text = json.dumps(doc, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise SaveError(f"存档里有不能 JSON 序列化的内容：{exc}") from exc

    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dnd-save-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SaveError(f"写存档失败：{path!r}（{exc}）") from exc
    _archive_guarded(path, keep=history_keep, max_bytes=history_max_bytes)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SaveError(f"写存档失败：{path!r}（{exc}）") from exc
    try:
        _append_save_log(path, state=state, ledger=ledger, loop=loop, note=note,
                         max_bytes=save_log_max_bytes)
    except OSError:
        pass                     # 留痕失败不能把“已经存好”的档判失败
    try:
        # 旁挂调用留痕（含思考链/prompt）：主存档已经写好了，这个失败也不判失败。
        write_calls_log(path, getattr(loop, "client", None))
    except (SaveError, OSError):
        pass
    return path


def load_game(path, *, client: ModelClient | None = None) -> GameSession:
    """读一局。文件不存在/不是 JSON/版本不对，都抛 `SaveError` 并说清原因。"""
    path = os.fspath(path)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as exc:
        raise SaveError(f"读不到存档 {path!r}：{exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise SaveError(f"存档不是合法 JSON：{path!r}（{exc}）") from exc
    return load_from_dict(doc, client=client)


