"""`categorize_conversation`: the model files a conversation under a tenant category.

The provider is a `FakeLLMProvider` (or a small double where the call itself
must misbehave), patched in as `app.agents.categorize.get_provider`.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select, text, update
from structlog.testing import capture_logs

import app.agents.categorize as categorize_module
from app.agents.categorize import categorize_conversation
from app.config import Settings
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.category import Category
from app.models.conversation import Conversation
from app.models.message import Message

BATCH = "quero a segunda via do boleto"


async def _setup(client, db_session, categories=(("billing", "invoices"), ("support", "help"))):
    """A tenant with `categories`, one conversation; returns (conversation, agent, ids, cats)."""
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "x", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    created = {}
    for name, description in categories:
        response = await client.post(
            "/categories", json={"name": name, "description": description}, headers=headers
        )
        assert response.status_code == 201, response.text
        created[name] = uuid.UUID(response.json()["id"])
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "u", "message": BATCH},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    conversation = await db_session.get(Conversation, uuid.UUID(sent.json()["conversation_id"]))
    ids = list(
        (
            await db_session.execute(
                select(Message.id).where(Message.conversation_id == conversation.id)
            )
        ).scalars()
    )
    agent_row = await db_session.get(Agent, uuid.UUID(agent.json()["id"]))
    return conversation, agent_row, ids, created


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr(categorize_module, "get_provider", lambda agent, settings=None: provider)
    return provider


def _fake(monkeypatch, *answers: str) -> FakeLLMProvider:
    return _patch_provider(
        monkeypatch, FakeLLMProvider([LLMResponse(content=answer) for answer in answers])
    )


async def _stored_category(db_session, conversation_id):
    return (
        await db_session.execute(
            select(Conversation.category_id).where(Conversation.id == conversation_id)
        )
    ).scalar_one()


async def _categorize(db_session, conversation, agent, ids):
    await categorize_conversation(db_session, conversation, agent, BATCH, message_ids=ids)


async def test_answer_files_and_a_later_answer_moves(client, db_session, monkeypatch):
    conversation, agent, ids, cats = await _setup(client, db_session)
    provider = _fake(monkeypatch, "1", " 2\n")

    with capture_logs() as logs:
        await _categorize(db_session, conversation, agent, ids)
    # Categories are numbered in name order: 1 is billing.
    assert conversation.category_id == cats["billing"]
    assert await _stored_category(db_session, conversation.id) == cats["billing"]
    assert any(log["event"] == "category.assigned" for log in logs)

    await _categorize(db_session, conversation, agent, ids)
    # The write is committed, and the loaded object is clean: nothing for a
    # later flush of the turn to write again.
    assert db_session.in_transaction() is False
    assert conversation not in db_session.dirty
    assert conversation.category_id == cats["support"]
    assert await _stored_category(db_session, conversation.id) == cats["support"]
    assert provider.call_count == 2


async def test_call_parameters(client, db_session, monkeypatch):
    conversation, agent, ids, _cats = await _setup(client, db_session)
    provider = _fake(monkeypatch, "1")

    await _categorize(db_session, conversation, agent, ids)

    call = provider.calls[0]
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 8
    assert call["model"] == "gpt-4o-mini"


@pytest.mark.parametrize("answer", ["0", "7", "abc", "-1", ""])
async def test_no_valid_choice_leaves_it_where_it_was(answer, client, db_session, monkeypatch):
    conversation, agent, ids, cats = await _setup(client, db_session)
    _fake(monkeypatch, "2", answer)
    await _categorize(db_session, conversation, agent, ids)
    assert conversation.category_id == cats["support"]

    with capture_logs() as logs:
        await _categorize(db_session, conversation, agent, ids)

    assert conversation.category_id == cats["support"]
    assert await _stored_category(db_session, conversation.id) == cats["support"]
    assert not any(log["event"] == "category.assigned" for log in logs)


async def test_unparseable_answer_on_uncategorized_stays_uncategorized(
    client, db_session, monkeypatch
):
    conversation, agent, ids, _cats = await _setup(client, db_session)
    _fake(monkeypatch, "billing, I think")
    await _categorize(db_session, conversation, agent, ids)
    assert conversation.category_id is None
    assert await _stored_category(db_session, conversation.id) is None


class SlowProvider:
    def __init__(self, session) -> None:
        self.session = session
        self.in_transaction: list[bool] = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.in_transaction.append(self.session.in_transaction())
        await asyncio.sleep(1)
        return LLMResponse(content="1")


async def test_timeout_leaves_it_and_no_transaction_during_call(client, db_session, monkeypatch):
    conversation, agent, ids, cats = await _setup(client, db_session)
    _fake(monkeypatch, "2")
    await _categorize(db_session, conversation, agent, ids)

    provider = _patch_provider(monkeypatch, SlowProvider(db_session))
    monkeypatch.setattr(
        categorize_module, "get_settings", lambda: Settings(category_timeout_seconds=0.05)
    )
    with capture_logs() as logs:
        await _categorize(db_session, conversation, agent, ids)

    assert provider.in_transaction == [False]
    assert conversation.category_id == cats["support"]
    assert await _stored_category(db_session, conversation.id) == cats["support"]
    assert any(log["event"] == "category.classify_failed" for log in logs)


class FailingProvider:
    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise RuntimeError("provider down")


async def test_provider_error_leaves_it(client, db_session, monkeypatch):
    conversation, agent, ids, cats = await _setup(client, db_session)
    _fake(monkeypatch, "1")
    await _categorize(db_session, conversation, agent, ids)

    _patch_provider(monkeypatch, FailingProvider())
    with capture_logs() as logs:
        await _categorize(db_session, conversation, agent, ids)

    assert conversation.category_id == cats["billing"]
    assert await _stored_category(db_session, conversation.id) == cats["billing"]
    assert any(log["event"] == "category.classify_failed" for log in logs)


async def test_no_categories_makes_no_call(client, db_session, monkeypatch):
    conversation, agent, ids, _cats = await _setup(client, db_session, categories=())
    provider = _fake(monkeypatch)  # any call would fail: nothing is scripted

    await _categorize(db_session, conversation, agent, ids)

    assert provider.call_count == 0
    assert conversation.category_id is None


async def test_locked_conversation_makes_no_call(client, db_session, monkeypatch):
    conversation, agent, ids, cats = await _setup(client, db_session)
    conversation.category_id = cats["billing"]
    conversation.category_locked = True
    await db_session.commit()
    provider = _fake(monkeypatch)

    await _categorize(db_session, conversation, agent, ids)

    assert provider.call_count == 0
    assert await _stored_category(db_session, conversation.id) == cats["billing"]


async def test_lock_taken_after_load_is_not_overwritten(client, db_session, monkeypatch):
    conversation, agent, ids, cats = await _setup(client, db_session)
    await db_session.commit()
    # A human locks the row while the turn holds the loaded (unlocked) object.
    await db_session.execute(
        update(Conversation.__table__)
        .where(Conversation.__table__.c.id == conversation.id)
        .values(category_locked=True)
    )
    await db_session.commit()
    assert conversation.category_locked is False
    provider = _fake(monkeypatch, "1")

    with capture_logs() as logs:
        await _categorize(db_session, conversation, agent, ids)

    assert provider.call_count == 1
    assert await _stored_category(db_session, conversation.id) is None
    assert conversation.category_id is None
    assert not any(log["event"] == "category.assigned" for log in logs)


class DeletingProvider:
    """Answers `1` after deleting that category, as an API call meanwhile would."""

    def __init__(self, session, category_id) -> None:
        self.session = session
        self.category_id = category_id

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        await self.session.execute(
            delete(Category.__table__).where(Category.__table__.c.id == self.category_id)
        )
        await self.session.commit()
        return LLMResponse(content="1")


async def test_category_deleted_during_call_is_swallowed(client, db_session, monkeypatch):
    conversation, agent, ids, cats = await _setup(client, db_session)
    _patch_provider(monkeypatch, DeletingProvider(db_session, cats["billing"]))

    with capture_logs() as logs:
        await _categorize(db_session, conversation, agent, ids)

    assert any(log["event"] == "category.classify_failed" for log in logs)
    assert not any(log["event"] == "category.assigned" for log in logs)
    # The session is still usable and the loaded object still readable.
    assert (await db_session.execute(text("select 1"))).scalar_one() == 1
    assert conversation.category_id is None
    assert conversation.state is not None
    assert await _stored_category(db_session, conversation.id) is None


async def test_prompt_lists_every_category_and_the_batch_once(client, db_session, monkeypatch):
    conversation, agent, ids, _cats = await _setup(
        client,
        db_session,
        categories=(("support", "help with the app"), ("billing", "invoices and payments")),
    )
    provider = _fake(monkeypatch, "0")

    await _categorize(db_session, conversation, agent, ids)

    system, user = provider.calls[0]["messages"]
    assert "1. billing: invoices and payments" in system.content
    assert "2. support: help with the app" in system.content
    assert "0" in system.content
    assert user.content.count(BATCH) == 1
    assert user.content.endswith(f"user: {BATCH}")
