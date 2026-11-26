"""Tenant secrets: named, encrypted, listed by name, never read back."""

import os
import uuid

from sqlalchemy import func, select

from app.models.tenant_secret import TenantSecret
from app.repositories.secrets import SecretsRepository

PLAINTEXT = "sk-live-super-secret-value"


async def _tenant(client) -> tuple[dict, uuid.UUID]:
    response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    body = response.json()
    return {"X-API-Key": body["api_key"]}, uuid.UUID(body["id"])


async def _put(client, headers, name, value=PLAINTEXT):
    return await client.put(f"/secrets/{name}", json={"value": value}, headers=headers)


async def test_set_then_list_has_the_name_and_no_value(client, db_session):
    headers, _ = await _tenant(client)
    response = await _put(client, headers, "crm_token")
    assert response.status_code == 204 and response.content == b""
    await _put(client, headers, "api_key")

    listed = await client.get("/secrets", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert [r["name"] for r in rows] == ["api_key", "crm_token"]
    assert all(set(r) == {"name", "created_at", "updated_at"} for r in rows)
    assert PLAINTEXT not in listed.text


async def test_set_twice_keeps_one_row_and_moves_updated_at(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "crm_token", "one")
    first = (await client.get("/secrets", headers=headers)).json()[0]
    await _put(client, headers, "crm_token", "two")
    second = (await client.get("/secrets", headers=headers)).json()[0]

    assert len((await client.get("/secrets", headers=headers)).json()) == 1
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]
    resolved = await SecretsRepository(db_session).resolve(tenant_id, {"crm_token"})
    assert resolved == {"crm_token": "two"}


async def test_stored_column_is_not_the_plaintext_and_resolve_returns_it(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "crm_token")

    stored = await db_session.scalar(
        select(TenantSecret.value_encrypted).where(TenantSecret.tenant_id == tenant_id)
    )
    assert stored and PLAINTEXT not in stored
    repo = SecretsRepository(db_session)
    assert await repo.resolve(tenant_id, {"crm_token", "missing"}) == {"crm_token": PLAINTEXT}
    assert await repo.resolve(tenant_id, set()) == {}


async def test_another_tenant_sees_and_resolves_nothing(client, db_session):
    headers, tenant_id = await _tenant(client)
    other, other_id = await _tenant(client)
    await _put(client, headers, "crm_token")

    assert (await client.get("/secrets", headers=other)).json() == []
    assert await SecretsRepository(db_session).resolve(other_id, {"crm_token"}) == {}
    assert (await client.delete("/secrets/crm_token", headers=other)).status_code == 404
    assert len((await client.get("/secrets", headers=headers)).json()) == 1


async def test_bad_names_and_values_are_422_and_never_echoed(client, db_session):
    headers, _ = await _tenant(client)
    for name in ("Upper", "has-dash", "a" * 65, "with space"):
        assert (await _put(client, headers, name)).status_code == 422, name
    assert (await _put(client, headers, "a" * 64)).status_code == 204
    assert (await _put(client, headers, "ok", "")).status_code == 422
    too_long = await _put(client, headers, "ok", "x" * 8193)
    assert too_long.status_code == 422
    assert "x" * 100 not in too_long.text
    assert (await _put(client, headers, "ok", "x" * 8192)).status_code == 204
    assert (await client.delete("/secrets/Bad-Name", headers=headers)).status_code == 422


async def test_delete_then_delete_is_204_then_404(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "crm_token")
    assert (await client.delete("/secrets/crm_token", headers=headers)).status_code == 204
    gone = await client.delete("/secrets/crm_token", headers=headers)
    assert gone.status_code == 404
    assert gone.json()["error"]["message"] == "secret not found"
    count = await db_session.scalar(
        select(func.count()).select_from(TenantSecret).where(TenantSecret.tenant_id == tenant_id)
    )
    assert count == 0


async def test_requires_an_api_key(client, db_session):
    assert (await client.get("/secrets")).status_code in (401, 403)


async def test_migration_0019_upgrades_and_downgrades():
    import asyncpg

    from tests.conftest import _ASYNCPG_ADMIN_DSN
    from tests.test_migration_open_conversation import _alembic

    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0019")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            indexes = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'tenant_secrets'"
            )
            assert {"ix_tenant_secrets_tenant_id", "uq_tenant_secrets_tenant_name"} <= {
                r["indexname"] for r in indexes
            }
        finally:
            await conn.close()
        _alembic(database_url, "downgrade", "0018")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await conn.fetchval("SELECT to_regclass('tenant_secrets')") is None
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
