"""Envelope assembly: cross-project fan-out and token-budget truncation."""

import asyncio
from collections.abc import Awaitable, Callable

from ..config import Project
from ..schemas import Envelope, FailureReason, Item, Source

MAX_ENVELOPE_CHARS = 60_000
# A per-item floor keeps a large result set from shrinking to useless stubs;
# past that many items the envelope budget is exceeded and the tail is dropped.
MIN_ITEM_CHARS = 1_000
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
    """Share the envelope budget across items; truncation is self-declared.

    The budget is divided by item count rather than capped per item, so a
    single read gets the whole budget while a search shows every candidate
    shortened. Dropping items outright would hide candidates the caller is
    searching for, so that happens only past the per-item floor.
    """
    if not env.items:
        return
    share = max(MAX_ENVELOPE_CHARS // len(env.items), MIN_ITEM_CHARS)
    clipped: list[str] = []
    total = 0
    kept: list[Item] = []
    dropped = 0
    for item in env.items:
        if total + min(len(item.body), share) > MAX_ENVELOPE_CHARS:
            dropped += 1
            continue
        if len(item.body) > share:
            clipped.append(f"{item.url} {share}/{len(item.body)}")
            item.body = item.body[:share]
        total += len(item.body)
        kept.append(item)
    env.items[:] = kept
    if clipped or dropped:
        # Say what was cut and by how much: the caller can only decide whether
        # to fetch the rest if it knows the rest exists. Dropped items lead,
        # since a long list of clipped ones would otherwise crowd them out.
        parts = [f"{dropped} item(s) dropped"] if dropped else []
        parts += clipped[:3]
        if len(clipped) > 3:
            parts.append(f"and {len(clipped) - 3} more shortened")
        env.add_failure("*", FailureReason.TRUNCATED, "; ".join(parts))


class NotConfigured(Exception):
    """The project has no configuration for this source."""


class AuthExpired(Exception):
    """Refresh token invalid."""
