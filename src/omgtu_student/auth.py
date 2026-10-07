"""One manual login in an isolated, visible browser; no credentials are read."""

from __future__ import annotations

import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .state import SessionStore, StateError, university_domain

LOGIN_URL = "https://omgtu.ru/ecab/"
COURSES_URL = "https://up.omgtu.ru/index.php?r=remote/read"
_COURSE_LINK_SELECTOR = '#List a[href*="remote/read/taskList"]'


def _portal_landing(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "up.omgtu.ru"
        and parse_qs(parsed.query).get("r", [""])[0] in {"", "site/index"}
    )


def _course_list(page: Any) -> bool:
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname != "up.omgtu.ru":
        return False
    if parse_qs(parsed.query).get("r", [""])[0] != "remote/read":
        return False
    return bool(page.locator("#List").count() and page.locator(_COURSE_LINK_SELECTOR).count())


def _restrict_navigation(route: Any) -> None:
    """Keep top-level login navigation on the university's HTTPS domains."""
    request = route.request
    parsed = urlsplit(request.url)
    if (
        request.is_navigation_request()
        and request.frame.parent_frame is None
        and (parsed.scheme != "https" or not university_domain(parsed.hostname))
    ):
        route.abort()
    else:
        route.continue_()


def login(store: SessionStore, timeout_seconds: int = 300, channel: str | None = None) -> dict[str, Any]:
    """Wait for the user to log in, then store only encrypted OmGTU cookies.

    Playwright's browser context is ephemeral: no profile directory, storage-state
    JSON, trace, video or screenshots are produced. The user performs all login
    actions (including CAPTCHAs and any second factor) in the visible window.
    """
    store._require_windows()
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 30 <= timeout_seconds <= 1800
    ):
        raise StateError("invalid_timeout", "Login timeout must be an integer from 30 to 1800 seconds.")
    if channel not in {None, "chrome", "msedge"}:
        raise StateError("invalid_browser", "Use Chromium, chrome or msedge for the login browser.")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise StateError(
            "dependency_missing", "Install the Playwright dependency and Chromium browser first."
        ) from None

    print(
        "A separate OmGTU login window will open. Enter credentials only in that window. "
        "Open Contact work if needed. The window closes after the encrypted session is saved. "
        "No password or login form is read by this tool.",
        file=sys.stderr,
        flush=True,
    )
    try:
        stored_state = store.load()
    except StateError:
        # A damaged or different-user file does not prevent a fresh manual login.
        stored_state = None
    deadline = time.monotonic() + timeout_seconds
    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": False}
            if channel:
                launch_options["channel"] = channel
            try:
                browser = playwright.chromium.launch(**launch_options)
            except PlaywrightError:
                if channel:
                    raise
                print(
                    "Chromium could not open; trying the installed Microsoft Edge for manual login.",
                    file=sys.stderr,
                    flush=True,
                )
                browser = playwright.chromium.launch(headless=False, channel="msedge")
            try:
                context = browser.new_context(storage_state=stored_state, accept_downloads=False)
                context.route("**/*", _restrict_navigation)
                page = context.new_page()
                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightError:
                    # The visible window remains usable after a transient navigation failure.
                    print(
                        "Open https://omgtu.ru/ecab/ in the login window to continue.",
                        file=sys.stderr,
                        flush=True,
                    )
                redirected: set[int] = set()
                while time.monotonic() < deadline:
                    pages = context.pages
                    if not pages or not browser.is_connected():
                        raise StateError(
                            "login_cancelled", "The login window was closed before a session was saved."
                        )
                    for candidate in pages:
                        try:
                            if _course_list(candidate):
                                state = context.storage_state()
                                store.save(state)
                                return {
                                    "status": "authenticated",
                                    "authenticated": True,
                                    "stored": True,
                                    "protection": "windows_current_user_dpapi",
                                }
                            if _portal_landing(candidate.url) and id(candidate) not in redirected:
                                redirected.add(id(candidate))
                                candidate.goto(COURSES_URL, wait_until="domcontentloaded", timeout=15_000)
                        except PlaywrightError:
                            # The user can be navigating or opening the portal in another tab.
                            continue
                    try:
                        pages[0].wait_for_timeout(
                            min(1000, max(1, int((deadline - time.monotonic()) * 1000)))
                        )
                    except PlaywrightError:
                        continue
                raise StateError("login_timeout", "Login was not completed in time. Run login again.")
            finally:
                browser.close()
    except StateError:
        raise
    except PlaywrightError:
        raise StateError(
            "browser_unavailable",
            "The login browser could not run. Install Chromium with playwright install chromium, "
            "or use chrome/msedge.",
        ) from None
