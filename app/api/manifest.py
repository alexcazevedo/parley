"""Apply a manifest (one declarative file of agents, departments, tools and channels)
to the calling tenant, and serve its JSON Schema. Logic: `app/manifest/`.
"""

import json
from contextlib import nullcontext
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.manifest import yaml_io
from app.manifest.apply import ManifestError, apply_manifest, parse_manifest
from app.manifest.export import export_manifest
from app.manifest.schema import Manifest
from app.mcp.sync import sync_server
from app.models.tenant import Tenant

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/manifest", tags=["manifest"])

MAX_BODY_BYTES = 1_048_576  # 1 MiB, both formats
YAML_TYPES = {"application/yaml", "text/yaml"}


class ChangeOut(BaseModel):
    kind: str
    key: str
    # "create" | "update" | "adopt" (a hand-made item taken over) | "delete"
    # (a managed item no longer in the file) | "unchanged"
    action: str
    # The changed field names, for an update or an adoption.
    fields: list[str]
    # The item's id; null for a create in a dry run and for a delete that ran.
    id: str | None


class ApplyOut(BaseModel):
    dry_run: bool
    changes: list[ChangeOut]
    generated: dict[str, Any]


def problems_response(problems: list[dict[str, str]]) -> JSONResponse:
    """422 in the API's error format; `details` is `[{"field", "message"}]`."""
    request_id = structlog.contextvars.get_contextvars().get("request_id", "unknown")
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Validation failed",
                "request_id": request_id,
                "details": problems,
            }
        },
    )


@router.get("/schema")
async def manifest_schema() -> dict[str, Any]:
    return Manifest.model_json_schema()


class ExportOut(BaseModel):
    manifest: dict[str, Any]
    secrets_needed: list[str]
    warnings: list[str]


def _comment(text: str) -> str:
    return "# " + " ".join(text.split()) + "\n"


@router.get("", response_model=ExportOut, responses={200: {"content": {"application/yaml": {}}}})
async def export(
    format: Literal["json", "yaml"] = Query(default="json"),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """The tenant as a manifest. `format=yaml` returns the manifest alone, with
    `secrets_needed` and the warnings as leading comment lines."""
    result = await export_manifest(db, tenant)
    await db.rollback()  # read only: release the connection
    if format == "json":
        return {
            "manifest": result.manifest,
            "secrets_needed": result.secrets_needed,
            "warnings": result.warnings,
        }
    header = "# secrets_needed: " + (", ".join(result.secrets_needed) or "none") + "\n"
    header += "".join(_comment(f"warning: {w}") for w in result.warnings)
    return Response(content=header + yaml_io.dump(result.manifest), media_type="application/yaml")


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="request body too large"
    )


async def _read_body(request: Request) -> bytes:
    """The body, cut off with a 413 once more than `MAX_BODY_BYTES` arrive
    (counted as received, whatever `Content-Length` says)."""
    declared = request.headers.get("content-length", "")
    if declared.strip().isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _too_large()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_body(body: bytes, content_type: str) -> Any:
    """The body as data, or a 422 response for text that is not a mapping."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError([{"field": "(root)", "message": "the body is not UTF-8"}]) from None
    if content_type in YAML_TYPES:
        try:
            return yaml_io.load(text)
        except yaml_io.YamlError as exc:
            where = f"line {exc.line}: " if exc.line is not None else ""
            raise ManifestError(
                [{"field": exc.path, "message": f"invalid YAML, {where}{exc.message}"}]
            ) from None
    try:
        return json.loads(text)
    except RecursionError:
        raise ManifestError([{"field": "(root)", "message": "nested too deeply"}]) from None
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        where = f"line {line}: " if line is not None else ""
        raise ManifestError(
            [{"field": "(root)", "message": f"invalid JSON, {where}{getattr(exc, 'msg', 'error')}"}]
        ) from None


@router.post(
    "/apply",
    response_model=ApplyOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"type": "object"}},
                "application/yaml": {"schema": {"type": "string"}},
                "text/yaml": {"schema": {"type": "string"}},
            },
        }
    },
)
async def apply(
    request: Request,
    dry_run: bool = Query(default=False),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> Any:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json" and content_type not in YAML_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Content-Type must be application/json, application/yaml or text/yaml",
        )
    raw = await _read_body(request)
    try:
        body = _parse_body(raw, content_type)
        manifest = parse_manifest(body)
        result = await apply_manifest(db, tenant, manifest, dry_run=dry_run)
    except ManifestError as exc:
        await db.rollback()
        return problems_response(exc.problems)
    except IntegrityError as exc:  # a concurrent API write took a name
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a conflicting change was made during the apply; apply again",
        ) from exc
    except BaseException:
        await db.rollback()
        raise
    if dry_run:
        await db.rollback()
    else:
        await db.commit()
        for server_id in result.sync_server_ids:
            # After the commit, so no network call runs inside the apply
            # transaction. A failed sync is the server's `error` status.
            try:
                await sync_server(lambda: nullcontext(db), tenant.id, server_id)
            except Exception:
                logger.exception("manifest.mcp_sync_error", server_id=str(server_id))
                await db.rollback()
    return result.as_dict()
