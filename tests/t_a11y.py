# coding=utf-8
"""真机/读屏验收的**离线部分**：页面语义、键盘焦点、动效偏好、验收单完整性。

说清楚边界：

    ✅ 能自动查：lang/viewport、aria-live、对话框语义、按钮可读名、
       表单控件标签、焦点圈/焦点陷阱、reduced-motion、验收单要素；
    ❌ 不能代替：真人用 Narrator/NVDA 听一遍、真手机点一遍 —— 那要人做，
       步骤见 `docs/验收-叙事与真机.md`，做完前一律标"未验证"。
"""

from __future__ import annotations

import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.webui import PAGE_HTML  # noqa: E402

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


def text_of(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


buttons = re.findall(r"<button\b([^>]*)>(.*?)</button>", PAGE_HTML, re.S)
overlays = re.findall(r'<div class="overlay\b[^>]*>', PAGE_HTML)

section("A. 文档与视口")
ok('<html lang="zh-CN">' in PAGE_HTML, "A1 页面声明 lang=zh-CN")
ok('name="viewport"' in PAGE_HTML and "width=device-width" in PAGE_HTML,
   "A2 有响应式 viewport")

section("B. 读屏语义")
ok('id="log" role="log" aria-live="polite"' in PAGE_HTML,
   "B1 日志是 role=log 的礼貌播报区")
ok('id="status" role="status" aria-live="polite"' in PAGE_HTML,
   "B2 状态行会播报")
ok('id="error" role="alert" aria-live="assertive"' in PAGE_HTML,
   "B3 错误是 role=alert（立即播报）")
ok(len(overlays) >= 14 and all('role="dialog"' in o for o in overlays),
   "B4 每个弹层都是 dialog（≥14，新增面板也算）", len(overlays))
ok(all('aria-modal="true"' in o and 'aria-label="' in o for o in overlays),
   "B5 每个弹层有模态语义 + 可读名")
for attrs, body in buttons:
    label = text_of(body)
    named = bool(label) or "aria-label=" in attrs
    if not named:
        ok(False, "B6 按钮都有可读名", attrs[:60])
        break
else:
    ok(True, "B6 所有静态按钮都有文字或 aria-label")

section("C. 表单标签")
ok('id="say"' in PAGE_HTML and 'aria-label="台词（可选）"' in PAGE_HTML,
   "C1 台词框有 aria-label（placeholder 不算标签）")
ok('id="importFile"' in PAGE_HTML and 'aria-label="选择存档文件"' in PAGE_HTML,
   "C2 文件选择有 aria-label")
for lab in ("角色名", "职业", "种族", "等级", "属性生成方式", "随机种子", "选择目标"):
    if f"aria-label','{lab}'" not in PAGE_HTML:
        ok(False, f"C3 动态控件有标签：{lab}")
        break
else:
    ok(True, "C3 动态创建的表单控件都有 aria-label")

section("D. 键盘与焦点")
ok(":focus-visible" in PAGE_HTML, "D1 有 :focus-visible 焦点圈")
ok("function closeAny()" in PAGE_HTML and "lastFocus" in PAGE_HTML,
   "D2 关闭弹层会把焦点还回去")
ok("e.key === 'Tab'" in PAGE_HTML and "first.focus()" in PAGE_HTML,
   "D3 弹层内 Tab 会被圈住")
ok('aria-expanded' in PAGE_HTML and "setAttribute('aria-expanded','true')" in PAGE_HTML,
   "D4 工具栏会报告展开状态")

section("E. 动效偏好")
ok("prefers-reduced-motion: reduce" in PAGE_HTML,
   "E1 尊重系统的减少动效偏好")

section("F. 验收单（真人部分未执行，先查要素）")
doc = os.path.join(ROOT, "docs", "验收-叙事与真机.md")
ok(os.path.exists(doc), "F1 验收单存在")
if os.path.exists(doc):
    d = open(doc, encoding="utf-8").read()
    for need, label in (("Narrator", "F2 写了 Narrator 步骤"),
                        ("NVDA", "F3 写了 NVDA 步骤"),
                        ("375", "F4 写了窄屏尺寸"),
                        ("200%", "F5 写了 200% 缩放"),
                        ("未验证", "F6 明确标了未验证口径"),
                        ("签字", "F7 留了验收签字位")):
        ok(need in d, label)

# --------------------------------------------------------------------------- #
print("\n" + "=" * 70)
if FAILS:
    print(f"共 {COUNT} 项：通过 {COUNT - len(FAILS)}，失败 {len(FAILS)}")
    for f in FAILS:
        print(f"  ✗ {f}")
    raise SystemExit(1)
print(f"共 {COUNT} 项：全部通过")
