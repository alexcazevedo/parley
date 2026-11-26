"""Frustration classifier: one best-effort LLM call scoring user frustration.

Runs BEFORE the agent turn on `ACTIVE_AI` conversations to detect
proactive-escalation triggers. This module only produces the score; the
threshold comparison and the handoff itself live in the turn flow
(`app/conversations/turn.py`).

Strictly best-effort, mirroring `app/handoff/summarizer.py`'s pattern: any
failure (LLM error, timeout, unparseable response) is logged and turned into
`None` rather than raised, so a broken/slow classifier never blocks the
normal agent turn.
"""

import asyncio
import re
import uuid
from collections.abc import Sequence

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import end_transaction
from app.llm.base import ROLE_SYSTEM, ROLE_USER, LLMMessage
from app.llm.decisions import DecisionUsage, HandOver, decide_sentiment, target_label
from app.llm.factory import get_provider
from app.llm.jobs import (
    OUTCOME_OK,
    OUTCOME_UNUSABLE,
    CallTimer,
    Job,
    failure_outcome,
    job_model,
    job_target,
    record_job_call,
    usage_of,
)
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole

logger = structlog.get_logger(__name__)

# Same order of magnitude as the agent turn's own context window, but a fixed
# constant (per the ticket) rather than reusing `agent.context_message_limit`.
SENTIMENT_MESSAGE_LIMIT = 10

SENTIMENT_SYSTEM_PROMPT = (
    "You rate how frustrated the USER is in a customer-support conversation, "
    "on a scale from 0.0 (calm/happy) to 1.0 (furious/about to give up). "
    "Consider whether frustration is escalating over the course of the "
    "conversation, not just the latest message. Anchors: 0.0-0.3 neutral or "
    "merely asking questions; 0.4-0.6 impatient, repeating themselves; "
    "0.7-0.8 explicit complaint about the service or the assistant ('this "
    "isn't helping', 'terrible service'), sarcasm, or insults; 0.9-1.0 "
    "furious, threatening to leave or demanding escalation. Reply with ONLY "
    "the number, no words, no explanation."
)

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")

_ROLE_LABELS = {
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "assistant",
    MessageRole.HUMAN_OPERATOR: "operator",
}


async def score_frustration(
    session: AsyncSession,
    conversation: Conversation,
    agent: Agent,
    input_text: str,
    message_ids: Sequence[uuid.UUID] = (),
) -> float | None:
    """Best-effort frustration score in [0.0, 1.0], or `None` on any failure.

    Never raises: LLM errors, timeouts, and unparseable responses are all
    logged and swallowed so a broken classifier never blocks the agent turn.

    `message_ids` are the rows of the batch being scored: they are left out of
    the history, because `input_text` already carries them (rendered) once.
    """
    settings = get_settings()
    tenant_id, conversation_id = conversation.tenant_id, conversation.id
    called: CallTimer | None = None
    handed_over = False
    try:
        messages = await _recent_messages(session, conversation, message_ids)
        prompt = _build_prompt(messages, input_text)
        provider = get_provider(agent)
        model = job_model(Job.SENTIMENT, agent, settings)
        target = job_target(Job.SENTIMENT, settings)
        recorded = model
        images = 0
        if target.kind == "typesafe":
            recorded = target_label(target)
            images = await evidence_images(session, conversation, messages, message_ids)
        # No transaction open during the model call; the timeout wraps only it
        # (on the decision path, the decision and any hand-over together).
        await end_transaction(session)
        called = CallTimer()
        if target.kind == "typesafe":
            usage = DecisionUsage()
            async with asyncio.timeout(settings.sentiment_timeout_seconds):
                try:
                    score = await decide_sentiment(
                        target, evidence=_transcript_text(prompt), images=images, usage=usage
                    )
                    prompt_tokens, completion_tokens = usage.prompt_tokens, usage.completion_tokens
                except HandOver as handed:
                    logger.info("sentiment.handed_over", why=handed.why)
                    handed_over, recorded = True, model
                    response = await provider.complete(
                        prompt, model=model, temperature=0.0, max_tokens=16
                    )
                    score = _parse_score(response.content)
                    prompt_tokens, completion_tokens = usage_of(response)
        else:
            response = await asyncio.wait_for(
                provider.complete(prompt, model=model, temperature=0.0, max_tokens=16),
                timeout=settings.sentiment_timeout_seconds,
            )
            score = _parse_score(response.content)
            prompt_tokens, completion_tokens = usage_of(response)
    except Exception as exc:
        logger.warning("sentiment.classify_failed", conversation_id=str(conversation_id))
        if called is not None:
            await record_job_call(
                session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                agent_run_id=None,
                job=Job.SENTIMENT,
                model=recorded,
                outcome=failure_outcome(exc),
                latency_ms=called.elapsed_ms(),
                handed_over=handed_over,
            )
        return None
    await record_job_call(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        agent_run_id=None,
        job=Job.SENTIMENT,
        model=recorded,
        outcome=OUTCOME_OK if score is not None else OUTCOME_UNUSABLE,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=called.elapsed_ms(),
        handed_over=handed_over,
    )
    return score


async def evidence_images(
    session: AsyncSession,
    conversation: Conversation,
    history: Sequence[Message],
    message_ids: Sequence[uuid.UUID] = (),
) -> int:
    """How many images the evidence holds: in the history and in the batch.

    Only the decision path asks; a decision model takes no images, so any
    image hands the call over to the agent's model.
    """
    count = sum(1 for message in history if _is_image(message.media_mime))
    if message_ids:
        batch = await session.execute(
            select(func.count(Message.id)).where(
                Message.tenant_id == conversation.tenant_id,
                Message.conversation_id == conversation.id,
                Message.id.in_(list(message_ids)),
                Message.media_mime.like("image/%"),
            )
        )
        count += int(batch.scalar_one())
    return count


def _is_image(mime: str | None) -> bool:
    return bool(mime) and str(mime).startswith("image/")


async def _recent_messages(
    session: AsyncSession,
    conversation: Conversation,
    exclude_ids: Sequence[uuid.UUID] = (),
) -> list[Message]:
    query = select(Message).where(Message.conversation_id == conversation.id)
    if exclude_ids:
        query = query.where(Message.id.not_in(list(exclude_ids)))
    result = await session.execute(
        query.order_by(Message.seq.desc())
        .limit(SENTIMENT_MESSAGE_LIMIT)
    )
    return list(reversed(result.scalars().all()))


def _build_prompt(messages: list[Message], input_text: str) -> list[LLMMessage]:
    lines = []
    for message in messages:
        label = _ROLE_LABELS.get(message.role)
        if label is None or not (message.content or "").strip():
            continue
        lines.append(f"{label}: {message.content.strip()}")
    # One `user:` line per line of the batch: input_text joins the batch's
    # rendered messages with newlines.
    lines.extend(f"user: {line.strip()}" for line in input_text.splitlines() if line.strip())
    transcript = "\n".join(lines)

    return [
        LLMMessage(role=ROLE_SYSTEM, content=SENTIMENT_SYSTEM_PROMPT),
        LLMMessage(role=ROLE_USER, content=f"Conversation transcript:\n{transcript}"),
    ]


def _transcript_text(prompt: list[LLMMessage]) -> str:
    """The evidence a decision model judges: the prompt's transcript message."""
    content = prompt[-1].content
    return content if isinstance(content, str) else ""


def _parse_score(content: str | None) -> float | None:
    if not content:
        return None
    match = _FLOAT_RE.search(content)
    if match is None:
        return None
    try:
        value = float(match.group())
    except ValueError:
        return None
    return max(0.0, min(1.0, value))
