"""Capture white-theme README screenshots without saving research or provider changes.

Install the optional capture tools with:
    python -m pip install playwright
    python -m playwright install chromium

Run against an existing server with:
    python -m scripts.capture_screenshots --base-url http://127.0.0.1:1102

For a frontend development server, pass its isolated backend with --api-url.
Use demo data or review all captured content before publishing.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlsplit

from playwright.sync_api import Page, Route, expect, sync_playwright

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:1102")
    parser.add_argument("--api-url", help="Backend origin when capturing a separate frontend dev server.")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    api_url = (args.api_url or args.base_url).rstrip("/")

    def open_paper_text(page: Page) -> None:
        page.get_by_role("button", name="Text", exact=True).click()
        expect(page.get_by_role("region", name="Retrieved paper text")).to_be_visible()

    def open_paper_question(page: Page) -> None:
        open_paper_text(page)
        page.get_by_role("button", name="Ask a question", exact=True).click()
        expect(page.get_by_role("complementary", name="Assistant", exact=True)).to_be_visible()
        page.get_by_role("textbox", name="Message", exact=True).fill(
            "Which assumptions should I check against the original paper before using these findings?"
        )

    def open_workspace_file(page: Page) -> None:
        expect(page.locator(".workspace-preview")).to_be_visible()
        page.evaluate("window.scrollTo(0, 0)")

    def prepare_research_question(page: Page) -> None:
        page.get_by_role("textbox", name="Research question", exact=True).fill(
            "How should a local research assistant compare retrieved evidence with long-context reading?"
        )

    def open_provider_form(page: Page) -> None:
        page.get_by_role("button", name="Providers", exact=True).click()
        page.get_by_role("button", name="Add provider", exact=True).click()
        page.get_by_label("Name", exact=True).fill("Hosted research model")
        page.locator(".provider-form").get_by_label("Kind").select_option("openai_compatible")
        page.get_by_label("Base URL", exact=True).fill("https://api.openai.com/v1")
        expect(page.get_by_label("API key (optional)", exact=True)).to_be_empty()

    errors: list[str] = []
    saved_catalogs: dict[str, list[dict]] = {}

    def read_only_api(route: Route) -> None:
        url = urlsplit(route.request.url)
        if route.request.method == "GET" and url.path in saved_catalogs:
            route.fulfill(json={"models": saved_catalogs[url.path], "discovery_error": None})
        elif route.request.method not in {"GET", "HEAD", "OPTIONS"}:
            errors.append(f"Blocked an unexpected {route.request.method} API request.")
            route.abort()
        elif api_url != base_url:
            response = route.fetch(url=f"{api_url}{url.path}" + (f"?{url.query}" if url.query else ""))
            route.fulfill(response=response)
        else:
            route.continue_()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1680, "height": 1050}, device_scale_factor=1,
            color_scheme="light", reduced_motion="reduce",
        )
        context.add_init_script("""
            localStorage.setItem('scholarweave-theme', 'light');
            localStorage.setItem('scholarweave-theme-version', '3');
            localStorage.setItem('scholarweave.assistant.width', '440');
        """)
        providers_response = context.request.get(f"{api_url}/api/providers")
        assert providers_response.ok, "Cannot load saved provider catalogs."
        saved_catalogs = {
            f"/api/providers/{quote(provider['id'], safe='')}/models": provider["models"]
            for provider in providers_response.json()
        }
        documents_response = context.request.get(f"{api_url}/api/documents")
        assert documents_response.ok, "Cannot load papers for the screenshots."
        papers = [paper for paper in documents_response.json() if paper["status"] == "ready"]
        if not papers:
            raise RuntimeError("A prepared paper is required for the reader screenshots.")
        paper_path = f"/library?paper={quote(papers[0]['id'], safe='')}"
        notes_response = context.request.get(f"{api_url}/api/workspace/notes")
        assert notes_response.ok, "Cannot load notes for the screenshot."
        notes = notes_response.json()
        if not notes:
            raise RuntimeError("A saved note is required for the Notes screenshot.")
        note = max(
            [item for item in notes if item["kind"] == "note"] or notes,
            key=lambda item: item["size_bytes"],
        )
        targets: list[tuple[str, str, Callable[[Page], None] | None]] = [
            ("/library", "papers.png", None),
            (paper_path, "paper-reader.png", open_paper_text),
            (paper_path, "research-chat.png", open_paper_question),
            (f"/library/notes?path={quote(note['path'], safe='')}", "workspace.png", open_workspace_file),
            ("/deep-work", "research-workspace.png", prepare_research_question),
        ]
        tracks_response = context.request.get(f"{api_url}/api/tracks")
        assert tracks_response.ok, "Cannot load research tracks."
        tracks = tracks_response.json()
        if tracks:
            targets.append((f"/tracks?id={quote(tracks[0]['id'], safe='')}", "tracks.png", None))
        else:
            print("No research track available; skipping tracks.png.")
        targets.extend([
            ("/runs", "runs.png", None),
            ("/settings", "settings.png", None),
            ("/settings", "settings-providers.png", open_provider_form),
        ])
        context.route(f"{base_url}/api/**", read_only_api)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        for route, filename, prepare in targets:
            response = page.goto(f"{base_url}{route}", wait_until="networkidle")
            assert response is not None and response.ok, f"Failed to load {route}."
            expect(page.locator(".app-main")).to_be_visible()
            expect(page.locator("html")).to_have_attribute("data-theme", "light")
            if prepare is not None:
                prepare(page)
            expect(page.get_by_text("Loading…", exact=True)).to_have_count(0)
            page.evaluate("document.fonts.ready")
            page.wait_for_function(
                """
                () => Array.from(document.images).every(image => image.complete && image.naturalWidth > 0)
                """
            )
            clip = None
            if filename == "research-workspace.png":
                steps = page.locator(".research-steps").bounding_box()
                assert steps is not None
                clip = {"x": 0, "y": 0, "width": 1680, "height": steps["y"] + steps["height"] + 24}
            elif filename == "settings-providers.png":
                form = page.locator(".provider-form").bounding_box()
                assert form is not None
                clip = {"x": 0, "y": 0, "width": 1680, "height": form["y"] + form["height"] + 8}
            assert not errors, "\n".join(errors)
            page.screenshot(path=str(OUTPUT_DIR / filename), animations="disabled", clip=clip)
            print(f"captured {route} -> docs/screenshots/{filename}")
        browser.close()
    print("Captured all README screenshots in white without API writes or live model discovery.")


if __name__ == "__main__":
    main()
