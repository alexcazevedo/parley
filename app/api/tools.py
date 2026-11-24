"""CRUD REST for tools. `auth_config` is write-only: encrypted on the way in
via `app.security.secrets` and never returned by any response — this is a
hard requirement, not just a convention.
"""

import json
import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, set_next_cursor
from app.models.tenant import Tenant
from app.repositories.base import parse_after
from app.repositories.tools import ToolsRepository
from app.security import secrets as secrets_crypto
from app.tools.builtin import BUILTIN_TOOLS
from app.tools.ssrf import SSRFError, validate_tool_endpoint

router = APIRouter(prefix="/tools", tags=["tools"])


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _check_name(value: str | None) -> str | None:
    if value is None:
        return value
    if not _NAME_RE.match(value):
        raise ValueError("name must match ^[A-Za-z0-9_-]{1,64}$")
    if value in {spec.name for spec, _ in BUILTIN_TOOLS}:
        raise ValueError(f"name '{value}' is reserved for a built-in tool")
    return value


def _check_method(value: str | None) -> str | None:
    if value is None:
        return value
    upper = value.upper()
    if upper not in _METHODS:
        raise ValueError(f"method must be one of {sorted(_METHODS)}")
    return upper


class _ToolChecks(BaseModel):
    @field_validator("name", check_fields=False)
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _check_name(value)

    @field_validator("method", check_fields=False)
    @classmethod
    def _method(cls, value: str | None) -> str | None:
        return _check_method(value)


class ToolCreate(_ToolChecks):
    name: str
    description: str | None = Field(default=None, max_length=2000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    endpoint: str
    method: str = "POST"
    auth_config: dict[str, Any] | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=60)


class ToolUpdate(_ToolChecks):
    name: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    input_schema: dict[str, Any] | None = None
    endpoint: str | None = None
    method: str | None = None
    auth_config: dict[str, Any] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=60)


class ToolOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    input_schema: dict[str, Any]
    endpoint: str
    method: str
    timeout_seconds: int
    created_at: datetime

    model_config = {"from_attributes": True}


def _repo(db: AsyncSession) -> ToolsRepository:
    return ToolsRepository(db)


def _validate_endpoint(endpoint: str) -> None:
    """Static SSRF check (no DNS) before anything is written; the full check,
    with DNS, still runs on every call."""
    try:
        validate_tool_endpoint(endpoint)
    except SSRFError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid endpoint: {exc}",
        ) from exc


def _encode_auth_config(auth_config: dict[str, Any] | None) -> str | None:
    if auth_config is None:
        return None
    return secrets_crypto.encrypt(json.dumps(auth_config))


@router.post("", response_model=ToolOut, status_code=status.HTTP_201_CREATED)
async def create_tool(
    payload: ToolCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ToolOut:
    _validate_endpoint(payload.endpoint)
    data = payload.model_dump()
    auth_config = data.pop("auth_config")
    data["auth_config_encrypted"] = _encode_auth_config(auth_config)
    try:
        tool = await _repo(db).create(tenant.id, **data)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="tool name already exists"
        ) from exc
    await db.refresh(tool)
    return ToolOut.model_validate(tool)


@router.get("", response_model=list[ToolOut])
async def list_tools(
    response: Response,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ToolOut]:
    after = parse_after(decode_cursor(cursor))
    rows = await _repo(db).list_page(tenant.id, limit=limit + 1, after=after)
    more = len(rows) > limit
    rows = rows[:limit]
    if more:
        set_next_cursor(
            response, {"created_at": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)}
        )
    return [ToolOut.model_validate(t) for t in rows]


@router.get("/{tool_id}", response_model=ToolOut)
async def get_tool(
    tool_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ToolOut:
    tool = await _repo(db).get(tenant.id, tool_id)
    if tool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool not found")
    return ToolOut.model_validate(tool)


@router.patch("/{tool_id}", response_model=ToolOut)
async def update_tool(
    tool_id: uuid.UUID,
    payload: ToolUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ToolOut:
    changes = payload.model_dump(exclude_unset=True)
    if "endpoint" in changes:
        _validate_endpoint(changes["endpoint"] or "")
    if "auth_config" in changes:
        changes["auth_config_encrypted"] = _encode_auth_config(changes.pop("auth_config"))
    try:
        tool = await _repo(db).update(tenant.id, tool_id, **changes)
        if tool is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool not found")
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="tool name already exists"
        ) from exc
    await db.refresh(tool)
    return ToolOut.model_validate(tool)


@router.delete("/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(
    tool_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await _repo(db).delete(tenant.id, tool_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool not found")
    await db.commit()
