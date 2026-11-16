"""CRUD REST for agents. Every route is scoped to the calling tenant."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.tenant import Tenant
from app.repositories.agents import AgentsRepository

router = APIRouter(prefix="/agents", tags=["agents"])


def _strip_never_item(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 300:
        raise ValueError("each never item must be 1-300 characters")
    return value


Scope = Annotated[str, Field(max_length=2000)]
NeverList = Annotated[list[Annotated[str, AfterValidator(_strip_never_item)]], Field(max_length=50)]
RefusalMessage = Annotated[str, Field(min_length=1, max_length=1000)]


class AgentCreate(BaseModel):
    name: str
    instructions: str
    provider: str = "openai_compatible"
    model: str
    temperature: float = 0.7
    max_tokens: int = 1024
    max_tool_iterations: int = 5
    memory_enabled: bool = True
    handoff_enabled: bool = True
    tool_names: list[str] = Field(default_factory=list)
    context_message_limit: int = 20
    summary_threshold: int = 40
    boundary_scope: Scope | None = None
    boundary_never: NeverList = Field(default_factory=list)
    boundary_refusal_message: RefusalMessage | None = None
    boundary_check_enabled: bool = True


class AgentUpdate(BaseModel):
    name: str | None = None
    instructions: str | None = None
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_tool_iterations: int | None = None
    memory_enabled: bool | None = None
    handoff_enabled: bool | None = None
    tool_names: list[str] | None = None
    context_message_limit: int | None = None
    summary_threshold: int | None = None
    boundary_scope: Scope | None = None
    boundary_never: NeverList | None = None
    boundary_refusal_message: RefusalMessage | None = None
    boundary_check_enabled: bool | None = None


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
    context_message_limit: int
    summary_threshold: int
    boundary_scope: str | None
    boundary_never: list[str]
    boundary_refusal_message: str | None
    boundary_check_enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


def _repo(db: AsyncSession) -> AgentsRepository:
    return AgentsRepository(db)


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> AgentOut:
    agent = await _repo(db).create(tenant.id, **payload.model_dump())
    await db.commit()
    await db.refresh(agent)
    return AgentOut.model_validate(agent)


@router.get("", response_model=list[AgentOut])
async def list_agents(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[AgentOut]:
    agents = await _repo(db).list(tenant.id)
    return [AgentOut.model_validate(a) for a in agents]


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
    # Non-nullable columns: an explicit null means "leave as is".
    for key in ("boundary_never", "boundary_check_enabled"):
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
