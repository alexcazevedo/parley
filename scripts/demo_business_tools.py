"""The business tools success demo against a running stack with the stub model
and the example project (docker-compose.example.yml + docker-compose.demo.yml;
the top of docker-compose.demo.yml shows how to start the stack).

    uv run python scripts/demo_business_tools.py --base-url http://localhost:8001 \
        --inbox-url http://localhost:9200 --admin-key <key> --tools-token demo-tools-token

Creates its own tenant, registers the example's MCP server, creates an agent and
an external channel, sends `/tool` commands for one customer and the first for a
second one, and reads the example's quotes. Exits non-zero on the first failed
expectation; prints `business tools demo: ok` last.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

import httpx

TIMEOUT_SECONDS = 60
STUB_RESULT = "Stub tool result: "
FIRST = "cartolina-branca-150g"
SECOND = "cartolina-branca-180g"


def fail(message: str) -> None:
    print(f"FAILED: {message}")
    sys.exit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def poll(what: str, fetch: Any, done: Any) -> Any:
    deadline = time.time() + TIMEOUT_SECONDS
    while True:
        value = fetch()
        if done(value):
            return value
        if time.time() >= deadline:
            fail(f"timed out after {TIMEOUT_SECONDS}s waiting for {what}")
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--inbox-url", default="http://localhost:9200")
    parser.add_argument("--admin-key", required=True)
    parser.add_argument("--tools-token", required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    inbox = args.inbox_url.rstrip("/")

    with httpx.Client(timeout=30.0) as client:

        def call(method: str, path: str, status: int, headers: dict, **kwargs: Any) -> Any:
            response = client.request(method, f"{base}{path}", headers=headers, **kwargs)
            expect(
                response.status_code == status,
                f"{method} {path}: {response.status_code} {response.text}",
            )
            return response.json()

        def quotes() -> dict[str, Any]:
            response = client.get(f"{inbox}/quotes")
            expect(response.status_code == 200, f"GET /quotes: {response.status_code}")
            return response.json()

        print("== waiting for the API ==")
        poll("the API", lambda: _up(client, f"{base}/health"), bool)
        print("== waiting for the example project ==")
        poll("the example", lambda: _up(client, f"{inbox}/quotes"), bool)

        print("== creating a tenant ==")
        tenant = call(
            "POST", "/tenants", 201, {"X-Admin-Key": args.admin_key},
            json={"name": f"Business tools demo {uuid.uuid4().hex[:8]}"},
        )
        headers = {"X-API-Key": tenant["api_key"]}

        print("== registering the example's MCP server ==")
        server = call("POST", "/mcp-servers", 201, headers, json={
            "name": "shop", "url": "http://project-inbox:9200/mcp",
            "auth_config": {"type": "bearer", "token": args.tools_token},
        })
        expect(server["status"] == "ok", f"the server status is {server['status']}: {server}")
        exposed = {t["exposed_name"] for t in server["tools"]}
        expect(
            {"shop__search_product", "shop__add_to_quote"} <= exposed,
            f"the tools are {sorted(exposed)}",
        )
        print(f"  tools: {', '.join(sorted(exposed))}")

        print("== creating an agent and an external channel ==")
        agent = call("POST", "/agents", 201, headers, json={
            "name": "Vendedora", "instructions": "Você atende a papelaria.",
            "model": "stub", "mcp_servers": ["shop"], "boundary_check_enabled": False,
        })
        channel = call("POST", "/channels", 201, headers, json={
            "type": "external", "name": "Inbox", "agent_id": agent["id"],
            "config": {"events_url": "http://project-inbox:9200/events"},
        })

        def say(user_id: str, text: str, replies: int) -> str:
            sent = call("POST", "/channels/external/messages", 201, headers, json={
                "channel_id": channel["id"], "user_id": user_id, "message": text})
            path = f"/conversations/{sent['conversation_id']}/messages"
            messages = poll(
                "the reply",
                lambda: call("GET", path, 200, headers),
                lambda ms: sum(m["role"] == "assistant" for m in ms) >= replies,
            )
            reply = [m for m in messages if m["role"] == "assistant"][-1]["content"]
            print(f"  {user_id}: {text}\n  agent: {reply}")
            expect(reply.startswith(STUB_RESULT), f"unexpected reply: {reply}")
            return reply

        suffix = uuid.uuid4().hex[:6]
        ana, bia = f"ana-{suffix}", f"bia-{suffix}"
        add = '/tool shop__add_to_quote {"product_id": "' + FIRST + '", "quantity": 1}'

        print("== ana searches ==")
        reply = say(ana, '/tool shop__search_product {"query": "cartolina branca"}', 1)
        expect(FIRST in reply and SECOND in reply, "the reply does not name both products")

        print("== ana adds the first ==")
        say(ana, add, 2)
        found = quotes()
        expect(
            [(x["product_id"], x["quantity"]) for x in found.get(ana, [])] == [(FIRST, 1)],
            f"ana's quote is {found.get(ana)}",
        )
        expect(bia not in found, "bia has a quote before writing")

        print("== bia adds the first ==")
        say(bia, add, 1)
        found = quotes()
        expect(
            [(x["product_id"], x["quantity"]) for x in found.get(bia, [])] == [(FIRST, 1)],
            f"bia's quote is {found.get(bia)}",
        )
        expect(found[ana][0]["quantity"] == 1, "ana's quote changed")

    print("business tools demo: ok")
    return 0


def _up(client: httpx.Client, url: str) -> bool:
    try:
        return client.get(url).status_code == 200
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    sys.exit(main())
