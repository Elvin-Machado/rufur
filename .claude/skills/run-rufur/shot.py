"""Screenshot RUFUR UI views with headless Chromium: shot.py BASE_URL OUT_DIR VIEW [VIEW ...]."""

import glob
import os
import sys

from playwright.sync_api import sync_playwright

LABELS = {"brain": "Brain", "timeline": "Timeline", "review": "Review", "agents": "Agents", "usage": "Usage"}


def browser_path() -> str | None:
    # The pip playwright often wants a newer Chromium than the one cached; any cached headless shell works.
    hits = sorted(glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    hits += sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    return hits[-1] if hits else None


base, out, views = sys.argv[1], sys.argv[2], sys.argv[3:]
os.makedirs(out, exist_ok=True)
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception:
        browser = p.chromium.launch(executable_path=browser_path())
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors: list[str] = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(base)
    page.get_by_text("live", exact=True).wait_for(timeout=10000)  # SSE connected
    for v in views:
        page.get_by_role("navigation", name="Views").get_by_role("button", name=LABELS[v]).click()
        page.wait_for_timeout(2500)  # force graph settles / lists load
        path = os.path.join(out, f"{v}.png")
        page.screenshot(path=path)
        print(path)
    print("console errors:", errors or "none")
    browser.close()
