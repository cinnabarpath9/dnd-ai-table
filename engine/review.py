# coding=utf-8
"""存档复盘的数据层（网页与 CLI 共用）。

两级口径（照狼人杀的做法）：
    ① `lite_rows()` —— 列表只要的小东西：座位/任务/token/耗时/成败 + 思考链**长度**；
    ② `full_row()` —— 点开某一条才取思考链与 prompt 全文。

⚠️ 全文含**剧透**（DM 私有信息），不要发给玩家座位。
"""
from __future__ import annotations

import json
import os

from .persist import calls_path_for

#: 列表里保留的字段（都是数字/短串）
LITE_KEYS = ("seq", "actor", "role", "task", "model", "prompt_tokens",
             "completion_tokens", "cached_tokens", "elapsed", "ok", "error")


def list_games(save_dir: str) -> list[dict]:
    """存档目录里的所有主存档（按修改时间新→旧）。坏文件跳过，不中断。"""
    out = []
    try:
        names = os.listdir(save_dir)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name.endswith(".calls.jsonl"):
            continue
        path = os.path.join(save_dir, name)
        if not os.path.isfile(path):
            continue
        calls = calls_for(path)
        out.append({
            "name": name,
            "mtime": os.path.getmtime(path),
            "calls": len(calls),
            "has_calls": os.path.exists(calls_path_for(path)),
            "size": os.path.getsize(path),
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def calls_for(save_path: str) -> list[dict]:
    """读旁挂 `*.calls.jsonl`；没有就退回主存档里的元数据（无思考链）。"""
    cp = calls_path_for(save_path)
    if os.path.exists(cp):
        rows = []
        try:
            with open(cp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return rows
    try:
        with open(save_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return []
    return [dict(x) for x in ((doc.get("client") or {}).get("calls") or [])
            if isinstance(x, dict)]


def lite_rows(rows: list[dict]) -> list[dict]:
    """列表口径：剥掉 reasoning / prompt，只留长度。"""
    out = []
    for row in rows:
        item = {k: row.get(k) for k in LITE_KEYS}
        item["error"] = str(item.get("error") or "")[:80]
        item["reasoning_len"] = int(row.get("reasoning_len")
                                    or len(str(row.get("reasoning") or "")))
        item["prompt_chars"] = int(row.get("prompt_chars")
                                   or len(str(row.get("prompt") or "")))
        out.append(item)
    return out


def full_row(rows: list[dict], index: int) -> dict | None:
    """取某一条的全文（思考链 + prompt）。越界返回 None，不猜。"""
    if not isinstance(index, int) or index < 0 or index >= len(rows):
        return None
    row = dict(rows[index])
    row["reasoning"] = str(row.get("reasoning") or "")
    row["prompt"] = str(row.get("prompt") or "")
    row["reasoning_len"] = len(row["reasoning"])
    row["prompt_chars"] = len(row["prompt"])
    return row


def resolve(save_dir: str, name: str) -> str:
    """把外部传来的存档名收成一个**合法路径**；不合法返回空串。

    只认一个路径段、只认 `.json`、必须真的在这个目录里。
    """
    name = str(name or "")
    if not name or os.path.basename(name) != name or name.startswith("."):
        return ""
    if not name.endswith(".json") or name.endswith(".calls.jsonl"):
        return ""
    path = os.path.abspath(os.path.join(save_dir, name))
    root = os.path.abspath(save_dir)
    try:
        if os.path.commonpath([root, path]) != root:
            return ""
    except ValueError:
        return ""
    return path if os.path.isfile(path) else ""