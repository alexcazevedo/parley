"""PRODUCT.md's decision-model success demo as a test: one tenant with departments
and an agent with a scope, `never` items and sentiment escalation, whose boundary
check, sentiment and department run on a scripted decision model (never the
service). The chat model is scripted too."""

import time

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from sqlalchemy import select

import app.agents.categorize as categorize_module
import app.agents.sentiment as sentiment_module
from app.agents.boundaries import DEFAULT_REFUSAL_MESSAGE
from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse
from app.llm.compare import compare_boundary, parse_target
from app.llm.decisions import category_label
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun
from app.models.category import Category
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick
from tests.decision_fakes import Delay, decision_model  # noqa: F401
from tests.test_model_compare import _add_run, _tenant
from tests.test_model_jobs import (
    AGENT_MODEL,
    BOUNDED,
    _conversation,
    _patch,
    _rows,
    _session_factory,
    _setup,
    _usage,
    job_env,  # noqa: F401
)

JEV = "typesafe:jev-latest"
REPLY = "Tome dipirona de 8 em 8 horas."
ALLOWED_REPLY = "Temos horário às 10h."
NEVER_ITEM = BOUNDED["boundary_never"][0]


@pytest.fixture
def jev(job_env):  # noqa: F811
    job_env(
        BOUNDARY_CHECK_MODEL=JEV,
        SENTIMENT_MODEL=JEV,
        CATEGORY_MODEL=JEV,
        TYPESAFE_API_KEY="test-key-not-used",
    )


async def _customer_turn(client, fake_redis, db_session, provider, script=None, *, name, **agent):
    """One customer message through the queue and the scheduler; returns
    (conversation id, the decision model's department label for 'support').

    `script(support_label)` returns the steps the decision model answers, in the
    order the turn asks: department, sentiment, boundary check."""
    headers, channel_id, _agent_id = await _setup(
        client, db_session, name=name, sentiment_escalation_enabled=True, **agent
    )
    for department in ("billing", "support"):
        created = await client.post(
            "/categories", json={"name": department, "description": "d"}, headers=headers
        )
        assert created.status_code == 201, created.text
    conversation = await _conversation(client, db_session, headers, channel_id)
    support = (
        await db_session.execute(
            select(Category).where(
                Category.tenant_id == conversation.tenant_id, Category.name == "support"
            )
        )
    ).scalar_one()
    if script is not None:
        script(category_label(support.id))
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry())
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)
    return conversation.id


async def _run(db_session, conversation_id) -> AgentRun:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    return result.scalars().one()


async def _calls(db_session, conversation_id):
    """`{job: (model, outcome, handed_over)}` of the conversation's recorded calls."""
    rows = await _rows(db_session, conversation_id)
    assert len({row.job for row in rows}) == len(rows)
    return {row.job: (row.model, row.outcome, row.handed_over) for row in rows}


def _all_jobs(decision_model, steps_after=()):  # noqa: F811
    def script(support):
        decision_model.answer({"department": support}, {"level": 0}, *steps_after)

    return script


async def test_message_is_answered_and_each_job_is_recorded(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    provider = FakeLLMProvider([LLMResponse(content=ALLOWED_REPLY)])
    script = _all_jobs(decision_model, [{"allowed": True, "broken": "none"}])
    conversation_id = await _customer_turn(
        client, fake_redis, db_session, provider, script, name="Acme", **BOUNDED
    )

    run = await _run(db_session, conversation_id)
    assert run.output_text == ALLOWED_REPLY and run.boundary_reason is None
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    assert await _calls(db_session, conversation_id) == {
        "boundary_check": (JEV, "ok", False),
        "sentiment": (JEV, "ok", False),
        "category": (JEV, "ok", False),
    }


async def test_reply_breaking_a_never_item_is_refused(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    script = _all_jobs(decision_model, [{"allowed": False, "broken": "never_1"}])
    conversation_id = await _customer_turn(
        client, fake_redis, db_session, provider, script, name="Acme", **BOUNDED
    )

    run = await _run(db_session, conversation_id)
    assert run.boundary_reason == NEVER_ITEM
    assert run.blocked_text == REPLY
    assert run.output_text == DEFAULT_REFUSAL_MESSAGE
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]


@pytest.mark.parametrize(
    ("step", "outcome"),
    [
        (ModelHTTPError(status_code=502, model_name="jev-latest", body="down"), "error"),
        (Delay(5), "timeout"),
    ],
)
async def test_decision_model_error_or_timeout_blocks_without_another_model(
    client, fake_redis, db_session, jev, job_env, decision_model, step, outcome  # noqa: F811
):
    job_env(BOUNDARY_CHECK_TIMEOUT_SECONDS="0.1")
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    script = _all_jobs(decision_model, [step])
    conversation_id = await _customer_turn(
        client, fake_redis, db_session, provider, script, name="Acme", **BOUNDED
    )

    run = await _run(db_session, conversation_id)
    assert run.boundary_reason == "check_failed"
    assert run.output_text == DEFAULT_REFUSAL_MESSAGE
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    assert (await _calls(db_session, conversation_id))["boundary_check"] == (JEV, outcome, False)


async def test_more_never_items_than_the_limit_go_to_the_agent_model(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    # none, scope, role, never_1, never_2: five options over a limit of four.
    decision_model.max_choice_options = 4
    bounded = {**BOUNDED, "boundary_never": [NEVER_ITEM, "falar de política"]}
    provider = FakeLLMProvider([LLMResponse(content=ALLOWED_REPLY), _usage("ALLOW")])
    conversation_id = await _customer_turn(
        client, fake_redis, db_session, provider, _all_jobs(decision_model),
        name="Acme", **bounded,
    )

    assert len(decision_model.calls) == 2  # department and sentiment, not the check
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL, AGENT_MODEL]
    calls = await _calls(db_session, conversation_id)
    assert calls["boundary_check"] == (AGENT_MODEL, "ok", True)
    assert calls["sentiment"] == (JEV, "ok", False)


async def test_compare_prints_the_one_reply_the_candidate_disagrees_on(
    client, db_session, test_engine, jev, decision_model  # noqa: F811
):
    conversation = await _tenant(client, db_session, **BOUNDED)
    await _add_run(db_session, conversation, 1, reply=ALLOWED_REPLY)
    blocked = await _add_run(db_session, conversation, 2, blocked=REPLY, reason=NEVER_ITEM)
    decision_model.answer(
        {"allowed": True, "broken": "none"}, {"allowed": True, "broken": "none"}
    )

    result = await compare_boundary(
        test_engine, conversation.tenant_id, parse_target(JEV)
    )

    assert (result.compared, result.agree, result.disagree) == (2, 1, 1)
    [line] = result.lines
    assert str(blocked.id) in line and "candidate: allowed" in line
    assert f"recorded: blocked ({NEVER_ITEM})" in line


async def test_with_the_settings_unset_the_agent_model_serves_every_job(
    client, fake_redis, db_session, monkeypatch, job_env, decision_model  # noqa: F811
):
    job_env(BOUNDARY_CHECK_MODEL="", SENTIMENT_MODEL="", CATEGORY_MODEL="", TYPESAFE_API_KEY="")
    # Department and sentiment get their own provider in each module.
    categories = _patch(monkeypatch, categorize_module, FakeLLMProvider([_usage("0")]))
    sentiment = _patch(monkeypatch, sentiment_module, FakeLLMProvider([_usage("0.1")]))
    provider = FakeLLMProvider([LLMResponse(content=ALLOWED_REPLY), _usage("ALLOW")])
    conversation_id = await _customer_turn(
        client, fake_redis, db_session, provider, name="Acme", **BOUNDED
    )

    assert decision_model.targets == [] and decision_model.calls == []
    for used in (categories, sentiment, provider):
        assert {call["model"] for call in used.calls} == {AGENT_MODEL}
    assert await _calls(db_session, conversation_id) == {
        "boundary_check": (AGENT_MODEL, "ok", False),
        "sentiment": (AGENT_MODEL, "ok", False),
        "category": (AGENT_MODEL, "ok", False),
    }
    assert (await _run(db_session, conversation_id)).output_text == ALLOWED_REPLY
