"""CRUD REST for agents. Every route is scoped to the calling tenant."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, set_next_cursor
from app.models.agent import DEFAULT_HANDOFF_MESSAGE
from app.models.tenant import Tenant
from app.repositories.agents import AgentsRepository
from app.repositories.base import parse_after

router = APIRouter(prefix="/agents", tags=["agents"])

UNKNOWN_PROVIDER = "unknown provider: use openai_compatible, openai, anthropic or google"


def _strip_never_item(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 300:
        raise ValueError("each never item must be 1-300 characters")
    return value


Scope = Annotated[str, Field(max_length=2000)]
NeverList = Annotated[list[Annotated[str, AfterValidator(_strip_never_item)]], Field(max_length=50)]
RefusalMessage = Annotated[str, Field(min_length=1, max_length=1000)]
HandoffMessage = Annotated[str, Field(min_length=1, max_length=1000)]
FailureText = Annotated[str, Field(min_length=1, max_length=1000)]
Name = Annotated[str, Field(min_length=1, max_length=255)]
Temperature = Annotated[float, Field(ge=0, le=2)]
MaxTokens = Annotated[int, Field(ge=1, le=32768)]
MaxToolIterations = Annotated[int, Field(ge=1, le=20)]
ContextLimit = Annotated[int, Field(ge=1, le=200)]
SummaryThreshold = Annotated[int, Field(ge=0, le=1000)]
SessionGapHours = Annotated[int, Field(ge=1, le=720)]
SessionRecapLimit = Annotated[int, Field(ge=0, le=10)]
SessionRecapMaxAgeDays = Annotated[int, Field(ge=1, le=3650)]


class AgentCreate(BaseModel):
    name: Name
    instructions: str
    provider: str = "openai_compatible"
    model: Name
    temperature: Temperature = 0.7
    max_tokens: MaxTokens = 1024
    max_tool_iterations: MaxToolIterations = 5
    memory_enabled: bool = True
    handoff_enabled: bool = True
    tool_names: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    context_message_limit: ContextLimit = 20
    summary_threshold: SummaryThreshold = 40
    boundary_scope: Scope | None = None
    boundary_never: NeverList = Field(default_factory=list)
    boundary_refusal_message: RefusalMessage | None = None
    boundary_check_enabled: bool = True
    sentiment_escalation_enabled: bool = False
    knowledge_enabled: bool = False
    handoff_message: HandoffMessage | None = None
    error_message: FailureText | None = None
    unsupported_media_message: FailureText | None = None
    session_memory_enabled: bool = True
    session_gap_hours: SessionGapHours = 48
    session_recap_limit: SessionRecapLimit = 3
    session_recap_max_age_days: SessionRecapMaxAgeDays = 90


class AgentUpdate(BaseModel):
    name: Name | None = None
    instructions: str | None = None
    provider: str | None = None
    model: Name | None = None
    temperature: Temperature | None = None
    max_tokens: MaxTokens | None = None
    max_tool_iterations: MaxToolIterations | None = None
    memory_enabled: bool | None = None
    handoff_enabled: bool | None = None
    tool_names: list[str] | None = None
    mcp_servers: list[str] | None = None
    context_message_limit: ContextLimit | None = None
    summary_threshold: SummaryThreshold | None = None
    boundary_scope: Scope | None = None
    boundary_never: NeverList | None = None
    boundary_refusal_message: RefusalMessage | None = None
    boundary_check_enabled: bool | None = None
    sentiment_escalation_enabled: bool | None = None
    knowledge_enabled: bool | None = None
    handoff_message: HandoffMessage | None = None
    # An explicit null clears these two back to the default text.
    error_message: FailureText | None = None
    unsupported_media_message: FailureText | None = None
    session_memory_enabled: bool | None = None
    session_gap_hours: SessionGapHours | None = None
    session_recap_limit: SessionRecapLimit | None = None
    session_recap_max_age_days: SessionRecapMaxAgeDays | None = None


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    instructions: str
    provider: str
    model: str
    temperature: float
    max_tokens: int
    max_tool_iterations: int
    memory_enabled: bool
    handoff_enabled: bool
    tool_names: list[str]
    mcp_servers: list[str]
    context_message_limit: int
    summary_threshold: int
    boundary_scope: str | None
    boundary_never: list[str]
    boundary_refusal_message: str | None
    boundary_check_enabled: bool
    sentiment_escalation_enabled: bool
    knowledge_enabled: bool
    handoff_message: str
    error_message: str | None
    unsupported_media_message: str | None
    session_memory_enabled: bool
    session_gap_hours: int
    session_recap_limit: int
    session_recap_max_age_days: int
    created_at: datetime

    model_config = {"from_attributes": True}


def _repo(db: AsyncSession) -> AgentsRepository:
    return AgentsRepository(db)


def provider_problem(provider: str) -> str | None:
    """Why this server cannot serve `provider`, or None (shared with the manifest).

    The text names a setting, never its value.
    """
    from app.llm.providers.native import NATIVE_PROVIDERS, has_credentials, key_setting

    if provider == "openai_compatible":
        return None
    if provider not in NATIVE_PROVIDERS:
        return UNKNOWN_PROVIDER
    if not has_credentials(provider):
        setting = key_setting(provider)
        return f"provider '{provider}' is not configured on this server: {setting} is not set"
    return None


def _check_provider(provider: str | None) -> None:
    problem = provider_problem(provider) if provider is not None else None
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)


def agent_values(payload: AgentCreate) -> dict[str, Any]:
    """The column values of a new agent (shared with `app/manifest/apply.py`).

    A missing `handoff_message` is the column default, written explicitly.
    """
    values = payload.model_dump()
    if values["handoff_message"] is None:
        values["handoff_message"] = DEFAULT_HANDOFF_MESSAGE
    return values


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    _check_provider(payload.provider)
    agent = await _repo(db).create(tenant.id, **agent_values(payload))
    await db.commit()
    await db.refresh(agent)
    return AgentOut.model_validate(agent)


@router.get("", response_model=list[AgentOut])
async def list_agents(
    response: Response,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[AgentOut]:
    after = parse_after(decode_cursor(cursor))
    rows = await _repo(db).list_page(tenant.id, limit=limit + 1, after=after)
    more = len(rows) > limit
    rows = rows[:limit]
    if more:
        set_next_cursor(
            response, {"created_at": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)}
        )
    return [AgentOut.model_validate(a) for a in rows]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    agent = await _repo(db).get(tenant.id, agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
    return AgentOut.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    changes = payload.model_dump(exclude_unset=True)
    _check_provider(changes.get("provider"))
    # Non-nullable columns: an explicit null means "leave as is".
    for key in (
        "boundary_never",
        "boundary_check_enabled",
        "sentiment_escalation_enabled",
        "knowledge_enabled",
        "handoff_message",
        "mcp_servers",
        "session_memory_enabled",
        "session_gap_hours",
        "session_recap_limit",
        "session_recap_max_age_days",
    ):
        if key in changes and changes[key] is None:
            del changes[key]
    agent = await _repo(db).update(tenant.id, agent_id, **changes)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
    await db.commit()
    await db.refresh(agent)
    return AgentOut.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await _repo(db).delete(tenant.id, agent_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
    await db.commit()
