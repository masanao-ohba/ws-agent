"""GitHub MCP payload parsing.

Fixtures mirror the GitHub MCP response shapes, so a server-side shape
change breaks these tests rather than silently returning nothing.
"""

import json
from typing import Any

from agents.wsagent.config import Project
from agents.wsagent.tools.github import _read_item, _search_items

PROJECT = Project(id="p", name="P", members=[])


def wrap(payload: Any) -> dict[str, Any]:
    """MCP delivers the JSON body as a single text content part."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_issue_search_items_use_html_url() -> None:
    result = wrap(
        {
            "total_count": 2,
            "items": [
                {
                    "html_url": "https://github.com/acme/example/issues/123",
                    "number": 123,
                    "title": "Example issue",
                    "body": "Example body",
                    "state": "open",
                }
            ],
        }
    )
    items = _search_items(PROJECT, "issues", result)
    assert len(items) == 1
    assert items[0].url == "https://github.com/acme/example/issues/123"
    assert items[0].title == "[123] Example issue"
    assert items[0].extra["state"] == "open"


def test_code_search_composes_url_from_string_repository() -> None:
    """Code hits carry `repository` as a plain "org/name" string and have no
    html_url, so the citation URL has to be composed."""
    result = wrap(
        {
            "items": [
                {
                    "name": "Example.php",
                    "path": "src/Example.php",
                    "sha": "0000000",
                    "repository": "acme/example",
                    "text_matches": [{"fragment": "class Example"}, {"fragment": "Example::find"}],
                }
            ]
        }
    )
    items = _search_items(PROJECT, "code", result)
    assert items[0].url == "https://github.com/acme/example/blob/HEAD/src/Example.php"
    assert items[0].body == "class Example\nExample::find"


def test_items_without_url_are_dropped() -> None:
    """Item.url is the citation; an item that cannot be cited is not returned."""
    assert _search_items(PROJECT, "issues", wrap({"items": [{"number": 1, "title": "x"}]})) == []
    assert _search_items(PROJECT, "code", wrap({"items": [{"path": "a.php"}]})) == []


def test_read_item_parses_single_object() -> None:
    result = wrap(
        {
            "html_url": "https://github.com/acme/example/pull/456",
            "number": 456,
            "title": "Example pull request",
            "body": "## Summary",
            "state": "closed",
            "comments": 3,
        }
    )
    items = _read_item(PROJECT, result)
    assert items[0].url.endswith("/pull/456")
    assert items[0].extra["comments"] == 3


def test_empty_content_is_not_an_error() -> None:
    assert _search_items(PROJECT, "issues", {}) == []
    assert _read_item(PROJECT, {}) == []
