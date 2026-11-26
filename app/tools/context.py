"""Who a tool call is for: the conversation, its channel and the contact.

Loaded once per turn (`load_call_context`) and carried on `ToolContext.call`.
The ledger stores `as_record()` on every `ToolExecution` row; HTTP tools get
`as_headers(call_id)`; MCP tools (a later ticket) get `as_meta(call_id)`.
Nothing here changes a tool's arguments.
"""

import uuid
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.conversation import Conversation

HEADER_CONVERSATION_ID = "X-Parley-Conversation-Id"
HEADER_CHANNEL_ID = "X-Parley-Channel-Id"
HEADER_USER_ID = "X-Parley-User-Id"
HEADER_CONTACT_NAME = "X-Parley-Contact-Name"
HEADER_CALL_ID = "X-Parley-Call-Id"

HEADER_NAMES = (
    HEADER_CONVERSATION_ID,
    HEADER_CHANNEL_ID,
    HEADER_USER_ID,
    HEADER_CONTACT_NAME,
    HEADER_CALL_ID,
)


@dataclass(frozen=True, slots=True)
class CallContext:
    conversation_id: uuid.UUID
    channel_id: uuid.UUID
    # The contact's `external_user_id`: the id the app gave parley for them.
    user_id: str
    contact_name: str | None = None

    def as_record(self) -> dict[str, str]:
        """What `tool_executions.context` holds; `contact_name` only when known."""
        record = {
            "conversation_id": str(self.conversation_id),
            "channel_id": str(self.channel_id),
            "user_id": self.user_id,
        }
        if self.contact_name:
            record["contact_name"] = self.contact_name
        return record

    def as_headers(self, call_id: uuid.UUID | str) -> dict[str, str]:
        """HTTP headers; the name is percent-encoded (UTF-8), left out when unknown."""
        headers = {
            HEADER_CONVERSATION_ID: str(self.conversation_id),
            HEADER_CHANNEL_ID: str(self.channel_id),
            HEADER_USER_ID: self.user_id,
        }
        if self.contact_name:
            headers[HEADER_CONTACT_NAME] = quote(self.contact_name, safe="")
        headers[HEADER_CALL_ID] = str(call_id)
        return headers

    def as_meta(self, call_id: uuid.UUID | str) -> dict[str, str]:
        """MCP `_meta` keys (`parley/...`), plain values."""
        meta = {f"parley/{key}": value for key, value in self.as_record().items()}
        meta["parley/call_id"] = str(call_id)
        return meta


async def load_call_context(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> CallContext:
    """The conversation's context, tenant-scoped. Raises `LookupError` if absent."""
    result = await session.execute(
        select(
            Conversation.id,
            Conversation.channel_id,
            Contact.external_user_id,
            Contact.display_name,
        )
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.id == conversation_id,
            Contact.tenant_id == tenant_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"conversation {conversation_id} not found in tenant {tenant_id}")
    return CallContext(
        conversation_id=row.id,
        channel_id=row.channel_id,
        user_id=row.external_user_id,
        contact_name=row.display_name or None,
    )
