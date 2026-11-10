# coding=utf-8
"""浏览器验收：观战（全 AI）/ 托管 / 接管 / BGM 开关。

用法：先起 `python -m engine.demo_web --port 8801`，再跑本脚本。
截图落在 `真模型记录记录` 还是 `tools/真机截图/`？本轮放 `tools/真机截图/模式/`。
"""
import os
import sys

from playwright.sync_api import sync_playwright

EDGE = os.environ.get("DND_BROWSER") or (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
URL = os.environ.get("DND_URL") or "http://127.0.0.1:8801/"
SHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tools", "真模型记录")


def main() -> int:
    os.makedirs(SHOT, exist_ok=True)
    fails = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:200]))
        page.on("dialog", lambda d: d.accept())

        def confirm_lobby(timeout=5000):
            """--session-zero 下每次“新开”都会先弹大厅；非大厅模式直接 no-op。"""
            try:
                page.wait_for_selector("#session0.open", timeout=timeout)
            except Exception:                         # noqa: BLE001
                return
            if page.query_selector("#session0.open"):
                page.check("#s0-ack")
                page.click("#s0Start")
                try:
                    page.wait_for_selector("#session0", state="hidden", timeout=15_000)
                except Exception:                     # noqa: BLE001
                    fails.append("大厅点了确认但没关（15 秒内）")

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        confirm_lobby()

        # ---- BGM ----
        page.click("#bgmBtn")
        page.wait_for_timeout(300)
        on = page.get_attribute("#bgmBtn", "aria-pressed")
        if on != "true":
            fails.append("BGM 点了没开")
        page.screenshot(path=os.path.join(SHOT, "2026-11-09-BGM开关.png"), full_page=True)
        page.click("#bgmBtn")      # 关掉，避免后续截图里一直是开

        # ---- 观战：先证明“选中的模式不会被 0.8s 轮询改回去” ----
        page.select_option("#gameMode", "spectate")
        page.wait_for_timeout(1600)                   # 跨两次 poll
        if page.input_value("#gameMode") != "spectate":
            fails.append("选了观战，1.6 秒后被轮询改回了别的模式")
        page.click("#modeStart")
        confirm_lobby()
        try:
            page.wait_for_function(
                "document.getElementById('modeText').textContent.includes('观战')",
                timeout=12_000)
        except Exception:                             # noqa: BLE001
            fails.append("观战横幅没出现（12 秒内）")
        banner = page.query_selector("#modebanner")
        if not banner or banner.is_hidden():
            fails.append("观战横幅没显示")
        if page.is_visible("#composer"):
            fails.append("观战还显示输入框")
        # 等这一局真的跑完（此后 poll 必须继续活着）
        try:
            page.wait_for_function(
                "document.getElementById('status').textContent.includes('结束')",
                timeout=35_000)
        except Exception:                             # noqa: BLE001
            fails.append("观战 35 秒内没跑完（没法验证结束后新开）")
        page.screenshot(path=os.path.join(SHOT, "2026-11-09-观战全AI.png"), full_page=True)
        log0 = page.inner_text("#log")

        # ---- 托管 + 接管 ----
        page.select_option("#gameMode", "hosted")
        page.click("#modeStart")
        confirm_lobby()
        try:
            page.wait_for_function(
                "document.getElementById('modeText').textContent.includes('托管')",
                timeout=12_000)
        except Exception:                             # noqa: BLE001
            fails.append("托管横幅没出现（12 秒内）")
        text = page.inner_text("#modeText")
        if "托管" not in text:
            fails.append(f"托管横幅不对：{text!r}")
        if page.is_visible("#composer"):
            fails.append("托管时还显示输入框")
        page.wait_for_timeout(2500)   # 让新一局的日志/轮次至少推进两轮
        page.click("#takeOver")
        try:
            page.wait_for_selector("#composer", state="visible", timeout=12_000)
        except Exception:                             # noqa: BLE001
            fails.append("接管后输入框没回来（12 秒内）")
        if page.input_value("#gameMode") != "normal":
            fails.append("接管后模式选择没回到「真人」")
        log1 = page.inner_text("#log")
        if len(log1) <= len(log0):
            fails.append("观战结束后新开托管，页面日志没有继续更新（poll 停了）")
        page.screenshot(path=os.path.join(SHOT, "2026-11-09-托管接管.png"),
                        full_page=True)

        # ---- BGM 持久化：上次开着，刷新后第一次点按钮应该“开”，不是反关 ----
        page.evaluate(
            "localStorage.setItem('dnd_bgm', JSON.stringify({on:true, vol:0.35}));"
            "location.reload()")
        page.wait_for_timeout(1500)
        confirm_lobby()
        page.click("#bgmBtn")
        page.wait_for_timeout(500)
        if page.get_attribute("#bgmBtn", "aria-pressed") != "true":
            fails.append("BGM 上次开着时，刷新后第一次点按钮没开（被 kick 先开再关）")

        if errs:
            fails.append("页面 JS 错误：" + " | ".join(errs))
        browser.close()

    if fails:
        print("❌ " + "\n❌ ".join(fails))
        return 1
    print("✅ 观战/托管/接管/BGM 浏览器验收通过")
    print("截图：", SHOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())