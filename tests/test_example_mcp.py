"""The example project's MCP tools, driven in process (no real connection)."""

import json

import httpx
from mcp import Client

from examples.project_inbox import tools
from examples.project_inbox.app import create_app


def _app(tmp_path, **kw):
    mock = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    return create_app(tmp_path / "s.json", mock, catch_up_seconds=3600, **kw)


def _text(result) -> str:
    return result.content[0].text


async def _add(c, product, user="ana", call_id=None, **args):
    meta = {"parley/user_id": user, **({"parley/call_id": call_id} if call_id else {})}
    if user is None:
        meta = {}
    return await c.call_tool("add_to_quote", {"product_id": product, **args}, meta=meta)


async def test_search_finds_both_cartolinas_ignoring_case_and_accents(tmp_path):
    _app(tmp_path)
    async with Client(tools.server) as c:
        found = json.loads(
            _text(await c.call_tool("search_product", {"query": "CARTOLÍNA Branca"}))
        )
        assert [p["id"] for p in found] == ["cartolina-branca-150g", "cartolina-branca-180g"]
        assert json.loads(_text(await c.call_tool("search_product", {"query": "xyz"}))) == []


async def test_add_lands_in_that_users_quote_only(tmp_path):
    app = _app(tmp_path)
    async with Client(tools.server) as c:
        r = await _add(c, "cola-branca-90g", user="ana", quantity=2)
        assert not r.is_error
        await _add(c, "tesoura-escolar", user="bia")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as h:
        quotes = (await h.get("/quotes")).json()
    assert quotes["ana"] == [
        {"product_id": "cola-branca-90g", "name": "Cola branca 90g", "quantity": 2}
    ]
    assert [x["product_id"] for x in quotes["bia"]] == ["tesoura-escolar"]


async def test_same_call_id_adds_nothing_and_answers_the_same(tmp_path):
    _app(tmp_path)
    async with Client(tools.server) as c:
        first = await _add(c, "cola-branca-90g", call_id="c1")
        again = await _add(c, "cola-branca-90g", call_id="c1")
    assert _text(first) == _text(again)
    assert tools._state["quotes"]["ana"][0]["quantity"] == 1


async def test_errors_are_error_results(tmp_path):
    _app(tmp_path)
    async with Client(tools.server) as c:
        unknown = await _add(c, "nao-existe")
        no_stock = await _add(c, "lapis-hb-n2")
        no_user = await _add(c, "cola-branca-90g", user=None)
    assert unknown.is_error and "não encontrado" in _text(unknown)
    assert no_stock.is_error and "estoque" in _text(no_stock)
    assert no_user.is_error
    assert tools._state["quotes"] == {}


async def test_search_is_marked_read_only(tmp_path):
    _app(tmp_path)
    async with Client(tools.server) as c:
        listed = {t.name: t for t in (await c.list_tools()).tools}
    assert listed["search_product"].annotations.read_only_hint is True
    assert not listed["add_to_quote"].annotations


async def test_http_needs_the_token_and_accepts_the_compose_host(tmp_path):
    app = _app(tmp_path, tools_token="tok")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    accept = {"accept": "application/json, text/event-stream"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://project-inbox:9200"
        ) as h:
            assert (await h.post("/mcp", json=init, headers=accept)).status_code == 401
            bad = {**accept, "authorization": "Bearer nope"}
            assert (await h.post("/mcp", json=init, headers=bad)).status_code == 401
            ok = {**accept, "authorization": "Bearer tok"}
            assert (await h.post("/mcp", json=init, headers=ok)).status_code == 200
        other = httpx.AsyncClient(transport=transport, base_url="http://evil.example")
        async with other as h:
            r = await h.post("/mcp", json=init, headers={**accept, "authorization": "Bearer tok"})
            assert r.status_code == 421
