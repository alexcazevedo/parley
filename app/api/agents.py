"""CRUD REST for agents. Every route is scoped to the calling tenant."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.tenant import Tenant
from app.repositories.agents import AgentsRepository

router = APIRouter(prefix="/agents", tags=["agents"])


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
