"""JSON CLI. Credentials are entered by the user in the university's own page."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omgtu", description="Локальные инструменты контактной работы ОмГТУ"
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    auth_parser = commands.add_parser("auth", help="Вход и проверка сохранённой сессии")
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser("status")
    auth_commands.add_parser("clear", help="Удалить сохранённую локальную сессию")
    login_parser = auth_commands.add_parser("login", help="Открыть отдельное окно ручного входа")
    login_parser.add_argument("--timeout", type=int, default=300)
    login_parser.add_argument("--channel", choices=["chrome", "msedge"])
    commands.add_parser("courses")
    for name in ("assignments", "latest", "download", "bind"):
        sub = commands.add_parser(name)
        sub.add_argument("--course", required=True, help="Название или ID дисциплины")
        if name == "latest":
            sub.add_argument(
                "--kind",
                choices=["homework", "lecture", "requirements", "announcement", "any"],
                default="homework",
            )
        if name in ("download", "bind"):
            sub.add_argument(
                "--assignment", action="append", required=True, help="ID записи; можно повторять"
            )
        if name == "download":
            sub.add_argument("--output", required=True, help="Локальный каталог скачанных материалов")
        if name == "bind":
            sub.add_argument("--project", required=True, help="Каталог учебного проекта")
    sync = commands.add_parser("sync")
    sync.add_argument("--project", required=True)
    commands.add_parser("serve", help="Запустить MCP через stdio")
    return parser


def emit(result: object) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        from .server import main as serve

        serve()
        return 0
    from . import operations
    from .portal import PortalError
    from .state import SessionStore, StateError

    try:
        store = SessionStore()
        if args.command == "auth":
            if args.auth_command == "login":
                from .auth import login

                result = login(store, timeout_seconds=args.timeout, channel=args.channel)
            elif args.auth_command == "clear":
                store.clear()
                result = {"status": "cleared"}
            else:
                result = operations.auth_status(store)
        elif args.command == "courses":
            result = operations.list_courses(store)
        elif args.command == "assignments":
            result = operations.list_assignments(store, args.course)
        elif args.command == "latest":
            result = operations.latest_assignment(store, args.course, args.kind)
        elif args.command == "download":
            result = operations.download_materials(store, args.course, args.assignment, args.output)
        elif args.command == "bind":
            result = operations.bind_project(store, args.course, args.assignment, args.project)
        else:
            result = operations.sync_project(store, args.project)
        emit(result)
        unsuccessful = result.get("status") in ("failed", "partial", "login_required", "login_timeout")
        return 2 if unsuccessful else 0
    except (PortalError, StateError) as error:
        emit({"status": "error", "error": {"code": error.code, "message": error.message}})
        return 2
    except KeyboardInterrupt:
        emit({"status": "cancelled"})
        return 130
    except Exception:
        # Raw transport/browser exception messages can contain headers or page content.
        emit(
            {
                "status": "error",
                "error": {
                    "code": "unexpected_error",
                    "message": (
                        "Инструмент не смог завершить операцию. Проверьте установку и доступность портала."
                    ),
                },
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
