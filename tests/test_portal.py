"""Synthetic portal fixtures only: no student identifiers or real page captures."""

import httpx
import pytest

from omgtu_student.portal import (
    COURSES_URL,
    PortalClient,
    PortalError,
    parse_assignments,
    parse_courses,
    select_latest,
)


def course_page() -> str:
    return """<table id="List"><thead><tr><th>Дисциплина</th><th>Преподаватели</th>
    <th>Количество заданий</th><th>Задание</th></tr></thead><tbody>
    <tr><td>Синтетическая математика</td><td>Учебный преподаватель, Второй преподаватель</td><td>3</td>
    <td><h4><a href="index.php?r=remote/read/taskList&amp;discipline=101&amp;time=0">🔎</a></h4></td></tr>
    <tr><td>Пустая дисциплина</td><td>Преподаватель</td><td>0</td>
    <td><a href="?r=remote/read/taskList&amp;discipline=102&amp;time=0">🔎</a></td></tr>
    <tr><td>Посторонняя ссылка</td><td>Преподаватель</td><td>5</td>
    <td><a href="https://untrusted.example/index.php?r=remote/read/taskList&amp;discipline=103">🔎</a></td></tr>
    </tbody></table>"""


def assignment_page(rows: str) -> str:
    return f"""<table id="List"><tr><th>Номер</th><th>Комментарий</th><th>Файлы</th>
    <th>Дата создания</th><th>Преподаватель</th></tr><tbody>{rows}</tbody></table>"""


def assignment_row(comment: str, date: str, links: str = "") -> str:
    return (
        f"<tr><td>1</td><td><div>{comment}</div></td><td>{links}</td>"
        f"<td>{date}</td><td>Преподаватель</td></tr>"
    )


def file_link(record_id: str, name: str) -> str:
    return f'<a href="?r=remote/read/downloadFile&amp;id={record_id}&amp;name={name}">{name}</a>'


def test_courses_parse_icons_and_reject_external_link():
    courses = parse_courses(course_page())
    assert [item["id"] for item in courses] == ["101", "102"]
    assert courses[0]["name"] == "Синтетическая математика"
    assert courses[0]["teachers"] == ["Учебный преподаватель", "Второй преподаватель"]
    assert courses[0]["assignment_count"] == 3
    assert courses[1]["assignment_count"] == 0


def test_hexadecimal_course_ids_are_supported():
    courses = parse_courses(course_page().replace("discipline=101", "discipline=C101AB"))
    assert courses[0]["id"] == "C101AB"
    assert (
        parse_assignments(assignment_page(assignment_row("ДЗ", "2026-01-01")), "C101AB")[0]["kind"]
        == "homework"
    )


def test_assignments_keep_all_files_with_same_record_id_and_sort_dates():
    html = assignment_page(
        assignment_row(
            "Домашнее задание",
            "2026-01-02 10:20:30",
            file_link("501", "tasks.pdf") + file_link("501", "example.docx"),
        )
        + assignment_row("Конспект", "2026-01-04 08:00:00", file_link("502", "lecture.pdf"))
        + assignment_row("Приглашение на олимпиаду", "2026-01-05 08:00:00")
    )
    assignments = parse_assignments(html, "101")
    assert [item["kind"] for item in assignments] == ["announcement", "lecture", "homework"]
    homework = assignments[2]
    assert homework["id"] == "501"
    assert len(homework["attachments"]) == 2
    assert {file["name"] for file in homework["attachments"]} == {"tasks.pdf", "example.docx"}
    assert homework["created_at"] == "2026-01-02T10:20:30"
    assert select_latest(assignments)["id"] == "501"


def test_link_names_can_mark_homework_and_external_links_are_not_attachments():
    html = assignment_page(
        assignment_row(
            'Решение системы. <a href="https://reference.example/topic">Материал</a>',
            "2026-02-03 15:00:00",
            file_link("503", "ДЗ_система.pdf")
            + '<a href="https://untrusted.example/download?id=1">other.pdf</a>'
            + '<a href="?r=remote/write/delete&amp;id=503">delete.pdf</a>',
        )
    )
    assignment = parse_assignments(html, "101")[0]
    assert assignment["kind"] == "homework"
    assert len(assignment["attachments"]) == 1
    assert "Материал" in assignment["comment"]


def test_no_attachment_ids_are_stable_if_rows_reordered():
    first = assignment_row("Требования к проекту", "2026-01-02 10:00:00")
    second = assignment_row("Объявление", "2026-01-03 10:00:00")
    forward = parse_assignments(assignment_page(first + second), "101")
    reverse = parse_assignments(assignment_page(second + first), "101")
    assert [item["id"] for item in forward] == [item["id"] for item in reverse]
    assert forward[1]["kind"] == "requirements"


def test_filename_comes_from_query_not_download_icon():
    html = assignment_page(
        assignment_row(
            "Задачи",
            "2026-01-01",
            '<a href="?r=remote/read/downloadFile&amp;id=501&amp;name=%D0%94%D0%97.pdf">\ue022</a>',
        )
    )
    assignment = parse_assignments(html, "101")[0]
    assert assignment["attachments"][0]["name"] == "ДЗ.pdf"
    assert assignment["kind"] == "homework"


@pytest.mark.parametrize(
    "comment", ["Методика рейтингового контроля", "Руководство к работе", "Методические указания"]
)
def test_methodical_materials_are_requirements(comment):
    assert (
        parse_assignments(assignment_page(assignment_row(comment, "2026-01-01")), "101")[0]["kind"]
        == "requirements"
    )


def test_any_kind_selects_latest_by_date():
    assignments = [
        {"id": "1", "kind": "homework", "created_at": "2026-01-01"},
        {"id": "2", "kind": "announcement", "created_at": "2026-01-02"},
    ]
    assert select_latest(assignments, "any")["id"] == "2"


@pytest.mark.parametrize("date", ["2026-01-03 10:15:00", "03.01.2026 10:15:00"])
def test_portal_date_formats(date):
    assert (
        parse_assignments(assignment_page(assignment_row("ДЗ", date)), "101")[0]["created_at"]
        == "2026-01-03T10:15:00"
    )


@pytest.mark.parametrize(
    "html", ['<form><input type="password"></form>', '<form action="?r=site/login">Вход</form>']
)
def test_login_page_with_http_200_is_not_a_course_table(html):
    with pytest.raises(PortalError) as caught:
        parse_courses(html)
    assert caught.value.code == "login_required"


def test_missing_table_is_an_explicit_layout_error():
    with pytest.raises(PortalError) as caught:
        parse_assignments("<h1>Unexpected page</h1>", "101")
    assert caught.value.code == "unexpected_page"


def test_empty_valid_tables_are_supported():
    assert parse_courses('<table id="List"><tbody></tbody></table>') == []
    assert parse_assignments('<table id="List"><tbody></tbody></table>', "101") == []


def test_latest_homework_rejects_tie_missing_date_and_newer_unknown():
    homework = {"id": "1", "kind": "homework", "created_at": "2026-01-02T10:00:00", "attachments": [{}]}
    cases = [
        [homework, {**homework, "id": "2"}],
        [homework, {**homework, "id": "2", "created_at": None}],
        [homework, {**homework, "id": "2", "kind": "unknown", "created_at": "2026-01-03T10:00:00"}],
        [{**homework, "kind": "unknown"}],
    ]
    for assignments in cases:
        with pytest.raises(PortalError) as caught:
            select_latest(assignments)
        assert caught.value.code == "ambiguous_assignment"


def test_status_uses_domain_filtered_session_cookie():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=course_page())

    state = {
        "cookies": [
            {
                "name": "synthetic_session",
                "value": "synthetic",
                "domain": "up.omgtu.ru",
                "path": "/",
                "secure": True,
            },
            {"name": "external_cookie", "value": "excluded", "domain": "example.org", "path": "/"},
            {"name": "other_path", "value": "excluded", "domain": "up.omgtu.ru", "path": "/private"},
        ]
    }
    with PortalClient(state, transport=httpx.MockTransport(handler)) as client:
        assert client.auth_status()["authenticated"] is True
    assert len(seen) == 1
    assert str(seen[0].url) == COURSES_URL
    assert seen[0].method == "GET"
    assert seen[0].headers["cookie"] == "synthetic_session=synthetic"


def test_expired_session_status_is_public_and_safe():
    with PortalClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<input type="password">'))
    ) as client:
        assert client.auth_status() == {
            "status": "login_required",
            "authenticated": False,
            "source_url": COURSES_URL,
        }


@pytest.mark.parametrize(
    "location",
    [
        "https://untrusted.example/collect",
        "http://up.omgtu.ru/index.php?r=remote/read",
        "https://up.omgtu.ru/index.php?r=remote/write/delete",
        "https://up.omgtu.ru/index.php?r=remote/read&r=remote/write",
        "https://up.omgtu.ru.evil.example/index.php?r=remote/read",
        "https://up.omgtu.ru:8443/index.php?r=remote/read",
    ],
)
def test_bad_redirect_is_blocked_before_another_request(location):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    with PortalClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PortalError) as caught:
            client.list_courses()
    assert caught.value.code == "blocked_redirect"
    assert seen == [COURSES_URL]


@pytest.mark.parametrize("route", ["site/login", "site"])
def test_login_redirect_is_detected_without_following_it(route):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "index.php?r=" + route})

    with PortalClient(transport=httpx.MockTransport(handler)) as client:
        assert client.auth_status()["status"] == "login_required"
    assert len(seen) == 1


def test_network_error_does_not_include_sensitive_exception_text():
    def handler(request):
        raise httpx.ConnectError("private-cookie-value", request=request)

    with PortalClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PortalError) as caught:
            client.list_courses()
    assert caught.value.code == "network_error"
    assert "private-cookie-value" not in str(caught.value)


@pytest.mark.parametrize(
    "course_id", ["../101", "101&r=remote/write", "", "https://example.org", "кириллица"]
)
def test_invalid_course_does_not_make_any_requests(course_id):
    seen = []
    with PortalClient(transport=httpx.MockTransport(lambda request: seen.append(request))) as client:
        with pytest.raises(PortalError) as caught:
            client.list_assignments(course_id)
    assert caught.value.code == "invalid_course"
    assert not seen
