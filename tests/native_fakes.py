"""A scripted vendor model for the native providers (`app.llm.providers.native`).

The `native_model` fixture replaces `build_native_model` with one that returns
a Pydantic AI `FunctionModel`: no vendor client is ever built, so no test
reaches a vendor. A test scripts what each model request answers, in order:

    native_model.reply("Olá!", input_tokens=12, output_tokens=3)
    native_model.call("get_order", {"order_id": "1"}, call_id="call_1")
    native_model.fail(RuntimeError("boom"))
    native_model.delay(5.0)            # the next request sleeps first

and reads back every request in `native_model.requests` (`NativeRequest`:
the provider and model asked for, the Pydantic AI messages, the tool
definitions and the model settings).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic_ai import messages as pai
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage


@dataclass
class NativeRequest:
    provider: str
    model: str
    messages: list[pai.ModelMessage]
    tools: list[Any]
    settings: dict[str, Any]

    def parts(self) -> list[Any]:
        return [part for message in self.messages for part in message.parts]


@dataclass
class _Delay:
    seconds: float


@dataclass
class NativeScript:
    steps: list[Any] = field(default_factory=list)
    requests: list[NativeRequest] = field(default_factory=list)

    def reply(self, text: str, *, input_tokens: int = 10, output_tokens: int = 5) -> NativeScript:
        self.steps.append(
            pai.ModelResponse(
                parts=[pai.TextPart(content=text)],
                usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            )
        )
        return self

    def call(
        self,
        name: str,
        arguments: dict[str, Any] | str,
        *,
        call_id: str = "call_1",
        text: str | None = None,
        input_tokens: int = 10,
        output_tokens: int = 5,
    ) -> NativeScript:
        parts: list[Any] = [pai.TextPart(content=text)] if text else []
        parts.append(pai.ToolCallPart(tool_name=name, args=arguments, tool_call_id=call_id))
        self.steps.append(
            pai.ModelResponse(
                parts=parts,
                usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            )
        )
        return self

    def empty(self) -> NativeScript:
        self.steps.append(pai.ModelResponse(parts=[], usage=RequestUsage(input_tokens=1)))
        return self

    def fail(self, error: Exception | None = None) -> NativeScript:
        self.steps.append(error or RuntimeError("vendor exploded"))
        return self

    def delay(self, seconds: float) -> NativeScript:
        self.steps.append(_Delay(seconds))
        return self

    def build(self, provider: str, model: str, settings: Any = None) -> FunctionModel:
        async def answer(messages: list[pai.ModelMessage], info: AgentInfo) -> pai.ModelResponse:
            self.requests.append(
                NativeRequest(
                    provider=provider,
                    model=model,
                    messages=list(messages),
                    tools=list(info.function_tools),
                    settings=dict(info.model_settings or {}),
                )
            )
            if not self.steps:
                raise AssertionError("native model: no scripted step left")
            step = self.steps.pop(0)
            if isinstance(step, _Delay):
                await asyncio.sleep(step.seconds)
                step = self.steps.pop(0) if self.steps else pai.ModelResponse(parts=[])
            if isinstance(step, Exception):
                raise step
            return step

        return FunctionModel(answer, model_name=f"fake-{provider}")


@pytest.fixture
def native_model(monkeypatch) -> NativeScript:
    import app.llm.providers.native as native_module

    script = NativeScript()
    monkeypatch.setattr(native_module, "build_native_model", script.build)
    return script
