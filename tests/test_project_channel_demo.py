"""PRODUCT.md's success demo as one test: the example project and parley, wired
in-process. Parley's app runs the real code; only the model is scripted
(`FakeLLMProvider`). The example's client reaches parley through
`httpx.ASGITransport`, and parley's pushes reach the example the same way
(`deliver_due`, called by the test). Assertions are on the example's
`GET /inbox`, what the project's user sees.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.agents.categorize as categorize_module
from app.events.delivery import deliver_due
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.main import app as parley_app
from app.models.channel_event import ChannelEvent
from examples.project_inbox.app import create_app
from tests.test_external_channel import _agent, _channel, _run_turn, _tenant

T0 = datetime(2030, 1, 1, tzinfo=UTC)
USER = "ana"


class PushRoute(httpx.AsyncBaseTransport):
    """Where parley's pushes go: the example app, or a 503 during an outage."""

    def __init__(self) -> None:
        self.up = True
        self.target: httpx.AsyncBaseTransport | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.up or self.target is None:
            return httpx.Response(503)
        return await self.target.handle_async_request(request)


async def test_the_success_demo(client, db_session, test_engine, monkeypatch, tmp_path):
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    # The agent's category choice: the second of the tenant's categories.
    classifier = FakeLLMProvider([LLMResponse(content="2")], repeat_last=True)
    monkeypatch.setattr(categorize_module, "get_provider", lambda agent, settings=None: classifier)

    # 1. Tenant, agent, a category, an external channel pushing to the example.
    headers = await _tenant(client)
    agent_id = await _agent(client, headers)
    for name, description in (("billing", "invoices"), ("support", "product help")):
        made = await client.post(
            "/categories", json={"name": name, "description": description}, headers=headers
        )
        assert made.status_code == 201, made.text
    events_url = f"https://inbox-{uuid.uuid4().hex[:10]}.example.com/parley/events"
    created = await _channel(client, headers, agent_id, config={"events_url": events_url})
    assert created.status_code == 201, created.text
    channel_id, secret = created.json()["id"], created.json()["events_secret"]

    state_path = tmp_path / "inbox.json"
    route = PushRoute()
    push_http = httpx.AsyncClient(transport=route)
    clock = {"now": T0}

    def start_example() -> httpx.AsyncClient:
        """The example project, started (again) from its state file."""
        example = create_app(
            state_path,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=parley_app)),
            parley_url="http://test",
            api_key=headers["X-API-Key"],
            channel_id=channel_id,
            events_secret=secret,
        )
        route.target = httpx.ASGITransport(app=example)
        return httpx.AsyncClient(transport=route.target, base_url="http://inbox")

    inbox_app = start_example()

    async def inbox() -> dict:
        return (await inbox_app.get("/inbox")).json()["customers"][USER]

    async def agent_texts() -> list[str]:
        return [m["text"] for m in (await inbox())["messages"] if m["from"] == "agent"]

    async def push(step: timedelta = timedelta(minutes=2)) -> int:
        clock["now"] += step
        return await deliver_due(factory, push_http, now=clock["now"])

    async def customer_says(text: str, reply: str) -> None:
        sent = await inbox_app.post("/customer", json={"user_id": USER, "text": text})
        assert sent.status_code == 200, sent.text
        await _run_turn(db_session, uuid.UUID((await inbox())["conversation_id"]), reply)

    # 2. A customer message: after the turn and the push, the reply is in the inbox.
    await customer_says("Where is my order?", "It leaves today.")
    await push()
    assert await agent_texts() == ["It leaves today."]
    assert (await inbox())["agent_active"] is True

    # 3. The department the agent chose shows in the inbox.
    assert (await inbox())["department"] == "support"

    # 4. A staff message pauses the agent; the next customer message gets no reply.
    staff = await inbox_app.post(
        "/staff", json={"user_id": USER, "text": "I am on it.", "name": "Marina"}
    )
    assert staff.status_code == 200, staff.text
    await push()
    assert (await inbox())["agent_active"] is False
    await customer_says("Any news?", "must not be sent")
    await push()
    assert await agent_texts() == ["It leaves today."]

    # 5. Hand back: the agent is on again and the next message is answered.
    handed = await inbox_app.post("/hand-back", json={"user_id": USER})
    assert handed.status_code == 200, handed.text
    await push()
    assert (await inbox())["agent_active"] is True
    await customer_says("Thanks, and now?", "Delivered tomorrow.")
    await push()
    assert await agent_texts() == ["It leaves today.", "Delivered tomorrow."]

    # 6. Outage: nothing arrives; after a restart the feed brings the reply, once.
    route.up = False
    await customer_says("Where is it now?", "At the depot.")
    assert await push() == 0
    assert await agent_texts() == ["It leaves today.", "Delivered tomorrow."]
    route.up = True
    inbox_app = start_example()  # same state file
    assert await agent_texts() == ["It leaves today.", "Delivered tomorrow."]
    caught = await inbox_app.post("/catch-up")
    assert caught.status_code == 200, caught.text
    assert await agent_texts() == ["It leaves today.", "Delivered tomorrow.", "At the depot."]
    assert await push(timedelta(hours=1)) >= 1  # parley retries the same event
    assert await agent_texts() == ["It leaves today.", "Delivered tomorrow.", "At the depot."]
    before = (await inbox_app.get("/inbox")).json()["seq"]
    assert (await inbox_app.post("/catch-up")).json() == {"applied": 0}
    assert (await inbox_app.get("/inbox")).json()["seq"] == before

    # 7. Parley gives up on one event, pushes a later one; catch-up finds both.
    seen = ["It leaves today.", "Delivered tomorrow.", "At the depot."]
    route.up = False
    await customer_says("And the invoice?", "Sent by email.")
    for _ in range(12):
        await push(timedelta(hours=2))
        async with factory() as session:
            gave_up = await session.scalar(
                select(ChannelEvent.id).where(
                    ChannelEvent.channel_id == uuid.UUID(channel_id),
                    ChannelEvent.gave_up_at.is_not(None),
                )
            )
        if gave_up is not None:
            break
    assert gave_up is not None, "parley never gave up on the event"
    route.up = True
    await customer_says("Thank you!", "You are welcome.")
    assert await push() == 1
    assert await agent_texts() == [*seen, "You are welcome."]  # the given-up one is missing
    caught = await inbox_app.post("/catch-up")
    assert caught.status_code == 200, caught.text
    assert sorted(await agent_texts()) == sorted([*seen, "Sent by email.", "You are welcome."])
    assert (await inbox_app.post("/catch-up")).json() == {"applied": 0}
    assert sorted(await agent_texts()) == sorted([*seen, "Sent by email.", "You are welcome."])
