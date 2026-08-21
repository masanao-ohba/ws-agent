"""GitHub tools: official remote MCP (readonly endpoint) via the gateway.

Every tool answers with one text content part holding JSON.
"""

import json
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..config import Project, registry
from ..gateway import secrets
from ..gateway.envelope import NotConfigured, fan_out
from ..gateway.mcp_client import (
    GITHUB_ALLOWED,
    GITHUB_TOOLSETS,
    GITHUB_URL,
    McpError,
    McpSession,
)
from ..gateway.provider import GithubAppTokens
from ..schemas import Item, Source

_tokens: GithubAppTokens | None = None


def _token(project: Project) -> str:
    """A stored PAT wins; the App path is the multi-project future."""
    global _tokens
    assert project.github is not None
    if project.github.pat_secret:
        return secrets.store().get(project.github.pat_secret)
    reg = registry()
    if reg.github_app is None or project.github.installation_id is None:
        raise NotConfigured()
    if _tokens is None:
        _tokens = GithubAppTokens(
            app_id=reg.github_app.app_id,
            private_key_secret=reg.github_app.private_key_secret,
            secrets=secrets.store(),
        )
    return _tokens.get(project.id, project.github.installation_id)


def _session(project: Project) -> McpSession:
    if project.github is None:
        raise NotConfigured()
    token = _token(project)
    return McpSession(
        url=GITHUB_URL,
        token=token,
        allowed=GITHUB_ALLOWED,
        extra_headers={"X-MCP-Toolsets": GITHUB_TOOLSETS},
    )


def _payload(result: dict[str, Any]) -> Any:
    """MCP wraps every answer as text content holding JSON."""
    content = result.get("content") or []
    if not content:
        return {}
    return json.loads(content[0].get("text") or "{}")


def _search_items(project: Project, kind: str, result: dict[str, Any]) -> list[Item]:
    """Search results: {total_count, incomplete_results, items[]}.

    Item shapes differ by kind: issues/PRs carry html_url + title + body,
    while code hits carry only name/path/sha and a repository *string*
    (not the object the REST API returns), so their URL is composed.
    """
    body = _payload(result)
    items = body.get("items", []) if isinstance(body, dict) else []
    out = []
    for it in items:
        if kind == "code":
            repo = it.get("repository", "")
            path = it.get("path", "")
            url = f"https://github.com/{repo}/blob/HEAD/{path}" if repo and path else ""
            title = f"{repo}/{path}"
            text = "\n".join(
                fragment.get("fragment", "") for fragment in it.get("text_matches", [])
            )
        else:
            url = it.get("html_url", "")
            title = f"[{it.get('number')}] {it.get('title', '')}"
            text = it.get("body") or ""
        if url:
            out.append(
                Item(
                    project=project.id,
                    url=url,
                    title=title,
                    body=text,
                    extra={"kind": kind, "state": it.get("state")},
                )
            )
    return out


def _read_item(project: Project, result: dict[str, Any]) -> list[Item]:
    """issue_read / pull_request_read return the object itself, not a list."""
    obj = _payload(result)
    if not isinstance(obj, dict) or not obj.get("html_url"):
        return []
    return [
        Item(
            project=project.id,
            url=obj["html_url"],
            title=f"[{obj.get('number')}] {obj.get('title', '')}",
            body=obj.get("body") or "",
            extra={"state": obj.get("state"), "comments": obj.get("comments")},
        )
    ]


async def search_github(query: str, kind: str, tool_context: ToolContext) -> dict[str, Any]:
    """Search GitHub code, issues, or pull requests across the user's projects.

    Use this to locate implementation, discussions, or changes. Returns a
    snapshot envelope with source URLs. Answer in Japanese.

    Args:
        query: Search keywords (GitHub search syntax allowed).
        kind: One of "code", "issues", "prs".
    """
    tool = {"code": "search_code", "issues": "search_issues", "prs": "search_pull_requests"}[kind]
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        assert project.github is not None
        scope = " ".join(f"repo:{r}" for r in project.github.repos)
        result = _session(project).call_tool(
            tool, {"query": f"{scope} {query}".strip(), "perPage": 20}
        )
        return _search_items(project, kind, result)

    return (await fan_out(Source.GITHUB, projects, fetch)).to_tool_result()


def _owning(projects: list[Project], repo: str) -> list[Project]:
    """The project whose repos hint names this repo, else fall back to all."""
    return [p for p in projects if p.github and repo in p.github.repos] or projects


async def list_github_commits(repo: str, tool_context: ToolContext) -> dict[str, Any]:
    """List the most recent commits of a GitHub repository.

    Use this to learn what changed lately — code search indexes lag behind,
    while commits are always current. Returns a snapshot envelope; each
    item's body is the commit message. Answer in Japanese.

    Args:
        repo: Repository in "org/name" form.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        owner, name = repo.split("/", 1)
        result = _session(project).call_tool(
            "list_commits", {"owner": owner, "repo": name, "perPage": 20}
        )
        commits = _payload(result)
        out = []
        for c in commits if isinstance(commits, list) else []:
            meta = c.get("commit", {})
            message = meta.get("message", "")
            out.append(
                Item(
                    project=project.id,
                    url=c.get("html_url", ""),
                    title=message.split("\n", 1)[0],
                    body=message,
                    extra={
                        "sha": c.get("sha"),
                        "date": meta.get("author", {}).get("date"),
                        "author": (c.get("author") or {}).get("login")
                        or meta.get("author", {}).get("name"),
                    },
                )
            )
        return [i for i in out if i.url]

    return (await fan_out(Source.GITHUB, _owning(projects, repo)[:1], fetch)).to_tool_result()


async def read_github_file(repo: str, path: str, tool_context: ToolContext) -> dict[str, Any]:
    """Read one file from a GitHub repository (default branch, full content).

    Use this after search_github or list_github_commits to read the current
    source itself. Answer in Japanese.

    Args:
        repo: Repository in "org/name" form.
        path: File path within the repository.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        owner, name = repo.split("/", 1)
        result = _session(project).call_tool(
            "get_file_contents", {"owner": owner, "repo": name, "path": path}
        )
        # content[0] is a status text; the file
        # itself arrives as a resource part {uri, mimeType, text}.
        for part in result.get("content") or []:
            resource = part.get("resource")
            if isinstance(resource, dict) and resource.get("text"):
                return [
                    Item(
                        project=project.id,
                        url=f"https://github.com/{repo}/blob/HEAD/{path}",
                        title=f"{repo}/{path}",
                        body=resource["text"],
                        extra={"mime_type": resource.get("mimeType")},
                    )
                ]
        return []

    return (await fan_out(Source.GITHUB, _owning(projects, repo)[:1], fetch)).to_tool_result()


async def get_github_item(repo: str, number: int, tool_context: ToolContext) -> dict[str, Any]:
    """Read one GitHub issue or pull request with its discussion.

    Use this after search_github to read a specific item in full.
    Answer in Japanese.

    Args:
        repo: Repository in "org/name" form.
        number: Issue or pull request number.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])
    owning = [p for p in projects if p.github and repo in p.github.repos] or projects

    async def fetch(project: Project) -> list[Item]:
        owner, name = repo.split("/", 1)
        session = _session(project)
        # An issue number and a PR number share one sequence; try the issue
        # reader first and fall back rather than asking the model which it is.
        try:
            result = session.call_tool(
                "issue_read", {"method": "get", "owner": owner, "repo": name,
                               "issue_number": number}
            )
        except McpError:
            result = session.call_tool(
                "pull_request_read", {"method": "get", "owner": owner, "repo": name,
                                      "pullNumber": number}
            )
        return _read_item(project, result)

    return (await fan_out(Source.GITHUB, owning[:1], fetch)).to_tool_result()
