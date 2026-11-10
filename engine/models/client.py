# coding=utf-8
"""模型客户端接口 + 假模型。

对应决策记录 §5.12.1 的 M6，以及 P1 就立下的那条纪律：

> **所有真模型调用走同一个出口**，能全局开关、能记账、能重放。
> （狼人杀项目的血泪教训：**"自称离线的测试一直在真花钱"** ——
> 根因是"我记得我桩了"，**记忆不是证据**。）

所以这一层刻意做成**接口 + 两种实现**：

    ModelClient (ABC)      —— 唯一的出口
      ├─ FakeModel         —— **确定性、零网络、零花费**（回归与 demo 全程用它）
      └─ （P4）RealModel   —— 真上游。**只在显式授权时构造**

`FakeModel` 不是"随便返回点东西"，它按**剧本**（script）回应：
    · 默认是全自动的**本地战术 AI**（能真打完一场）；
    · 测试可以塞一段固定回应，用来验"格式错/非法选项/超时"这些边界。

⚠️ 关键设计：**请求是被显式构造的 `ModelRequest`，不是裸字符串。**
这样以后接真模型时，"发了什么、回了什么、花了多少"才能被逐条记账。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import threading
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ..rules.menu import Menu, Option

__all__ = [
    "ModelRequest", "ModelReply", "ModelClient", "FakeModel", "RealModel",
    "ModelError", "RetryPolicy", "RecordedCall", "parse_intent_reply",
]


class ModelError(Exception):
    """模型侧的错误（上游挂了、超时、返回不可解析）。

    `retryable=True` 才允许重试 —— 只有"秒失败/连不上/5xx/429"这类才标它。
    解析失败、参数错这类**重试也没用**，别让重试掩盖真 bug。
    """

    def __init__(self, message: str = "", *, retryable: bool = False):
        super().__init__(message)
        self.retryable = bool(retryable)


def _no_real_calls() -> bool:
    """进程级真调用闸门：`DND_NO_REAL_CALLS=1` 时，真客户端在发请求前就失败。

    狼人杀那边的血泪教训："自称离线的测试一直在真花钱"。对象级
    `enabled=False` 只能管住拿得到对象的人；环境变量能管住整个进程。
    只拦真客户端，假模型不受影响。
    """
    return str(os.environ.get("DND_NO_REAL_CALLS", "")).strip().lower() in (
        "1", "true", "yes", "on")


def read_api_key() -> str:
    """读千帆 key：环境变量优先，其次 `~/.dsh/.credentials.yaml` 的 `QIANFAN_API_KEY`。

    **只读、不打印**；读不到返回空串，由调用方决定怎么报。
    """
    key = os.environ.get("QIANFAN_API_KEY", "").strip()
    if key:
        return key
    path = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
    try:
        for line in open(path, encoding="utf-8"):
            m = re.match(r"\s{2}QIANFAN_API_KEY:\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip("'\"")
    except OSError:
        pass
    return ""


def _usage_int(usage: dict, *keys, default: int = 0) -> int:
    """从不同家的 usage 里取一个整数；缺字段/坏值都按 0，不炸记账。"""
    for key in keys:
        if key not in usage:
            continue
        raw = usage.get(key)
        if isinstance(raw, dict) or raw is None:
            continue
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return default
    return default


def _cached_tokens(usage: dict) -> int:
    """提示缓存命中 token。**各家字段不同，能认几个认几个**：

    - OpenAI 兼容：`prompt_tokens_details.cached_tokens` / `cached_tokens`
    - DeepSeek：`prompt_cache_hit_tokens`
    - Anthropic：`cache_read_input_tokens` / `input_tokens_details.cached_tokens`
    """
    if not isinstance(usage, dict):
        return 0
    for key in ("prompt_tokens_details", "input_tokens_details"):
        detail = usage.get(key)
        if isinstance(detail, dict):
            got = _usage_int(detail, "cached_tokens")
            if got:
                return got
    return _usage_int(usage, "cached_tokens", "prompt_cache_hit_tokens",
                      "cache_read_input_tokens")


@dataclass(frozen=True)
class RetryPolicy:
    """一次调用的重试/预算口径（P4 之前的骨架，**默认不重试**）。

    对应交接文档 §9.1 的双层口径：
      · `max_attempts`    总尝试次数（1 = 不重试；4 = 首发 + 3 次重试）
      · `backoff`         每次重试前的等待秒数；比尝试次数短就复用最后一个
      · `budget_seconds`  整手预算：**所有尝试合计**的时间上限，用完不再发请求
      · `per_attempt_timeout` 单次硬墙钟（由 `_complete_with_timeout` 执行；
        传输层 socket timeout 只算单次操作，不够）

    默认 `max_attempts=1`：假模型/现有回归行为**一点不变**；真模型显式传策略。
    """

    max_attempts: int = 1
    backoff: tuple[float, ...] = ()
    budget_seconds: float = 0.0
    per_attempt_timeout: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts 至少 1，拿到 {self.max_attempts}")
        if self.budget_seconds < 0:
            raise ValueError(f"budget_seconds 不能是负数：{self.budget_seconds}")
        if self.per_attempt_timeout < 0:
            raise ValueError(
                f"per_attempt_timeout 不能是负数：{self.per_attempt_timeout}")
        if any(d < 0 for d in self.backoff):
            raise ValueError(f"backoff 里不能有负数：{self.backoff}")
        object.__setattr__(self, "backoff", tuple(float(x) for x in self.backoff))


# --------------------------------------------------------------------------- #
# 请求 / 回应
# --------------------------------------------------------------------------- #

@dataclass
class ModelRequest:
    """一次模型调用请求。**所有字段都要能被记账与重放。**"""
    role: str                      # dm / player / planner / filler
    actor: str                     # 谁（角色名 / "dm" / "planner"）
    task: str                      # 任务标识（如 "combat_turn" / "narrate"）
    prompt: str                    # 实际发出去的提示词
    #: 期望的输出形态（给真客户端选 schema / 给假模型选策略）
    expect: str = "intent"         # intent / narration / patch / free
    #: 合法选项（来自菜单）——**模型只能从这里选**（D13/§5.8）
    allowed: dict[str, Any] = field(default_factory=dict)
    model: str = ""                # 哪个模型（记账用；空 = 用默认）
    meta: dict[str, Any] = field(default_factory=dict)

    def tokens_est(self) -> int:
        from ..state.assembler import estimate_tokens
        return estimate_tokens(self.prompt)


@dataclass
class ModelReply:
    """一次模型回应。"""
    text: str = ""                 # 原始文本
    #: 上游返回的思考链（`reasoning_content`）。**只留痕、不进判定**；
    #: 没有就空串（有些模型/网关不返回，不能假装有）。
    reasoning: str = ""
    data: Any = None               # 解析后的结构（intent / patch / …）
    ok: bool = True
    error: str = ""
    #: 上游给的用量（假模型也填，口径统一）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 提示缓存命中 token（上游返回才有；没有就是 0，不代表没缓存）
    cached_tokens: int = 0
    elapsed: float = 0.0
    model: str = ""
    #: 被审核/拒答（`format_check` 判定）；**不当作普通失败**，单独计数与展示。
    refused: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> float:
        return (self.cached_tokens / self.prompt_tokens
                if self.prompt_tokens else 0.0)


#: 旁挂留痕里单条思考链/prompt 的截断上限（复盘够用，别把内存撑爆）
REASONING_KEEP = 40_000
PROMPT_KEEP = 40_000


@dataclass
class RecordedCall:
    """一条调用留痕（记账 + 复盘）。"""
    seq: int
    role: str
    actor: str
    task: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    elapsed: float
    ok: bool
    error: str = ""
    #: 这一手实际发了几次请求（含首发；`enabled=False` 时为 0）。
    #: 有了它才能对账"最坏 15 次请求"到底发生过没有。
    attempts: int = 1
    #: 上游返回的思考链（**只写旁挂 jsonl，不进主存档**；见 `calls_log.py`）。
    #: 截到 `REASONING_KEEP` 字，防止一条 10 万字的思考把内存撑爆。
    reasoning: str = ""
    #: 这一手看到的 prompt（同样只进旁挂文件，`--packet` 复盘要用）。
    prompt: str = ""
    #: 被审核/拒答（单独统计；复盘里标出来，不混进"失败"）。
    refused: bool = False
    #: 提示缓存命中 token（上游返回才有；0 不代表没命中）
    cached_tokens: int = 0

    def line(self) -> str:
        flag = "✓" if self.ok else "✗"
        tries = f" 试{self.attempts}" if self.attempts != 1 else ""
        cache = (f" 缓存{self.cached_tokens}"
                 f"（{self.cached_tokens / self.prompt_tokens:.0%}）"
                 if self.cached_tokens and self.prompt_tokens else "")
        return (f"#{self.seq} [{flag}] {self.role}/{self.actor} {self.task} "
                f"{self.model or '(fake)'} 入{self.prompt_tokens} 出{self.completion_tokens} "
                f"{self.elapsed:.2f}s{tries}{cache}"
                + (f"  {self.error}" if self.error else ""))


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #

class ModelClient(ABC):
    """**唯一的模型出口。**

    子类必须实现 `_complete()`。记账、开关、重放都在基类里做，
    子类不许绕过去自己发请求。
    """

    def __init__(self, *, name: str = "", enabled: bool = False,
                 policy: RetryPolicy | None = None,
                 sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None,
                 real: bool = False):
        self.name = name
        self.enabled = enabled          # 全局闸门：关掉就一律不真发
        self.real = bool(real)          # 真客户端才吃 DND_NO_REAL_CALLS
        self.calls: list[RecordedCall] = []
        self._seq = 0
        #: 重试/预算口径。默认单次尝试 —— 现有回归与假模型行为不变。
        self.policy = policy or RetryPolicy()
        #: 可注入，测试里不用真的等（也不让系统时钟影响断言）
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        #: 调用开始/结束钩子（网页"思考提示"用）。签名 `(actor, task, phase)`，
        #: phase = "start" / "end"。**只做展示**，不参与记账与判定。
        self.on_call: Callable[["str", "str", "str"], None] | None = None
        #: 钩子自己抛的异常记在这里（**不静默吞**：H1 卫生闸门要能看到）。
        self.hook_errors: list[str] = []

    # ---------- 对外唯一入口 ----------

    def complete(self, req: ModelRequest) -> ModelReply:
        self._seq += 1
        started = self._clock()
        if self.on_call is not None:
            try:
                self.on_call(req.actor, req.task, "start")
            except Exception as exc:                 # noqa: BLE001
                if len(self.hook_errors) < 20:
                    self.hook_errors.append(f"start: {type(exc).__name__}: {exc}")
        if self.real and _no_real_calls():
            reply = ModelReply(
                ok=False, model=req.model,
                error="真调用被进程闸门禁用（DND_NO_REAL_CALLS=1）；"
                      "要跑真模型先显式取消该环境变量")
            attempts = 0
        elif not self.enabled:
            reply = ModelReply(ok=False, error="模型调用被全局禁用（enabled=False）",
                               model=req.model)
            attempts = 0
        else:
            reply, attempts = self._run_with_retry(req, started)
        reply.elapsed = round(self._clock() - started, 3)
        if not reply.model:
            reply.model = req.model or self.name
        if not reply.prompt_tokens:
            reply.prompt_tokens = req.tokens_est()
        if not reply.completion_tokens and reply.text:
            from ..state.assembler import estimate_tokens
            reply.completion_tokens = estimate_tokens(reply.text)

        self.calls.append(RecordedCall(
            seq=self._seq, role=req.role, actor=req.actor, task=req.task,
            model=reply.model, prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            cached_tokens=reply.cached_tokens,
            elapsed=reply.elapsed, ok=reply.ok, error=reply.error,
            attempts=attempts,
            reasoning=(reply.reasoning or "")[:REASONING_KEEP],
            prompt=(req.prompt or "")[:PROMPT_KEEP],
            refused=bool(getattr(reply, "refused", False))))
        if self.on_call is not None:
            try:
                self.on_call(req.actor, req.task, "end")
            except Exception as exc:                 # noqa: BLE001
                if len(self.hook_errors) < 20:
                    self.hook_errors.append(f"end: {type(exc).__name__}: {exc}")
        return reply

    def _complete_with_timeout(self, req: ModelRequest,
                               seconds: float) -> ModelReply:
        """给单次调用加硬墙钟上限。

        `urllib` 的 `timeout` 只管单次 socket 操作：上游若持续吐字节/卡在
        传输层，180s 可以永远不结束。这里用守护线程等结果，超过墙钟就返回
        非重试错误；迟到的线程自己结束，不再拖住整桌。
        """
        if seconds <= 0:
            return self._complete(req)
        result: dict = {}
        def run() -> None:
            try:
                result["reply"] = self._complete(req)
            except Exception as exc:              # noqa: BLE001
                result["exc"] = exc
        worker = threading.Thread(target=run, daemon=True,
                                  name=f"model-call-{self.name}-{self._seq}")
        worker.start()
        worker.join(float(seconds))
        if worker.is_alive():
            raise ModelError(f"单次调用超过硬上限 {seconds:g}s（上游未返回）",
                             retryable=False)
        if "exc" in result:
            raise result["exc"]
        reply = result.get("reply")
        if not isinstance(reply, ModelReply):
            raise ModelError("模型线程没有返回 ModelReply", retryable=False)
        return reply

    def _run_with_retry(self, req: ModelRequest, started: float
                       ) -> tuple[ModelReply, int]:
        """按 `self.policy` 跑一次或多次。返回 `(最终回应, 实际尝试次数)`。

        **只有 `retryable=True` 的 ModelError 才重试**；解析失败/参数错不重试。
        """
        policy = self.policy
        reply = ModelReply(ok=False, error="没有发出任何尝试", model=req.model)
        attempts = 0
        for attempt in range(1, policy.max_attempts + 1):
            if policy.budget_seconds and attempts and \
                    (self._clock() - started) >= policy.budget_seconds:
                reply = ModelReply(
                    ok=False, model=req.model, error=(
                        f"整手预算用完（{policy.budget_seconds:g}s，已试 {attempts} 次）"))
                break
            attempts = attempt
            retryable = False
            try:
                reply = self._complete_with_timeout(
                    req, policy.per_attempt_timeout)
            except ModelError as exc:
                retryable = bool(getattr(exc, "retryable", False))
                reply = ModelReply(ok=False, error=str(exc), model=req.model)
            except Exception as exc:                     # noqa: BLE001
                reply = ModelReply(ok=False,
                                   error=f"{type(exc).__name__}: {exc}",
                                   model=req.model)
            if reply.ok or not retryable or attempt >= policy.max_attempts:
                break
            delay = (policy.backoff[min(attempt - 1, len(policy.backoff) - 1)]
                     if policy.backoff else 0.0)
            if policy.budget_seconds:
                remaining = policy.budget_seconds - (self._clock() - started)
                if remaining <= 0:
                    reply = ModelReply(
                        ok=False, model=req.model, error=(
                            f"整手预算用完（{policy.budget_seconds:g}s，"
                            f"已试 {attempts} 次）"))
                    break
                delay = min(delay, remaining)
            if delay > 0:
                self._sleep(delay)
        return reply, attempts

    @abstractmethod
    def _complete(self, req: ModelRequest) -> ModelReply:
        """真正干活的地方。子类实现。"""

    # ---------- 记账 ----------

    def ledger_text(self) -> str:
        return "\n".join(c.line() for c in self.calls)

    def load_calls(self, rows: list[dict]) -> None:
        """从存档恢复调用记账（读档后 `report()` 的账接着算，不归零）。"""
        restored: list[RecordedCall] = []
        for row in rows or []:
            if not isinstance(row, dict):
                raise ValueError(f"调用记账条目必须是对象：{row!r}")
            try:
                restored.append(RecordedCall(
                    seq=int(row["seq"]), role=str(row.get("role", "")),
                    actor=str(row.get("actor", "")), task=str(row.get("task", "")),
                    model=str(row.get("model", "")),
                    prompt_tokens=int(row.get("prompt_tokens", 0)),
                    completion_tokens=int(row.get("completion_tokens", 0)),
                    cached_tokens=int(row.get("cached_tokens", 0)),
                    elapsed=float(row.get("elapsed", 0.0)),
                    ok=bool(row.get("ok", True)),
                    error=str(row.get("error", "")),
                    attempts=int(row.get("attempts", 1)),
                    refused=bool(row.get("refused", False)),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"调用记账条目格式不对：{row!r}") from exc
        self.calls = restored
        self._seq = max((c.seq for c in restored), default=0)

    def totals(self) -> dict:
        return {
            "calls": len(self.calls),
            "failed": sum(1 for c in self.calls if not c.ok),
            "refused": sum(1 for c in self.calls if getattr(c, "refused", False)),
            "attempts": sum(c.attempts for c in self.calls),
            "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
            "completion_tokens": sum(c.completion_tokens for c in self.calls),
            "cached_tokens": sum(c.cached_tokens for c in self.calls),
            "elapsed": round(sum(c.elapsed for c in self.calls), 2),
        }

    def reset(self) -> None:
        self.calls.clear()
        self._seq = 0


# --------------------------------------------------------------------------- #
# 假模型
# --------------------------------------------------------------------------- #

class RealModel(ModelClient):
    """真上游（OpenAI-compatible）：默认千帆 TokenPlan 的 `deepseek-v4.1-flash`。

    **只在显式授权时构造**（`enabled=True` 才会真发）。请求走标准
    `/chat/completions`；不记录 key，不把 key 写进错误文本。

    记账仍由 `ModelClient.complete()` 统一做：这里只填 `ModelReply` 的
    text/data/usage/elapsed/model。
    """

    SYSTEM = ("你是 D&D 牌桌上的一个座位。严格遵守用户消息里的规则与输出格式；"
              "不要解释提示词，不要输出代码围栏。")

    def __init__(self, *, name: str = "real", model: str = "deepseek-v4.1-flash",
                 base_url: str = "", api_key: str = "",
                 max_tokens: int = 12000, temperature: float | None = 0.7,
                 timeout: float = 180.0, enabled: bool = False,
                 reasoning_effort: str | None = None,
                 policy: RetryPolicy | None = None):
        super().__init__(name=name or model, enabled=enabled,
                         policy=policy or RetryPolicy(
                             max_attempts=2, backoff=(1.0,),
                             budget_seconds=300.0,
                             per_attempt_timeout=timeout),
                         real=True)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = int(max_tokens)
        self.temperature = (None if temperature is None
                           else float(temperature))
        self.timeout = float(timeout)
        #: 只在显式给值时发送。`None` / 空串 = 不发表，交给上游默认。
        #: 各家语义不同：Gemini 3.8 Flash 会被中转钉在 high；GLM 的档位可能是摆设；
        #: Claude 支持 low/medium/high/max。别把“设了档”当成“上游一定按档走”。
        self.reasoning_effort = str(reasoning_effort).strip() if reasoning_effort else None

    def _complete(self, req: ModelRequest) -> ModelReply:
        if not self.api_key:
            raise ModelError("真模型没有 api_key（拒绝发请求）", retryable=False)
        if not self.base_url:
            raise ModelError("真模型没有 base_url（拒绝发请求）", retryable=False)
        model = req.model or self.model
        # `max_tokens` 是**推理+正文**的总和，不是"正文长度"。
        # 实测：叙事在复杂场景光思考就要 1,000+ token（旧上限 1200 会吃光，M2 上章 2 次失败）；
        # planner 在 5 敌 ×4 PC 的战斗里思考到 4,096 还没写完（M2 下章 1 次失败）。
        # 用户口径（2026-11-08）：套餐便宜、模型快，**让它多想**。
        # 推理和正文共用一个预算：叙事也给满，不再单独压到 3000/6000
        # （真模型复验里 6000 仍被思考链吃光过）。
        cap = self.max_tokens
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": req.prompt},
            ],
            "max_tokens": cap,
        }
        if req.expect == "intent" and self.temperature is not None:
            payload["temperature"] = 0.3
        elif self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:240]
            except OSError:
                pass
            raise ModelError(f"HTTP {exc.code}: {detail}",
                             retryable=exc.code in (408, 409, 425, 429)
                             or exc.code >= 500)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelError(f"传输失败：{exc}", retryable=True)

        try:
            data = json.loads(raw)
        except ValueError:
            raise ModelError(f"上游不是 JSON：{raw[:160]!r}", retryable=False)
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"上游没有 choices：{str(data)[:160]}",
                             retryable=False)
        choice = choices[0]
        msg = choice.get("message") or {}
        content = str(msg.get("content") or "").strip()
        reasoning = str(msg.get("reasoning_content") or "").strip()
        usage = data.get("usage") or {}
        prompt_tokens = _usage_int(usage, "prompt_tokens")
        completion_tokens = _usage_int(usage, "completion_tokens")
        cached_tokens = _cached_tokens(usage)
        if not content:
            why = ("只返回了推理内容（max_tokens 被推理占满）"
                   if reasoning else "空回复")
            return ModelReply(text="", reasoning=reasoning, ok=False, error=why,
                              prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens,
                              cached_tokens=cached_tokens,
                              model=str(data.get("model") or model))
        return ModelReply(text=content, reasoning=reasoning,
                          data=_try_json(content), ok=True,
                          prompt_tokens=prompt_tokens,
                          completion_tokens=completion_tokens,
                          cached_tokens=cached_tokens,
                          model=str(data.get("model") or model))


class FakeModel(ModelClient):
    """确定性假模型：**零网络、零花费**，但行为要真。

    两种模式：
      · `script=` 给定剧本时：按剧本逐条返回（用来验边界：非法选项、格式错、空回复）；
      · 不给剧本时：**跑本地战术 AI**（真挑动作、真组成合法 JSON），
        所以"一整轮"能被完整跑通而不需要任何模型。
    """

    def __init__(self, *, script: list[str] | None = None,
                 planner: Callable[[ModelRequest], str] | None = None,
                 name: str = "fake", fail_on: list[str] | None = None,
                 enabled: bool = True):
        super().__init__(name=name, enabled=enabled)
        self.script = list(script or [])
        self._i = 0
        self.planner = planner
        self.fail_on = set(fail_on or [])

    def _complete(self, req: ModelRequest) -> ModelReply:
        if req.task in self.fail_on:
            raise ModelError(f"假模型被要求在这个任务上失败：{req.task}")

        if self.script:
            if self._i >= len(self.script):
                return ModelReply(text="", ok=False,
                                  error="剧本用完了（没有更多预设回应）")
            text = self.script[self._i]
            self._i += 1
            return ModelReply(text=text, data=_try_json(text))

        if self.planner:
            text = self.planner(req)
            return ModelReply(text=text, data=_try_json(text))

        # 默认：按任务生成合理回应
        if req.expect == "patch":
            return ModelReply(text="[]", data=[])
        if req.expect == "narration":
            return ModelReply(text=self._narrate(req))
        return ModelReply(text=self._intent(req), data=_try_json(self._intent(req)))

    # ---------- 默认策略 ----------

    @staticmethod
    def _narrate(req: ModelRequest) -> str:
        return (f"（DM 叙事·假模型）{req.actor} 的回合结束了，场面继续推进。"
                f"任务={req.task}")

    @staticmethod
    def _intent(req: ModelRequest) -> str:
        """按 `req.allowed` 里的菜单挑一个合理动作，输出合法 JSON。

        要区分**两种形态**（它们的输出 schema 不一样）：
          · 战斗轮：单位 + 选项 id + 目标 → `[{"unit":..,"actions":[{"id":..,"target":..}]}]`
          · 日常轮：单个角色的行动 + 台词 → `{"id": "look", "say": "..."}`

        策略刻意简单（够用即可）：**能打就近战打、集火血最少的**；
        打不到就移动。这跟 `demo_battle` 的本地 AI 是同一套思路。
        """
        allowed = req.allowed or {}
        units = allowed.get("units") or []
        if not units:
            opts = allowed.get("options") or []
            if not opts:
                return json.dumps({"actions": []}, ensure_ascii=False)
            # 日常轮：选项里有 name/summary 而没有 kind=="attack" 这类战斗字段
            if all(("summary" in o or "name" in o) and "targets" not in o
                   for o in opts):
                pick = opts[0]
                return json.dumps(
                    {"id": pick.get("id"),
                     "say": f"（假模型）我选择「{pick.get('name') or pick.get('id')}」。"},
                    ensure_ascii=False)
            pick = _pick_option(opts)
            return json.dumps({"actions": [pick]}, ensure_ascii=False)

        out = []
        for u in units:
            opts = u.get("options") or []
            # ⚠️ 即使一个动作都没有，也要输出 `actions: []` 而不是把键整个丢掉 ——
            #    丢键会让解析器只剩 `{"unit":"E1"}`，看起来像"模型忘了给动作"，
            #    实际是"这个单位此刻确实没得选"。日志要能区分这两件事。
            if not opts:
                out.append({"unit": u.get("unit"), "actions": []})
                continue
            out.append({"unit": u.get("unit"), "actions": [_pick_option(opts)]})
        return json.dumps(out, ensure_ascii=False)


def _pick_option(opts: list[dict]) -> dict:
    """从菜单选项里挑一个：优先**有目标的攻击**，其次有目标的豁免动作，
    再次通用动作，最后结束回合。返回 `{"id":..., "target":...}`。

    ⚠️ 一条踩过的坑：以前按 `kind` 的名字排序，结果"协助/回避"这类**无目标的
    通用动作**排在攻击后面——但攻击常常因为"目标列表为空（够不着）"被降级，
    于是假模型**每回合都在协助**，战斗永远打不完（demo 里真的这样跑了两轮）。
    所以排序要看**实际能不能打到**，不是看动作名字。
    """
    def usable_targets(o: dict) -> int:
        """这个选项此刻有几个合法目标。

        ⚠️ 注意 `targets` 键**可能不存在**（`to_dict()` 在目标列表为空时不输出它）。
        `None` 必须当成 0，不能当成"有目标" —— 实测踩过：因为这一条，
        假模型会挑一个"够不着"的攻击，然后每回合都被引擎拒绝、白费一回。
        """
        tg = o.get("targets")
        return len(tg) if isinstance(tg, list) else 0

    def kind_rank(o: dict) -> int:
        # 有目标的法术优先于普通攻击（假模型也要把 U2 的施法链路真跑出来）；
        # 无目标的增益/位移仍排在攻击之后（`usable_targets` 已经先降权）。
        return {"spell": 0, "attack": 1, "save_action": 2,
                "special": 3, "move": 4, "dash": 5, "disengage": 6,
                "dodge": 7, "hide": 8, "help": 9, "ready": 10,
                "end_turn": 99}.get(o.get("kind", ""), 50)

    # ⚠️ **先过滤掉"需要目标但此刻一个合法目标都没有"的选项**，而不是只给它们降权。
    #    历史 bug：只当排序键用，结果"够不着的攻击"仍然会被选中
    #    （它的 `-t` 是 0，比其他负值都大，反而排前面），然后每回合被引擎拒绝。
    alive_opts = [o for o in opts
                  if not (o.get("requires_target") and usable_targets(o) == 0)]
    if not alive_opts:
        # 一个"能打的"都没有（常见于敌人全灭/目标全倒）。这时**别硬挑攻击** ——
        # 挑了也只会被引擎拒绝。优先收尾：结束回合 > 任意不需目标的选项 > 原样返回。
        for k in ("end_turn", "dodge", "dash", "move"):
            for o in opts:
                if o.get("kind") == k:
                    return {"id": o.get("id")}
        free = [o for o in opts if not o.get("requires_target")]
        if free:
            return {"id": free[0].get("id")}
        return {"id": (opts[0].get("id") if opts else "")}

    def score(o: dict) -> tuple:
        t = usable_targets(o)
        # 能打到东西的优先；然后看种类；再看目标数（AoE 多目标更好）；
        # 同为法术时先用高环（把"选环阶"这条链路也真跑出来）。
        return (0 if t else 1, kind_rank(o), -t,
                -int(o.get("max_targets") or 0),
                -int(o.get("slot_level") or 0), o.get("id", ""))

    ranked = sorted(alive_opts, key=score)
    o = ranked[0]
    out: dict[str, Any] = {"id": o.get("id")}
    if o.get("requires_target"):
        tg = o.get("targets")
        if isinstance(tg, list) and tg:
            out["target"] = tg[0]
    return out


def _try_json(text: str) -> Any:
    """温和地解析 JSON（去 code fence、取第一个完整结构）。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = t.find(opener), t.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    # 数组被 max_tokens 截断（长跑批次实测）：`[{"unit":"E1",...},{"unit":"E4","act`
    # 严格解析必然失败，但**前面几条是完整的**。丢整批等于让所有敌人白站一轮，
    # 所以从截断的数组里捞完整的对象，捞到几条算几条（只对 `[` 开头的回应做）。
    if t.lstrip().startswith("["):
        salvaged = _balanced_objects(t)
        if salvaged:
            return salvaged
    return None


def repair_truncated_json(text: str) -> dict | None:
    """把**被截断/尾部有渣的单个 JSON 对象**补成可解析的（只补格式，不改内容）。

    PC 的 `say` 偶尔被写太长，在字符串中间停住，或者收尾带一个多余的字：
        {"id":"a1","target":"E3","say":"小翠压低剑锋，照着……劈下去："还
    这里按"从尾部逐步回退 + 补齐引号/右括号"的方式试，**取第一个能解析的**；
    `say` 保留原样（截断处就截断），不是替模型续写剧情。

    只处理 `{` 开头的单对象；数组交给 `_try_json` 的打捞逻辑。
    """
    t = text.strip()
    if not t.startswith("{"):
        return None

    def _closed(cand: str) -> str:
        depth = 0
        in_str = False
        esc = False
        for ch in cand:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        return cand + ('"' if in_str else "") + "}" * max(0, depth)

    for cut in range(0, min(80, len(t))):
        cand = t[:len(t) - cut] if cut else t
        try:
            obj = json.loads(_closed(cand))
        except ValueError:
            continue
        if isinstance(obj, dict):
            if _closed(cand) != t:
                obj["_truncated"] = True
            return obj
    return None


def _balanced_objects(text: str) -> list[dict]:
    """从（可能被截断的）文本里捞出**完整**的顶层 `{...}` 对象。"""
    out: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except ValueError:
                    obj = None
                if isinstance(obj, dict):
                    out.append(obj)
                start = -1
    return out




# --------------------------------------------------------------------------- #
# 意图解析（把模型回的 JSON 变成引擎能执行的东西）
# --------------------------------------------------------------------------- #

@dataclass
class Intent:
    """一个单位的一条意图。"""
    unit: str
    option_id: str
    target: str = ""
    targets: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def parse_intent_reply(reply: ModelReply,
                       menus: dict[str, Menu], *,
                       fill_missing_target: bool = False
                       ) -> tuple[list[Intent], list[str]]:
    """把模型回应解析成 `Intent` 列表。返回 (意图列表, 问题列表)。

    **校验的严格程度刻意很高**（对应 §5.7 的第 2 项能力）：
      · 单位 id 必须在菜单里；
      · 选项 id 必须在那个单位的菜单里（**不能发明动作**）；
      · 需要目标的选项必须给合法目标。

    任何一条不合法 → 记进问题列表，调用方**按单位**决定是重试还是走本地兜底。
    **不静默忽略** —— 静默忽略会让"模型输出崩了"看起来像"单位没行动"。
    """
    problems: list[str] = []
    data = reply.data
    if data is None:
        return [], [f"回应无法解析成 JSON：{reply.text[:80]!r}"]

    # 容忍模型常见的几种形状（真模型实测会自由发挥）：
    #   ① 标准数组：[{"unit":"E1","actions":[...]}]
    #   ② 单单位对象：{"unit":"布兰","actions":[...]}
    #   ③ 单单位动作：{"id":"a1","target":"E1"}（菜单只有一个单位时）
    #   ④ 单位当键：{"E1":{"action":{"id":"a1","target":"布兰"},...}, ...}
    #   ⑤ actions 里每条自己带 unit：{"actions":[{"unit":"E1",...}, ...]}
    # **范围不放大**：条目仍要过"单位/选项/目标"三关，非法照样记问题。
    rows: list[dict] = []
    if isinstance(data, list):
        if (len(menus) == 1 and data
                and all(isinstance(x, dict) and not x.get("unit")
                        and any(k in x for k in ("id", "action", "action_id"))
                        for x in data)):
            # 单单位菜单下的"动作数组"：`[{"id":"a1","target":"E1"}]`
            rows = [{"unit": next(iter(menus)), "actions": list(data)}]
        else:
            rows = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        if "unit" in data:
            rows = [data]
        elif isinstance(data.get("actions"), list):
            acts = data["actions"]
            tagged = [a for a in acts if isinstance(a, dict) and a.get("unit")]
            if tagged:
                groups: dict[str, list] = {}
                for a in acts:
                    if not isinstance(a, dict):
                        problems.append("计划里有一条行动不是对象")
                        continue
                    uid = str(a.get("unit") or "")
                    if not uid:
                        if len(menus) == 1:
                            uid = next(iter(menus))
                        else:
                            problems.append(
                                "计划里有一条行动缺少 unit（多单位菜单无法归属）")
                            continue
                    groups.setdefault(uid, []).append(a)
                rows = [{"unit": u, "actions": v} for u, v in groups.items()]
            elif len(menus) != 1:
                return [], ["对象形回应缺少 unit，但菜单里有多个单位"
                            f"（{list(menus)}）—— 必须逐单位给 actions"]
            else:
                rows = [{"unit": next(iter(menus)), "actions": acts}]
        elif len(menus) == 1 and ("actions" in data
                                  or any(k in data for k in
                                         ("id", "action", "action_id"))):
            uid = next(iter(menus))
            acts = data.get("actions")
            if acts is None:
                acts = [data]
            rows = [{"unit": uid, "actions": acts}]
        elif data and all(isinstance(v, dict)
                          and ("action" in v or "actions" in v)
                          for v in data.values()):
            for uid, body in data.items():
                acts = body.get("actions")
                if acts is None:
                    one = body.get("action")
                    acts = [one] if isinstance(one, dict) else []
                for k in ("bonus_action", "reaction"):
                    if body.get(k):
                        problems.append(
                            f"{uid} 的 {k} 未执行（v1 只执行主动作）")
                rows.append({"unit": str(uid), "actions": acts})
        else:
            rows = []
    else:
        return [], [f"回应形状不认识：{type(data).__name__}"]

    if not rows:
        return [], ["回应里没有单位条目"]

    out: list[Intent] = []
    for r in rows:
        uid = str(r.get("unit") or "")
        if uid not in menus:
            problems.append(f"未知单位 {uid!r}（菜单里有：{list(menus)}）")
            continue
        menu = menus[uid]
        acts = r.get("actions")
        if acts is None and isinstance(r.get("action"), dict):
            acts = [r["action"]]
        if acts is None:
            # ⚠️ "**压根没给 actions 键**" 和 "**明确给了空数组**" 是两件事：
            #    前者是模型输出残缺（要报问题），后者是"这个单位此刻确实没得选"
            #    （正常，交给本地兜底即可，不该刷成错误）。
            problems.append(f"{uid} 的条目里没有 actions 键（输出残缺）")
            continue
        if not isinstance(acts, list):
            problems.append(f"{uid} 的 actions 不是数组")
            continue
        if not acts:
            problems.append(f"{uid} 明确表示本回合不行动（actions 为空）")
            continue
        for k in ("bonus_action", "reaction"):
            if r.get(k):
                problems.append(f"{uid} 的 {k} 未执行（v1 只执行主动作）")
        for a in acts:
            if not isinstance(a, dict):
                problems.append(f"{uid} 的某条行动不是对象")
                continue
            if isinstance(a.get("action"), dict):
                a = {**a, **a["action"]}
            oid = str(a.get("id") or a.get("action") or
                      a.get("action_id") or "")
            if not oid:
                problems.append(f"{uid} 的某条行动没有 id/action")
                continue
            for k in ("bonus_action", "reaction"):
                if a.get(k):
                    problems.append(f"{uid} 的 {k} 未执行（v1 只执行主动作）")
            try:
                opt = menu.by_id(oid)
            except Exception:                            # noqa: BLE001
                problems.append(f"{uid} 选了菜单里没有的选项 {oid!r}")
                continue
            raw_targets = a.get("targets")
            targets: list[str] = []
            if isinstance(raw_targets, list):
                targets = [str(x) for x in raw_targets if str(x or "").strip()]
            elif raw_targets:
                targets = [str(raw_targets)]
            primary = str(a.get("target") or "").strip()
            if primary:
                targets = [primary] + [x for x in targets if x != primary]
            it = Intent(unit=uid, option_id=oid,
                        target=targets[0] if targets else "",
                        targets=list(targets))
            if opt.requires_target:
                if not targets:
                    # ⚠️ 区分两种情况，否则日志会误导排查方向：
                    #   · 选项的 targets 本来就是空的（**没有合法目标可打**）→ 这是
                    #     "该选项此刻不可用"，应当让调用方走本地兜底；
                    #   · targets 非空但模型没给/给错 → 这才是模型的格式问题。
                    if not opt.target_ids:
                        problems.append(
                            f"{uid} 选了 {oid}（{opt.name}），"
                            f"但该选项此刻没有合法目标 —— 不可用")
                        continue
                    if fill_missing_target:
                        targets = [opt.target_ids[0]]
                        it.target = targets[0]
                        it.targets = list(targets)
                        it.extra["auto_target"] = True
                    else:
                        problems.append(
                            f"{uid} 的 {oid}（{opt.name}）需要目标但没给"
                            f"（可选：{opt.target_ids}）")
                        continue
                if len(targets) > max(1, int(opt.max_targets or 1)):
                    problems.append(
                        f"{uid} 的 {oid} 给了 {len(targets)} 个目标，"
                        f"最多 {max(1, int(opt.max_targets or 1))} 个")
                    continue
                bad = [t for t in targets if t not in opt.target_ids]
                if bad:
                    problems.append(
                        f"{uid} 的 {oid} 目标 {bad!r} 不在合法目标里"
                        f"（{opt.target_ids}）")
                    continue
            say = str(a.get("say") or "").strip()
            if say:
                # 战斗轮也可以带台词：别在解析层丢掉（HumanSeat 会把它塞进 action）
                it.extra["say"] = say
            out.append(it)
    return out, problems


