# coding=utf-8
"""一键回归：跑 tests/ 下所有 t_*.py。

零模型调用、零网络、零花费 —— 这是 P1 阶段的硬要求。
（SRD 数据采集是唯一需要网络的一步，跑完就有本地缓存，回归不再联网。）

跑法：
    python tests/run_all.py             # 全部
    python tests/run_all.py dice        # 只跑文件名含 "dice" 的
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 回归进程默认禁真调用（Q08）：即使某个测试误建了 RealModel(enabled=True)，
#: 也只会在发请求前失败，不会真花钱。要跑真批次用 tools/real_smoke.py。
os.environ.setdefault("DND_NO_REAL_CALLS", "1")


#: 两类都跑：`t_*` 是行为测试，`check_*` 是结构/一致性自检。
#: ⚠️ 这个 glob 必须覆盖**所有**测试文件名形态 —— 漏一类就等于那批断言永远不会红
#: （狼人杀项目的教训：新写的测试忘了登记，会静默漏跑）。
PATTERNS = ("t_*.py", "check_*.py")


def scripts(pat: str | None) -> list[str]:
    out: list[str] = []
    for pattern in PATTERNS:
        out.extend(glob.glob(os.path.join(HERE, pattern)))
    out = sorted(set(out))
    if pat:
        out = [p for p in out if pat in os.path.basename(p)]
    return out


def audit() -> list[str]:
    """清单 vs 目录自查：有没有 .py 文件既不匹配 t_* 也不匹配 check_*？

    有的话它不是"被跳过"，而是**从来没被跑过** —— 必须显式说出来。
    """
    on_disk = {os.path.basename(p) for p in glob.glob(os.path.join(HERE, "*.py"))}
    covered = {os.path.basename(p) for p in scripts(None)}
    ignored = {"run_all.py", "__init__.py"}
    return sorted(on_disk - covered - ignored)


def doc_item_count() -> int | None:
    """从 docs/40-待办与验证.md 里读"N 项回归全绿"的那个 N（没有就 None）。"""
    p = os.path.join(ROOT, "docs", "40-待办与验证.md")
    try:
        text = open(p, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"([\d,]+)\s*项回归全绿", text)
    return int(m.group(1).replace(",", "")) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", default=None, help="只跑文件名含这段的")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--no-netguard", action="store_true",
                    help="不装出口闸（默认装：外连一律抛，证明测试真离线）")
    args = ap.parse_args()

    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    #: 本机 Windows 系统代理指向一个回环端口；不排除回环时，测试的
    #: 127.0.0.1 请求会被代理吞成 502（2026-11-06 实测的偶发红）。
    child_env["NO_PROXY"] = "127.0.0.1,localhost"
    child_env["no_proxy"] = "127.0.0.1,localhost"
    if not args.no_netguard:
        child_env["DND_NETGUARD"] = "1"

    files = scripts(args.pattern)
    if not files:
        print(f"❌ 没找到测试（pattern={args.pattern!r}）")
        return 2

    ignored = audit()
    print("=" * 78)
    print(f"回归：{len(files)} 个脚本｜超时 {args.timeout:.0f}s/个"
          f"｜出口闸 {'关（--no-netguard）' if args.no_netguard else '开（只放行回环）'}")
    if ignored:
        print(f"⚠️ 有 {len(ignored)} 个 .py 既不是 t_* 也不是 check_*，"
              f"**从来没被跑过**：{', '.join(ignored)}")
        print("   （要跑就改名成 t_<名字>.py 或 check_<名字>.py）")
    print("=" * 78)

    failed: list[str] = []
    items_total = 0
    per_script: dict[str, int] = {}
    t0 = time.time()
    for path in files:
        name = os.path.basename(path)
        started = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, path],
                cwd=ROOT, capture_output=True, timeout=args.timeout,
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            print(f"\n⏱ 超时 {name}（>{args.timeout:.0f}s）")
            failed.append(f"{name}（超时）")
            continue
        dt = time.time() - started
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        # 只回显脚本自己的汇总行，避免刷屏
        tail = [ln for ln in out.splitlines() if ln.startswith("共 ")]
        status = "✅" if proc.returncode == 0 else "❌"
        summary = tail[-1] if tail else ""
        m_items = re.search(r"共 (\d+) 项", summary)
        if m_items:
            items_total += int(m_items.group(1))
            per_script[name] = int(m_items.group(1))
        print(f"{status} {name}  ({dt:.1f}s)  {summary}")
        if proc.returncode != 0:
            failed.append(name)
            print("─" * 78)
            # 只打 FAIL 行 + stderr 尾部，避免把整份 PASS 列表刷出来
            fails = [ln for ln in out.splitlines() if ln.strip().startswith(("FAIL", "✗"))]
            if fails:
                for ln in fails[:40]:
                    print(ln)
                if len(fails) > 40:
                    print(f"... 另有 {len(fails) - 40} 条 FAIL")
            else:
                print(out[-3000:] if out else "(无 stdout)")
            if err:
                print("--- stderr ---")
                print(err[-2000:])

    print("\n" + "=" * 78)
    elapsed = round(time.time() - t0, 1)
    print(f"合计 {len(files)} 个脚本，耗时 {elapsed}s")
    print(f"实测 {items_total} 项")
    # 落一份报告，给 check_docs.py 对账（文档里的数字不能靠人记）
    try:
        with open(os.path.join(HERE, "_last_report.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"scripts": len(files), "items": items_total,
                       "per_script": per_script,
                       "failed": failed, "elapsed": elapsed,
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      fh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    doc_n = doc_item_count()
    if doc_n is not None and doc_n != items_total:
        print(f"⚠️ 文档写的是 {doc_n} 项，实测 {items_total} 项"
              f" —— 该更新 docs/40 §4")
    if failed:
        print(f"❌ 失败 {len(failed)} 个：{', '.join(failed)}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
