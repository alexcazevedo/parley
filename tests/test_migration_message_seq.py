"""Migration 0013 numbers existing messages in `(created_at, id)` order into
`messages.seq` and turns `summary_cursor` into `summary_last_seq`
(PRODUCT.md E4.8, ticket 01); downgrade gives the row count back.

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


async def _insert_conversation(conn, ids, user: str, summary_cursor) -> uuid.UUID:
    contact_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO contacts (id, tenant_id, channel_id, external_user_id)"
        " VALUES ($1, $2, $3, $4)",
        contact_id,
        ids["tenant"],
        ids["channel"],
        user,
    )
    conversation_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO conversations (id, tenant_id, contact_id, channel_id, agent_id, state,"
        " summary_cursor) VALUES ($1, $2, $3, $4, $5, 'ACTIVE_AI', $6)",
        conversation_id,
        ids["tenant"],
        contact_id,
        ids["channel"],
        ids["agent"],
        summary_cursor,
    )
    return conversation_id


async def _insert_message(conn, ids, conversation_id, content: str, minutes_ago: int):
    message_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO messages (id, tenant_id, conversation_id, channel_id, direction, role,"
        " type, content, created_at)"
        " VALUES ($1, $2, $3, $4, 'in', 'user', 'text', $5,"
        " now() - make_interval(mins => $6::int))",
        message_id,
        ids["tenant"],
        conversation_id,
        ids["channel"],
        content,
        minutes_ago,
    )
    return message_id


async def test_upgrade_numbers_messages_and_moves_the_summary_cursor():
    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0012")

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

            summarized = await _insert_conversation(conn, ids, "user-a", 3)
            untouched = await _insert_conversation(conn, ids, "user-b", 0)
            never = await _insert_conversation(conn, ids, "user-c", None)
            # Inserted out of created_at order on purpose.
            a = {
                "m4": await _insert_message(conn, ids, summarized, "m4", 10),
                "m1": await _insert_message(conn, ids, summarized, "m1", 40),
                "m3": await _insert_message(conn, ids, summarized, "m3", 20),
                "m5": await _insert_message(conn, ids, summarized, "m5", 5),
                "m2": await _insert_message(conn, ids, summarized, "m2", 30),
            }
            await _insert_message(conn, ids, untouched, "b1", 15)
            await _insert_message(conn, ids, never, "c1", 25)
        finally:
            await conn.close()

        _alembic(database_url, "upgrade", "head")

        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            rows = await conn.fetch("SELECT content, seq FROM messages ORDER BY seq")
            # Globally numbered in (created_at, id) order, from 1.
            assert [row["content"] for row in rows] == ["m1", "m2", "c1", "m3", "b1", "m4", "m5"]
            assert [row["seq"] for row in rows] == list(range(1, 8))

            seq_of = {
                row["id"]: row["seq"] for row in await conn.fetch("SELECT id, seq FROM messages")
            }
            last_seq = {
                row["id"]: row["summary_last_seq"]
                for row in await conn.fetch("SELECT id, summary_last_seq FROM conversations")
            }
            assert last_seq == {summarized: seq_of[a["m3"]], untouched: None, never: None}
            assert (
                await conn.fetchval(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'conversations' AND column_name = 'summary_cursor'"
                )
                is None
            )

            column = await conn.fetchrow(
                "SELECT is_nullable, column_default, data_type FROM information_schema.columns"
                " WHERE table_name = 'messages' AND column_name = 'seq'"
            )
            assert column["is_nullable"] == "NO"
            assert column["data_type"] == "bigint"
            assert "messages_seq" in column["column_default"]

            indexdef = await conn.fetchval(
                "SELECT indexdef FROM pg_indexes"
                " WHERE indexname = 'ix_messages_conversation_id_seq'"
            )
            assert indexdef is not None
            assert "(conversation_id, seq)" in indexdef

            # A new row continues after the numbered ones.
            new_id = await _insert_message(conn, ids, untouched, "b2", 60)
            assert await conn.fetchval("SELECT seq FROM messages WHERE id = $1", new_id) == 8
        finally:
            await conn.close()

        _alembic(database_url, "downgrade", "0012")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            cursors = {
                row["id"]: row["summary_cursor"]
                for row in await conn.fetch("SELECT id, summary_cursor FROM conversations")
            }
            assert cursors == {summarized: 3, untouched: None, never: None}
            assert (
                await conn.fetchval(
                    "SELECT 1 FROM information_schema.columns"
                    " WHERE table_name = 'messages' AND column_name = 'seq'"
                )
                is None
            )
            assert (
                await conn.fetchval("SELECT 1 FROM pg_class WHERE relname = 'messages_seq'")
                is None
            )
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
