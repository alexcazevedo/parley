"""Stub model for trying parley without a model key.

Answers `POST /v1/chat/completions` (the endpoint parley's OpenAI-compatible
provider calls) deterministically, never streams and never fails: every
request gets a valid response. `POST /v1/embeddings` answers each input with
a deterministic 64-number vector from its hashed words, so texts sharing
words are close.

Each auxiliary caller (boundary check, sentiment, categorization, summary,
handoff brief) is recognized by a fragment of its system prompt and answered
in the format its parser accepts. Anything else is the agent's turn: the
command `/tool <name> <JSON arguments>` calls that tool when it is offered
(and the reply after its result is `Stub tool result: <content>`), a handoff
request calls `handoff_to_human`, any other message calls `search_knowledge`
when it is offered, and the reply after a search quotes the first passage
found.

A typed decision (a decision job set to `llm:<model>`, `app/llm/decisions.py`)
comes as Pydantic AI's tool-mode structured output: one offered tool named
`final_result` whose parameters are the answer type, and `tool_choice`
`required`. The stub calls that tool with a fixed answer chosen by the
answer's fields: allowed with no rule broken, the lowest frustration level,
no department. This form is recognized before anything else.

The fragments below duplicate the prompts in app/; tests/test_mock_llm_server.py
fails when a prompt changes and a fragment stops matching, and when the form
Pydantic AI sends for a typed decision stops matching the one recognized here.

Runs as the `mock-llm` service of docker-compose.stub.yml.
Standalone: `uv run uvicorn scripts.mock_llm_server:app --port 9100`.
"""

import hashlib
import json
import math
import re
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
    (
        "short recap of one customer-support session",
        "RECAP: The customer asked a question and the assistant answered it.\nNONE",
    ),
]

# Pydantic AI's tool-mode structured output: the tool's name and, by a field
# of the answer type, the fixed answer.
STRUCTURED_TOOL = "final_result"
STRUCTURED_CALL_ID = "call_stub_structured"
STRUCTURED_ANSWERS: list[tuple[str, dict[str, Any]]] = [
    ("allowed", {"allowed": True, "broken": "none"}),
    ("level", {"level": 0}),
    ("department", {"department": "none"}),
]

HANDOFF_TOOL = "handoff_to_human"
HANDOFF_WORDS = ("human", "person", "pessoa", "atendente")
KNOWLEDGE_TOOL = "search_knowledge"
KNOWLEDGE_ANSWER_PREFIX = "Stub answer from knowledge: "
KNOWLEDGE_NOTHING = "Stub: nothing found in the knowledge base."
TOOL_COMMAND_RE = re.compile(r"^/tool(?=\s|$)\s*(\S*)(.*)$", re.DOTALL)
TOOL_CALL_ID = "call_stub_tool"
TOOL_RESULT_PREFIX = "Stub tool result: "
TOOL_INVALID = "Stub: invalid /tool command."
TOOL_NOT_OFFERED = "Stub: tool {name} is not offered."


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


def _offers(tools: Any, name: str) -> bool:
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == name
        for tool in tools
    )


def _structured_answer(body: dict[str, Any]) -> dict[str, Any] | None:
    """The fixed typed answer when the request is a structured-output request, else None."""
    if body.get("tool_choice") != "required":
        return None
    tools = body.get("tools")
    for tool in tools if isinstance(tools, list) else []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or function.get("name") != STRUCTURED_TOOL:
            continue
        parameters = function.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if not isinstance(properties, dict):
            return None
        for field, answer in STRUCTURED_ANSWERS:
            if field in properties:
                return answer
    return None


def _tool_name(messages: list[dict[str, Any]], call_id: Any) -> str:
    """The name of the tool call `call_id` in the assistant messages."""
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id") == call_id:
                function = call.get("function")
                if isinstance(function, dict):
                    return str(function.get("name") or "")
    return ""


def _knowledge_answer(content: Any) -> str:
    """The reply after a `search_knowledge` result: its first passage, or nothing found."""
    try:
        payload = json.loads(_text(content))
    except ValueError:
        payload = None
    data = payload.get("data") if isinstance(payload, dict) else None
    passages = data.get("passages") if isinstance(data, dict) else None
    if isinstance(passages, list) and passages and isinstance(passages[0], dict):
        return KNOWLEDGE_ANSWER_PREFIX + str(passages[0].get("text") or "")
    return KNOWLEDGE_NOTHING


def _tool_command(text: str) -> tuple[str, dict[str, Any]] | None | str:
    """`(name, arguments)` for a `/tool` command, `TOOL_INVALID` for a malformed
    one, None when the text is no `/tool` command."""
    match = TOOL_COMMAND_RE.match(text.strip())
    if match is None:
        return None
    name, rest = match.group(1), match.group(2).strip()
    if not name:
        return TOOL_INVALID
    try:
        arguments = json.loads(rest) if rest else {}
    except ValueError:
        return TOOL_INVALID
    if not isinstance(arguments, dict):
        return TOOL_INVALID
    return name, arguments


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def _answer(body: dict[str, Any]) -> dict[str, Any]:
    """The assistant message for one request: {"content": ...} or with tool_calls."""
    raw = body.get("messages")
    messages = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []

    structured = _structured_answer(body)
    if structured is not None:
        return _call(STRUCTURED_CALL_ID, STRUCTURED_TOOL, structured)

    system = " ".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
    for fragment, answer in AUXILIARY_ANSWERS:
        if fragment in system:
            return {"role": "assistant", "content": answer}

    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        if last.get("tool_call_id") == TOOL_CALL_ID:
            return {"role": "assistant", "content": TOOL_RESULT_PREFIX + _text(last.get("content"))}
        if _tool_name(messages, last.get("tool_call_id")) == KNOWLEDGE_TOOL:
            return {"role": "assistant", "content": _knowledge_answer(last.get("content"))}
        return {"role": "assistant", "content": "Done. Is there anything else I can help with?"}

    last_user = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            last_user = _text(message.get("content"))
            break

    tools = body.get("tools")
    command = _tool_command(last_user)
    if isinstance(command, str):
        return {"role": "assistant", "content": command}
    if command is not None:
        name, arguments = command
        if not _offers(tools, name):
            return {"role": "assistant", "content": TOOL_NOT_OFFERED.format(name=name)}
        return _call(TOOL_CALL_ID, name, arguments)
    wants_human = any(word in last_user.lower() for word in HANDOFF_WORDS)
    if _offers(tools, HANDOFF_TOOL) and wants_human:
        return _call(
            "call_stub_handoff", HANDOFF_TOOL, {"reason": "The customer asked for a human."}
        )
    if _offers(tools, KNOWLEDGE_TOOL) and not wants_human and last_user.strip():
        return _call("call_stub_knowledge", KNOWLEDGE_TOOL, {"query": last_user})
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


EMBEDDING_DIMENSIONS = 64
_WORD_RE = re.compile(r"\w+")


def embedding(text: str) -> list[float]:
    """Unit vector counting the text's words in hashed buckets (a lone bucket when none)."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for word in _WORD_RE.findall(text.lower()):
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS] += 1.0
    if not any(vector):
        vector[0] = 1.0
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw = body.get("input")
    texts = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return {
        "object": "list",
        "model": str(body.get("model") or "stub"),
        "data": [
            {"object": "embedding", "index": index, "embedding": embedding(str(item))}
            for index, item in enumerate(texts)
        ],
        "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
