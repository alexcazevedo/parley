"""The example project (examples/project_inbox) with parley faked by MockTransport."""

import hashlib
import hmac
import json
import time

import httpx
import pytest

from examples.project_inbox.app import create_app

SECRET = "s3cret"
CHANNEL = "11111111-1111-1111-1111-111111111111"
CONVERSATION = "22222222-2222-2222-2222-222222222222"


def _event(seq: int, kind: str = "message.reply", data: dict | None = None) -> dict:
    return {
        "id": f"00000000-0000-0000-0000-{seq:012d}",
        "seq": seq,
        "type": kind,
        "channel_id": CHANNEL,
        "conversation_id": CONVERSATION,
        "created_at": "2026-01-01T00:00:00+00:00",
        "data": data
        if data is not None
        else {"message_id": f"m{seq}", "seq": seq, "content": f"r{seq}"},
    }


def _signed(event: dict, secret: str = SECRET, timestamp: int | None = None) -> dict:
    body = json.dumps(event, separators=(",", ":")).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {
        "content": body,
        "headers": {
            "X-Parley-Timestamp": ts,
            "X-Parley-Signature": "sha256=" + mac,
            "content-type": "application/json",
        },
    }


class FakeParley:
    def __init__(self, feed: list[dict] | None = None) -> None:
        self.feed = feed or []
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/channels/external/messages":
            return httpx.Response(
                201, json={"conversation_id": CONVERSATION, "message_id": "x", "duplicate": False}
            )
        if path.endswith("/resume"):
            return httpx.Response(200, json={})
        if path == f"/channels/{CHANNEL}/events":
            after = int(request.url.params["after"])
            return httpx.Response(200, json=[e for e in self.feed if e["seq"] > after])
        return httpx.Response(404)


def _build(tmp_path, parley: FakeParley):
    app = create_app(
        tmp_path / "state.json",
        httpx.AsyncClient(transport=httpx.MockTransport(parley)),
        parley_url="http://parley",
        api_key="key",
        channel_id=CHANNEL,
        events_secret=SECRET,
    )
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://inbox")


async def _inbox(client) -> dict:
    return (await client.get("/inbox")).json()


async def test_signed_push_is_applied_once(tmp_path):
    app, client = _build(tmp_path, FakeParley())
    await client.post("/customer", json={"user_id": "ana", "text": "oi"})
    push = _signed(_event(1))
    assert (await client.post("/parley/events", **push)).json() == {"applied": True}
    assert (await client.post("/parley/events", **push)).json() == {"applied": False}
    inbox = await _inbox(client)
    ana = inbox["customers"]["ana"]
    assert [m["from"] for m in ana["messages"]] == ["customer", "agent"]
    assert inbox["seq"] == 0  # a push never moves the feed position


async def test_bad_signature_is_401_and_changes_nothing(tmp_path):
    _, client = _build(tmp_path, FakeParley())
    wrong = _signed(_event(1), secret="other")
    old = _signed(_event(2), timestamp=int(time.time()) - 1000)
    for push in (wrong, old):
        assert (await client.post("/parley/events", **push)).status_code == 401
    assert (await client.post("/parley/events", content=b"{}")).status_code == 401
    assert await _inbox(client) == {"seq": 0, "customers": {}}


async def test_catch_up_applies_only_events_not_already_pushed(tmp_path):
    parley = FakeParley([_event(1), _event(2), _event(3)])
    _, client = _build(tmp_path, parley)
    await client.post("/parley/events", **_signed(_event(1)))
    assert (await client.post("/catch-up")).json() == {"applied": 2}
    assert (await client.post("/catch-up")).json() == {"applied": 0}
    feed_calls = [c for c in parley.calls if c.url.path.endswith("/events")]
    assert feed_calls[0].url.params["after"] == "0"  # the push did not move it
    assert feed_calls[1].url.params["after"] == "3"
    assert (await _inbox(client))["seq"] == 3


async def test_push_after_a_given_up_event_does_not_skip_it(tmp_path):
    parley = FakeParley([_event(1), _event(2), _event(3)])
    _, client = _build(tmp_path, parley)
    await client.post("/customer", json={"user_id": "ana", "text": "oi"})
    await client.post("/parley/events", **_signed(_event(1)))
    await client.post("/parley/events", **_signed(_event(3)))  # event 2 never pushed
    assert (await _inbox(client))["seq"] == 0
    assert (await client.post("/catch-up")).json() == {"applied": 1}
    texts = [m["text"] for m in (await _inbox(client))["customers"]["ana"]["messages"]]
    assert texts == ["oi", "r1", "r3", "r2"]  # 2 once, 3 not again
    assert (await client.post("/catch-up")).json() == {"applied": 0}


async def test_applied_ids_are_kept_in_the_state_file_and_trimmed(tmp_path):
    parley = FakeParley([_event(1)])
    _, client = _build(tmp_path, parley)
    await client.post("/parley/events", **_signed(_event(1)))
    _, again = _build(tmp_path, parley)  # restart: the id is remembered
    assert (await again.post("/catch-up")).json() == {"applied": 0}
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["applied"] == [_event(1)["id"]]
    for seq in range(2, 1100):
        await client.post("/parley/events", **_signed(_event(seq)))
    state = json.loads((tmp_path / "state.json").read_text())
    assert len(state["applied"]) == 1000 and state["applied"][-1] == _event(1099)["id"]


async def test_state_survives_rebuilding_the_app(tmp_path):
    parley = FakeParley()
    _, client = _build(tmp_path, parley)
    await client.post("/customer", json={"user_id": "ana", "text": "oi"})
    await client.post("/parley/events", **_signed(_event(4)))
    before = await _inbox(client)
    _, again = _build(tmp_path, parley)
    assert await _inbox(again) == before
    assert before["seq"] == 0
    assert (await _inbox(client))["customers"]["ana"]["messages"][-1]["text"] == "r4"


async def test_events_update_handoff_state_and_hand_back_resumes(tmp_path):
    parley = FakeParley()
    _, client = _build(tmp_path, parley)
    await client.post("/staff", json={"user_id": "ana", "text": "ja vou", "name": "Rui"})
    sent = json.loads(parley.calls[0].content)
    assert sent["sender"] == "staff" and sent["staff_name"] == "Rui"
    assert parley.calls[0].headers["X-API-Key"] == "key"
    pushes = [
        _event(
            1, "conversation.handoff", {"reason": "x", "requested_by": "operator", "brief": "b"}
        ),
        _event(2, "conversation.agent", {"active": False, "cause": "staff_message"}),
        _event(
            3, "conversation.category", {"category_id": "c", "name": "billing", "source": "agent"}
        ),
    ]
    for event in pushes:
        await client.post("/parley/events", **_signed(event))
    ana = (await _inbox(client))["customers"]["ana"]
    assert (ana["agent_active"], ana["department"], ana["handoff_brief"]) == (False, "billing", "b")
    assert (await client.post("/hand-back", json={"user_id": "ana"})).status_code == 200
    assert parley.calls[-1].url.path == f"/conversations/{CONVERSATION}/resume"
    await client.post(
        "/parley/events",
        **_signed(_event(4, "conversation.agent", {"active": True, "cause": "resumed"})),
    )
    ana = (await _inbox(client))["customers"]["ana"]
    assert (ana["agent_active"], ana["handoff_brief"]) == (True, None)
    assert "textContent" in (await client.get("/")).text


@pytest.mark.parametrize("path", ["/customer", "/staff"])
async def test_parley_error_is_502(tmp_path, path):
    app = create_app(
        tmp_path / "s.json",
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        parley_url="http://parley",
        api_key="k",
        channel_id=CHANNEL,
        events_secret=SECRET,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://inbox")
    reply = await client.post(path, json={"user_id": "a", "text": "t", "name": "n"})
    assert reply.status_code == 502
