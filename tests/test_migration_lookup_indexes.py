"""Migration 0016 adds the lookup indexes for handoffs, memories and the inbox.

Runs alembic in a subprocess against a scratch database of its own on the
test server; the database is dropped afterwards.
"""

import uuid

import asyncpg

from tests.conftest import _ASYNCPG_ADMIN_DSN
from tests.test_migration_open_conversation import _alembic

INDEXES = {
    "ix_handoffs_conversation_id_created_at",
    "ix_memories_contact_id",
    "ix_conversations_tenant_state_last_message_at",
}


async def _existing_indexes(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE indexname = ANY($1::text[])",
        list(INDEXES),
    )
    return {r["indexname"] for r in rows}


async def test_upgrade_adds_and_downgrade_removes_the_lookup_indexes():
    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0016")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await _existing_indexes(conn) == INDEXES
        finally:
            await conn.close()

        _alembic(database_url, "downgrade", "0015")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await _existing_indexes(conn) == set()
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
