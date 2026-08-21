"""Minimal Streamable-HTTP MCP client used by the gateway (GitHub).

MCP here is a data-fetch protocol, not an LLM-facing toolset: the allowlists
below are the single read-only gate. A tool name outside its service allowlist
cannot be called — the guard is code, not convention. verify_allowlists()
lets a deploy-time smoke test catch a server-side rename loudly instead of
silently returning nothing.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"

# The only downstream MCP tools this gateway may invoke. Read-only by name.
GITHUB_ALLOWED: frozenset[str] = frozenset(
    {
        "search_code",
        "search_issues",
        "search_pull_requests",
        "search_repositories",
        "issue_read",
        "pull_request_read",
        "get_file_contents",
        "list_commits",
        "list_issues",
        "list_pull_requests",
    }
)
GITHUB_URL = "https://api.githubcopilot.com/mcp/readonly"
GITHUB_TOOLSETS = "repos,issues,pull_requests"

# Slack is not reached over MCP: the official server exposes three
# channel-scoped tools and no search, so tools/slack.py
# calls the Web API directly.


class McpError(Exception):
    pass


class ToolNotAllowed(McpError):
    pass


@dataclass
class McpSession:
    """One short-lived session: initialize on first call, no local state kept
    beyond the server-issued session id. 401 handling (token refresh + single
    retry) lives in the caller, which owns the TokenProvider."""

    url: str
    token: str
    allowed: frozenset[str]
    extra_headers: dict[str, str]
    _session_id: str | None = None

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.allowed:
            raise ToolNotAllowed(f"{name} is not in the read-only allowlist for {self.url}")
        self._ensure_initialized()
        result = self._rpc({"method": "tools/call", "params": {"name": name, "arguments": arguments}})
        if result.get("isError"):
            text = _first_text(result)
            raise McpError(f"{name}: {text[:200]}")
        return result

    def list_tools(self) -> list[str]:
        self._ensure_initialized()
        result = self._rpc({"method": "tools/list"})
        return [t["name"] for t in result["tools"]]

    def _ensure_initialized(self) -> None:
        if self._session_id is not None:
            return
        self._rpc(
            {
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ws-agent", "version": "0.1"},
                },
            }
        )
        self._notify({"method": "notifications/initialized"})

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        } | self.extra_headers
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        resp = httpx.post(self.url, json=payload, headers=self._headers(), timeout=25)
        if sid := resp.headers.get("Mcp-Session-Id"):
            self._session_id = sid
        return resp

    def _rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._post({"jsonrpc": "2.0", "id": 1} | payload)
        if resp.status_code == 401:
            raise McpError("unauthorized")
        resp.raise_for_status()
        data = _parse_jsonrpc(resp.text)
        if "error" in data:
            raise McpError(str(data["error"]))
        return dict(data["result"])

    def _notify(self, payload: dict[str, Any]) -> None:
        self._post({"jsonrpc": "2.0"} | payload)


def _parse_jsonrpc(body: str) -> dict[str, Any]:
    """Server may answer as plain JSON or as an SSE 'data:' frame."""
    stripped = body.strip()
    if stripped.startswith("{"):
        return dict(json.loads(stripped))
    data: dict[str, Any] | None = None
    for line in stripped.splitlines():
        if line.startswith("data: "):
            data = json.loads(line[6:])
    if data is None:
        raise McpError(f"unparseable MCP response: {stripped[:120]}")
    return data


def _first_text(result: dict[str, Any]) -> str:
    content = result.get("content") or []
    return str(content[0].get("text", "")) if content else ""


def verify_allowlists(sessions: list[McpSession]) -> None:
    """Deploy smoke test: every allowlisted tool must exist server-side."""
    for s in sessions:
        missing = s.allowed - set(s.list_tools())
        if missing:
            raise McpError(f"{s.url}: allowlisted tools missing on server: {sorted(missing)}")
