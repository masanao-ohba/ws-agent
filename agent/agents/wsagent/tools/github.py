"""GitHub tools: official remote MCP (readonly endpoint) via the gateway.

Every tool answers with one text content part holding JSON.
"""

import asyncio
import json
from typing import Any

import httpx
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
from ..schemas import FailureReason, Item, Source

_MAX_PATHS_PER_REPO = 30
_MAX_TREE_ENTRIES = 300
_REPO_CONCURRENCY = 3

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
    snapshot envelope with source URLs. Some projects have no code index, and
    a kind="code" search there comes back as a not_configured failure; reach
    their source with search_github_paths instead.

    Args:
        query: Search keywords (GitHub search syntax allowed).
        kind: One of "code", "issues", "prs".
    """
    tool = {"code": "search_code", "issues": "search_issues", "prs": "search_pull_requests"}[kind]
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        assert project.github is not None
        # An unindexed project answers zero for any term; say so instead of
        # letting an empty result read as "this code does not exist".
        if kind == "code" and not project.github.code_search_indexed:
            raise NotConfigured()
        scope = " ".join(f"repo:{r}" for r in project.github.repos)
        result = _session(project).call_tool(
            tool, {"query": f"{scope} {query}".strip(), "perPage": 20}
        )
        return _search_items(project, kind, result)

    return (await fan_out(Source.GITHUB, projects, fetch)).to_tool_result()


def _match_paths(tree: dict[str, Any], query: str) -> tuple[list[str], bool]:
    """Blob paths of one tree containing `query`, case-insensitively.

    Returns the capped paths and whether anything was left out — by the cap
    here, or by GitHub itself when a tree is too large (`truncated`).
    """
    needle = query.lower()
    paths = [
        e["path"]
        for e in tree.get("tree") or []
        if e.get("type") == "blob" and needle in (e.get("path") or "").lower()
    ]
    kept = paths[:_MAX_PATHS_PER_REPO]
    return kept, bool(tree.get("truncated")) or len(kept) < len(paths)


async def _tree(client: httpx.AsyncClient, repo: str, token: str) -> dict[str, Any]:
    """The whole file list of the default branch, in one request.

    This is the git data API, not the search index, so it answers for forks
    too — which is the point of reading paths at all.
    """
    resp = await client.get(
        f"https://api.github.com/repos/{repo}/git/trees/HEAD",
        params={"recursive": "1"},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    resp.raise_for_status()
    return dict(resp.json())


async def search_github_paths(query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Find the source files of a feature by searching file paths.

    This is how you reach the code itself: source layouts name their
    controllers, models, tables and templates after the feature, so the
    paths reveal where a feature lives and read_github_file(repo, path)
    then shows what it actually does. It matches the path text only,
    never file contents, so `query` must be a fragment of an identifier as
    it is written in the source — an English class, module or directory
    name. A Japanese feature name matches nothing on its own; work out
    what the code would call it and try several spellings. Matching is
    partial and ignores case. When no spelling works, walk the layout with
    list_github_tree instead.

    Args:
        query: Fragment of a path or identifier, e.g. "PaymentReceipt".
    """
    projects = registry().projects_for(tool_context.state["project_ids"])
    # fan_out turns exceptions into failures, but a cap is not an exception:
    # the repos that hit one are collected here and declared on the envelope
    # afterwards, so a capped answer never passes as complete.
    capped: list[str] = []

    async def fetch(project: Project) -> list[Item]:
        if project.github is None:
            raise NotConfigured()
        token = _token(project)
        sem = asyncio.Semaphore(_REPO_CONCURRENCY)

        async def one(client: httpx.AsyncClient, repo: str) -> list[Item]:
            async with sem:
                tree = await _tree(client, repo, token)
            paths, incomplete = _match_paths(tree, query)
            if incomplete:
                capped.append(repo)
            return [
                Item(
                    project=project.id,
                    url=f"https://github.com/{repo}/blob/HEAD/{path}",
                    title=f"{repo}/{path}",
                    body="",
                    extra={"repo": repo, "path": path},
                )
                for path in paths
            ]

        async with httpx.AsyncClient(timeout=25) as client:
            found = await asyncio.gather(*(one(client, r) for r in project.github.repos))
        return [item for repo_items in found for item in repo_items]

    env = await fan_out(Source.GITHUB, projects, fetch)
    if capped:
        env.add_failure("*", FailureReason.TRUNCATED, ", ".join(sorted(set(capped))))
    return env.to_tool_result()


def _owning(projects: list[Project], repo: str) -> list[Project]:
    """The project whose repos hint names this repo, else fall back to all."""
    return [p for p in projects if p.github and repo in p.github.repos] or projects


async def list_github_commits(repo: str, tool_context: ToolContext) -> dict[str, Any]:
    """List the most recent commits of a GitHub repository.

    Use this to learn what changed lately — code search indexes lag behind,
    while commits are always current. Returns a snapshot envelope; each
    item's body is the commit message.

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
    source itself.

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


def _entries_at(tree: dict[str, Any], path: str) -> tuple[list[tuple[str, str]], bool]:
    """The (path, type) pairs directly under `path` in one tree.

    A recursive tree lists every level flattened, so one level is the
    entries whose remainder after the prefix holds no further slash.
    Directories come first: the point is to see where to descend next.
    Returns the capped entries and whether anything was left out — by the
    cap here, or by GitHub itself when a tree is too large (`truncated`).
    """
    prefix = f"{path.strip('/')}/" if path.strip('/') else ""
    trees: list[tuple[str, str]] = []
    blobs: list[tuple[str, str]] = []
    for e in tree.get("tree") or []:
        entry_path = e.get("path") or ""
        if not entry_path.startswith(prefix):
            continue
        rest = entry_path[len(prefix):]
        if not rest or "/" in rest:
            continue
        (trees if e.get("type") == "tree" else blobs).append((entry_path, e.get("type") or ""))
    entries = trees + blobs
    kept = entries[:_MAX_TREE_ENTRIES]
    return kept, bool(tree.get("truncated")) or len(kept) < len(entries)


async def list_github_tree(repo: str, path: str, tool_context: ToolContext) -> dict[str, Any]:
    """List the entries directly inside one directory of a GitHub repository.

    Use this to look over the layout one level at a time and learn the words
    the code itself uses. search_github_paths is faster when you can guess a
    fragment of an identifier; when you cannot — when you do not know what
    the source calls the feature you are after — descend here instead, one
    directory at a time from the root, and pick from the names you see.
    Then read the file you chose with read_github_file(repo, path).
    Directories are listed before files.

    Args:
        repo: Repository in "org/name" form.
        path: Directory within the repository; "" is the repository root.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])
    # As in search_github_paths: a cap is not an exception, so the repo that
    # hit one is declared on the envelope after the fan-out.
    capped: list[str] = []

    async def fetch(project: Project) -> list[Item]:
        if project.github is None:
            raise NotConfigured()
        token = _token(project)
        async with httpx.AsyncClient(timeout=25) as client:
            tree = await _tree(client, repo, token)
        entries, incomplete = _entries_at(tree, path)
        if incomplete:
            capped.append(repo)
        out = []
        for entry_path, kind in entries:
            # A directory and a file live under different GitHub URL prefixes.
            view = "tree" if kind == "tree" else "blob"
            out.append(
                Item(
                    project=project.id,
                    url=f"https://github.com/{repo}/{view}/HEAD/{entry_path}",
                    title=f"{repo}/{entry_path}",
                    body="",
                    extra={"repo": repo, "path": entry_path, "type": kind},
                )
            )
        return out

    env = await fan_out(Source.GITHUB, _owning(projects, repo)[:1], fetch)
    if capped:
        env.add_failure("*", FailureReason.TRUNCATED, ", ".join(sorted(set(capped))))
    return env.to_tool_result()


async def get_github_item(repo: str, number: int, tool_context: ToolContext) -> dict[str, Any]:
    """Read one GitHub issue or pull request with its discussion.

    Use this after search_github to read a specific item in full.

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
