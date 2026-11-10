# coding=utf-8
"""U9 离线抽样：拿一批**手写叙事样本**跑体检，看规则能不能分辨好坏硬伤。

用法：
    python tools\\narrative_sample.py
    python tools\\narrative_sample.py path\\to\\narratives.jsonl

口径：只判"可判定的硬伤"（泄漏/编号/空/重复/长度/围栏）。写得好不好、
人物像不像，这工具不判 —— 那要真模型 + 人读。
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.rules.narrate_lint import check_narration  # noqa: E402

DEFAULT = os.path.join(ROOT, "tests", "fixtures", "narratives.jsonl")


def sample(path: str = DEFAULT) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = json.loads(line)
            issues = check_narration(
                d.get("text", ""),
                forbidden=d.get("forbidden", ()),
                min_chars=int(d.get("min_chars", 30)),
                max_chars=int(d.get("max_chars", 1200)))
            got = sorted({i.code for i in issues})
            expect = sorted(set(d.get("expect", [])))
            rows.append({"id": d.get("id", "?"), "got": got, "expect": expect,
                         "ok": got == expect, "issues": issues})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="叙事离线抽样体检")
    ap.add_argument("path", nargs="?", default=DEFAULT)
    args = ap.parse_args(argv)
    rows = sample(args.path)
    bad = 0
    print("编号｜结果｜检出｜期望")
    for r in rows:
        mark = "✅" if r["ok"] else "❌"
        print(f"{r['id']}｜{mark}｜{','.join(r['got']) or '（无）'}｜"
              f"{','.join(r['expect']) or '（无）'}")
        if not r["ok"]:
            bad += 1
            for i in r["issues"]:
                print(f"    {i}")
    print(f"\n共 {len(rows)} 条：通过 {len(rows) - bad}，不符合期望 {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())