"""PRODUCT.md's success demo for categories and waiting lines, end to end.

Everything goes through public entry points: the API client, the webchat
message endpoint and the worker pipeline (ingest -> debounce -> scheduler
tick). Only the models are scripted: the classifier's provider
(`app.agents.categorize.get_provider`) and the agent's own provider, which
asks for a person with the `handoff_to_human` tool. Category, `waiting_since`
and the claim are never written to a row by the test.
"""

import time
import uuid

from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick
from tests.test_categorize_turn import CATEGORIES, DEBOUNCE_SECONDS, Pipeline, _session_factory


def _reply(text: str) -> LLMResponse:
    return LLMResponse(content=text)


def _ask_for_a_person() -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="c1", name="handoff_to_human", arguments={"reason": "pediu"})]
    )


class Demo(Pipeline):
    """The shared pipeline, with several contacts and a scripted agent model."""

    def agent_answers(self, *responses: LLMResponse) -> None:
        self.runtime = AgentRuntime(
            provider=FakeLLMProvider(list(responses)),
            registry=ToolRegistry([BuiltinExecutor()]),
        )

    async def say(self, user_id: str, text: str) -> uuid.UUID:
        response = await self.client.post(
            "/channels/webchat/messages",
            json={"channel_id": self.channel_id, "user_id": user_id, "message": text},
            headers=self.headers,
        )
        assert response.status_code == 201, response.text
        queue = RedisQueue(self.fake_redis)
        while await self.fake_redis.llen(JOBS_KEY) > 0:
            assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
        await tick(
            self.fake_redis,
            _session_factory(self.db_session),
            self.runtime,
            now=time.time() + 5,
        )
        return uuid.UUID(response.json()["conversation_id"])

    async def conversation(self, conversation_id) -> dict:
        response = await self.client.get(f"/conversations/{conversation_id}", headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    async def lines(self) -> dict[str | None, list[str]]:
        response = await self.client.get("/lines", headers=self.headers)
        assert response.status_code == 200, response.text
        return {
            line["category_name"]: [c["id"] for c in line["conversations"]]
            for line in response.json()
        }

    async def listed(self, **params) -> list[str]:
        response = await self.client.get("/conversations", params=params, headers=self.headers)
        assert response.status_code == 200, response.text
        return [c["id"] for c in response.json()]

    async def post(self, path: str, **kwargs):
        return await self.client.post(path, headers=self.headers, **kwargs)

    async def move(self, conversation_id, category_id):
        return await self.client.put(
            f"/conversations/{conversation_id}/category",
            json={"category_id": None if category_id is None else str(category_id)},
            headers=self.headers,
        )


async def test_success_demo(client, fake_redis, db_session, monkeypatch, fake_handoff_brief):
    demo = Demo(client, fake_redis, db_session, monkeypatch)
    # Clause 1: define "billing" and "support" for the tenant (through the API).
    await demo.setup(categories=CATEGORIES)
    billing, support = demo.categories["billing"], demo.categories["support"]
    listed = await client.get("/categories", headers=demo.headers)
    assert [c["name"] for c in listed.json()] == ["billing", "support"]

    # The list is numbered by name: 1 is billing, 2 is support.
    classifier = demo.classifier_answers("1", "2", "1", "2")
    demo.agent_answers(
        _reply("Já vejo o boleto."),
        _reply("Vamos ver o app."),
        _ask_for_a_person(),
        _ask_for_a_person(),
    )

    # Clause 2: a billing question and a support question from two contacts,
    # each conversation filed under the right category.
    ana = await demo.say("ana", "quero a segunda via do boleto")
    bruno = await demo.say("bruno", "o app não abre")
    assert ana != bruno
    assert (await demo.conversation(ana))["category_id"] == str(billing)
    assert (await demo.conversation(bruno))["category_id"] == str(support)
    assert await demo.listed(category_id=str(billing)) == [str(ana)]
    assert await demo.listed(category_id=str(support)) == [str(bruno)]

    # Clause 3: both ask for a person; each waits in its own line, in the
    # order they asked (Ana first).
    await demo.say("ana", "quero falar com uma pessoa")
    await demo.say("bruno", "quero falar com uma pessoa")
    assert classifier.call_count == 4
    assert await demo.lines() == {"billing": [str(ana)], "support": [str(bruno)], None: []}
    ana_waiting = (await demo.conversation(ana))["waiting_since"]
    bruno_waiting = (await demo.conversation(bruno))["waiting_since"]
    assert ana_waiting is not None and bruno_waiting is not None
    assert ana_waiting < bruno_waiting

    # Clause 4: move Ana to the support line; she lands by her original wait
    # time, ahead of Bruno although she joined the line later.
    moved = await demo.move(ana, support)
    assert moved.status_code == 200, moved.text
    assert moved.json()["waiting_since"] == ana_waiting
    assert await demo.lines() == {
        "billing": [],
        "support": [str(ana), str(bruno)],
        None: [],
    }

    # Clause 5: one operator picks her; a second one is refused.
    first = await demo.post(f"/conversations/{ana}/claim", json={"operator_id": "op-1"})
    assert first.status_code == 200, first.text
    assert first.json()["claimed_by"] == "op-1"
    second = await demo.post(f"/conversations/{ana}/claim", json={"operator_id": "op-2"})
    assert second.status_code == 409
    assert second.json()["error"]["message"] == "conversation is already claimed"
    assert (await demo.conversation(ana))["claimed_by"] == "op-1"
    assert (await demo.lines())["support"] == [str(bruno)]
    assert await demo.listed(claimed_by="op-1") == [str(ana)]

    # Clause 6: release puts her back in her place, still ahead of Bruno.
    released = await demo.post(f"/conversations/{ana}/release")
    assert released.status_code == 200, released.text
    assert released.json()["claimed_by"] is None
    assert released.json()["waiting_since"] == ana_waiting
    assert await demo.lines() == {
        "billing": [],
        "support": [str(ana), str(bruno)],
        None: [],
    }


async def test_a_humans_choice_is_not_overwritten_by_the_model(
    client, fake_redis, db_session, monkeypatch
):
    demo = Demo(client, fake_redis, db_session, monkeypatch)
    await demo.setup(categories=CATEGORIES)
    support = demo.categories["support"]
    classifier = demo.classifier_answers("1", "1")
    demo.agent_answers(_reply("ok"), _reply("ok"))

    conversation_id = await demo.say("ana", "quero a segunda via do boleto")
    assert (await demo.conversation(conversation_id))["category_id"] == str(
        demo.categories["billing"]
    )

    moved = await demo.move(conversation_id, support)
    assert moved.status_code == 200, moved.text
    assert moved.json()["category_locked"] is True

    # A later batch the model would file under billing again.
    calls_before = classifier.call_count
    await demo.say("ana", "e o boleto de novo")
    stored = await demo.conversation(conversation_id)
    assert stored["category_id"] == str(support)
    assert classifier.call_count == calls_before


async def test_tenant_without_categories_makes_no_classifier_call(
    client, fake_redis, db_session, monkeypatch, fake_handoff_brief
):
    demo = Demo(client, fake_redis, db_session, monkeypatch)
    await demo.setup()
    classifier = demo.classifier_answers()
    demo.agent_answers(_reply("oi!"), _ask_for_a_person())

    conversation_id = await demo.say("ana", "oi")
    await demo.say("ana", "quero falar com uma pessoa")

    assert classifier.call_count == 0
    assert (await demo.conversation(conversation_id))["category_id"] is None
    # Handed off, it waits in the one uncategorized line.
    assert await demo.lines() == {None: [str(conversation_id)]}
