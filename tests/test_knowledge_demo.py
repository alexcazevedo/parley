"""PRODUCT.md's success demo as a test: an entry, a pair and a PDF; the PDF
becomes ready; two agents, one with `knowledge_enabled` on and one off, get the
same question on their own web chat channel. Only the model is scripted."""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.knowledge.ingest import process_due
from app.llm.base import ROLE_TOOL, LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.knowledge_passage import KnowledgePassage
from tests.test_external_media import _pdf
from tests.test_knowledge_tool import (
    _entry,
    _executions,
    _replies,
    _search_step,
    _tenant,
    _turn,
    factory,  # noqa: F401 - fixture
)

FACT = "The quokka warranty lasts ninety-seven days."
QUESTION = "How long is the quokka warranty?"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_success_demo(client, fake_redis, db_session, factory, media_root):  # noqa: F811
    headers = await _tenant(client)
    await _entry(client, headers, "Opening hours", "We open at nine and close at six.")
    qa = await client.post(
        "/knowledge/qa",
        json={"question": "Do you ship abroad?", "answer": "Yes, to every country in Europe."},
        headers=headers,
    )
    assert qa.status_code == 201, qa.text
    pdf = await client.post(
        "/knowledge/documents",
        files={"file": ("warranty.pdf", _pdf(FACT), "application/pdf")},
        data={"title": "Warranty"},
        headers=headers,
    )
    assert pdf.status_code == 201, pdf.text
    assert pdf.json()["status"] == "processing"

    await process_due(factory)
    got = await client.get(f"/knowledge/{pdf.json()['id']}", headers=headers)
    assert got.json()["status"] == "ready"
    pdf_passages = {
        str(i)
        for i in (
            await db_session.scalars(
                select(KnowledgePassage.id).where(
                    KnowledgePassage.item_id == pdf.json()["id"]
                )
            )
        ).all()
    }
    assert pdf_passages

    # Switch on: the model searches, the reply uses the passage.
    on = FakeLLMProvider([_search_step(QUESTION), LLMResponse(content=FACT)])
    conv = await _turn(
        client, fake_redis, db_session, on, headers, text=QUESTION,
        knowledge_enabled=True, boundary_check_enabled=False,
    )
    tool_message = on.calls[1]["messages"][-1]
    assert tool_message.role == ROLE_TOOL and FACT in tool_message.content
    assert FACT in (await _replies(db_session, conv))[0]
    [execution] = await _executions(db_session, conv)
    assert execution.tool_name == "search_knowledge"
    assert {p["id"] for p in execution.result["passages"]} & pdf_passages

    # Switch off: no tool offered, no execution, no fact.
    off = FakeLLMProvider([LLMResponse(content="I do not know.")])
    conv = await _turn(
        client, fake_redis, db_session, off, headers, text=QUESTION,
        knowledge_enabled=False, boundary_check_enabled=False,
    )
    assert "search_knowledge" not in [spec.name for spec in off.calls[0]["tools"]]
    assert await _executions(db_session, conv) == []
    assert FACT not in (await _replies(db_session, conv))[0]
