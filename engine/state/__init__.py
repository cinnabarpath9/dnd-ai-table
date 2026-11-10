# coding=utf-8
"""状态层：`stat_data` 状态树 + JSONPatch + 可见性过滤。

对应决策记录 §5.12.1 的 M2（状态存储）与 M3（可见性装配器）。

**这一层不调模型。** 模型只在"填表/低频"场景直接产出 JSONPatch；
战斗与日常的每一次数值改动都由引擎算出 patch 再落库。

    patch.py       JSONPatch + MVU 的 delta 扩展（含事务回滚）
    tree.py        stat_data 状态树（MVU 那棵树的结构）+ 审计
    visibility.py  三档可见性 + 泄漏检查
    assembler.py   上下文装配器（把状态+台账按视角拼成给模型的块）
    world.py       世界状态（地点/派系/任务/时间，按视角过滤）
"""

from .patch import PatchError, Op, apply_patch, diff, parse_path
from .tree import (
    CHARACTER_TEMPLATE, EMPTY_STAT_DATA, AuditEntry, StateError, StateTree,
    init_from_template, new_character,
)
from .visibility import (
    DM, PUBLIC, SECTION_ORDER, Entry, Ledger, Visibility, lint_leaks, pc,
)
from .world import World, WorldError, new_world

__all__ = [
    "StateTree", "StateError", "AuditEntry", "CHARACTER_TEMPLATE",
    "EMPTY_STAT_DATA", "init_from_template", "new_character",
    "Op", "PatchError", "apply_patch", "diff", "parse_path",
    "Visibility", "Entry", "Ledger", "SECTION_ORDER", "lint_leaks",
    "PUBLIC", "DM", "pc", "World", "WorldError", "new_world",
]
