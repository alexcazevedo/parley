"""A project's own inbox on top of parley's `external` channel.

Everything a real project must do, in one file, importing nothing from parley:

  1. Forward what customers and staff write:  POST /channels/external/messages
  2. Hand a conversation back to the agent:   POST /conversations/{id}/resume
  3. Receive pushed events and check their signature (POST /parley/events)
  4. Catch up from the feed after a restart or a missed push
     (GET /channels/{channel_id}/events?after=<seq>)

State is one JSON file (INBOX_STATE_PATH): the inbox, the feed position (`seq`)
and the ids of the events already applied. A real project would use its
database. The position moves only when the feed is read, never on a push: after
parley gives up on one event it pushes the later ones, and a position moved by
them would skip it. Catch-up also runs every CATCH_UP_SECONDS (default 30),
because a given-up event produces no signal.
Config: PARLEY_URL, PARLEY_API_KEY, PARLEY_CHANNEL_ID, PARLEY_EVENTS_SECRET.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger("project_inbox")

TOLERANCE_SECONDS = 300  # refuse a push whose timestamp is older (replay)
KEEP_EVENT_IDS = 1000  # how many applied event ids to remember


class CustomerIn(BaseModel):
    user_id: str
    text: str


class StaffIn(BaseModel):
    user_id: str
    text: str
    name: str


class HandBackIn(BaseModel):
    user_id: str


def signature_ok(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Same check parley documents: HMAC-SHA256 of "<timestamp>.<raw body>"."""
    try:
        if abs(time.time() - int(timestamp)) > TOLERANCE_SECONDS:
            return False
    except ValueError:
        return False
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    # compare_digest: no timing leak about how much of the signature matched
    return hmac.compare_digest("sha256=" + mac.hexdigest(), signature)


class Inbox:
    """The state file plus the rules for applying one event to it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        empty: dict[str, Any] = {"seq": 0, "applied": [], "customers": {}, "conversations": {}}
        self.state = json.loads(path.read_text()) if path.exists() else empty

    def save(self) -> None:
        # write then rename, so a crash never leaves half a file
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(self.path)

    def customer(self, user_id: str) -> dict[str, Any]:
        blank = {
            "messages": [],
            "agent_active": True,
            "department": None,
            "handoff_brief": None,
            "conversation_id": None,
        }
        return self.state["customers"].setdefault(user_id, blank)

    def add_message(self, user_id: str, sender: str, text: str, **extra: Any) -> None:
        self.customer(user_id)["messages"].append({"from": sender, "text": text, **extra})

    def remember_conversation(self, user_id: str, conversation_id: str) -> None:
        self.customer(user_id)["conversation_id"] = conversation_id
        self.state["conversations"][conversation_id] = user_id
        self.save()

    def apply(self, event: dict[str, Any]) -> bool:
        """Apply one event; False when this event id was already applied.
        Does not touch the feed position: only reading the feed moves it."""
        if event["id"] in self.state["applied"]:
            return False  # parley pushes at least once, so duplicates are normal
        data = event["data"]
        conversation_id = event["conversation_id"]
        # Events name the conversation, not your user: map it back. A
        # conversation this inbox never saw is kept under its own id.
        user_id = self.state["conversations"].get(conversation_id, conversation_id)
        customer = self.customer(user_id)
        kind = event["type"]
        if kind == "message.reply":
            self.add_message(user_id, "agent", data["content"], message_id=data["message_id"])
        elif kind == "conversation.handoff":
            customer["agent_active"] = False
            customer["handoff_brief"] = data.get("brief") or customer["handoff_brief"]
        elif kind == "conversation.handoff_brief":
            customer["handoff_brief"] = data["brief"]
        elif kind == "conversation.agent":
            customer["agent_active"] = data["active"]
            if data["active"]:
                customer["handoff_brief"] = None  # handed back: the brief is stale
        elif kind == "conversation.category":
            customer["department"] = data["name"]
        # An unknown type is stored as seen and otherwise ignored: parley may
        # add event types later, and an old inbox must not break on them.
        applied = self.state["applied"]
        applied.append(event["id"])
        del applied[:-KEEP_EVENT_IDS]
        self.save()
        return True

    def feed_read(self, seq: int) -> None:
        """The feed was read up to `seq`: that is the position to resume from."""
        if seq > self.state["seq"]:
            self.state["seq"] = seq
            self.save()


def create_app(
    state_path: str | Path | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    parley_url: str | None = None,
    api_key: str | None = None,
    channel_id: str | None = None,
    events_secret: str | None = None,
    catch_up_seconds: float | None = None,
) -> FastAPI:
    """Build the app. Arguments default to the environment; tests inject a
    client (with a MockTransport standing in for parley) and a state path."""
    parley_url = (parley_url or os.environ.get("PARLEY_URL", "http://api:8000")).rstrip("/")
    api_key = api_key or os.environ.get("PARLEY_API_KEY", "")
    channel_id = channel_id or os.environ.get("PARLEY_CHANNEL_ID", "")
    events_secret = events_secret or os.environ.get("PARLEY_EVENTS_SECRET", "")
    every = catch_up_seconds or float(os.environ.get("CATCH_UP_SECONDS", "30"))
    inbox = Inbox(Path(state_path or os.environ.get("INBOX_STATE_PATH", "/tmp/inbox.json")))
    http = client or httpx.AsyncClient(timeout=10.0)
    headers = {"X-API-Key": api_key}

    async def catch_up() -> int:
        """Read the feed after the stored position. Applying is idempotent by
        event id, so what a push already delivered is skipped."""
        applied = 0
        while True:
            reply = await http.get(
                f"{parley_url}/channels/{channel_id}/events",
                params={"after": inbox.state["seq"], "limit": 100},
                headers=headers,
            )
            reply.raise_for_status()
            events = reply.json()
            for event in events:
                applied += inbox.apply(event)
                inbox.feed_read(event["seq"])
            if len(events) < 100:
                return applied

    async def catch_up_forever() -> None:
        while True:
            try:
                await catch_up()
            except Exception:  # parley down or starting: log, try again later
                logger.warning("catch-up failed", exc_info=True)
            await asyncio.sleep(every)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(catch_up_forever())  # first run is at start
        yield
        task.cancel()
        if client is None:
            await http.aclose()

    app = FastAPI(title="project inbox (parley example)", lifespan=lifespan)

    async def forward(user_id: str, text: str, **extra: Any) -> None:
        reply = await http.post(
            f"{parley_url}/channels/external/messages",
            headers=headers,
            json={"channel_id": channel_id, "user_id": user_id, "message": text, **extra},
        )
        if reply.status_code >= 400:
            raise HTTPException(502, f"parley answered {reply.status_code}")
        inbox.remember_conversation(user_id, reply.json()["conversation_id"])

    @app.post("/customer")
    async def customer_message(body: CustomerIn) -> dict[str, str]:
        inbox.add_message(body.user_id, "customer", body.text)
        await forward(body.user_id, body.text, sender="customer")
        return {"status": "forwarded"}

    @app.post("/staff")
    async def staff_message(body: StaffIn) -> dict[str, str]:
        # A staff message pauses the agent; parley then pushes conversation.agent.
        inbox.add_message(body.user_id, "staff", body.text, name=body.name)
        await forward(body.user_id, body.text, sender="staff", staff_name=body.name)
        return {"status": "forwarded"}

    @app.post("/hand-back")
    async def hand_back(body: HandBackIn) -> dict[str, str]:
        conversation_id = inbox.customer(body.user_id)["conversation_id"]
        if conversation_id is None:
            raise HTTPException(404, "no conversation for this user yet")
        reply = await http.post(
            f"{parley_url}/conversations/{conversation_id}/resume", headers=headers
        )
        if reply.status_code >= 400:
            raise HTTPException(502, f"parley answered {reply.status_code}")
        return {"status": "resumed"}  # agent_active flips when the push arrives

    @app.post("/parley/events")
    async def receive_event(request: Request) -> dict[str, bool]:
        body = await request.body()  # the signature covers these exact bytes
        if not signature_ok(
            events_secret,
            request.headers.get("X-Parley-Timestamp", ""),
            body,
            request.headers.get("X-Parley-Signature", ""),
        ):
            raise HTTPException(401, "bad signature")
        # Any 2xx tells parley "got it"; anything else makes it retry later.
        return {"applied": inbox.apply(json.loads(body))}

    @app.post("/catch-up")
    async def catch_up_route() -> dict[str, int]:
        return {"applied": await catch_up()}

    @app.get("/inbox")
    async def show_inbox() -> dict[str, Any]:
        return {"seq": inbox.state["seq"], "customers": inbox.state["customers"]}

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return PAGE

    return app


# API data goes into the page with textContent only, never innerHTML.
PAGE = """<!doctype html>
<meta charset="utf-8"><title>Project inbox</title>
<h1>Project inbox</h1><div id="inbox"></div>
<script>
fetch("/inbox").then(r => r.json()).then(data => {
  const root = document.getElementById("inbox");
  for (const [user, c] of Object.entries(data.customers)) {
    const box = document.createElement("section");
    const title = document.createElement("h2");
    title.textContent = user + " (agent " + (c.agent_active ? "on" : "off") +
      ", department: " + (c.department || "none") + ")";
    box.append(title);
    if (c.handoff_brief) {
      const brief = document.createElement("p");
      brief.textContent = "Handoff brief: " + c.handoff_brief;
      box.append(brief);
    }
    for (const m of c.messages) {
      const line = document.createElement("div");
      line.textContent = m.from + ": " + m.text;
      box.append(line);
    }
    root.append(box);
  }
});
</script>
"""

app = create_app()
