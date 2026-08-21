"""Static contract tests: envelope invariants, registry schema, allowlists."""

import asyncio
from pathlib import Path

import pytest
import yaml

from agents.wsagent.config import Registry
from agents.wsagent.gateway.envelope import (
    MAX_ENVELOPE_CHARS,
    MAX_ITEM_CHARS,
    NotConfigured,
    fan_out,
    truncate,
)
from agents.wsagent.gateway.mcp_client import GITHUB_ALLOWED, McpSession, ToolNotAllowed
from agents.wsagent.schemas import Envelope, FailureReason, Item, Source

CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "projects.yaml"


def _item(body: str = "x") -> Item:
    return Item(project="p1", url="https://example.com", title="t", body=body)


def test_projects_yaml_matches_runtime_schema() -> None:
    Registry.model_validate(yaml.safe_load(CONFIG.read_text()))


def test_envelope_failure_flips_complete() -> None:
    env = Envelope(source=Source.BACKLOG, projects=["p1"])
    assert env.complete
    env.add_failure("p1", FailureReason.TIMEOUT)
    assert not env.complete
    assert env.to_tool_result()["failures"]


def test_truncation_is_self_declared() -> None:
    env = Envelope(source=Source.DRIVE, projects=["p1"])
    env.items = [_item("a" * (MAX_ITEM_CHARS + 1))]
    truncate(env)
    assert len(env.items[0].body) == MAX_ITEM_CHARS
    assert not env.complete
    assert any(f.reason == FailureReason.TRUNCATED for f in env.failures)


def test_envelope_total_budget() -> None:
    env = Envelope(source=Source.DRIVE, projects=["p1"])
    env.items = [_item("a" * MAX_ITEM_CHARS) for _ in range(20)]
    truncate(env)
    assert sum(len(i.body) for i in env.items) <= MAX_ENVELOPE_CHARS


def test_fan_out_converts_exceptions_to_failures() -> None:
    from agents.wsagent.config import Project

    projects = [
        Project(id="ok", name="ok", members=[]),
        Project(id="none", name="none", members=[]),
        Project(id="boom", name="boom", members=[]),
    ]

    async def fetch(project: Project) -> list[Item]:
        if project.id == "ok":
            return [_item()]
        if project.id == "none":
            raise NotConfigured()
        raise RuntimeError("kaboom")

    env = asyncio.run(fan_out(Source.BACKLOG, projects, fetch))
    assert env.count == 1
    reasons = {f.project: f.reason for f in env.failures}
    assert reasons["none"] == FailureReason.NOT_CONFIGURED
    assert reasons["boom"] == FailureReason.UPSTREAM_ERROR
    assert not env.complete


def test_mcp_allowlist_blocks_write_tools() -> None:
    session = McpSession(url="https://example.com", token="t", allowed=GITHUB_ALLOWED, extra_headers={})
    with pytest.raises(ToolNotAllowed):
        session.call_tool("create_issue", {})


def test_github_allowlist_is_read_only_by_name() -> None:
    assert all(
        t.startswith(("get_", "list_", "search_")) or t.endswith("_read") for t in GITHUB_ALLOWED
    )


def test_secret_store_has_no_write_path() -> None:
    """Credentials are static values managed by humans; the runtime only reads
    them. A write method here would let system operation mutate what is
    supposed to be statically managed configuration."""
    from agents.wsagent.gateway.secrets import SecretManagerStore

    assert not hasattr(SecretManagerStore, "set")
    assert not any(
        name.startswith(("set", "add", "write", "update", "rotate"))
        for name in vars(SecretManagerStore)
        if not name.startswith("__")
    )


def test_slack_read_methods_reject_write_methods() -> None:
    import asyncio

    from agents.wsagent.config import Project, SlackConfig
    from agents.wsagent.tools.slack import READ_METHODS, MethodNotAllowed, _call

    assert not any(
        m.startswith(("chat.post", "chat.delete", "chat.update", "conversations.invite"))
        for m in READ_METHODS
    )
    project = Project(
        id="p",
        name="P",
        members=[],
        slack=SlackConfig(user_token_secret="s", team_id="T1"),
    )
    with pytest.raises(MethodNotAllowed):
        asyncio.run(_call(project, "chat.postMessage", {}))
