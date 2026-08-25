"""Scripted provider for tests (and offline demos).

Lives in `app/` rather than `tests/` so any package — including future
integration harnesses — can script an agent turn without a network call.
"""

from app.llm.base import LLMMessage, LLMResponse, ToolSpec


class FakeLLMProvider:
    """Returns `scripted` responses in order, recording every call.

    With `repeat_last=True` the final scripted response is returned forever,
    which is how the "model loops on tool calls" case is exercised.
    """

    def __init__(self, scripted: list[LLMResponse], *, repeat_last: bool = False) -> None:
        self.scripted = list(scripted)
        self.repeat_last = repeat_last
        self.calls: list[dict] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec] | None = None,
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools or []),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if len(self.scripted) > 1 or not self.repeat_last:
            if not self.scripted:
                raise AssertionError("FakeLLMProvider ran out of scripted responses")
            return self.scripted.pop(0)
        return self.scripted[0]
