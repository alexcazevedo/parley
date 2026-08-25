"""Scripted end-to-end demo against a *running* `docker compose up` stack —
the PRODUCT.md "success demo" driven over plain HTTP with `httpx`, exactly
the way a real integrator would use the harness. No test framework, no
shortcuts through the pipeline: everything goes through `/dev/messages` (the
same ingestion path as `/channels/webchat/messages`) and the public REST API.

Usage:
    cp .env.example .env   # then set a real LLM_API_KEY
    docker compose up -d --build
    uv run scripts/demo.py

Exit codes: 0 success, 2 `.env` still has the placeholder LLM key (expected
in CI/sandboxed runs with no real key — not a pipeline failure), 1 any
expectation about the pipeline's behaviour was not met.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent

PLACEHOLDER_LLM_KEY = "changeme-llm-key"
DEFAULT_ADMIN_KEY = "changeme-admin-key"
DEFAULT_API_PORT = "8001"
DEFAULT_LLM_MODEL = "gpt-4o-mini"

HEALTH_TIMEOUT_SECONDS = 30
HEALTH_POLL_SECONDS = 1
REPLY_TIMEOUT_SECONDS = 45
REPLY_POLL_SECONDS = 1
# Longer than DEBOUNCE_SECONDS (default 3s): enough time to be sure a
# handed-off conversation really stayed silent, not just "hasn't replied yet".
SILENCE_WAIT_SECONDS = 6

ORDER_ID = "12345"
AGENT_INSTRUCTIONS = (
    "Você é o atendente virtual da Acme. Responda sempre em português, de "
    "forma cordial e objetiva. Quando o cliente mencionar um número de "
    "pedido, use a ferramenta get_order para consultar o status antes de "
    "responder. Se o cliente pedir para falar com uma pessoa/atendente "
    "humano, use a ferramenta handoff_to_human em vez de tentar resolver "
    "sozinho."
)


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _settings() -> dict[str, str]:
    """`.env` values, with real environment variables taking priority —
    mirrors pydantic-settings' own precedence (see app/config.py).
    """
    return {**_load_env_file(ROOT / ".env"), **os.environ}


def _wait_for_api(client: httpx.Client, base_url: str) -> bool:
    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            resp = client.get(f"{base_url}/health")
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(HEALTH_POLL_SECONDS)
    return False


def _get_messages(
    client: httpx.Client, base_url: str, headers: dict[str, str], conversation_id: str
) -> list[dict[str, Any]]:
    resp = client.get(f"{base_url}/conversations/{conversation_id}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json()


def _wait_for_new_message(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    conversation_id: str,
    baseline_count: int,
) -> list[dict[str, Any]]:
    """Poll until the transcript grew past `baseline_count` AND the newest
    message is a reply (not the user message we just sent — the user's own
    message also grows the transcript, which would end the wait too early)."""

    def _got_reply(msgs: list[dict[str, Any]]) -> bool:
        return len(msgs) > baseline_count and msgs[-1]["role"] != "user"

    deadline = time.time() + REPLY_TIMEOUT_SECONDS
    messages = _get_messages(client, base_url, headers, conversation_id)
    while not _got_reply(messages):
        if time.time() >= deadline:
            raise TimeoutError(
                f"timed out after {REPLY_TIMEOUT_SECONDS}s waiting for a reply "
                f"(still {len(messages)} messages, expected more than {baseline_count})"
            )
        time.sleep(REPLY_POLL_SECONDS)
        messages = _get_messages(client, base_url, headers, conversation_id)
    return messages


def _print_transcript(messages: list[dict[str, Any]]) -> None:
    print("-- transcript --")
    for message in messages:
        print(f"  [{message['role']}] {message['content']}")


def main() -> int:
    env = _settings()

    llm_key = env.get("LLM_API_KEY", "")
    if not llm_key or llm_key == PLACEHOLDER_LLM_KEY:
        print(
            "LLM_API_KEY in .env is still the placeholder "
            f"({PLACEHOLDER_LLM_KEY!r}). Copy .env.example to .env and set a "
            "real OpenAI-compatible key before running the live demo — "
            "`pytest`'s scripted-LLM e2e test (tests/test_e2e_demo.py) already "
            "covers the pipeline without a real key."
        )
        return 2

    admin_key = env.get("ADMIN_API_KEY", DEFAULT_ADMIN_KEY)
    api_port = env.get("API_PORT", DEFAULT_API_PORT)
    llm_model = env.get("LLM_MODEL", DEFAULT_LLM_MODEL)
    base_url = f"http://localhost:{api_port}"

    with httpx.Client(timeout=15.0) as client:
        print(f"== waiting for the API at {base_url} ==")
        if not _wait_for_api(client, base_url):
            print(f"FAILED: {base_url}/health never returned 200 within {HEALTH_TIMEOUT_SECONDS}s")
            return 1

        print("== creating tenant ==")
        tenant_resp = client.post(
            f"{base_url}/tenants",
            json={"name": f"Demo Tenant {uuid.uuid4().hex[:8]}"},
            headers={"X-Admin-Key": admin_key},
        )
        tenant_resp.raise_for_status()
        tenant = tenant_resp.json()
        headers = {"X-API-Key": tenant["api_key"]}
        print(f"  tenant: {tenant['id']}")

        print("== registering the get_order tool (mock-tools:9000) ==")
        tool_resp = client.post(
            f"{base_url}/tools",
            json={
                "name": "get_order",
                "description": "Consulta o status de um pedido a partir do seu id.",
                "input_schema": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                    "additionalProperties": False,
                },
                "endpoint": "http://mock-tools:9000/orders",
                "method": "GET",
                "timeout_seconds": 5,
            },
            headers=headers,
        )
        tool_resp.raise_for_status()

        print("== creating agent (memory + handoff enabled, get_order wired) ==")
        agent_resp = client.post(
            f"{base_url}/agents",
            json={
                "name": "Atendente Acme",
                "instructions": AGENT_INSTRUCTIONS,
                "model": llm_model,
                "tool_names": ["get_order"],
                "memory_enabled": True,
                "handoff_enabled": True,
            },
            headers=headers,
        )
        agent_resp.raise_for_status()
        agent = agent_resp.json()
        print(f"  agent: {agent['id']}")

        print("== creating web chat channel ==")
        channel_resp = client.post(
            f"{base_url}/channels",
            json={"type": "webchat", "name": "Web Demo", "agent_id": agent["id"]},
            headers=headers,
        )
        channel_resp.raise_for_status()
        channel_id = channel_resp.json()["id"]

        user_id = f"demo-user-{uuid.uuid4().hex[:8]}"

        def send(text: str) -> str:
            resp = client.post(
                f"{base_url}/dev/messages",
                json={"channel_id": channel_id, "user_id": user_id, "message": text},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()["conversation_id"]

        print("\n== sending 3 rapid messages (must debounce into one turn) ==")
        texts = ["Oi", "Queria saber onde está", f"meu pedido {ORDER_ID}"]
        conversation_id = ""
        for text in texts:
            conversation_id = send(text)
            print(f"  -> {text!r}")
        baseline = len(_get_messages(client, base_url, headers, conversation_id))

        print(f"  waiting up to {REPLY_TIMEOUT_SECONDS}s for the agent's reply...")
        messages = _wait_for_new_message(client, base_url, headers, conversation_id, baseline)
        _print_transcript(messages)

        reply = messages[-1]
        if reply["role"] != "assistant" or ORDER_ID not in reply["content"]:
            print(f"\nFAILED: expected an assistant reply mentioning order {ORDER_ID}")
            return 1
        print(f"\nOK: agent answered about order {ORDER_ID}")

        print("\n== asking for a human ==")
        baseline = len(messages)
        conversation_id = send("Quero falar com uma pessoa")
        messages = _wait_for_new_message(client, base_url, headers, conversation_id, baseline)
        _print_transcript(messages)

        conv_resp = client.get(f"{base_url}/conversations/{conversation_id}", headers=headers)
        conv_resp.raise_for_status()
        if conv_resp.json()["state"] != "HUMAN_HANDOFF":
            print("\nFAILED: conversation did not transition to HUMAN_HANDOFF")
            return 1
        print("OK: agent called handoff_to_human, conversation is now HUMAN_HANDOFF")

        print("\n== a message while handed off must get no AI reply ==")
        baseline = len(messages)
        send("alguém aí?")
        time.sleep(SILENCE_WAIT_SECONDS)
        messages = _get_messages(client, base_url, headers, conversation_id)
        if len(messages) != baseline + 1 or messages[-1]["role"] != "user":
            print("FAILED: the AI replied while the conversation was handed off")
            return 1
        print("OK: AI stayed silent; the message was still persisted")

        print("\n== operator replies via POST /conversations/{id}/messages ==")
        operator_resp = client.post(
            f"{base_url}/conversations/{conversation_id}/messages",
            json={"content": "Oi, aqui é o atendente. Como posso ajudar?"},
            headers=headers,
        )
        operator_resp.raise_for_status()
        print(f"  operator: {operator_resp.json()['content']!r}")

        print("\n== resuming the conversation ==")
        resume_resp = client.post(
            f"{base_url}/conversations/{conversation_id}/resume", headers=headers
        )
        resume_resp.raise_for_status()
        if resume_resp.json()["state"] != "ACTIVE_AI":
            print("FAILED: /resume did not bring the conversation back to ACTIVE_AI")
            return 1
        print("OK: conversation resumed to ACTIVE_AI")

        print("\n== sending a final message after resume ==")
        baseline = len(_get_messages(client, base_url, headers, conversation_id))
        conversation_id = send("Obrigado!")
        messages = _wait_for_new_message(client, base_url, headers, conversation_id, baseline)
        _print_transcript(messages)
        if messages[-1]["role"] != "assistant":
            print("\nFAILED: expected an AI reply after resume")
            return 1
        print("\nOK: AI is replying again after resume")

    print("\nDemo completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
