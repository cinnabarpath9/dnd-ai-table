# coding=utf-8
"""长跑体积测量（假模型，零花费）：整本逐场记 prompt/世界/台账/存档增长。

    python tools/longrun_measure.py

三章 21 场；不改仓库里的进度文件（在临时目录里跑）。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from engine.adventure import (AdventureRunner, CampaignProgress, debt_text,
                              load_adventure, load_campaign, progress_path_for)
from engine.adventure.progress import progress_path_for
from engine.dice import Random
from engine.models import FakeModel, TableClient
from engine.persist import save_game
from engine.rules.loop import GameLoop, make_hero_unit
from engine.state import Ledger, StateTree, new_character


class SizedFake(FakeModel):
    """假模型 + 记录每次收到的 prompt 长度（用来估上下文膨胀）。"""

    def __init__(self, name="fake", sink=None):
        super().__init__(name=name)
        self.sink = sink if sink is not None else []

    def complete(self, req):
        self.sink.append(len(req.prompt or ""))
        return super().complete(req)


NAMES = ("布兰", "艾尔玛", "罗恩", "小翠")
st = StateTree.from_template(
    {n: new_character(n, level=3, job="战士", hp_max=24, ac=15) for n in NAMES},
    current=NAMES[0])
st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
            "value": {"类别": "武器", "已装备": True}} for n in NAMES],
          actor="填表", reason="装备")
sink = []
table = TableClient(dm=SizedFake("dm", sink),
                    planner=SizedFake("pl", sink),
                    players={n: SizedFake(n, sink) for n in NAMES})
loop = GameLoop(st, Ledger(), table, rng=Random(20261109), prompt_budget=6000)
loop.set_field([make_hero_unit(st, n) for n in NAMES])

import shutil
camp_src = "adventures/M2测试本.campaign.json"
camp = load_campaign(camp_src)
tmp = tempfile.mkdtemp(prefix="longrun-")
for name in (os.path.basename(camp_src),
             "M2测试本-上-钟下的账.json",
             "M2测试本-下-水寨的账.json",
             "M2测试本-续-上游的税关.json"):
    shutil.copy2(os.path.join("adventures", name), os.path.join(tmp, name))
camp = load_campaign(os.path.join(tmp, os.path.basename(camp_src)))
rows = []
scene_no = 0
for chapter in camp.chapters:
    adv_path = os.path.join(tmp, chapter.adventure)
    adv = load_adventure(adv_path)
    # 外审 A1：路径必须走 progress_path_for（会去掉 .json），否则欠账读到空进度
    prog = CampaignProgress.load(progress_path_for(adv_path),
                                 adventure=adv.title)
    print(f"[{chapter.id}] 欠账 {len(debt_text(camp, chapter.id))} 字")
    runner = AdventureRunner(loop, adv, progress=prog,
                             campaign_debt=debt_text(camp, chapter.id),
                             campaign=camp, chapter_id=chapter.id)
    runner.seed_map()
    for scene in adv.scenes:
        scene_no += 1
        before = len(sink)
        loop.logs_before = len(loop.logs)
        runner.run_scene(scene, combat_rounds=3)
        prompt_max = max(sink[before:], default=0) if len(sink) > before else 0
        rows.append({
            "n": scene_no, "chapter": chapter.id, "scene": scene.id,
            "calls": len(sink) - before,
            "prompt_max": prompt_max,
            "world": len(loop.world.render(NAMES[0])),
            "ledger": len(loop.ledger.render(NAMES[0])),
            "ledger_trim": len(loop.ledger.render(NAMES[0], last_rounds=loop.ledger_rounds)),
            "logs": sum(len(r.lines) for r in loop.logs),
        })

save_path = os.path.join(tmp, "final.json")
save_game(save_path, state=st, ledger=loop.ledger, loop=loop, note="longrun")
size = os.path.getsize(save_path)
hist = os.path.join(tmp, "history")
print("\n场次  章  场  调用  prompt最长  世界  台账全  台账裁  日志")
for row in rows:
    print(f"{row['n']:>3}  {row['chapter']}  {row['scene']:<4} "
          f"{row['calls']:>3}  {row['prompt_max']:>8}  {row['world']:>4}  "
          f"{row['ledger']:>5}  {row['ledger_trim']:>5}  {row['logs']:>4}")
print(f"\n存档 {size/1024:.1f} KB｜历史文件 {len(os.listdir(hist)) if os.path.exists(hist) else 0} 份")
print(f"总调用 {len(sink)} 次｜prompt 最长 {max(r['prompt_max'] for r in rows)} 字")
print("sink 前 5 长:", sorted(sink, reverse=True)[:5])