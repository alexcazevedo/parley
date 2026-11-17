"""The one way to load the agent a conversation runs with (PRODUCT.md F3).

`Conversation.agent_id` is copied from the channel at creation time, and a
database written by an older version can hold a channel or conversation that
points at another tenant's agent. Every code path that runs an agent for a
conversation (turn, sentiment check, rolling summary, handoff brief) goes
through `load_conversation_agent`, which only hands out an agent owned by the
conversation's own tenant. Callers treat `None` as "no agent available".
"""

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.conversation import Conversation

logger = structlog.get_logger(__name__)


async def load_conversation_agent(
    session: AsyncSession, conversation: Conversation
) -> Agent | None:
    """The conversation's agent, or `None` if it is missing or another tenant's."""
    # Looked up by id alone (not filtered by tenant) so a mismatch can be told
    # apart from a missing row and logged with both tenants.
    agent = await session.scalar(select(Agent).where(Agent.id == conversation.agent_id))
    if agent is None:
        return None
    if agent.tenant_id != conversation.tenant_id:
        logger.warning(
            "agent.tenant_mismatch",
            conversation_id=str(conversation.id),
            agent_id=str(agent.id),
            conversation_tenant_id=str(conversation.tenant_id),
            agent_tenant_id=str(agent.tenant_id),
        )
        return None
    return agent
