"""Path search: the substitute for code search where no index exists.

Fixtures mirror the git tree API response shape. Nothing here touches the
network: the unindexed path never reaches an HTTP call.
"""

import asyncio
from typing import Any

import pytest

from agents.wsagent.config import GithubConfig, Project, Registry
from agents.wsagent.schemas import FailureReason
from agents.wsagent.tools import github
from agents.wsagent.tools.github import (
    _MAX_PATHS_PER_REPO,
    _MAX_TREE_ENTRIES,
    _entries_at,
    _match_paths,
    search_github,
)


def tree(*entries: tuple[str, str], truncated: bool = False) -> dict[str, Any]:
    return {
        "sha": "0000000",
        "truncated": truncated,
        "tree": [{"path": path, "type": kind, "sha": "x"} for path, kind in entries],
    }


class _Context:
    """Only `state` is read by the tools; ToolContext itself needs a runner."""

    def __init__(self, project_ids: list[str]) -> None:
        self.state: dict[str, Any] = {"project_ids": project_ids}


def test_code_search_indexed_defaults_to_true() -> None:
    """Absent configuration means the ordinary case: GitHub indexes the repo."""
    assert GithubConfig().code_search_indexed is True


def test_unindexed_project_declares_code_search_as_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project(
        id="p",
        name="P",
        members=[],
        github=GithubConfig(pat_secret="s", repos=["acme/fork"], code_search_indexed=False),
    )
    monkeypatch.setattr(github, "registry", lambda: Registry(projects=[project]))

    result = asyncio.run(search_github("Mail", "code", _Context(["p"])))  # type: ignore[arg-type]

    assert result["count"] == 0
    assert not result["complete"]
    assert result["failures"] == [
        {"project": "p", "reason": FailureReason.NOT_CONFIGURED, "detail": ""}
    ]


def test_unindexed_project_still_searches_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the code index is missing; issues and PRs are searched as usual."""
    project = Project(
        id="p",
        name="P",
        members=[],
        github=GithubConfig(pat_secret="s", repos=["acme/fork"], code_search_indexed=False),
    )
    monkeypatch.setattr(github, "registry", lambda: Registry(projects=[project]))

    def no_network(project: Project) -> None:
        raise TimeoutError()

    monkeypatch.setattr(github, "_session", no_network)

    result = asyncio.run(search_github("Mail", "issues", _Context(["p"])))  # type: ignore[arg-type]

    assert [f["reason"] for f in result["failures"]] == [FailureReason.TIMEOUT]


def test_match_paths_is_case_insensitive_and_partial() -> None:
    found, _ = _match_paths(
        tree(
            ("app/Models/MailLink.php", "blob"),
            ("app/Services/maillink_sender.php", "blob"),
            ("app/Models/User.php", "blob"),
        ),
        "maillink",
    )
    assert found == ["app/Models/MailLink.php", "app/Services/maillink_sender.php"]


def test_match_paths_keeps_blobs_only() -> None:
    """Directories carry the query in their name too, but cannot be read."""
    found, _ = _match_paths(
        tree(("app/MailLink", "tree"), ("app/MailLink/Sender.php", "blob")), "maillink"
    )
    assert found == ["app/MailLink/Sender.php"]


def test_match_paths_caps_per_repo_and_says_so() -> None:
    over = _MAX_PATHS_PER_REPO + 1
    found, incomplete = _match_paths(
        tree(*((f"app/Mail{i}.php", "blob") for i in range(over))), "mail"
    )
    assert len(found) == _MAX_PATHS_PER_REPO
    assert incomplete


def test_match_paths_reports_a_tree_github_cut_short() -> None:
    """A truncated tree hides paths that would have matched."""
    _, incomplete = _match_paths(tree(("app/Mail.php", "blob"), truncated=True), "mail")
    assert incomplete


def test_match_paths_within_the_cap_is_complete() -> None:
    found, incomplete = _match_paths(tree(("app/Mail.php", "blob")), "mail")
    assert found == ["app/Mail.php"]
    assert not incomplete


def test_entries_at_root_lists_only_the_top_level() -> None:
    """Deeper entries are in the same flat list; only the top level is asked for."""
    found, _ = _entries_at(
        tree(
            ("README.md", "blob"),
            ("src", "tree"),
            ("src/Model", "tree"),
            ("src/Model/Table/UsersTable.php", "blob"),
        ),
        "",
    )
    assert found == [("src", "tree"), ("README.md", "blob")]


def test_entries_at_a_middle_path_lists_its_children_only() -> None:
    found, _ = _entries_at(
        tree(
            ("src", "tree"),
            ("src/Model", "tree"),
            ("src/Model/Entity", "tree"),
            ("src/Model/Table", "tree"),
            ("src/Model/Table/UsersTable.php", "blob"),
            ("src/Controller", "tree"),
        ),
        "src/Model",
    )
    assert found == [("src/Model/Entity", "tree"), ("src/Model/Table", "tree")]


def test_entries_at_excludes_directories_that_are_not_direct_children() -> None:
    """A recursive tree carries every intermediate directory as type=tree."""
    found, _ = _entries_at(
        tree(("src", "tree"), ("src/Model", "tree"), ("src/Model/Table", "tree")), ""
    )
    assert found == [("src", "tree")]


def test_entries_at_puts_directories_before_files() -> None:
    found, _ = _entries_at(
        tree(("a.php", "blob"), ("b", "tree"), ("c.php", "blob"), ("d", "tree")), ""
    )
    assert found == [("b", "tree"), ("d", "tree"), ("a.php", "blob"), ("c.php", "blob")]


def test_entries_at_caps_a_wide_directory_and_says_so() -> None:
    over = _MAX_TREE_ENTRIES + 1
    found, incomplete = _entries_at(
        tree(*((f"src/File{i}.php", "blob") for i in range(over))), "src"
    )
    assert len(found) == _MAX_TREE_ENTRIES
    assert incomplete


def test_entries_at_reports_a_tree_github_cut_short() -> None:
    _, incomplete = _entries_at(tree(("src", "tree"), truncated=True), "")
    assert incomplete


def test_entries_at_ignores_a_trailing_slash() -> None:
    entries = tree(("src", "tree"), ("src/Model", "tree"), ("src/App.php", "blob"))
    assert _entries_at(entries, "src") == _entries_at(entries, "src/")
    assert _entries_at(entries, "src")[0] == [("src/Model", "tree"), ("src/App.php", "blob")]
