"""Frustration classifier: one best-effort LLM call scoring user frustration.

Runs (in a future ticket, #21) BEFORE the agent turn on `ACTIVE_AI`
conversations to detect proactive-escalation triggers (see PRODUCT.md,
item #4 of the seamless roadmap). This module only produces the score —
turn wiring, threshold comparison, and the handoff itself are ticket 21's
job (see "Not included" below).

Strictly best-effort, mirroring `app/handoff/summarizer.py`'s pattern: any
failure (LLM error, timeout, unparseable response) is logged and turned into
`None` rather than raised, so a broken/slow classifier never blocks the
normal agent turn.
"""

import asyncio
import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import end_transaction
from app.llm.base import ROLE_SYSTEM, ROLE_USER, LLMMessage
from app.llm.factory import get_provider, resolve_model
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
) -> float | None:
    """Best-effort frustration score in [0.0, 1.0], or `None` on any failure.

    Never raises: LLM errors, timeouts, and unparseable responses are all
    logged and swallowed so a broken classifier never blocks the agent turn.
    """
    settings = get_settings()
    try:
        messages = await _recent_messages(session, conversation)
        prompt = _build_prompt(messages, input_text)
        provider = get_provider(agent)
        model = resolve_model(agent)
        # No transaction open during the model call; the timeout wraps only it.
        await end_transaction(session)
        response = await asyncio.wait_for(
            provider.complete(prompt, model=model, temperature=0.0, max_tokens=16),
            timeout=settings.sentiment_timeout_seconds,
        )
        return _parse_score(response.content)
    except Exception:
        logger.warning("sentiment.classify_failed", conversation_id=str(conversation.id))
        return None


async def _recent_messages(session: AsyncSession, conversation: Conversation) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
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
    lines.append(f"user: {input_text.strip()}")
    transcript = "\n".join(lines)

    return [
        LLMMessage(role=ROLE_SYSTEM, content=SENTIMENT_SYSTEM_PROMPT),
        LLMMessage(role=ROLE_USER, content=f"Conversation transcript:\n{transcript}"),
    ]


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
