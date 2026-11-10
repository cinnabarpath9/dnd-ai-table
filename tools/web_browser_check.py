import json
import os
import sys

from playwright.sync_api import sync_playwright

#: 浏览器路径可用环境变量覆盖（外审 A6：写死路径换台机器就崩）
EDGE = os.environ.get("DND_BROWSER") or (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def run(url: str, shot_dir: str, turns: int, do_save: bool) -> int:
    os.makedirs(shot_dir, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:160]))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        if page.query_selector("#session0.open"):
            page.screenshot(path=os.path.join(shot_dir, "01-open.png"),
                            full_page=True)
            page.check("#s0-ack")
            page.click("#s0Start")
            page.wait_for_selector("#session0", state="hidden", timeout=30_000)
            print("开桌：已勾选并开始")
        page.wait_for_selector("#slots button.slot", timeout=180_000)
        page.screenshot(path=os.path.join(shot_dir, "02-turn.png"),
                        full_page=True)

        for turn in range(turns):
            cards = page.query_selector_all("#slots button.slot")
            card = next((c for c in cards
                         if "结束回合" not in (c.inner_text() or "")), cards[0])
            oid = card.get_attribute("data-id")
            card.click()
            tgt = page.query_selector_all("#targetbox button")
            target = ""
            if tgt:
                target = tgt[0].inner_text().strip().split()[0]
                tgt[0].click()
            page.fill("#say", f"（浏览器验收第 {turn + 1} 轮）")
            before = page.inner_text("#turnline")
            with page.expect_response(
                    lambda r: "/api/answer" in r.url, timeout=120_000) as info:
                page.click("#submit")
            print(f"第 {turn + 1} 轮：{oid}{(' ' + target) if target else ''}"
                  f" → {info.value.status}")
            if info.value.status != 200:
                body = {}
                try:
                    body = info.value.json()
                except Exception:                       # noqa: BLE001
                    pass
                print("  拒绝：", json.dumps(body, ensure_ascii=False)[:160])
                return 1
            page.wait_for_function(
                "(prev) => { const t=document.querySelector('#turnline').textContent;"
                " return t !== prev && t.includes('轮到'); }",
                arg=before, timeout=300_000)

        page.screenshot(path=os.path.join(shot_dir, "03-played.png"),
                        full_page=True)
        print("状态：", page.inner_text("#status"))
        print("回合：", page.inner_text("#turnline"))
        print("日志字数：", len(page.inner_text("#log")))
        if do_save:
            page.click('button[data-panel="settings"]')
            page.wait_for_selector("#saveGame", timeout=10_000)
            with page.expect_response(
                    lambda r: "/api/act" in r.url, timeout=60_000) as info2:
                page.click("#saveGame")
            print("存档：", info2.value.status,
                  json.dumps(info2.value.json(), ensure_ascii=False)[:120])
            page.wait_for_timeout(1500)
            page.screenshot(path=os.path.join(shot_dir, "04-saved.png"),
                            full_page=True)
        browser.close()
    if errs:
        print("页面错误：", sorted(set(errs))[:4])
        print("❌ 页面有 JS 错误（外审 A5：退出码必须非 0，不能绿灯掩盖）")
        return 1
    return 0


if __name__ == "__main__":
    url, shot_dir = sys.argv[1], sys.argv[2]
    turns = 2
    if "--turns" in sys.argv:
        turns = int(sys.argv[sys.argv.index("--turns") + 1])
    sys.exit(run(url, shot_dir, turns, "--save" in sys.argv))