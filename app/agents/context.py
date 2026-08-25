"""Builds the prompt transcript for one agent turn.

Short-term memory is the tail of the persisted conversation; long-term memory
and the rolling summary are folded into the system prompt. Nothing tenant-secret
(tool endpoints, auth configs, API keys) ever reaches this module — tools are
advertised through the provider's `tools` argument, which carries name,
description and JSON Schema only.
"""

from collections.abc import Sequence

from app.llm.base import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ROLE_USER,
    LLMMessage,
    ToolCall,
    ToolSpec,
)
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.message import Message, MessageRole

MEMORY_HEADER = "## Known facts about this contact"
SUMMARY_HEADER = "## Conversation summary"

# Persisted role -> LLM role. HUMAN_OPERATOR collapses onto "assistant": from
# the contact's side a human agent and the AI are the same voice.
_ROLE_MAP = {
    MessageRole.USER: ROLE_USER,
    MessageRole.ASSISTANT: ROLE_ASSISTANT,
    MessageRole.HUMAN_OPERATOR: ROLE_ASSISTANT,
    MessageRole.TOOL: ROLE_TOOL,
    MessageRole.SYSTEM: ROLE_SYSTEM,
}


def _tool_calls_from(message: Message) -> list[ToolCall]:
    """Rehydrate the tool calls the runtime stashed in `raw_payload`."""
    raw = (message.raw_payload or {}).get("tool_calls") or []
    calls: list[ToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        calls.append(
            ToolCall(
                id=str(entry["id"]),
                name=str(entry.get("name") or ""),
                arguments=entry.get("arguments") or {},
            )
        )
    return calls


class ContextBuilder:
    def build(
        self,
        agent: Agent,
        conversation: Conversation,
        recent_messages: Sequence[Message],
        memories: Sequence[Memory] = (),
        tools: Sequence[ToolSpec] = (),
        style_hint: str | None = None,
    ) -> list[LLMMessage]:
        """System prompt + the last `agent.context_message_limit` messages.

        `tools` is accepted (and validated to be spec-shaped by the type) but not
        injected into the prompt: OpenAI-compatible backends receive tools through
        the request's `tools` field, so duplicating them in the system prompt only
        burns tokens. It stays in the signature because prompt-side tool listing
        is a plausible per-agent knob later.

        `style_hint` is appended as the final system-prompt part — the humanizer
        uses it to ask for short, blank-line-separated chat bubbles.
        """
        limit = agent.context_message_limit or 20
        messages: list[LLMMessage] = [
            LLMMessage(
                role=ROLE_SYSTEM,
                content=self._system_prompt(agent, conversation, memories, style_hint),
            )
        ]
        messages.extend(self._history(list(recent_messages)[-limit:]))
        return messages

    def _system_prompt(
        self,
        agent: Agent,
        conversation: Conversation,
        memories: Sequence[Memory],
        style_hint: str | None = None,
    ) -> str:
        parts = [agent.instructions.strip()]
        facts = [m.content.strip() for m in memories if m.content and m.content.strip()]
        if facts:
            parts.append(MEMORY_HEADER + "\n" + "\n".join(f"- {fact}" for fact in facts))
        summary = (conversation.summary or "").strip()
        if summary:
            parts.append(SUMMARY_HEADER + "\n" + summary)
        if style_hint and style_hint.strip():
            parts.append(style_hint.strip())
        return "\n\n".join(parts)

    def _history(self, window: Sequence[Message]) -> list[LLMMessage]:
        built: list[LLMMessage] = []
        # A `role="tool"` entry is only valid right after the assistant message
        # that requested it. When the window slices an old turn in half we drop
        # the orphaned results rather than send a transcript the API rejects.
        open_call_ids: set[str] = set()
        for message in window:
            role = _ROLE_MAP.get(message.role)
            if role is None:
                continue
            if role == ROLE_TOOL:
                if message.tool_call_id not in open_call_ids:
                    continue
                built.append(
                    LLMMessage(
                        role=ROLE_TOOL,
                        content=message.content,
                        tool_call_id=message.tool_call_id,
                    )
                )
                continue
            tool_calls = _tool_calls_from(message) if role == ROLE_ASSISTANT else []
            if tool_calls:
                open_call_ids.update(call.id for call in tool_calls)
                built.append(
                    LLMMessage(role=ROLE_ASSISTANT, content=None, tool_calls=tool_calls)
                )
                continue
            built.append(LLMMessage(role=role, content=message.content))
        return built
