# coding=utf-8
"""旁挂调用留痕（含思考链）与复盘 CLI。"""
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.dice import Random  # noqa: E402
from engine.models import ModelReply, ModelRequest, TableClient  # noqa: E402
from engine.models.client import FakeModel, RecordedCall  # noqa: E402
from engine.persist import calls_path_for, save_game  # noqa: E402
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, new_character  # noqa: E402
import tools.review_save as review  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond, label, extra=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(label)
        print(f"  ✗ {label}  extra={extra!r}")


class ThinkingFake(FakeModel):
    """假模型，但回一段思考链 —— 验旁挂文件真的把它写下来了。"""

    def _complete(self, req):
        reply = super()._complete(req)
        reply.reasoning = "我在想：这一步该先看渡口还是先问老周。"
        return reply


def make_loop():
    st = StateTree.from_template(
        {"布兰": new_character("布兰", level=3, job="战士", hp_max=24, ac=15)},
        current="布兰")
    st.commit([{"op": "insert", "path": "/角色列表/布兰/物品/长剑",
                "value": {"类别": "武器", "已装备": True}}],
              actor="填表", reason="装备")
    loop = GameLoop(st, Ledger(), TableClient(
        dm=ThinkingFake(), planner=ThinkingFake(), players={"布兰": ThinkingFake()}),
        rng=Random(1))
    loop.set_field([make_hero_unit(st, "布兰")])
    return st, loop


st, loop = make_loop()
loop.client.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                  prompt="这是它看到的上下文", expect="narration"))
loop.client.complete(ModelRequest(role="player", actor="布兰",
                                  task="explore_action", prompt="菜单里有观察",
                                  expect="intent"))

td = tempfile.mkdtemp(prefix="calls-test-")
save = os.path.join(td, "a.json")
save_game(save, state=st, ledger=loop.ledger, loop=loop)
cp = calls_path_for(save)
ok(os.path.exists(cp), "A1 存档旁边生成了 *.calls.jsonl", cp)
rows = [json.loads(x) for x in io.open(cp, encoding="utf-8") if x.strip()]
ok(len(rows) == len(loop.client.calls), "A2 行数 = 调用次数",
   (len(rows), len(loop.client.calls)))
ok(rows[0]["reasoning_len"] > 0 and "先看渡口" in rows[0]["reasoning"],
   "A3 ⭐ 思考链写进旁挂（主存档不存）", rows[0].get("reasoning_len"))
ok(rows[0]["prompt_chars"] > 0 and "上下文" in rows[0]["prompt"],
   "A4 prompt 也写进旁挂", rows[0].get("prompt_chars"))
main = json.load(io.open(save, encoding="utf-8"))
ok("reasoning" not in json.dumps(main, ensure_ascii=False),
   "A5 ⭐ 主存档里没有思考链（不会膨胀）")

# 复盘渲染：思考链 + 剧透警告
buf = io.StringIO()
_stdout = sys.stdout
sys.stdout = buf
try:
    review.main(["--save", save, "--reasoning", "--limit", "1"])
finally:
    sys.stdout = _stdout
out = buf.getvalue()
ok("剧透" in out, "B1 复盘头部有剧透警告", out[:40])
ok("思考链：我在想" in out, "B2 ⭐ --reasoning 打出思考链", out[:200])


# 没有旁挂时：诚实说只有元数据
td2 = tempfile.mkdtemp(prefix="calls-test2-")
save2 = os.path.join(td2, "b.json")
st2, loop2 = make_loop()
loop2.client.complete(ModelRequest(role="dm", actor="dm", task="narrate",
                                   prompt="x", expect="narration"))
save_game(save2, state=st2, ledger=loop2.ledger, loop=loop2)
os.remove(calls_path_for(save2))
buf = io.StringIO()
sys.stdout = buf
try:
    review.main(["--save", save2])
finally:
    sys.stdout = _stdout
out2 = buf.getvalue()
ok("没有旁挂" in out2, "B3 旁挂缺失时明确说只有元数据", out2[:200])

# 主存档只存元数据：调用列表仍能重建
ok(len(json.load(io.open(save2, encoding="utf-8"))["client"]["calls"]) == 1,
   "A6 主存档仍保留元数据（token/耗时）")

print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")