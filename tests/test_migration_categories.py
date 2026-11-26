"""Migration 0014 adds `categories` and the two `conversations` columns; downgrade removes them.

Runs alembic in a subprocess against a scratch database of its own on the
test server; the database is dropped afterwards.
"""

import uuid

import asyncpg
import pytest

from tests.conftest import _ASYNCPG_ADMIN_DSN
from tests.test_migration_open_conversation import _alembic


async def test_upgrade_adds_categories_and_downgrade_removes_them():
    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "head")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await conn.fetchval("SELECT to_regclass('public.categories')") is not None
            assert (
                await conn.fetchval(
                    "SELECT contype::text FROM pg_constraint"
                    " WHERE conname = 'uq_categories_tenant_name'"
                )
                == "u"
            )
            columns = {
                r["column_name"]: r
                for r in await conn.fetch(
                    "SELECT column_name, is_nullable, column_default FROM"
                    " information_schema.columns WHERE table_name = 'conversations'"
                )
            }
            assert columns["category_id"]["is_nullable"] == "YES"
            assert columns["category_locked"]["is_nullable"] == "NO"
            assert columns["category_locked"]["column_default"] == "false"

            tenant, agent, channel, contact, conv = (uuid.uuid4() for _ in range(5))
            category = uuid.uuid4()
            await conn.execute(
                "INSERT INTO tenants (id, name, api_key_hash) VALUES ($1, 'Acme', 'h')", tenant
            )
            await conn.execute(
                "INSERT INTO agents (id, tenant_id, name, instructions, model)"
                " VALUES ($1, $2, 'Bot', 'Hi.', 'gpt-4o-mini')",
                agent,
                tenant,
            )
            await conn.execute(
                "INSERT INTO channels (id, tenant_id, type, name, agent_id)"
                " VALUES ($1, $2, 'webchat', 'Web', $3)",
                channel,
                tenant,
                agent,
            )
            await conn.execute(
                "INSERT INTO contacts (id, tenant_id, channel_id, external_user_id)"
                " VALUES ($1, $2, $3, 'u')",
                contact,
                tenant,
                channel,
            )
            await conn.execute(
                "INSERT INTO categories (id, tenant_id, name) VALUES ($1, $2, 'billing')",
                category,
                tenant,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO categories (id, tenant_id, name) VALUES ($1, $2, 'billing')",
                    uuid.uuid4(),
                    tenant,
                )
            await conn.execute(
                "INSERT INTO conversations (id, tenant_id, contact_id, channel_id, agent_id,"
                " state, category_id, category_locked)"
                " VALUES ($1, $2, $3, $4, $5, 'ACTIVE_AI', $6, true)",
                conv,
                tenant,
                contact,
                channel,
                agent,
                category,
            )
            await conn.execute("DELETE FROM categories WHERE id = $1", category)
            row = await conn.fetchrow(
                "SELECT category_id, category_locked FROM conversations WHERE id = $1", conv
            )
            assert row["category_id"] is None
            assert row["category_locked"] is True
        finally:
            await conn.close()

        _alembic(database_url, "downgrade", "0013")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await conn.fetchval("SELECT to_regclass('public.categories')") is None
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = 'conversations'"
                    " AND column_name IN ('category_id', 'category_locked')"
                )
                == 0
            )
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
