# coding=utf-8
"""P4 前置：重试 / 预算骨架（`RetryPolicy`）的回归。

守的是"账算得清、重试不掩盖 bug"：
    R1 默认单次尝试（现有行为不变）
    R2 只有 retryable 的错误才重试；非 retryable / 普通异常一律不重试
    R3 重试有上限、有退避、退避用尽复用最后一个
    R4 **整手预算**用完就停（不再发请求），错误要能看懂
    R5 实际尝试次数要记账（`RecordedCall.attempts` / `totals()`），并随存档往返
"""

from __future__ import annotations

import os
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.longrun import build_table_loop  # noqa: E402
from engine.models.client import (  # noqa: E402
    ModelClient, ModelError, ModelReply, ModelRequest, RetryPolicy,
)
from engine.persist import dump_game, load_from_dict  # noqa: E402
from engine.models import FakeModel  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def ok(cond: bool, label: str, extra="") -> None:
    global COUNT
    COUNT += 1
    tail = f"  |  {extra}" if (extra != "" and not cond) else ""
    print(f"  {'PASS' if cond else 'FAIL'} {label}{tail}")
    if not cond:
        FAILS.append(label)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def raises(fn, exc=Exception) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


class FakeClock:
    """可注入的时钟：sleep 就是把时间往前推，测试不用真等。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Flaky(ModelClient):
    """按剧本依次抛错/返回。`seen` 记真正调了几次 `_complete`。"""

    def __init__(self, outcomes, **kw):
        kw.setdefault("enabled", True)
        super().__init__(name="flaky", **kw)
        self.outcomes = list(outcomes)
        self.seen = 0

    def _complete(self, req):
        self.seen += 1
        if not self.outcomes:
            raise AssertionError("剧本用完了还被调用")
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class Slow(ModelClient):
    """模拟上游卡住：`_complete` 真睡，验证墙钟上限能先返回。"""

    def __init__(self, **kw):
        kw.setdefault("enabled", True)
        super().__init__(name="slow", **kw)

    def _complete(self, req):
        time.sleep(0.2)
        return ModelReply(text="late")


REQ = ModelRequest(role="dm", actor="dm", task="t", prompt="hi")
OK_REPLY = ModelReply(text="ok")


# --------------------------------------------------------------------------- #
section("R1 默认口径：单次尝试，行为与以前一样")

c = Flaky([ModelError("5xx", retryable=True)])
r = c.complete(REQ)
ok(not r.ok and c.seen == 1 and c.calls[-1].attempts == 1,
   "R1 默认不重试（只发 1 次）", (c.seen, c.calls[-1].attempts))
ok(c.policy.max_attempts == 1 and c.policy.backoff == (),
   "R1b 默认策略就是 RetryPolicy()", c.policy)


# --------------------------------------------------------------------------- #
section("R2 哪些错误才重试")

clock = FakeClock()
c = Flaky([ModelError("429", retryable=True), OK_REPLY],
          policy=RetryPolicy(max_attempts=3, backoff=(0.5, 1.0)),
          sleep=clock.sleep, clock=clock)
r = c.complete(REQ)
ok(r.ok and c.seen == 2, "R2a retryable 的错误会重试一次然后成功",
   (r.ok, c.seen))
ok(clock.sleeps == [0.5], "R2b 只睡了一次，且是第一条退避", clock.sleeps)
ok(c.calls[-1].attempts == 2, "R2c 记账里写了「试 2 次」", c.calls[-1].attempts)

clock = FakeClock()
c = Flaky([ModelError("JSON 解析失败（重试没用）", retryable=False), OK_REPLY],
          policy=RetryPolicy(max_attempts=3, backoff=(0.5,)),
          sleep=clock.sleep, clock=clock)
r = c.complete(REQ)
ok(not r.ok and c.seen == 1 and clock.sleeps == [],
   "R2d ⭐ 非 retryable 的错误不重试（不掩盖真 bug）", (c.seen, r.error))

c = Flaky([ValueError("代码 bug"), OK_REPLY],
          policy=RetryPolicy(max_attempts=3, backoff=(0.5,)),
          sleep=lambda s: None)
r = c.complete(REQ)
ok(not r.ok and c.seen == 1, "R2e 普通异常也不重试", (c.seen, r.error))


# --------------------------------------------------------------------------- #
section("R3 上限与退避")

clock = FakeClock()
errs = [ModelError("5xx", retryable=True) for _ in range(10)]
c = Flaky(errs, policy=RetryPolicy(max_attempts=5, backoff=(0.1, 0.2)),
          sleep=clock.sleep, clock=clock)
r = c.complete(REQ)
ok(not r.ok and c.seen == 5 and c.calls[-1].attempts == 5,
   "R3a 最多 5 次尝试（首发 + 4 次重试）", (c.seen, c.calls[-1].attempts))
ok(clock.sleeps == [0.1, 0.2, 0.2, 0.2],
   "R3b 退避用完后复用最后一个", clock.sleeps)

clock = FakeClock()
c = Flaky([ModelError("x", retryable=True), ModelError("y", retryable=True),
           OK_REPLY],
          policy=RetryPolicy(max_attempts=3, backoff=(0.4,)),
          sleep=clock.sleep, clock=clock)
r = c.complete(REQ)
ok(r.ok and clock.sleeps == [0.4, 0.4], "R3c 退避比尝试次数短 → 复用最后一个",
   clock.sleeps)


# --------------------------------------------------------------------------- #
section("R4 整手预算：用完就停，不再发请求")

clock = FakeClock()
c = Flaky([ModelError("5xx", retryable=True) for _ in range(10)],
          policy=RetryPolicy(max_attempts=5, backoff=(0.2,),
                             budget_seconds=0.3),
          sleep=clock.sleep, clock=clock)
r = c.complete(REQ)
ok(c.seen == 2, "R4a 预算 0.3s / 退避 0.2s → 只发得出去 2 次", c.seen)
ok(not r.ok and "预算" in r.error and "2 次" in r.error,
   "R4b ⭐ 错误说清是「预算用完」、试了几次", r.error)
ok(c.calls[-1].attempts == 2, "R4c 记账里也是 2 次", c.calls[-1].attempts)


# --------------------------------------------------------------------------- #
section("R5 关掉 / 记账 / 存档")

c = Flaky([OK_REPLY], enabled=False)
r = c.complete(REQ)
ok(not r.ok and c.seen == 0 and c.calls[-1].attempts == 0,
   "R5a enabled=False → 一次都不发，尝试数记 0", (c.seen, c.calls[-1].attempts))

clock = FakeClock()
c = Flaky([ModelError("5xx", retryable=True), OK_REPLY],
          policy=RetryPolicy(max_attempts=2, backoff=(0.3,)),
          sleep=clock.sleep, clock=clock)
c.complete(REQ)
ok(c.totals()["attempts"] == 2 and c.totals()["calls"] == 1,
   "R5b 总账：1 次调用 / 2 次尝试", c.totals())

st, led, loop = build_table_loop(seed=7)
loop.client = c
doc = dump_game(state=st, ledger=led, loop=loop)
sess = load_from_dict(doc, client=FakeModel(name="restored"))
ok(sess.loop.client.calls and sess.loop.client.calls[-1].attempts == 2,
   "R5c ⭐ 尝试次数随存档往返（读档后账没丢）",
   [x.attempts for x in sess.loop.client.calls])

ok(raises(lambda: RetryPolicy(max_attempts=0), ValueError),
   "R5d max_attempts=0 直接报错")
ok(raises(lambda: RetryPolicy(backoff=(-1,)), ValueError),
   "R5e 负退避直接报错")
ok(RetryPolicy(backoff=[0, 1]).backoff == (0.0, 1.0),
   "R5f 退避传 list 也会规整成 tuple")
ok(RetryPolicy(per_attempt_timeout=150).per_attempt_timeout == 150,
   "R5g 单次上限只是口径字段（传输层去执行）")
_slow = Slow(policy=RetryPolicy(max_attempts=1, per_attempt_timeout=0.05))
_r = _slow.complete(REQ)
ok(not _r.ok and "硬上限" in _r.error,
   "R5h ⭐ 单次调用超过硬墙钟上限 → 返回失败，不拖住整桌", _r.error)


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")