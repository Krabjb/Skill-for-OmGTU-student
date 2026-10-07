"""Read-only portal access and parsers; no credentials or private HTML are logged."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://up.omgtu.ru/index.php"
COURSES_URL = f"{BASE_URL}?r=remote/read"
READ_ROUTES = frozenset({"remote/read", "remote/read/taskList", "remote/read/downloadFile"})
MAX_FILE_SIZE = 128 * 1024 * 1024
MAX_PAGE_SIZE = 8 * 1024 * 1024
_KINDS = frozenset({"homework", "lecture", "requirements", "announcement", "unknown", "any"})


class PortalError(Exception):
    """An error with a safe public code/message, never a server response or cookie."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _validate_url(url: str, *, route: str | None = None) -> str:
    try:
        parts = urlsplit(url)
        params = parse_qs(parts.query, keep_blank_values=True)
        routes = params.get("r", [])
        valid = (
            parts.scheme == "https"
            and parts.hostname == "up.omgtu.ru"
            and parts.port in {None, 443}
            and parts.username is None
            and parts.password is None
            and parts.path == "/index.php"
            and not parts.fragment
            and len(routes) == 1
            and routes[0] in READ_ROUTES
            and (route is None or routes[0] == route)
            and len(url) <= 8192
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PortalError("blocked_url", "Only approved HTTPS portal read routes are supported.")
    return url


def _course_url(course_id: str) -> str:
    if not isinstance(course_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", course_id):
        raise PortalError(
            "invalid_course", "The course identifier must contain only ASCII letters and digits."
        )
    return f"{BASE_URL}?{urlencode({'r': 'remote/read/taskList', 'discipline': course_id, 'time': '0'})}"


def _login_html(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('input[type="password"]'):
        return True
    for form in soup.find_all("form"):
        action = str(form.get("action", "")).lower()
        if "site/login" in action or "loginform" in str(form.get("id", "")).lower():
            return True
    return False


def _table(html: str) -> Any:
    if _login_html(html):
        raise PortalError("login_required", "The portal session has expired; sign in again locally.")
    table = BeautifulSoup(html, "html.parser").select_one("table#List")
    if table is None:
        raise PortalError("unexpected_page", "The portal page has an unsupported layout.")
    return table


def parse_courses(html: str, url: str = COURSES_URL) -> list[dict[str, Any]]:
    """Parse the discipline table, ignoring any links outside approved read routes."""
    _validate_url(url, route="remote/read")
    courses: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _table(html).find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        for anchor in cells[-1].find_all("a", href=True):
            candidate = urljoin(url, anchor["href"])
            try:
                _validate_url(candidate, route="remote/read/taskList")
                ids = parse_qs(urlsplit(candidate).query).get("discipline", [])
                if len(ids) != 1:
                    continue
                _course_url(ids[0])
            except PortalError:
                continue
            if ids[0] in seen:
                break
            seen.add(ids[0])
            count = _text(cells[2])
            courses.append(
                {
                    "id": ids[0],
                    "name": _text(cells[0]),
                    "url": candidate,
                    "teachers": [part.strip() for part in re.split(r"[,;]", _text(cells[1])) if part.strip()],
                    "assignment_count": int(count) if re.fullmatch(r"\d+", count) else None,
                }
            )
            break
    return courses


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate)
        # Portal timestamps are local wall-clock values, not UTC.
        return parsed.replace(tzinfo=None)
    except ValueError:
        pass
    for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate, pattern)
        except ValueError:
            continue
    return None


def _infer_kind(comment: str, attachments: list[dict[str, str]]) -> str:
    text = (comment + " " + " ".join(item["name"] for item in attachments)).casefold()
    if re.search(r"олимпиад|конкурс|объявлен|приглаша|расписание|консультаци", text):
        return "announcement"
    if re.search(
        r"требован|критери[йи]|порядок\s+(?:сдачи|выполнения)|правила\s+оформления"
        r"|методик\w*\s+рейтингового\s+контроля|руководство|методическ\w*\s+указан",
        text,
    ):
        return "requirements"
    if re.search(r"\bд\s*\.?\s*з(?=[\W_]|$)|домашн\w*\s+(?:задан|работ)|homework", text):
        return "homework"
    if re.search(r"конспект|лекци|lecture|теоретическ\w*\s+материал", text):
        return "lecture"
    return "unknown"


def parse_assignments(html: str, course_id: str) -> list[dict[str, Any]]:
    """Parse rows with comment, multiple files, date and teacher in portal order."""
    course_url = _course_url(course_id)
    assignments: list[dict[str, Any]] = []
    for row in _table(html).find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        comment = _text(cells[1])
        comment_links = []
        for anchor in cells[1].find_all("a", href=True):
            linked_url = urljoin(course_url, anchor["href"])
            try:
                parts = urlsplit(linked_url)
                if parts.scheme in {"https", "http"} and parts.hostname and not parts.username:
                    comment_links.append({"text": _text(anchor), "url": linked_url})
            except ValueError:
                continue
        teacher = _text(cells[-1])
        parsed_date = _datetime(_text(cells[-2]))
        date = parsed_date.isoformat(timespec="seconds") if parsed_date else None
        attachments: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for anchor in cells[2].find_all("a", href=True):
            candidate = urljoin(course_url, anchor["href"])
            try:
                _validate_url(candidate, route="remote/read/downloadFile")
            except PortalError:
                continue
            query = parse_qs(urlsplit(candidate).query, keep_blank_values=True)
            file_ids = query.get("id", [])
            if len(file_ids) != 1 or not file_ids[0] or candidate in seen_urls:
                continue
            seen_urls.add(candidate)
            names = query.get("name", [])
            name = (names[0] if names and names[0] else _text(anchor)) or f"attachment-{file_ids[0]}"
            attachments.append({"id": file_ids[0], "name": name, "url": candidate})
        if not comment and not attachments and not date:
            continue
        stable_fields = json.dumps([course_id, comment, date, teacher], ensure_ascii=False).encode("utf-8")
        assignment_id = (
            attachments[0]["id"] if attachments else "row-" + hashlib.sha256(stable_fields).hexdigest()[:20]
        )
        assignments.append(
            {
                "id": assignment_id,
                "comment": comment,
                "comment_links": comment_links,
                "created_at": date,
                "teacher": teacher,
                "url": course_url,
                "attachments": attachments,
                "kind": _infer_kind(comment, attachments),
            }
        )
    return sorted(assignments, key=lambda item: _datetime(item["created_at"]) or datetime.min, reverse=True)


def select_latest(assignments: list[dict[str, Any]], kind: str = "homework") -> dict[str, Any]:
    """Select by dates and kind, refusing ties or potentially newer unclassified work."""
    if kind not in _KINDS:
        raise PortalError(
            "invalid_kind", "Use homework, lecture, requirements, announcement, unknown or any."
        )
    candidates = [item for item in assignments if kind == "any" or item.get("kind") == kind]
    if not candidates:
        if kind == "homework" and any(item.get("kind") == "unknown" for item in assignments):
            raise PortalError(
                "ambiguous_assignment", "No assignment is clearly marked as homework; choose an ID."
            )
        raise PortalError("assignment_not_found", "No assignment of the requested kind was found.")
    if any(_datetime(item.get("created_at")) is None for item in candidates):
        raise PortalError("ambiguous_assignment", "A matching assignment has no usable date; choose an ID.")
    latest = max(candidates, key=lambda item: _datetime(item["created_at"]) or datetime.min)
    latest_date = _datetime(latest["created_at"])
    if sum(_datetime(item["created_at"]) == latest_date for item in candidates) > 1:
        raise PortalError("ambiguous_assignment", "Several assignments have the latest date; choose an ID.")
    if kind == "homework":
        for item in assignments:
            if item.get("kind") == "unknown" and item.get("attachments"):
                item_date = _datetime(item.get("created_at"))
                if item_date is None or item_date >= latest_date:
                    raise PortalError(
                        "ambiguous_assignment", "Newer unclassified materials may be homework; choose an ID."
                    )
    return latest


def _cookies_from_state(storage_state: dict[str, Any] | None) -> CookieJar:
    jar = CookieJar()
    for entry in (storage_state or {}).get("cookies", []):
        if not isinstance(entry, dict):
            continue
        domain = entry.get("domain", "")
        if domain not in {"up.omgtu.ru", ".up.omgtu.ru", "omgtu.ru", ".omgtu.ru"}:
            continue
        name, value, path = entry.get("name"), entry.get("value"), entry.get("path", "/")
        if not isinstance(name, str) or not isinstance(value, str) or not isinstance(path, str):
            continue
        expires = entry.get("expires", -1)
        expires = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
        jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain.startswith("."),
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=bool(entry.get("secure", True)),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": bool(entry.get("httpOnly", False))},
                rfc2109=False,
            )
        )
    return jar


def _filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(name))
    name = name.strip().rstrip(". ")
    if not name or name in {".", ".."}:
        name = "attachment"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", name.split(".")[0], re.IGNORECASE):
        name = "_" + name
    if len(name) > 160:
        suffix = Path(name).suffix[:20]
        name = name[: 160 - len(suffix)] + suffix
    return name


def _with_hash(name: str, url: str) -> str:
    suffix = Path(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    return f"{stem}-{hashlib.sha256(url.encode()).hexdigest()[:12]}{suffix}"


def _version_name(name: str, content_hash: str) -> str:
    suffix = Path(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    return f"{stem[:100]}-{content_hash}{suffix[:20]}"


def _same_file(path: Path, size: int, content_hash: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as existing:
        while chunk := existing.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == content_hash


def _inside(directory: Path, filename: str) -> Path:
    target = directory / filename
    if target.resolve().parent != directory:
        raise PortalError("unsafe_path", "A download path would leave the selected directory.")
    return target


def _publish(part_path: Path, destination: Path) -> None:
    # Linking publishes a complete file atomically and fails if the destination exists.
    try:
        os.link(part_path, destination)
    except FileExistsError as exc:
        raise PortalError("file_exists", "The destination already exists and was not overwritten.") from exc
    except OSError as exc:
        raise PortalError("write_failed", "The complete file could not be saved atomically.") from exc
    finally:
        part_path.unlink(missing_ok=True)


class PortalClient:
    """HTTPS GET-only client with manual, route-checked redirects and protected errors."""

    def __init__(self, storage_state: dict[str, Any] | None = None, transport: Any = None):
        self._http = httpx.Client(
            cookies=_cookies_from_state(storage_state),
            transport=transport,
            timeout=httpx.Timeout(45.0),
            follow_redirects=False,
            headers={"User-Agent": "omgtu-student/0.1 (+local read-only study tool)"},
        )

    def __enter__(self) -> PortalClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @contextmanager
    def _stream(self, url: str) -> Iterator[httpx.Response]:
        current_url = _validate_url(url)
        response: httpx.Response | None = None
        try:
            for _ in range(6):
                response = self._http.send(self._http.build_request("GET", current_url), stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    response.close()
                    response = None
                    if not location:
                        raise PortalError("blocked_redirect", "The portal returned an incomplete redirect.")
                    target = urljoin(current_url, location)
                    try:
                        parts = urlsplit(target)
                        query = parse_qs(parts.query)
                        if parts.hostname == "up.omgtu.ru" and query.get("r", [""])[0] in {
                            "site",
                            "site/login",
                            "site/index",
                            "site/auth",
                        }:
                            raise PortalError(
                                "login_required", "The portal session has expired; sign in again locally."
                            )
                        current_url = _validate_url(target)
                    except (ValueError, PortalError) as exc:
                        if isinstance(exc, PortalError) and exc.code == "login_required":
                            raise
                        raise PortalError(
                            "blocked_redirect", "The portal redirect is outside approved read routes."
                        ) from exc
                    continue
                if response.status_code == 401:
                    raise PortalError(
                        "login_required", "The portal session has expired; sign in again locally."
                    )
                if response.status_code == 403:
                    raise PortalError("access_denied", "The portal denied access to the requested material.")
                if response.status_code in {404, 410}:
                    raise PortalError("file_missing", "The requested portal material is no longer available.")
                if response.status_code >= 400:
                    raise PortalError("portal_error", "The portal could not serve the requested material.")
                yield response
                return
            raise PortalError("redirect_limit", "The portal returned too many redirects.")
        except httpx.HTTPError as exc:
            raise PortalError(
                "network_error", "The portal could not be reached securely; try again later."
            ) from exc
        finally:
            if response is not None:
                response.close()

    def _html(self, url: str) -> str:
        with self._stream(url) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_PAGE_SIZE:
                    raise PortalError("page_too_large", "The portal page exceeds the supported size.")
            encoding = response.encoding or "utf-8"
            html = bytes(body).decode(encoding, errors="replace")
        if _login_html(html):
            raise PortalError("login_required", "The portal session has expired; sign in again locally.")
        return html

    def auth_status(self) -> dict[str, Any]:
        try:
            self.list_courses()
        except PortalError as exc:
            if exc.code != "login_required":
                raise
            return {"status": "login_required", "authenticated": False, "source_url": COURSES_URL}
        return {"status": "authenticated", "authenticated": True, "source_url": COURSES_URL}

    def list_courses(self) -> list[dict[str, Any]]:
        return parse_courses(self._html(COURSES_URL))

    def list_assignments(self, course_id: str) -> list[dict[str, Any]]:
        return parse_assignments(self._html(_course_url(course_id)), course_id)

    def _download(self, attachment: dict[str, str], destination: Path) -> dict[str, Any]:
        part_path: Path | None = None
        try:
            with self._stream(attachment["url"]) as response:
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > MAX_FILE_SIZE:
                    raise PortalError("file_too_large", "The attachment exceeds the 128 MiB download limit.")
                digest = hashlib.sha256()
                size = 0
                prefix = bytearray()
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=".omgtu-", suffix=".part", dir=destination.parent, delete=False
                ) as output:
                    part_path = Path(output.name)
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_FILE_SIZE:
                            raise PortalError(
                                "file_too_large", "The attachment exceeds the 128 MiB download limit."
                            )
                        if len(prefix) < 16384:
                            prefix.extend(chunk[: 16384 - len(prefix)])
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size == 0:
                    raise PortalError("empty_file", "The portal returned an empty attachment.")
                sample = bytes(prefix).decode("utf-8-sig", errors="replace")
                content_type = response.headers.get("content-type", "").split(";")[0].lower().strip()
                html_like = content_type in {"text/html", "application/xhtml+xml"} or re.search(
                    r"<\s*(?:!doctype\s+html|html|head|body|form)\b", sample, re.IGNORECASE
                )
                if html_like:
                    if _login_html(sample):
                        raise PortalError(
                            "login_required", "The portal session has expired; sign in again locally."
                        )
                    raise PortalError(
                        "unexpected_content", "The portal returned an HTML page instead of an attachment."
                    )
                if size < 4096 and re.fullmatch(
                    r"\s*(?:файл\s+не\s+найден|file\s+not\s+found|not\s+found)[.!]?\s*", sample, re.IGNORECASE
                ):
                    raise PortalError("file_missing", "The requested portal material is no longer available.")
                reused = False
                if destination.exists():
                    if _same_file(destination, size, digest.hexdigest()):
                        reused = True
                    else:
                        destination = _inside(
                            destination.parent, _version_name(destination.name, digest.hexdigest())
                        )
                        if destination.exists():
                            if not _same_file(destination, size, digest.hexdigest()):
                                raise PortalError("file_exists", "An existing version was not overwritten.")
                            reused = True
                if reused:
                    part_path.unlink(missing_ok=True)
                else:
                    _publish(part_path, destination)
                part_path = None
                return {
                    "name": attachment["name"],
                    "source_url": attachment["url"],
                    "path": str(destination),
                    "saved_name": destination.name,
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "content_type": content_type,
                    "fetched_at": _utc_now(),
                    "reused": reused,
                }
        except OSError as exc:
            raise PortalError(
                "write_failed", "The attachment could not be written to the selected directory."
            ) from exc
        finally:
            if part_path is not None:
                part_path.unlink(missing_ok=True)

    def download_materials(
        self, course_id: str, assignment_ids: list[str], output_dir: str
    ) -> dict[str, Any]:
        if (
            not isinstance(assignment_ids, list)
            or not assignment_ids
            or len(assignment_ids) > 200
            or any(not isinstance(item, str) or not item for item in assignment_ids)
        ):
            raise PortalError("invalid_selection", "Provide one to 200 explicit assignment IDs.")
        assignments = self.list_assignments(course_id)
        requested = list(dict.fromkeys(assignment_ids))
        by_id = {item["id"]: item for item in assignments}
        selected = [by_id[item] for item in requested if item in by_id]
        errors: list[dict[str, Any]] = [
            {
                "assignment_id": item,
                "code": "assignment_not_found",
                "message": "The selected assignment was not found.",
            }
            for item in requested
            if item not in by_id
        ]
        try:
            directory = Path(output_dir).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError, TypeError) as exc:
            raise PortalError("invalid_output", "The download directory could not be created.") from exc
        plan: dict[str, dict[str, Any]] = {}
        for assignment in selected:
            for attachment in assignment["attachments"]:
                record = plan.setdefault(attachment["url"], {"attachment": attachment, "assignment_ids": []})
                record["assignment_ids"].append(assignment["id"])
        files: list[dict[str, Any]] = []
        for url, record in plan.items():
            attachment = record["attachment"]
            try:
                # A URL suffix keeps paths stable if selected records or their order change.
                filename = _with_hash(_filename(attachment["name"]), url)
                destination = _inside(directory, filename)
                downloaded = self._download(attachment, destination)
                downloaded["assignment_ids"] = record["assignment_ids"]
                files.append(downloaded)
            except PortalError as exc:
                errors.append(
                    {
                        "name": attachment["name"],
                        "source_url": url,
                        "assignment_ids": record["assignment_ids"],
                        "code": exc.code,
                        "message": exc.message,
                    }
                )
        status = "complete" if not errors else "partial" if files else "failed"
        manifest_name = f"manifest-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.json"
        manifest_path = _inside(directory, manifest_name)
        result = {
            "schema_version": 1,
            "status": status,
            "course_id": course_id,
            "source_url": _course_url(course_id),
            "fetched_at": _utc_now(),
            "requested_assignment_ids": requested,
            "assignments": selected,
            "files": files,
            "errors": errors,
            "manifest": str(manifest_path),
            "manifest_path": str(manifest_path),
        }
        part_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".omgtu-", suffix=".part", dir=directory, delete=False
            ) as output:
                part_path = Path(output.name)
                json.dump(result, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            _publish(part_path, manifest_path)
            part_path = None
        except OSError as exc:
            raise PortalError(
                "manifest_failed", "Files were downloaded, but the manifest could not be saved."
            ) from exc
        finally:
            if part_path is not None:
                part_path.unlink(missing_ok=True)
        return result
