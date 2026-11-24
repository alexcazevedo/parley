"""Builds the prompt transcript for one agent turn.

Short-term memory is the tail of the persisted conversation; long-term memory
and the rolling summary are folded into the system prompt. Nothing tenant-secret
(tool endpoints, auth configs, API keys) ever reaches this module — tools are
advertised through the provider's `tools` argument, which carries name,
description and JSON Schema only.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence

import structlog

from app.agents.boundaries import effective_boundaries, render_boundaries
from app.config import get_settings
from app.llm.base import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ROLE_USER,
    ContentPart,
    ImagePart,
    LLMMessage,
    TextPart,
    ToolCall,
    ToolSpec,
)
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.message import Message, MessageRole

logger = structlog.get_logger(__name__)

MEMORY_HEADER = "## Known facts about this contact"
SUMMARY_HEADER = "## Conversation summary"

# How a media message reads when the model only gets text: either because
# vision is off/unavailable (images) or because transcription produced nothing
# (audio). The wording is addressed to the model, in the same
# language as the agent's prompts.
# The defaults of the `image_placeholder` / `audio_placeholder` settings, which
# are read at call time (`render_message_text`).
IMAGE_PLACEHOLDER = "[imagem enviada pelo cliente]"
AUDIO_PLACEHOLDER = "[áudio enviado pelo cliente]"

ImageLoader = Callable[[Message], Awaitable[bytes | None]]
"""Fetches the bytes behind a persisted image message (media storage)."""

# Persisted role -> LLM role. HUMAN_OPERATOR collapses onto "assistant": from
# the contact's side a human agent and the AI are the same voice.
_ROLE_MAP = {
    MessageRole.USER: ROLE_USER,
    MessageRole.ASSISTANT: ROLE_ASSISTANT,
    MessageRole.HUMAN_OPERATOR: ROLE_ASSISTANT,
    MessageRole.TOOL: ROLE_TOOL,
    MessageRole.SYSTEM: ROLE_SYSTEM,
}


def render_message_text(message: Message) -> str:
    """The message as plain text, whatever its media type.

    Shared by the prompt transcript and the turn's joined `input_text` so both
    describe the same conversation — a caption-less photo must not silently
    become an empty line in the input the agent is asked about.
    """
    content = (message.content or "").strip()
    if message.type == "image":
        placeholder = get_settings().image_placeholder
        return f"{placeholder}: {content}" if content else placeholder
    if message.type == "audio":
        # Ticket 28 stores the transcript as the content; until then (or on a
        # failed transcription) the model is told a voice note came in.
        return content or get_settings().audio_placeholder
    return message.content or ""


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
    """Assembles the prompt transcript, including image content when enabled.

    `image_loader` is how persisted image messages get their bytes (the runtime
    passes media storage); leaving it `None` — as the pure unit tests do — makes
    every image degrade to its text placeholder. `vision_enabled=False` does the
    same for a deployment whose model cannot see.
    """

    def __init__(
        self,
        image_loader: ImageLoader | None = None,
        vision_enabled: bool = True,
    ) -> None:
        self._image_loader = image_loader
        self._vision_enabled = vision_enabled

    async def build(
        self,
        agent: Agent,
        conversation: Conversation,
        recent_messages: Sequence[Message],
        memories: Sequence[Memory] = (),
        tools: Sequence[ToolSpec] = (),
        style_hint: str | None = None,
    ) -> list[LLMMessage]:
        """System prompt + the history the runtime selected.

        `agent.context_message_limit` is applied by the runtime (it counts
        conversation messages, not tool rows); this does not cut again.

        `tools` is accepted (and validated to be spec-shaped by the type) but not
        injected into the prompt: OpenAI-compatible backends receive tools through
        the request's `tools` field, so duplicating them in the system prompt only
        burns tokens. It stays in the signature because prompt-side tool listing
        is a plausible per-agent knob later.

        `style_hint` is appended as the final system-prompt part — the humanizer
        uses it to ask for short, blank-line-separated chat bubbles.
        """
        messages: list[LLMMessage] = [
            LLMMessage(
                role=ROLE_SYSTEM,
                content=self._system_prompt(agent, conversation, memories, style_hint),
            )
        ]
        messages.extend(await self._history(list(recent_messages)))
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
        boundaries = effective_boundaries(agent)
        if boundaries is not None:
            parts.append(render_boundaries(boundaries))
        return "\n\n".join(parts)

    async def _history(self, window: Sequence[Message]) -> list[LLMMessage]:
        built: list[LLMMessage] = []
        with_bytes = self._image_message_ids(window)
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
                built.append(LLMMessage(role=ROLE_ASSISTANT, content=None, tool_calls=tool_calls))
                continue
            built.append(
                LLMMessage(role=role, content=await self._content_for(message, with_bytes))
            )
        return built

    def _image_message_ids(self, window: Sequence[Message]) -> set[uuid.UUID]:
        """Ids of the image messages allowed to carry actual bytes.

        Only the newest `settings.vision_max_images` of the window: every image
        is a large chunk of the prompt budget, and older photos are rarely what
        the current question is about.
        """
        budget = get_settings().vision_max_images
        if not self._vision_enabled or self._image_loader is None or budget <= 0:
            return set()
        candidates = [
            message.id
            for message in window
            if message.role == MessageRole.USER
            and message.type == "image"
            and message.media_key
            and message.media_mime
        ]
        return set(candidates[-budget:])

    async def _content_for(
        self, message: Message, with_bytes: set[uuid.UUID]
    ) -> str | list[ContentPart]:
        """Multimodal content for an in-budget image, plain text otherwise."""
        if message.id not in with_bytes:
            return render_message_text(message)
        data = await self._load_image(message)
        if not data:
            return render_message_text(message)
        caption = (message.content or "").strip()
        return [
            ImagePart(mime=message.media_mime or "", data=data),
            TextPart(text=caption or get_settings().image_placeholder),
        ]

    async def _load_image(self, message: Message) -> bytes | None:
        """Best-effort byte fetch: a missing or unreadable file degrades to text.

        Storage is a side system (a deleted file, a permissions problem); losing
        the picture is bad, losing the whole turn is worse.
        """
        assert self._image_loader is not None
        try:
            return await self._image_loader(message)
        except Exception:
            logger.warning(
                "context.image_load_failed",
                message_id=str(message.id),
                media_key=message.media_key,
                exc_info=True,
            )
            return None
