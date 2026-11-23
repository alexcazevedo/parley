"""Category classifier: one best-effort LLM call filing a conversation under a subject.

Runs on every batch of customer messages (see `app/conversations/turn.py`):
the model picks one of the tenant's categories (`app.models.category`) from
a numbered list, or `0` when none fits. Modelled on `app/agents/sentiment.py`.

Rules (PRODUCT.md, "Categories and waiting lines"):

- A conversation whose category a human chose (`category_locked`) and a
  tenant with no categories make no model call at all.
- The model only picks from the list; it never creates a category and never
  clears one: `0`, an out-of-range or unparseable answer, a timeout or a
  provider error leave the conversation where it was.
- The write is one conditional `UPDATE ... AND category_locked IS FALSE`, so
  a human's choice made while the model was thinking is never overwritten.

Strictly best-effort: this function never raises, and the caller's session
stays usable whatever fails. The reads run inside a savepoint (a missing
`categories` table, say, rolls back only the savepoint), no transaction is
open during the model call (`end_transaction` before it), and the write runs
in a savepoint of its own (a category deleted meanwhile fails the foreign
key: logged as `category.classify_failed`, swallowed).
"""

import asyncio
import re
import uuid
from collections.abc import Sequence
from typing import Any, cast

import structlog
from sqlalchemy import Table, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.agents.sentiment import _ROLE_LABELS, _recent_messages
from app.config import get_settings
from app.db import end_transaction
from app.llm.base import ROLE_SYSTEM, ROLE_USER, LLMMessage
from app.llm.factory import get_provider, resolve_model
from app.models.agent import Agent
from app.models.category import Category
from app.models.conversation import Conversation
from app.models.message import Message
from app.repositories.categories import CategoriesRepository

logger = structlog.get_logger(__name__)

CATEGORY_SYSTEM_PROMPT = (
    "You file a customer-support conversation under its subject. Pick the one "
    "category from the numbered list below that best describes what the "
    "conversation is about. If none of them fits, answer 0. Reply with ONLY "
    "the number, no words, no explanation.\n\nCategories:\n{categories}"
)

_INT_RE = re.compile(r"-?\d+")

_conversations = cast(Table, Conversation.__table__)


async def categorize_conversation(
    session: AsyncSession,
    conversation: Conversation,
    agent: Agent,
    input_text: str,
    message_ids: Sequence[uuid.UUID] = (),
) -> None:
    """File `conversation` under one of its tenant's categories. Never raises.

    `message_ids` are the rows of the batch: they are left out of the history,
    because `input_text` already carries them (rendered) once.

    On a valid new choice the row is updated (only while `category_locked` is
    false) and `conversation.category_id` is set on the loaded object too,
    without marking it dirty, so no later flush of the turn writes it again.
    """
    conversation_id = conversation.id
    tenant_id = conversation.tenant_id
    try:
        if conversation.category_locked:
            return
        async with session.begin_nested():
            categories = await CategoriesRepository(session).list(tenant_id)
            if not categories:
                return
            history = await _recent_messages(session, conversation, message_ids)
        prompt = _build_prompt(categories, history, input_text)
        current = conversation.category_id
        provider = get_provider(agent)
        model = resolve_model(agent)
        # No transaction open during the model call; the timeout wraps only it.
        await end_transaction(session)
        response = await asyncio.wait_for(
            provider.complete(prompt, model=model, temperature=0.0, max_tokens=8),
            timeout=get_settings().category_timeout_seconds,
        )
    except Exception:
        logger.warning("category.classify_failed", conversation_id=str(conversation_id))
        return

    chosen = _parse_choice(response.content, categories)
    if chosen is None or chosen.id == current:
        return
    category_id = chosen.id

    try:
        async with session.begin_nested():
            result = await session.execute(
                update(_conversations)
                .where(
                    _conversations.c.id == conversation_id,
                    _conversations.c.tenant_id == tenant_id,
                    _conversations.c.category_locked.is_(False),
                )
                .values(category_id=category_id)
            )
        await end_transaction(session)
    except Exception:
        logger.warning(
            "category.classify_failed",
            conversation_id=str(conversation_id),
            category_id=str(category_id),
        )
        return
    if cast(CursorResult[Any], result).rowcount != 1:
        # A human locked the category after the turn loaded the conversation.
        return
    set_committed_value(conversation, "category_id", category_id)
    logger.info(
        "category.assigned",
        conversation_id=str(conversation_id),
        category_id=str(category_id),
    )


def _build_prompt(
    categories: list[Category], history: list[Message], input_text: str
) -> list[LLMMessage]:
    listing = "\n".join(
        f"{number}. {_one_line(category.name)}: {_one_line(category.description or '')}"
        for number, category in enumerate(categories, start=1)
    )
    lines = []
    for message in history:
        label = _ROLE_LABELS.get(message.role)
        if label is None or not (message.content or "").strip():
            continue
        lines.append(f"{label}: {message.content.strip()}")
    # One `user:` line per line of the batch, as the sentiment prompt does.
    lines.extend(f"user: {line.strip()}" for line in input_text.splitlines() if line.strip())
    transcript = "\n".join(lines)

    return [
        LLMMessage(role=ROLE_SYSTEM, content=CATEGORY_SYSTEM_PROMPT.format(categories=listing)),
        LLMMessage(role=ROLE_USER, content=f"Conversation transcript:\n{transcript}"),
    ]


def _one_line(text: str) -> str:
    """Collapse whitespace so a multi-line description cannot break the list."""
    return " ".join(text.split())


def _parse_choice(content: str | None, categories: list[Category]) -> Category | None:
    """The category the answer's first integer names, or `None` (0, out of range, none)."""
    if not content:
        return None
    match = _INT_RE.search(content)
    if match is None:
        return None
    number = int(match.group())
    if not 1 <= number <= len(categories):
        return None
    return categories[number - 1]
