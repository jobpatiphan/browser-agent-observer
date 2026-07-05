#!/usr/bin/env python3
"""Playwright + browser-agent-observer.

Launches a Chromium wired to the observer's proxy + CDP, and narrates page
events onto the dashboard timeline. Nothing observer-specific is required — any
Playwright script works as long as the browser exposes CDP on 9222 and routes
through the proxy on 8083; the narration below is just the nice-to-have layer.

    pip install playwright && playwright install chromium
    ./run.sh up                         # in the observer checkout
    python integrations/playwright/observe_playwright.py https://example.com
"""
import sys
from pathlib import Path

# Make the zero-dependency observer client importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "clients"))
from observer import obs  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

PROXY = "http://127.0.0.1:8083"


def main(url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            proxy={"server": PROXY},
            args=["--remote-debugging-port=9222", "--ignore-certificate-errors"],
        )
        page = browser.new_page()
        # Mirror high-signal events onto the Activity timeline.
        page.on("framenavigated",
                lambda f: f == page.main_frame and obs.action("navigate", target=f.url))
        page.on("console", lambda m: obs.narrate(f"console: {m.text}"[:200]))
        obs.narrate(f"Playwright driving {url}", level="info")
        page.goto(url)
        page.wait_for_timeout(4000)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "https://example.com")
