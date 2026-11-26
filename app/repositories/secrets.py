import builtins
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_secret import TenantSecret
from app.security.secrets import decrypt, encrypt


class SecretsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def set(self, tenant_id: uuid.UUID, name: str, value: str) -> None:
        """Create or replace; an atomic upsert on (tenant_id, name)."""
        token = encrypt(value)
        stmt = insert(TenantSecret).values(
            id=uuid.uuid4(), tenant_id=tenant_id, name=name, value_encrypted=token
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_tenant_secrets_tenant_name",
            set_={"value_encrypted": token, "updated_at": func.clock_timestamp()},
        )
        await self.session.execute(stmt)

    async def names(self, tenant_id: uuid.UUID) -> list[TenantSecret]:
        result = await self.session.execute(
            select(TenantSecret)
            .where(TenantSecret.tenant_id == tenant_id)
            .order_by(TenantSecret.name)
        )
        return list(result.scalars().all())

    async def delete(self, tenant_id: uuid.UUID, name: str) -> bool:
        result = await self.session.execute(
            delete(TenantSecret)
            .where(TenantSecret.tenant_id == tenant_id, TenantSecret.name == name)
            .returning(TenantSecret.id)
        )
        return result.first() is not None

    async def resolve(self, tenant_id: uuid.UUID, names: "builtins.set[str]") -> dict[str, str]:
        """Decrypted values of the names that exist for this tenant."""
        if not names:
            return {}
        result = await self.session.execute(
            select(TenantSecret.name, TenantSecret.value_encrypted).where(
                TenantSecret.tenant_id == tenant_id, TenantSecret.name.in_(names)
            )
        )
        return {name: decrypt(token) for name, token in result.all()}
