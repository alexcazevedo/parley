"""ContextBuilder: system prompt assembly + short-term history window.

Pure unit tests — the models are built in memory, nothing is persisted.
"""

import uuid

from app.agents.context import MEMORY_HEADER, SUMMARY_HEADER, ContextBuilder
from app.llm.base import ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_TOOL, ROLE_USER, ToolSpec
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.memory import Memory, MemoryType
from app.models.message import Message, MessageDirection, MessageRole

TENANT_ID = uuid.uuid4()


def _agent(**overrides) -> Agent:
    defaults = dict(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        name="Bot",
        instructions="Você é o atendente da Acme.",
        provider="openai_compatible",
        model="gpt-4o-mini",
        temperature=0.7,
        max_tokens=1024,
        max_tool_iterations=5,
        context_message_limit=20,
    )
    return Agent(**{**defaults, **overrides})


def _conversation(summary: str | None = None) -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        contact_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        summary=summary,
    )


def _message(role: MessageRole, content: str, **overrides) -> Message:
    defaults = dict(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        conversation_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        direction=MessageDirection.IN if role == MessageRole.USER else MessageDirection.OUT,
        role=role,
        type="text",
        content=content,
    )
    return Message(**{**defaults, **overrides})


def _memory(content: str) -> Memory:
    return Memory(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        contact_id=uuid.uuid4(),
        type=MemoryType.FACT,
        content=content,
        importance=3,
    )


async def test_system_prompt_is_just_instructions_without_memories_or_summary():
    built = await ContextBuilder().build(
        _agent(), _conversation(), [_message(MessageRole.USER, "Oi")]
    )

    assert built[0].role == ROLE_SYSTEM
    assert built[0].content == "Você é o atendente da Acme."
    assert [m.role for m in built] == [ROLE_SYSTEM, ROLE_USER]
    assert built[1].content == "Oi"


async def test_memories_and_summary_are_folded_into_the_system_prompt():
    built = await ContextBuilder().build(
        _agent(),
        _conversation(summary="O cliente perguntou sobre o pedido 12345."),
        [],
        memories=[_memory("Prefere ser chamado de Zé"), _memory("Cliente desde 2019")],
    )

    prompt = built[0].content
    assert prompt.startswith("Você é o atendente da Acme.")
    assert f"{MEMORY_HEADER}\n- Prefere ser chamado de Zé\n- Cliente desde 2019" in prompt
    assert f"{SUMMARY_HEADER}\nO cliente perguntou sobre o pedido 12345." in prompt


async def test_builder_sends_the_history_it_is_given_without_cutting_again():
    # The limit is applied by the runtime (tests/test_context_limit.py).
    history = [_message(MessageRole.USER, f"msg {i}") for i in range(10)]

    built = await ContextBuilder().build(_agent(context_message_limit=3), _conversation(), history)

    assert len(built) == 11  # system + 10
    assert [m.content for m in built[1:]] == [f"msg {i}" for i in range(10)]


async def test_operator_messages_speak_with_the_assistant_voice():
    built = await ContextBuilder().build(
        _agent(),
        _conversation(),
        [
            _message(MessageRole.USER, "Oi"),
            _message(MessageRole.HUMAN_OPERATOR, "Aqui é a Ana, posso ajudar?"),
        ],
    )

    assert [m.role for m in built] == [ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT]


async def test_tool_call_and_result_are_replayed_as_an_openai_transcript():
    call_id = "call_abc"
    history = [
        _message(MessageRole.USER, "cadê meu pedido 12345"),
        _message(
            MessageRole.ASSISTANT,
            'get_order({"order_id": "12345"})',
            type="tool_call",
            tool_call_id=call_id,
            raw_payload={
                "tool_calls": [
                    {"id": call_id, "name": "get_order", "arguments": {"order_id": "12345"}}
                ]
            },
        ),
        _message(
            MessageRole.TOOL,
            '{"ok": true, "data": {"status": "shipped"}}',
            type="tool_result",
            tool_call_id=call_id,
        ),
        _message(MessageRole.ASSISTANT, "Seu pedido está a caminho."),
    ]

    built = await ContextBuilder().build(_agent(), _conversation(), history)

    assert [m.role for m in built] == [
        ROLE_SYSTEM,
        ROLE_USER,
        ROLE_ASSISTANT,
        ROLE_TOOL,
        ROLE_ASSISTANT,
    ]
    assert built[2].content is None
    assert [(c.id, c.name, c.arguments) for c in built[2].tool_calls] == [
        (call_id, "get_order", {"order_id": "12345"})
    ]
    assert built[3].tool_call_id == call_id
    assert built[4].content == "Seu pedido está a caminho."


async def test_tool_result_orphaned_by_the_window_is_dropped():
    # The window starts *after* the assistant message that requested the call,
    # so the result has nothing to attach to and the API would reject it.
    history = [
        _message(MessageRole.TOOL, '{"ok": true}', type="tool_result", tool_call_id="call_gone"),
        _message(MessageRole.USER, "e aí?"),
    ]

    built = await ContextBuilder().build(_agent(), _conversation(), history)

    assert [m.role for m in built] == [ROLE_SYSTEM, ROLE_USER]


async def test_tool_specs_never_leak_into_the_prompt():
    spec = ToolSpec(
        name="get_order",
        description="Look up an order",
        parameters={"type": "object", "properties": {"order_id": {"type": "string"}}},
    )

    built = await ContextBuilder().build(
        _agent(), _conversation(), [_message(MessageRole.USER, "Oi")], tools=[spec]
    )

    rendered = " ".join(m.content or "" for m in built)
    assert "get_order" not in rendered
