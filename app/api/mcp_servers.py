"""CRUD REST for a tenant's MCP servers, plus a sync and a try call on request.

Registration validates the URL statically (the tool address checks, no DNS),
saves and commits, then discovers the tools once with no transaction held
across the call, and returns the server with its status. `auth_config` is
write-only, as a tool's: encrypted on the way in, never returned.
"""

import json
import re
import uuid
from contextlib import nullcontext
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, set_next_cursor
from app.api.tools import endpoint_problem
from app.config import get_settings
from app.db import end_transaction
from app.mcp.client import McpError, call_tool, exposed_name
from app.mcp.sync import sync_server
from app.models.mcp_server import McpServer, McpServerStatus
from app.models.tenant import Tenant
from app.repositories.base import parse_after
from app.repositories.mcp import McpServersRepository
from app.security import secrets as secrets_crypto
from app.tools.mcp import cut_text
from app.tools.validation import validate_arguments

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])

_NAME_RE = re.compile(r"^[a-z0-9_]{1,24}$")
_AUTH_TYPES = {"bearer", "header"}


def _check_name(value: str | None) -> str | None:
    if value is not None and not _NAME_RE.match(value):
        raise ValueError("name must match ^[a-z0-9_]{1,24}$")
    return value


def _check_auth(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return value
    if str(value.get("type") or "").lower() not in _AUTH_TYPES:
        raise ValueError("auth_config.type must be 'bearer' or 'header'")
    if not isinstance(value.get("token"), str) or not value["token"]:
        raise ValueError("auth_config.token must be a non-empty string")
    return value


class _ServerChecks(BaseModel):
    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _check_name(value)

    @field_validator("auth_config", check_fields=False)
    @classmethod
    def _auth(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_auth(value)


class McpServerCreate(_ServerChecks):
    name: str
    url: str = Field(max_length=2048)
    auth_config: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=60)


class McpServerUpdate(_ServerChecks):
    name: str | None = None
    url: str | None = Field(default=None, max_length=2048)
    auth_config: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=60)


class McpToolOut(BaseModel):
    name: str
    exposed_name: str
    description: str | None
    input_schema: dict[str, Any]
    read_only: bool


class McpServerOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    allowed_tools: list[str] | None
    timeout_seconds: int
    status: McpServerStatus
    error: str | None
    skipped: list[str]
    synced_at: datetime | None
    created_at: datetime
    tools: list[McpToolOut]


class McpCallIn(BaseModel):
    # The tool's name on the server (not the exposed `<server>__<tool>`).
    tool: str = Field(min_length=1, max_length=255)
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_id: str = Field(min_length=1, max_length=255)


class McpCallOut(BaseModel):
    ok: bool
    result: str | None
    error: str | None


def _repo(db: AsyncSession) -> McpServersRepository:
    return McpServersRepository(db)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found")


def _duplicate() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="MCP server name already exists"
    )


def _validate_url(url: str) -> None:
    problem = endpoint_problem(url)
    if problem is not None:
        raise HTTPException(
            status_code=422,
            detail=problem.replace("invalid endpoint", "invalid url", 1),
        )


def _encode_auth_config(auth_config: dict[str, Any] | None) -> str | None:
    if auth_config is None:
        return None
    return secrets_crypto.encrypt(json.dumps(auth_config))


async def _out(db: AsyncSession, servers: list[McpServer]) -> list[McpServerOut]:
    if not servers:
        return []
    tools = await _repo(db).tools(servers[0].tenant_id, [s.id for s in servers])
    by_server: dict[uuid.UUID, list[McpToolOut]] = {}
    names = {s.id: s.name for s in servers}
    for tool in tools:
        by_server.setdefault(tool.server_id, []).append(
            McpToolOut(
                name=tool.name,
                exposed_name=exposed_name(names[tool.server_id], tool.name),
                description=tool.description,
                input_schema=tool.input_schema or {},
                read_only=tool.read_only,
            )
        )
    return [
        McpServerOut(
            id=s.id,
            name=s.name,
            url=s.url,
            allowed_tools=s.allowed_tools,
            timeout_seconds=s.timeout_seconds,
            status=s.status,
            error=s.error,
            skipped=list(s.skipped or []),
            synced_at=s.synced_at,
            created_at=s.created_at,
            tools=by_server.get(s.id, []),
        )
        for s in servers
    ]


async def _sync_and_load(db: AsyncSession, tenant_id: uuid.UUID, server_id: uuid.UUID) -> McpServer:
    """Discover now (on the request's session, no transaction across the call)
    and return the server as stored afterwards."""
    await sync_server(lambda: nullcontext(db), tenant_id, server_id)
    server = await _repo(db).get(tenant_id, server_id)
    if server is None:  # deleted meanwhile
        raise _not_found()
    await db.refresh(server)
    return server


@router.post("", response_model=McpServerOut, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    payload: McpServerCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> McpServerOut:
    _validate_url(payload.url)
    data = payload.model_dump()
    data["auth_config_encrypted"] = _encode_auth_config(data.pop("auth_config"))
    try:
        server = await _repo(db).create(
            tenant.id, status=McpServerStatus.PENDING, skipped=[], **data
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _duplicate() from exc
    server = await _sync_and_load(db, tenant.id, server.id)
    return (await _out(db, [server]))[0]


@router.get("", response_model=list[McpServerOut])
async def list_mcp_servers(
    response: Response,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[McpServerOut]:
    after = parse_after(decode_cursor(cursor))
    rows = await _repo(db).list_page(tenant.id, limit=limit + 1, after=after)
    more = len(rows) > limit
    rows = rows[:limit]
    if more:
        set_next_cursor(
            response, {"created_at": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)}
        )
    return await _out(db, rows)


@router.get("/{server_id}", response_model=McpServerOut)
async def get_mcp_server(
    server_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> McpServerOut:
    server = await _repo(db).get(tenant.id, server_id)
    if server is None:
        raise _not_found()
    return (await _out(db, [server]))[0]


@router.patch("/{server_id}", response_model=McpServerOut)
async def update_mcp_server(
    server_id: uuid.UUID,
    payload: McpServerUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> McpServerOut:
    """Changing `url`, `auth_config`, `allowed_tools` or `timeout_seconds`
    discovers the tools again, as registration does."""
    changes = payload.model_dump(exclude_unset=True)
    for key in ("name", "url", "timeout_seconds"):
        if key in changes and changes[key] is None:
            changes.pop(key)
    if "url" in changes:
        _validate_url(changes["url"])
    if "auth_config" in changes:
        changes["auth_config_encrypted"] = _encode_auth_config(changes.pop("auth_config"))
    try:
        server = await _repo(db).update(tenant.id, server_id, **changes)
        if server is None:
            raise _not_found()
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _duplicate() from exc
    resync = {"url", "auth_config_encrypted", "allowed_tools", "timeout_seconds"}
    if resync & changes.keys():
        server = await _sync_and_load(db, tenant.id, server_id)
    return (await _out(db, [server]))[0]


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not await _repo(db).delete(tenant.id, server_id):
        raise _not_found()
    await db.commit()


@router.post("/{server_id}/sync", response_model=McpServerOut)
async def sync_mcp_server(
    server_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> McpServerOut:
    """Discover the server's tools now; the body shows the outcome."""
    if await _repo(db).get(tenant.id, server_id) is None:
        raise _not_found()
    server = await _sync_and_load(db, tenant.id, server_id)
    return (await _out(db, [server]))[0]


@router.post("/{server_id}/call", response_model=McpCallOut)
async def call_mcp_tool(
    server_id: uuid.UUID,
    payload: McpCallIn,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> McpCallOut:
    """Try one imported tool for `user_id`, outside any conversation.

    `_meta` holds only `parley/user_id` and a fresh `parley/call_id`; nothing
    is written to `tool_executions`. A tool's error result and a failure both
    come back as `ok: false` with the text in `error`.
    """
    repo = _repo(db)
    server = await repo.get(tenant.id, server_id)
    if server is None:
        raise _not_found()
    tools = await repo.tools(tenant.id, [server.id])
    tool = next((t for t in tools if t.name == payload.tool), None)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP tool not found")
    invalid = validate_arguments(tool.input_schema, payload.arguments)
    if invalid is not None:
        return McpCallOut(ok=False, result=None, error=invalid)
    meta = {"parley/user_id": payload.user_id, "parley/call_id": str(uuid.uuid4())}
    await end_transaction(db)
    try:
        answer = await call_tool(server, tool.name, payload.arguments, meta)
    except McpError as exc:
        return McpCallOut(ok=False, result=None, error=exc.reason)
    text, _ = cut_text(answer.text, get_settings().tool_max_response_bytes)
    if answer.is_error:
        return McpCallOut(ok=False, result=None, error=text or "the tool returned an error")
    return McpCallOut(ok=True, result=text, error=None)
