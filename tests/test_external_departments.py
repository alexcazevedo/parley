"""`conversation.category` events on an external channel: the agent's choice and
an operator's move are recorded; a move locks the category against the agent.

The classifier's model is scripted with `FakeLLMProvider` patched in as
`app.agents.categorize.get_provider`; the answer is the category's number in
the tenant's list ("1", "2"; "0" keeps the category).
"""

import uuid

from sqlalchemy import select

import app.agents.categorize as categorize_module
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.channel_event import ChannelEvent
from app.models.conversation import Conversation
from tests.test_external_channel import _post, _run_turn, _setup
from tests.test_external_staff import _types

CATEGORY = "conversation.category"


async def _categories(client, headers) -> dict[str, str]:
    ids = {}
    for name, description in (("billing", "invoices"), ("support", "product help")):
        response = await client.post(
            "/categories", json={"name": name, "description": description}, headers=headers
        )
        assert response.status_code == 201, response.text
        ids[name] = response.json()["id"]
    return ids


def _script(monkeypatch, *answers: str) -> FakeLLMProvider:
    provider = FakeLLMProvider([LLMResponse(content=a) for a in answers])
    monkeypatch.setattr(categorize_module, "get_provider", lambda agent, settings=None: provider)
    return provider


async def _stored(db_session, conversation_id):
    db_session.expire_all()
    return (await db_session.get(Conversation, conversation_id)).category_id


async def _category_events(client, headers, channel_id):
    return [d for t, d in await _types(client, headers, channel_id) if t == CATEGORY]


async def test_agent_choice_records_one_event(client, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    ids = await _categories(client, headers)
    _script(monkeypatch, "2")
    response = await _post(client, headers, channel_id, "o app não abre")
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    await _run_turn(db_session, conversation_id)

    assert await _category_events(client, headers, channel_id) == [
        {"category_id": ids["support"], "name": "support", "source": "agent"}
    ]


async def test_unchanged_choice_records_nothing_more(client, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    await _categories(client, headers)
    _script(monkeypatch, "2", "2")
    response = await _post(client, headers, channel_id, "o app não abre")
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    await _run_turn(db_session, conversation_id)
    await _run_turn(db_session, conversation_id)
    assert len(await _category_events(client, headers, channel_id)) == 1


async def test_operator_move_records_event_and_locks(client, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    ids = await _categories(client, headers)
    provider = _script(monkeypatch, "2", "2")
    response = await _post(client, headers, channel_id, "o app não abre")
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    await _run_turn(db_session, conversation_id)

    move = await client.put(
        f"/conversations/{conversation_id}/category",
        json={"category_id": ids["billing"]},
        headers=headers,
    )
    assert move.status_code == 200, move.text
    # Same value again: no event.
    again = await client.put(
        f"/conversations/{conversation_id}/category",
        json={"category_id": ids["billing"]},
        headers=headers,
    )
    assert again.status_code == 200
    events = await _category_events(client, headers, channel_id)
    assert events[1:] == [{"category_id": ids["billing"], "name": "billing", "source": "operator"}]
    calls = provider.call_count

    # A new turn: the agent does not choose again.
    await _post(client, headers, channel_id, "e a fatura?")
    await _run_turn(db_session, conversation_id)
    assert provider.call_count == calls
    assert await _category_events(client, headers, channel_id) == events
    assert str(await _stored(db_session, conversation_id)) == ids["billing"]


async def test_operator_clears_category(client, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    await _categories(client, headers)
    _script(monkeypatch, "1")
    response = await _post(client, headers, channel_id, "boleto")
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    await _run_turn(db_session, conversation_id)
    move = await client.put(
        f"/conversations/{conversation_id}/category", json={"category_id": None}, headers=headers
    )
    assert move.status_code == 200
    events = await _category_events(client, headers, channel_id)
    assert events[-1] == {"category_id": None, "name": None, "source": "operator"}
    assert await _stored(db_session, conversation_id) is None


async def test_webchat_conversation_records_none(client, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client, channel_type="webchat")
    ids = await _categories(client, headers)
    _script(monkeypatch, "1")
    response = await _post(client, headers, channel_id, "boleto", path="webchat")
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    await _run_turn(db_session, conversation_id)
    assert str(await _stored(db_session, conversation_id)) == ids["billing"]
    move = await client.put(
        f"/conversations/{conversation_id}/category",
        json={"category_id": ids["support"]},
        headers=headers,
    )
    assert move.status_code == 200
    rows = await db_session.execute(
        select(ChannelEvent).where(ChannelEvent.conversation_id == conversation_id)
    )
    assert rows.scalars().all() == []
