"""Capture README screenshots without saving research or provider changes.

Install the optional capture tools with:
    python -m pip install playwright
    python -m playwright install chromium

Run against an existing server with:
    python -m scripts.capture_screenshots --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Page, Route, expect, sync_playwright

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    def open_paper_text(page: Page) -> None:
        page.get_by_role("button", name="Retrieved text", exact=True).click()
        expect(page.get_by_role("region", name="Retrieved paper text")).to_be_visible()

    def open_workspace_file(page: Page) -> None:
        response = page.request.get(f"{base_url}/api/workspace/summaries")
        assert response.ok, "Cannot load saved summaries for the screenshot."
        summaries = response.json()
        if not summaries:
            raise RuntimeError("A saved paper summary is required for the Notes screenshot.")
        summary = max(summaries, key=lambda item: item["size_bytes"])
        page.get_by_title(summary["path"], exact=True).click()
        expect(page.locator(".workspace-preview")).to_be_visible()
        page.locator("details.file-tree-folder").evaluate_all(
            "folders => folders.forEach(folder => { folder.open = !!folder.querySelector('.file-row.active'); })"
        )
        page.locator(".file-list").evaluate("list => { list.scrollTop = 0; }")
        page.evaluate("window.scrollTo(0, 0)")

    def open_provider_form(page: Page) -> None:
        page.get_by_text("Providers", exact=True).click()
        page.get_by_role("button", name="Add provider", exact=True).click()
        page.get_by_label("Name", exact=True).fill("Hosted research model")
        page.locator(".provider-form select").select_option("openai_compatible")
        page.get_by_label("Base URL", exact=True).fill("https://api.openai.com/v1")
        expect(page.get_by_label("API key (optional)", exact=True)).to_be_empty()

    targets: list[tuple[str, str, Callable[[Page], None] | None]] = [
        ("/library", "papers.png", open_paper_text),
        ("/library/notes", "workspace.png", open_workspace_file),
        ("/settings", "settings-providers.png", open_provider_form),
    ]
    errors: list[str] = []
    saved_catalogs = {}

    def read_only_api(route: Route) -> None:
        if route.request.method == "GET" and route.request.url in saved_catalogs:
            route.fulfill(json={"models": saved_catalogs[route.request.url], "discovery_error": None})
        elif route.request.method not in {"GET", "HEAD", "OPTIONS"}:
            errors.append(f"Blocked an unexpected {route.request.method} API request.")
            route.abort()
        else:
            route.continue_()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, device_scale_factor=1,
            color_scheme="light", reduced_motion="reduce",
        )
        providers_response = context.request.get(f"{base_url}/api/providers")
        assert providers_response.ok, "Cannot load saved provider catalogs."
        saved_catalogs = {
            f"{base_url}/api/providers/{quote(provider['id'], safe='')}/models": provider["models"]
            for provider in providers_response.json()
        }
        context.route(f"{base_url}/api/**", read_only_api)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        for route, filename, prepare in targets:
            response = page.goto(f"{base_url}{route}", wait_until="networkidle")
            assert response is not None and response.ok, f"Failed to load {route}."
            expect(page.locator("main")).to_be_visible()
            if prepare is not None:
                prepare(page)
            page.evaluate("document.fonts.ready")
            page.wait_for_function(
                """
                () => Array.from(document.images).every(image => image.complete && image.naturalWidth > 0)
                """
            )
            clip = None
            if filename == "settings-providers.png":
                form = page.locator(".provider-form").bounding_box()
                assert form is not None
                clip = {"x": 0, "y": 0, "width": 1440, "height": form["y"] + form["height"] + 8}
            assert not errors, "\n".join(errors)
            page.screenshot(path=str(OUTPUT_DIR / filename), animations="disabled", clip=clip)
            print(f"captured {route} -> docs/screenshots/{filename}")
        browser.close()
    print("Captured all README screenshots without API writes or live model discovery.")


if __name__ == "__main__":
    main()
