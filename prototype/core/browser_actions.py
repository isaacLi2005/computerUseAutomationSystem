"""Shared low-level browser actions used by both discovery.py and replay.py."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def click_and_wait(page, x, y):
    """Clicks (x, y) and waits out any navigation it triggers -- a plain
    click only waits for the event to dispatch. Most clicks don't navigate
    at all, so timing out here is normal, not an error."""
    try:
        with page.expect_navigation(timeout=3000):
            page.mouse.click(x, y)
    except PlaywrightTimeoutError:
        pass
