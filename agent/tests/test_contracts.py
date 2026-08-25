"""Static contract tests: envelope invariants, registry schema, allowlists."""

import asyncio
from pathlib import Path

import pytest
import yaml

from agents.wsagent.config import Registry
from agents.wsagent.gateway.envelope import (
    MAX_ENVELOPE_CHARS,
    MIN_ITEM_CHARS,
    NotConfigured,
    fan_out,
    truncate,
)
from agents.wsagent.gateway.mcp_client import GITHUB_ALLOWED, McpSession, ToolNotAllowed
from agents.wsagent.schemas import Envelope, FailureReason, Item, Source

CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "projects.yaml"
ORG_CONFIG = CONFIG.with_name("projects.organization-personal.yaml")


def _item(body: str = "x") -> Item:
    return Item(project="p1", url="https://example.com", title="t", body=body)


def test_projects_yaml_matches_runtime_schema() -> None:
    Registry.model_validate(yaml.safe_load(CONFIG.read_text()))


@pytest.mark.skipif(not ORG_CONFIG.exists(), reason="env-local registry, not in the repository")
def test_organization_personal_yaml_matches_runtime_schema() -> None:
    raw = yaml.safe_load(ORG_CONFIG.read_text())
    Registry.model_validate(raw)
    # A key the schema does not define is dropped without a word, so config
    # that reads as meaningful would have no effect. anchors are session
    # state, supplied per conversation, never from the registry.
    assert all("anchors" not in project for project in raw["projects"])


def test_envelope_failure_flips_complete() -> None:
    env = Envelope(source=Source.BACKLOG, projects=["p1"])
    assert env.complete
    env.add_failure("p1", FailureReason.TIMEOUT)
    assert not env.complete
    assert env.to_tool_result()["failures"]


def test_truncation_is_self_declared() -> None:
    env = Envelope(source=Source.DRIVE, projects=["p1"])
    env.items = [_item("a" * (MAX_ENVELOPE_CHARS + 1))]
    truncate(env)
    assert len(env.items[0].body) == MAX_ENVELOPE_CHARS
    assert not env.complete
    failure = next(f for f in env.failures if f.reason == FailureReason.TRUNCATED)
    # The caller can only decide whether to fetch the rest if it is told the
    # rest exists: kept/total is part of the declaration.
    assert f"{MAX_ENVELOPE_CHARS}/{MAX_ENVELOPE_CHARS + 1}" in failure.detail


def test_a_single_item_gets_the_whole_budget() -> None:
    """A read returns one item; capping it below the budget wastes the rest."""
    env = Envelope(source=Source.BACKLOG, projects=["p1"])
    env.items = [_item("a" * 7_096)]
    truncate(env)
    assert len(env.items[0].body) == 7_096
    assert env.complete


def test_every_candidate_survives_a_crowded_search() -> None:
    """Shortening all candidates beats showing a few and hiding the rest."""
    env = Envelope(source=Source.BACKLOG, projects=["p1"])
    env.items = [_item("a" * 10_000) for _ in range(20)]
    truncate(env)
    assert len(env.items) == 20
    assert all(len(i.body) == MAX_ENVELOPE_CHARS // 20 for i in env.items)


def test_envelope_total_budget() -> None:
    env = Envelope(source=Source.DRIVE, projects=["p1"])
    env.items = [_item("a" * 10_000) for _ in range(20)]
    truncate(env)
    assert sum(len(i.body) for i in env.items) <= MAX_ENVELOPE_CHARS


def test_items_past_the_floor_are_dropped_and_declared() -> None:
    """Below the floor an item says nothing, so the tail is dropped instead."""
    count = MAX_ENVELOPE_CHARS // MIN_ITEM_CHARS + 10
    env = Envelope(source=Source.DRIVE, projects=["p1"])
    env.items = [_item("a" * 5_000) for _ in range(count)]
    truncate(env)
    assert len(env.items) == MAX_ENVELOPE_CHARS // MIN_ITEM_CHARS
    assert "dropped" in next(
        f for f in env.failures if f.reason == FailureReason.TRUNCATED
    ).detail


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


def test_issue_title_carries_its_status() -> None:
    """Whether an issue records what shipped or what is still under discussion
    decides how it may be used, so it rides in the title, not in extra."""
    from agents.wsagent.config import BacklogConfig, Project
    from agents.wsagent.tools.backlog import _issue_item

    project = Project(
        id="p",
        name="P",
        members=[],
        backlog=BacklogConfig(domain="d.backlog.jp", project_keys=["X"], api_key_secret="s"),
    )
    item = _issue_item(
        project,
        {"issueKey": "X-1", "summary": "件名", "status": {"name": "仕様検討・見積中"}},
    )
    assert item.title == "[X-1][仕様検討・見積中] 件名"
