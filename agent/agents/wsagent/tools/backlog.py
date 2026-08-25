"""Backlog tools: REST API v2, GET only. No write code path exists.

Per-project API key, 429 backoff honouring Retry-After, offset paging
with count=100.
"""

from typing import Any

import httpx
from google.adk.tools.tool_context import ToolContext

from ..config import Project, registry
from ..gateway.envelope import NotConfigured, fan_out
from ..schemas import Item, Source

_MAX_RETRIES = 5
# Wiki and document list endpoints return the whole page body inline
# (180k-character pages exist), so search results keep a preview only.
_SEARCH_BODY_CHARS = 400


async def _get(project: Project, path: str, params: dict[str, Any]) -> Any:
    if project.backlog is None:
        raise NotConfigured()
    cfg = project.backlog
    from ..gateway import secrets  # lazy: no Secret Manager at import time

    key = secrets.store().get(cfg.api_key_secret)
    async with httpx.AsyncClient(timeout=25) as client:
        for attempt in range(_MAX_RETRIES):
            resp = await client.get(
                f"https://{cfg.domain}/api/v2/{path}", params=params | {"apiKey": key}
            )
            if resp.status_code == 429:
                import asyncio

                wait = int(resp.headers.get("Retry-After", 15 * (attempt + 1)))
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
    raise TimeoutError("backlog: rate limited after retries")


def _issue_item(project: Project, issue: dict[str, Any]) -> Item:
    assert project.backlog is not None
    key = issue["issueKey"]
    return Item(
        project=project.id,
        url=f"https://{project.backlog.domain}/view/{key}",
        title=f"[{key}] {issue['summary']}",
        body=issue.get("description") or "",
        extra={"status": issue.get("status", {}).get("name")},
    )


async def search_backlog_issues(query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Search Backlog issues across the user's projects.

    Use this to find issues by keyword. Returns a snapshot envelope; each
    item's url points at the issue.

    Args:
        query: Keyword(s) to search in issue summary and description.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        assert project.backlog is not None
        # All configured Backlog projects in one query: /issues accepts
        # repeated projectId[] params.
        ids = [
            (await _get(project, f"projects/{key}", {}))["id"]
            for key in project.backlog.project_keys
        ]
        issues = await _get(
            project,
            "issues",
            {"projectId[]": ids, "keyword": query, "count": 20, "sort": "updated"},
        )
        return [_issue_item(project, i) for i in issues]

    return (await fan_out(Source.BACKLOG, projects, fetch)).to_tool_result()


async def get_backlog_issue(issue_key: str, tool_context: ToolContext) -> dict[str, Any]:
    """Read one Backlog issue with its comments.

    Use this after search_backlog_issues to read the full discussion of a
    specific issue.

    Args:
        issue_key: Issue key like "PROJ-123".
    """
    projects = registry().projects_for(tool_context.state["project_ids"])
    matching = [
        p
        for p in projects
        if p.backlog and any(issue_key.startswith(k + "-") for k in p.backlog.project_keys)
    ] or projects

    async def fetch(project: Project) -> list[Item]:
        issue = await _get(project, f"issues/{issue_key}", {})
        comments = await _get(project, f"issues/{issue_key}/comments", {"count": 100})
        item = _issue_item(project, issue)
        item.body += "\n\n" + "\n---\n".join(
            f"{c['createdUser']['name']}: {c['content']}" for c in comments if c.get("content")
        )
        return [item]

    return (await fan_out(Source.BACKLOG, matching[:1], fetch)).to_tool_result()


def _preview(items: list[Item]) -> list[Item]:
    for item in items:
        item.body = item.body[:_SEARCH_BODY_CHARS]
    return items


def _wiki_item(project: Project, wiki: dict[str, Any]) -> Item:
    assert project.backlog is not None
    return Item(
        project=project.id,
        url=f"https://{project.backlog.domain}/alias/wiki/{wiki['id']}",
        title=wiki["name"],
        body=wiki.get("content") or "",
        extra={"wiki_id": wiki["id"], "updated": wiki.get("updated")},
    )


async def search_backlog_wikis(query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Search Backlog wiki pages across the user's projects.

    Wiki pages and documents are two different Backlog features: wiki pages
    live under /alias/wiki/ and are found here, documents live under
    /document/ and are found with search_backlog_documents. Item bodies are
    previews; read a page in full with get_backlog_wiki.

    Args:
        query: Keyword(s) to search in wiki page names and content.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        assert project.backlog is not None
        # /wikis scopes to one project per call: projectIdOrKey is singular
        # here, unlike the repeated projectId[] that /issues takes.
        items: list[Item] = []
        for key in project.backlog.project_keys:
            wikis = await _get(project, "wikis", {"projectIdOrKey": key, "keyword": query})
            items.extend(_wiki_item(project, w) for w in wikis)
        return _preview(items)

    return (await fan_out(Source.BACKLOG, projects, fetch)).to_tool_result()


async def get_backlog_wiki(wiki_id: int, tool_context: ToolContext) -> dict[str, Any]:
    """Read one Backlog wiki page in full.

    Use this after search_backlog_wikis, whose item bodies are previews.

    Args:
        wiki_id: Wiki page id from a previous search result (extra.wiki_id).
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        return [_wiki_item(project, await _get(project, f"wikis/{wiki_id}", {}))]

    return (await fan_out(Source.BACKLOG, projects[:1], fetch)).to_tool_result()


async def _project_keys_by_id(project: Project) -> dict[int, str]:
    """Numeric project id -> project key, for endpoints that speak only ids."""
    assert project.backlog is not None
    return {
        (await _get(project, f"projects/{key}", {}))["id"]: key
        for key in project.backlog.project_keys
    }


def _document_item(project: Project, doc: dict[str, Any], keys_by_id: dict[int, str]) -> Item:
    assert project.backlog is not None
    key = keys_by_id.get(doc["projectId"], "")
    return Item(
        project=project.id,
        url=f"https://{project.backlog.domain}/document/{key}/{doc['id']}",
        title=doc.get("title") or "",
        body=doc.get("plain") or "",
        extra={"document_id": doc["id"]},
    )


async def search_backlog_documents(query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Search Backlog documents across the user's projects.

    Documents and wiki pages are two different Backlog features: documents
    live under /document/ and are found here, wiki pages live under
    /alias/wiki/ and are found with search_backlog_wikis. Item bodies are
    previews; read a document in full with get_backlog_document.

    Args:
        query: Keyword(s) to search in document titles and text.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        keys_by_id = await _project_keys_by_id(project)
        docs = await _get(
            project,
            "documents",
            # projectId[] takes numeric ids only. /documents accepts a
            # projectIdOrKey[] param with HTTP 200 and silently ignores it,
            # returning every document the API key can read.
            {"projectId[]": list(keys_by_id), "keyword": query, "count": 20},
        )
        return _preview([_document_item(project, d, keys_by_id) for d in docs])

    return (await fan_out(Source.BACKLOG, projects, fetch)).to_tool_result()


async def get_backlog_document(document_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Read one Backlog document in full.

    Use this after search_backlog_documents, whose item bodies are previews.

    Args:
        document_id: Document id from a previous search result
            (extra.document_id), a 32-character hex string.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        doc = await _get(project, f"documents/{document_id}", {})
        # The response names its project by numeric id; the citation URL
        # needs the project key.
        return [_document_item(project, doc, await _project_keys_by_id(project))]

    return (await fan_out(Source.BACKLOG, projects[:1], fetch)).to_tool_result()
