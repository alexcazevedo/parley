"""Rolling conversation summary: keeps the LLM's context bounded without
losing older facts as a conversation grows past `agent.context_message_limit`.

Only reads from `messages` and writes to `conversation.summary` /
`conversation.summary_cursor` — raw messages are never edited or deleted.
`summary_cursor` (migration 0003) is the *count* of messages already folded
into `summary`, not a timestamp or id: messages are append-only, so "every
row at index >= cursor" is always exactly the unsummarized tail.
"""

from collections.abc import Sequence

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import ROLE_SYSTEM, ROLE_USER, LLMMessage, LLMProvider
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
    total = await session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    total = total or 0
    cursor = conversation.summary_cursor or 0
    if total - cursor < threshold:
        return

    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
        .offset(cursor)
    )
    new_messages = list(result.scalars().all())
    if not new_messages:
        return

    transcript = _render(new_messages)
    if not transcript:
        # Nothing summarizable in this batch (e.g. all tool-call plumbing):
        # advance the cursor anyway so it does not get stuck re-checking the
        # same non-content messages every turn.
        conversation.summary_cursor = cursor + len(new_messages)
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
    response = await llm.complete(prompt, model=agent.model, temperature=0.2, max_tokens=1024)
    summary = (response.content or "").strip()
    if not summary:
        return

    conversation.summary = summary
    conversation.summary_cursor = cursor + len(new_messages)
    await session.flush()
    logger.info(
        "summary.updated", conversation_id=str(conversation.id), cursor=conversation.summary_cursor
    )


def _render(messages: Sequence[Message]) -> str:
    lines = []
    for message in messages:
        if message.role not in _SUMMARIZABLE_ROLES:
            continue
        speaker = "user" if message.role == MessageRole.USER else "assistant"
        lines.append(f"{speaker}: {message.content}")
    return "\n".join(lines)
