# coding=utf-8
"""网页动作（背包/法术）—— **玩家点按钮改状态，模型不参与**。

全部走 `StateTree.commit`，所以有审计、有 schema 校验、失败可回滚。
战斗轮里不允许换装备/改准备法术（5e 的动作经济里这不是免费动作）。
"""

from __future__ import annotations

from .rules.armor import (
    apply_armor_class, armor_profile, homebrew_armor_profile,
)
from .rules.equipment import DAILY_SLOT, slot_of, validate_equip
from .rules.effect_catalog import (
    ENTRIES as BUILTIN_ENTRIES, TIER_NAMES,
    check_equip_budget, character_budget, get_entry, item_tier_total,
    summarize, validate_entry, validate_item_entries,
)
from .rules.effects import add_mod, clear_mods, coerce_effect, describe_one
from .rules.homebrew import ACTIONS_KEY, coerce_action_spec, describe_spec
from .rules.item_effects import coerce_item_effects
from .rules.npc_units import NpcError, coerce_npc_stats, npc_summary
from .rules.economy import (
    EconomyError, buy, sell, shop_stock, use_consumable,
)
from .rules.loop import GameLoop
from .state import StateError, StateTree
from .state.patch import escape_token
from .state.world import World, WorldError

__all__ = ["ActionError", "apply_action"]

#: DM 造物可写的引擎字段。**只有这里列出的会参与计算；其余只进展示文本。**
_ITEM_STAT_KEYS = {
    "攻击加值": int, "伤害骰": str, "伤害类型": str,
    "护甲加值": int, "护甲类型": str, "射程": int, "触及": int,
    "武器特性": list, "来源": str, "装备类型": str, "双手": bool,
}


class ActionError(ValueError):
    """动作不合法（找不到物品、战斗中换装、缺字段……）。"""


def _char_path(character: str) -> str:
    return f"/角色列表/{escape_token(character)}"


def _in_combat(loop: GameLoop) -> bool:
    return bool(getattr(loop, "initiative", None))


def _budget_hint(state: StateTree, base: str, item: dict,
                 extra: dict | None = None) -> str:
    """造物时的提示：这件单件就超过当前等级预算 → 先别指望能立刻穿。"""
    add = item_tier_total(item, extra)
    if not add:
        return ""
    try:
        level = sum(int(v) for v in (state.read(f"{base}/职业") or {}).values())
    except (TypeError, ValueError):
        level = 1
    level = level or 1
    limit = character_budget(level)
    if add > limit:
        return (f"（⚠️ 等级 {level} 的品级上限 {limit}，这件 {add}："
                f"升级或换掉别的词条装备后才能穿）")
    return ""


def _effect_targets(loop: GameLoop, who) -> list:
    """网页修正的"给谁"：全队 / 敌人 / 全场 / 某个名字或 id。"""
    w = str(who or "").strip()
    if w in ("全队", "party"):
        return [u for u in loop.field if u.team in ("pc", "ally")]
    if w in ("敌人", "enemy"):
        return [u for u in loop.field if u.team == "enemy" and u.alive]
    if w in ("全场", "所有人", "all"):
        return [u for u in loop.field if u.alive]
    u = loop.find_unit(w) if w else None
    return [u] if u is not None else []


def _toggle_bool(state: StateTree, path: str, *, actor: str,
                 reason: str) -> bool:
    cur = state.read(path)
    if not isinstance(cur, bool):
        raise ActionError(f"{path} 当前不是开关值（{cur!r}）")
    new = not cur
    state.commit([{"op": "replace", "path": path, "value": new}],
                 actor=actor, reason=reason)
    return new


def _resync_pc_units(loop: GameLoop, state: StateTree, character: str) -> None:
    """回滚后把内存里的 PC 单位拉回"状态树这一份真相"。

    网页动作里有几处会顺手改 `unit.hp/ac`（喝药、换甲）；如果动作在**改完之后**
    才失败，事务会把状态树还原，但内存对象还留着新值 —— 这里补一刀。
    """
    for u in getattr(loop, "field", []) or []:
        if getattr(u, "team", "") != "pc":
            continue
        name = character if (u.name == character or u.id == character) else u.name
        if not state.has_character(name):
            continue
        cur, mx, _tmp = state.hp(name)
        u.hp, u.hp_max = int(cur), int(mx)
        ac = state.read(f"{_char_path(name)}/护甲等级")
        if ac is not None:
            u.ac = int(ac)


def _rebuild_pc_unit(loop: GameLoop, state: StateTree, character: str) -> None:
    """把 PC 的**动作列表**按最新装备重造；只改内存单位，状态树不动。

    换武器、DM 改武器数值后要立刻生效；战斗中不改（动作经济先锁住）。
    """
    if _in_combat(loop):
        return
    from .rules.loop import make_hero_unit
    old = next((u for u in (loop.field or [])
                if u.name == character or u.id == character), None)
    if old is None:
        return
    new = make_hero_unit(state, character, team=old.team, uid=old.id)
    # **原地更新**旧 Unit：外部还拿着同一个对象（测试/界面/战斗引用），
    # 换对象会让这些引用停在旧动作列表上。
    old.actions = new.actions
    old.spells = new.spells
    old.spell_slots = new.spell_slots
    old.cast_ability = new.cast_ability
    old.notes = new.notes
    # H3：装备/词条改的是"身上挂什么"，重建后必须把这些一起换掉 ——
    # 只换 actions 会让刚穿上的斗篷特效留在旧对象上（等于没生效）。
    # 但**不能把场景/轮次修正也换成新单位的**：新单位只从状态树造，
    # 不知道本场刚挂的 party 修正（外审 P1-2）。
    keep_mods = [m for m in old.mods if m.get("scope") != "item"]
    item_mods = [m for m in new.mods if m.get("scope") == "item"]
    old.mods = keep_mods + item_mods
    old.item_effects = new.item_effects
    # 充能：保留旧对象里同名条目的当前值；新出现的特效按新单位的满额走。
    old.item_charges = {
        key: (dict(old.item_charges[key]) if key in old.item_charges else dict(rec))
        for key, rec in new.item_charges.items()
    }
    if new.hp_max != old.hp_max:
        old.hp_max = new.hp_max
        old.hp = min(old.hp, old.hp_max)


def _refresh_pc_actions(loop: GameLoop, state: StateTree, character: str) -> None:
    """DM 造/改技能后刷新动作列表。

    只换 `actions` 与 `notes`，不碰法术位/HP/装备 —— 所以**战斗中也能用**：
    DM 现造一个技能，下一次生成菜单就能挑到；已经花掉的资源不受影响。
    """
    old = next((u for u in (loop.field or [])
                if u.name == character or u.id == character), None)
    if old is None:
        return
    from .rules.loop import make_hero_unit
    new = make_hero_unit(state, character, team=old.team, uid=old.id)
    old.actions = new.actions
    old.notes = new.notes


def apply_action(state: StateTree, loop: GameLoop, character: str,
                 payload: dict) -> dict:
    """执行一个网页动作。成功返回 `{ok: True, message, value}`。

    **动作是一笔事务**：块内抛异常 → 状态树回到动作前（记一条审计），
    再把错误交给调用方；不会留下"钱扣了货没给"这种半截状态。
    """
    if not isinstance(payload, dict):
        raise ActionError("动作必须是 JSON 对象")
    kind = str(payload.get("kind") or "").strip()
    if not character:
        raise ActionError("没有当前角色")
    try:
        with state.transaction(actor=f"玩家:{character}",
                               reason=f"网页动作 {kind or '?'}"):
            return _apply_action(state, loop, character, payload)
    except Exception as exc:                      # noqa: BLE001
        _resync_pc_units(loop, state, character)
        if isinstance(exc, ActionError):
            raise
        raise ActionError(f"这一步没生效（已回滚）：{exc}") from exc


def _apply_action(state: StateTree, loop: GameLoop, character: str,
                  payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ActionError("动作必须是 JSON 对象")
    kind = str(payload.get("kind") or "").strip()
    if not character:
        raise ActionError("没有当前角色")
    base = _char_path(character)
    actor = f"玩家:{character}"

    if kind == "dm_create_item":
        if _in_combat(loop):
            raise ActionError("战斗中不能往背包里造物（先结束战斗）")
        name = str(payload.get("name") or "").strip()
        category = str(payload.get("category") or "物品").strip()
        if not name:
            raise ActionError("造物缺少名称")
        item_path = f"{base}/物品/{escape_token(name)}"
        if state.read(item_path) is not None:
            raise ActionError(f"背包里已经有「{name}」")
        item: dict = {"类别": category, "已装备": False, "来源": "DM"}
        lore_fields: dict = {}
        for key in ("描述", "机制", "背景"):
            val = str(payload.get(key) or "").strip()
            if val:
                lore_fields[key] = val
        for key, typ in _ITEM_STAT_KEYS.items():
            if key == "来源" or key not in payload:
                continue
            val = payload.get(key)
            if val in (None, ""):
                continue
            if typ is int:
                try:
                    item[key] = int(val)
                except (TypeError, ValueError) as exc:
                    raise ActionError(f"{key} 必须是整数（拿到 {val!r}）") from exc
            elif typ is list:
                if isinstance(val, str):
                    val = [x.strip() for x in val.replace("，", ",").split(",")]
                item[key] = [str(x) for x in (val or []) if str(x).strip()]
            elif typ is bool:
                if isinstance(val, str):
                    val = val.strip().lower() in ("1", "true", "是", "yes", "on")
                item[key] = bool(val)
            else:
                item[key] = str(val).strip()
        if "词条" in payload:
            ids, errs = validate_item_entries(payload.get("词条"),
                                                   loop.world.entries())
            if errs:
                raise ActionError("词条：" + "；".join(errs))
            if ids:
                item["词条"] = ids
        if "特效" in payload:
            try:
                effects = coerce_item_effects(payload.get("特效"))
            except ValueError as exc:
                raise ActionError(f"特效：{exc}") from exc
            if effects:
                item["特效"] = effects
        state.commit([{"op": "insert", "path": item_path, "value": item}],
                     actor="DM", reason=f"DM 造物「{name}」")
        if lore_fields:
            loop.world.set_lore_fields("物品", name, lore_fields,
                                       actor="DM", reason=f"DM 设定「{name}」")
        return {"ok": True, "value": item,
                "message": f"DM 造物「{name}」已放进背包；可装备后按引擎结算"
                           + (f"；词条：{summarize(item.get('词条'))}"
                              if item.get("词条") else "")
                           + _budget_hint(state, base, item, loop.world.entries())}

    if kind == "set_item_stats":
        if _in_combat(loop):
            raise ActionError("战斗中不能改物品数值（先结束战斗）")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("缺少物品名")
        item_path = f"{base}/物品/{escape_token(name)}"
        item = state.read(item_path)
        if not isinstance(item, dict):
            raise ActionError(f"背包里没有「{name}」")
        patches = []
        new_ids: list[str] | None = None
        for key, typ in _ITEM_STAT_KEYS.items():
            if key not in payload:
                continue
            val = payload.get(key)
            child = f"{item_path}/{escape_token(key)}"
            if val in (None, ""):
                if item.get(key) is not None:
                    patches.append({"op": "remove", "path": child})
                continue
            if typ is int:
                try:
                    val = int(val)
                except (TypeError, ValueError) as exc:
                    raise ActionError(f"{key} 必须是整数（拿到 {val!r}）") from exc
            elif typ is list:
                if isinstance(val, str):
                    val = [x.strip() for x in val.replace("，", ",").split(",")]
                val = [str(x) for x in (val or []) if str(x).strip()]
            elif typ is bool:
                if isinstance(val, str):
                    val = val.strip().lower() in ("1", "true", "是", "yes", "on")
                val = bool(val)
            else:
                val = str(val).strip()
            patches.append({"op": "replace" if item.get(key) is not None
                            else "insert", "path": child, "value": val})
        if "词条" in payload:
            ids, errs = validate_item_entries(payload.get("词条"),
                                                   loop.world.entries())
            if errs:
                raise ActionError("词条：" + "；".join(errs))
            new_ids = ids
            child = f"{item_path}/词条"
            if ids:
                patches.append({"op": "replace" if item.get("词条") is not None
                                else "insert", "path": child, "value": ids})
            elif item.get("词条") is not None:
                patches.append({"op": "remove", "path": child})
        if "特效" in payload:
            try:
                effects = coerce_item_effects(payload.get("特效"))
            except ValueError as exc:
                raise ActionError(f"特效：{exc}") from exc
            child = f"{item_path}/特效"
            if effects:
                patches.append({"op": "replace" if item.get("特效") is not None
                                else "insert", "path": child,
                                "value": effects})
            elif item.get("特效") is not None:
                patches.append({"op": "remove", "path": child})
        # 已装备物品改词条：单件校验过了，还要复核角色总预算（外审 P1-5）
        if new_ids is not None and item.get("已装备"):
            try:
                level = sum(int(v) for v in
                            (state.read(f"{base}/职业") or {}).values())
            except (TypeError, ValueError):
                level = 1
            candidate = dict(item)
            candidate["词条"] = new_ids
            budget_err = check_equip_budget(
                state.read(f"{base}/物品") or {}, name, candidate, level or 1,
                loop.world.entries())
            if budget_err:
                raise ActionError(budget_err)
        if not patches:
            return {"ok": True, "message": "没有变化"}
        state.commit(patches, actor="DM", reason=f"DM 改物品数值「{name}」")
        if str(item.get("类别") or "") in ("护甲", "armor", "盾牌", "shield"):
            apply_armor_class(state, character, actor="DM",
                              reason=f"DM 改「{name}」后重算 AC")
        _rebuild_pc_unit(loop, state, character)
        return {"ok": True, "message": f"「{name}」的引擎数值已更新"}

    if kind in ("dm_create_action", "set_action_stats"):
        # 造/改 DM 技能（H1）。**写入即校验**：写不坏的字段才进引擎；
        # 战斗中允许 —— 只换动作列表，不碰法术位/装备。
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("技能要有名字")
        try:
            spec = coerce_action_spec(payload)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        path = f"{base}/{ACTIONS_KEY}/{escape_token(name)}"
        exists = state.read(path) is not None
        if kind == "dm_create_action" and exists:
            raise ActionError(f"角色已经有「{name}」这个技能（改数值用「改技能」）")
        if kind == "set_action_stats" and not exists:
            raise ActionError(f"角色没有「{name}」这个技能（要新造用「造技能」）")
        lore_fields: dict = {}
        for key in ("机制", "背景"):
            val = str(payload.get(key) or "").strip()
            if val:
                lore_fields[key] = val
        patches = []
        if state.read(f"{base}/{ACTIONS_KEY}") is None:
            patches.append({"op": "insert", "path": f"{base}/{ACTIONS_KEY}",
                            "value": {}})
        patches.append({"op": "replace" if exists else "insert",
                        "path": path, "value": spec})
        state.commit(patches,
                     actor="DM",
                     reason=f"DM {'改' if exists else '造'}技能「{name}」")
        if lore_fields:
            loop.world.set_lore_fields("技能", name, lore_fields,
                                       actor="DM", reason=f"DM 设定技能「{name}」")
        _refresh_pc_actions(loop, state, character)
        return {"ok": True, "value": spec,
                "message": (f"「{name}」已写入当局：{describe_spec(spec)}；"
                            f"下一轮行动菜单可用")}

    if kind == "dm_set_npc_stats":
        # H2：给已登记（或同时登记）的人物写数值块。**人物原本只是叙事对象，
        # 有了数值块才能用自己的 AC/生命/攻击/豁免参与引擎结算。**
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("人物要有名字")
        try:
            stats = coerce_npc_stats(payload)
        except NpcError as exc:
            raise ActionError(str(exc)) from exc
        role = str(payload.get("身份") or "").strip()
        node = loop.world.npc(name)
        if node is None:
            try:
                loop.world.register_npc(name, role=role, actor="DM",
                                        reason=f"登记人物「{name}」")
            except WorldError as exc:
                raise ActionError(str(exc)) from exc
        elif role and str(node.get("身份") or "") != role:
            state.commit([{"op": "replace",
                           "path": f"/世界/角色/{escape_token(name)}/身份",
                           "value": role}],
                         actor="DM", reason=f"改人物「{name}」身份")
        try:
            loop.world.set_npc_stats(name, stats, actor="DM",
                                     reason=f"设置人物「{name}」数值")
        except WorldError as exc:
            raise ActionError(str(exc)) from exc
        in_field = any(u.name == name or u.id == name for u in loop.field)
        msg = f"「{name}」数值已写入：{npc_summary(stats)}"
        if in_field:
            msg += "；已在场上，本场不重算（下一场生效）"
        return {"ok": True, "value": stats, "message": msg}

    if kind == "dm_add_npc_to_combat":
        # H2：把带数值的人物放进当前战斗。先有战斗再加人，避免往探索桌里塞敌人。
        if not _in_combat(loop):
            raise ActionError("先进入战斗，再把人物放上场")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("缺少人物名")
        team = str(payload.get("team") or "enemy").strip()
        if team not in ("enemy", "ally"):
            raise ActionError("阵营只能是 enemy（敌人）或 ally（盟友）")
        pos = None
        if payload.get("x") not in (None, "") or payload.get("y") not in (None, ""):
            try:
                pos = (float(payload.get("x") or 0), float(payload.get("y") or 0))
            except (TypeError, ValueError) as exc:
                raise ActionError("站位 x/y 必须是数字") from exc
        try:
            u = loop.add_npc(name, team=team, position=pos)
        except (NpcError, ValueError) as exc:
            raise ActionError(str(exc)) from exc
        side = "敌人" if team == "enemy" else "盟友"
        return {"ok": True, "value": {"id": u.id, "hp": u.hp},
                "message": (f"「{name}」以{side}身份加入战斗："
                            f"AC {u.ac}｜HP {u.hp}/{u.hp_max}｜先攻 {u.initiative_bonus:+d}")}

    if kind in ("dm_apply_effect", "dm_clear_effects"):
        # H2b：临场临时修正。只改引擎判定用的加值，**不改**基础 AC/属性。
        who = payload.get("target") or "全队"
        targets = _effect_targets(loop, who)
        if not targets:
            raise ActionError(f"场上没有目标：{who!r}")
        scope = str(payload.get("scope") or payload.get("持续") or "").strip()
        if kind == "dm_clear_effects":
            removed = sum(clear_mods(u, scope=scope or None) for u in targets)
            tail = f"（只清 {scope}）" if scope else "（全部）"
            return {"ok": True, "value": {"removed": removed},
                    "message": f"清掉 {removed} 条临时修正{tail}"}
        spec = {}
        for k in ("name", "value", "rounds", "object",
                  "名称", "值", "轮数", "对象"):
            if k in payload:
                spec[k] = payload[k]
        # ⚠️ 不能直接用 payload["kind"] —— 它是网页动作名（dm_apply_effect），
        #    不是修正类型。修正类型走「类型」。
        if payload.get("类型"):
            spec["kind"] = payload["类型"]
        elif payload.get("effect_kind"):
            spec["kind"] = payload["effect_kind"]
        spec["scope"] = payload.get("scope") or payload.get("持续") or "scene"
        try:
            eff = coerce_effect(spec)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        for u in targets:
            add_mod(u, eff, source="DM")
        return {"ok": True, "value": eff,
                "message": (f"给 {'、'.join(u.name for u in targets)} 挂上："
                            f"{describe_one(eff)}")}

    if kind == "dm_create_entry":
        # H3：DM 自写词条（只在本战役有效）。**仍然只能用引擎承认的词汇**。
        eid = str(payload.get("id") or "").strip()
        if (len(eid) < 2 or len(eid) > 40
                or not eid.isascii() or eid != eid.lower()
                or not all(ch.isalnum() or ch == "_" for ch in eid)):
            raise ActionError("词条 id 只能用小写字母/数字/下划线，2~40 字符"
                             "（例：x_life_blessing）")
        if any(str(e.get("id")) == eid for e in BUILTIN_ENTRIES):
            raise ActionError(f"「{eid}」是内置词条，换一个 id"
                             f"（建议 x_ 开头）")
        spec = {k: payload[k] for k in
                ("名称", "品级", "适用", "触发", "效果", "属性", "数值",
                 "保留生命", "每场", "冷却", "说明") if k in payload}
        try:
            entry = validate_entry(spec, allow_id=eid)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        try:
            loop.world.register_entry(eid, entry, actor="DM",
                                      reason=f"DM 写词条「{entry['名称']}」")
        except WorldError as exc:
            raise ActionError(str(exc)) from exc
        tier = TIER_NAMES.get(int(entry.get("品级") or 1), "?")
        return {"ok": True, "value": entry,
                "message": (f"词条「{entry['名称']}」（{tier}）已写入本战役；"
                            f"id={eid}，物品/人物可直接选它")}

    if kind == "dm_set_npc_entries":
        # H3：生物词条（如坐骑的「千里之足」）
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("缺少人物名")
        node = loop.world.npc(name)
        if node is None:
            raise ActionError(f"世界树里没有人物「{name}」")
        extra = loop.world.entries()
        ids = payload.get("词条") or []
        clean: list[str] = []
        errors: list[str] = []
        for raw in ids:
            eid = str(raw or "").strip()
            entry = get_entry(eid, extra)
            if entry is None:
                errors.append(f"没有这个词条：{eid!r}")
                continue
            if entry.get("适用") == "装备":
                errors.append(f"「{entry.get('名称')}」是装备词条，不能挂在生物上")
                continue
            if eid in clean:
                errors.append(f"词条重复：{eid}")
                continue
            clean.append(eid)
        if errors:
            raise ActionError("；".join(errors))
        path = f"/世界/角色/{escape_token(name)}/词条"
        op = "replace" if node.get("词条") is not None else "insert"
        state.commit([{"op": op, "path": path, "value": clean}],
                     actor="DM", reason=f"设置人物「{name}」生物词条")
        in_field = any(u.name == name or u.id == name for u in loop.field)
        msg = f"「{name}」生物词条已更新：{summarize(clean, extra)}"
        if in_field:
            msg += "；已在场上，本场不重算（下一场生效）"
        return {"ok": True, "value": clean, "message": msg}

    if kind == "equip_item":
        if _in_combat(loop):
            raise ActionError("战斗中不能换装备（先结束战斗）")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("缺少物品名")
        item_path = f"{base}/物品/{escape_token(name)}"
        item = state.read(item_path)
        if not isinstance(item, dict):
            raise ActionError(f"背包里没有「{name}」")
        cat = str(item.get("类别") or "").lower()
        currently = bool(item.get("已装备"))
        action_cn = "卸下" if currently else "装备"
        items = state.read(f"{base}/物品") or {}

        if currently:
            _toggle_bool(state, f"{item_path}/已装备", actor=actor,
                         reason=f"卸下「{name}」")
            if str(item.get("装备栏") or "") == "双手":
                state.commit([{"op": "remove", "path": f"{item_path}/装备栏"}],
                             actor=actor, reason=f"卸下「{name}」")
            ac = None
            if cat in ("护甲", "armor", "盾牌", "shield", "饰品", "accessory"):
                ac, _parts, warnings = apply_armor_class(
                    state, character, actor=actor,
                    reason=f"卸下「{name}」后重算 AC")
                unit = next((u for u in loop.field
                             if u.name == character or u.id == character), None)
                if unit is not None and ac is not None:
                    unit.ac = int(ac)
            _rebuild_pc_unit(loop, state, character)
            msg = f"「{name}」已卸下"
            return {"ok": True, "value": False, "ac": ac, "message": msg}

        if cat in ("护甲", "armor", "盾牌", "shield"):
            rec = homebrew_armor_profile(item) or armor_profile(name)
            if rec is None:
                raise ActionError(f"SRD 护甲表里没有「{name}」，"
                                  f"也没有写「护甲加值」，不能算 AC")
        # 同栏旧装备自动让位（上装/头盔/下装/靴子）；手部不自动抢，
        # 避免把"换甲"变成"把武器顶掉"。
        hint = slot_of(item, name)
        if hint in ("上装", "头盔", "下装", "靴子"):
            displaced = []
            for other, ov in (items or {}).items():
                if other == name or not isinstance(ov, dict) or not ov.get("已装备"):
                    continue
                if slot_of(ov, other) == hint:
                    displaced.append({"op": "replace",
                                      "path": f"{base}/物品/{escape_token(other)}/已装备",
                                      "value": False})
            if displaced:
                state.commit(displaced, actor=actor, reason=f"换下 {hint} 旧装备")
                items = state.read(f"{base}/物品") or {}

        slot, errors = validate_equip(items, name, item,
                                       target=payload.get("slot"))
        if errors:
            raise ActionError("；".join(errors))
        # H3 预算闸：等级低的角色穿不上满身传说（词条品级合计）
        try:
            level = sum(int(v) for v in (state.read(f"{base}/职业") or {}).values())
        except (TypeError, ValueError):
            level = 1
        budget_err = check_equip_budget(items, name, item, level or 1,
                                              loop.world.entries())
        if budget_err:
            raise ActionError(budget_err)
        if item.get("已装备") is None:
            state.commit([{"op": "insert", "path": f"{item_path}/已装备",
                           "value": False}], actor=actor,
                         reason=f"装备「{name}」初始化")
        old_ac = int(state.read(f"{base}/护甲等级") or 10)
        _toggle_bool(state, f"{item_path}/已装备", actor=actor,
                     reason=f"装备「{name}」")
        if slot == DAILY_SLOT:
            state.commit([{"op": "replace" if item.get("装备类型") is not None
                           else "insert", "path": f"{item_path}/装备类型",
                           "value": "日常"}], actor=actor,
                         reason=f"装备「{name}」")
        else:
            state.commit([{"op": "replace" if item.get("装备栏") is not None
                           else "insert", "path": f"{item_path}/装备栏",
                           "value": slot}], actor=actor,
                         reason=f"装备「{name}」到 {slot}")
        ac = None
        warnings: list[str] = []
        if cat in ("护甲", "armor", "盾牌", "shield", "饰品", "accessory"):
            ac, _parts, warnings = apply_armor_class(
                state, character, actor=actor,
                reason=f"装备「{name}」后重算 AC")
            unit = next((u for u in loop.field
                         if u.name == character or u.id == character), None)
            if unit is not None and ac is not None:
                unit.ac = int(ac)
        if slot != DAILY_SLOT:
            _rebuild_pc_unit(loop, state, character)
        msg = f"「{name}」已装备到 {slot or '日常'}"
        if ac is not None:
            msg += f"；AC {old_ac} → {ac}"
        if warnings:
            msg += "｜⚠️ " + "；".join(warnings)
        return {"ok": True, "value": True, "slot": slot, "ac": ac,
                "warnings": warnings, "message": msg}

    if kind == "prepare_spell":
        if _in_combat(loop):
            raise ActionError("战斗中不能改准备法术（先结束战斗）")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("缺少法术名")
        spell_path = f"{base}/施法/法术书/{escape_token(name)}"
        spell = state.read(spell_path)
        if not isinstance(spell, dict):
            raise ActionError(f"法术书里没有「{name}」")
        new = _toggle_bool(state, f"{spell_path}/准备中",
                           actor=actor,
                           reason=f"{'准备' if not spell.get('准备中') else '取消准备'}「{name}」")
        return {"ok": True, "value": new,
                "message": f"「{name}」已{'准备' if new else '取消准备'}"}

    if kind == "use_item":
        if _in_combat(loop):
            raise ActionError("战斗中喝药要用附赠动作 —— 点底部技能栏里的「喝药」选项（背包按钮只在战斗外可用）")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("缺少物品名")
        try:
            out = use_consumable(state, character, name, rng=loop.rng,
                                 actor=actor, reason=f"喝下「{name}」")
        except EconomyError as exc:
            raise ActionError(str(exc)) from exc
        unit = next((u for u in loop.field
                     if (u.name == character or u.id == character) and u.alive),
                    None)
        if unit is not None:
            unit.hp = int(out["hp"])          # 与 stat_data 保持同一份真相
        msg = (f"喝下「{name}」：治疗 {out['formula']} → 回 {out['healed']} 点"
               f"（{out['hp']}/{out['hp_max']}）")
        if out["left"] == 0:
            msg += "，这瓶用完了"
        else:
            msg += f"，还剩 {out['left']} 个"
        if out.get("浪费"):
            msg += f"；超过上限，浪费 {out['浪费']} 点"
        return {"ok": True, "value": out, "message": msg}

    if kind in ("buy_item", "sell_item"):
        if _in_combat(loop):
            raise ActionError("战斗中不能交易（先结束战斗）")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ActionError("缺少物品名")
        try:
            qty = int(payload.get("qty") or 1)
        except (TypeError, ValueError) as exc:
            raise ActionError("数量必须是整数") from exc
        try:
            if not shop_stock(state):
                raise ActionError("这里没有可交易的地方（世界树没给这个地点货架）")
            if kind == "buy_item":
                out = buy(state, character, name, qty=qty, actor=actor,
                          reason=f"买入「{name}」")
                msg = (f"买到「{name}」×{out['qty']}，花 {out['paid']} 金币，"
                       f"余额 {out['left_gold']}")
            else:
                out = sell(state, character, name, qty=qty, actor=actor,
                           reason=f"卖出「{name}」")
                msg = (f"卖出「{name}」×{qty}，得 {out['earned']} 金币，"
                       f"余额 {out['gold']}")
        except EconomyError as exc:
            raise ActionError(str(exc)) from exc
        return {"ok": True, "value": out, "message": msg}

    if kind == "travel":
        if _in_combat(loop):
            raise ActionError("战斗中不能赶路（先结束战斗）")
        to = str(payload.get("to") or payload.get("name") or "").strip()
        try:
            World(state).move_to(to, actor=actor,
                                 reason=f"前往「{to}」")
        except WorldError as exc:
            raise ActionError(str(exc)) from exc
        return {"ok": True, "value": {"to": to},
                "message": f"你们沿已知的路走到了「{to}」"}

    raise ActionError(f"不认识的动作：{kind!r}")

