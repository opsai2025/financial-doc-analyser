"""Full-page screenshots of the scratch server (port 8511) in Chrome, for visual checks.

Usage: python scripts/ui_screenshots.py <out folder> <username> <password> <page> [<page> ...]
Pages are URL paths such as analysis, history, dashboard, settings, profile.
Needs Playwright (installed in `fin-doc`) and Google Chrome or Edge (uses the installed browser).
Streamlit scrolls inside its own container, so the window is made tall instead of scrolling.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8511"


def main(out: Path, username: str, password: str, pages: list[str], height: int = 2600) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": height})
        page.goto(BASE)
        page.get_by_label("نام کاربری").fill(username)
        page.get_by_label("رمز عبور").fill(password)
        page.get_by_role("button", name="ورود").click()
        page.wait_for_timeout(4000)
        for name in pages:
            page.goto(f"{BASE}/{name}")
            page.wait_for_timeout(4000)
            target = out / f"{username}_{name}.png"
            page.screenshot(path=str(target), full_page=True)
            print(target)
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 5:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4:])
