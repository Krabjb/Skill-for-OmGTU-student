"""Security invariants use only fabricated sessions, never a user's cookies."""

from __future__ import annotations

import os

import pytest

from omgtu_student.state import SessionStore, StateError, sanitize_storage_state, university_domain


def cookie(domain="up.omgtu.ru", value="synthetic-test-session-72cbb7"):
    return {
        "name": "PHPSESSID",
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


@pytest.mark.parametrize("domain", ["omgtu.ru", ".omgtu.ru", "up.omgtu.ru", ".up.omgtu.ru", "auth.omgtu.ru"])
def test_university_cookie_domains(domain):
    assert university_domain(domain)


@pytest.mark.parametrize(
    "domain", ["evilomgtu.ru", "omgtu.ru.evil.example", "omgtu.ru:443", "omgtu.ru/", "..omgtu.ru", "", None]
)
def test_external_or_malformed_domains_are_not_university(domain):
    assert not university_domain(domain)


def test_only_university_cookies_are_retained():
    source = {
        "cookies": [cookie(), cookie("omgtu.ru.evil.example"), cookie("analytics.example")],
        "origins": [
            {"origin": "https://up.omgtu.ru", "localStorage": [{"name": "password", "value": "secret"}]}
        ],
        "password": "never-retain-this",
        "login": "never-retain-this-either",
    }
    assert sanitize_storage_state(source) == {"cookies": [cookie()], "origins": []}


@pytest.mark.parametrize("bad", [None, {}, {"cookies": "no"}, {"cookies": [None]}])
def test_invalid_state_has_no_sensitive_error_details(bad):
    with pytest.raises(StateError) as error:
        sanitize_storage_state(bad)
    assert error.value.code == "invalid_state"


@pytest.mark.parametrize("expiry", [float("nan"), float("inf"), True, "tomorrow", -2])
def test_invalid_expiry_rejected(expiry):
    item = cookie()
    item["expires"] = expiry
    with pytest.raises(StateError):
        sanitize_storage_state({"cookies": [item]})


@pytest.mark.skipif(os.name != "nt", reason="Current-user Windows DPAPI")
def test_dpapi_roundtrip_never_writes_plaintext(tmp_path):
    store = SessionStore(tmp_path)
    assert store.load() is None
    secret = "synthetic-not-real-session-913c6af"
    source = {"cookies": [cookie(value=secret)], "origins": [], "password": "never-save-password"}
    store.save(source)
    ciphertext = store.path.read_bytes()
    assert secret.encode() not in ciphertext
    assert b"never-save-password" not in ciphertext
    assert b"PHPSESSID" not in ciphertext
    assert store.load() == {"cookies": [cookie(value=secret)], "origins": []}
    assert sorted(file.name for file in tmp_path.iterdir()) == ["session.dpapi"]
    status = store.status()
    assert status["stored"] is True
    assert status["authenticated"] is None
    assert secret not in repr(status)
    store.clear()
    assert store.load() is None


@pytest.mark.skipif(os.name != "nt", reason="Current-user Windows DPAPI")
def test_tampered_session_returns_safe_error(tmp_path):
    store = SessionStore(tmp_path)
    store.path.write_bytes(b"plain-cookie-must-not-be-read")
    with pytest.raises(StateError) as error:
        store.load()
    assert error.value.code == "state_corrupt"
    assert "plain-cookie" not in str(error.value)
    assert store.status()["stored"] is False


@pytest.mark.skipif(os.name != "nt", reason="Current-user Windows DPAPI")
def test_reject_external_only_session_without_writing(tmp_path):
    store = SessionStore(tmp_path)
    with pytest.raises(StateError) as error:
        store.save({"cookies": [cookie("evilomgtu.ru")], "origins": []})
    assert error.value.code == "invalid_state"
    assert not store.path.exists()


def test_default_storage_outside_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert SessionStore().path == tmp_path / "OmgtuStudent" / "session.dpapi"


def test_unsupported_os_explicit_safe_error(monkeypatch, tmp_path):
    store = SessionStore(tmp_path)
    monkeypatch.setattr("omgtu_student.state.os.name", "posix")
    with pytest.raises(StateError) as error:
        store.load()
    assert error.value.code == "unsupported_os"
