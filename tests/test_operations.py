import json

import pytest

from omgtu_student import operations
from omgtu_student.portal import PortalError


class FakePortal:
    courses = [{"id": "A123", "name": "Математика"}, {"id": "B123", "name": "Высшая математика"}]
    assignments = [{"id": "101"}, {"id": "102"}]

    def list_courses(self):
        return self.courses

    def list_assignments(self, course_id):
        assert course_id == "A123"
        return self.assignments

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def test_exact_course_name_wins_over_substring():
    assert operations.resolve_course(FakePortal(), "математика")["id"] == "A123"


def test_ambiguous_course_does_not_guess():
    with pytest.raises(PortalError) as error:
        operations.resolve_course(FakePortal(), "матем")
    assert error.value.code == "ambiguous_course"


def test_binding_preserves_other_ignore_rules_and_explicit_assignment(monkeypatch, tmp_path):
    monkeypatch.setattr(operations, "open_client", lambda _: FakePortal())
    ignore = tmp_path / ".gitignore"
    ignore.write_text("existing-rule", encoding="utf-8")
    result = operations.bind_project(object(), "Математика", ["101", "101"], str(tmp_path))
    assert result["binding"]["assignment_ids"] == ["101"]
    assert ignore.read_text(encoding="utf-8").startswith("existing-rule\n")
    assert ".omgtu/" in ignore.read_text(encoding="utf-8")
    assert json.loads((tmp_path / ".omgtu/project.json").read_text(encoding="utf-8"))["course_id"] == "A123"
    operations.bind_project(object(), "Математика", ["101"], str(tmp_path))
    assert ignore.read_text(encoding="utf-8").count(".omgtu/") == 1


def test_sync_fetches_bound_assignment_even_when_newer_exists(monkeypatch, tmp_path):
    data = tmp_path / ".omgtu"
    data.mkdir()
    (data / "project.json").write_text(
        json.dumps({"schema_version": 1, "course_id": "A123", "assignment_ids": ["101"]}), encoding="utf-8"
    )
    received = []

    def download(store, course, ids, destination):
        received.append((course, ids, destination))
        return {"status": "complete"}

    monkeypatch.setattr(operations, "download_materials", download)
    result = operations.sync_project(object(), str(tmp_path))
    assert result["status"] == "complete"
    assert received[0][0:2] == ("A123", ["101"])
    assert received[0][2] == str(data / "materials")


def test_missing_or_corrupt_binding_fails_without_network(tmp_path):
    with pytest.raises(PortalError) as error:
        operations.sync_project(object(), str(tmp_path))
    assert error.value.code == "binding_required"


def test_expired_auth_status_does_not_claim_success(monkeypatch):
    class EmptyStore:
        def load(self):
            return None

    assert operations.auth_status(EmptyStore()) == {"status": "login_required", "authenticated": False}
