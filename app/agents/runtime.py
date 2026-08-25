"""The agent loop: context -> LLM -> tools -> reply.

One `run_turn` call is one `AgentRun` row. The loop alternates model calls and
tool executions until the model answers with text or we hit
`agent.max_tool_iterations`. Everything it persists (the AgentRun, the
tool-call/tool-result messages) goes through the session the turn owns, so a
failed turn rolls back as a unit — `process_turn` is the only committer.
"""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.context import ContextBuilder
from app.config import Settings
from app.llm.base import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, LLMMessage, LLMProvider, ToolCall
from app.llm.factory import get_provider, resolve_model
from app.memory.service import MemoryService
from app.models.agent import Agent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.message import Message, MessageDirection, MessageRole
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)

# Kept as module constants rather than agent columns: they are product copy, not
# per-tenant configuration, and adding columns is outside this ticket.
MAX_ITERATIONS_MESSAGE = "Desculpe, não consegui concluir. Vou encaminhar."
ERROR_MESSAGE = "Desculpe, tive um problema técnico. Pode tentar novamente?"
EMPTY_REPLY_MESSAGE = "Desculpe, não entendi. Pode reformular?"

# `AgentRunStatus` has no `max_iterations` member and its Postgres enum lives in
# a migration this ticket must not touch, so the capped run is recorded as
# ERROR carrying this marker. Promote it to a real status when the enum moves.
MAX_ITERATIONS_ERROR = "max_iterations"

TURN_TIMEOUT_SECONDS = 60


class AgentRuntime:
    """A `SessionAwareTurnRuntime` (see app/conversations/turn.py).

    `provider` is normally left None so each agent gets its own provider from
    `app.llm.factory`; tests inject a `FakeLLMProvider` instead.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider | None = None,
        registry: ToolRegistry | None = None,
        settings: Settings | None = None,
        context_builder: ContextBuilder | None = None,
        timeout_seconds: float = TURN_TIMEOUT_SECONDS,
    ) -> None:
        self._provider = provider
        self._registry = registry or ToolRegistry()
        self._settings = settings
        self._context_builder = context_builder or ContextBuilder()
        self._timeout_seconds = timeout_seconds

    async def run_turn(
        self, session: AsyncSession, conversation: Conversation, input_text: str
    ) -> str:
        agent = await session.get(Agent, conversation.agent_id)
        if agent is None:
            raise RuntimeError(f"conversation {conversation.id} points at a missing agent")

        run = AgentRun(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            status=AgentRunStatus.RUNNING,
            input_text=input_text,
        )
        session.add(run)
        await session.flush()

        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                output = await self._loop(session, agent, conversation, run, input_text)
        except Exception as exc:  # a failed turn still owes the user a reply
            run.status = AgentRunStatus.ERROR
            run.error = f"{type(exc).__name__}: {exc}"[:2000]
            output = ERROR_MESSAGE
            logger.exception(
                "agent.run_failed", conversation_id=str(conversation.id), agent_run_id=str(run.id)
            )

        run.output_text = output
        run.latency_ms = int((time.perf_counter() - started) * 1000)
        await session.flush()
        logger.info(
            "agent.run_finished",
            conversation_id=str(conversation.id),
            agent_run_id=str(run.id),
            status=str(run.status),
            llm_calls=run.llm_calls,
            tool_calls=run.tool_calls,
            latency_ms=run.latency_ms,
        )
        return output

    async def _loop(
        self,
        session: AsyncSession,
        agent: Agent,
        conversation: Conversation,
        run: AgentRun,
        input_text: str,
    ) -> str:
        provider = self._provider or get_provider(agent, self._settings)
        model = resolve_model(agent, self._settings)
        tool_specs = await self._registry.specs(agent, session)
        memories = await self._load_memories(session, agent, conversation)
        history = await self._recent_messages(session, agent, conversation)
        messages = self._context_builder.build(
            agent, conversation, history, memories, tool_specs
        )
        if not any(message.role == ROLE_USER for message in messages):
            # The batch isn't visible on this session (or was pruned); the model
            # still needs the turn's input.
            messages.append(LLMMessage(role=ROLE_USER, content=input_text))

        prompt_tokens = 0
        completion_tokens = 0
        ctx = ToolContext(
            session=session,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            contact_id=conversation.contact_id,
            agent=agent,
            agent_run_id=run.id,
        )

        for _ in range(max(1, agent.max_tool_iterations or 1)):
            response = await provider.complete(
                messages,
                tool_specs,
                model=model,
                temperature=agent.temperature,
                max_tokens=agent.max_tokens,
            )
            run.llm_calls += 1
            if response.usage is not None:
                prompt_tokens += response.usage.prompt_tokens
                completion_tokens += response.usage.completion_tokens
                run.prompt_tokens = prompt_tokens
                run.completion_tokens = completion_tokens

            if not response.tool_calls:
                run.status = AgentRunStatus.SUCCESS
                return (response.content or "").strip() or EMPTY_REPLY_MESSAGE

            messages.append(
                LLMMessage(
                    role=ROLE_ASSISTANT, content=response.content, tool_calls=response.tool_calls
                )
            )
            self._persist_tool_calls(session, conversation, run, response.tool_calls)

            terminal_text: str | None = None
            for call in response.tool_calls:
                result = await self._registry.execute(call.name, call.arguments, ctx)
                run.tool_calls += 1
                content = result.to_content()
                logger.info(
                    "agent.tool_executed",
                    conversation_id=str(conversation.id),
                    tool=call.name,
                    ok=result.ok,
                )
                self._persist_tool_result(session, conversation, run, call, content)
                messages.append(
                    LLMMessage(role=ROLE_TOOL, content=content, tool_call_id=call.id)
                )
                # A tool (e.g. `handoff_to_human`) can end the loop on the spot
                # instead of handing control back to the model — see
                # app/tools/builtin.py and app/tools/base.py's `ToolResult`.
                if result.ok and result.meta.get("terminal"):
                    terminal_text = result.meta.get("final_text") or EMPTY_REPLY_MESSAGE
            await session.flush()

            if terminal_text is not None:
                run.status = AgentRunStatus.SUCCESS
                return terminal_text

        run.status = AgentRunStatus.ERROR
        run.error = MAX_ITERATIONS_ERROR
        logger.warning(
            "agent.max_iterations",
            conversation_id=str(conversation.id),
            max_tool_iterations=agent.max_tool_iterations,
        )
        return MAX_ITERATIONS_MESSAGE

    async def _recent_messages(
        self, session: AsyncSession, agent: Agent, conversation: Conversation
    ) -> list[Message]:
        limit = agent.context_message_limit or 20
        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    async def _load_memories(
        self, session: AsyncSession, agent: Agent, conversation: Conversation
    ) -> list[Memory]:
        """Long-term facts folded into the system prompt (see `ContextBuilder`).

        Gated by `agent.memory_enabled` — the same flag that gates the
        `remember` tool in `app/tools/builtin.py`, so a memory-disabled agent
        neither writes nor reads long-term facts.
        """
        if not agent.memory_enabled:
            return []
        return await MemoryService(session).get_contact_memories(
            conversation.tenant_id, conversation.contact_id
        )

    def llm_provider(self, agent: Agent) -> LLMProvider:
        """The provider this runtime would use for `agent`.

        Exposed so `app.conversations.turn` can run the post-turn summarizer
        with the exact same (possibly scripted, in tests) provider the turn
        itself just used, instead of constructing an unrelated one.
        """
        return self._provider or get_provider(agent, self._settings)

    def _persist_tool_calls(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: AgentRun,
        calls: list[ToolCall],
    ) -> None:
        """Record the assistant's tool-call step so later turns can replay it."""
        payload: dict[str, Any] = {
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls
            ]
        }
        summary = ", ".join(
            f"{call.name}({json.dumps(call.arguments, ensure_ascii=False, default=str)})"
            for call in calls
        )
        session.add(
            Message(
                id=uuid.uuid4(),
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                channel_id=conversation.channel_id,
                direction=MessageDirection.OUT,
                role=MessageRole.ASSISTANT,
                type="tool_call",
                content=summary,
                raw_payload=payload,
                agent_run_id=run.id,
                tool_call_id=calls[0].id if calls else None,
                # Explicit timestamps: Postgres `now()` is the *transaction*
                # timestamp, so every row a turn writes would otherwise tie and
                # replay out of order.
                created_at=datetime.now(UTC),
            )
        )

    def _persist_tool_result(
        self,
        session: AsyncSession,
        conversation: Conversation,
        run: AgentRun,
        call: ToolCall,
        content: str,
    ) -> None:
        session.add(
            Message(
                id=uuid.uuid4(),
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                channel_id=conversation.channel_id,
                direction=MessageDirection.IN,
                role=MessageRole.TOOL,
                type="tool_result",
                content=content,
                agent_run_id=run.id,
                tool_call_id=call.id,
                created_at=datetime.now(UTC),
            )
        )
