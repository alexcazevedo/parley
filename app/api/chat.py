"""Bootstrap endpoint for the demo web chat page.

No auth — dev/demo shortcut (prototype mode, see PRODUCT.md). Seeds a fixed
demo tenant (identified by a FIXED plaintext API key, since keys are only
stored hashed — see `app/models/tenant.py`) with the same tenant/tool/agent/
channel shape as `scripts/demo.py`, idempotently: a second call returns the
same tenant/channel/agent instead of creating duplicates.
"""

from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.config import get_settings
from app.models.channel import Channel, ChannelType
from app.models.tenant import Tenant
from app.repositories.agents import AgentsRepository
from app.repositories.channels import ChannelsRepository
from app.repositories.tools import ToolsRepository
from app.security.auth import hash_api_key

router = APIRouter(prefix="/chat", tags=["chat"])

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("", include_in_schema=False)
async def chat_page() -> FileResponse:
    """Serves the self-contained demo web chat page."""
    return FileResponse(_STATIC_DIR / "chat.html")


TENANT_NAME = "Web Chat Demo"
AGENT_NAME = "Atendente Acme"
CHANNEL_NAME = "Web Demo"
AGENT_INSTRUCTIONS = (
    "Você é o atendente virtual da Acme. Responda sempre em português, de "
    "forma cordial e objetiva. Quando o cliente mencionar um número de "
    "pedido, use a ferramenta get_order para consultar o status antes de "
    "responder. Se o cliente pedir para falar com uma pessoa/atendente "
    "humano, use a ferramenta handoff_to_human em vez de tentar resolver "
    "sozinho."
)


class BootstrapOut(BaseModel):
    api_key: str
    channel_id: str
    agent_id: str


async def _get_or_create_webchat_channel(db: AsyncSession, tenant: Tenant) -> Channel:
    channels_repo = ChannelsRepository(db)
    existing = await channels_repo.list(tenant.id, type=ChannelType.WEBCHAT)
    if existing:
        channel = existing[0]
        # Idempotent upgrade: if config lacks humanize key, set it and commit.
        if "humanize" not in channel.config:
            # Merge the new key into the config dict and update
            new_config = {**channel.config, "humanize": True}
            updated_channel = await channels_repo.update(tenant.id, channel.id, config=new_config)
            channel = cast(Channel, updated_channel)

        # Idempotent upgrade: flip the demo agent's sentiment escalation flag
        # on if an earlier bootstrap left it off. Plain bool column — simple
        # setattr + commit, not the JSONB merge-and-write dance above.
        agents_repo = AgentsRepository(db)
        agent = await agents_repo.get(tenant.id, channel.agent_id)
        if agent is not None and not agent.sentiment_escalation_enabled:
            agent.sentiment_escalation_enabled = True

        await db.commit()
        return channel

    settings = get_settings()
    tools_repo = ToolsRepository(db)
    tool = await tools_repo.get_by_name(tenant.id, "get_order")
    if tool is None:
        await tools_repo.create(
            tenant.id,
            name="get_order",
            description="Consulta o status de um pedido a partir do seu id.",
            input_schema={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
            endpoint="http://mock-tools:9000/orders",
            method="GET",
            timeout_seconds=5,
        )

    agents_repo = AgentsRepository(db)
    agent = await agents_repo.create(
        tenant.id,
        name=AGENT_NAME,
        instructions=AGENT_INSTRUCTIONS,
        model=settings.llm_model,
        tool_names=["get_order"],
        memory_enabled=True,
        handoff_enabled=True,
        sentiment_escalation_enabled=True,
    )
    await db.flush()

    channel = await channels_repo.create(
        tenant.id,
        type=ChannelType.WEBCHAT,
        name=CHANNEL_NAME,
        agent_id=agent.id,
        config={"humanize": True},
    )
    await db.commit()
    await db.refresh(channel)
    return channel


@router.post("/bootstrap", response_model=BootstrapOut)
async def bootstrap_chat(db: AsyncSession = Depends(get_db)) -> BootstrapOut:
    settings = get_settings()
    plaintext_key = settings.webchat_demo_api_key
    key_hash = hash_api_key(plaintext_key)

    result = await db.execute(select(Tenant).where(Tenant.api_key_hash == key_hash))
    tenant = result.scalar_one_or_none()

    if tenant is None:
        tenant = Tenant(name=TENANT_NAME, api_key_hash=key_hash)
        db.add(tenant)
        await db.flush()

    channel = await _get_or_create_webchat_channel(db, tenant)
    await db.commit()

    return BootstrapOut(
        api_key=plaintext_key,
        channel_id=str(channel.id),
        agent_id=str(channel.agent_id),
    )
