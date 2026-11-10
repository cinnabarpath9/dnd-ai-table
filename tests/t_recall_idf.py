# coding=utf-8
"""词法 IDF + 别名表（2026-11-06，P5-G3 后的召回补强）。

守的口径：

    · 别名只做**确定等价**（短刀=匕首、药水=治疗药水），不猜近义；
    · 别名同时作用于结构化词元（人/地/标签）和正文里的跨说法；
    · IDF 只加权**文本证据**，结构化证据（线索 3 / 人物 2 / 地点 1）分值不变；
    · 文本证据总分封顶，不能单独把一条抬进 REAL；
    · 不给 idf 时行为与旧版一致（每命中 1 分）。
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.memory import (  # noqa: E402
    MemoryStore, Query, build_idf, canonical, recall, score_entry,
)
from engine.memory.recall import ALIASES, TEXT_CAP, _canon_text  # noqa: E402
from engine.state.visibility import DM  # noqa: E402

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


def store_with(*rows) -> MemoryStore:
    st = MemoryStore(label="idf测试")
    for summary, kw in rows:
        st.record(summary, **kw)
    return st


# --------------------------------------------------------------------------- #
section("A. 别名：只折确定等价")

ok(canonical("短刀") == "匕首", "A1 短刀 → 匕首")
ok(canonical("匕首") == "匕首", "A2 正名不变")
ok(canonical("clue:p2") == "clue:p2", "A3 标签 id 不参与别名折叠")
ok(canonical("阿猫", {"阿猫": "艾尔玛"}) == "艾尔玛", "A4 自定义别名表生效")
ok(canonical("长剑") == "长剑", "A5 没登记的写法原样返回")
ok(_canon_text("治疗药水") == "治疗药水",
   "A5b ⭐ 正名里含别名的子串时不会被二次替换（治疗治疗药水）",
   _canon_text("治疗药水"))
ok(_canon_text("喝下药水") == "喝下治疗药水",
   "A5c 别名照常折成正名", _canon_text("喝下药水"))

st = store_with(("他掏出一把短刀。", {}))
s, why = score_entry(st._entries[0],
                     Query(text="你看到一把匕首", viewer=DM))
ok(s == 0.5 and "相关词" in why,
   "A6 ⭐ 情境说匕首、摘要写短刀 → 命中弱证据", (s, why))

st = store_with(("他掏出一把长剑。", {}))
s2, _ = score_entry(st._entries[0], Query(text="你看到一把匕首", viewer=DM))
ok(s2 == 0.0, "A7 不同物品不会被别名误配")

st = store_with(("门轴上刻着名字。", {"tags": ("短刀",)}))
s3, why3 = score_entry(st._entries[0], Query(tags=("匕首",), viewer=DM))
ok(s3 == 3.0 and "匕首" in why3,
   "A8 ⭐ 标签也走别名：短刀标签能被「匕首」查询命中", (s3, why3))


# --------------------------------------------------------------------------- #
section("B. IDF：稀有词权重更高")

st = store_with(
    ("哨塔的门开着。", {"tags": ("哨塔",)}),
    ("石壁上刻着龙纹。", {"tags": ("龙纹",)}),
    ("哨塔二层很暗。", {"tags": ("哨塔",)}),
)
idf = build_idf(st._entries)
ok(idf["龙纹"] > idf["哨塔"],
   "B1 ⭐ 只出现一次的「龙纹」IDF > 出现两次的「哨塔」",
   (round(idf["龙纹"], 3), round(idf["哨塔"], 3)))
ok(all(1.0 <= v <= 3.0 for v in idf.values()), "B2 IDF 落在合理区间", idf)

q = Query(text="龙纹 哨塔", viewer=DM)
r_idf = recall(st, q, count=3, use_idf=True)
r_flat = recall(st, q, count=3, use_idf=False)
ok(r_idf.ids()[0] == "AM0002",
   "B3 ⭐ 开 IDF：稀有词那条排第一（AM0002 龙纹）", r_idf.ids())
ok(r_flat.ids()[0] == "AM0001",
   "B4 关 IDF：同分按登记顺序（AM0001 哨塔）—— 旧行为可对拍", r_flat.ids())
ok(r_idf.items[0].score > r_idf.items[1].score,
   "B5 分数确实拉开了", [(r.entry.id, round(r.score, 3)) for r in r_idf.items])


# --------------------------------------------------------------------------- #
section("C. 结构化分数不变、文本证据封顶")

entry = st._entries[0]                                  # tags=哨塔
s, why = score_entry(entry, Query(tags=("哨塔",), viewer=DM), idf=idf)
ok(s == 3.0, "C1 共享线索仍是 3 分（IDF 不碰结构化证据）", (s, why))

st2 = store_with(("哨塔哨塔哨塔哨塔。", {"tags": ("哨塔",), "people": ("甲",),
                                      "places": ("哨塔",)}))
long_text = "哨塔 甲 " * 30
s2, _ = score_entry(st2._entries[0],
                    Query(text=long_text, tags=("哨塔",), people=("甲",),
                          places=("哨塔",), viewer=DM), idf=idf)
ok(s2 == 3.0 + 2.0 + 1.0 + TEXT_CAP,
   "C2 ⭐ 文本证据再多也封顶 TEXT_CAP（不会靠描写抬进 REAL 以上）",
   (s2, 6.0 + TEXT_CAP))


# --------------------------------------------------------------------------- #
section("D. 召回整链：别名 + IDF + 可见性")

st3 = store_with(
    ("阿甲在哨塔捡到一把短刀。", {"tags": ("短刀",), "people": ("阿甲",),
                                 "visible_to": "public"}),
    ("秘密：匕首其实有毒。", {"tags": ("匕首",), "people": ("阿甲",),
                              "visible_to": "dm"}),
)
r = recall(st3, Query(tags=("匕首",), viewer="阿甲"), count=5)
ok(r.ids() == ["AM0001"], "D1 ⭐ 阿甲只召回公开那条（秘密仍被过滤）", r.ids())
r_dm = recall(st3, Query(tags=("匕首",), viewer=DM), count=5)
ok(set(r_dm.ids()) == {"AM0001", "AM0002"},
   "D2 ⭐ 别名让两条短刀/匕首记录都被 DM 召回", r_dm.ids())
ok(all("短刀" in r.entry.tags or "匕首" in r.entry.tags for r in r_dm.items),
   "D3 记录本身没被改写（只在打分时折正名）")


# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")