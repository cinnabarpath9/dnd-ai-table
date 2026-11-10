# coding=utf-8
"""剧本锻造台：模板/小说 → 草稿 → 校验 → 试跑。"""
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import AdventureError, parse_adventure  # noqa: E402
from tools.forge.contract import doctor, to_adventure, write_adventure  # noqa: E402
from tools.forge.from_markdown import parse_markdown  # noqa: E402
from tools.forge.from_novel import draft_from_text  # noqa: E402
from tools.forge.cards import (  # noqa: E402
    draft_from_card, load_card, load_card_bytes, load_worldbook,
    merge_entries, suggest_clues, worldbook_entries)
from tools.forge.serve import convert  # noqa: E402

TEMPLATE = os.path.join(ROOT, "tools", "forge", "template.md")

NOVEL = """第一章 渡口的雾

天光刚亮，渡口上还浮着雾。布兰站在钟绳下，看见绳上挂着一截黑蜡。
他伸手去摸，蜡里混着河沙。艾尔玛蹲在缆桩旁，断口有两道刀痕。

第二章 闸房

闸房里堆着湿麻袋，墙角有拖痕。几只巨鼠正围着麻袋撕咬，听见脚步也不躲。
"""


COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    assert cond, f"{label}  extra={extra!r}"


def test_template_parses():
    with open(TEMPLATE, encoding="utf-8") as f:
        draft = parse_markdown(f.read())
    ok(draft["title"].startswith("雾渡"), "模板块标题", draft["title"])
    ok(len(draft["scenes"]) == 2, "模板切出两场", [s["id"] for s in draft["scenes"]])
    s2 = draft["scenes"][1]
    ok(s2["combat"]["enemies"] == [["Bandit", 2], ["Giant Rat", 3]],
       "战斗行解析成敌人表", s2["combat"])
    ok(s2["combat"]["distance"] == 30, "战斗距离", s2["combat"])


def test_template_valid():
    with open(TEMPLATE, encoding="utf-8") as f:
        draft = parse_markdown(f.read())
    report = doctor(draft)
    ok(report["ok"], "模板产出能跑", report["errors"])
    ok(report["stats"]["scenes"] == 2, "规模统计", report["stats"])
    adv = parse_adventure(to_adventure(draft))
    ok(adv.title and len(adv.scenes) == 2, "to_adventure 产物可解析")


def test_missing_brief_reported():
    draft = {
        "title": "缺简报的一本",
        "scenes": [{
            "id": "s1", "title": "空地",
            "situation": "空地上什么都没有，只有一面倒了半截的木牌。",
            "brief": {"present": "", "span": "一会", "offstage": "没人"},
            "clues": [{"text": "木牌背面有刻痕", "skill": "investigation",
                       "dc": 10, "source": "刻痕很新"}],
            "promises": [], "world": {"location": "空地"},
        }],
    }
    report = doctor(draft)
    ok(not report["ok"], "缺『在场』必须报错", report["errors"])
    ok(any("在场" in e for e in report["errors"]), "错误点名在场", report["errors"])


def test_write_refuses_invalid():
    draft = {"title": "", "scenes": []}
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "x.json")
        try:
            write_adventure(draft, target)
            raised = False
        except AdventureError:
            raised = True
        ok(raised and not os.path.exists(target), "不合格的剧本不落盘")


def test_novel_draft():
    draft = draft_from_text(NOVEL, title="渡口的雾", source="unit.txt")
    ok(len(draft["scenes"]) == 2, "按章切两场", [s["id"] for s in draft["scenes"]])
    first = draft["scenes"][0]
    ok("黑蜡" in first["situation"], "局面取自原文（不改写）", first["situation"])
    ok(first["brief"]["present"] == "", "简报留空（等人/模型补）")
    report = doctor(draft)
    ok(not report["ok"], "小说粗稿必然还不完整", report["errors"][:2])


def test_playtest_runs():
    from tools.forge.cli import main as cli_main
    with open(TEMPLATE, encoding="utf-8") as f:
        draft = parse_markdown(f.read())
    with tempfile.TemporaryDirectory() as td:
        target = write_adventure(draft, os.path.join(td, "a.json"))
        code = cli_main(["playtest", target, "--scenes", "1"])
    ok(code == 0, "试跑第一场通过（假模型）", code)


CARD = os.path.join(ROOT, "tests", "fixtures", "tavern_card_v2.json")


def test_card_json():
    card = load_card(CARD)
    ok(card["name"] == "老周·摆渡人", "V2 卡取 data.name", card.get("name"))
    draft = draft_from_card(card)
    ok(len(draft["scenes"]) == 1, "卡面先给一场开场", len(draft["scenes"]))
    ok("茶棚" in draft["scenes"][0]["situation"],
       "局面取自 scenario 原文", draft["scenes"][0]["situation"][:30])
    ok(len(draft["threads"]) == 3, "世界书三条 → 背景线", draft["threads"])
    ok(draft["npcs"][0]["name"] == "老周·摆渡人", "卡面主角进 NPC", draft["npcs"])


def test_card_v1_flat():
    flat = {"name": "平铺卡", "scenario": "一间只剩半面墙的屋子，风从缺口灌进来。",
            "first_mes": "屋主把灯芯拧小，低声说：别出声。", "creator_notes": "守着门。"}
    draft = draft_from_card(flat)
    ok(draft["title"].startswith("角色卡《平铺卡》"), "V1 平铺也能读", draft["title"])
    ok(len(draft["threads"]) == 0, "没有世界书就没有背景线")


def test_card_png():
    """SillyTavern 把卡 JSON 塞进 PNG 的 tEXt `chara` 块（base64）。"""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    import base64

    payload = base64.b64encode(io.open(CARD, "rb").read()).decode()
    meta = PngInfo()
    meta.add_text("chara", payload)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "card.png")
        Image.new("RGB", (8, 8), (20, 20, 20)).save(path, pnginfo=meta)
        card = load_card(path)
        ok(card["name"] == "老周·摆渡人", "PNG 卡能解出 chara", card.get("name"))
        draft = draft_from_card(load_card_bytes(open(path, "rb").read()))
        ok(len(draft["scenes"]) == 1, "PNG 卡也能出草稿")


def test_worldbook_file():
    import json as _json
    entries = {"entries": {"0": {"comment": "旧码头", "content": "水下压着旧码头的石桩。",
                                 "enabled": True},
                            "1": {"comment": "无名", "content": "",
                                  "enabled": False}}}
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "wb.json")
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(entries, f, ensure_ascii=False)
        extra = load_worldbook(path)
        ok(len(extra) == 2, "世界书对象形态也能读", len(extra))
        draft = draft_from_card(load_card(CARD), extra_entries=extra)
        ok(any(t["title"] == "旧码头" for t in draft["threads"]),
           "外部世界书并进背景线", [t["title"] for t in draft["threads"]])


def test_main_thread_uses_normalized_name():
    """外审 B5：卡名含大小写/空格时，主线条目也要认得出来。"""
    card = {"name": "Alice Chen", "scenario": "屋子。", "first_mes": "抬头。",
            "character_book": {"entries": [
                {"comment": "甲", "keys": ["别的"], "content": "别处。",
                 "enabled": True},
                {"comment": "乙", "keys": ["Alice Chen"], "content": "她的背景。",
                 "enabled": True}]}}
    draft = draft_from_card(card)
    kinds = {t["title"]: t["kind"] for t in draft["threads"]}
    ok(kinds.get("乙") == "main", "按归一化卡名认出主线", kinds)


def test_disabled_entries_never_suggest():
    """外审 B6：enabled:false 的条目不该出现在候选线索里。"""
    entries = [{"comment": "关掉的", "keys": ["账本"],
                "content": "账本第三页有刀痕。", "enabled": False}]
    ok(suggest_clues(merge_entries(entries)) == [],
       "被禁用的世界书条目不提线索")


def test_brief_type_error_is_readable():
    """外审 B7：brief 写成字符串时给能看懂的 ValueError，而不是 AttributeError。"""
    from tools.forge.contract import to_adventure
    try:
        to_adventure({"title": "x", "scenes": [{"situation": "y" * 10,
                                                "brief": "一段文字"}]})
        raised = ""
    except ValueError as exc:
        raised = str(exc)
    except AttributeError:
        raised = "AttributeError"
    ok("brief 必须是对象" in raised, "brief 类型错给可读报错", raised)


def test_template_meta_collected():
    """外审 B3：自带模板把人数/时长写在标题之后，不能丢。"""
    with open(TEMPLATE, encoding="utf-8") as f:
        draft = parse_markdown(f.read())
    ok(draft["meta"].get("players") == "4", "模板 meta 收到人数", draft["meta"])
    ok(bool(draft["meta"].get("hours")), "模板 meta 收到时长", draft["meta"])


def test_bad_itxt_does_not_crash():
    """外审 B1：损坏的压缩 iTXt 块要跳过，不能把转换打崩。"""
    import struct
    import base64

    raw = io.open(CARD, "rb").read()
    payload = base64.b64encode(raw).decode().encode("utf-8")
    ihdr = struct.pack(">I", 13) + b"IHDR" + b"\x00" * 13
    ihdr += struct.pack(">I", 0)[0:0]  # 占位（长度/CRC 由下面补）
    chunk = (struct.pack(">I", len(b"chara\x00\x00\x00\x00\x00" + b"\x00" * 8 + b"bad"))
             + b"iTXt"
             + b"chara\x00\x00\x00\x00\x00" + b"\x00" * 8 + b"bad")
    png = (b"\x89PNG\r\n\x1a\n" + ihdr + chunk + b"\x00" * 4
           + struct.pack(">I", 0) + b"IEND" + b"\x00" * 4)
    try:
        load_card_bytes(png)
        ok(True, "坏 iTXt 不抛穿（要么跳过、要么给 ValueError）")
    except ValueError:
        ok(True, "坏 iTXt 给 ValueError 而不是 zlib.error")


def test_api_rejects_non_object():
    from tools.forge.serve import _api_convert
    for bad in ([1, 2], "x", None):
        try:
            _api_convert(bad)
            raised = False
        except (TypeError, ValueError):
            raised = True
        ok(raised, f"非对象 payload 报错而不是崩：{type(bad).__name__}")


def test_serve_convert():
    raw = io.open(CARD, "rb").read()
    out = convert("card", "card.json", raw)
    ok(out["ok"] and out["report"]["stats"]["scenes"] == 1,
       "锻造台服务端转换（不落盘）", out["summary"])
    out2 = convert("md", "t.md", io.open(TEMPLATE, "rb").read())
    ok(out2["report"]["stats"]["scenes"] == 2, "服务端也能吃模板", out2["summary"])
    try:
        convert("card", "x.txt", b"not a card")
        raised = ""
    except ValueError as exc:
        raised = str(exc)
    ok("卡面" in raised or "PNG" in raised,
       "坏文件给的是『卡面/PNG』这类可操作报错（不是任意 ValueError）", raised)


def test_alias_merge():
    """同一个 NPC 的多个称呼要合成一条，不然世界书会碎。"""
    entries = [
        {"comment": "老周", "keys": ["老周", "摆渡人"],
         "content": "老周在雾渡口摆渡，左脚踏过一次水。", "enabled": True},
        {"comment": "周伯", "keys": ["周伯", "摆渡人"],
         "content": "周伯守着茶棚，只在有人问账本时开口。", "enabled": True},
        {"comment": "缆绳", "keys": ["缆绳"],
         "content": "缆绳的断口还湿着。", "enabled": True},
    ]
    merged = merge_entries(entries)
    ok(len(merged) == 2, "共享别名 → 合成一条", [m["title"] for m in merged])
    first = merged[0]
    ok("别名" in first["summary"], "合并后摘要里带别名", first["summary"][:40])
    ok("周伯" in first["summary"], "两段正文都保留", first["summary"])


def test_clue_suggestions():
    """只有**可查的痕迹**才算候选线索；地名不算。"""
    entries = [
        {"comment": "雾渡口的来历", "keys": ["雾渡口"],
         "content": "雾渡口原本是盐商的私渡，后来盐路废了。", "enabled": True},
        {"comment": "盲婆", "keys": ["盲婆"],
         "content": "盲婆用黑蜡在钟绳上做记号，谁碰过就留痕。", "enabled": True},
    ]
    clues = suggest_clues(merge_entries(entries))
    ok(len(clues) == 1, "地名不触发线索建议", [c["from"] for c in clues])
    ok(clues[0]["skill"] == "investigation", "记号 → 调查", clues[0])
    ok("记号" in clues[0]["why"], "建议要说明为什么", clues[0]["why"])


def test_suggestions_not_written_into_draft():
    """候选线索只建议、不写进草稿：DC 不能替作者定。"""
    card = load_card(CARD)
    draft = draft_from_card(card)
    ok(draft["scenes"][0]["clues"] == [],
       "草稿里没有线索（建议不落进剧本）", draft["scenes"][0]["clues"])
    clues = suggest_clues(merge_entries(worldbook_entries(card)))
    ok(len(clues) >= 1, "但有候选建议", len(clues))


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS {name}")
        except AssertionError as exc:
            fails += 1
            print(f"  FAIL {name}: {exc}")
    print(f"\n共 {COUNT} 项：{'全部通过' if not fails else f'失败 {fails}'}")
    raise SystemExit(1 if fails else 0)