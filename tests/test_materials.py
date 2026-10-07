"""Synthetic file streams verify complete/partial downloads and local path safety."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

import omgtu_student.portal as portal
from omgtu_student.portal import PortalClient, PortalError


def page(*rows: tuple[str, list[str]]) -> str:
    body = ""
    for record_id, names in rows:
        links = "".join(
            f'<a href="?r=remote/read/downloadFile&amp;id={record_id}&amp;name={name}">{name}</a>'
            for name in names
        )
        body += (
            f"<tr><td>1</td><td>ДЗ: синтетическое задание</td><td>{links}</td>"
            "<td>2026-01-01 10:00:00</td><td>Преподаватель</td></tr>"
        )
    return f'<table id="List">{body}</table>'


def transport(html: str, responses: dict[str, httpx.Response], seen: list | None = None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if request.url.params["r"] == "remote/read/taskList":
            return httpx.Response(200, text=html)
        return responses[request.url.params["name"]]

    return httpx.MockTransport(handler)


def test_download_all_same_record_attachments_manifest_hashes_and_excludes_unselected(tmp_path):
    seen = []
    payloads = {"task.pdf": b"%PDF-1.7\nsynthetic task", "example.docx": b"PK\x03\x04synthetic office file"}
    responses = {
        name: httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
        for name, body in payloads.items()
    }
    with PortalClient(
        transport=transport(page(("501", list(payloads)), ("502", ["unselected.pdf"])), responses, seen)
    ) as client:
        result = client.download_materials("101", ["501"], str(tmp_path))
    assert result["status"] == "complete"
    assert result["errors"] == []
    assert len(result["files"]) == 2
    assert all(file["assignment_ids"] == ["501"] for file in result["files"])
    for file in result["files"]:
        assert Path(file["path"]).read_bytes() == payloads[file["name"]]
        assert file["sha256"] == hashlib.sha256(payloads[file["name"]]).hexdigest()
        assert file["size"] == len(payloads[file["name"]])
        assert file["source_url"].startswith("https://up.omgtu.ru/")
        assert file["fetched_at"].endswith("+00:00")
    assert json.loads(Path(result["manifest"]).read_text(encoding="utf-8")) == result
    assert len(seen) == 3
    assert not list(tmp_path.glob("*.part"))


def test_same_url_is_deduplicated_but_same_id_different_name_is_not(tmp_path):
    seen = []
    html = page(("501", ["a.pdf", "a.pdf", "b.pdf"]))
    responses = {name: httpx.Response(200, content=b"%PDF-1.7\nsynthetic") for name in ["a.pdf", "b.pdf"]}
    with PortalClient(transport=transport(html, responses, seen)) as client:
        result = client.download_materials("101", ["501", "501"], str(tmp_path))
    assert result["status"] == "complete"
    assert len(result["files"]) == 2
    assert len(seen) == 3


def test_partial_download_records_missing_id_and_http_404(tmp_path):
    responses = {
        "ok.pdf": httpx.Response(200, content=b"%PDF-1.7\nsynthetic"),
        "missing.pdf": httpx.Response(404),
    }
    with PortalClient(transport=transport(page(("501", ["ok.pdf", "missing.pdf"])), responses)) as client:
        result = client.download_materials("101", ["501", "999"], str(tmp_path))
    assert result["status"] == "partial"
    assert len(result["files"]) == 1
    assert {error["code"] for error in result["errors"]} == {"file_missing", "assignment_not_found"}
    assert Path(result["manifest"]).exists()
    assert not (tmp_path / "missing.pdf").exists()
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            httpx.Response(
                200, content=b'<form><input type="password"></form>', headers={"content-type": "text/html"}
            ),
            "login_required",
        ),
        (
            httpx.Response(
                200,
                content=b"<!DOCTYPE html><html>Error</html>",
                headers={"content-type": "application/octet-stream"},
            ),
            "unexpected_content",
        ),
        (
            httpx.Response(200, content=b"File not found", headers={"content-type": "text/plain"}),
            "file_missing",
        ),
        (httpx.Response(200, content=b""), "empty_file"),
    ],
)
def test_login_html_missing_and_empty_are_never_saved_as_files(tmp_path, response, code):
    with PortalClient(transport=transport(page(("501", ["file.pdf"])), {"file.pdf": response})) as client:
        result = client.download_materials("101", ["501"], str(tmp_path))
    assert result["status"] == "failed"
    assert result["files"] == []
    assert result["errors"][0]["code"] == code
    assert not (tmp_path / "file.pdf").exists()
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("advertised", [True, False])
def test_size_limit_catches_headers_and_actual_stream(tmp_path, monkeypatch, advertised):
    monkeypatch.setattr(portal, "MAX_FILE_SIZE", 5)
    headers = {"content-length": "10"} if advertised else {}
    response = httpx.Response(200, content=b"0123456789", headers=headers)
    with PortalClient(
        transport=transport(page(("501", ["too-large.pdf"])), {"too-large.pdf": response})
    ) as client:
        result = client.download_materials("101", ["501"], str(tmp_path))
    assert result["errors"][0]["code"] == "file_too_large"
    assert not (tmp_path / "too-large.pdf").exists()
    assert not list(tmp_path.glob("*.part"))


def test_filename_traversal_windows_names_and_collisions_are_safe(tmp_path):
    names = ["..%2F..%2Fescape.pdf", "CON", "Report.pdf", "report.pdf", "bad%3Aname.pdf"]
    actual_names = ["../../escape.pdf", "CON", "Report.pdf", "report.pdf", "bad:name.pdf"]
    responses = {name: httpx.Response(200, content=b"%PDF-1.7\nsynthetic") for name in actual_names}
    # Link labels are descriptive; the server query filename does not control local paths.
    html = (
        page(("501", names))
        .replace(">..%2F..%2Fescape.pdf<", ">../../escape.pdf<")
        .replace(">bad%3Aname.pdf<", ">bad:name.pdf<")
    )
    with PortalClient(transport=transport(html, responses)) as client:
        result = client.download_materials("101", ["501"], str(tmp_path))
    assert result["status"] == "complete"
    assert len(result["files"]) == 5
    assert len({file["saved_name"].casefold() for file in result["files"]}) == 5
    for file in result["files"]:
        assert Path(file["path"]).resolve().parent == tmp_path.resolve()
        assert ":" not in file["saved_name"]
        assert "/" not in file["saved_name"]
    assert any(file["saved_name"].startswith("_CON-") for file in result["files"])


def test_download_refuses_overwrite_even_on_rerun(tmp_path):
    (tmp_path / "file.pdf").write_bytes(b"original local data")
    html = page(("501", ["file.pdf"]))
    response = {"file.pdf": httpx.Response(200, content=b"%PDF-1.7\nnew")}
    with PortalClient(transport=transport(html, response)) as client:
        first = client.download_materials("101", ["501"], str(tmp_path))
    assert first["status"] == "complete"
    assert first["files"][0]["saved_name"] != "file.pdf"
    with PortalClient(transport=transport(html, response)) as client:
        second = client.download_materials("101", ["501"], str(tmp_path))
    assert second["status"] == "complete"
    assert second["errors"] == []
    assert second["files"][0]["reused"] is True
    assert second["files"][0]["path"] == first["files"][0]["path"]
    assert (tmp_path / "file.pdf").read_bytes() == b"original local data"
    assert Path(first["files"][0]["path"]).read_bytes() == b"%PDF-1.7\nnew"


def test_changed_remote_file_is_saved_as_new_version_and_then_reused(tmp_path):
    html = page(("501", ["file.pdf"]))
    results = []
    for payload in [b"%PDF-1.7\nfirst", b"%PDF-1.7\nchanged", b"%PDF-1.7\nchanged"]:
        with PortalClient(
            transport=transport(html, {"file.pdf": httpx.Response(200, content=payload)})
        ) as client:
            results.append(client.download_materials("101", ["501"], str(tmp_path)))
    assert all(item["status"] == "complete" for item in results)
    assert results[0]["files"][0]["path"] != results[1]["files"][0]["path"]
    assert results[1]["files"][0]["path"] == results[2]["files"][0]["path"]
    assert results[2]["files"][0]["reused"] is True
    assert Path(results[0]["files"][0]["path"]).read_bytes() == b"%PDF-1.7\nfirst"
    assert Path(results[1]["files"][0]["path"]).read_bytes() == b"%PDF-1.7\nchanged"


def test_download_paths_are_stable_when_selection_and_order_change(tmp_path):
    html = page(("501", ["task.pdf"]), ("502", ["task.pdf"]))
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params["r"] == "remote/read/taskList":
            return httpx.Response(200, text=html)
        return httpx.Response(200, content=b"%PDF-1.7\n" + request.url.params["id"].encode())

    results = []
    with PortalClient(transport=httpx.MockTransport(handler)) as client:
        for ids in [["501"], ["502", "501"], ["501", "502"]]:
            results.append(client.download_materials("101", ids, str(tmp_path)))
    paths = [{file["assignment_ids"][0]: file for file in result["files"]} for result in results]
    assert paths[0]["501"]["path"] == paths[1]["501"]["path"] == paths[2]["501"]["path"]
    assert paths[1]["502"]["path"] == paths[2]["502"]["path"]
    assert paths[1]["501"]["reused"] is True
    assert all(file["reused"] for file in paths[2].values())
    assert len(list(tmp_path.glob("task-*.pdf"))) == 2


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"%PDF-1.7\npartial"
        raise httpx.ReadError("synthetic-private-token")


def test_interrupted_stream_has_no_complete_file_and_no_partfile(tmp_path):
    response = httpx.Response(200, stream=BrokenStream())
    with PortalClient(transport=transport(page(("501", ["file.pdf"])), {"file.pdf": response})) as client:
        result = client.download_materials("101", ["501"], str(tmp_path))
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "network_error"
    assert "synthetic-private-token" not in json.dumps(result)
    assert not (tmp_path / "file.pdf").exists()
    assert not list(tmp_path.glob("*.part"))


def test_unknown_selected_id_is_explicit_failure_manifest(tmp_path):
    with PortalClient(transport=transport(page(("501", ["file.pdf"])), {})) as client:
        result = client.download_materials("101", ["999"], str(tmp_path))
    assert result["status"] == "failed"
    assert result["requested_assignment_ids"] == ["999"]
    assert result["assignments"] == []
    assert result["files"] == []
    assert result["errors"][0]["assignment_id"] == "999"


def test_empty_selection_is_rejected_before_requests(tmp_path):
    seen = []
    with PortalClient(transport=httpx.MockTransport(lambda request: seen.append(request))) as client:
        with pytest.raises(PortalError) as caught:
            client.download_materials("101", [], str(tmp_path))
    assert caught.value.code == "invalid_selection"
    assert not seen
