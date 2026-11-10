# coding=utf-8
"""JSONPatch（RFC 6902 子集 + MVU 的 `delta` 扩展）。

对应决策记录 §2：MVU 让模型在回复末尾输出**本次实际变化**的 patch 数组，
扩展负责 apply。这条设计比"每轮重写整份状态"好一个数量级 ——
token 少、而且不会因为漏抄一个字段就把状态抹掉。

**本项目保留这条设计，但换了"谁算数"**：
    MVU：      模型算 → 模型写 patch → 扩展落库     （模型说了算）
    本项目：   模型出意图 → **代码算** → 代码写 patch → 落库 （代码说了算）
所以这个模块的调用方**主要是引擎自己**，而不是模型。模型只在"填表/低频"场景直接产 patch。

支持的 op：
    add / insert / replace / remove / move / copy / test / delta
其中：
    · `insert` 是 `add` 的别名（MVU 世界书里用的是 insert）
    · `delta` 是本项目/MVU 的扩展：**对已有数值做增减**，不要求先读到旧值
    · `test` 用于乐观锁（apply 前确认状态没被别处改过）

⚠️ 三条硬口径（都来自那张角色卡的变量更新规则）：
    1. **delta 只用于数字**，不用于文字或"分数的字符串"；
    2. **缺父对象时不允许自动造** —— 要么显式 insert 父对象，要么整个 patch 失败并回滚；
    3. **下划线开头的字段是只读的**（界面配置、备份、脚本），patch 不许碰。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PatchError", "PatchOpError", "Patch", "Op", "apply_patch",
    "parse_path", "escape_token", "unescape_token", "diff",
    "READONLY_PREFIX",
]

#: 下划线开头的键视为只读（MVU 世界书的规定）
READONLY_PREFIX = "_"


class PatchError(Exception):
    """patch 整体失败（apply 会回滚，状态保持原样）。"""


class PatchOpError(PatchError):
    """某一条 op 失败。带 op 下标，方便定位是哪一条。"""

    def __init__(self, index: int, op: str, msg: str):
        self.index = index
        self.op = op
        self.msg = msg
        super().__init__(f"第 {index} 条 op（{op}）失败：{msg}")


# --------------------------------------------------------------------------- #
# JSON Pointer
# --------------------------------------------------------------------------- #

def escape_token(tok: str) -> str:
    """`~` → `~0`，`/` → `~1`（RFC 6901 的顺序不能反）。"""
    return str(tok).replace("~", "~0").replace("/", "~1")


def unescape_token(tok: str) -> str:
    """`~1` → `/`，`~0` → `~`（**顺序要跟 escape 相反**）。"""
    return str(tok).replace("~1", "/").replace("~0", "~")


def parse_path(path: str) -> list[str]:
    """把 `/角色列表/艾尔玛/生命值/当前` 拆成 ["角色列表","艾尔玛","生命值","当前"]。

    ⚠️ 路径**相对于 stat_data**，不带 `/stat_data` 前缀（MVU 的规定）。
    """
    if path is None:
        raise PatchError("path 不能为空")
    p = str(path)
    if p == "":
        return []
    if not p.startswith("/"):
        raise PatchError(f"path 必须以 / 开头（相对 stat_data）：{path!r}")
    return [unescape_token(t) for t in p[1:].split("/")]


def _join(path: list[str]) -> str:
    return "/" + "/".join(escape_token(t) for t in path) if path else ""


def _check_readonly(path: list[str], *, op: str, index: int) -> None:
    for tok in path:
        if tok.startswith(READONLY_PREFIX):
            raise PatchOpError(index, op,
                               f"路径含只读字段 {tok!r}（下划线开头，patch 不许碰）")


# --------------------------------------------------------------------------- #
# op
# --------------------------------------------------------------------------- #

@dataclass
class Op:
    op: str
    path: str
    value: Any = None
    from_: str = ""          # `move` / `copy` 的源路径（JSON 里写 "from"）
    #: JSON 里到底有没有 `value` 键（`value:null` 与"没写"是两件事）
    has_value: bool = True

    @classmethod
    def from_dict(cls, d: dict | "Op") -> "Op":
        if isinstance(d, Op):
            return d
        if not isinstance(d, dict):
            raise PatchError(f"每条 op 必须是对象，收到 {type(d).__name__}")
        if "op" not in d:
            raise PatchError(f"op 缺 'op' 字段：{d}")
        if "path" not in d:
            raise PatchError(f"op 缺 'path' 字段：{d}")
        return cls(op=str(d["op"]).lower(), path=d["path"],
                   value=d.get("value"), from_=d.get("from", ""),
                   has_value=("value" in d))

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"op": self.op, "path": self.path}
        if self.op in ("add", "insert", "replace", "delta", "test"):
            d["value"] = self.value
        if self.op in ("move", "copy"):
            d["from"] = self.from_
        return d


#: 允许的 op 名
OPS = ("add", "insert", "replace", "remove", "move", "copy", "test", "delta")

#: op 是否需要一个 value
NEEDS_VALUE = ("add", "insert", "replace", "delta", "test")


Patch = list  # 语义别名：一个 patch 就是一个 Op 列表


# --------------------------------------------------------------------------- #
# 取值 / 赋值
# --------------------------------------------------------------------------- #

def _get(doc: Any, path: list[str], *, index: int, op: str) -> Any:
    cur = doc
    for i, tok in enumerate(path):
        if isinstance(cur, dict):
            if tok not in cur:
                raise PatchOpError(index, op,
                                   f"路径不存在：{_join(path[:i + 1])}")
            cur = cur[tok]
        elif isinstance(cur, list):
            if tok == "-":
                raise PatchOpError(index, op, "不能在取值的路径里用 '-'")
            try:
                cur = cur[int(tok)]
            except (ValueError, IndexError):
                raise PatchOpError(index, op,
                                   f"数组下标非法：{_join(path[:i + 1])}") from None
        else:
            raise PatchOpError(index, op,
                               f"{_join(path[:i])} 是 {type(cur).__name__}，不能往里走")
    return cur


def _parent(doc: Any, path: list[str], *, index: int, op: str) -> tuple[Any, str]:
    """返回 (父容器, 最后一个 token)。**父对象不存在就报错，不自动造。**"""
    if not path:
        raise PatchOpError(index, op, "空路径没有父容器（不能用整份替换）")
    return _get(doc, path[:-1], index=index, op=op), path[-1]


def _set_child(container: Any, tok: str, value: Any, *, index: int, op: str,
               allow_append: bool = False) -> None:
    if isinstance(container, dict):
        container[tok] = value
    elif isinstance(container, list):
        if tok == "-":
            if not allow_append:
                raise PatchOpError(index, op, "这个 op 不允许用 '-' 追加")
            container.append(value)
            return
        try:
            i = int(tok)
        except ValueError:
            raise PatchOpError(index, op, f"数组下标非法：{tok!r}") from None
        if i == len(container) and allow_append:
            container.append(value)
            return
        if not (0 <= i < len(container)):
            raise PatchOpError(index, op, f"数组下标越界：{i}（长度 {len(container)}）")
        container[i] = value
    else:
        raise PatchOpError(index, op,
                           f"不能在 {type(container).__name__} 上设置 {tok!r}")


def _del_child(container: Any, tok: str, *, index: int, op: str) -> None:
    if isinstance(container, dict):
        if tok not in container:
            raise PatchOpError(index, op, f"要删的键不存在：{tok!r}")
        del container[tok]
    elif isinstance(container, list):
        try:
            i = int(tok)
        except ValueError:
            raise PatchOpError(index, op, f"数组下标非法：{tok!r}") from None
        if not (0 <= i < len(container)):
            raise PatchOpError(index, op, f"数组下标越界：{i}")
        container.pop(i)
    else:
        raise PatchOpError(index, op, f"不能从 {type(container).__name__} 删东西")


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def apply_patch(doc: Any, patch: Patch, *, in_place: bool = False,
                strict: bool = True) -> Any:
    """把 patch 应用到 doc 上。

    **事务语义**：任何一条 op 失败 → **整份 patch 回滚**，doc 保持调用前的样子。
    这很重要：一次回复里的 patch 是一个整体（"扣了法术位又打死怪"不该只生效一半）。

    `in_place=True` 时会**先存一份备份**，失败就整体还原 —— 否则"改了一半再报错"
    会把状态留在既不是改前也不是改后的中间态上（这个 bug 回归 C4 钉住）。

    `strict=False` 时跳过不认识的 op（用于容忍模型多写字段的场景），
    但**默认 strict=True** —— 静默跳过错误会让状态悄悄跑偏。
    """
    work = doc if in_place else copy.deepcopy(doc)
    #: in_place 的还原点。不是 in_place 时不需要（我们改的是副本）。
    backup = copy.deepcopy(doc) if in_place else None

    ops: list[Op] = []
    if patch is None:
        return work
    if not isinstance(patch, list):
        raise PatchError(f"patch 必须是数组，收到 {type(patch).__name__}")
    for d in patch:
        ops.append(Op.from_dict(d))

    try:
        for i, o in enumerate(ops):
            if o.op not in OPS:
                if strict:
                    raise PatchOpError(i, o.op,
                                       f"不认识的 op（支持：{', '.join(OPS)}）")
                continue
            # ⚠️ 缺 `value` 与 `value:null` 都不许**静默写 None**：
            #    旧代码直接把 None 写进生命值/法术位，之后 `hp()` 在很远的地方崩。
            if o.op in NEEDS_VALUE:
                if not o.has_value:
                    raise PatchOpError(i, o.op,
                                       "缺少 value（add/insert/replace 不允许省略）")
                if o.value is None:
                    raise PatchOpError(i, o.op,
                                       "value 不能是 null（状态树不允许空值）")
            _apply_one(work, o, i, strict=strict)
    except PatchError:
        if in_place and backup is not None:
            _restore(doc, backup)
        raise
    return work


def _restore(doc: Any, backup: Any) -> None:
    """就地还原：**必须保住原来的容器身份**，调用方持有的引用要还能用。"""
    if isinstance(doc, dict) and isinstance(backup, dict):
        doc.clear()
        doc.update(backup)
    elif isinstance(doc, list) and isinstance(backup, list):
        doc[:] = backup
    else:
        # 顶层不是容器（理论上不该走到，因为 op 必然要进容器）——
        # 明确报错，不要静默什么都不做。
        raise PatchError("无法还原：根不是可变的 dict / list")


def _is_own_subtree(src: list[str], dst: list[str]) -> bool:
    """`dst` 是否在 `src` 的子树里（含相等）。"""
    return src == dst[:len(src)]


def _apply_one(doc: Any, o: Op, i: int, *, strict: bool) -> None:
    path = parse_path(o.path)
    _check_readonly(path, op=o.op, index=i)

    if o.op == "test":
        got = _get(doc, path, index=i, op=o.op)
        if got != o.value:
            raise PatchOpError(i, o.op,
                               f"test 失败：期望 {o.value!r}，实际 {got!r}")
        return

    if o.op in ("add", "insert"):
        parent, tok = _parent(doc, path, index=i, op=o.op)
        if isinstance(parent, dict) and tok in parent:
            # MVU 的 insert 是"新增"；键已存在时 JSONPatch 的 add 是覆盖。
            # 这里按 JSONPatch 语义（覆盖），因为"insert 已存在的键"在 MVU 里
            # 表示"更新这个角色"——两边语义其实一致。
            pass
        _set_child(parent, tok, copy.deepcopy(o.value), index=i, op=o.op,
                   allow_append=True)
        return

    if o.op == "replace":
        _get(doc, path, index=i, op=o.op)          # 必须先存在
        parent, tok = _parent(doc, path, index=i, op=o.op)
        _set_child(parent, tok, copy.deepcopy(o.value), index=i, op=o.op)
        return

    if o.op == "remove":
        parent, tok = _parent(doc, path, index=i, op=o.op)
        _del_child(parent, tok, index=i, op=o.op)
        return

    if o.op == "move":
        src = parse_path(o.from_)
        dst = list(path)
        _check_readonly(src, op=o.op, index=i)
        # ⚠️ 目标在源子树里（/a → /a/b）时，删掉源之后写入的是已脱离文档的孤儿 dict，
        #    patch 会"成功"但整棵源丢失。JSON Patch 对此明确禁止，这里也拒绝。
        if _is_own_subtree(src, dst):
            raise PatchOpError(
                i, o.op,
                f"不能把 {_join(src)!r} 移进它自己的子树 {_join(dst)!r}")
        val = copy.deepcopy(_get(doc, src, index=i, op=o.op))
        # 先删后加（顺序不能反：目标是源的子路径时反了会出错）
        p_parent, p_tok = _parent(doc, path, index=i, op=o.op)
        s_parent, s_tok = _parent(doc, src, index=i, op=o.op)
        _del_child(s_parent, s_tok, index=i, op=o.op)
        _set_child(p_parent, p_tok, val, index=i, op=o.op, allow_append=True)
        return

    if o.op == "copy":
        src = parse_path(o.from_)
        val = copy.deepcopy(_get(doc, src, index=i, op=o.op))
        parent, tok = _parent(doc, path, index=i, op=o.op)
        _set_child(parent, tok, val, index=i, op=o.op, allow_append=True)
        return

    if o.op == "delta":
        cur = _get(doc, path, index=i, op=o.op)
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            raise PatchOpError(
                i, o.op,
                f"delta 只能改数字，{_join(path)} 现在是 "
                f"{type(cur).__name__}（规则：delta 不用于文字或分数字符串）")
        if isinstance(o.value, bool) or not isinstance(o.value, (int, float)):
            raise PatchOpError(i, o.op, f"delta 的值必须是数字，收到 {o.value!r}")
        parent, tok = _parent(doc, path, index=i, op=o.op)
        # 整数保持整数（避免 12 - 8 变成 4.0 这种）
        newv = cur + o.value
        if isinstance(cur, int) and isinstance(o.value, int):
            newv = int(newv)
        _set_child(parent, tok, newv, index=i, op=o.op)
        return

    raise PatchOpError(i, o.op, "未实现的 op")


# --------------------------------------------------------------------------- #
# diff（生成 patch 用；引擎自己改完状态后可以据此记账）
# --------------------------------------------------------------------------- #

def diff(before: Any, after: Any, *, path: str = "") -> list[dict]:
    """比较两份状态，生成一份 patch。

    优先用 `delta` 表达数字变化（省 token，也让"只写变化"这条纪律落到实机上）。
    """
    out: list[dict] = []
    _diff_into(before, after, path, out)
    return out


def _diff_into(a: Any, b: Any, path: str, out: list[dict]) -> None:
    if a == b:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}/{escape_token(k)}"
            if k not in b:
                out.append({"op": "remove", "path": p})
            elif k not in a:
                out.append({"op": "insert", "path": p, "value": b[k]})
            else:
                _diff_into(a[k], b[k], p, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                _diff_into(x, y, f"{path}/{i}", out)
            return
        out.append({"op": "replace", "path": path, "value": b})
        return
    # 数字 → delta（更省 token，也更能表达"变化了多少"）
    if (isinstance(a, (int, float)) and isinstance(b, (int, float))
            and not isinstance(a, bool) and not isinstance(b, bool)):
        out.append({"op": "delta", "path": path, "value": b - a})
        return
    out.append({"op": "replace", "path": path, "value": b})
