"""Envelope assembly: cross-project fan-out and token-budget truncation."""

import asyncio
from collections.abc import Awaitable, Callable

from ..config import Project
from ..schemas import Envelope, FailureReason, Item, Source

MAX_ITEM_CHARS = 4_000
MAX_ENVELOPE_CHARS = 24_000
PROJECT_CONCURRENCY = 3

# Raising is allowed: the fan-out turns exceptions into envelope failures, so
# the LLM never sees one.
Fetcher = Callable[[Project], Awaitable[list[Item]]]


async def fan_out(source: Source, projects: list[Project], fetch: Fetcher) -> Envelope:
    """Query each project concurrently and merge into one envelope. A project
    unconfigured for this source fails as not_configured, never silently skipped."""
    env = Envelope(source=source, projects=[p.id for p in projects])
    sem = asyncio.Semaphore(PROJECT_CONCURRENCY)

    async def one(project: Project) -> None:
        async with sem:
            try:
                env.items.extend(await fetch(project))
            except NotConfigured:
                env.add_failure(project.id, FailureReason.NOT_CONFIGURED)
            except TimeoutError:
                env.add_failure(project.id, FailureReason.TIMEOUT)
            except AuthExpired as e:
                env.add_failure(project.id, FailureReason.AUTH_EXPIRED, str(e))
            except Exception as e:  # noqa: BLE001 - boundary: everything becomes a failure
                env.add_failure(project.id, FailureReason.UPSTREAM_ERROR, str(e)[:200])

    await asyncio.gather(*(one(p) for p in projects))
    truncate(env)
    return env


def truncate(env: Envelope) -> None:
    """Cap per-item and whole-envelope body size; truncation is self-declared."""
    truncated = False
    for item in env.items:
        if len(item.body) > MAX_ITEM_CHARS:
            item.body = item.body[:MAX_ITEM_CHARS]
            truncated = True
    total = 0
    kept: list[Item] = []
    for item in env.items:
        total += len(item.body)
        if total > MAX_ENVELOPE_CHARS:
            truncated = True
            break
        kept.append(item)
    env.items[:] = kept
    if truncated:
        env.add_failure("*", FailureReason.TRUNCATED)


class NotConfigured(Exception):
    """The project has no configuration for this source."""


class AuthExpired(Exception):
    """Refresh token invalid."""
