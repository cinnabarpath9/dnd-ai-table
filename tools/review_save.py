# coding=utf-8
"""复盘一局存档：把每次模型调用摊开（**含思考链，剧透警告**）。

    python tools/review_save.py                      # 最近一份存档
    python tools/review_save.py --list               # 列出所有存档
    python tools/review_save.py --seat 布兰 --task narrate --reasoning
    python tools/review_save.py --packet             # 连它当时看到的上下文
    python tools/review_save.py --json               # 原样输出（给别的工具吃）

思考链与 prompt 在**旁挂文件** `<存档名>.calls.jsonl` 里（主存档只存 token/耗时）。
没有旁挂文件时退化成"只有元数据"，并明确说出来。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.persist import calls_path_for  # noqa: E402

DEFAULT_DIR = os.path.join(ROOT, "saves", "real")


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_calls(save_path: str) -> tuple[list[dict], bool]:
    """返回 (调用列表, 有没有旁挂文件)。没有旁挂就读主存档的元数据。"""
    cp = calls_path_for(save_path)
    if os.path.exists(cp):
        rows = []
        with open(cp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows, True
    doc = _load_json(save_path)
    return list(((doc.get("client") or {}).get("calls") or [])), False


def _fmt_sec(v) -> str:
    try:
        s = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{s:.1f}s" if s < 100 else f"{s:.0f}s"


def _fmt_chars(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 10000:.1f} 万字" if n >= 10000 else f"{n} 字"


def render(rows: list[dict], args) -> int:
    shown = 0
    for row in rows:
        if args.seat and str(row.get("actor")) != args.seat \
                and str(row.get("role")) != args.seat:
            continue
        if args.task and str(row.get("task")) != args.task:
            continue
        shown += 1
        if args.limit and shown > args.limit:
            break
        head = (f"\n▸ #{row.get('seq')} {row.get('actor') or row.get('role')}"
                f" · {row.get('task')} · {_fmt_sec(row.get('elapsed'))}"
                f" · 入 {row.get('prompt_tokens')} / 出 {row.get('completion_tokens')}")
        if row.get("cached_tokens"):
            head += f"（缓存 {row['cached_tokens']}）"
        if not row.get("ok", True):
            head += f" · ❌ {row.get('error') or '失败'}"
        elif row.get("refused"):
            head += " · 🚫 审核/拒答"
        print(head)
        rlen = row.get("reasoning_len")
        if args.reasoning:
            text = str(row.get("reasoning") or "").strip()
            print("   思考链：" + (text if text else "（这次没有思考链）"))
        elif rlen:
            print(f"   思考链：{_fmt_chars(rlen)}（加 --reasoning 看全文）")
        if args.packet:
            pkt = str(row.get("prompt") or "")
            print(f"   —— 它当时看到的上下文（{_fmt_chars(len(pkt))}）——")
            for line in pkt.splitlines():
                print("   | " + line)
    total = len(rows)
    print(f"\n共 {total} 次调用；本次筛选显示 {min(shown, total)} 条。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="复盘一局存档（含思考链，剧透警告）")
    ap.add_argument("--save", default="", help="存档路径；不给就用最近的")
    ap.add_argument("--save-dir", default=DEFAULT_DIR)
    ap.add_argument("--list", action="store_true", help="列出所有存档")
    ap.add_argument("--seat", default="", help="只看某个座位（actor/role）")
    ap.add_argument("--task", default="", help="只看某种任务（如 narrate / combat_turn）")
    ap.add_argument("--reasoning", action="store_true", help="打印思考链全文")
    ap.add_argument("--packet", action="store_true", help="打印当时看到的上下文")
    ap.add_argument("--limit", type=int, default=0, help="最多看几条（0=全部）")
    ap.add_argument("--json", action="store_true", help="原样输出 JSON 行")
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.save_dir, "*.json")))
    paths = [p for p in paths if not p.endswith(".calls.jsonl")]
    if args.list:
        for p in paths:
            try:
                doc = _load_json(p)
            except (OSError, ValueError):
                print(f"{os.path.basename(p)}  （读不了，跳过）")
                continue
            calls = len((doc.get("client") or {}).get("calls") or [])
            has_calls = os.path.exists(calls_path_for(p))
            print(f"{os.path.basename(p)}  调用 {calls} 条"
                  f"{'（含思考链）' if has_calls else '（无旁挂，只有元数据）'}")
        return 0

    path = args.save or (paths[-1] if paths else "")
    if not path or not os.path.exists(path):
        print("没找到存档。先跑一局，或用 --save 指定。")
        return 2

    rows, has_calls = _load_calls(path)
    if args.json:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return 0

    print(f"存档：{path}")
    print(f"⚠️ 下面含**剧透**（DM 私有信息 / 思考链）；不要给玩家座位看。")
    if not has_calls:
        print("（没有旁挂 `*.calls.jsonl`：只有 token/耗时元数据，"
              "思考链与 prompt 不在存档里）")
    if not rows:
        print("这份存档里没有调用记录。")
        return 0
    return render(rows, args)


if __name__ == "__main__":
    raise SystemExit(main())