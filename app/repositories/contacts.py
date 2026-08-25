import uuid

from sqlalchemy import select

from app.models.contact import Contact
from app.repositories.base import BaseRepository


class ContactsRepository(BaseRepository[Contact]):
    model = Contact

    async def get_by_external_user_id(
        self, tenant_id: uuid.UUID, channel_id: uuid.UUID, external_user_id: str
    ) -> Contact | None:
        result = await self.session.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id,
                Contact.channel_id == channel_id,
                Contact.external_user_id == external_user_id,
            )
        )
        return result.scalar_one_or_none()
