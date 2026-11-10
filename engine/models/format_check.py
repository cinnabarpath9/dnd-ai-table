# coding=utf-8
"""模型输出的**严格体检**（P0-3，借自参考拆解 §3 的四象限）。

分工：

    · `parse_intent_reply()` 负责**尽力消费**（宽松：对象/数组/空 actions 都认）；
    · 本模块负责**严格度量**：这段输出到底是
      `valid` / `invalid`（完整但非法）/ `truncated`（截断）/ `refusal`（拒答）/ `error`；
    · 只有 `invalid` 值得发起**一次**纠错；截断/拒答/传输错误都走原流程
      （重生成或本地兜底），不拿一次"补写"冒充新生成。

**不静默降级**：每个问题都带 `(级别, 说明)`，调用方必须把它写进日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import ModelReply, parse_intent_reply
from ..rules.menu import Menu

__all__ = ["CheckResult", "classify", "repair_hint", "REFUSAL_MARKERS",
           "MODERATION_MARKERS"]

#: 审核/安全拦截的措辞（比拒答更硬：模型说"不能协助这个请求"）。
MODERATION_MARKERS = (
    "content policy", "content_filter", "content filter", "moderation",
    "safety", "blocked", "prohibited", "not allowed to", "can't assist",
    "cannot assist", "can't help with that", "cannot help with that",
    "违反", "违规", "不符合", "安全策略", "内容政策", "无法协助", "不能协助",
)

#: 拒答/道歉的常见开头（只在"整段没有 JSON"时才判拒答，避免误伤正文）。
REFUSAL_MARKERS = (
    "抱歉", "对不起", "很遗憾", "无法", "我不能", "不能帮",
    "作为 ai", "作为ai", "i'm sorry", "i am sorry", "i cannot",
    "i can't", "as an ai",
)

#: 纠错提示最多带几个单位/选项，防止"纠错提示本身把预算撑爆"。
_MAX_UNITS = 6
_MAX_IDS = 12


@dataclass
class CheckResult:
    status: str                      # valid / invalid / truncated / refusal / error
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "valid"

    def line(self) -> str:
        if self.ok:
            return "valid"
        head = f"{self.status}（{len(self.problems)} 个问题）"
        details = "；".join(self.problems[:4])
        return f"{head}：{details}" if details else head


def _looks_moderation(text: str) -> bool:
    """审核命中：比"我不想答"更硬，通常不该再原样重试。"""
    if "{" in text or "[" in text:
        return False
    low = text.lower()
    return any(m in low for m in MODERATION_MARKERS)


def _looks_refusal(text: str) -> bool:
    if "{" in text or "[" in text:
        return False
    low = text.lower()
    return any(m in low for m in REFUSAL_MARKERS)


def _looks_truncated(text: str) -> bool:
    """括号没配平 = 更像被截断；配平但解析失败 = 完整但非法。"""
    stripped = text.strip()
    if not stripped:
        return True
    opens = stripped.count("{") + stripped.count("[")
    closes = stripped.count("}") + stripped.count("]")
    if opens > closes:
        return True
    if stripped.startswith("```") and closes < opens + 2:
        return True
    return False


def classify(reply: ModelReply, menus: dict[str, Menu]) -> CheckResult:
    """给一次模型回应定性。**纯函数，不写全局、不重试。**"""
    if not reply.ok:
        return CheckResult("error", [f"调用失败：{reply.error or '未知错误'}"])
    if reply.data is not None:
        _intents, problems = parse_intent_reply(reply, menus)
        return CheckResult("valid" if not problems else "invalid", list(problems))

    text = (reply.text or "").strip()
    if not text:
        return CheckResult("truncated", ["空回复（没有正文，也没有 JSON）"])
    if _looks_moderation(text):
        return CheckResult("moderation", [f"审核/安全拦截：{text[:60]!r}"])
    if _looks_refusal(text):
        return CheckResult("refusal", [f"模型拒答/道歉：{text[:60]!r}"])
    if _looks_truncated(text):
        return CheckResult("truncated", [f"JSON 没写完（括号不配平）：{text[:60]!r}"])
    return CheckResult("invalid", [f"不是合法 JSON：{text[:60]!r}"])


def repair_hint(problems: list[str], menus: dict[str, Menu]) -> str:
    """组装"一次纠错"要追加的短提示。**只列问题 + 合法 id，不重发整段规则。**"""
    lines = ["【上一次输出不合格。只输出一个合法 JSON，不要解释、不要代码围栏。】",
             "问题："]
    for p in list(problems)[:6]:
        lines.append(f"- {p}")
    if menus:
        lines.append("合法单位与选项（只能从这里选）：")
        for uid, menu in list(menus.items())[:_MAX_UNITS]:
            ids = [o.id for o in (menu.options or [])][:_MAX_IDS]
            lines.append(f"- {uid}: {', '.join(ids) or '（本回合没有可选动作）'}")
    else:
        lines.append("- 没有可选动作时请输出 `[]`")
    return "\n".join(lines)