# coding=utf-8
"""真模型 smoke：**DM + 4 个玩家位全用同一款 DS**，只验流程能不能顺下来。

默认千帆 TokenPlan `deepseek-v4.1-flash`（OpenAI-compatible）。
key 从环境变量 `QIANFAN_API_KEY` 或 `~/.dsh/.credentials.yaml` 的 refs 读，
**不打印、不写进任何文件**。

用法：
    python tools\\real_smoke.py                 # 跑前 1 场（5 次左右调用）
    python tools\\real_smoke.py --scenes all    # 跑完整短剧本（含战斗）
    python tools\\real_smoke.py --scenes 2 --combat-rounds 1

退出码：0 = 流程跑通（允许有可恢复的格式问题，会列出来）；
        1 = 有场景没跑完/抛异常/状态没通过校验。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.adventure import (  # noqa: E402
    AdventureRunner, CampaignProgress, ProgressError, debt_text,
    load_adventure, load_campaign, progress_path_for,
)
from engine.adventure.campaign import chapter_path  # noqa: E402
from engine.dice import Random  # noqa: E402
from engine.models import RealModel, TableClient  # noqa: E402
from engine.models.client import _no_real_calls, read_api_key  # noqa: E402
from engine.demo_web import (  # noqa: E402
    load_seat_config, missing_seat_envs, seat_api_key, seat_spec,
)
from engine.rules.loop import GameLoop, make_hero_unit  # noqa: E402
from engine.state import Ledger, StateTree, World, new_character  # noqa: E402

DEFAULT_BASE = "https://qianfan.baidubce.com/v2/tokenplan/personal"
DEFAULT_MODEL = "deepseek-v4.1-flash"
SAMPLE = os.path.join(ROOT, "adventures", "哨塔的拖痕.json")
#: 真局存档默认落在 `saves/real/`，演示/假模型走 `saves/mock/`（Q04：两拨别混）。
SAVE_DIR = os.path.join(ROOT, "saves", "real")
#: trace 留痕的容量预算（Q04）：份数 + 总字节，两个闸都过。
TRACE_KEEP = 10
TRACE_MAX_BYTES = 256 * 1024 * 1024
AB = {"力量": 16, "敏捷": 14, "体质": 15, "智力": 10, "感知": 12, "魅力": 10}
PCS = [
    ("布兰", "战士", dict(AB)),
    ("艾尔玛", "盗贼", {**AB, "敏捷": 16, "力量": 12}),
    ("罗恩", "牧师", {**AB, "感知": 16, "力量": 13}),
    ("小翠", "法师", {**AB, "智力": 16, "力量": 8}),
]


def _read_key() -> str:
    """读 key（实现搬到 `engine.models.client.read_api_key`，这里保留名字给测试打桩）。"""
    return read_api_key()


class LoggedReal(RealModel):
    """给 smoke 用：留下原始文本 + 思考链 + prompt，方便复盘“它到底读了什么”。

    注意记录点必须是 `complete()`：`_complete()` 抛错时会被基类吞成失败
    `ModelReply`，失败调用就进不了 trace（外审 P1-6）。
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.texts: list[tuple[str, str, str]] = []
        self.records: list[dict] = []

    def complete(self, req):
        reply = super().complete(req)
        self.texts.append((req.task, reply.text, reply.error))
        self.records.append({
            "seat": req.role, "actor": req.actor, "task": req.task,
            "prompt": req.prompt, "text": reply.text,
            "reasoning": reply.reasoning, "ok": reply.ok,
            "error": reply.error, "model": reply.model,
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
            "cached_tokens": reply.cached_tokens,
            "elapsed": round(float(reply.elapsed), 3),
        })
        return reply


class CappedReal(LoggedReal):
    """共享一个调用计数器：到上限就拒绝再发请求（真钱场景的硬闸）。"""

    def __init__(self, counter: dict, cap: int, **kw):
        super().__init__(**kw)
        self.counter = counter
        self.cap = int(cap)

    def _complete(self, req):
        if self.counter["n"] >= self.cap:
            from engine.models import ModelError
            raise ModelError(f"smoke 调用上限 {self.cap} 到了（拒绝继续花钱）",
                             retryable=False)
        self.counter["n"] += 1
        return super()._complete(req)


def _scene_count(v: str):
    if v == "all":
        return "all"
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("--scenes 只认 1 / 2 / 3 / all")
    if not 1 <= n <= 3:
        raise argparse.ArgumentTypeError("--scenes 只认 1 / 2 / 3 / all")
    return str(n)


def build(seed: int, base: str, model: str, key: str, *, cap: int = 80,
          seat_cfg: dict | None = None, effort: str | None = None):
    counter = {"n": 0}
    st = StateTree.from_template(
        {name: new_character(name, level=3, job=job, abilities=dict(ab),
                             hp_max=24, ac=15) for name, job, ab in PCS},
        current=PCS[0][0])
    units = [make_hero_unit(st, name) for name, _job, _ab in PCS]

    def _client(label: str):
        spec = seat_spec(label, seat_cfg)
        eff = spec["reasoning_effort"] if "reasoning_effort" in spec else effort
        return CappedReal(
            counter, cap, name=label,
            model=str(spec.get("model") or model),
            base_url=str(spec.get("base_url") or base),
            api_key=seat_api_key(spec, key),
            max_tokens=int(spec.get("max_tokens", 12000)),
            temperature=float(spec.get("temperature", 0.7)),
            timeout=float(spec.get("timeout", 180.0)),
            reasoning_effort=eff, enabled=True)

    loop = GameLoop(st, Ledger(), TableClient(
        dm=_client("dm"),
        # 默认 max_tokens 已抬到 12000（用户口径：套餐便宜，让它多想想），
        # planner 不再单独抬；长跑批次里 5 敌混编两次推理占满就是这么修掉的。
        planner=_client("planner"),
        players={name: _client(f"pc:{name}") for name, _job, _ab in PCS},
        name="real-table"), rng=Random(seed), prompt_budget=6000)
    loop.set_field(units)
    w = World(st).ensure()
    w.discover("哨塔")
    w.travel("哨塔")
    return st, loop, counter


def seat_calls(loop) -> dict[str, list]:
    out: dict[str, list] = {}
    for label, client in loop.client.seats().items():
        out[label] = list(client.calls)
    return out


def write_trace(path: str, *, seed: int, model: str, adventure: str,
                scenes, seats: dict, note: str = "",
                keep: int = TRACE_KEEP,
                max_bytes: int = TRACE_MAX_BYTES) -> str:
    """把每次调用的 prompt / 正文 / 思考链写成 JSONL（复盘用）。

    一行 meta + 每行一次调用；**不写 key**。这是“看思维链有没有读对信息”
    的唯一原始材料 —— 没有它，事后只能看正文瞎猜。

    写完按「份数 + 总字节」裁同目录里最旧的 trace（Q04）：trace 一份就有
    1~2 MB，只靠 `.gitignore` 不解决磁盘被吃满。刚写的这份是最新的，不会被裁。
    """
    target = os.path.abspath(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        meta = {
            "type": "meta",
            "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "seed": int(seed), "model": model, "adventure": adventure,
            "scenes": [s.id for s in scenes], "note": note,
        }
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        seq = 0
        for seat, cli in seats.items():
            for rec in getattr(cli, "records", []):
                seq += 1
                f.write(json.dumps({"type": "call", "seq": seq,
                                    "seat_label": seat, **rec},
                                   ensure_ascii=False) + "\n")
    _prune_traces(target, keep=keep, max_bytes=max_bytes)
    return target


def _looks_like_trace(path: str) -> bool:
    """首行是不是本工具写的 meta 行。**只认自己写的**，别人的 JSONL 不删。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            first = f.readline(2000)
        rec = json.loads(first)
        return isinstance(rec, dict) and rec.get("type") == "meta"
    except (OSError, ValueError):
        return False


def _prune_traces(target: str, *, keep: int = TRACE_KEEP,
                  max_bytes: int = TRACE_MAX_BYTES) -> list[str]:
    """裁同目录里最旧的 trace（`*.jsonl` 且首行是本工具的 meta），返回删掉的路径。

    ⚠️ 两条保护：
    1. 只删**自己写的** trace —— 目录里可能有别人放的 JSONL，按扩展名乱删会误伤；
    2. 刚写的 `target` **永远不裁** —— 哪怕它的 mtime 因为复制/同步变老了，
       也不能把这次刚跑出来的原始材料删掉。字节预算先扣掉它的体积。
    """
    from engine.persist import prune_kept
    folder = os.path.dirname(os.path.abspath(target))
    me = os.path.abspath(target)
    olds = [os.path.join(folder, x) for x in os.listdir(folder)
            if x.endswith(".jsonl") and os.path.isfile(os.path.join(folder, x))]
    trace_like = [p for p in olds
                  if os.path.abspath(p) == me or _looks_like_trace(p)]
    others = [p for p in trace_like if os.path.abspath(p) != me]
    try:
        mine = os.path.getsize(target)
    except OSError:
        mine = 0
    removed = prune_kept(others, keep=max(0, int(keep) - 1),
                         max_bytes=(None if not max_bytes
                                    else max(0, int(max_bytes) - mine)))
    if max_bytes:
        # prune_kept 有意"至少留一份"（存档不能被删光）；trace 不一样：
        # 新档自己就超上限时，把别的全删掉才是对的，否则上限等于没生效。
        rest = [p for p in others if os.path.exists(p)]
        total = mine + sum(os.path.getsize(p) for p in rest)
        if total > int(max_bytes):
            removed += prune_kept(rest, keep=0, max_bytes=None)
    return removed


def default_save_path(save_dir: str, adventure: str, model: str) -> str:
    """真局默认存档名：同一剧本 + 同一模型**固定一个名字**。

    固定名字是有意的：覆盖前旧档进 `history/`，再按 ADR-118 的双阈值裁剪，
    不会因为每次换文件名就把 `saves/real/` 攒到撑爆磁盘。
    中文照留；只替换路径分隔符和 Windows 非法字符。
    """
    def _slug(text: str, fallback: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\s]+', "-", str(text or "")).strip("-.")
        return cleaned or fallback

    adv = _slug(os.path.basename(adventure).rsplit(".", 1)[0], "adventure")
    mdl = _slug(model, "model")
    return os.path.join(save_dir, f"{adv}-{mdl}.json")


def save_path_for(args) -> str:
    """这次跑完该往哪写终局存档（空串 = 不写）。

    - 显式 `--save`：写它；
    - `--only` 单场复验且没给 `--save`：**不写**——否则会把
      `saves/real/<剧本>-<模型>.json` 覆盖成“只跑了一场”的半截档（外审 P2-6）；
    - 其余：写 `saves/real/<剧本>-<模型>.json`。
    """
    if args.save:
        return str(args.save)
    if getattr(args, "only", ""):
        return ""
    if getattr(args, "campaign", ""):
        return default_save_path(args.save_dir, args.campaign, args.model)
    return default_save_path(args.save_dir, args.adventure, args.model)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真模型 5 座 DS smoke")
    ap.add_argument("--scenes", type=_scene_count, default="1",
                    help="跑几场：1 / 2 / 3 / all（默认 1）")
    ap.add_argument("--combat-rounds", type=int, default=2)
    ap.add_argument("--max-calls", type=int, default=80,
                    help="硬闸：整场最多发几次请求（默认 80）")
    ap.add_argument("--allow-fallback", action="store_true",
                    help="有失败调用也返回 0（默认失败即非零）")
    ap.add_argument("--require-combat-end", action="store_true",
                    help="战斗到轮数上限仍未结束时返回 1")
    ap.add_argument("--seed", type=int, default=20261106)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--seat-config", default=None,
                    help="每座位模型 JSON（dm/planner/pc:名字 各自 model/base_url/api_key_env）")
    ap.add_argument("--reasoning-effort", default=None,
                    help="单模型默认档；座位配置里的 reasoning_effort 优先（null=不发送）")
    ap.add_argument("--adventure", default=SAMPLE,
                    help="剧本 JSON（默认哨塔；M1 用 adventures/雾渡的第三声钟.json）")
    ap.add_argument("--trace", default="",
                    help="把每次调用的 prompt/正文/思考链写成 JSONL（复盘用）")
    ap.add_argument("--trace-keep", type=int, default=TRACE_KEEP,
                    help=f"trace 目录最多留几份（默认 {TRACE_KEEP}）")
    ap.add_argument("--trace-max-mb", type=int, default=TRACE_MAX_BYTES // (1 << 20),
                    help="trace 目录总量上限 MB（默认 256；0 = 不限）")
    ap.add_argument("--save", default="",
                    help="跑完把终局存档写到这个路径（不给就自动写进 saves/real/）")
    ap.add_argument("--save-dir", default=SAVE_DIR,
                    help="自动存档目录（默认 saves/real：真局与演示分开放）")
    ap.add_argument("--only", default="",
                    help="只跑这几场（逗号分隔的场次 id，如 s3 或 s3,s5）；"
                         "给它就忽略 --scenes —— 小额复验/复现单场用")
    ap.add_argument("--campaign", default="",
                    help="战役 JSON：给了就按章连跑（配 --chapters），"
                         "前一章的欠账会在后一章开头交给 DM")
    ap.add_argument("--chapters", default="",
                    help="章 id（逗号分隔，按给的顺序跑），如 ch1,ch2；"
                         "只有 --campaign 时有效")
    args = ap.parse_args(argv)

    if _no_real_calls():
        print("❌ 检测到 DND_NO_REAL_CALLS=1：真调用被进程闸门禁用。")
        print("   要跑真模型批次，先显式取消该环境变量（当前 shell：")
        print("   PowerShell: Remove-Item Env:DND_NO_REAL_CALLS）")
        return 2

    # ⚠️ 参数与剧本先在**读 key / 建客户端之前**校验：场次写错不该先花钱。
    # 计划 = 一串 (标签, 剧本, 剧本路径, 场次, 本章欠账)。单本跑就是一串一项。
    plan: list[tuple[str, object, str, list]] = []
    if args.campaign:
        if args.only:
            print("❌ --only 只对单本有效；按章跑请用 --chapters")
            return 2
        camp = load_campaign(args.campaign)
        want = [x.strip() for x in args.chapters.split(",") if x.strip()]
        if not want:
            print(f"❌ 给了 --campaign 就要 --chapters"
                  f"（这一战役的章：{[c.id for c in camp.chapters]}）")
            return 2
        known_ch = {c.id: c for c in camp.chapters}
        bad = [x for x in want if x not in known_ch]
        if bad:
            print(f"❌ 战役里没有这些章：{bad}（现有：{list(known_ch)}）")
            return 2
        for cid in want:
            path = chapter_path(camp, known_ch[cid])
            adv_c = load_adventure(path)
            picked = (adv_c.scenes if args.scenes == "all"
                      else adv_c.scenes[:int(args.scenes)])
            if not picked:
                print(f"❌ 章 {cid} 选中的场次是空的")
                return 2
            plan.append((cid, adv_c, path, picked))
    else:
        adv = load_adventure(args.adventure)
        if args.only:
            want_sc = [x.strip() for x in args.only.split(",") if x.strip()]
            known = {s.id: s for s in adv.scenes}
            bad = [x for x in want_sc if x not in known]
            if bad:
                print(f"❌ --only 里有剧本没有的场次：{bad}（现有：{list(known)}）")
                return 2
            scenes = [s for s in adv.scenes if s.id in want_sc]  # 按剧本顺序
        else:
            scenes = (adv.scenes if args.scenes == "all"
                      else adv.scenes[:int(args.scenes)])
        if not scenes:
            print("❌ 选中的场次是空的（--scenes / --only 至少给一场）")
            return 2
        plan.append(("", adv, args.adventure, scenes))

    scenes = [s for _lab, _adv, _p, scs in plan for s in scs]

    seat_cfg = None
    if args.seat_config:
        try:
            seat_cfg = load_seat_config(args.seat_config)
        except Exception as exc:                     # noqa: BLE001
            print(f"❌ 座位配置读不了：{type(exc).__name__}: {exc}")
            return 2
        missing = missing_seat_envs(seat_cfg)
        if missing:
            print("❌ 座位配置里的环境变量未设置：" + "，".join(missing))
            return 2

    key = _read_key()
    if not key and seat_cfg is None:
        print("❌ 没有 QIANFAN_API_KEY（环境变量或 ~/.dsh/.credentials.yaml）")
        return 2

    st, loop, counter = build(args.seed, args.base_url, args.model, key,
                              cap=args.max_calls, seat_cfg=seat_cfg,
                              effort=args.reasoning_effort)

    runners: list = []
    shown = "mixed" if seat_cfg is not None else args.model
    print(f"模型 {shown}｜{len(scenes)} 场｜4 PC + DM + planner"
          + (f"｜战役 {len(plan)} 章" if args.campaign else ""))
    t0 = time.time()
    problems: list[str] = []
    for label, adv_c, adv_path, picked in plan:
        # 欠账**在轮到时才取**：必须包含前面几章刚跑出来的“提过/已兑现”。
        # 在拼计划时提前算，会拿到空进度，把上一章已还的伏笔又列一遍（实测踩过）。
        debt = debt_text(camp, label) if args.campaign else ""
        progress = None
        if args.campaign:
            prog_path = progress_path_for(adv_path)
            try:
                progress = CampaignProgress.load(
                    prog_path, adventure=os.path.basename(adv_path))
            except (ProgressError, OSError) as exc:
                print(f"⚠️ 章 {label} 的进度文件读不出来（按新局记）：{exc}")
                progress = CampaignProgress(
                    adventure=os.path.basename(adv_path), path=prog_path)
        chapter = camp.chapter(label) if (args.campaign and label) else None
        runner = AdventureRunner(loop, adv_c, progress=progress,
                                 campaign_debt=debt,
                                 campaign=(camp if args.campaign else None),
                                 chapter_id=label,
                                 rest=(chapter.rest if chapter else ""))
        runner.seed_map()
        runners.append(runner)
        if label:
            print(f"\n── 章 {label}《{adv_c.title}》"
                  + (f"｜欠账 {len(debt)} 字" if debt else "｜无欠账")
                  + ("｜开场长休" if runner.rest == "long" else ""))
        for scene in picked:
            before = len(loop.logs)
            try:
                runner.run_scene(scene, combat_rounds=args.combat_rounds)
            except Exception as exc:                     # noqa: BLE001
                print(f"❌ {scene.id} 抛异常：{type(exc).__name__}: {exc}")
                return 1
            rounds = loop.logs[before:]
            for r in rounds:
                problems.extend(f"{scene.id}/{r.n}: {p}" for p in r.problems)
            print(f"  {scene.id}《{scene.title}》→ {len(rounds)} 轮，"
                  f"问题 {sum(len(r.problems) for r in rounds)}")

    calls = seat_calls(loop)
    total_in = sum(c.prompt_tokens for rows in calls.values() for c in rows)
    total_out = sum(c.completion_tokens for rows in calls.values() for c in rows)
    total_cached = sum(getattr(c, "cached_tokens", 0)
                       for rows in calls.values() for c in rows)
    fails = [c for rows in calls.values() for c in rows if not c.ok]
    print("\n座位调用：")
    for label, rows in calls.items():
        ok = sum(1 for c in rows if c.ok)
        print(f"  {label:8s} {len(rows):2d} 次（成功 {ok}）  "
              f"入 {sum(c.prompt_tokens for c in rows)} / "
              f"出 {sum(c.completion_tokens for c in rows)}"
              + (f" / 缓存 {sum(getattr(c, 'cached_tokens', 0) for c in rows)}"
                 if total_cached else ""))

    print(f"\n合计 {sum(len(r) for r in calls.values())} 次调用，"
          f"入 {total_in} / 出 {total_out} tokens，"
          f"{time.time() - t0:.1f}s")
    if total_cached:
        print(f"提示缓存命中 {total_cached} tokens"
              f"（{total_cached / total_in:.0%}）")
    else:
        print("提示缓存：上游没返回 cached_tokens（不是 0 命中的证明）")
    if args.trace:
        trace = write_trace(args.trace, seed=args.seed,
                        model=("mixed" if seat_cfg is not None else args.model),
                            adventure=(os.path.basename(args.campaign) if args.campaign
                                       else os.path.basename(args.adventure)),
                            scenes=scenes, seats=loop.client.seats(),
                            note=("smoke " + (f"only={args.only}" if args.only
                                              else f"scenes={args.scenes}")
                                  + f" max_calls={args.max_calls}"),
                            keep=args.trace_keep,
                            max_bytes=max(0, args.trace_max_mb) * (1 << 20))
        print(f"🧾 调用留痕（含思考链）：{trace}")
    if fails:
        print(f"失败调用 {len(fails)} 次：")
        for c in fails[:8]:
            print(f"  ✗ {c.role}/{c.actor} {c.task}: {c.error[:100]}")
    if problems:
        print("\n战斗段原始回应（每座最多 2 条，截 300 字）：")
        for label, cli in loop.client.seats().items():
            shown = 0
            for task, text, err in getattr(cli, "texts", []):
                if text and task in ("plan_all_enemies", "combat_turn"):
                    print(f"  [{label}/{task}] {text[:300]!r}")
                    shown += 1
                    if shown >= 2:
                        break
        print(f"\n可恢复问题 {len(problems)} 条（前 8）：")
        for p in problems[:8]:
            print(f"  · {p}")
    try:
        st.validate()
    except Exception as exc:                             # noqa: BLE001
        print(f"❌ 状态校验失败：{exc}")
        return 1

    save_path = save_path_for(args)
    if not save_path and args.only and not args.save:
        print("（--only 复验：不自动写存档；要存请显式给 --save）")
    if save_path:
        from engine.persist import save_game
        try:
            path = save_game(save_path, state=st, ledger=loop.ledger, loop=loop,
                             note=(f"真模型 {args.model} seed={args.seed} "
                                   f"adventure=" + (os.path.basename(args.campaign) if args.campaign else os.path.basename(args.adventure)) + " "
                                   f"trace={args.trace or '-'}"))
            print(f"💾 终局存档：{path}")
        except Exception as exc:                         # noqa: BLE001
            # 花了钱跑完的结论不能因为写盘失败就丢；照实说没存上
            print(f"⚠️ 终局存档失败（不影响上面的结论）：{exc}")

    done = [s.id for s in scenes
            if any(s.id in r.scenes_done for r in runners)]
    if len(done) != len(scenes):
        print(f"❌ 有场景没跑完：{done}")
        return 1

    unfinished = []
    for s in scenes:
        if getattr(s, "combat", None):
            alive = [u for u in loop.field if u.team == "enemy" and u.alive]
            if alive:
                unfinished.append(f"{s.id}（余 {len(alive)} 敌）")
    if unfinished:
        print(f"战斗结果：未结束 {unfinished}（轮数上限 {args.combat_rounds}）")
        if args.require_combat_end:
            print("❌ --require-combat-end：战斗没打完")
            return 1

    used = {label: len(rows) for label, rows in calls.items()}
    has_combat = any(getattr(s, "combat", None) for s in scenes)
    empty = [label for label, n in used.items()
             if n == 0 and not (label == "planner" and not has_combat)]
    if empty:
        print(f"❌ 有座位一次都没被调用：{empty}")
        return 1
    if fails and not args.allow_fallback:
        print(f"❌ 有 {len(fails)} 次失败调用（要容忍请加 --allow-fallback）")
        return 1
    if counter["n"] >= args.max_calls:
        print(f"⚠️ 已到调用上限 {args.max_calls}（后续未再发请求）")
    print("\n✅ 流程跑通（真模型；问题清单见上，未掩盖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())







