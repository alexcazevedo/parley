"""Tenant-scoped media serving: `GET /media/{message_id}` streams back the
file stored by `app/messages/ingest.py` for an `image`/`audio` message.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.media.storage import get_media_storage
from app.models.tenant import Tenant
from app.repositories.messages import MessagesRepository

router = APIRouter(tags=["media"])


@router.get("/media/{message_id}")
async def get_media(
    message_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    message = await MessagesRepository(db).get(tenant.id, message_id)
    if message is None or not message.media_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")

    storage = get_media_storage()
    path = storage.path(message.media_key)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media not found")

    return FileResponse(path, media_type=message.media_mime)
