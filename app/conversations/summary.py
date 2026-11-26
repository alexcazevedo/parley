"""Rolling conversation summary: keeps the LLM's context bounded without
losing older facts as a conversation grows past `agent.context_message_limit`.

Only reads from `messages` and writes to `conversation.summary` /
`conversation.summary_last_seq` — raw messages are never edited or deleted.
`summary_last_seq` (migration 0013) is the `messages.seq` of the last message
folded into `summary`: the unsummarized tail is every message with a higher
`seq`, whatever clock stamped its `created_at`.

A `seq` value is taken at insert, not at commit, so a message with a lower
`seq` could become visible after the summary moved past it. Every insert of a
message (`app.messages.ingest`, the operator message endpoint) and
`_summarize` therefore lock the conversation row (`SELECT id ... FOR UPDATE`)
first: once `_summarize` holds the lock, every message with a `seq` taken so
far is committed and visible, and any later one gets a higher `seq`.
"""

from collections.abc import Sequence

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import end_transaction
from app.llm.base import ROLE_SYSTEM, ROLE_USER, LLMMessage, LLMProvider, LLMResponse
from app.llm.jobs import (
    NATIVE_KINDS,
    OUTCOME_OK,
    OUTCOME_UNUSABLE,
    CallTimer,
    Job,
    failure_outcome,
    job_model,
    job_target,
    record_job_call,
    target_label,
    usage_of,
    writing_provider,
)
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole

logger = structlog.get_logger(__name__)

SUMMARIZER_SYSTEM_PROMPT = (
    "You maintain a running summary of a customer support conversation. Given "
    "the prior summary (if any) and a batch of new messages, produce an "
    "updated summary: concise, factual, third person, preserving names, "
    "commitments and open issues. Reply with the summary text only."
)

# Tool-call/tool-result plumbing carries no conversational content the
# summary needs.
_SUMMARIZABLE_ROLES = frozenset(
    {MessageRole.USER, MessageRole.ASSISTANT, MessageRole.HUMAN_OPERATOR}
)


async def maybe_summarize(
    session: AsyncSession, agent: Agent, conversation: Conversation, llm: LLMProvider
) -> None:
    """Advance the rolling summary if enough new messages have piled up.

    Never raises: callers run this after a turn already produced its reply,
    so a broken summarizer must not cost the user their answer.

    Reads first, then ends the session's transaction
    (`app.db.end_transaction`) before the model call, then writes (flushed,
    not committed — the caller commits). No transaction is held while the
    model runs; rows written before this call are committed by it and no
    longer roll back with a later failure.
    """
    threshold = agent.summary_threshold or 0
    if threshold <= 0:
        return
    try:
        await _summarize(session, agent, conversation, llm, threshold)
    except Exception:
        logger.exception("summary.failed", conversation_id=str(conversation.id))


async def _summarize(
    session: AsyncSession,
    agent: Agent,
    conversation: Conversation,
    llm: LLMProvider,
    threshold: int,
) -> None:
    # The lock waits for any insert of a message into this conversation that
    # is still in flight (see the module docstring); it is released by
    # `end_transaction` before the model call. The cursor is read under it.
    last_seq = await session.scalar(
        select(Conversation.summary_last_seq)
        .where(Conversation.id == conversation.id)
        .with_for_update()
    )
    after_cursor = Message.conversation_id == conversation.id
    if last_seq is not None:
        after_cursor = after_cursor & (Message.seq > last_seq)

    pending = await session.scalar(select(func.count()).select_from(Message).where(after_cursor))
    if (pending or 0) < threshold:
        return

    result = await session.execute(select(Message).where(after_cursor).order_by(Message.seq))
    new_messages = list(result.scalars().all())
    if not new_messages:
        return
    covered_seq = new_messages[-1].seq

    transcript = _render(new_messages)
    if not transcript:
        # Nothing summarizable in this batch (e.g. all tool-call plumbing):
        # advance the cursor anyway so it does not get stuck re-checking the
        # same non-content messages every turn.
        conversation.summary_last_seq = covered_seq
        await session.flush()
        return

    prompt: list[LLMMessage] = [LLMMessage(role=ROLE_SYSTEM, content=SUMMARIZER_SYSTEM_PROMPT)]
    prior = (conversation.summary or "").strip()
    if prior:
        prompt.append(LLMMessage(role=ROLE_USER, content=f"Prior summary:\n{prior}"))
    prompt.append(LLMMessage(role=ROLE_USER, content=f"New messages:\n{transcript}"))

    # Fixed temperature/max_tokens rather than reusing `agent.temperature`:
    # summarization wants a low-variance, deterministic-ish output regardless
    # of how the agent is tuned for conversation. Shortcut noted per mvp mode.
    # Reads done, writes not started: no transaction while the model runs.
    # No setting: `agent.model`, exactly as before; `llm:<model>`: that model;
    # a native form: that model on its vendor (`NativeProvider`), recorded as set.
    model = agent.model
    target = job_target(Job.SUMMARY)
    if target.kind == "llm" or target.kind in NATIVE_KINDS:
        model = job_model(Job.SUMMARY, agent)
    recorded = target_label(target) if target.kind in NATIVE_KINDS else model
    llm = writing_provider(target, llm)
    await end_transaction(session)
    called = CallTimer()
    try:
        response = await llm.complete(prompt, model=model, temperature=0.2, max_tokens=1024)
    except Exception as exc:
        await _record(session, conversation, recorded, failure_outcome(exc), called)
        raise
    summary = (response.content or "").strip()
    await _record(
        session,
        conversation,
        recorded,
        OUTCOME_OK if summary else OUTCOME_UNUSABLE,
        called,
        response,
    )
    if not summary:
        return

    conversation.summary = summary
    conversation.summary_last_seq = covered_seq
    await session.flush()
    logger.info(
        "summary.updated", conversation_id=str(conversation.id), last_seq=covered_seq
    )


async def _record(
    session: AsyncSession,
    conversation: Conversation,
    model: str,
    outcome: str,
    called: CallTimer,
    response: LLMResponse | None = None,
) -> None:
    prompt_tokens, completion_tokens = usage_of(response)
    await record_job_call(
        session,
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        agent_run_id=None,
        job=Job.SUMMARY,
        model=model,
        outcome=outcome,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=called.elapsed_ms(),
    )


def _render(messages: Sequence[Message]) -> str:
    lines = []
    for message in messages:
        if message.role not in _SUMMARIZABLE_ROLES:
            continue
        speaker = "user" if message.role == MessageRole.USER else "assistant"
        lines.append(f"{speaker}: {message.content}")
    return "\n".join(lines)
