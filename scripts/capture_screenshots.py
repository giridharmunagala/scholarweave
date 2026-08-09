"""Capture documentation screenshots of the running ScholarWeave UI.

Usage:
    .venv/bin/uvicorn backend.app:app --port 8123 &
    .venv/bin/python scripts/capture_screenshots.py --base-url http://127.0.0.1:8123
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def fetch_json(base_url: str, path: str):
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8123")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    runs = fetch_json(base_url, "/api/runs") or []
    completed = next((run for run in runs if run.get("status") == "completed"), None)
    run_id = completed["id"] if completed else (runs[0]["id"] if runs else None)

    def hide_chat_list(page) -> None:
        button = page.get_by_role("button", name="Hide chat list")
        if button.count():
            button.first.click()
            page.wait_for_timeout(600)

    def open_workspace_file(page) -> None:
        file_button = page.get_by_text("summary.md", exact=True)
        if file_button.count():
            file_button.first.click()
            page.wait_for_timeout(1500)

    def open_providers_tab(page) -> None:
        tab = page.get_by_text("Providers", exact=True)
        if tab.count():
            tab.first.click()
            page.wait_for_timeout(1200)
        # Documentation screenshots should not publish a personal cloud endpoint.
        page.evaluate(
            """
            () => {
              document.querySelectorAll('code, span, div').forEach((node) => {
                if (node.children.length === 0 && node.textContent.includes('.openai.azure.com')) {
                  node.textContent = 'https://<resource>.openai.azure.com';
                }
              });
            }
            """
        )
        page.wait_for_timeout(300)

    targets: list[tuple[str, str, object]] = [
        ("/", "research-chat.png", hide_chat_list),
        ("/papers", "papers.png", None),
        ("/tools", "tools.png", None),
        ("/workspace", "workspace.png", open_workspace_file),
        ("/runs", "runs.png", None),
        ("/settings", "settings.png", None),
        ("/settings", "settings-providers.png", open_providers_tab),
    ]
    if run_id:
        targets.append((f"/runs/{run_id}", "run-inspector.png", None))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        for route, filename, prepare in targets:
            page.goto(f"{base_url}{route}", wait_until="networkidle")
            page.wait_for_timeout(2500)
            if prepare is not None:
                prepare(page)
            page.screenshot(path=str(OUTPUT_DIR / filename))
            print(f"captured {route} -> docs/screenshots/{filename}")
        browser.close()


if __name__ == "__main__":
    main()
