"""Mock business-tool server for the compose demo.

Stands in for a customer's own HTTP endpoint (PRODUCT.md: "the customer
implements the business tools; the harness manages the conversation"): a
single `GET /orders?order_id=...` returning a fake order status, exactly what
`scripts/demo.py` registers as the `get_order` tool and the agent calls
during the scripted run (see tickets/11-e2e-demo-readme.md).

Runs as the `mock-tools` service in docker-compose, reachable from the
api/worker containers at `http://mock-tools:9000/orders` — `TOOL_ALLOWED_HOSTS`
allowlists that hostname past the SSRF guard (see app/tools/ssrf.py).

Standalone: `uv run uvicorn scripts.mock_tool_server:app --port 9000`.
"""

import re

from fastapi import FastAPI, Query

app = FastAPI(title="Mock Tool Server")

# Keyed by the order id the demo script sends ("12345"); anything else falls
# back to a generic "not found" status so the agent still gets a JSON reply
# to reason about instead of an error.
_FAKE_ORDERS: dict[str, dict[str, str]] = {
    "12345": {"order_id": "12345", "status": "shipped", "eta": "amanhã"},
}


def _normalize_order_id(order_id: str) -> str:
    """Strip everything but digits so "12.345", "12 345", "#12345" and
    "12345" all resolve to the same order — Whisper transcribes pt-BR
    numbers with a thousands separator (e.g. "12.345"), see ticket 30.
    """
    return re.sub(r"\D", "", order_id)


@app.get("/orders")
async def get_order(order_id: str = Query(...)) -> dict[str, str]:
    normalized = _normalize_order_id(order_id)
    order = _FAKE_ORDERS.get(normalized)
    if order is not None:
        return order
    return {"order_id": order_id, "status": "not_found"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
