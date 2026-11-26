"""The MCP tools of the example project: what an app implements to give an
agent business tools. Imports nothing from parley, only the official `mcp` SDK.

parley sends the customer in the call `_meta`: `parley/user_id` (the id this
project gave parley), `parley/call_id` (the same when a call is repeated),
`parley/conversation_id`, `parley/channel_id`, `parley/contact_name`.
"""

import json
import unicodedata
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

CATALOG: list[dict[str, Any]] = [
    {"id": "cartolina-branca-150g", "name": "Cartolina branca 150g", "price": 1.5, "stock": 200},
    {"id": "cartolina-branca-180g", "name": "Cartolina branca 180g", "price": 2.0, "stock": 120},
    {"id": "cartolina-colorida-180g", "name": "Cartolina colorida 180g", "price": 2.5, "stock": 80},
    {
        "id": "caderno-brochura-96f",
        "name": "Caderno brochura 96 folhas",
        "price": 18.9,
        "stock": 40,
    },
    {"id": "caneta-azul-esf", "name": "Caneta esferográfica azul", "price": 2.5, "stock": 500},
    {"id": "lapis-hb-n2", "name": "Lápis preto HB nº 2", "price": 1.2, "stock": 0},
    {"id": "cola-branca-90g", "name": "Cola branca 90g", "price": 4.5, "stock": 60},
    {"id": "tesoura-escolar", "name": "Tesoura escolar sem ponta", "price": 7.9, "stock": 30},
]

server = MCPServer("shop")

# The state is the inbox's JSON file (a dict) and how to save it; app.py binds it.
_state: dict[str, Any] = {}


def _no_save() -> None:
    pass


_save: Callable[[], None] = _no_save


def bind(state: dict[str, Any], save: Callable[[], None]) -> None:
    _state["quotes"] = state.setdefault("quotes", {})
    _state["calls"] = state.setdefault("calls", {})
    global _save
    _save = save


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in plain if not unicodedata.combining(c)).split()


@server.tool(
    description="Busca produtos de papelaria pelo nome. Devolve até cinco, com o id de cada um.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def search_product(query: str) -> str:
    wanted = _words(query)
    found = [
        p for p in CATALOG if wanted and all(w in " ".join(_words(p["name"])) for w in wanted)
    ][:5]
    return json.dumps(found, ensure_ascii=False)


@server.tool(
    description="Adiciona um produto ao orçamento do cliente. Use o id devolvido pela busca.",
)
async def add_to_quote(product_id: str, ctx: Context, quantity: int = 1) -> str:
    meta = ctx.request_context.meta or {}
    user_id = meta.get("parley/user_id")
    if not user_id:
        raise ToolError("Não consegui identificar o cliente para montar o orçamento.")
    call_id = meta.get("parley/call_id")
    if call_id and call_id in _state["calls"]:
        return _state["calls"][call_id]  # a repeated call changes nothing
    product = next((p for p in CATALOG if p["id"] == product_id), None)
    if product is None:
        raise ToolError(f"Produto {product_id} não encontrado.")
    if quantity < 1:
        raise ToolError("A quantidade deve ser pelo menos 1.")
    if product["stock"] < quantity:
        raise ToolError(f"{product['name']} está sem estoque suficiente.")
    lines = _state["quotes"].setdefault(user_id, [])
    line = next((x for x in lines if x["product_id"] == product_id), None)
    if line:
        line["quantity"] += quantity
    else:
        lines.append({"product_id": product_id, "name": product["name"], "quantity": quantity})
    answer = f"Adicionado ao orçamento: {quantity} x {product['name']}."
    if call_id:
        _state["calls"][call_id] = answer
        for old in list(_state["calls"])[:-1000]:  # keep the newest 1000
            del _state["calls"][old]
    _save()
    return answer
