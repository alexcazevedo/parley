"""Parley as an MCP client: the only module that imports the `mcp` SDK.

`connect(server)` is the only place a connection opens. Before it opens, the
URL passes `assert_safe_url` (the tool address checks, with DNS); the HTTP
client follows no redirects, sends the server's token as a header and waits
at most the server's `timeout_seconds`. Tests replace `connect` with one that
returns `Client(<in-process MCPServer>)` (`tests/mcp_fakes.py`).

`list_tools` and `call_tool` turn every transport or protocol failure into
`McpError(reason)`; the reason is short, safe to store and show, and never
holds the token. `McpError.sent` says whether a request may have reached the
server: false when the address was refused, the connection never opened or
the server refused the token; true for anything else (a timeout, a broken
connection, a protocol error), whose outcome is unknown. A tool's own error
result is not a failure: `call_tool` returns it with `is_error=True`.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
import structlog
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import RequestParamsMeta

from app.models.mcp_server import McpServer
from app.security import secrets as secrets_crypto
from app.tools.ssrf import SSRFError, assert_safe_url

logger = structlog.get_logger(__name__)

SEPARATOR = "__"
EXPOSED_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OAUTH_REASON = "the server requires OAuth; parley supports only a bearer or header token"
# Pages of `tools/list` read at most (a paginator that never ends cannot hang a sync).
MAX_LIST_PAGES = 20
_MAX_REASON_CHARS = 300


class McpError(Exception):
    """A transport or protocol failure talking to an MCP server.

    `sent` is whether a request may have reached the server (so a tool may
    have run). Default true: an unclassified failure has an unknown outcome.
    """

    def __init__(self, reason: str, *, sent: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sent = sent


@dataclass(frozen=True)
class DiscoveredTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]
    read_only: bool


@dataclass(frozen=True)
class McpCallResult:
    text: str
    is_error: bool


def exposed_name(server_name: str, tool_name: str) -> str:
    """The name a model sees for a server's tool."""
    return f"{server_name}{SEPARATOR}{tool_name}"


@dataclass
class _HttpState:
    """What the HTTP layer saw that the SDK's error does not carry."""

    statuses: list[int] = field(default_factory=list)
    asks_oauth: bool = False


def _auth_headers(server: McpServer) -> dict[str, str]:
    """Decrypt `auth_config` at connection time. The token is never logged."""
    if not server.auth_config_encrypted:
        return {}
    try:
        config = json.loads(secrets_crypto.decrypt(server.auth_config_encrypted))
    except Exception:
        logger.warning("mcp.auth_config_unreadable", server=server.name)
        return {}
    if not isinstance(config, dict):
        return {}
    auth_type = str(config.get("type") or "none").lower()
    token = config.get("token") or ""
    if auth_type == "bearer" and token:
        return {"Authorization": f"Bearer {token}"}
    if auth_type == "header" and token:
        return {str(config.get("header_name") or "Authorization"): str(token)}
    return {}


def _http_client(
    server: McpServer,
    state: _HttpState,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """The connection's own HTTP client (`transport` is for tests)."""
    async def record(response: httpx2.Response) -> None:
        state.statuses.append(response.status_code)
        if response.status_code == 401 and response.headers.get("www-authenticate"):
            state.asks_oauth = True

    return httpx2.AsyncClient(
        headers=_auth_headers(server),
        timeout=float(server.timeout_seconds),
        follow_redirects=False,
        event_hooks={"response": [record]},
        transport=transport,
    )


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_REASON_CHARS else text[: _MAX_REASON_CHARS - 3] + "..."


def failure_reason(exc: BaseException, state: _HttpState | None = None) -> str:
    """A short, token-free reason for a failure."""
    if state is not None:
        if state.asks_oauth:
            return OAUTH_REASON
        for status in (401, 403):
            if status in state.statuses:
                return f"the server refused the token (HTTP {status})"
    for leaf in _leaves(exc):
        if isinstance(leaf, McpError):
            return leaf.reason
        if isinstance(leaf, SSRFError):
            return _short(f"address refused: {leaf}")
        if isinstance(leaf, (httpx2.TimeoutException, TimeoutError)):
            return "the server did not answer in time"
        if isinstance(leaf, httpx2.TransportError):
            return "could not reach the server"
        if isinstance(leaf, MCPError):
            return _short(f"protocol error: {leaf.message}")
    leaf = _leaves(exc)[0]
    return _short(f"{type(leaf).__name__}: {leaf}" if str(leaf) else type(leaf).__name__)


def may_have_sent(exc: BaseException, state: _HttpState | None = None) -> bool:
    """Whether a request may have reached the server before `exc`.

    False only when it cannot have: the server refused the token (nothing
    runs behind a 401 or 403), the address was refused, or the connection
    never opened. A read timeout or a connection that broke later is true.
    """
    if state is not None and (state.asks_oauth or {401, 403} & set(state.statuses)):
        return False
    leaves = _leaves(exc)
    if any(isinstance(leaf, McpError) for leaf in leaves):
        return all(leaf.sent for leaf in leaves if isinstance(leaf, McpError))
    return not any(
        isinstance(leaf, (SSRFError, httpx2.ConnectError, httpx2.ConnectTimeout))
        for leaf in leaves
    )


@asynccontextmanager
async def _connect(server: McpServer) -> AsyncIterator[Client]:
    try:
        await assert_safe_url(server.url)
    except SSRFError as exc:
        raise McpError(failure_reason(exc), sent=False) from None
    state = _HttpState()
    try:
        async with _http_client(server, state) as http:
            transport = streamable_http_client(server.url, http_client=http)
            async with Client(transport, read_timeout_seconds=float(server.timeout_seconds)) as c:
                yield c
    except McpError:
        raise
    except Exception as exc:
        raise McpError(failure_reason(exc, state), sent=may_have_sent(exc, state)) from None


def connect(server: McpServer) -> AbstractAsyncContextManager[Client]:
    """An open MCP session with `server` (streamable HTTP)."""
    return _connect(server)


def _overall_timeout(server: McpServer) -> float:
    # Connecting and one request each get `timeout_seconds`; a slow server
    # cannot keep a sync or a call much past that.
    return float(server.timeout_seconds) * 3


async def list_tools(server: McpServer) -> list[DiscoveredTool]:
    """Every tool `server` offers. Raises `McpError` on any failure."""
    found: list[DiscoveredTool] = []
    try:
        async with asyncio.timeout(_overall_timeout(server)):
            async with connect(server) as client:
                cursor: str | None = None
                for _ in range(MAX_LIST_PAGES):
                    page = await client.list_tools(cursor=cursor)
                    for tool in page.tools:
                        annotations = tool.annotations
                        found.append(
                            DiscoveredTool(
                                name=tool.name,
                                description=tool.description,
                                input_schema=dict(tool.input_schema or {}),
                                read_only=bool(annotations and annotations.read_only_hint),
                            )
                        )
                    cursor = page.next_cursor
                    if not cursor:
                        break
    except McpError:
        raise
    except Exception as exc:
        raise McpError(failure_reason(exc), sent=may_have_sent(exc)) from None
    return found


def _text_of(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        kind = getattr(block, "type", "unknown")
        if kind == "text":
            parts.append(str(getattr(block, "text", "")))
        else:
            parts.append(f"[{kind} content omitted]")
    return "\n".join(parts)


async def call_tool(
    server: McpServer, name: str, arguments: dict[str, Any], meta: dict[str, Any]
) -> McpCallResult:
    """Call one tool with `meta` as the request's `_meta`. A tool's error result
    comes back with `is_error=True`; a failure raises `McpError`."""
    try:
        async with asyncio.timeout(_overall_timeout(server)):
            async with connect(server) as client:
                result = await client.call_tool(
                    name, arguments, meta=cast(RequestParamsMeta, meta)
                )
    except McpError:
        raise
    except Exception as exc:
        raise McpError(failure_reason(exc), sent=may_have_sent(exc)) from None
    return McpCallResult(text=_text_of(list(result.content)), is_error=bool(result.is_error))
