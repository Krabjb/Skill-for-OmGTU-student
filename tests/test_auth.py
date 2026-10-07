"""Login recognition and navigation boundaries without a real account."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from omgtu_student.auth import _course_list, _portal_landing, _restrict_navigation, login
from omgtu_student.state import SessionStore, StateError


class FakePage:
    def __init__(self, url, has_table=True, has_course_links=True):
        self.url = url
        self.has_table = has_table
        self.has_course_links = has_course_links

    def locator(self, selector):
        present = self.has_table if selector == "#List" else self.has_course_links
        return SimpleNamespace(count=lambda: int(present))


@pytest.mark.parametrize("route", ["site/index", ""])
def test_logged_portal_landing_is_recognized(route):
    assert _portal_landing(f"https://up.omgtu.ru/index.php?r={route}")


@pytest.mark.parametrize(
    "url",
    [
        "https://up.omgtu.ru/index.php?r=site/login",
        "https://evilomgtu.ru/index.php?r=site/index",
        "http://up.omgtu.ru/index.php?r=site/index",
        "https://up.omgtu.ru.evil.example/index.php?r=site/index",
    ],
)
def test_non_portal_landing_rejected(url):
    assert not _portal_landing(url)


def test_course_list_requires_authenticated_table_and_discipline_links():
    url = "https://up.omgtu.ru/index.php?r=remote/read"
    assert _course_list(FakePage(url))
    assert not _course_list(FakePage(url, has_table=False))
    assert not _course_list(FakePage(url, has_course_links=False))
    assert not _course_list(FakePage("https://up.omgtu.ru/index.php?r=site/login"))
    assert not _course_list(FakePage("https://up.omgtu.ru.evil.example/index.php?r=remote/read"))


class FakeRoute:
    def __init__(self, url, *, navigation=True, top_level=True):
        self.request = SimpleNamespace(
            url=url,
            is_navigation_request=lambda: navigation,
            frame=SimpleNamespace(parent_frame=None if top_level else object()),
        )
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


@pytest.mark.parametrize(
    "url", ["https://omgtu.ru/ecab/", "https://up.omgtu.ru/index.php", "https://auth.omgtu.ru/"]
)
def test_university_login_navigation_allowed(url):
    route = FakeRoute(url)
    _restrict_navigation(route)
    assert route.action == "continue"


@pytest.mark.parametrize("url", ["https://example.com/", "http://up.omgtu.ru/", "https://evilomgtu.ru/"])
def test_external_main_frame_navigation_blocked(url):
    route = FakeRoute(url)
    _restrict_navigation(route)
    assert route.action == "abort"


def test_external_captcha_iframe_and_resources_are_allowed():
    for route in [
        FakeRoute("https://captcha.example/", top_level=False),
        FakeRoute("https://cdn.example/", navigation=False),
    ]:
        _restrict_navigation(route)
        assert route.action == "continue"


@pytest.mark.parametrize("timeout", [True, 0, 29, 1801, "300"])
def test_invalid_timeout_does_not_start_browser(timeout, tmp_path, monkeypatch):
    monkeypatch.setattr(SessionStore, "_require_windows", staticmethod(lambda: None))
    with pytest.raises(StateError) as error:
        login(SessionStore(tmp_path), timeout_seconds=timeout)
    assert error.value.code == "invalid_timeout"


def test_invalid_browser_does_not_start_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(SessionStore, "_require_windows", staticmethod(lambda: None))
    with pytest.raises(StateError) as error:
        login(SessionStore(tmp_path), channel="untrusted-executable")
    assert error.value.code == "invalid_browser"


@pytest.fixture
def isolated_login_runtime(monkeypatch):
    """A fake browser with no credential-form APIs, for lifecycle assertions."""
    calls = {"saved": None, "closed": False}

    class BrowserError(Exception):
        pass

    class LoginPage(FakePage):
        def __init__(self):
            super().__init__("about:blank")

        def goto(self, url, **kwargs):
            calls.setdefault("navigation", []).append(url)
            self.url = "https://up.omgtu.ru/index.php?r=site/index" if "/ecab/" in url else url

        def wait_for_timeout(self, milliseconds):
            pass

    page = LoginPage()

    class BrowserContext:
        pages = [page]

        def route(self, pattern, handler):
            calls["route"] = (pattern, handler)

        def new_page(self):
            return page

        def storage_state(self):
            return {"cookies": [{"name": "synthetic_session", "value": "fabricated-secret"}], "origins": []}

    context = BrowserContext()

    class Browser:
        def new_context(self, **kwargs):
            calls["context"] = kwargs
            return context

        def is_connected(self):
            return True

        def close(self):
            calls["closed"] = True

    def launch(**kwargs):
        calls["launch"] = kwargs
        if calls.get("fail_bundled_launch") and "channel" not in kwargs:
            raise BrowserError("synthetic browser cannot launch")
        return Browser()

    class Manager:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        def __exit__(self, *_):
            pass

    module = ModuleType("playwright.sync_api")
    module.Error = BrowserError
    module.sync_playwright = Manager
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)

    class Store:
        @staticmethod
        def _require_windows():
            pass

        @staticmethod
        def load():
            return None

        @staticmethod
        def save(state):
            calls["saved"] = state

    return Store(), calls, context


def test_login_uses_visible_ephemeral_context_and_no_secret_response(isolated_login_runtime, capsys):
    store, calls, _ = isolated_login_runtime
    result = login(store)
    assert result["status"] == "authenticated"
    assert calls["launch"] == {"headless": False}
    assert calls["context"] == {"storage_state": None, "accept_downloads": False}
    assert calls["saved"] is not None
    assert calls["closed"] is True
    assert "fabricated-secret" not in repr(result)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fabricated-secret" not in captured.err


def test_cancelled_login_closes_browser_without_saving(isolated_login_runtime):
    store, calls, context = isolated_login_runtime
    context.pages = []
    with pytest.raises(StateError) as error:
        login(store)
    assert error.value.code == "login_cancelled"
    assert calls["saved"] is None
    assert calls["closed"] is True


def test_bundled_browser_failure_uses_installed_edge_for_manual_login(isolated_login_runtime):
    store, calls, _ = isolated_login_runtime
    calls["fail_bundled_launch"] = True
    result = login(store)
    assert result["authenticated"] is True
    assert calls["launch"] == {"headless": False, "channel": "msedge"}
    assert calls["closed"] is True


def test_login_timeout_closes_browser_without_saving(isolated_login_runtime, monkeypatch):
    store, calls, _ = isolated_login_runtime
    ticks = iter([0, 301])
    monkeypatch.setattr("omgtu_student.auth.time.monotonic", lambda: next(ticks))
    with pytest.raises(StateError) as error:
        login(store)
    assert error.value.code == "login_timeout"
    assert calls["saved"] is None
    assert calls["closed"] is True
