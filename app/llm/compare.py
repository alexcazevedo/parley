"""Replay a tenant's stored replies through the boundary check on a candidate model.

`uv run python -m app.llm.compare --tenant <id> --model <llm:name|typesafe:name> [--limit N]`

Reads the tenant's newest successful agent runs of agents that have boundaries
and the reply check on, judges each stored reply again on the candidate and
prints where the candidate disagrees with what was recorded. Nothing is
written. The agent's boundaries as they are now are used, so a rule changed
since a run changes the answer. Customer text is printed. Exit code 0 when it
ran, 1 for a bad argument or an unknown tenant.
"""

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from typing import NoReturn

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.agents.boundaries import (
    REASON_CHECK_FAILED,
    REASON_CHECK_UNPARSEABLE,
    Boundaries,
    Verdict,
    check_payload,
    check_reply,
    effective_boundaries,
)
from app.config import get_settings
from app.http_client import close_shared_client
from app.llm.decisions import HandOver, decide_boundary
from app.llm.factory import get_provider, resolve_model
from app.llm.jobs import Job, JobSettingsError, JobTarget, job_target
from app.models.agent import Agent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation
from app.models.tenant import Tenant

logger = structlog.get_logger(__name__)

DEFAULT_LIMIT = 200
REPLY_PREVIEW_CHARS = 120
_FAILURE_REASONS = frozenset({REASON_CHECK_FAILED, REASON_CHECK_UNPARSEABLE})


class CompareError(Exception):
    """The command cannot run; the message says why."""


@dataclass
class CompareResult:
    compared: int = 0
    agree: int = 0
    disagree: int = 0
    skipped: int = 0
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _StoredRun:
    id: uuid.UUID
    agent: Agent
    boundaries: Boundaries
    user_text: str
    reply: str | None
    recorded_reason: str | None
    recorded_blocked: bool


def parse_target(raw: str) -> JobTarget:
    """`llm:<name>` or `typesafe:<name>`; raises `CompareError` otherwise."""
    settings = get_settings().model_copy(update={"boundary_check_model": raw})
    try:
        target = job_target(Job.BOUNDARY_CHECK, settings)
    except JobSettingsError:
        raise CompareError("--model: must be `llm:<model>` or `typesafe:<model>`") from None
    if target.kind == "agent":
        raise CompareError("--model: must be `llm:<model>` or `typesafe:<model>`")
    if target.kind == "typesafe" and not settings.typesafe_api_key:
        raise CompareError("--model: `typesafe:` needs TYPESAFE_API_KEY")
    return target


async def _load(engine: AsyncEngine, tenant_id: uuid.UUID, limit: int) -> list[_StoredRun]:
    """The runs to replay, read in one short session that ends before any model call."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        if await session.get(Tenant, tenant_id) is None:
            raise CompareError(f"unknown tenant {tenant_id}")
        agents = (
            (await session.execute(select(Agent).where(Agent.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        eligible: dict[uuid.UUID, tuple[Agent, Boundaries]] = {}
        for agent in agents:
            boundaries = effective_boundaries(agent)
            if boundaries is not None and boundaries.check_enabled:
                eligible[agent.id] = (agent, boundaries)
        if not eligible:
            return []
        rows = await session.execute(
            select(AgentRun, Conversation.agent_id)
            .join(Conversation, Conversation.id == AgentRun.conversation_id)
            .where(
                AgentRun.tenant_id == tenant_id,
                Conversation.tenant_id == tenant_id,
                Conversation.agent_id.in_(eligible),
                AgentRun.status == AgentRunStatus.SUCCESS,
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id)
            .limit(limit)
        )
        runs = [
            _StoredRun(
                id=run.id,
                agent=eligible[agent_id][0],
                boundaries=eligible[agent_id][1],
                user_text=run.input_text or "",
                reply=run.blocked_text if run.blocked_text is not None else run.output_text,
                recorded_reason=run.boundary_reason,
                recorded_blocked=run.boundary_reason is not None,
            )
            for run, agent_id in rows.all()
        ]
        await session.rollback()
    return runs


async def _candidate(target: JobTarget, run: _StoredRun, reply: str) -> Verdict | None:
    """The candidate's verdict, or None when it gave no decision."""
    if target.kind == "typesafe":
        try:
            return await decide_boundary(
                target, boundaries=run.boundaries, evidence=check_payload(run.user_text, reply)
            )
        except HandOver as handed:
            logger.info("compare.handed_over", why=handed.why)
            return None
        except Exception as exc:
            logger.warning("compare.call_failed", error=type(exc).__name__)
            return None
    assert target.model is not None
    verdict = await check_reply(
        get_provider(run.agent),
        model=target.model or resolve_model(run.agent),
        boundaries=run.boundaries,
        user_text=run.user_text,
        reply_text=reply,
    )
    if not verdict.allowed and verdict.reason in _FAILURE_REASONS:
        return None
    return verdict


def _describe(blocked: bool, reason: str | None) -> str:
    if not blocked:
        return "allowed"
    return f"blocked ({reason})" if reason else "blocked"


async def compare_boundary(
    engine: AsyncEngine,
    tenant_id: uuid.UUID,
    target: JobTarget,
    *,
    limit: int = DEFAULT_LIMIT,
) -> CompareResult:
    """Replay the tenant's stored replies on `target`. Writes nothing."""
    result = CompareResult()
    for run in await _load(engine, tenant_id, limit):
        if run.reply is None or run.recorded_reason in _FAILURE_REASONS:
            result.skipped += 1
            continue
        verdict = await _candidate(target, run, run.reply)
        if verdict is None:
            result.skipped += 1
            continue
        result.compared += 1
        if verdict.allowed == (not run.recorded_blocked):
            result.agree += 1
            continue
        result.disagree += 1
        preview = " ".join(run.reply.split())[:REPLY_PREVIEW_CHARS]
        result.lines.append(
            f"{run.id} recorded: {_describe(run.recorded_blocked, run.recorded_reason)}; "
            f"candidate: {_describe(not verdict.allowed, verdict.reason)}; reply: {preview}"
        )
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CompareError(message)


async def run(argv: list[str], engine: AsyncEngine) -> int:
    """The command on `engine`: prints, returns the exit code."""
    parser = _Parser(prog="python -m app.llm.compare")
    parser.add_argument("--tenant", required=True, help="tenant id")
    parser.add_argument("--model", required=True, help="llm:<name> or typesafe:<name>")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    try:
        args = parser.parse_args(argv)
        try:
            tenant_id = uuid.UUID(args.tenant)
        except ValueError:
            raise CompareError("--tenant: not a tenant id") from None
        if args.limit < 1:
            raise CompareError("--limit: must be at least 1")
        target = parse_target(args.model)
        print(
            f"compare: boundary check on {args.model}, tenant {tenant_id}. "
            "Boundaries are the agents' current ones: a rule changed since a run changes "
            "the answer. Customer text is printed below."
        )
        result = await compare_boundary(engine, tenant_id, target, limit=args.limit)
    except CompareError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 1
    for line in result.lines:
        print(line)
    print(
        f"compared {result.compared} replies: {result.agree} agree, "
        f"{result.disagree} disagree, {result.skipped} skipped"
    )
    return 0


async def _main(argv: list[str]) -> int:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return await run(argv, engine)
    finally:
        await close_shared_client()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
