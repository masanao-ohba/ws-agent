"""Agent assembly only. No business logic, no HTTP, no project resolution here."""

import logging
import os
from collections.abc import AsyncGenerator
from functools import cached_property
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event
from google.adk.models.google_llm import Gemini
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types

from .prompts.system import EXPLORER_PROMPT, ORCHESTRATOR_PROMPT
from .tools.backlog import (
    get_backlog_document,
    get_backlog_issue,
    get_backlog_wiki,
    search_backlog_documents,
    search_backlog_issues,
    search_backlog_wikis,
)
from .tools.github import (
    get_github_item,
    list_github_commits,
    list_github_tree,
    read_github_file,
    search_github,
    search_github_paths,
)
from .tools.gws import read_drive_document, search_drive_files
from .tools.slack import get_slack_thread, search_slack_messages

logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO. Backlog's only auth mechanism is an
# apiKey query parameter, so those lines would put the credential into
# Cloud Logging in plain text. WARNING and above only.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Identifiers the model must never supply; authoritative values live in state.
_STATE_ONLY_ARGS = frozenset({"project_id", "project_ids", "user_email"})


def _strip_model_supplied_identifiers(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> None:
    for key in _STATE_ONLY_ARGS & args.keys():
        value = args.pop(key)
        logger.warning(
            "stripped model-supplied %s from %s (len=%d)", key, tool.name, len(str(value))
        )


def _log_tool_result(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: Any
) -> None:
    # Coverage evidence for evaluating retrieval quality: which tools ran,
    # with what query, and how much came back. Values only — no item content.
    if isinstance(tool_response, dict):
        summary = "count=%s complete=%s failures=%s" % (
            tool_response.get("count"),
            tool_response.get("complete"),
            [(f.get("project"), f.get("reason")) for f in tool_response.get("failures", [])],
        )
    else:
        summary = f"non-envelope:{type(tool_response).__name__}"
    logger.info("tool=%s args=%s %s", tool.name, args, summary)


class _GlobalEndpointGemini(Gemini):
    """Gemini whose inference calls go to the global endpoint.

    Regional on-demand capacity (DSQ) can momentarily exhaust into a 429
    RESOURCE_EXHAUSTED, while the global endpoint routes to wherever capacity
    exists. Only the model client changes; sessions and
    the engine stay regional.
    """

    @cached_property
    def api_client(self):
        from google.genai import Client

        return Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location="global",
            http_options=genai_types.HttpOptions(
                headers=self._tracking_headers(),
                retry_options=self.retry_options,
            ),
        )


def _instruction(ctx: ReadonlyContext) -> str:
    """System prompt plus session-provided reference data."""
    prompt = ORCHESTRATOR_PROMPT
    anchors = ctx.state.get("anchors") or []
    if anchors:
        lines = "\n".join(
            f"- {a.get('name', '')}: {a.get('url', '')}" for a in anchors if a.get("url")
        )
        prompt += f"\nKey records the user has pinned as reference points:\n{lines}\n"
    return prompt


def _explorer_instruction(ctx: ReadonlyContext) -> str:
    """Explorer prompt plus registry-provided reference data."""
    from .config import registry

    prompt = EXPLORER_PROMPT
    projects = registry().projects_for(ctx.state.get("project_ids") or [])
    repos = [r for p in projects if p.github for r in p.github.repos]
    if repos:
        lines = "\n".join(f"- {r}" for r in repos)
        prompt += f"\nGitHub repositories configured for these projects:\n{lines}\n"
    return prompt


# One user turn gets at most this many explorer assignments; past that the
# orchestrator must answer from what it already holds.
_MAX_EXPLORER_CALLS = 3


def _limit_explorer_calls(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    # The counter key carries the invocation id, so each user turn starts at
    # zero without any reset step, and the temp: prefix keeps the spent
    # counters out of the persisted session state.
    key = f"temp:explorer_calls:{tool_context.invocation_id}"
    calls = tool_context.state.get(key, 0)
    if calls >= _MAX_EXPLORER_CALLS:
        logger.warning("explorer call budget exhausted; skipping %s", tool.name)
        return {"error": "assignment budget exhausted; answer from the material already gathered"}
    tool_context.state[key] = calls + 1
    return None


class _RetryPlanOnlyAgent(BaseAgent):
    """Re-run once when a turn ends without a single tool call.

    The model sometimes answers a question by narrating its search plan and
    stopping — finish_reason STOP, zero tool calls — which the runtime counts
    as a completed turn. Such a turn holds no retrieved evidence, so it is
    re-run once; the second attempt almost always executes the plan it just
    wrote. Retrieval-grounded turns pass through untouched.
    """

    inner: Agent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        for attempt in range(2):
            # Events are held back until the first tool call: a plan-only
            # turn is a promise, not an answer, and its retry replaces it
            # entirely. From the first call on, everything streams through.
            held: list[Event] = []
            called_tool = False
            async for event in self.inner.run_async(ctx):
                if not called_tool and event.get_function_calls():
                    called_tool = True
                    for h in held:
                        yield h
                    held.clear()
                if called_tool or attempt == 1:
                    yield event
                else:
                    held.append(event)
            if called_tool or attempt == 1:
                return
            logger.warning("turn ended with no tool call; retrying once")


_explorer_agent = Agent(
    name="explorer",
    model=_GlobalEndpointGemini(model="gemini-2.5-flash"),
    planner=BuiltInPlanner(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=-1)
    ),
    # Default temperature (1.0) makes retrieval trajectories diverge run to
    # run; 0.5 keeps them consistent.
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.5),
    description=(
        "Executes one retrieval assignment across Backlog, Google Workspace,"
        " Slack and GitHub and reports the material found"
    ),
    instruction=_explorer_instruction,
    tools=[
        search_drive_files,
        read_drive_document,
        search_backlog_issues,
        get_backlog_issue,
        search_backlog_wikis,
        get_backlog_wiki,
        search_backlog_documents,
        get_backlog_document,
        search_slack_messages,
        get_slack_thread,
        search_github,
        search_github_paths,
        list_github_tree,
        get_github_item,
        list_github_commits,
        read_github_file,
    ],
    before_tool_callback=_strip_model_supplied_identifiers,
    after_tool_callback=_log_tool_result,
)

_orchestrator_agent = Agent(
    name="wsagent_llm",
    model=_GlobalEndpointGemini(model="gemini-2.5-flash"),
    # -1 (dynamic) lets the model skip thinking, and measurement showed it
    # always does — while judging whether material matches the question is
    # exactly the step that needs it. A fixed budget forces the deliberation.
    planner=BuiltInPlanner(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=8192)
    ),
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.5),
    description="Read-only cross-source workspace information agent",
    instruction=_instruction,
    tools=[AgentTool(agent=_explorer_agent)],
    before_tool_callback=_limit_explorer_calls,
)

root_agent = _RetryPlanOnlyAgent(name="wsagent", inner=_orchestrator_agent, sub_agents=[])
