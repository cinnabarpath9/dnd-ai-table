# coding=utf-8
"""结构重构 A~D 的守卫：一个入口、一处归位、一个 helper、一条转换线。"""
import glob
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.state.coerce import int_or  # noqa: E402
from engine.models import client as client_mod  # noqa: E402
from engine.models import format_check  # noqa: E402
from tools.forge import pipeline  # noqa: E402
from tools.forge import serve  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(label)
        print(f"  ✗ {label}  extra={extra!r}")


def read(p):
    return io.open(p, encoding="utf-8").read()


def py_files(*dirs):
    out = []
    for d in dirs:
        out += glob.glob(os.path.join(ROOT, d, "**", "*.py"), recursive=True)
    return out


# ---------- A. 进度路径一个入口 ----------
builders = []
for p in py_files("engine", "tools"):
    text = read(p)
    if '".progress.json"' in text or "'.progress.json'" in text:
        builders.append(os.path.relpath(p, ROOT))
ok(builders == [os.path.join("engine", "adventure", "progress.py")],
   "A1 ⭐ 只有 progress.py 拼 .progress.json", builders)

for rel in ("engine/adventure/campaign.py", "engine/adventure/runner.py",
            "tools/longrun_measure.py", "tools/real_smoke.py"):
    ok("progress_path_for" in read(os.path.join(ROOT, rel)),
       f"A2 {rel} 走 progress_path_for")

# ---------- B. 截断修复归位 ----------
ok("repair_truncated_json" in client_mod.__dict__, "B1 client 里定义修复函数")
ok("repair_truncated_json" not in format_check.__all__,
   "B2 format_check 不再转导（__all__ 里没有）", format_check.__all__)
ok(hasattr(format_check, "repair_truncated_json") is False,
   "B3 format_check 也不再 import 它（改策略只看一个文件）")
ok("from ..models.client import repair_truncated_json" in
   read(os.path.join(ROOT, "engine", "rules", "loop.py")),
   "B4 loop 直接从 client 取")

# ---------- C. 数值 helper 一份 ----------
ok(int_or("24") == 24 and int_or(None) == 0 and int_or("x", 7) == 7,
   "C1 int_or 三种输入都对")
ok(int_or("", 5) == 5, "C2 空串用默认值")
ok("_int_or" not in read(os.path.join(ROOT, "engine", "rules", "item_effects.py")),
   "C3 item_effects 不再自带 _int_or")
loop_src = read(os.path.join(ROOT, "engine", "rules", "loop.py"))
ok("def _num(" not in loop_src, "C4 loop 不再自带 _num")
ok("u.hp = int_or(u.hp_max)" in loop_src, "C5 长休的 hp_max 也走 int_or")

# 坏存档：hp_max 写成字符串也不能炸
from engine.dice import Random  # noqa: E402
from engine.models import FakeModel, TableClient  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402

st = StateTree.from_template({"布兰": new_character("布兰", level=3, job="战士",
                                                    hp_max=24, ac=15)},
                             current="布兰")
st.commit([{"op": "insert", "path": "/角色列表/布兰/物品/长剑",
            "value": {"类别": "武器", "已装备": True}}], actor="x", reason="y")
loop = GameLoop(st, Ledger(), TableClient(dm=FakeModel(), planner=FakeModel(),
                                          players={"布兰": FakeModel()}),
                rng=Random(1))
loop.set_field([make_hero_unit(st, "布兰")])
loop.field[0].hp_max = "24"
res = loop.long_rest()
ok(int(loop.field[0].hp) == 24, "C6 ⭐ hp_max 是字符串时长休照样满血", res)

# ---------- D. 锻造台一条转换线 ----------
CARD = os.path.join(ROOT, "tests", "fixtures", "card.json")
if not os.path.exists(CARD):
    cands = glob.glob(os.path.join(ROOT, "tests", "fixtures", "*card*.json"))
    CARD = cands[0] if cands else ""
ok(bool(CARD), "D1 找到角色卡夹具", CARD)

if CARD:
    data = open(CARD, "rb").read()
    name = os.path.basename(CARD)
    out = pipeline.convert("card", name, data)
    web = serve.convert("card", name, data)
    ok(out["draft"] == web["draft"], "D2 ⭐ CLI 与网页同一份草稿")
    ok(out["suggestions"] == web["suggestions"], "D3 候选线索同一条线")
    ok(out["report"]["ok"] == web["report"]["ok"], "D4 doctor 结果一致")

md = os.path.join(ROOT, "tools", "forge", "template.md")
md_out = pipeline.convert("md", "template.md", open(md, "rb").read())
ok(len(md_out["draft"]["scenes"]) == 2, "D5 模板经 pipeline 出 2 场")

gb = ("第一章 渡口\n\n天光刚亮，渡口上还浮着雾。布兰站在钟绳下。\n\n"
      "第二章 闸房\n\n闸房里堆着湿麻袋，墙角有拖痕。").encode("gb18030")
ok(pipeline.decode_text(gb).startswith("第一章"), "D6 ⭐ GB18030 能解（网页旧坑）")
nov = pipeline.convert("novel", "x.txt", gb)
ok(len(nov["draft"]["scenes"]) == 2, "D7 小说粗稿走 pipeline", len(nov["draft"]["scenes"]))

try:
    pipeline.convert("pdf", "x.pdf", b"")
    ok(False, "D8 不认识的 kind 要抛")
except ValueError as exc:
    ok("不认识的类型" in str(exc), "D8 不认识的 kind 明确报错")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for x in FAILS:
        print(f"  ✗ {x}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")