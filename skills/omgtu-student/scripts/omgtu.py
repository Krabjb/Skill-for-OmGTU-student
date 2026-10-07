"""Run the installed client without putting its dependencies or secrets in the skill."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": "unsupported_os",
                        "message": "Install this skill on Windows using scripts/install.ps1.",
                    },
                }
            )
        )
        return 2
    python = Path(local) / "OmgtuStudent" / "venv" / "Scripts" / "python.exe"
    if not python.is_file():
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": "not_installed",
                        "message": "Run scripts/install.ps1 from the OmGTU skill repository first.",
                    },
                }
            )
        )
        return 2
    # Argument arrays preserve spaces and avoid shell interpolation of university text.
    return subprocess.call([str(python), "-X", "utf8", "-m", "omgtu_student", *sys.argv[1:]], shell=False)


if __name__ == "__main__":
    raise SystemExit(main())
