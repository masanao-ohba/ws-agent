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
    item's url points at the issue. Answer in Japanese.

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
    specific issue. Answer in Japanese.

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
