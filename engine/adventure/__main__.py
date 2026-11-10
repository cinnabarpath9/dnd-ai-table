# coding=utf-8
"""剧本自检 / 骨架视图。

    python -m engine.adventure                    # 检查默认剧本
    python -m engine.adventure --render           # 打骨架视图（人看的）
    python -m engine.adventure path\\to\\x.json    # 检查指定剧本

检查不过 exit=1，逐条列出哪个场景、哪一段简报有问题。
**它不跑模型、不联网、不写文件。**
"""

from __future__ import annotations

import argparse
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from engine.adventure import (  # noqa: E402
    AdventureError, CampaignProgress, ProgressError, load_adventure,
    progress_path_for, render_skeleton,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")


def main() -> int:
    ap = argparse.ArgumentParser(description="剧本自检 / 骨架视图 / 跨局记忆")
    ap.add_argument("path", nargs="?", default=DEFAULT, help="剧本 JSON 路径")
    ap.add_argument("--render", action="store_true", help="打印骨架视图")
    ap.add_argument("--progress", action="store_true",
                    help="打印跨局记忆（哪些伏笔提过、哪些还欠着）")
    ap.add_argument("--progress-path", default="",
                    help="进度文件路径（默认与剧本同目录的 *.progress.json）")
    ap.add_argument("--pay", default="",
                    help="把某条伏笔标成已兑现（写回进度文件）")
    ap.add_argument("--note", default="", help="配合 --pay 的说明")
    args = ap.parse_args()

    try:
        adv = load_adventure(args.path)
    except AdventureError as exc:
        print(f"❌ {exc}")
        for p in exc.problems:
            print(f"   · {p}")
        return 1

    print(f"✅ 剧本合法：{adv.title}")
    print(f"   场景 {len(adv.scenes)}｜幕 {len(adv.acts)}｜"
          f"因果线 {len(adv.threads)}｜伏笔 {len(adv.promises)}｜"
          f"决策 {len(adv.decisions)}｜议程 {len(adv.agenda)}")
    for w in adv.warnings:
        print(f"   ⚠️ {w}")

    if args.render:
        print()
        print(render_skeleton(adv), end="")

    if args.pay or args.progress:
        p_path = args.progress_path or progress_path_for(args.path)
        try:
            prog = CampaignProgress.load(p_path, missing_ok=True,
                                         adventure=adv.title)
        except ProgressError as exc:
            print(f"\n❌ 跨局记忆读不了：{exc}")
            return 1

        if args.pay:
            if not any(p.id == args.pay for p in adv.promises):
                print(f"\n❌ 剧本里没有伏笔 {args.pay!r}"
                      f"（现有：{[p.id for p in adv.promises]}）")
                return 1
            changed = prog.close(args.pay, note=args.note)
            print("\n" + ("✅ 已标记兑现" if changed else "（本来就是已兑现，没改）")
                  + f"：{args.pay}")
            print(f"   进度文件：{prog.path}")

        if args.progress:
            print("\n【跨局记忆】")
            print(prog.summary(adv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())