from omgtu_student.portal import parse_assignments


def test_comment_material_links_are_preserved_as_metadata_without_fetching():
    html = '''<table id="List"><tr><td>1</td><td>Задание и
      <a href="https://example.org/material">материал</a>
      <a href="javascript:alert(1)">unsupported</a></td><td></td>
      <td>2030-01-01 12:00:00</td><td>Преподаватель</td></tr></table>'''
    result = parse_assignments(html, "A123")
    assert result[0]["comment_links"] == [{"text": "материал", "url": "https://example.org/material"}]
    assert result[0]["attachments"] == []
