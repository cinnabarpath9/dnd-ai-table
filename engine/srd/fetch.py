# coding=utf-8
"""SRD 5.2.1（D&D 2024）数据采集层。

数据来源：open5e v2 API 的 `srd-2024` 文档（System Reference Document 5.2，
Wizards of the Coast 在 CC-BY-4.0 下发布；open5e 是第三方镜像）。

为什么用 API 而不是 GitHub 上的现成 JSON：
    本机网络环境访问 github.com / raw.githubusercontent.com / cdn.jsdelivr.net 全部失败，
    只有 api.open5e.com 与 www.dnd5eapi.co 可达。open5e v2 的 `srd-2024` 就是 2024 版 SRD。

落盘口径（照抄狼人杀项目的纪律）：
    · `raw/` = 原样快照，**只增不改**，用于「数据变了没有」的对账与离线复算；
    · `index.json` = 精简后的查表索引（key → 记录），引擎只读它；
    · 每个资源记 来源 URL / 条数 / sha256 / 抓取时间，写进 `manifest.json`；
    · **断点续抓**：已完成的资源默认跳过，`--force` 才重抓。

跑法：
    python -m engine.srd.fetch                 # 增量抓取（已有就跳过）
    python -m engine.srd.fetch --force         # 全量重抓
    python -m engine.srd.fetch --only creatures spells
    python -m engine.srd.fetch --list          # 只看清单，不抓
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

API = "https://api.open5e.com/v2"
DOC = "srd-2024"
DOC_QUERY = f"document__key={DOC}"
PAGE = 200          # 每页条数（open5e 上限内取一个稳的值）
TIMEOUT = 60
RETRIES = 3
RETRY_SLEEP = 2.0
UA = "dnd-ai-table/0.1 (P1 data fetcher; local cache)"

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RAW = os.path.join(DATA, "raw")

#: 要抓的资源。`doc=True` 表示支持 document__key 过滤。
RESOURCES: dict[str, dict] = {
    "creatures":        {"doc": True,  "why": "怪物数值块（判定与敌对 AI 的输入）"},
    "spells":           {"doc": True,  "why": "法术（施法判定与后果）"},
    "magicitems":       {"doc": True,  "why": "魔法物品"},
    "weapons":          {"doc": True,  "why": "武器（攻击判定用）"},
    "armor":            {"doc": True,  "why": "护甲（AC 计算用）"},
    "classes":          {"doc": True,  "why": "职业（等级/熟练/特性）"},
    "feats":            {"doc": True,  "why": "专长"},
    "species":          {"doc": True,  "why": "种族"},
    "backgrounds":      {"doc": True,  "why": "背景"},
    "rules":            {"doc": True,  "why": "规则条文（判定口径的权威文本）"},
    "rulesets":         {"doc": True,  "why": "规则章节目录"},
    "weaponproperties": {"doc": True,  "why": "武器属性（灵巧/重型/触及…）"},
    "skills":           {"doc": True,  "why": "技能 → 属性映射"},
    "items":            {"doc": True,  "why": "装备"},
    # 下面这些 srd-2024 下是空的，留给引擎内置常量（见 engine.rules.constants）
    "conditions":       {"doc": True,  "why": "（srd-2024 为空 → 引擎内置）"},
    "damagetypes":      {"doc": True,  "why": "（srd-2024 为空 → 引擎内置）"},
    "sizes":            {"doc": True,  "why": "（srd-2024 为空 → 引擎内置）"},
    "languages":        {"doc": True,  "why": "（srd-2024 为空 → 引擎内置）"},
    "abilities":        {"doc": True,  "why": "（srd-2024 为空 → 引擎内置）"},
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _http_json(url: str) -> dict:
    last = None
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:      # 4xx 重试没意义，但 429 有意义
            last = f"HTTP {exc.code}"
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as exc:                    # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if attempt < RETRIES:
            time.sleep(RETRY_SLEEP * attempt)
    raise RuntimeError(f"抓取失败（试了 {RETRIES} 次）：{url} —— 最后一次 {last}")


def fetch_resource(name: str, use_doc: bool) -> tuple[list[dict], list[str]]:
    """抓一个资源的全部分页。返回 (记录列表, 出现过的 URL 列表)。"""
    base = f"{API}/{name}/?"
    if use_doc:
        base += DOC_QUERY + "&"
    url = f"{base}limit={PAGE}"
    out: list[dict] = []
    urls: list[str] = []
    while url:
        urls.append(url)
        page = _http_json(url)
        results = page.get("results")
        if results is None:
            raise RuntimeError(f"{name} 的响应里没有 results：{list(page)[:10]}")
        out.extend(results)
        nxt = page.get("next")
        # open5e 的 next 是绝对 URL；把它对齐到我们自己的分页大小
        if nxt:
            if f"limit={PAGE}" not in nxt:
                sep = "&" if "?" in nxt else "?"
                nxt = f"{nxt}{sep}limit={PAGE}"
            url = nxt
        else:
            url = None
    return out, urls


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #

def _write_json(path: str, obj) -> str:
    """原子写 + 返回 sha256。临时文件写一半被打断不会污染旧数据。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    return hashlib.sha256(blob).hexdigest()


def _read_manifest() -> dict:
    p = os.path.join(DATA, "manifest.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    return {}


def build_index(name: str, records: list[dict]) -> dict:
    """精简成引擎用的查表索引。key → 记录，另外给几个常用快速索引。"""
    by_key: dict[str, dict] = {}
    by_name: dict[str, str] = {}
    for rec in records:
        k = rec.get("key")
        if not k:
            continue
        by_key[k] = rec
        nm = rec.get("name")
        if nm and nm not in by_name:      # 重名以第一个为准（并记进 duplicates）
            by_name[nm] = k

    dupes = sorted({r["name"] for r in records if r.get("name")}
                   - {v for v in []} ) if False else None  # 占位：重名检测在下面单独算
    seen: dict[str, int] = {}
    for rec in records:
        nm = rec.get("name")
        if nm:
            seen[nm] = seen.get(nm, 0) + 1
    duplicates = sorted(n for n, c in seen.items() if c > 1)

    idx = {"key": name, "document": DOC, "count": len(records),
           "by_key": by_key, "by_name": by_name}
    if duplicates:
        idx["duplicate_names"] = duplicates
    return idx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓取 SRD 5.2.1（D&D 2024）到本地")
    ap.add_argument("--only", nargs="*", default=None, help="只抓这些资源")
    ap.add_argument("--force", action="store_true", help="已有也重抓")
    ap.add_argument("--list", action="store_true", help="只列清单")
    args = ap.parse_args(argv)

    if args.list:
        print(f"{'资源':<20}{'document 过滤':<16}为什么")
        print("-" * 90)
        for name, meta in RESOURCES.items():
            print(f"{name:<20}{str(meta['doc']):<16}{meta['why']}")
        return 0

    todo = args.only or list(RESOURCES)
    unknown = [t for t in todo if t not in RESOURCES]
    if unknown:
        print(f"❌ 不认识的资源：{unknown}")
        print(f"   可选：{', '.join(RESOURCES)}")
        return 2

    os.makedirs(RAW, exist_ok=True)
    manifest = _read_manifest()
    manifest.setdefault("document", DOC)
    manifest.setdefault("resources", {})

    print(f"数据源：{API}/  文档：{DOC}")
    print(f"落盘：{DATA}")
    print("=" * 90)

    total_records = 0
    for name in todo:
        meta = RESOURCES[name]
        raw_path = os.path.join(RAW, f"{name}.json")
        idx_path = os.path.join(DATA, f"{name}.index.json")
        already = os.path.exists(idx_path) and os.path.exists(raw_path)

        if already and not args.force:
            rec = manifest["resources"].get(name, {})
            print(f"  ⏭  {name:<20} 已有（{rec.get('count', '?')} 条，跳过）")
            total_records += int(rec.get("count") or 0)
            continue

        try:
            records, urls = fetch_resource(name, meta["doc"])
        except Exception as exc:                    # noqa: BLE001
            print(f"  ❌ {name:<20} {exc}")
            return 1

        raw_sha = _write_json(raw_path, records)
        idx = build_index(name, records)
        idx_sha = _write_json(idx_path, idx)

        empty = len(records) == 0
        manifest["resources"][name] = {
            "count": len(records),
            "urls": urls,
            "raw_sha256": raw_sha,
            "index_sha256": idx_sha,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "empty": empty,
            "duplicate_names": idx.get("duplicate_names", []),
        }
        total_records += len(records)
        flag = "（空 —— 该资源在 srd-2024 下没有数据，引擎用内置常量）" if empty else ""
        print(f"  ✅ {name:<20} {len(records):>5} 条{flag}")

    manifest["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_json(os.path.join(DATA, "manifest.json"), manifest)

    print("=" * 90)
    print(f"完成：{len(todo)} 个资源，合计 {total_records} 条记录。")
    empties = [n for n, r in manifest["resources"].items() if r.get("empty")]
    if empties:
        print(f"⚠️ 空的资源（需要引擎内置常量）：{', '.join(empties)}")
    print(f"清单：{os.path.join(DATA, 'manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
