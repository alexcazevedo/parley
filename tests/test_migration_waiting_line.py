"""Migration 0015 adds `waiting_since`, `claimed_by`, `claimed_at` and
`ix_conversations_waiting`, and backfills `waiting_since` for conversations
already in `HUMAN_HANDOFF`.

Runs alembic in a subprocess against a scratch database of its own on the
test server; the database is dropped afterwards.
"""

import uuid

import asyncpg

from tests.conftest import _ASYNCPG_ADMIN_DSN
from tests.test_migration_open_conversation import (
    _alembic,
    _insert_contact,
    _insert_conversation,
)


async def test_upgrade_backfills_waiting_since_and_adds_the_index():
    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0014")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            ids = {"tenant": uuid.uuid4(), "agent": uuid.uuid4(), "channel": uuid.uuid4()}
            await conn.execute(
                "INSERT INTO tenants (id, name, api_key_hash) VALUES ($1, 'Acme', 'h')",
                ids["tenant"],
            )
            await conn.execute(
                "INSERT INTO agents (id, tenant_id, name, instructions, model)"
                " VALUES ($1, $2, 'Bot', 'Hi.', 'gpt-4o-mini')",
                ids["agent"],
                ids["tenant"],
            )
            await conn.execute(
                "INSERT INTO channels (id, tenant_id, type, name, agent_id)"
                " VALUES ($1, $2, 'webchat', 'Web', $3)",
                ids["channel"],
                ids["tenant"],
                ids["agent"],
            )

            async def handoff(conversation_id, minutes_ago, *, resolved):
                await conn.execute(
                    "INSERT INTO handoffs (id, tenant_id, conversation_id, requested_by,"
                    " created_at, resolved_at)"
                    " VALUES ($1, $2, $3, 'agent', now() - make_interval(mins => $4::int),"
                    " CASE WHEN $5 THEN now() ELSE NULL END)",
                    uuid.uuid4(),
                    ids["tenant"],
                    conversation_id,
                    minutes_ago,
                    resolved,
                )

            # Newest unresolved handoff wins over an older one and over a
            # newer resolved one.
            a = await _insert_conversation(
                conn, ids, await _insert_contact(conn, ids, "a"), "HUMAN_HANDOFF", created=500
            )
            await handoff(a, 120, resolved=False)
            await handoff(a, 300, resolved=False)
            await handoff(a, 10, resolved=True)
            # No handoff row: now().
            b = await _insert_conversation(
                conn, ids, await _insert_contact(conn, ids, "b"), "HUMAN_HANDOFF", created=500
            )
            # Other states stay null.
            c = await _insert_conversation(
                conn, ids, await _insert_contact(conn, ids, "c"), "ACTIVE_AI", created=500
            )
            await handoff(c, 60, resolved=False)
        finally:
            await conn.close()

        _alembic(database_url, "upgrade", "head")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            rows = {
                r["id"]: r
                for r in await conn.fetch(
                    "SELECT id, waiting_since, claimed_by, claimed_at,"
                    " now() - waiting_since AS age FROM conversations"
                )
            }
            a_age = rows[a]["age"].total_seconds()
            assert 119 * 60 <= a_age <= 121 * 60
            assert rows[b]["waiting_since"] is not None
            assert rows[b]["age"].total_seconds() < 120
            assert rows[c]["waiting_since"] is None
            for row in rows.values():
                assert row["claimed_by"] is None and row["claimed_at"] is None

            indexdef = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_conversations_waiting'"
            )
            assert indexdef is not None
            assert "(tenant_id, waiting_since)" in indexdef
            assert "HUMAN_HANDOFF" in indexdef
        finally:
            await conn.close()

        _alembic(database_url, "downgrade", "0014")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert (
                await conn.fetchval(
                    "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_conversations_waiting'"
                )
                is None
            )
            columns = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'conversations'"
                )
            }
            assert not columns & {"waiting_since", "claimed_by", "claimed_at"}
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
