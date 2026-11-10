# coding=utf-8
"""座位层：把"一张桌子"拆成几个座位，每个座位一个模型客户端。

为什么单独一层（决策记录 §5.22）：

    · DM 是**一个头脑**                         → 一个 dm 座位
    · 敌方是**一次调用规划所有敌人**（D17）       → 一个 planner 座位
    · 友方是**每个角色各自决策、不合并**（D18）    → 每个 PC 一个座位
    · 真人玩家**没有模型**                       → 一个和模型接口相同的座位

**真人座位存在的意义**：`GameLoop` 只认 `ModelClient` 这个接口。
真人坐进来之后，循环、装配器、菜单、可见性、记账**一行都不用改** ——
"人"和"模型"在引擎眼里是同一件事：一个会给出意图的座位。

纪律：

    1. 分发**只发生在** `TableClient._complete()` 里，别处不许绕；
    2. 每个座位仍然各自记一笔账（谁花了多少 token 能分开看），
       牌桌再记一笔总账（`GameLoop.report()` 用的就是它）；
    3. **没配的座位要明说**，不许静默降级成 DM 或别的座位 ——
       座位串味是那种"看起来在跑、其实在替别人做决定"的错。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .client import ModelClient, ModelError, ModelReply, ModelRequest

__all__ = ["TableClient", "HumanSeat"]


# --------------------------------------------------------------------------- #
# 牌桌：按座位分发
# --------------------------------------------------------------------------- #

class TableClient(ModelClient):
    """按座位分发请求：`dm` / `planner` / 每个 PC 各自一个客户端。

    ⚠️ 这一层**自己不产生任何回应**，只做路由 + 总账。
    """

    def __init__(self, *, dm: ModelClient | None = None,
                 planner: ModelClient | None = None,
                 players: dict[str, ModelClient] | None = None,
                 default: ModelClient | None = None,
                 pilot: ModelClient | None = None,
                 name: str = "table"):
        super().__init__(name=name, enabled=True)
        self.dm = dm
        # 敌方规划默认和 DM 同一个头脑（第一版就是同一个模型）；
        # 想分开配（比如 DM 用 Gemini、规划用 Haiku）时显式传 planner。
        self.planner = planner
        self.players = dict(players or {})
        self.default = default
        #: 托管座位（真模型代打真人角色）。**不参与 seat_for**，只走 complete_pilot。
        self.pilot = pilot

    # ---------- 座位 ----------

    def seats(self) -> dict[str, ModelClient]:
        """所有配好的座位（标签 → 客户端）。测试与记账用。"""
        out: dict[str, ModelClient] = {}
        if self.dm is not None:
            out["dm"] = self.dm
        if self.planner is not None:
            out["planner"] = self.planner
        for who, cli in self.players.items():
            out[f"pc:{who}"] = cli
        if self.pilot is not None:
            out["pilot"] = self.pilot
        if self.default is not None:
            out["default"] = self.default
        return out

    def seat_for(self, req: ModelRequest) -> ModelClient:
        """这个请求该由哪个座位回答。**找不到就抛，不猜。**"""
        if req.role == "dm":
            seat = self.dm or self.default
        elif req.role == "planner":
            seat = self.planner or self.dm or self.default
        elif req.role == "player":
            seat = self.players.get(req.actor) or self.default
        else:
            # 不认识的 role 不静默串味给 DM —— 显式报错，避免新角色类型被旧逻辑接管。
            raise ModelError(
                f"不认识的 role={req.role!r}（只支持 dm / planner / player）")
        if seat is None:
            have = sorted(self.seats())
            raise ModelError(
                f"没有为 {req.role}/{req.actor} 配座位（现有座位：{have or '无'}）")
        return seat

    # ---------- 唯一出口 ----------

    def _complete(self, req: ModelRequest) -> ModelReply:
        seat = self.seat_for(req)
        reply = seat.complete(req)
        if not reply.model:
            reply.model = getattr(seat, "name", "") or ""
        return reply

    def complete_pilot(self, req: ModelRequest) -> ModelReply:
        """托管座位出口：走真模型，但**记进牌桌总账**、也发思考提示。

        和 `complete()` 的区别：不按 role 路由（托管只在 PC 意图上被调用），
        并且把 pilot 的这次 RecordedCall 追加进牌桌 `calls`，否则自检/复盘会少算。
        """
        if self.pilot is None:
            raise ModelError("这一桌没有配托管座位（pilot）")
        if self.on_call is not None:
            self.on_call(req.actor, req.task, "start")
        try:
            reply = self.pilot.complete(req)
        finally:
            if self.on_call is not None:
                self.on_call(req.actor, req.task, "end")
        if not reply.model:
            reply.model = getattr(self.pilot, "name", "") or ""
        if self.pilot.calls:
            self.calls.append(self.pilot.calls[-1])
        return reply

    # ---------- 记账 ----------

    def seat_summary(self) -> str:
        """每个座位各花了多少。**分开看才知道钱花在谁身上。**"""
        lines = []
        for label, cli in self.seats().items():
            t = cli.totals()
            lines.append(
                f"{label:<14} {getattr(cli, 'name', '') or '(未命名)':<18} "
                f"{t['calls']:>4} 次｜入 {t['prompt_tokens']:>8,}｜"
                f"出 {t['completion_tokens']:>7,}｜失败 {t['failed']}")
        return "\n".join(lines) if lines else "（一个座位都没配）"


# --------------------------------------------------------------------------- #
# 真人座位
# --------------------------------------------------------------------------- #

#: 探索轮的选项没有 `slot` 键；战斗菜单的每一项都有（`Option.to_dict()`）。
#: 用这个区别来选输出 schema —— 比"猜 kind 的名字"可靠。
_COMBAT_KEYS = ("slot", "requires_target")


class HumanSeat(ModelClient):
    """真人玩家坐进引擎的座位。

    它把"菜单 → 人 → 意图"这一段补上，**对 `GameLoop` 而言和模型座位没有区别**。

    输入约定（一行）：

        <选项 id>                     例：`look`
        <选项 id> <台词>               例：`look 我贴着门听一下`
        <选项 id> <目标> [台词]        例：`a3 E1 我劈他`（只有需要目标的选项才要目标）

    支持中文逗号/冒号分隔（`a3，E1`），`?` 重印菜单（不算一次尝试）。

    ⚠️ 三次都没给出合法输入 → 返回 `ok=False`，交给循环的本地兜底。
       **不假装人给了答案** —— 否则会变成"引擎替人做了决定"。
    """

    #: 显式身份标记。`_seat_is_human` 以前靠类名 `"HumanSeat"`，子类会漏。
    is_human = True

    def __init__(self, *, name: str = "human",
                 ask: Callable[[str], str] | None = None,
                 auto: list[str] | Callable[[ModelRequest], str] | None = None,
                 show_context: bool = False,
                 max_tries: int = 3,
                 out: Callable[[str], None] | None = None):
        super().__init__(name=name, enabled=True)
        self._ask = ask or input
        self._auto = auto
        self.show_context = show_context
        self.max_tries = max(1, int(max_tries))
        self._out = out or (lambda s: print(s, flush=True))

    # ---------- 输入 ----------

    def _answer(self, req: ModelRequest, prompt: str) -> str:
        if callable(self._auto):
            return str(self._auto(req) or "")
        if isinstance(self._auto, list):
            if not self._auto:
                # ⚠️ 无人值守时绝不能回落到 input()：没有 stdin 会永久挂住。
                raise ModelError(
                    "auto 答案已用尽 —— 无人值守模式不能等 stdin；"
                    "请补足答案或改用 callable")
            ans = str(self._auto.pop(0))
            self._out(f"{prompt}{ans}")
            return ans
        return str(self._ask(prompt) or "")

    # ---------- 渲染 ----------

    @staticmethod
    def _menu_block(req: ModelRequest, opts: list[dict]) -> str:
        who = req.actor or "你"
        lines = [f"〔轮到 {who} 了〕"]
        for o in opts:
            head = f"  [{o.get('id')}] {o.get('name') or o.get('kind') or '?'}"
            summary = str(o.get("summary") or "").strip()
            if summary:
                head += f"｜{summary}"
            note = str(o.get("note") or "").strip()
            if note:
                head += f"　⚠️{note}"
            if o.get("requires_target"):
                tg = o.get("targets") or []
                head += ("　→ 目标：" + "/".join(str(x) for x in tg)) if tg else \
                        "　→ 目标：（此刻没有合法目标）"
            lines.append(head)
        lines.append('  输入选项 id；需要目标时再接一个目标（如 a3 E1）。'
                     '输入 ? 再看一遍。')
        return "\n".join(lines)

    # ---------- 唯一出口 ----------

    def _complete(self, req: ModelRequest) -> ModelReply:
        if req.expect != "intent":
            raise ModelError(f"真人座位只处理意图类请求，收到的是 expect={req.expect!r}")
        allowed = req.allowed or {}
        if allowed.get("units"):
            raise ModelError("真人座位不替敌方做多单位规划（那是 planner 座位的活）")
        opts = list(allowed.get("options") or [])
        if not opts:
            raise ModelError("真人座位没有拿到可选行动（菜单为空）")

        combat = any(k in o for k in _COMBAT_KEYS for o in opts)
        by_id = {str(o.get("id")): o for o in opts}
        block = self._menu_block(req, opts)

        if self.show_context and req.prompt:
            self._out(req.prompt.rstrip())

        tries = 0                    # 只数"给了但不合法"的输入，`?` 不算
        last = ""
        rounds = 0                   # 防止有人一直按 ? 把循环卡死
        while tries < self.max_tries and rounds < self.max_tries * 4 + 8:
            rounds += 1
            if rounds == 1:
                self._out(block)
            raw = self._answer(req, "> ").strip()
            if raw in ("?", "？"):
                self._out(block)
                continue
            if not raw:
                tries += 1
                last = "空输入"
                self._out("  ⚠️ 没有输入。再试一次，或按 Ctrl+C 退出。")
                continue

            m = re.match(r"^\s*([^\s,，:：]+)\s*[,，:：]?\s*(.*)$", raw)
            oid = (m.group(1) if m else "")
            rest = (m.group(2) if m else "").strip()

            if oid not in by_id:
                tries += 1
                last = f"没有这个选项 {oid!r}"
                self._out(f"  ⚠️ 选项 {oid!r} 不在菜单里"
                          f"（合法：{'、'.join(by_id)}）。")
                continue

            opt = by_id[oid]
            target = ""
            say = ""
            if combat:
                parts = rest.split()
                if opt.get("requires_target"):
                    if not parts:
                        tries += 1
                        tg = opt.get("targets") or []
                        last = "需要目标但没给"
                        self._out(f"  ⚠️ 这个选项需要目标（可选："
                                  f"{'、'.join(str(x) for x in tg) or '此刻没有'}）。")
                        continue
                    target = parts[0]
                    say = " ".join(parts[1:])
                    tg = [str(x) for x in (opt.get("targets") or [])]
                    if target not in tg:
                        tries += 1
                        last = f"目标 {target!r} 不合法"
                        self._out(f"  ⚠️ 目标 {target!r} 不在合法目标里"
                                  f"（可选：{'、'.join(tg) or '此刻没有'}）。")
                        continue
                else:
                    say = rest
            else:
                say = rest

            data: dict[str, Any]
            if combat:
                act: dict[str, Any] = {"id": oid}
                if target:
                    act["target"] = target
                if say:
                    act["say"] = say
                data = {"actions": [act]}
            else:
                data = {"id": oid, "say": say or f"我选择「{opt.get('name') or oid}」。"}
            return ModelReply(text=json.dumps(data, ensure_ascii=False), data=data)

        raise ModelError(f"真人座位连续 {self.max_tries} 次没给出合法输入（最后一次：{last}）")