import json
import runpy
import subprocess
from pathlib import Path

import pytest

SCAN = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "check_publish.py"))["scan"]


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    return tmp_path


def stage(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(root, "add", "--", name)


def test_scan_uses_staged_content_even_if_working_copy_was_cleaned(repo):
    token = "gh" + "p_" + "z" * 36
    stage(repo, "README.md", token)
    (repo / "README.md").write_text("safe", encoding="utf-8")
    assert ("README.md", "api_token") in SCAN(repo, staged=True)


def test_scan_rejects_cookie_captures_even_under_tests(repo):
    cookie_name = "university" + "_session"
    state = {"cookies": [{"name": cookie_name, "value": "synthetic-audit-value"}]}
    stage(repo, "tests/captured.json", json.dumps(state))
    assert ("tests/captured.json", "captured_session_json") in SCAN(repo, staged=True)


@pytest.mark.parametrize(
    "name", ["private/data.md", "session.dpapi", ".omgtu/project.json", "materials/task.doc"]
)
def test_scan_rejects_private_artifacts(repo, name):
    stage(repo, name, "synthetic")
    assert (name, "private_artifact") in SCAN(repo, staged=True)


def test_scan_accepts_normal_source_and_documentation(repo):
    stage(repo, "src/client.py", 'SESSION_PATH = "session.dpapi"\n')
    stage(repo, "README.md", "Enter credentials yourself in the university window.")
    assert SCAN(repo, staged=True) == []
