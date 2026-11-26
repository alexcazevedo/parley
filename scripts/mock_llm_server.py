"""Stub model for trying parley without a model key.

Answers `POST /v1/chat/completions` (the one endpoint parley's
OpenAI-compatible provider calls) deterministically, never streams and never
fails: every request gets a valid response.

Each auxiliary caller (boundary check, sentiment, categorization, summary,
handoff brief) is recognized by a fragment of its system prompt and answered
in the format its parser accepts. Anything else is the agent's turn.

The fragments below duplicate the prompts in app/; tests/test_mock_llm_server.py
fails when a prompt changes and a fragment stops matching.

Runs as the `mock-llm` service of docker-compose.stub.yml.
Standalone: `uv run uvicorn scripts.mock_llm_server:app --port 9100`.
"""

import json
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="Mock LLM Server")

# (system prompt fragment, fixed answer). Order matters only if fragments overlap.
AUXILIARY_ANSWERS: list[tuple[str, str]] = [
    ("compliance checker for a customer-support assistant", "ALLOW"),
    ("how frustrated the USER is", "0.1"),
    ("You file a customer-support conversation under its subject", "0"),
    (
        "running summary of a customer support conversation",
        "The customer asked a question and the assistant answered it.",
    ),
    (
        "brief for a human operator who is about to take over",
        "The customer asked for help and the assistant could not resolve it.",
    ),
]

HANDOFF_TOOL = "handoff_to_human"
HANDOFF_WORDS = ("human", "person", "pessoa", "atendente")


def _text(content: Any) -> str:
    """Plain text of a message content: a string or a list of OpenAI parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _offers_handoff(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == HANDOFF_TOOL
        for tool in tools
    )


def _answer(body: dict[str, Any]) -> dict[str, Any]:
    """The assistant message for one request: {"content": ...} or with tool_calls."""
    raw = body.get("messages")
    messages = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []

    system = " ".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
    for fragment, answer in AUXILIARY_ANSWERS:
        if fragment in system:
            return {"role": "assistant", "content": answer}

    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return {"role": "assistant", "content": "Done. Is there anything else I can help with?"}

    last_user = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            last_user = _text(message.get("content"))
            break

    if _offers_handoff(body.get("tools")) and any(
        word in last_user.lower() for word in HANDOFF_WORDS
    ):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_stub_handoff",
                    "type": "function",
                    "function": {
                        "name": HANDOFF_TOOL,
                        "arguments": json.dumps({"reason": "The customer asked for a human."}),
                    },
                }
            ],
        }
    return {"role": "assistant", "content": f"Stub reply to: {last_user}"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    message = _answer(body)
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "model": str(body.get("model") or "stub"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
