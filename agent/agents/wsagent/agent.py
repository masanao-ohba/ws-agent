"""Agent assembly only. No business logic, no HTTP, no project resolution here."""

import logging
import os
from functools import cached_property
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.google_llm import Gemini
from google.adk.planners import BuiltInPlanner
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types

from .prompts.system import SYSTEM_PROMPT
from .tools.backlog import get_backlog_issue, search_backlog_issues
from .tools.github import (
    get_github_item,
    list_github_commits,
    read_github_file,
    search_github,
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
    """System prompt plus registry- and session-provided reference data."""
    from .config import registry

    prompt = SYSTEM_PROMPT
    anchors = ctx.state.get("anchors") or []
    if anchors:
        lines = "\n".join(
            f"- {a.get('name', '')}: {a.get('url', '')}" for a in anchors if a.get("url")
        )
        prompt += f"\nKey records the user has pinned as reference points:\n{lines}\n"
    projects = registry().projects_for(ctx.state.get("project_ids") or [])
    repos = [r for p in projects if p.github for r in p.github.repos]
    if repos:
        lines = "\n".join(f"- {r}" for r in repos)
        prompt += f"\nGitHub repositories configured for these projects:\n{lines}\n"
    return prompt


root_agent = Agent(
    name="wsagent",
    model=_GlobalEndpointGemini(model="gemini-2.5-flash"),
    planner=BuiltInPlanner(
        thinking_config=genai_types.ThinkingConfig(thinking_budget=-1)
    ),
    # Default temperature (1.0) makes retrieval trajectories diverge run to
    # run; 0.5 keeps them consistent.
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.5),
    description="Read-only cross-source workspace information agent",
    instruction=_instruction,
    tools=[
        search_drive_files,
        read_drive_document,
        search_backlog_issues,
        get_backlog_issue,
        search_slack_messages,
        get_slack_thread,
        search_github,
        get_github_item,
        list_github_commits,
        read_github_file,
    ],
    before_tool_callback=_strip_model_supplied_identifiers,
    after_tool_callback=_log_tool_result,
)
