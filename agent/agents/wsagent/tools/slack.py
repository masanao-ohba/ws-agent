"""Slack tools: Web API with a user token, read methods only.

The official Slack MCP exposes three channel-scoped tools and no search,
so it cannot serve cross-source discovery. These tools
call the Web API directly (search.messages, conversations.replies), which is
also where permalinks come from — every item must cite its source.

Only the methods listed in READ_METHODS are reachable; the token's scopes and
the authorizing user's channel membership are the actual access boundary.
"""

from typing import Any

import httpx
from google.adk.tools.tool_context import ToolContext

from ..config import Project, registry
from ..gateway import secrets
from ..gateway.envelope import NotConfigured, fan_out
from ..schemas import Item, Source

API = "https://slack.com/api"

# The gate: a write method is not reachable because it is not in this set.
READ_METHODS = frozenset(
    {
        "search.messages",
        "conversations.replies",
        "conversations.history",
        "conversations.list",
        "users.info",
        "auth.test",
    }
)

_team_url: dict[str, str] = {}


class MethodNotAllowed(Exception):
    """A method outside READ_METHODS was requested — a bug, not a runtime state."""


def _token(project: Project) -> str:
    """The stored user token, used as-is.

    Token rotation is deliberately not used: a rotating
    credential would make the runtime rewrite its own secrets, which neither
    Secret Manager nor this system's "hold what the project provides" model
    is meant for.
    """
    if project.slack is None:
        raise NotConfigured()
    return secrets.store().get(project.slack.user_token_secret)


async def _call(project: Project, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method not in READ_METHODS:
        raise MethodNotAllowed(method)
    token = _token(project)
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(
            f"{API}/{method}", headers={"Authorization": f"Bearer {token}"}, data=params
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
    if not body.get("ok"):
        error = body.get("error", "unknown")
        if error == "ratelimited":
            raise TimeoutError("slack: rate limited")
        raise RuntimeError(f"slack {method}: {error}")
    return body


async def _permalink_base(project: Project) -> str:
    """Workspace URL, needed to cite messages that carry only a timestamp."""
    if project.id not in _team_url:
        body = await _call(project, "auth.test", {})
        _team_url[project.id] = str(body.get("url", "")).rstrip("/")
    return _team_url[project.id]


def _archive_url(base: str, channel: str, ts: str) -> str:
    return f"{base}/archives/{channel}/p{ts.replace('.', '')}"


async def search_slack_messages(query: str, tool_context: ToolContext) -> dict[str, Any]:
    """Search Slack messages across the user's project workspaces.

    Use this to find past discussions and decisions. Returns a snapshot
    envelope; each item's url is the message permalink, and extra carries
    channel/thread identifiers for get_slack_thread.

    Args:
        query: Keywords to search for.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        body = await _call(project, "search.messages", {"query": query, "count": 20})
        items = []
        for m in body.get("messages", {}).get("matches", []):
            channel = m.get("channel", {})
            items.append(
                Item(
                    project=project.id,
                    url=m.get("permalink", ""),
                    title=f"#{channel.get('name', '?')} / {m.get('username', '?')}",
                    body=m.get("text", ""),
                    extra={
                        "channel_id": channel.get("id"),
                        "ts": m.get("ts"),
                        "thread_ts": m.get("thread_ts") or m.get("ts"),
                    },
                )
            )
        return [i for i in items if i.url]

    return (await fan_out(Source.SLACK, projects, fetch)).to_tool_result()


async def get_slack_thread(
    channel_id: str, thread_ts: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Read one Slack thread in full (parent message and all replies).

    Use this after search_slack_messages to reconstruct the context of a
    discussion.

    Args:
        channel_id: Channel id like "C0123456789", from a search result.
        thread_ts: Thread timestamp from a search result.
    """
    projects = registry().projects_for(tool_context.state["project_ids"])

    async def fetch(project: Project) -> list[Item]:
        body = await _call(
            project,
            "conversations.replies",
            {"channel": channel_id, "ts": thread_ts, "limit": 100},
        )
        base = await _permalink_base(project)
        return [
            Item(
                project=project.id,
                url=_archive_url(base, channel_id, m.get("ts", thread_ts)),
                title=f"{m.get('user', '?')} @ {m.get('ts', '')}",
                body=m.get("text", ""),
                extra={"channel_id": channel_id, "ts": m.get("ts")},
            )
            for m in body.get("messages", [])
        ]

    return (await fan_out(Source.SLACK, projects, fetch)).to_tool_result()
