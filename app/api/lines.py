"""`GET /lines`: the waiting lines, one per category plus one for the
uncategorized, each holding the unclaimed conversations handed to a human,
oldest handoff first.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.conversations import (
    ConversationOut,
    _attach_contacts,
    _attach_latest_handoffs,
)
from app.api.deps import get_current_tenant, get_db
from app.models.tenant import Tenant
from app.repositories.categories import CategoriesRepository
from app.repositories.conversations import ConversationsRepository

router = APIRouter(prefix="/lines", tags=["lines"])


class LineOut(BaseModel):
    category_id: uuid.UUID | None
    category_name: str | None
    conversations: list[ConversationOut]


@router.get("", response_model=list[LineOut])
async def list_lines(
    limit: int = Query(default=200, ge=1, le=500),
    include_handoff: bool = Query(default=False),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[LineOut]:
    """Every category of the tenant by name (empty ones too), then the
    uncategorized line, always present. `limit` caps the total across lines,
    keeping the oldest.
    """
    categories = await CategoriesRepository(db).list(tenant.id)
    conversations = await ConversationsRepository(db).list_waiting(tenant.id, limit)
    outs = [ConversationOut.model_validate(c) for c in conversations]
    await _attach_contacts(db, tenant, conversations, outs)
    if include_handoff:
        await _attach_latest_handoffs(db, tenant, conversations, outs)

    by_category: dict[uuid.UUID | None, list[ConversationOut]] = {}
    for conversation, out in zip(conversations, outs, strict=True):
        by_category.setdefault(conversation.category_id, []).append(out)

    lines = [
        LineOut(category_id=c.id, category_name=c.name, conversations=by_category.get(c.id, []))
        for c in categories
    ]
    lines.append(
        LineOut(category_id=None, category_name=None, conversations=by_category.get(None, []))
    )
    return lines
