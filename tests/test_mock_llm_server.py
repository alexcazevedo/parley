"""The stub model (scripts/mock_llm_server.py) against the real provider and parsers."""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.agents.boundaries import Boundaries, _check_system_prompt, _parse_verdict
from app.agents.categorize import CATEGORY_SYSTEM_PROMPT, _parse_choice
from app.agents.sentiment import SENTIMENT_SYSTEM_PROMPT, _parse_score
from app.conversations.summary import SUMMARIZER_SYSTEM_PROMPT
from app.handoff.summarizer import HANDOFF_SUMMARY_SYSTEM_PROMPT
from app.llm.base import ROLE_SYSTEM, ROLE_USER, LLMMessage, ToolSpec
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.tools.builtin import HANDOFF_SPEC
from app.tools.validation import validate_arguments
from scripts.mock_llm_server import AUXILIARY_ANSWERS, app

pytestmark = pytest.mark.asyncio

OTHER_TOOL = ToolSpec(name="get_order", description="x", parameters={})


@pytest.fixture
async def provider():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    yield OpenAICompatibleProvider("http://stub/v1", "stub", client=client)
    await client.aclose()


async def _complete(provider: OpenAICompatibleProvider, system: str, user: str):
    return await provider.complete(
        [LLMMessage(role=ROLE_SYSTEM, content=system), LLMMessage(role=ROLE_USER, content=user)],
        model="stub",
    )


async def test_plain_reply_echoes_last_user_text(provider):
    response = await provider.complete(
        [
            LLMMessage(role=ROLE_SYSTEM, content="You are helpful."),
            LLMMessage(role=ROLE_USER, content="first"),
            LLMMessage(role=ROLE_USER, content="Where is my order?"),
        ],
        [HANDOFF_SPEC],
        model="stub",
    )
    assert response.content == "Stub reply to: Where is my order?"
    assert response.tool_calls == []
    assert response.usage.prompt_tokens > 0


@pytest.mark.parametrize("text", ["I want a HUMAN", "quero falar com uma Pessoa", "atendente!"])
async def test_handoff_tool_call(provider, text):
    response = await provider.complete(
        [LLMMessage(role=ROLE_SYSTEM, content="You are helpful."),
         LLMMessage(role=ROLE_USER, content=text)],
        [HANDOFF_SPEC],
        model="stub",
    )
    [call] = response.tool_calls
    assert call.name == "handoff_to_human"
    assert validate_arguments(HANDOFF_SPEC.parameters, call.arguments) is None


async def test_no_handoff_without_the_tool(provider):
    response = await provider.complete(
        [LLMMessage(role=ROLE_USER, content="a human please")], [OTHER_TOOL], model="stub"
    )
    assert response.tool_calls == []
    assert response.content == "Stub reply to: a human please"


async def test_tool_result_gets_short_text(provider):
    raw: dict[str, Any] = {
        "model": "stub",
        "messages": [
            {"role": "user", "content": "a human please"},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
        "tools": [{"type": "function", "function": {"name": "handoff_to_human"}}],
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        body = (await client.post("http://stub/v1/chat/completions", json=raw)).json()
    message = body["choices"][0]["message"]
    assert message["content"] and not message.get("tool_calls")


async def test_multimodal_user_content(provider):
    raw = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url"}]}
        ]
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        body = (await client.post("http://stub/v1/chat/completions", json=raw)).json()
    assert body["choices"][0]["message"]["content"] == "Stub reply to: hi"


async def test_boundary_check_is_allowed(provider):
    system = _check_system_prompt(
        Boundaries(scopes=("orders",), never=("refunds",), refusal_message="no", check_enabled=True)
    )
    response = await _complete(provider, system, 'Data to judge:\n{"reply": "x"}')
    assert _parse_verdict(response.content).allowed is True


async def test_sentiment_is_calm(provider):
    response = await _complete(
        provider, SENTIMENT_SYSTEM_PROMPT, "Conversation transcript:\nuser: hi"
    )
    score = _parse_score(response.content)
    assert score is not None and score < 0.3


async def test_categorization_is_none(provider):
    system = CATEGORY_SYSTEM_PROMPT.format(categories="1. Billing: invoices")
    response = await _complete(provider, system, "Conversation transcript:\nuser: hi")
    categories = [SimpleNamespace(id="a")]
    assert _parse_choice(response.content, categories) is None  # type: ignore[arg-type]
    assert (response.content or "").strip() == "0"


@pytest.mark.parametrize("prompt", [SUMMARIZER_SYSTEM_PROMPT, HANDOFF_SUMMARY_SYSTEM_PROMPT])
async def test_summary_and_brief_are_a_sentence(provider, prompt):
    response = await _complete(provider, prompt, "New messages:\nuser: hi")
    assert (response.content or "").strip()


async def test_every_fragment_still_matches_its_prompt():
    prompts = [
        _check_system_prompt(
            Boundaries(scopes=("a",), never=(), refusal_message="n", check_enabled=True)
        ),
        SENTIMENT_SYSTEM_PROMPT,
        CATEGORY_SYSTEM_PROMPT,
        SUMMARIZER_SYSTEM_PROMPT,
        HANDOFF_SUMMARY_SYSTEM_PROMPT,
    ]
    assert len(prompts) == len(AUXILIARY_ANSWERS)
    for prompt, (fragment, _) in zip(prompts, AUXILIARY_ANSWERS, strict=True):
        assert fragment in prompt
