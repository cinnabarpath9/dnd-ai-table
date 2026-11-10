# coding=utf-8
"""把剧本推到牌桌上：世界播种 → 同桌轮 → 战斗。

分工很硬：

    · 剧本（`adventure.json`）管"这一局有哪几场、每场 DM 该知道什么"；
    · 引擎（`GameLoop` / `World` / `Unit`）管"发生了什么、数值是多少"；
    · **剧本不写数值** —— HP、骰子、法术位永远只在引擎里。

所以这一层只做三件事：

    1. 进场景时把**地点/时间**播种进世界状态（走 `World` 的 commit，留审计）；
    2. 把单场简报（在场与主动性 / 时间跨度 / 离场）只塞进 **DM 的**任务说明；
    3. 场景配了 `combat` 就按它开战。

⚠️ 简报**不进玩家提示词**。这不是"提醒 DM 别说漏"，而是"玩家根本拿不到"。
回归里有一条专门钉这个（DM 提示词有「离场」，玩家提示词没有）。
"""

from __future__ import annotations

from .model import (
    Adventure, AdventureError, Scene, campaign_guide, campaign_player_goal,
    clue_brief_text, dm_brief_text,
)
from . import progress as _progress_mod
from .progress import CampaignProgress, ProgressError
from ..memory import parse_game_time
from ..rules.loop import GameLoop, RoundLog
from ..rules.persona import NpcState, roll_persona

__all__ = ["AdventureRunner"]


class AdventureRunner:
    """一段剧本的推进器。**不产生模型调用**，只调用循环与世界状态。"""

    def __init__(self, loop: GameLoop, adventure: Adventure, *,
                 progress: CampaignProgress | None = None,
                 campaign_debt: str = "",
                 campaign=None, chapter_id: str = "",
                 rest: str = "", on_scene_end=None):
        if not isinstance(loop, GameLoop):
            raise TypeError(f"AdventureRunner 需要 GameLoop，拿到 {type(loop).__name__}")
        if not isinstance(adventure, Adventure):
            raise TypeError(f"AdventureRunner 需要 Adventure，"
                            f"拿到 {type(adventure).__name__}")
        if progress is not None and not isinstance(progress, CampaignProgress):
            raise TypeError(f"progress 需要 CampaignProgress，"
                            f"拿到 {type(progress).__name__}")
        self.loop = loop
        self.adv = adventure
        #: 每场收束后的存档点；由网页层提供，测试/终端不传就不落盘。
        self.on_scene_end = on_scene_end
        #: 区域地图（旅行层）只读数据；没有就不画，不凭标题造假地图。
        self.loop.region_map = (adventure.meta.get("地图")
                                if isinstance(adventure.meta.get("地图"), dict)
                                else None)
        #: 全团倒地：run_all 停在这里，由网页层决定自动读档或失败收束。
        self.failed = False
        self.failure_reason = ""
        #: 玩家可见的长期目标；每轮玩家提示词都带上，避免 AI 不知道为什么上路。
        loop.campaign_goal = campaign_player_goal(adventure)
        #: 跨局记忆（可空）。有它才记 beat、才在首场给 DM 看上一局的欠账。
        self.progress = progress
        #: 跨剧本欠账（战役层 P5-D2）：第 N 章开局时由 `campaign.debt_text()` 生成，
        #: **只拼进首场 DM 的任务说明**，玩家提示词里一个字都没有。
        self.campaign_debt = str(campaign_debt or "")
        #: 战役（可空）：有它才能兑现**前面章节**的伏笔（`scene.pays_external`）。
        self.campaign = campaign
        self.chapter_id = str(chapter_id or "")
        self._prev_progress: dict = {}
        self.logs: list[RoundLog] = []
        #: 与 loop 的进度保持同步：读档回来已跑过的场不再重复跑。
        self.scenes_done: list[str] = list(getattr(loop, "scenes_done", []))
        #: 开局块（跨局/跨章欠账）**每本剧本只给一次**。
        #: 旧写法用 `_first`（=这一局的第一场）兼职两件事：既表示"新开一局"，
        #: 又决定"要不要塞欠账"；续跑时它早就是 False，欠账被静默丢掉。
        #: 现在拆开：要不要塞看"是不是本剧本第一场 + 还没塞过"，与存档无关。
        self._opening_done = False
        self._npcs_ready = False
        self._map_ready = False
        loop.set_story_position(adventure=self.adv.title)
        #: 章节之间的休整（Q35）：`"long"` = 开场先长休一次。
        #: 只认 "" / "long"；写错不猜（配置错应该早点红）。
        if rest not in ("", "long"):
            raise ValueError(f"rest 只能是 \"\" 或 \"long\"，拿到 {rest!r}")
        self.rest = rest
        self.rest_summary: dict = {}
        if self.rest == "long":
            # 外审 B4：同一章重复构造 runner（断档重开、重跑同章）不能反复回血。
            # 进度文件记 `rest_done`；没有进度文件（单本/测试）时只能按次算。
            already = bool(self.progress is not None
                           and getattr(self.progress, "rest_done", False))
            if already:
                self.rest_summary = {"rested": [], "revived": [],
                                     "stayed_dead": [], "skipped": "已休过"}
            else:
                self.rest_summary = loop.long_rest(
                    reason=f"《{self.adv.title}》开场前长休")
                if self.progress is not None:
                    self.progress.rest_done = True
                    try:
                        self.progress.save()
                    except Exception as exc:        # noqa: BLE001
                        self.rest_summary["note"] = f"进度落盘失败：{exc}"

    # ---------- 提示词 ----------

    def dm_hint(self, scene: Scene) -> str:
        """DM 的任务说明。**简报、可发现的痕迹与跨局欠账只在这里出现。**"""
        parts = ["请描述当前场景，并说明轮到冒险者行动了。"]
        guide = campaign_guide(self.adv)
        if guide:
            parts.append("【剧本四要素 · DM 全程方向，别直接念给玩家】\n" + guide)
        parts.extend([dm_brief_text(scene, self.adv),
                      clue_brief_text(scene, self.loop.world.found_clues())])
        # 欠账的注入点是**这本剧本的第一场**，与"这一局的第一场"无关：
        # 同一个存档连着跑两章时，第二章的第一场也要拿到上一章的欠账
        #（M2 测试本 E3 抓到过旧实现把它丢掉）。**同一局只塞一次**：
        # 重跑第一场不再重复塞（`_opening_done`）。
        opening = (bool(self.adv.scenes)
                   and scene.id == self.adv.scenes[0].id
                   and not self._opening_done)
        if opening:
            self._opening_done = True
        if opening and self.progress is not None:
            debt = self.progress.debt_text(self.adv)
            if debt:
                parts.append(debt)
        if opening and self.campaign_debt:
            parts.append(self.campaign_debt)
        return "\n".join(p for p in parts if p)

    def _act_of(self, scene: Scene) -> str:
        """这一场属于哪一幕（没归幕返回空串）。"""
        for act in self.adv.acts:
            if scene.id in act.scenes:
                return act.id
        return ""

    # ---------- 世界节点图（U13） ----------

    def seed_map(self) -> None:
        """把剧本 `map` 里的点与通道播种进世界树（**幂等**，重复跑不重置）。

        顺序有讲究：
          1. **先把所有场次会去的地点登记成"存在但未发现"** —— 否则第一场就
             登记通道时，第三场才去的地点还不存在，通道两端就没有着落；
          2. 再登记 `map.places`（可带 `discovered` / `heard` 初始可见度）；
          3. 最后登记通道。剧本是**唯一真相**，世界树只是照着播种。
        """
        if self._map_ready:
            return
        w = self.loop.world
        for sc in self.adv.scenes:
            loc = sc.world_location
            if loc and w.location(loc) is None:
                w.register_location(
                    loc,
                    description=str((sc.world or {}).get("location_description")
                                    or ""),
                    actor="DM",
                    reason=f"剧本《{self.adv.title}》预先登记场景地点")
        for p in self.adv.map.get("places") or []:
            name = str(p.get("name") or "").strip()
            if not name or w.location(name) is not None:
                continue
            w.register_location(
                name, description=str(p.get("description") or ""),
                region=str(p.get("region") or ""),
                discovered=bool(p.get("discovered")),
                heard=bool(p.get("heard")),
                actor="DM", reason=f"剧本《{self.adv.title}》登记地点")
        for p in self.adv.map.get("places") or []:
            name = str(p.get("name") or "").strip()
            if name and p.get("shop") and w.location(name) is not None:
                w.set_shop(name, list(p["shop"]), actor="DM",
                           reason=f"剧本《{self.adv.title}》登记商店货架")
        for p in self.adv.map.get("passages") or []:
            a = str(p.get("from") or "").strip()
            b = str(p.get("to") or "").strip()
            if not a or not b or w.passage(a, b) is not None:
                continue
            w.add_passage(a, b, note=str(p.get("note") or ""),
                          known=bool(p.get("known", True)),
                          secret=str(p.get("secret") or ""),
                          actor="DM",
                          reason=f"剧本《{self.adv.title}》登记通道")
        self._map_ready = True

    # ---------- NPC 人格 ----------

    def register_npcs(self) -> None:
        """剧本里的 NPC：**跑第一场时掷五骰**，写进世界树（只给 DM）。

        重复调用/重复登记都跳过 —— 读档回来已经有人格，不要再掷一次。
        """
        if self._npcs_ready:
            return
        w = self.loop.world
        for seed in self.adv.npcs:
            name = str(seed.get("name") or "").strip()
            role = str(seed.get("role") or "").strip()
            if not name or w.npc(name) is not None:
                continue
            persona = roll_persona(self.loop.rng, name=name, role=role)
            ag = next((a for a in self.adv.agenda if a.who == name), None)
            state = NpcState(goal_short=(ag.next if ag else ""))
            w.register_npc(name, role=role, persona=persona.to_dict(),
                           state=state.to_dict(), actor="DM",
                           reason=f"剧本《{self.adv.title}》登记 NPC")
        self._npcs_ready = True

    # ---------- 世界 ----------

    def apply_world(self, scene: Scene) -> None:
        """把这一场的地点/时间播种进世界状态（有审计、可读档）。"""
        info = scene.world or {}
        w = self.loop.world

        date = str(info.get("date") or "").strip()
        period = str(info.get("period") or "").strip()
        weather = str(info.get("weather") or "").strip()
        if date or period or weather:
            w.advance_time(date=date or None, period=period or None,
                           weather=weather or None, actor="DM",
                           reason=f"进入场景 {scene.id}")

        loc = scene.world_location
        if loc:
            w.discover(loc, description=str(info.get("location_description") or ""),
                       actor="DM", reason=f"进入场景 {scene.id}")
            if w.current_location != loc:
                w.travel(loc, actor="DM", reason=f"进入场景 {scene.id}")

    # ---------- 战斗 ----------

    def run_combat(self, scene: Scene, *, rounds: int | None = None) -> list[RoundLog]:
        c = scene.combat
        if not c:
            return []
        enemies = [(str(e[0]), int(e[1])) for e in c["enemies"]]
        n_rounds = int(c.get("rounds", 4)) if rounds is None else int(rounds)
        made: list[RoundLog] = []

        self.logs.append(self.loop.start_combat(
            enemies=enemies, distance=float(c.get("distance", 30))))
        made.append(self.logs[-1])
        for _ in range(max(0, n_rounds)):
            if not self.loop.alive_enemies or not self.loop.alive_pcs:
                break
            self.logs.append(self.loop.run_combat_round())
            made.append(self.logs[-1])
        # 按轮数收场时，可能还有活着的敌人；不能静默撤走而不给收尾。
        alive_enemies = [str(getattr(u, "name", "")) for u in self.loop.alive_enemies]
        alive_pcs = list(self.loop.alive_pcs)
        if alive_enemies:
            names = "、".join(x for x in alive_enemies if x) or "残余敌人"
            if alive_pcs:
                text = (f"【DM】战斗在 {n_rounds} 轮后收束：{names} 没有倒下，"
                        "趁乱退走；你们暂时守住这里。")
            else:
                text = (f"【DM】战斗在 {n_rounds} 轮后收束：{names} 仍站着，"
                        "你们已无力再战，只能先撤。")
        elif not alive_pcs:
            text = "【DM】战斗结束：你们倒下，敌人没有继续追击。"
        else:
            text = "【DM】战斗结束：敌人已经全部倒下。"
        endlog = RoundLog(n=self.loop.round, phase="combat", sink=self.loop.on_line)
        endlog.add(text)
        self.logs.append(endlog)
        made.append(endlog)
        return made

    def _close_combat(self) -> None:
        """离开一场时的收场：撤走没打完的敌人、清轮次修正、收先攻序。

        **不在战斗刚跑完时做** —— 否则 `combat_rounds=0` 的调用方
        还没看到刚挂上的效果就被清掉了（t_effects D4/D5 的教训）。
        """
        if not getattr(self.loop, "initiative", None):
            return
        from ..rules.effects import clear_mods
        for u in self.loop.field:
            clear_mods(u, scope="round")
        self.loop.field = [u for u in self.loop.field if u.team != "enemy"]
        self.loop.end_combat()

    # ---------- 场景 ----------

    def run_scene(self, scene: Scene, *, combat_rounds: int | None = None
                  ) -> list[RoundLog]:
        """跑一场：世界播种 → 同桌轮（每人一次）→ 有战斗就开打。"""
        if not any(s.id == scene.id for s in self.adv.scenes):
            raise AdventureError([f"剧本 {self.adv.title!r} 里没有场景 "
                                   f"{scene.id!r}"])
        start = len(self.logs)
        # 进新场景前先收上一场：没打完的敌人/轮次修正不会跟进；
        # 同场景重跑也一样收（否则第二遍会接着第一遍的战场）。
        self._close_combat()
        self.loop.set_story_position(adventure=self.adv.title,
                                     act=self._act_of(scene),
                                     scene=scene.id, status="running")
        self.register_npcs()
        self.seed_map()                  # 先把地点/通道播进世界树
        self.apply_world(scene)          # 再定地点，线索才知道"在哪撞见"
        self.register_clues(scene)
        self.set_scene_context(scene)
        self.logs.append(self.loop.run_explore_round_all(
            situation=scene.situation, task_hint=self.dm_hint(scene)))
        if scene.combat:
            self.run_combat(scene, rounds=combat_rounds)
        self.scenes_done.append(scene.id)
        self.loop.mark_scene_done(scene.id)
        remaining = [s.id for s in self.adv.scenes
                     if s.id not in self.scenes_done]
        self.loop.set_story_position(
            status="done" if not remaining else "running")
        if self.progress is not None:
            # **跑完一场记一笔**：本场 `promises` 记 beat、`pays` 标已兑现。
            # 纯内存/有 path 都会走同一条；有 path 时由 progress 自己原子落盘。
            self.progress.record_scene(scene)
        self._pay_external(scene)
        self._record_memory(scene)
        return self.logs[start:]

    # ---------- 跨章兑现（Q23） ----------
    def _pay_external(self, scene: Scene) -> None:
        """把本场兑现的**前面章节**伏笔，关在那一章的进度文件里。

        欠账 `campaign.debts()` 读的是各章自己的进度；只在本章记一笔，
        下一章的开局仍然会把它列成"还欠着"（Q23）。
        """
        refs = getattr(scene, "pays_external", ()) or ()
        if not refs or self.campaign is None:
            return
        from .campaign import chapter_path as _chapter_path
        from .model import load_adventure as _load_adv

        for ref in refs:
            cid, _, pid = str(ref).partition(":")
            if not cid or not pid:
                continue
            chapter = self.campaign.chapter(cid)
            adv_prev = _load_adv(_chapter_path(self.campaign, chapter))
            prog = self._prev_progress.get(cid)
            if cid in self._prev_progress and prog is None:
                continue          # 上次已经确认读不出来，不反复重试
            if prog is None:
                try:
                    prog = CampaignProgress.load(
                        _progress_mod.progress_path_for(_chapter_path(self.campaign, chapter)),
                        adventure=adv_prev.title)
                except (ProgressError, OSError) as exc:
                    # 外审 A3：上一章进度文件坏了，不能从场景中段炸出去。
                    # 跳过这次兑现，但在日志里说清（不静默）。
                    if self.logs:
                        self.logs[-1].add(
                            f"⚠️ 跨章兑现失败（{cid}:{pid}）：上一章进度读不出来")
                    self._prev_progress[cid] = None
                    continue
                self._prev_progress[cid] = prog
            where = (f"在章 {self.chapter_id or '?'} 的《{scene.title}》"
                     if self.chapter_id else f"在《{scene.title}》")
            # 外审 A7：close() 默认就落盘；这里统一改成"关掉+一次 save"
            if prog.close(pid, note=f"{where}兑现", save=False):
                prog.save()
                if self.logs:
                    self.logs[-1].add(f"🔗 跨章兑现：{cid}:{pid}（{where}）")

    # ---------- 记忆（P5-G） ----------

    def _record_memory(self, scene: Scene) -> None:
        """跑完一场，往记忆库登记一条 AM。

        口径：
          · 时间：剧本写了 `world.date/period` 就用它；**没写就退回世界树当前的
            时间**（世界树是"现在几点"的唯一真相 —— 这一场没推进时间，
            就发生在当前时刻）。两者都没有才是"时间不明"；
          · 线索标签直接用剧本的 thread / promise id —— 跨场共享同一条因果线时，
            召回就能凭"同一条线"把人拉回来（这是预设里最强的 REAL LINK）；
          · 幂等键 = 场景 id：同一场重复跑不重复记（与 P5-D 的"一提一笔"同理）；
          · 可见性 public：这一场是玩家亲历的。**幕后（`brief.offstage`）
            不在这里登记** —— 那是 P5-G2 的事，别用 public 记忆把它夹带出去。
        """
        store = getattr(self.loop, "memory", None)
        if store is None:
            return
        info = scene.world or {}
        start = parse_game_time(str(info.get("date") or ""),
                                str(info.get("period") or ""))
        if not start.known:
            clock = self.loop.world.clock
            start = parse_game_time(str(clock.get("日期") or ""),
                                    str(clock.get("时段") or ""))
        people = tuple(u.name for u in self.loop.field
                       if u.team in ("pc", "ally"))
        place = scene.world_location
        summary = f"《{scene.title}》" + _short(scene.situation, 60)
        store.record(
            summary,
            start=start if start.known else None,
            end=start if start.known else None,
            people=people,
            places=(place,) if place else (),
            tags=tuple(scene.threads) + tuple(scene.promises),
            visible_to="public",
            source=scene.id, key=scene.id,
        )

    def set_scene_tags(self, scene: Scene) -> None:
        """把本场的线索 id 交给循环，当"现在正推进哪几条线"的召回依据。

        （保留旧名字：老调用方/测试还在用；新代码走 `set_scene_context`。）
        """
        self.loop.memory_tags = tuple(scene.threads) + tuple(scene.promises)

    # ---------- 线索（P5-G2） ----------

    def register_clues(self, scene: Scene) -> None:
        """把本场"可发现的痕迹"登记进世界树（**幂等**：重复跑不重置状态）。

        只登记"哪条痕迹存在、在哪个地点、发现没有"；线索正文留在剧本里，
        世界树存一份是为了 DM 视角能自查"还有哪几条没被发现"，也为了读档后
        "这条已经发现过了"不会丢。
        """
        if not scene.clues:
            return
        place = scene.world_location
        for c in scene.clues:
            self.loop.world.register_clue(
                c.id, scene=scene.id, place=c.place or place,
                skill=c.skill, dc=c.dc, text=c.text, secret=c.secret,
                source=c.source, actor="DM",
                reason=f"剧本《{self.adv.title}》场景 {scene.id} 的痕迹")

    def set_scene_context(self, scene: Scene) -> None:
        """本场线索 + 正在推进的线索 id + 场景临时修正，一起交给循环。"""
        self.loop.set_scene_context(
            scene_id=scene.id,
            clues=tuple(scene.clues),
            tags=tuple(scene.threads) + tuple(scene.promises),
            effects=list(scene.effects))

    def run_all(self, *, combat_rounds: int | None = None,
                resume: bool = False) -> list[RoundLog]:
        """按剧本顺序跑完所有场景（测试与自动 demo 用）。

        `resume=True` 时跳过 `loop.scenes_done` 里已跑完的场 —— 读档续跑用。
        """
        self.seed_map()                  # 全跑完再读档回来也要把地图补齐
        for scene in self.adv.scenes:
            if resume and scene.id in self.scenes_done:
                continue
            self.run_scene(scene, combat_rounds=combat_rounds)
            if not self.loop.alive_pcs:
                self.failed = True
                self.failure_reason = "全团倒地：没有可行动的 PC"
                self.loop.set_story_position(status="failed")
                break
            if self.on_scene_end is not None:
                try:
                    self.on_scene_end(scene)
                except Exception as exc:             # noqa: BLE001
                    # 检查点失败不能把正在跑的一局炸掉；但要在日志里说清。
                    if self.logs:
                        self.logs[-1].add(f"⚠️ 检查点保存失败：{exc}")
        self._close_combat()
        return self.logs


    def run_from(self, scene_id: str, *,
                 combat_rounds: int | None = None) -> list[RoundLog]:
        """**回到某一场重跑**：前面的场景不重放，从这一场跑到结尾。

        口径：
          · 前面的场景必须都已经跑完（否则报错 —— 缺上下文比"跳过"诚实）；
          · 只重置**运行态**的 `scenes_done`；进度文件（beat/已兑现）是历史，
            **不倒退**：这一场重跑不会重复记 beat，已兑现的伏笔仍是已兑现；
          · 重跑时：欠账只在**这本剧本的第一场**注入，从中间某场重跑不会重复塞；
          · 返回这次新跑出来的日志（不含之前的）。
        """
        ids = [s.id for s in self.adv.scenes]
        if scene_id not in ids:
            raise AdventureError([f"剧本 {self.adv.title!r} 里没有场景 "
                                  f"{scene_id!r}（现有：{ids}）"])
        idx = ids.index(scene_id)
        for s in self.adv.scenes[:idx]:
            if s.id not in self.scenes_done:
                raise AdventureError([
                    f"前面的场景 {s.id!r} 还没跑完，不能从 {scene_id!r} 开始 —— "
                    f"缺了那一段的上下文，重跑会前后不接"])
        self.scenes_done = [x for x in ids[:idx] if x in self.scenes_done]
        self.loop.scenes_done = list(self.scenes_done)
        start = len(self.logs)
        for scene in self.adv.scenes[idx:]:
            self.run_scene(scene, combat_rounds=combat_rounds)
        self._close_combat()
        return self.logs[start:]


def _short(text: str, n: int = 60) -> str:
    """压成一句：去掉换行、截到 n 字（截断处不留悬空的标点）。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n].rstrip("，。、；：") + "…"



