# coding=utf-8
"""开局人生路径问卷：8 问 → 一张可继续编辑的角色卡。

只做“把答案变成建卡器的选择”，不直接改状态树、不调模型。
答案里的职业/种族倾向都是软权重，最终仍由 `create_character` 校验。
"""
from __future__ import annotations

from engine.creation import BASE_CLASSES, SKILL_CHOICES, SKILLS, spells_for

__all__ = ["QUESTIONS", "LifepathError", "questions", "build"]


class LifepathError(ValueError):
    pass


#: 每题 4 个选项；class/species 是软权重，skills 是硬候选。
QUESTIONS: list[dict] = [
    {
        "id": "origin", "q": "你的童年主要在哪里度过？",
        "options": [
            {"id": "frontier", "label": "边境村庄，风雪和巡逻队是日常",
             "class": {"Ranger": 2, "Fighter": 1, "Druid": 1},
             "species": {"Human": 1, "Elf": 1}, "skills": ["survival", "perception"]},
            {"id": "city", "label": "城墙内的小巷，靠跑腿和耳语长大",
             "class": {"Rogue": 2, "Bard": 1, "Wizard": 1},
             "species": {"Human": 1, "Halfling": 1}, "skills": ["stealth", "persuasion"]},
            {"id": "academy", "label": "书库与讲堂，问题比答案多",
             "class": {"Wizard": 2, "Cleric": 1, "Bard": 1},
             "species": {"Elf": 2, "Gnome": 1}, "skills": ["arcana", "history"]},
            {"id": "temple", "label": "神庙庭院，钟声和药草味",
             "class": {"Cleric": 2, "Paladin": 1, "Monk": 1},
             "species": {"Human": 1, "Dwarf": 1}, "skills": ["religion", "insight"]},
        ],
    },
    {
        "id": "training", "q": "你最早接受的训练是什么？",
        "options": [
            {"id": "army", "label": "军团操练，盾墙与号令",
             "class": {"Fighter": 2, "Paladin": 1, "Ranger": 1},
             "species": {"Dwarf": 1, "Human": 1}, "skills": ["athletics", "perception"]},
            {"id": "guild", "label": "行会学徒，锁、账本和暗门",
             "class": {"Rogue": 2, "Bard": 1, "Ranger": 1},
             "species": {"Halfling": 1}, "skills": ["stealth", "sleight_of_hand"]},
            {"id": "college", "label": "学院课程，符文、历史和辩论",
             "class": {"Wizard": 2, "Bard": 1, "Sorcerer": 1},
             "species": {"Gnome": 1, "Elf": 1}, "skills": ["arcana", "investigation"]},
            {"id": "monastery", "label": "寺院修行，呼吸、拳脚与静默",
             "class": {"Monk": 2, "Cleric": 1, "Druid": 1},
             "species": {"Goliath": 1, "Human": 1}, "skills": ["insight", "medicine"]},
        ],
    },
    {
        "id": "turn", "q": "什么把你推上了冒险者的路？",
        "options": [
            {"id": "war", "label": "一场战争，把家乡变成了地图上的灰",
             "class": {"Fighter": 2, "Paladin": 1, "Warlock": 1},
             "species": {"Orc": 1, "Human": 1}, "skills": ["athletics", "intimidation"]},
            {"id": "plague", "label": "一场瘟疫，你送走了太多人",
             "class": {"Cleric": 2, "Druid": 1, "Ranger": 1},
             "species": {"Human": 1, "Elf": 1}, "skills": ["medicine", "nature"]},
            {"id": "betrayal", "label": "一次背叛，你不再相信递来的手",
             "class": {"Rogue": 2, "Warlock": 1, "Bard": 1},
             "species": {"Tiefling": 1, "Human": 1}, "skills": ["deception", "insight"]},
            {"id": "ruin", "label": "一座遗迹，你在里面看见了不该看的东西",
             "class": {"Wizard": 2, "Warlock": 1, "Druid": 1},
             "species": {"Dragonborn": 1, "Tiefling": 1}, "skills": ["investigation", "history"]},
        ],
    },
    {
        "id": "temper", "q": "同伴最常说你是个怎样的人？",
        "options": [
            {"id": "protect", "label": "嘴硬，但总把别人挡在身后",
             "class": {"Paladin": 2, "Fighter": 1, "Cleric": 1},
             "species": {"Dwarf": 1, "Human": 1}, "skills": ["persuasion", "insight"]},
            {"id": "free", "label": "坐不住，规矩越多越想翻墙",
             "class": {"Bard": 2, "Rogue": 1, "Ranger": 1},
             "species": {"Halfling": 1, "Elf": 1}, "skills": ["performance", "acrobatics"]},
            {"id": "truth", "label": "较真，宁可得罪人也要把事弄明白",
             "class": {"Wizard": 2, "Cleric": 1, "Monk": 1},
             "species": {"Gnome": 1, "Elf": 1}, "skills": ["investigation", "arcana"]},
            {"id": "power", "label": "不甘心只当棋子，想自己定规则",
             "class": {"Warlock": 2, "Sorcerer": 1, "Barbarian": 1},
             "species": {"Tiefling": 1, "Orc": 1}, "skills": ["intimidation", "arcana"]},
        ],
    },
    {
        "id": "weapon", "q": "真打起来，你最先摸向什么？",
        "options": [
            {"id": "heavy", "label": "重武器或盾，站住位置再说",
             "class": {"Fighter": 2, "Paladin": 1, "Barbarian": 1},
             "species": {"Dwarf": 1, "Goliath": 1}, "skills": ["athletics", "intimidation"]},
            {"id": "bow", "label": "弓、弩或标枪，先看距离",
             "class": {"Ranger": 2, "Rogue": 1, "Fighter": 1},
             "species": {"Elf": 1, "Halfling": 1}, "skills": ["perception", "stealth"]},
            {"id": "blade", "label": "轻刃、拳脚或匕首，近身才安心",
             "class": {"Rogue": 2, "Monk": 1, "Bard": 1},
             "species": {"Human": 1, "Tiefling": 1}, "skills": ["acrobatics", "stealth"]},
            {"id": "magic", "label": "法术、祷言或契约，不靠肌肉",
             "class": {"Wizard": 2, "Sorcerer": 1, "Warlock": 1},
             "species": {"Gnome": 1, "Dragonborn": 1}, "skills": ["arcana", "investigation"]},
        ],
    },
    {
        "id": "role", "q": "队伍里你最自然的位置是？",
        "options": [
            {"id": "leader", "label": "拿主意的人，哪怕代价难听",
             "class": {"Paladin": 2, "Bard": 1, "Fighter": 1},
             "species": {"Human": 1, "Dragonborn": 1}, "skills": ["persuasion", "insight"]},
            {"id": "scout", "label": "先走一步的人，替大家看路",
             "class": {"Ranger": 2, "Rogue": 1, "Monk": 1},
             "species": {"Elf": 1, "Halfling": 1}, "skills": ["stealth", "survival"]},
            {"id": "healer", "label": "收拾伤口、安抚人心的人",
             "class": {"Cleric": 2, "Druid": 1, "Paladin": 1},
             "species": {"Human": 1, "Dwarf": 1}, "skills": ["medicine", "religion"]},
            {"id": "trickster", "label": "让敌人猜错的人，嘴上也不吃亏",
             "class": {"Bard": 2, "Rogue": 1, "Warlock": 1},
             "species": {"Tiefling": 1, "Gnome": 1}, "skills": ["deception", "performance"]},
        ],
    },
    {
        "id": "motive", "q": "你真正想从这段旅途里得到什么？",
        "options": [
            {"id": "revenge", "label": "把欠我的那笔账收回来",
             "class": {"Barbarian": 2, "Rogue": 1, "Warlock": 1},
             "species": {"Orc": 1, "Tiefling": 1}, "skills": ["intimidation", "survival"]},
            {"id": "home", "label": "挣到能回去、也能护住家的东西",
             "class": {"Fighter": 2, "Paladin": 1, "Druid": 1},
             "species": {"Dwarf": 1, "Halfling": 1}, "skills": ["athletics", "nature"]},
            {"id": "knowledge", "label": "弄清一个不该被弄清的答案",
             "class": {"Wizard": 2, "Bard": 1, "Cleric": 1},
             "species": {"Gnome": 1, "Elf": 1}, "skills": ["arcana", "history"]},
            {"id": "redemption", "label": "把过去做错的事补回来",
             "class": {"Paladin": 2, "Cleric": 1, "Monk": 1},
             "species": {"Human": 1, "Dragonborn": 1}, "skills": ["religion", "persuasion"]},
        ],
    },
    {
        "id": "fear", "q": "夜里最让你睡不着的是什么？",
        "options": [
            {"id": "failure", "label": "关键时刻我又慢了一步",
             "class": {"Monk": 2, "Paladin": 1, "Fighter": 1},
             "species": {"Human": 1, "Dwarf": 1}, "skills": ["insight", "athletics"]},
            {"id": "loss", "label": "身边的人一个个离开，我却习惯不了",
             "class": {"Cleric": 2, "Druid": 1, "Bard": 1},
             "species": {"Halfling": 1, "Human": 1}, "skills": ["medicine", "persuasion"]},
            {"id": "unknown", "label": "黑暗里有什么在看着我，而我叫不出名字",
             "class": {"Wizard": 2, "Warlock": 1, "Ranger": 1},
             "species": {"Gnome": 1, "Tiefling": 1}, "skills": ["investigation", "arcana"]},
            {"id": "cage", "label": "被关进一个我逃不出去的地方",
             "class": {"Rogue": 2, "Barbarian": 1, "Sorcerer": 1},
             "species": {"Orc": 1, "Tiefling": 1}, "skills": ["acrobatics", "stealth"]},
        ],
    },
]


def questions() -> list[dict]:
    """给网页的题面：不暴露权重，只给题和选项。"""
    return [{"id": q["id"], "q": q["q"],
             "options": [{"id": o["id"], "label": o["label"]} for o in q["options"]]}
            for q in QUESTIONS]


def _order(cls: str) -> int:
    return BASE_CLASSES.index(cls) if cls in BASE_CLASSES else 99


def build(answers, *, name: str = "") -> dict:
    """answers: {question_id: option_id}；返回建卡器 payload + 人生摘要。"""
    if not isinstance(answers, dict):
        raise LifepathError("答案必须是对象")
    cls_score: dict[str, int] = {}
    sp_score: dict[str, int] = {}
    skill_score: dict[str, int] = {}
    picked: list[dict] = []
    for q in QUESTIONS:
        oid = str(answers.get(q["id"]) or "")
        opt = next((o for o in q["options"] if o["id"] == oid), None)
        if opt is None:
            raise LifepathError(f"第 {QUESTIONS.index(q) + 1} 题还没选")
        picked.append({"q": q["q"], "a": opt["label"]})
        for k, v in (opt.get("class") or {}).items():
            cls_score[k] = cls_score.get(k, 0) + int(v)
        for k, v in (opt.get("species") or {}).items():
            sp_score[k] = sp_score.get(k, 0) + int(v)
        for s in opt.get("skills") or []:
            skill_score[s] = skill_score.get(s, 0) + 1
    if not cls_score or not sp_score:
        raise LifepathError("答案没有产生职业/种族倾向")
    klass = max(cls_score, key=lambda k: (cls_score[k], -_order(k)))
    species = max(sp_score, key=lambda k: (sp_score[k], k))
    n_skills = SKILL_CHOICES.get(klass, 2)
    skills = sorted(skill_score, key=lambda s: (-skill_score[s], s))[:n_skills]
    skills = [s for s in skills if s in SKILLS]
    if len(skills) < n_skills:                    # 题面保证不会发生；兜底
        for s in sorted(SKILLS):
            if s not in skills:
                skills.append(s)
            if len(skills) >= n_skills:
                break
    spells = []
    try:
        if klass not in ("Paladin", "Ranger"):   # 半施法 v1 不自动配法术位
            for s in spells_for(klass, max_level=1)[:2]:
                spells.append(s["name"])
    except Exception:                             # noqa: BLE001
        spells = []
    payload = {
        "name": str(name or "无名冒险者").strip()[:20],
        "klass": klass, "species": species, "level": 3,
        "ability_mode": "suggest", "seed": 7,
        "skills": skills, "spells": spells,
    }
    return {"payload": payload,
            "summary": "；".join(f"{x['q']}→{x['a']}" for x in picked)}