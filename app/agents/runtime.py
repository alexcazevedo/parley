"""The agent loop: context -> LLM -> tools -> reply.

One `run_turn` call is one `AgentRun` row. The loop alternates model calls and
tool executions until the model answers with text or we hit
`agent.max_tool_iterations`. Everything it persists goes through the session
the turn owns.

No transaction is held across a model call (PRODUCT.md E3.6): the `AgentRun`
row is committed, status `RUNNING`, before the loop starts, and
`end_transaction` runs right before every `provider.complete` and before the
reply check. Whatever the turn wrote up to that point is therefore committed
and survives a later failure of the turn. `run_turn` also commits the run's
final status, output and counters (with the last step's tool messages) before
it returns, so a turn that rolls back afterwards — a lock lost before the
send — never leaves the run `RUNNING`.

The time limit is a deadline, not a timeout around the loop: only the model
calls run inside a timeout scope (for the time left), and an expired deadline
raises `TimeoutError` before the next model step or tool call. Database calls
and tool executions are never cancelled half-way, so a timed-out turn leaves
the session usable.

`message_ids` are the inbound messages the turn answers. They are stored on
the `AgentRun` and handed to the tools through `ToolContext`, so a re-run of
the same batch reuses the tool calls an earlier attempt already made (see
app/tools/ledger.py `find_prior`).
"""

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.boundaries import (
    REASON_CHECK_FAILED,
    Boundaries,
    Verdict,
    check_payload,
    check_reply,
    effective_boundaries,
)
from app.agents.context import KNOWLEDGE_PROMPT_TITLES, ContextBuilder
from app.agents.loading import load_conversation_agent
from app.config import Settings, get_settings
from app.conversations.sessions import current_session, session_first_seq
from app.db import end_transaction
from app.llm.base import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, LLMMessage, LLMProvider, ToolCall
from app.llm.decisions import HandOver, decide_boundary, target_label
from app.llm.factory import PROVIDER_OPENAI_COMPATIBLE, get_provider, resolve_model
from app.llm.jobs import (
    TYPED_KINDS,
    CallTimer,
    Job,
    JobTarget,
    failure_outcome,
    job_model,
    job_target,
    record_job_call,
)
from app.media.storage import get_media_storage
from app.memory.service import MemoryService
from app.models.agent import DEFAULT_HANDOFF_MESSAGE, Agent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.channel import Channel
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.message import Message, MessageDirection, MessageRole
from app.repositories.knowledge import KnowledgeItemsRepository
from app.tools.base import ToolContext
from app.tools.context import load_call_context
from app.tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)

# Kept as module constants rather than agent columns: they are product copy, not
# per-tenant configuration, and adding columns is outside this ticket.
MAX_ITERATIONS_MESSAGE = "Desculpe, não consegui concluir. Vou encaminhar."
ERROR_MESSAGE = "Desculpe, tive um problema técnico. Pode tentar novamente?"
EMPTY_REPLY_MESSAGE = "Desculpe, não entendi. Pode reformular?"

# `AgentRunStatus` has no `max_iterations` member and its Postgres enum lives in
# a migration this ticket must not touch, so the capped run is recorded as
# ERROR carrying this marker. Promote it to a real status when the enum moves.
MAX_ITERATIONS_ERROR = "max_iterations"

TURN_TIMEOUT_SECONDS = 60

# Appended to the system prompt when the conversation's channel has
# `config.humanize` on: the splitter in app/conversations/humanize.py cuts on
# blank lines, so we ask the model to produce them.
HUMANIZE_STYLE_HINT = (
    "Style: write like a human chat agent — short messages, split distinct thoughts "
    "into separate paragraphs with a blank line between them; at most a few sentences "
    "per paragraph."
)


async def _load_message_image(message: Message) -> bytes | None:
    """`ContextBuilder`'s image loader: the bytes ingest stored for a message."""
    if not message.media_key:
        return None
    return await get_media_storage().get(message.media_key)


def _time_left(deadline: float) -> float:
    """Seconds until `deadline` (a `time.perf_counter()` value).

    Raises `TimeoutError` once it has passed, so the loop stops before its next
    model step or tool call instead of being cancelled in the middle of one.
    """
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError("agent turn exceeded its time limit")
    return remaining


class AgentRuntime:
    """A `SessionAwareTurnRuntime` (see app/conversations/turn.py).

    `provider` is normally left None so each agent gets its own provider from
    `app.llm.factory`; tests inject a `FakeLLMProvider` instead.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider | None = None,
        registry: ToolRegistry | None = None,
        settings: Settings | None = None,
        context_builder: ContextBuilder | None = None,
        timeout_seconds: float = TURN_TIMEOUT_SECONDS,
    ) -> None:
        self._provider = provider
        self._registry = registry or ToolRegistry()
        self._settings = settings
        # Left None on purpose: the default builder is created per turn so it
        # picks up the current `llm_vision_enabled`/`vision_max_images` and the
        # media root in force at that moment.
        self._context_builder = context_builder
        self._timeout_seconds = timeout_seconds

    async def run_turn(
        self,
        session: AsyncSession,
        conversation: Conversation,
        input_text: str,
        *,
        message_ids: Sequence[uuid.UUID] = (),
    ) -> str:
        # Tenant-scoped: another tenant's agent is as unavailable as a missing one.
        agent = await load_conversation_agent(session, conversation)
        if agent is None:
            raise RuntimeError(
                f"conversation {conversation.id} has no agent available in its tenant"
            )
        # Read now: the commit below expires the agent, and a lazy load fails.
        error_text = agent.error_message or ERROR_MESSAGE

        run = AgentRun(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            status=AgentRunStatus.RUNNING,
            input_text=input_text,
            input_message_ids=[str(message_id) for message_id in message_ids] or None,
        )
        session.add(run)
        # Committed up front: the run is on record from the first model call
        # on, whatever happens to the turn afterwards.
        await session.commit()

        log_tokens = structlog.contextvars.bind_contextvars(run_id=str(run.id))
        try:
            started = time.perf_counter()
            deadline = started + self._timeout_seconds
            try:
                output = await self._loop(
                    session, agent, conversation, run, input_text, deadline, tuple(message_ids)
                )
            except Exception as exc:  # a failed turn still owes the user a reply
                run.status = AgentRunStatus.ERROR
                run.error = f"{type(exc).__name__}: {exc}"[:2000]
                output = error_text
                logger.exception(
                    "agent.run_failed",
                    conversation_id=str(conversation.id),
                    agent_run_id=str(run.id),
                )

            run.output_text = output
            run.latency_ms = int((time.perf_counter() - started) * 1000)
            # Committed here, not left for the turn: a turn that rolls back next
            # (lock lost before its send) must not leave the run `RUNNING`.
            await session.commit()
            logger.info(
                "agent.run_finished",
                conversation_id=str(conversation.id),
                agent_run_id=str(run.id),
                status=str(run.status),
                llm_calls=run.llm_calls,
                tool_calls=run.tool_calls,
                latency_ms=run.latency_ms,
            )
            return output
        finally:
            try:
                structlog.contextvars.reset_contextvars(**log_tokens)
            except Exception:  # never mask the run's outcome
                pass

    async def _loop(
        self,
        session: AsyncSession,
        agent: Agent,
        conversation: Conversation,
        run: AgentRun,
        input_text: str,
        deadline: float,
        message_ids: tuple[uuid.UUID, ...] = (),
    ) -> str:
        provider = self._provider or get_provider(agent, self._settings)
        model = resolve_model(agent, self._settings)
        run.provider = agent.provider or PROVIDER_OPENAI_COMPATIBLE
        run.model = model
        tool_specs = await self._registry.specs(agent, session)
        memories = await self._load_memories(session, agent, conversation)
        history = await self._recent_messages(session, agent, conversation)
        builder = self._context_builder or self._default_context_builder()
        # Passed only with the flag on, so a flag-off prompt is built exactly as before.
        knowledge: dict[str, Any] = {}
        if agent.session_memory_enabled and agent.memory_enabled:
            knowledge["recaps"] = await MemoryService(session).get_recap_lines(
                conversation,
                limit=agent.session_recap_limit,
                max_age_days=agent.session_recap_max_age_days,
            )
        if agent.knowledge_enabled:
            knowledge["knowledge_titles"] = await KnowledgeItemsRepository(session).titles(
                conversation.tenant_id, KNOWLEDGE_PROMPT_TITLES
            )
        messages = await builder.build(
            agent,
            conversation,
            history,
            memories,
            tool_specs,
            style_hint=await self._style_hint(session, conversation),
            **knowledge,
        )
        if not any(message.role == ROLE_USER for message in messages):
            # The batch isn't visible on this session (or was pruned); the model
            # still needs the turn's input.
            messages.append(LLMMessage(role=ROLE_USER, content=input_text))

        prompt_tokens = 0
        completion_tokens = 0
        ctx = ToolContext(
            session=session,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            contact_id=conversation.contact_id,
            agent=agent,
            agent_run_id=run.id,
            message_ids=message_ids,
            call=await load_call_context(session, conversation.tenant_id, conversation.id),
        )

        for _ in range(max(1, agent.max_tool_iterations or 1)):
            _time_left(deadline)  # raises once the deadline has passed
            await end_transaction(session)
            response = await asyncio.wait_for(
                provider.complete(
                    messages,
                    tool_specs,
                    model=model,
                    temperature=agent.temperature,
                    max_tokens=agent.max_tokens,
                ),
                timeout=_time_left(deadline),
            )
            run.llm_calls += 1
            if response.usage is not None:
                prompt_tokens += response.usage.prompt_tokens
                completion_tokens += response.usage.completion_tokens
                run.prompt_tokens = prompt_tokens
                run.completion_tokens = completion_tokens

            if not response.tool_calls:
                reply = (response.content or "").strip()
                if not reply:
                    run.status = AgentRunStatus.SUCCESS
                    return EMPTY_REPLY_MESSAGE
                reply = await self._enforce_boundaries(
                    session, provider, model, agent, conversation, run, input_text, reply, deadline
                )
                run.status = AgentRunStatus.SUCCESS
                return reply

            messages.append(
                LLMMessage(
                    role=ROLE_ASSISTANT, content=response.content, tool_calls=response.tool_calls
                )
            )
            # Built now (its `created_at` precedes the results') but added only
            # once every call of this step has returned: a tool may commit, and
            # a committed tool_call row without its results would make the next
            # turn's prompt invalid.
            step_rows = [self._tool_call_message(conversation, run, response.tool_calls)]

            terminal_text: str | None = None
            for call in response.tool_calls:
                _time_left(deadline)
                # No timeout scope: a tool is never cut off half-way (HTTP
                # tools carry their own timeout).
                result = await self._registry.execute(call.name, call.arguments, ctx)
                run.tool_calls += 1
                content = result.to_content()
                logger.info(
                    "agent.tool_executed",
                    conversation_id=str(conversation.id),
                    tool=call.name,
                    ok=result.ok,
                )
                step_rows.append(self._tool_result_message(conversation, run, call, content))
                messages.append(LLMMessage(role=ROLE_TOOL, content=content, tool_call_id=call.id))
                # A tool (e.g. `handoff_to_human`) can end the loop on the spot
                # instead of handing control back to the model — see
                # app/tools/builtin.py and app/tools/base.py's `ToolResult`.
                # An empty `final_text` (an agent without a handoff message)
                # falls back to the default handoff text.
                if result.ok and result.meta.get("terminal"):
                    terminal_text = result.meta.get("final_text") or DEFAULT_HANDOFF_MESSAGE
            session.add_all(step_rows)
            await session.flush()

            if terminal_text is not None:
                run.status = AgentRunStatus.SUCCESS
                return terminal_text

        run.status = AgentRunStatus.ERROR
        run.error = MAX_ITERATIONS_ERROR
        logger.warning(
            "agent.max_iterations",
            conversation_id=str(conversation.id),
            max_tool_iterations=agent.max_tool_iterations,
        )
        return MAX_ITERATIONS_MESSAGE

    async def _enforce_boundaries(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        model: str,
        agent: Agent,
        conversation: Conversation,
        run: AgentRun,
        input_text: str,
        reply: str,
        deadline: float,
    ) -> str:
        """The reply to send: `reply` itself, or the refusal if the check blocks it.

        Only model-written final replies come through here; canned messages and
        a tool's terminal text are not checked. No boundaries, or the check
        switched off, means no extra model call.
        """
        boundaries = effective_boundaries(agent, self._settings)
        if boundaries is None or not boundaries.check_enabled:
            return reply
        _time_left(deadline)
        await end_transaction(session)
        # `check_reply` makes only the model call (no database work) and has a
        # timeout of its own; the turn's deadline bounds it as well.
        target = job_target(Job.BOUNDARY_CHECK, self._settings)
        check_model = job_model(Job.BOUNDARY_CHECK, agent, self._settings)
        called = CallTimer()
        try:
            if target.kind in TYPED_KINDS:
                check = self._decide_check(
                    provider, target, check_model, boundaries, input_text, reply
                )
            else:
                check = check_reply(
                    provider,
                    model=check_model,
                    boundaries=boundaries,
                    user_text=input_text,
                    reply_text=reply,
                )
            verdict = await asyncio.wait_for(check, timeout=_time_left(deadline))
        except Exception as exc:
            # The turn's deadline cut the check off: recorded, then the turn fails.
            recorded = target_label(target) if target.kind in TYPED_KINDS else check_model
            await self._record_check(
                session, conversation, run, recorded, failure_outcome(exc), called.elapsed_ms()
            )
            raise
        await record_job_call(
            session,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            agent_run_id=run.id,
            job=Job.BOUNDARY_CHECK,
            model=verdict.model or check_model,
            outcome=verdict.outcome,
            prompt_tokens=verdict.prompt_tokens,
            completion_tokens=verdict.completion_tokens,
            latency_ms=verdict.latency_ms,
            handed_over=verdict.handed_over,
        )
        run.llm_calls += 1
        if verdict.allowed:
            return reply
        run.blocked_text = reply
        run.boundary_reason = verdict.reason
        logger.warning(
            "boundary.reply_blocked",
            conversation_id=str(conversation.id),
            agent_run_id=str(run.id),
            reason=verdict.reason,
        )
        return boundaries.refusal_message

    async def _decide_check(
        self,
        provider: LLMProvider,
        target: JobTarget,
        check_model: str,
        boundaries: Boundaries,
        user_text: str,
        reply: str,
    ) -> Verdict:
        """The typed check (a decision model or a chat model); what a decision model
        cannot take goes to `check_model` through today's text path (a chat model
        hands nothing over). One timeout covers both. Never raises: an error or a
        timeout blocks with `check_failed`, and is not handed over."""
        label = target_label(target)
        called = CallTimer()
        try:
            async with asyncio.timeout(get_settings().boundary_check_timeout_seconds):
                try:
                    verdict = await decide_boundary(
                        target, boundaries=boundaries, evidence=check_payload(user_text, reply)
                    )
                except HandOver as handed:
                    logger.info("boundary.check_handed_over", why=handed.why)
                    verdict = await check_reply(
                        provider,
                        model=check_model,
                        boundaries=boundaries,
                        user_text=user_text,
                        reply_text=reply,
                    )
                    return replace(
                        verdict,
                        model=check_model,
                        handed_over=True,
                        latency_ms=called.elapsed_ms(),
                    )
        except Exception as exc:
            logger.warning("boundary.check_failed", error=type(exc).__name__, model=label)
            return Verdict(
                allowed=False,
                reason=REASON_CHECK_FAILED,
                outcome=failure_outcome(exc),
                latency_ms=called.elapsed_ms(),
                model=label,
            )
        return replace(verdict, model=label, latency_ms=called.elapsed_ms())

    async def _record_check(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: AgentRun,
        model: str,
        outcome: str,
        latency_ms: int,
    ) -> None:
        await record_job_call(
            session,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            agent_run_id=run.id,
            job=Job.BOUNDARY_CHECK,
            model=model,
            outcome=outcome,
            latency_ms=latency_ms,
        )

    def _default_context_builder(self) -> ContextBuilder:
        settings = self._settings or get_settings()
        return ContextBuilder(
            image_loader=_load_message_image,
            vision_enabled=settings.llm_vision_enabled,
        )

    async def _style_hint(self, session: AsyncSession, conversation: Conversation) -> str | None:
        """The humanizer's prompt hint, when this conversation's channel wants it."""
        channel = await session.get(Channel, conversation.channel_id)
        if channel is None or not bool((channel.config or {}).get("humanize")):
            return None
        return HUMANIZE_STYLE_HINT

    async def _recent_messages(
        self, session: AsyncSession, agent: Agent, conversation: Conversation
    ) -> list[Message]:
        """Rows from the `limit`-th most recent counted message on, in `seq` order.

        Counted: USER, HUMAN_OPERATOR and ASSISTANT that is not a `tool_call`.
        Tool rows between counted ones come along, so the history starts with a
        counted message and a tool call never loses its results.

        With sessions on, only the current session's messages
        (`app.conversations.sessions`); no session row means the whole
        conversation, as before.
        """
        limit = agent.context_message_limit or 20
        counted = Message.role.in_([MessageRole.USER, MessageRole.HUMAN_OPERATOR]) | (
            (Message.role == MessageRole.ASSISTANT) & (Message.type != "tool_call")
        )
        in_scope = Message.conversation_id == conversation.id
        if agent.session_memory_enabled:
            first_seq = session_first_seq(await current_session(session, conversation))
            if first_seq is not None:
                in_scope = in_scope & (Message.seq >= first_seq)
        start_seq = (
            await session.execute(
                select(Message.seq)
                .where(in_scope, counted)
                .order_by(Message.seq.desc())
                .offset(limit - 1)
                .limit(1)
            )
        ).scalar_one_or_none()
        query = select(Message).where(in_scope)
        if start_seq is not None:
            query = query.where(Message.seq >= start_seq)
        result = await session.execute(query.order_by(Message.seq))
        return list(result.scalars().all())

    async def _load_memories(
        self, session: AsyncSession, agent: Agent, conversation: Conversation
    ) -> list[Memory]:
        """Long-term facts folded into the system prompt (see `ContextBuilder`).

        Gated by `agent.memory_enabled` — the same flag that gates the
        `remember` tool in `app/tools/builtin.py`, so a memory-disabled agent
        neither writes nor reads long-term facts.
        """
        if not agent.memory_enabled:
            return []
        return await MemoryService(session).get_contact_facts(
            conversation.tenant_id,
            conversation.contact_id,
            limit=get_settings().memory_context_limit,
        )

    def llm_provider(self, agent: Agent) -> LLMProvider:
        """The provider this runtime would use for `agent`.

        Exposed so `app.conversations.turn` can run the post-turn summarizer
        with the exact same (possibly scripted, in tests) provider the turn
        itself just used, instead of constructing an unrelated one.
        """
        return self._provider or get_provider(agent, self._settings)

    def _tool_call_message(
        self,
        conversation: Conversation,
        run: AgentRun,
        calls: list[ToolCall],
    ) -> Message:
        """The assistant's tool-call step, recorded so later turns can replay it."""
        payload: dict[str, Any] = {
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls
            ]
        }
        summary = ", ".join(
            f"{call.name}({json.dumps(call.arguments, ensure_ascii=False, default=str)})"
            for call in calls
        )
        return Message(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            channel_id=conversation.channel_id,
            direction=MessageDirection.OUT,
            role=MessageRole.ASSISTANT,
            type="tool_call",
            content=summary,
            raw_payload=payload,
            agent_run_id=run.id,
            tool_call_id=calls[0].id if calls else None,
            # Explicit timestamps: Postgres `now()` is the *transaction*
            # timestamp, so every row a turn writes would otherwise tie and
            # replay out of order.
            created_at=datetime.now(UTC),
        )

    def _tool_result_message(
        self,
        conversation: Conversation,
        run: AgentRun,
        call: ToolCall,
        content: str,
    ) -> Message:
        return Message(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            channel_id=conversation.channel_id,
            direction=MessageDirection.IN,
            role=MessageRole.TOOL,
            type="tool_result",
            content=content,
            agent_run_id=run.id,
            tool_call_id=call.id,
            created_at=datetime.now(UTC),
        )
