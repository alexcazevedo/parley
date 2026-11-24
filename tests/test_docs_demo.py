"""An integrating agent follows the guides literally.

Every request of the demo path is taken from the guide's own text: the curl
commands of `setup.md`, `messaging.md` and `operators.md` are extracted (method,
URL, headers, body), the shell variables are filled from earlier responses and
the request goes through the `client` fixture. A variable no earlier step
produced fails the test with its name. Only the model is scripted
(`FakeLLMProvider`) and the worker is driven by hand (ingest, debounce,
scheduler tick), as in tests/test_categories_lines_demo.py.

A guide that is wrong makes this test fail: fix the guide, not the test.
"""

import json
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.conversations import turn as turn_module
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick
from tests.test_categorize_turn import DEBOUNCE_SECONDS, _session_factory

ROOT = Path(__file__).resolve().parent.parent
GUIDES = ROOT / "docs" / "guides"

_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.S | re.M)
_INLINE_CURL = re.compile(r"`(curl [^`]+)`")
_ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_VAR = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)\}?")
_FLAGS_WITH_ARG = {"-X", "-H", "-d", "-F", "-o", "-w", "--data", "--data-raw"}

# `file=@name` in a guide's curl is replaced with the bytes below.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@dataclass
class Curl:
    guide: str
    method: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    forms: list[str] = field(default_factory=list)
    raw_method: str | None = None

    @property
    def path(self) -> str:
        """The URL without `$BASE_URL` and without the query string."""
        return self.url.removeprefix("$BASE_URL").split("?", 1)[0]


def _read(guide: str) -> str:
    return (GUIDES / f"{guide}.md").read_text()


def _commands(tokens: list[str], guide: str) -> tuple[list[Curl], dict[str, str]]:
    curls: list[Curl] = []
    assigns: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token != "curl":
            match = _ASSIGN.match(token)
            if match and not match.group(2).startswith("$("):
                assigns[match.group(1)] = match.group(2)
            continue
        curl = Curl(guide)
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("-"):
                i += 1
                if token in _FLAGS_WITH_ARG:
                    arg = tokens[i]
                    i += 1
                    if token == "-X":
                        curl.raw_method = arg
                    elif token == "-H":
                        name, _, value = arg.partition(": ")
                        curl.headers[name] = value
                    elif token in ("-d", "--data", "--data-raw"):
                        curl.body = arg
                    elif token == "-F":
                        curl.forms.append(arg)
                continue
            if curl.url or token == "|":
                break
            curl.url = token
            i += 1
        curl.method = curl.raw_method or ("POST" if curl.body or curl.forms else "GET")
        curls.append(curl)
    return curls, assigns


def extract(guide: str) -> tuple[list[Curl], dict[str, str]]:
    """The curl commands and the `NAME=value` assignments of a guide."""
    text = _read(guide)
    curls: list[Curl] = []
    assigns: dict[str, str] = {}
    for block in _FENCE.findall(text):
        if "curl" not in block and not re.search(r"^[A-Z][A-Z0-9_]*=", block, re.M):
            continue
        joined = block.replace("\\\n", " ")
        # `NAME=$(curl ... | jq -r .field)` captures a value: replay the curl.
        joined = re.sub(r"(?m)^[A-Z][A-Z0-9_]*=\$\((?=curl )", "", joined)
        found, got = _commands(shlex.split(joined), guide)
        curls += found
        assigns.update(got)
    outside = _FENCE.sub("", text)
    for span in _INLINE_CURL.findall(outside):
        found, _ = _commands(shlex.split(span), guide)
        curls += found
    return curls, assigns


def find(guide: str, method: str, path: str, contains: str = "") -> Curl:
    """The guide's curl for `METHOD path`; `contains` tells two apart."""
    curls, _ = extract(guide)
    hits = [
        c
        for c in curls
        if c.method == method
        and c.path == path
        and contains in (c.body or "") + " ".join(c.forms) + c.url
    ]
    assert hits, f"{guide}.md has no curl for {method} {path} {contains!r}"
    return hits[0]


def listed(guide: str, method: str, path: str) -> None:
    """The endpoint is written in the guide as `METHOD path`."""
    assert f"`{method} {path}" in _read(guide), f"{guide}.md does not list {method} {path}"


class Agent:
    """Holds the shell variables and replays the guides' requests."""

    def __init__(self, client) -> None:
        self.client = client
        self.vars: dict[str, str] = {"BASE_URL": "http://test"}
        self.replayed = 0
        self.direct = 0

    def fill(self, text: str, placeholders: dict[str, str] | None = None) -> str:
        def lookup(match: re.Match) -> str:
            name = match.group(1)
            assert name in self.vars, f"the guide uses ${name} before any step produced it"
            return self.vars[name]

        for placeholder, value in (placeholders or {}).items():
            text = text.replace(placeholder, value)
        return _VAR.sub(lookup, text)

    async def send(self, curl: Curl, placeholders: dict[str, str] | None = None):
        kwargs: dict = {}
        headers = {k: self.fill(v, placeholders) for k, v in curl.headers.items()}
        if curl.body is not None:
            kwargs["content"] = self.fill(curl.body, placeholders).encode()
            json.loads(kwargs["content"])  # the guide's body is valid JSON once filled
        if curl.forms:
            data, files = {}, {}
            for form in curl.forms:
                name, _, value = self.fill(form, placeholders).partition("=")
                if value.startswith("@"):
                    files[name] = (value[1:], JPEG, "application/octet-stream")
                else:
                    data[name] = value
            kwargs.update(data=data, files=files)
        self.replayed += 1
        return await self.client.request(
            curl.method, self.fill(curl.url, placeholders), headers=headers, **kwargs
        )

    async def run(
        self, guide: str, method: str, path: str, status: int, contains: str = "", **placeholders
    ):
        response = await self.send(find(guide, method, path, contains), placeholders)
        assert response.status_code == status, f"{method} {path}: {response.text}"
        return response

    async def get(self, path: str, status: int = 200):
        """A request the guide gives as a backticked endpoint, not as a curl."""
        self.direct += 1
        response = await self.client.get(
            self.fill(path), headers={"X-API-Key": self.vars["TENANT_KEY"]}
        )
        assert response.status_code == status, response.text
        return response


def _scripted_worker(monkeypatch) -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        summarizer_module,
        "get_provider",
        lambda agent, settings=None: FakeLLMProvider([LLMResponse(content="brief")]),
    )


async def _run_worker(fake_redis, db_session, *replies: str) -> None:
    """Ingest the queued jobs, wait out the debounce, run the turn."""
    runtime = AgentRuntime(
        provider=FakeLLMProvider([LLMResponse(content=r) for r in replies]),
        registry=ToolRegistry([BuiltinExecutor()]),
    )
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


def _top_seq(messages: list[dict], default: int = 0) -> int:
    return max([m["seq"] for m in messages], default=default)


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_the_guides_demo_path(
    client, fake_redis, db_session, monkeypatch, media_root, fake_handoff_brief
):
    import os

    _scripted_worker(monkeypatch)
    me = Agent(client)
    v = me.vars
    v["ADMIN_API_KEY"] = os.environ["ADMIN_API_KEY"]
    for guide in ("setup", "messaging", "operators"):
        _, assigns = extract(guide)
        for name, value in assigns.items():
            if name not in ("BASE_URL", "ADMIN_API_KEY") and not value.startswith("<"):
                v[name] = value

    # setup.md 1: tenant.
    r = await me.run("setup", "POST", "/tenants", 201)
    assert set(r.json()) >= {"id", "name", "api_key"}
    v["TENANT_ID"], v["TENANT_KEY"] = r.json()["id"], r.json()["api_key"]
    r = await me.run("setup", "GET", "/tenants/me", 200)
    assert r.json()["id"] == v["TENANT_ID"] and r.json()["name"] == "acme"

    # setup.md 2: tool.
    r = await me.run("setup", "POST", "/tools", 201)
    assert "auth_config" not in r.json()
    v["TOOL_ID"] = r.json()["id"]
    r = await me.run("setup", "GET", "/tools", 200)
    assert [t["name"] for t in r.json()] == ["get_order"]
    assert "auth_config" not in r.json()[0]

    # setup.md 3: agent that uses the tool.
    r = await me.run("setup", "POST", "/agents", 201)
    v["AGENT_ID"] = r.json()["id"]
    r = await me.run("setup", "GET", "/agents/$AGENT_ID", 200)
    assert r.json()["tool_names"] == ["get_order"]

    # setup.md 4: a web chat channel, and a WhatsApp one.
    r = await me.run("setup", "POST", "/channels", 201, contains="webchat")
    assert "secret" not in r.json()
    v["CHANNEL_ID"] = r.json()["id"]
    r = await me.run("setup", "GET", "/channels/$CHANNEL_ID", 200)
    assert r.json()["agent_id"] == v["AGENT_ID"]
    r = await me.run(
        "setup",
        "POST",
        "/channels",
        201,
        contains="whatsapp_evolution",
        **{"<evolution api key>": "evo-secret"},
    )
    assert "secret" not in r.json()
    whatsapp_id = r.json()["id"]

    # setup.md 5: a contact for the customer that messaging.md writes as.
    r = await me.run("setup", "POST", "/contacts", 201)
    v["CONTACT_ID"] = r.json()["id"]
    await me.run("setup", "GET", "/contacts/$CONTACT_ID", 200)
    for name in ("TENANT_KEY", "TENANT_ID", "AGENT_ID", "TOOL_ID", "CHANNEL_ID", "CONTACT_ID"):
        assert f"`${name}`" in _read("setup"), f"setup.md's table of kept variables lacks {name}"

    # messaging.md 1: send a web chat message.
    r = await me.run("messaging", "POST", "/channels/webchat/messages", 201)
    assert r.json()["duplicate"] is False
    v["CONVERSATION_ID"] = web_conversation = r.json()["conversation_id"]
    r = await me.run("messaging", "GET", "/conversations/$CONVERSATION_ID", 200)
    assert r.json()["state"] == "ACTIVE_AI"
    assert r.json()["contact_id"] == v["CONTACT_ID"], "the setup contact is the customer"

    # messaging.md 2: starting position, then poll. No reply before the worker runs.
    r = await me.run("messaging", "GET", "/conversations/$CONVERSATION_ID/messages", 200)
    v["LAST_SEQ"] = str(_top_seq(r.json()))
    r = await me.run(
        "messaging", "GET", "/conversations/$CONVERSATION_ID/messages", 200, contains="after_seq"
    )
    assert r.json() == []
    await _run_worker(fake_redis, db_session, "Your order left the warehouse today.")
    r = await me.run(
        "messaging", "GET", "/conversations/$CONVERSATION_ID/messages", 200, contains="after_seq"
    )
    replies = [m for m in r.json() if m["role"] == "assistant" and m["direction"] == "out"]
    assert [m["content"] for m in replies] == ["Your order left the warehouse today."]
    v["LAST_SEQ"] = str(_top_seq(r.json(), int(v["LAST_SEQ"])))

    # messaging.md 3: an image through the multipart endpoint.
    r = await me.run("messaging", "POST", "/channels/webchat/media", 201)
    assert r.json()["conversation_id"] == v["CONVERSATION_ID"]
    r = await me.run(
        "messaging", "GET", "/conversations/$CONVERSATION_ID/messages", 200, contains="limit=1"
    )
    assert r.json()[0]["type"] == "image" and r.json()[0]["media_url"]
    v["LAST_SEQ"] = str(_top_seq(r.json(), int(v["LAST_SEQ"])))

    # operators.md 1: hand the web chat conversation to a person.
    v["CONVERSATION_ID"] = web_conversation
    r = await me.run("operators", "POST", "/conversations/$CONVERSATION_ID/handoff", 200)
    assert r.json()["state"] == "HUMAN_HANDOFF" and r.json()["claimed_by"] is None
    assert r.json()["waiting_since"] and r.json()["latest_handoff"] is None
    r = await me.run("operators", "GET", "/conversations/$CONVERSATION_ID", 200)
    assert r.json()["latest_handoff"]["requested_by"] == "operator"
    assert r.json()["latest_handoff"]["reason"] == "customer asked for a person"

    # operators.md 2: the waiting lines.
    r = await me.run("operators", "GET", "/lines", 200)
    assert r.json()[-1]["category_id"] is None
    assert [c["id"] for c in r.json()[-1]["conversations"]] == [v["CONVERSATION_ID"]]

    # operators.md 3: claim.
    r = await me.run("operators", "POST", "/conversations/$CONVERSATION_ID/claim", 200)
    assert r.json()["claimed_by"] == v["OPERATOR_ID"] and r.json()["claimed_at"]
    r = await me.run("operators", "GET", "/lines", 200)
    assert all(not line["conversations"] for line in r.json())
    listed("operators", "GET", "/conversations?state=HUMAN_HANDOFF&claimed_by=$OPERATOR_ID")
    r = await me.get("/conversations?state=HUMAN_HANDOFF&claimed_by=$OPERATOR_ID")
    assert [c["id"] for c in r.json()] == [v["CONVERSATION_ID"]]

    # operators.md 4: read; messages are read as in messaging.md step 2.
    r = await me.get("/conversations/$CONVERSATION_ID/messages?after_seq=$LAST_SEQ")
    assert r.json() == []

    # operators.md 5: reply as the operator; the next poll shows it.
    r = await me.run("operators", "POST", "/conversations/$CONVERSATION_ID/messages", 201)
    assert r.json()["role"] == "human_operator" and r.json()["direction"] == "out"
    r = await me.get("/conversations/$CONVERSATION_ID/messages?after_seq=$LAST_SEQ")
    assert [m["role"] for m in r.json()] == ["human_operator"]
    v["LAST_SEQ"] = str(_top_seq(r.json()))

    # operators.md 6: resume. A second resume is refused with the guide's message.
    r = await me.run("operators", "POST", "/conversations/$CONVERSATION_ID/resume", 200)
    assert r.json()["state"] == "ACTIVE_AI"
    assert r.json()["claimed_by"] is None and r.json()["waiting_since"] is None
    again = await me.send(find("operators", "POST", "/conversations/$CONVERSATION_ID/resume"))
    assert again.status_code == 409
    assert again.json()["error"]["message"] == "cannot resume a conversation in state ACTIVE_AI"

    # operators.md 7: the inbox filter of the guide, on a conversation that is claimed again.
    await me.run("operators", "POST", "/conversations/$CONVERSATION_ID/handoff", 200)
    await me.run("operators", "POST", "/conversations/$CONVERSATION_ID/claim", 200)
    r = await me.run("operators", "GET", "/conversations", 200, contains="claimed_by")
    assert [c["id"] for c in r.json()] == [v["CONVERSATION_ID"]]
    assert r.json()[0]["latest_handoff"]["reason"]

    # operators.md 8: a category, then the human's choice of it.
    r = await me.run("operators", "POST", "/categories", 201)
    v["CATEGORY_ID"] = r.json()["id"]
    r = await me.run("operators", "PUT", "/conversations/$CONVERSATION_ID/category", 200)
    assert r.json()["category_id"] == v["CATEGORY_ID"] and r.json()["category_locked"] is True
    r = await me.run("operators", "GET", "/lines", 200)
    assert [line["category_name"] for line in r.json()] == ["billing", None]

    # operators.md 6 again: resume, and the agent answers the next message.
    await me.run("operators", "POST", "/conversations/$CONVERSATION_ID/resume", 200)
    await me.run("messaging", "POST", "/channels/webchat/messages", 201)
    await _run_worker(fake_redis, db_session, "Anything else?")
    r = await me.get("/conversations/$CONVERSATION_ID/messages?after_seq=$LAST_SEQ")
    assert [m["content"] for m in r.json() if m["role"] == "assistant"] == ["Anything else?"]

    # messaging.md 4 and 5: a WhatsApp channel and its webhook (its own channel id).
    # No worker run after it: Evolution is not reachable from the tests.
    assert "`POST $BASE_URL/webhooks/whatsapp/$CHANNEL_ID`" in _read("messaging")
    v["CHANNEL_ID"] = whatsapp_id
    r = await me.run("messaging", "GET", "/channels/$CHANNEL_ID", 200)
    assert r.json()["type"] == "whatsapp_evolution"
    assert r.json()["config"]["base_url"] and r.json()["config"]["instance"]
    v["EVOLUTION_KEY"] = "evo-secret"
    r = await me.run("messaging", "POST", "/webhooks/whatsapp/$CHANNEL_ID", 200)
    assert r.json() == {"status": "ok"}
    r = await me.run("messaging", "POST", "/webhooks/whatsapp/$CHANNEL_ID", 200)
    assert r.json() == {"status": "ok"}
    r = await me.run("messaging", "GET", "/conversations", 200, contains="limit=100")
    assert "5511999990000" in [c["contact_external_user_id"] for c in r.json()]

    assert me.replayed > 30
    print(f"curl requests replayed from the guides: {me.replayed}; called directly: {me.direct}")


def test_llms_txt_lists_every_guide_on_disk_and_nothing_else():
    text = (ROOT / "llms.txt").read_text()
    linked = re.findall(r"\]\(docs/guides/([^)]+)\)", text)
    on_disk = sorted(p.name for p in GUIDES.glob("*.md"))
    assert sorted(linked) == on_disk
    assert len(linked) == len(set(linked))
    assert not [line for line in text.splitlines() if "docs/guides/" in line and "](" not in line]


async def test_every_guide_is_served_as_it_is_on_disk(client):
    for path in sorted(GUIDES.glob("*.md")):
        response = await client.get(f"/docs/guides/{path.name}")
        assert response.status_code == 200, path.name
        assert response.text == path.read_text(), path.name
    served = await client.get("/llms.txt")
    assert served.text == (ROOT / "llms.txt").read_text()
