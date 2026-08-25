"""Static checks on the two-agent assembly and the explorer call budget."""

from types import SimpleNamespace
from typing import Any

from google.adk.tools.agent_tool import AgentTool

from agents.wsagent.agent import _MAX_EXPLORER_CALLS, _limit_explorer_calls, root_agent


def _tool_context(invocation_id: str) -> Any:
    return SimpleNamespace(invocation_id=invocation_id, state={})


def _tool() -> Any:
    return SimpleNamespace(name="explorer")


class TestLimitExplorerCalls:
    def test_allows_up_to_budget_then_skips(self) -> None:
        ctx = _tool_context("inv-1")
        for _ in range(_MAX_EXPLORER_CALLS):
            assert _limit_explorer_calls(_tool(), {}, ctx) is None
        result = _limit_explorer_calls(_tool(), {}, ctx)
        assert isinstance(result, dict)
        assert "budget exhausted" in result["error"]

    def test_counter_is_per_invocation(self) -> None:
        ctx = _tool_context("inv-1")
        for _ in range(_MAX_EXPLORER_CALLS):
            _limit_explorer_calls(_tool(), {}, ctx)
        # Same state carried into a new invocation starts a fresh budget.
        other = SimpleNamespace(invocation_id="inv-2", state=ctx.state)
        assert _limit_explorer_calls(_tool(), {}, other) is None


class TestComposition:
    def test_orchestrator_has_only_the_explorer_tool(self) -> None:
        tools = root_agent.inner.tools
        assert len(tools) == 1
        assert isinstance(tools[0], AgentTool)
        assert tools[0].agent.name == "explorer"

    def test_explorer_holds_the_sixteen_retrieval_tools(self) -> None:
        explorer = root_agent.inner.tools[0].agent
        assert len(explorer.tools) == 16
