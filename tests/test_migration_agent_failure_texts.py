"""Migration 0017 adds the two per-agent failure texts, nullable.

Runs alembic in a subprocess against a scratch database of its own on the
test server; the database is dropped afterwards.
"""

import uuid

import asyncpg

from tests.conftest import _ASYNCPG_ADMIN_DSN
from tests.test_migration_open_conversation import _alembic

COLUMNS = {"error_message", "unsupported_media_message"}


async def _agent_columns(conn) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'agents' AND column_name = ANY($1::text[])",
        list(COLUMNS),
    )
    return {r["column_name"]: r["is_nullable"] for r in rows}


async def test_upgrade_adds_and_downgrade_removes_the_failure_text_columns():
    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0017")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await _agent_columns(conn) == {c: "YES" for c in COLUMNS}
        finally:
            await conn.close()

        _alembic(database_url, "downgrade", "0016")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await _agent_columns(conn) == {}
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
