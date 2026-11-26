"""Migration 0012 closes existing duplicate open conversations, then adds
`uq_conversations_open_contact_channel` (PRODUCT.md E4.5, ticket 04).

Runs alembic in a subprocess against a scratch database of its own on the
test server; the database is dropped afterwards.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg

from tests.conftest import _ASYNCPG_ADMIN_DSN

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic(database_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": database_url}
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


async def _insert_conversation(conn, ids, contact_id, state, *, created, last=None) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO conversations (id, tenant_id, contact_id, channel_id, agent_id, state,"
        " last_message_at, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6::conversation_state,"
        " now() - make_interval(mins => $7::int),"
        " now() - make_interval(mins => $8::int))",
        conversation_id,
        ids["tenant"],
        contact_id,
        ids["channel"],
        ids["agent"],
        state,
        # NULL minutes -> NULL last_message_at
        last,
        created,
    )
    return conversation_id


async def _insert_handoff(conn, ids, conversation_id) -> uuid.UUID:
    handoff_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO handoffs (id, tenant_id, conversation_id, requested_by)"
        " VALUES ($1, $2, $3, 'agent')",
        handoff_id,
        ids["tenant"],
        conversation_id,
    )
    return handoff_id


async def _insert_contact(conn, ids, user: str) -> uuid.UUID:
    contact_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO contacts (id, tenant_id, channel_id, external_user_id)"
        " VALUES ($1, $2, $3, $4)",
        contact_id,
        ids["tenant"],
        ids["channel"],
        user,
    )
    return contact_id


async def test_upgrade_closes_duplicates_and_adds_the_index():
    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0011")

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

            # Contact A: three open duplicates, one HUMAN_HANDOFF (the oldest
            # and least recently active), plus an already closed one.
            a = await _insert_contact(conn, ids, "user-a")
            a_handoff = await _insert_conversation(
                conn, ids, a, "HUMAN_HANDOFF", created=300, last=200
            )
            a_recent = await _insert_conversation(conn, ids, a, "ACTIVE_AI", created=100, last=1)
            a_waiting = await _insert_conversation(conn, ids, a, "WAITING", created=50, last=None)
            a_closed = await _insert_conversation(conn, ids, a, "CLOSED", created=400, last=350)
            kept_handoff = await _insert_handoff(conn, ids, a_handoff)
            recent_handoff = await _insert_handoff(conn, ids, a_recent)
            waiting_handoff = await _insert_handoff(conn, ids, a_waiting)

            # Contact B: no HUMAN_HANDOFF; the most recently active wins
            # (last_message_at, else created_at), not the newest created.
            b = await _insert_contact(conn, ids, "user-b")
            b_active = await _insert_conversation(conn, ids, b, "ACTIVE_AI", created=500, last=2)
            b_newer = await _insert_conversation(conn, ids, b, "ACTIVE_AI", created=10, last=None)
            b_old = await _insert_conversation(conn, ids, b, "WAITING", created=600, last=400)

            # Contact C: one open conversation, untouched.
            c = await _insert_contact(conn, ids, "user-c")
            c_only = await _insert_conversation(conn, ids, c, "ACTIVE_AI", created=5, last=1)
        finally:
            await conn.close()

        _alembic(database_url, "upgrade", "head")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            states = {
                row["id"]: row["state"]
                for row in await conn.fetch("SELECT id, state::text AS state FROM conversations")
            }
            assert states == {
                a_handoff: "HUMAN_HANDOFF",
                a_recent: "CLOSED",
                a_waiting: "CLOSED",
                a_closed: "CLOSED",
                b_active: "ACTIVE_AI",
                b_newer: "CLOSED",
                b_old: "CLOSED",
                c_only: "ACTIVE_AI",
            }

            resolved = {
                row["id"]: row["resolved_at"]
                for row in await conn.fetch("SELECT id, resolved_at FROM handoffs")
            }
            assert resolved[kept_handoff] is None
            assert resolved[recent_handoff] is not None
            assert resolved[waiting_handoff] is not None

            # Nothing deleted.
            assert await conn.fetchval("SELECT count(*) FROM conversations") == 8
            assert await conn.fetchval("SELECT count(*) FROM handoffs") == 3

            indexdef = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes"
                " WHERE indexname = 'uq_conversations_open_contact_channel'"
            )
            assert indexdef is not None
            assert "UNIQUE" in indexdef
            assert "(tenant_id, contact_id, channel_id)" in indexdef
            assert "CLOSED" in indexdef
        finally:
            await conn.close()

        _alembic(database_url, "downgrade", "0011")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert (
                await conn.fetchval(
                    "SELECT 1 FROM pg_indexes"
                    " WHERE indexname = 'uq_conversations_open_contact_channel'"
                )
                is None
            )
            # Closed duplicates stay closed.
            assert (
                await conn.fetchval(
                    "SELECT state::text FROM conversations WHERE id = $1", a_recent
                )
                == "CLOSED"
            )
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
