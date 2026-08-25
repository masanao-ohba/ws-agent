"""Project registry. Source of truth is config/projects.yaml, delivered to the
runtime as the WS_PROJECTS env var (JSON) by scripts/deploy.py.

Resolution is lazy: nothing reads the environment at import time.
Credential values never appear here; only Secret Manager secret names do.
"""

import functools
import json
import os

from pydantic import BaseModel, Field


class BacklogConfig(BaseModel):
    domain: str
    project_keys: list[str]
    api_key_secret: str


class GithubConfig(BaseModel):
    """Either a static PAT or a GitHub App installation (future).

    The PAT is a static secret like every other credential here; the
    App path mints installation tokens instead and is not yet implemented.
    """

    pat_secret: str | None = None
    installation_id: int | None = None
    repos: list[str] = Field(default_factory=list)  # search hint, not a boundary
    # Forks are absent from GitHub's code index: a query there answers zero
    # for any term, which is indistinguishable from "no such code".
    code_search_indexed: bool = True


class GwsConfig(BaseModel):
    refresh_token_secret: str
    drive_roots: list[str] = Field(default_factory=list)  # search hint
    gmail_lists: list[str] = Field(default_factory=list)  # enforced query filter (future)


class SlackConfig(BaseModel):
    # Static user token (xoxp-). Rotation is deliberately not used.
    user_token_secret: str
    team_id: str
    channels: list[str] = Field(default_factory=list)  # search hint


class Project(BaseModel):
    id: str
    name: str
    members: list[str]
    backlog: BacklogConfig | None = None
    github: GithubConfig | None = None
    gws: GwsConfig | None = None
    slack: SlackConfig | None = None


class GithubApp(BaseModel):
    app_id: int
    private_key_secret: str


class Registry(BaseModel):
    projects: list[Project]
    github_app: GithubApp | None = None

    def get(self, project_id: str) -> Project:
        for p in self.projects:
            if p.id == project_id:
                return p
        raise KeyError(f"unknown project: {project_id}")

    def projects_for(self, project_ids: list[str]) -> list[Project]:
        return [self.get(pid) for pid in project_ids]


@functools.cache
def registry() -> Registry:
    raw = os.environ.get("WS_PROJECTS")
    if not raw:
        raise RuntimeError(
            "WS_PROJECTS is not set. It is rendered from config/projects.yaml by scripts/deploy.py"
        )
    return Registry.model_validate(json.loads(raw))
