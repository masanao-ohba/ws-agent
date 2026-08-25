"""Data contracts. The envelope is the single return shape of every tool."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Source(StrEnum):
    SLACK = "slack"
    DRIVE = "drive"
    GITHUB = "github"
    BACKLOG = "backlog"
    GMAIL = "gmail"


class FailureReason(StrEnum):
    AUTH_EXPIRED = "auth_expired"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    TRUNCATED = "truncated"
    NOT_CONFIGURED = "not_configured"
    UPSTREAM_ERROR = "upstream_error"


class Failure(BaseModel):
    project: str
    reason: FailureReason
    detail: str = ""


class Item(BaseModel):
    """One result item. `url` is mandatory: answers must cite sources."""

    project: str
    url: str
    title: str
    body: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class Envelope(BaseModel):
    """Uniform snapshot envelope returned by every tool. Incompleteness is
    self-declared: any failure or truncation flips `complete` to False."""

    source: Source
    projects: list[str]
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    complete: bool = True
    failures: list[Failure] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    def add_failure(self, project: str, reason: FailureReason, detail: str = "") -> None:
        self.failures.append(Failure(project=project, reason=reason, detail=detail))
        self.complete = False

    def to_tool_result(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["count"] = self.count
        return d
