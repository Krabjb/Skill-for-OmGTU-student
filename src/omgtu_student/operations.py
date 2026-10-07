"""Shared CLI/MCP operations; private project data never belongs in the package."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .portal import PortalClient, PortalError, select_latest
from .state import SessionStore


def resolve_course(client: PortalClient, query: str) -> dict[str, Any]:
    courses = client.list_courses()
    normalized = query.strip().casefold()
    matches = [c for c in courses if c["id"] == query.strip() or c["name"].casefold() == normalized]
    if not matches:
        matches = [c for c in courses if normalized and normalized in c["name"].casefold()]
    if len(matches) != 1:
        raise PortalError(
            "ambiguous_course" if matches else "course_not_found",
            "Укажите точное название или ID дисциплины из списка courses.",
        )
    return matches[0]


def open_client(store: SessionStore) -> PortalClient:
    state = store.load()
    if state is None:
        raise PortalError("login_required", "Выполните omgtu auth login в отдельном окне входа.")
    return PortalClient(state)


def auth_status(store: SessionStore) -> dict[str, Any]:
    if store.load() is None:
        return {"status": "login_required", "authenticated": False}
    with open_client(store) as client:
        return client.auth_status()


def list_courses(store: SessionStore) -> dict[str, Any]:
    with open_client(store) as client:
        return {"status": "ok", "courses": client.list_courses()}


def list_assignments(store: SessionStore, course: str) -> dict[str, Any]:
    with open_client(store) as client:
        selected = resolve_course(client, course)
        return {"status": "ok", "course": selected, "assignments": client.list_assignments(selected["id"])}


def latest_assignment(store: SessionStore, course: str, kind: str = "homework") -> dict[str, Any]:
    result = list_assignments(store, course)
    assignment = select_latest(result["assignments"], kind)
    return {"status": "ok", "course": result["course"], "assignment": assignment}


def download_materials(
    store: SessionStore, course: str, assignment_ids: list[str], output_dir: str
) -> dict[str, Any]:
    if not assignment_ids:
        raise PortalError("missing_assignment", "Укажите ID задания; связанные записи выбираются отдельно.")
    with open_client(store) as client:
        selected = resolve_course(client, course)
        return client.download_materials(selected["id"], assignment_ids, output_dir)


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".omgtu-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _ignore_project_data(project: Path) -> None:
    # Keep the ignore local to this project. Never alter parent repositories or other rules.
    ignore_path = project / ".gitignore"
    previous = ignore_path.read_text(encoding="utf-8-sig") if ignore_path.exists() else ""
    if ".omgtu/" not in previous.splitlines():
        with ignore_path.open("a", encoding="utf-8") as stream:
            if previous and not previous.endswith("\n"):
                stream.write("\n")
            stream.write("\n# Private OmGTU source material and project binding\n.omgtu/\n")


def bind_project(
    store: SessionStore, course: str, assignment_ids: list[str], project_dir: str
) -> dict[str, Any]:
    project = Path(project_dir).expanduser().resolve()
    if not project.is_dir():
        raise PortalError("invalid_project", "Каталог проекта должен существовать.")
    result = list_assignments(store, course)
    assignments = {a["id"]: a for a in result["assignments"]}
    ids = list(dict.fromkeys(assignment_ids))
    if not ids or any(i not in assignments for i in ids):
        raise PortalError("assignment_not_found", "Укажите существующие ID заданий из этой дисциплины.")
    binding = {
        "schema_version": 1,
        "course_id": result["course"]["id"],
        "course_name": result["course"]["name"],
        "assignment_ids": ids,
        "bound_at": datetime.now(UTC).isoformat(),
    }
    _ignore_project_data(project)
    path = project / ".omgtu" / "project.json"
    write_private_json(path, binding)
    return {"status": "ok", "binding_path": str(path), "binding": binding}


def sync_project(store: SessionStore, project_dir: str) -> dict[str, Any]:
    project = Path(project_dir).expanduser().resolve()
    path = project / ".omgtu" / "project.json"
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PortalError("binding_required", "Сначала привяжите проект командой bind.") from None
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != 1
        or not isinstance(binding.get("course_id"), str)
        or not isinstance(binding.get("assignment_ids"), list)
        or not binding["assignment_ids"]
        or not all(isinstance(i, str) for i in binding["assignment_ids"])
    ):
        raise PortalError("invalid_binding", "Некорректная привязка проекта; выполните bind заново.")
    _ignore_project_data(project)
    result = download_materials(
        store, binding["course_id"], binding["assignment_ids"], str(project / ".omgtu" / "materials")
    )
    result["binding_path"] = str(path)
    return result
