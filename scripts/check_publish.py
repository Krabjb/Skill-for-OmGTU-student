"""Reject private artifacts and recognizable secrets in the Git publication set.

This intentionally prints rule names and file paths, never the matched values.
Review the staged diff as well: heuristic scanning is not proof of anonymization.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

_TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".ps1", ".json"}
_PRIVATE_PARTS = {".venv", "__pycache__", ".omgtu", "private", "downloads", "materials", "playwright-report"}
_SECRET_PATTERNS = {
    "api_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|sk-(?:proj-)?[A-Za-z0-9_-]{30,})"
    ),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "literal_windows_user_path": re.compile(
        r"(?i)[a-z]:[\\/]+Users[\\/]+(?!<|example\b|user\b|public\b)[\w.-]+"
    ),
    "captured_session_json": re.compile(
        r'"cookies"\s*:\s*\[\s*\{\s*"name"\s*:\s*"(?!synthetic|test)[^"]+"'
        r'\s*,\s*"value"\s*:\s*"[^"\n]{12,}"'
    ),
}


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout


def scan(root: Path, *, staged: bool) -> list[tuple[str, str]]:
    files = (
        _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
        if staged
        else _git(root, "ls-files", "-z")
    )
    problems: list[tuple[str, str]] = []
    for raw in files.split(b"\x00"):
        if not raw:
            continue
        name = raw.decode("utf-8")
        path = Path(name)
        if (
            any(part.casefold() in _PRIVATE_PARTS or part.endswith(".egg-info") for part in path.parts)
            or path.suffix.lower() in {".dpapi", ".log", ".key", ".pem", ".doc", ".docx", ".pdf", ".zip"}
            or path.name.lower().startswith(".env")
            or re.search(r"(?i)(?:storage[-_]state|session).*\.json$", path.name)
        ):
            problems.append((name, "private_artifact"))
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES and path.name not in {
            ".gitignore",
            ".gitattributes",
            "LICENSE",
        }:
            problems.append((name, "unexpected_file_type"))
            continue
        content = _git(root, "show", f":{name}") if staged else (root / name).read_bytes()
        try:
            value = content.decode("utf-8-sig")
        except UnicodeError:
            problems.append((name, "binary_or_non_utf8"))
            continue
        if path.suffix.lower() == ".json":
            try:
                document = json.loads(value)
            except ValueError:
                document = None
            if isinstance(document, dict) and any(key in document for key in ("cookies", "storageState")):
                problems.append((name, "captured_session_json"))
        for rule, pattern in _SECRET_PATTERNS.items():
            if pattern.search(value):
                problems.append((name, rule))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    try:
        problems = scan(args.root, staged=args.staged)
    except (OSError, subprocess.CalledProcessError):
        print("Publication scan could not read the Git index.")
        return 2
    for name, rule in problems:
        print(f"BLOCKED {name}: {rule}")
    if not problems:
        print("Publication scan passed: no private artifacts or recognized secret patterns.")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
