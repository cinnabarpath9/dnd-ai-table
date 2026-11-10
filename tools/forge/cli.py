# coding=utf-8
"""锻造台命令行。

    python -m tools.forge doctor   <草稿或剧本.json>
    python -m tools.forge md       <模板.md> [-o 剧本.json]
    python -m tools.forge novel    <小说.txt> [-o 粗稿.json] [--title 书名]
    python -m tools.forge playtest <草稿或剧本.json> [--scenes 2] [--players 4]

`playtest` 用假模型把剧本跑一遍（零花费），只报"跑起来会不会卡住"：
缺简报、空战斗、场景没人可动 —— 这些在真模型局里会变成烧钱的纠错。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.adventure import AdventureError, load_adventure  # noqa: E402
from tools.forge.contract import doctor, to_adventure, write_adventure
from tools.forge.pipeline import convert as pipeline_convert  # noqa: E402
from tools.forge.cards import load_worldbook  # noqa: E402


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _print_report(report: dict) -> None:
    stats = report.get("stats") or {}
    if stats:
        print(f"规模：{stats.get('scenes')} 场 / {stats.get('combats')} 战 / "
              f"{stats.get('clues')} 条线索 / {stats.get('promises')} 条伏笔；"
              f"预计真模型调用 ≈ {stats.get('estimated_calls')} 次")
    for msg in report.get("errors", []):
        print(f"  ❌ {msg}")
    for msg in report.get("warnings", []):
        print(f"  ⚠️ {msg}")
    print("✅ 可以跑" if report.get("ok") else "❌ 还不能跑")


def _cmd_doctor(args) -> int:
    raw = _load_json(args.source)
    report = doctor(raw)
    _print_report(report)
    return 0 if report["ok"] else 2


def _cmd_md(args) -> int:
    data = open(args.source, "rb").read()
    out = pipeline_convert("md", os.path.basename(args.source), data)
    draft, report = out["draft"], out["report"]
    _print_report(report)
    if not report["ok"]:
        return 2
    if args.out:
        print("写入：", write_adventure(draft, args.out))
    return 0


def _cmd_novel(args) -> int:
    data = open(args.source, "rb").read()
    out = pipeline_convert("novel", os.path.basename(args.source), data,
                           title=args.title or "")
    draft, report = out["draft"], out["report"]
    _print_report(report)
    if args.out:
        target = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("写入粗稿：", target, "（缺的简报/线索/伏笔要补，见上面的 ❌/⚠️）")
    return 0 if report["ok"] else 2


def _cmd_card(args) -> int:
    extra = load_worldbook(args.worldbook) if args.worldbook else None
    data = open(args.source, "rb").read()
    out = pipeline_convert("card", os.path.basename(args.source), data,
                           title=args.title or "", extra_entries=extra)
    draft, report = out["draft"], out["report"]
    _print_report(report)
    print(out["summary"])
    clues = out["suggestions"]
    if clues:
        print(f"候选线索 {len(clues)} 条（**没写进草稿**，确认后再加）：")
        for clue in clues:
            print(f"  · [{clue['skill']} DC {clue['suggested_dc']}] "
                  f"{clue['text']}  （{clue['why']}｜来自「{clue['from']}」）")
    if report["errors"]:
        print("⚠️ 这是**粗稿**：先把上面点名的简报/线索/伏笔补上，再 doctor。")
    if args.out:
        target = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("写入草稿：", target)
    return 0


def _cmd_playtest(args) -> int:
    """假模型跑一段：只验"跑得动"，不验平衡。"""
    from engine.dice import Random
    from engine.models import FakeModel, TableClient
    from engine.rules.loop import GameLoop, make_hero_unit
    from engine.state import Ledger, StateTree, new_character
    from engine.adventure import AdventureRunner

    raw = _load_json(args.source)
    report = doctor(raw)
    if not report["ok"]:
        _print_report(report)
        print("先修上面的问题再试跑。")
        return 2
    adv = load_adventure_source(raw)
    names = ["布兰", "艾尔玛", "罗恩", "小翠"][:max(1, args.players)]
    st = StateTree.from_template(
        {n: new_character(n, level=3, job="战士", hp_max=24, ac=15) for n in names},
        current=names[0])
    st.commit([{"op": "insert", "path": f"/角色列表/{n}/物品/长剑",
                "value": {"类别": "武器", "已装备": True}} for n in names],
              actor="填表", reason="装备")
    table = TableClient(dm=FakeModel(name="dm"), planner=FakeModel(name="pl"),
                        players={n: FakeModel(name=n) for n in names})
    loop = GameLoop(st, Ledger(), table, rng=Random(20261108))
    loop.set_field([make_hero_unit(st, n) for n in names])
    runner = AdventureRunner(loop, adv)
    runner.seed_map()
    scenes = adv.scenes[:max(1, args.scenes)]
    problems = 0
    for scene in scenes:
        before = len(loop.logs)
        try:
            runner.run_scene(scene, combat_rounds=2)
        except Exception as exc:                        # noqa: BLE001
            print(f"  ❌ {scene.id} 跑了就抛：{type(exc).__name__}: {exc}")
            return 1
        rounds = loop.logs[before:]
        bad = sum(len(r.problems) for r in rounds)
        problems += bad
        print(f"  {scene.id}《{scene.title}》→ {len(rounds)} 轮，问题 {bad}")
    print(f"试跑完成：{problems} 个可恢复问题（假模型；真模型会更严）")
    return 0 if problems == 0 else 1


def _cmd_serve(args) -> int:
    from tools.forge.serve import main as serve_main
    return serve_main(["--port", str(args.port)])


def load_adventure_source(raw: dict):
    """草稿或成品都能吃：成品直接解析，草稿先补默认值。"""
    from engine.adventure import parse_adventure

    if raw.get("format") == "dnd-campaign-adventure":
        return parse_adventure(raw)
    return parse_adventure(to_adventure(raw))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="剧本锻造台（草稿 → 可跑剧本）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="检查一本剧本/草稿能不能跑")
    p.add_argument("source")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("md", help="固定模板 Markdown → 剧本 JSON")
    p.add_argument("source")
    p.add_argument("-o", "--out", default="")
    p.set_defaults(func=_cmd_md)

    p = sub.add_parser("card", help="酒馆角色卡（JSON/PNG）→ 草稿")
    p.add_argument("source")
    p.add_argument("--worldbook", default="",
                   help="单独的世界书 JSON（可选）")
    p.add_argument("--title", default="")
    p.add_argument("-o", "--out", default="")
    p.set_defaults(func=_cmd_card)

    p = sub.add_parser("novel", help="小说 txt → 粗稿（缺的字段要补）")
    p.add_argument("source")
    p.add_argument("-o", "--out", default="")
    p.add_argument("--title", default="")
    p.set_defaults(func=_cmd_novel)

    p = sub.add_parser("serve", help="开本地锻造台页面（独立运行）")
    p.add_argument("--port", type=int, default=8790)
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("playtest", help="假模型试跑（零花费）")
    p.add_argument("source")
    p.add_argument("--scenes", type=int, default=2)
    p.add_argument("--players", type=int, default=4)
    p.set_defaults(func=_cmd_playtest)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (AdventureError, ValueError, OSError) as exc:
        problems = getattr(exc, "problems", None) or [str(exc)]
        print("❌ " + "；".join(str(x) for x in problems))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
