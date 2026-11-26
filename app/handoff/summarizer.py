"""Best-effort operator-brief generation at handoff time.

`app/handoff/service.py` stays a pure state machine on purpose — this module
is where the one extra LLM call for the warm-handoff summary lives. The tool
`handoff_to_human` (`app/tools/builtin.py`) and the turn's sentiment path go
through `handoff_with_summary`; the operator endpoint
(`app/api/conversations.py`'s `POST /{id}/handoff`) calls `HandoffService.handoff`,
commits, then calls `generate_handoff_summary` and stores the brief.

Generation is strictly best-effort: any failure (LLM error, timeout, bad
response) is logged and swallowed — `generate_handoff_summary` returns
`None` rather than raising, so the escalation itself is never blocked or
delayed past `SUMMARY_TIMEOUT_SECONDS`.
"""

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.loading import load_conversation_agent
from app.db import end_transaction
from app.handoff.service import HandoffService
from app.llm.base import ROLE_SYSTEM, ROLE_USER, LLMMessage
from app.llm.factory import get_provider
from app.llm.jobs import (
    OUTCOME_OK,
    OUTCOME_UNUSABLE,
    CallTimer,
    Job,
    failure_outcome,
    job_model,
    record_job_call,
    usage_of,
)
from app.memory.service import MemoryService
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.handoff import Handoff, HandoffRequestedBy
from app.models.message import Message, MessageRole

logger = structlog.get_logger(__name__)

SUMMARY_TIMEOUT_SECONDS = 10

# Same order of magnitude as `agent.context_message_limit`'s default, but a
# fixed constant rather than reusing that column: the operator brief wants a
# generous, roughly-fixed window regardless of how an agent is tuned for
# everyday turn context. Shortcut noted per mvp mode.
SUMMARY_MESSAGE_LIMIT = 30

HANDOFF_SUMMARY_SYSTEM_PROMPT = (
    "You write a short brief for a human operator who is about to take over a "
    "customer-support conversation from an AI assistant. Read the contact "
    "facts, prior summary, handoff reason and recent transcript below, then "
    "write a compact brief covering: who the contact is (name/context if "
    "known), what they want, what the AI assistant already did or answered "
    "(including any tool results), why the conversation is escalating now, "
    "and the user's current tone. Write the brief in the same language as the "
    "conversation. Reply with the brief text only, no preamble or headings."
)

# Persisted role -> a short label for the transcript rendered into the
# summarizer prompt. Unlike `app/conversations/summary.py`'s rolling summary,
# tool results ARE included: the operator needs to see what the AI already
# tried, not just what it said.
_ROLE_LABELS = {
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "assistant",
    MessageRole.HUMAN_OPERATOR: "operator",
    MessageRole.TOOL: "tool_result",
}


async def generate_handoff_summary(
    session: AsyncSession, conversation: Conversation, reason: str | None
) -> str | None:
    """One best-effort chat-completion producing a compact operator brief.

    Never raises: any exception (including a timeout) is logged and turned
    into `None` so a broken/slow summarizer never blocks the handoff.
    """
    tenant_id, conversation_id = conversation.tenant_id, conversation.id
    called: CallTimer | None = None
    try:
        # Missing or another tenant's agent: no brief (the loader logs a mismatch).
        agent = await load_conversation_agent(session, conversation)
        if agent is None:
            logger.warning(
                "handoff.summary_skipped_missing_agent", conversation_id=str(conversation.id)
            )
            return None
        prompt = await _build_prompt(session, agent, conversation, reason)
        provider = get_provider(agent)
        model = job_model(Job.HANDOFF_BRIEF, agent)
        # No transaction open during the model call; the timeout wraps only it.
        await end_transaction(session)
        called = CallTimer()
        response = await asyncio.wait_for(
            provider.complete(prompt, model=model, temperature=0.2, max_tokens=512),
            timeout=SUMMARY_TIMEOUT_SECONDS,
        )
        summary = (response.content or "").strip()
    except Exception as exc:
        logger.exception("handoff.summary_failed", conversation_id=str(conversation_id))
        if called is not None:
            await record_job_call(
                session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                agent_run_id=None,
                job=Job.HANDOFF_BRIEF,
                model=model,
                outcome=failure_outcome(exc),
                latency_ms=called.elapsed_ms(),
            )
        return None
    prompt_tokens, completion_tokens = usage_of(response)
    await record_job_call(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        agent_run_id=None,
        job=Job.HANDOFF_BRIEF,
        model=model,
        outcome=OUTCOME_OK if summary else OUTCOME_UNUSABLE,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=called.elapsed_ms(),
    )
    return summary or None


async def handoff_with_summary(
    session: AsyncSession,
    conversation: Conversation,
    reason: str | None,
    requested_by: HandoffRequestedBy,
) -> Handoff:
    """`HandoffService.handoff` plus the best-effort operator brief.

    Order: refuse an illegal state first (`IllegalTransitionError`, before any
    write or model call; both call sites handle it: tool failure / 409), then
    generate the brief with no transaction open, then do the transition and set
    the summary. The state change and the `Handoff` row are left for the
    caller to commit. Anything the caller wrote before this call is committed
    while the brief is generated.
    """
    service = HandoffService(session)
    service.ensure_can_handoff(conversation)
    summary = await generate_handoff_summary(session, conversation, reason)
    # The brief goes in with the transition so the `conversation.handoff` event carries it.
    return await service.handoff(conversation, reason, requested_by, brief=summary)


async def _build_prompt(
    session: AsyncSession, agent: Agent, conversation: Conversation, reason: str | None
) -> list[LLMMessage]:
    messages = await _recent_messages(session, conversation)
    memories = await _contact_memories(session, agent, conversation)

    parts: list[str] = []
    facts = [m.content.strip() for m in memories if m.content and m.content.strip()]
    if facts:
        parts.append("Known facts about the contact:\n" + "\n".join(f"- {f}" for f in facts))
    prior_summary = (conversation.summary or "").strip()
    if prior_summary:
        parts.append("Prior conversation summary:\n" + prior_summary)
    parts.append(f"Handoff reason: {(reason or '').strip() or 'not given'}")
    transcript = _render_transcript(messages)
    if transcript:
        parts.append("Recent conversation transcript:\n" + transcript)

    return [
        LLMMessage(role=ROLE_SYSTEM, content=HANDOFF_SUMMARY_SYSTEM_PROMPT),
        LLMMessage(role=ROLE_USER, content="\n\n".join(parts)),
    ]


async def _recent_messages(session: AsyncSession, conversation: Conversation) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.seq.desc())
        .limit(SUMMARY_MESSAGE_LIMIT)
    )
    return list(reversed(result.scalars().all()))


async def _contact_memories(session: AsyncSession, agent: Agent, conversation: Conversation):
    if not agent.memory_enabled:
        return []
    return await MemoryService(session).get_contact_memories(
        conversation.tenant_id, conversation.contact_id
    )


def _render_transcript(messages: list[Message]) -> str:
    lines = []
    for message in messages:
        label = _ROLE_LABELS.get(message.role)
        if label is None or not (message.content or "").strip():
            continue
        lines.append(f"{label}: {message.content.strip()}")
    return "\n".join(lines)
