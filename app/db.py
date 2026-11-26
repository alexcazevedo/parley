"""Async SQLAlchemy engine/session setup."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, pool_pre_ping=True)

async_session_maker = async_sessionmaker(
    bind=engine, expire_on_commit=False, class_=AsyncSession
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def end_transaction(session: AsyncSession) -> None:
    """Commit the session's open transaction, if it has one; otherwise do nothing.

    Called right before slow work (a network call, a sleep) so the connection
    goes back to the pool instead of sitting idle in a transaction. Sessions
    use `expire_on_commit=False`, so loaded rows stay usable afterwards. What
    was written before is committed here and no longer rolls back if the
    caller fails later.
    """
    if session.in_transaction():
        await session.commit()
