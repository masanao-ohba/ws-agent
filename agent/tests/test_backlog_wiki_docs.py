"""Backlog wiki/document request contracts and search previews.

The Backlog API is served by an in-process httpx MockTransport, so the
assertions are on the query string that actually goes on the wire and a
renamed parameter breaks these tests. No socket is opened.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agents.wsagent.config import BacklogConfig, Project, Registry
from agents.wsagent.gateway import secrets
from agents.wsagent.tools import backlog

DOMAIN = "example.backlog.com"
PROJECT = Project(
    id="p",
    name="P",
    members=[],
    backlog=BacklogConfig(
        domain=DOMAIN, project_keys=["ALPHA", "BETA"], api_key_secret="backlog-key"
    ),
)
PROJECT_IDS = {"ALPHA": 11, "BETA": 22}

WIKI = {
    "id": 7,
    "projectId": 11,
    "name": "設計メモ",
    "content": "あ" * 2_000,
    "updated": "2026-08-01T00:00:00Z",
}
DOCUMENT_ID = "3f2a" * 8
DOCUMENT = {"id": DOCUMENT_ID, "projectId": 22, "title": "運用手順", "plain": "い" * 2_000}

CTX = SimpleNamespace(state={"project_ids": ["p"]})


class _Secrets:
    def get(self, secret_name: str) -> str:
        return "api-key"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Any:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path.startswith("/api/v2/projects/"):
            return httpx.Response(200, json={"id": PROJECT_IDS[path.rsplit("/", 1)[1]]})
        if path == "/api/v2/wikis":
            # The singular param really scopes the call: only ALPHA has a page.
            scoped = request.url.params.get("projectIdOrKey") == "ALPHA"
            return httpx.Response(200, json=[WIKI] if scoped else [])
        if path == "/api/v2/wikis/7":
            return httpx.Response(200, json=WIKI)
        if path == "/api/v2/documents":
            return httpx.Response(200, json=[DOCUMENT])
        if path == f"/api/v2/documents/{DOCUMENT_ID}":
            return httpx.Response(200, json=DOCUMENT)
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(respond)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=transport, **kw))
    monkeypatch.setattr(backlog, "registry", lambda: Registry(projects=[PROJECT]))
    secrets.set_store(_Secrets())
    yield captured
    secrets.set_store(None)


def _sent(calls: list[httpx.Request], path: str) -> list[httpx.Request]:
    return [c for c in calls if c.url.path == f"/api/v2/{path}"]


def test_document_search_filters_by_numeric_project_id(calls: list[httpx.Request]) -> None:
    """/documents answers HTTP 200 to projectIdOrKey[] and silently ignores it,
    returning every document the API key can read; only projectId[] filters."""
    asyncio.run(backlog.search_backlog_documents("q", CTX))

    params = _sent(calls, "documents")[0].url.params
    assert params.get_list("projectId[]") == ["11", "22"]
    assert "projectIdOrKey[]" not in params
    assert "projectIdOrKey" not in params
    assert params["keyword"] == "q"


def test_wiki_search_scopes_by_singular_project_key(calls: list[httpx.Request]) -> None:
    """/wikis takes one project per call — projectIdOrKey, not projectId[]."""
    asyncio.run(backlog.search_backlog_wikis("q", CTX))

    sent = _sent(calls, "wikis")
    assert [c.url.params["projectIdOrKey"] for c in sent] == ["ALPHA", "BETA"]
    assert all("projectId[]" not in c.url.params for c in sent)
    assert all(c.url.params["keyword"] == "q" for c in sent)


def test_search_bodies_are_previews(calls: list[httpx.Request]) -> None:
    wikis = asyncio.run(backlog.search_backlog_wikis("q", CTX))
    documents = asyncio.run(backlog.search_backlog_documents("q", CTX))

    assert [i["body"] for i in wikis["items"]] == [WIKI["content"][: backlog._SEARCH_BODY_CHARS]]
    assert [i["body"] for i in documents["items"]] == [
        DOCUMENT["plain"][: backlog._SEARCH_BODY_CHARS]
    ]


def test_read_tools_return_the_whole_body(calls: list[httpx.Request]) -> None:
    wiki = asyncio.run(backlog.get_backlog_wiki(7, CTX))["items"][0]
    document = asyncio.run(backlog.get_backlog_document(DOCUMENT_ID, CTX))["items"][0]

    assert wiki["body"] == WIKI["content"]
    assert document["body"] == DOCUMENT["plain"]


def test_urls_distinguish_wiki_pages_from_documents(calls: list[httpx.Request]) -> None:
    wiki = asyncio.run(backlog.search_backlog_wikis("q", CTX))["items"][0]
    document = asyncio.run(backlog.search_backlog_documents("q", CTX))["items"][0]

    assert wiki["url"] == f"https://{DOMAIN}/alias/wiki/7"
    # projectId 22 is BETA: the document URL cites the key, not the numeric id.
    assert document["url"] == f"https://{DOMAIN}/document/BETA/{DOCUMENT_ID}"
