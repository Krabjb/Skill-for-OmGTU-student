"""Cookie-only session storage encrypted for the current Windows user with DPAPI."""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_MAGIC = b"OMGTU-STUDENT\x00\x01"
_ENTROPY = b"omgtu-student.session.v1"
_MAX_BYTES = 2 * 1024 * 1024
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*omgtu\.ru$")


class StateError(Exception):
    """An error safe to show in a CLI or MCP response; never include session data."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def university_domain(domain: object) -> bool:
    """Accept only OmGTU cookie domains, with an optional cookie-leading dot."""
    return isinstance(domain, str) and bool(_DOMAIN.fullmatch(domain.lower().removeprefix(".")))


def sanitize_storage_state(state: object) -> dict[str, Any]:
    """Keep university cookies; discard all page storage and non-cookie metadata.

    The portal is server-rendered. Local storage, IndexedDB and arbitrary captured
    fields are deliberately excluded so login-form values cannot enter the file.
    Third-party analytics cookies are omitted rather than saved.
    """
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise StateError("invalid_state", "The browser returned an invalid session state.")
    cookies = []
    for cookie in state["cookies"]:
        if not isinstance(cookie, dict):
            raise StateError("invalid_state", "The browser returned an invalid session cookie.")
        if not university_domain(cookie.get("domain")):
            continue
        name, value, path = cookie.get("name"), cookie.get("value"), cookie.get("path", "/")
        expires = cookie.get("expires", -1)
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            or not isinstance(path, str)
            or not path.startswith("/")
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or expires < -1
        ):
            raise StateError("invalid_state", "The browser returned an invalid session cookie.")
        same_site = cookie.get("sameSite", "Lax")
        if same_site not in {"Strict", "Lax", "None"}:
            raise StateError("invalid_state", "The browser returned an invalid cookie policy.")
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": cookie["domain"].lower(),
                "path": path,
                "expires": expires,
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", False)),
                "sameSite": same_site,
            }
        )
    return {"cookies": cookies, "origins": []}


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    backing = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), backing), backing


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise StateError("unsupported_os", "Session storage requires Windows and current-user DPAPI.")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_ptr = ctypes.POINTER(_DataBlob)
        crypt32.CryptProtectData.argtypes = [
            blob_ptr,
            ctypes.c_wchar_p,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            blob_ptr,
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = [
            blob_ptr,
            ctypes.c_void_p,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            blob_ptr,
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        source, source_buffer = _blob(data)
        entropy, entropy_buffer = _blob(_ENTROPY)
        output = _DataBlob()
        if decrypt:
            success = crypt32.CryptUnprotectData(
                ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)
            )
        else:
            success = crypt32.CryptProtectData(
                ctypes.byref(source),
                "OmGTU Student session",
                ctypes.byref(entropy),
                None,
                None,
                1,
                ctypes.byref(output),
            )
        # Keep backing buffers alive throughout the native call.
        _ = source_buffer, entropy_buffer
        if not success:
            raise StateError(
                "state_decryption_failed" if decrypt else "state_encryption_failed",
                "Windows could not decrypt the session. Log in again on this Windows account."
                if decrypt
                else "Windows could not encrypt the session.",
            )
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))
    except StateError:
        raise
    except Exception:
        raise StateError("dpapi_unavailable", "Windows session protection is unavailable.") from None


class SessionStore:
    """The state file is outside source by default and is never plaintext."""

    def __init__(self, state_dir: Path | str | None = None) -> None:
        if state_dir is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise StateError("unsupported_os", "Windows LOCALAPPDATA is required for session storage.")
            state_dir = Path(local_app_data) / "OmgtuStudent"
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.path = self.state_dir / "session.dpapi"

    @staticmethod
    def _require_windows() -> None:
        if os.name != "nt":
            raise StateError("unsupported_os", "Session storage requires Windows and current-user DPAPI.")

    def load(self) -> dict[str, Any] | None:
        self._require_windows()
        try:
            if not self.path.exists():
                return None
            if self.path.stat().st_size > _MAX_BYTES:
                raise StateError("state_corrupt", "The session file is invalid. Log in again.")
            encrypted = self.path.read_bytes()
            if not encrypted.startswith(_MAGIC):
                raise StateError("state_corrupt", "The session file is invalid. Log in again.")
            plaintext = _dpapi(encrypted[len(_MAGIC) :], decrypt=True)
            if len(plaintext) > _MAX_BYTES:
                raise StateError("state_corrupt", "The session file is invalid. Log in again.")
            state = sanitize_storage_state(json.loads(plaintext))
            if not state["cookies"]:
                raise StateError("state_corrupt", "No university session was found. Log in again.")
            return state
        except StateError:
            raise
        except (ValueError, UnicodeError):
            raise StateError("state_corrupt", "The session file is invalid. Log in again.") from None
        except OSError:
            raise StateError("state_read_failed", "The encrypted session file could not be read.") from None

    def save(self, storage_state: dict[str, Any]) -> None:
        self._require_windows()
        state = sanitize_storage_state(storage_state)
        if not state["cookies"]:
            raise StateError("invalid_state", "No university session cookies were found.")
        try:
            plaintext = json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        except (ValueError, UnicodeError):
            raise StateError("invalid_state", "The browser returned an invalid session state.") from None
        if len(plaintext) > _MAX_BYTES:
            raise StateError("invalid_state", "The session state is too large.")
        encrypted = _MAGIC + _dpapi(plaintext, decrypt=False)
        temporary_path: Path | None = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(
                prefix=".session-", suffix=".tmp", dir=self.state_dir, delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        except OSError:
            raise StateError("state_write_failed", "The encrypted session file could not be saved.") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> None:
        self._require_windows()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise StateError(
                "state_delete_failed", "The encrypted session file could not be removed."
            ) from None

    def status(self) -> dict[str, Any]:
        """Local presence/decryption status only; does not assert the portal accepted it."""
        try:
            state = self.load()
            return {
                "stored": state is not None,
                "protection": "windows_current_user_dpapi",
                "authenticated": None,
                "message": "Check the portal to confirm the session is current."
                if state
                else "Login is required.",
            }
        except StateError as exc:
            return {"stored": False, "authenticated": None, "error": exc.code, "message": exc.message}
