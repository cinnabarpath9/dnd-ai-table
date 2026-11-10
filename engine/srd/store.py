# coding=utf-8
"""SRD 数据的只读查表接口。

引擎**只读** `*.index.json`（由 `fetch.py` 生成），不碰网络、不碰 raw 快照。
查不到就抛 `NotFound` —— 不要静默返回 None，那会让"规则没覆盖"伪装成"单位不存在"。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

__all__ = ["NotFound", "Store", "get_store", "DATA_DIR", "available"]

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")


class NotFound(KeyError):
    """查不到就抛，别静默返回 None。"""


class Store:
    """按资源名查 SRD 记录。"""

    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        if not os.path.isdir(data_dir):
            raise FileNotFoundError(
                f"没有 SRD 数据目录：{data_dir}\n"
                f"先跑一次采集：python -m engine.srd.fetch"
            )
        self._cache: dict[str, dict] = {}

    # ---- 装载 ----
    def _load(self, resource: str) -> dict:
        if resource in self._cache:
            return self._cache[resource]
        path = os.path.join(self.data_dir, f"{resource}.index.json")
        if not os.path.exists(path):
            raise NotFound(
                f"没有这个资源：{resource}（缺 {path}）\n"
                f"可选：{', '.join(self.available())}"
            )
        with open(path, encoding="utf-8") as fh:
            idx = json.load(fh)
        self._cache[resource] = idx
        return idx

    def available(self) -> list[str]:
        out = []
        for fn in sorted(os.listdir(self.data_dir)):
            if fn.endswith(".index.json"):
                out.append(fn[: -len(".index.json")])
        return out

    # ---- 查询 ----
    def count(self, resource: str) -> int:
        return int(self._load(resource).get("count", 0))

    def all_keys(self, resource: str) -> list[str]:
        return sorted(self._load(resource)["by_key"].keys())

    def by_key(self, resource: str, key: str) -> dict:
        """按 open5e 的 key 查（如 'srd-2024_goblin'）。接受带/不带前缀。"""
        idx = self._load(resource)
        rec = idx["by_key"].get(key)
        if rec is None and not key.startswith("srd-2024_"):
            rec = idx["by_key"].get(f"srd-2024_{key}")
        if rec is None:
            raise NotFound(f"{resource} 里没有 key={key!r}")
        return rec

    def by_name(self, resource: str, name: str, *, exact: bool = True) -> dict:
        """按名称查。`exact=False` 时做大小写无关的部分匹配（唯一命中才算）。"""
        idx = self._load(resource)
        if exact:
            k = idx["by_name"].get(name)
            if k is None:
                k = idx["by_name"].get(name.strip())
            if k is None:
                raise NotFound(f"{resource} 里没有 name={name!r}")
            return idx["by_key"][k]

        needle = name.strip().lower()
        hits = [k for nm, k in idx["by_name"].items() if needle in nm.lower()]
        if len(hits) == 1:
            return idx["by_key"][hits[0]]
        if not hits:
            raise NotFound(f"{resource} 里没有名字含 {name!r} 的记录")
        names = [idx["by_key"][k]["name"] for k in hits[:8]]
        raise NotFound(f"{resource} 里名字含 {name!r} 的有 {len(hits)} 条，"
                       f"说不清要哪个：{names}")

    def find(self, resource: str, name: str) -> list[dict]:
        """模糊找，返回列表（给"让模型/人挑"的场景用，不抛异常）。"""
        idx = self._load(resource)
        needle = name.strip().lower()
        out = []
        for nm, k in idx["by_name"].items():
            if needle in nm.lower():
                out.append(idx["by_key"][k])
        return out

    def records(self, resource: str) -> list[dict]:
        return list(self._load(resource)["by_key"].values())


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store()


def available() -> list[str]:
    return get_store().available()
