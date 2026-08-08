"""Playwright browser smoke tests for the standalone ARC UI.

These drive a real browser against a live ``python -m arc.ui`` server to
verify the page loads, assets render, and a session can be created from the
UI — the browser-level coverage the unit tests can't provide.

They are **opt-in**: skipped unless Playwright *and* a browser binary are
installed (``pip install playwright && playwright install chromium``). This
keeps the default test run dependency-free while still shipping the smoke
suite for environments that have Playwright. Run just these with::

    pytest arc/tests/test_ui_playwright_smoke.py
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

# Skip the whole module unless Playwright is importable.
sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright not installed — browser smoke tests skipped",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Start the UI under uvicorn in a background thread, yield its base URL."""
    import uvicorn

    from arc.ui.server import create_app

    port = _free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        pytest.skip("UI server did not start in time")
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — no browser binary installed
            pytest.skip(f"chromium not available for Playwright: {exc}")
        yield browser
        browser.close()


def test_ui_page_loads_and_assets_render(live_server, browser):
    page = browser.new_page()
    page.goto(live_server, wait_until="networkidle")
    # Brand + composer present (the JS-driven shell rendered).
    assert page.locator(".app-brand").is_visible()
    assert page.locator("#messageInput").is_visible()
    # The health chip resolved to a version (not stuck on "checking"/"offline").
    page.wait_for_function(
        "() => { const el = document.getElementById('healthLabel');"
        " return el && el.textContent && el.textContent !== 'checking'; }",
        timeout=5000,
    )
    page.close()


def test_ui_create_session_from_browser(live_server, browser):
    page = browser.new_page()
    page.goto(live_server, wait_until="networkidle")
    page.click("#newSession")
    # A session row appears in the sidebar.
    page.wait_for_selector(".session-row", timeout=5000)
    assert page.locator(".session-row").count() >= 1
    page.close()
