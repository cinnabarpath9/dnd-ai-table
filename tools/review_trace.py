# coding=utf-8
"""复盘真模型调用留痕：看正文，也看**思考链有没有读对信息**。

输入是 `tools/real_smoke.py --trace xxx.jsonl` 写出的逐调用记录。
本工具只做**展示与关键词核对**，不替人判断推理对错：

    python tools\\review_trace.py --trace tools\\真模型记录\\2026-11-XX-M1-trace.jsonl \
        --must 雾渡口 --must 盲婆 --must 旧闸

`--must` 给“这一局必须被读进去的事实”（场景名/NPC/关键数字）；工具会
逐条调用统计思考链里缺了哪些。缺了不一定是模型错（那一次调用可能不需要），
但**一次都没出现过**就值得停下来看。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass


def load(path: str) -> tuple[dict, list[dict]]:
    meta: dict = {}
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "meta":
                meta = rec
            elif rec.get("type") == "call":
                rows.append(rec)
    return meta, rows


def clip(text: str, n: int) -> str:
    text = str(text or "")
    if n <= 0 or len(text) <= n:
        return text
    return text[:n] + f"\n…（截掉 {len(text) - n} 字）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="复盘真模型调用：正文 + 思考链")
    ap.add_argument("--trace", required=True)
    ap.add_argument("--seat", default="", help="只看某个座位标签（如 布兰 / dm）")
    ap.add_argument("--task", default="", help="只看某个任务（如 combat_turn）")
    ap.add_argument("--limit", type=int, default=0, help="最多看几条（0=全部）")
    ap.add_argument("--chars", type=int, default=1200,
                    help="正文/思考链截断字数（0=不截）")
    ap.add_argument("--show", choices=("both", "reasoning", "text"),
                    default="both")
    ap.add_argument("--no-prompt", action="store_true", help="不打印 prompt")
    ap.add_argument("--must", action="append", default=[],
                    help="必须出现在思考链里的事实，可重复")
    ap.add_argument("--json", action="store_true", help="原样输出 JSON 行")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.trace):
        print(f"❌ 读不到 trace：{args.trace}")
        return 2
    meta, rows = load(args.trace)
    if args.seat:
        rows = [r for r in rows if r.get("seat_label") == args.seat
                or r.get("seat") == args.seat or r.get("actor") == args.seat]
    if args.task:
        rows = [r for r in rows if r.get("task") == args.task]
    if args.limit > 0:
        rows = rows[:args.limit]

    if args.json:
        if meta:
            print(json.dumps(meta, ensure_ascii=False))
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return 0

    print(f"trace：{args.trace}")
    if meta:
        print(f"剧本 {meta.get('adventure')}｜种子 {meta.get('seed')}｜"
              f"模型 {meta.get('model')}｜场景 {meta.get('scenes')}")
    with_reason = sum(1 for r in rows if str(r.get("reasoning") or "").strip())
    failed = sum(1 for r in rows if not r.get("ok"))
    empty = sum(1 for r in rows
                if not str(r.get("text") or "").strip()
                and not str(r.get("reasoning") or "").strip())
    prompt_tokens = sum(int(r.get("prompt_tokens") or 0) for r in rows)
    cached_tokens = sum(int(r.get("cached_tokens") or 0) for r in rows)
    print(f"调用 {len(rows)} 次；带思考链 {with_reason} 次；"
          f"失败 {failed} 次；正文/思考链都空 {empty} 次")
    if cached_tokens and prompt_tokens:
        print(f"提示缓存命中 {cached_tokens}/{prompt_tokens} tokens"
              f"（{cached_tokens / prompt_tokens:.0%}）")
        by_task: dict[str, list[int]] = {}
        for r in rows:
            key = str(r.get("task") or "?")
            by_task.setdefault(key, [0, 0])
            by_task[key][0] += int(r.get("cached_tokens") or 0)
            by_task[key][1] += int(r.get("prompt_tokens") or 0)
        for key, (hit, total) in sorted(by_task.items()):
            if total:
                print(f"  · {key:24s} {hit}/{total}（{hit / total:.0%}）")
    else:
        print("提示缓存：上游没返回 cached_tokens（不是 0 命中的证明）")
    print()

    missing_any = 0
    no_reason = 0
    for r in rows:
        print("=" * 78)
        print(f"#{r.get('seq')} {r.get('seat_label')}/{r.get('actor')} "
              f"{r.get('task')}｜{'OK' if r.get('ok') else 'FAIL'}"
              f"｜入 {r.get('prompt_tokens')} 出 {r.get('completion_tokens')}"
              f"｜缓存 {r.get('cached_tokens', 0)}"
              f"｜{r.get('elapsed')}s")
        if r.get("error"):
            print(f"ERROR: {r['error']}")
        if not args.no_prompt:
            print("\n[PROMPT]\n" + clip(r.get("prompt", ""), args.chars))
        if args.show in ("both", "reasoning"):
            print("\n[REASONING]\n"
                  + (clip(r.get("reasoning", ""), args.chars) or "（上游没返回思考链）"))
        if args.show in ("both", "text"):
            print("\n[TEXT]\n" + clip(r.get("text", ""), args.chars))
        if args.must:
            reason = str(r.get("reasoning") or "")
            if not reason.strip():
                no_reason += 1
            else:
                missing = [m for m in args.must if m not in reason]
                if missing:
                    missing_any += 1
                    print("\n[MISSING FACTS] " + "、".join(missing))

    if args.must:
        print("\n" + "=" * 78)
        print(f"must 核对：{len(rows)} 次调用，{missing_any} 次思考链缺至少一条；"
              f"{no_reason} 次上游没返回思考链（未参与核对）；事实清单 {args.must}")
        print("提示：单次缺不代表错（那次可能不需要这条）；看的是“整局一次都没读到”。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())