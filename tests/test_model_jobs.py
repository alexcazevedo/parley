"""A model per job (`app/llm/jobs.py`): each auxiliary job can run on another chat
model, and every job call leaves a `model_job_calls` row naming its model.

Providers are fakes patched in where each job gets its own (`get_provider` of
the job's module); the boundary check runs inside a real turn.
"""

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

import app.agents.categorize as categorize_module
import app.agents.sentiment as sentiment_module
import app.handoff.summarizer as summarizer_module
import app.llm.jobs as jobs_module
from app.agents.categorize import categorize_conversation
from app.agents.runtime import AgentRuntime
from app.agents.sentiment import score_frustration
from app.config import Settings, get_settings
from app.conversations.summary import maybe_summarize
from app.handoff.summarizer import generate_handoff_summary
from app.llm.base import LLMError, LLMResponse, LLMUsage
from app.llm.jobs import (
    DECISION_JOBS,
    Job,
    JobSettingsError,
    JobTarget,
    job_model,
    job_target,
    record_job_call,
    validate_job_settings,
)
from app.llm.providers.fake import FakeLLMProvider
from app.main import create_app
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.model_job_call import ModelJobCall
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick
from tests import native_fakes

native_model = native_fakes.native_model

AGENT_MODEL = "gpt-4o-mini"
BOUNDED = {
    "boundary_scope": "agendamento de consultas",
    "boundary_never": ["dar conselho médico"],
}
JOB_ENV = {
    Job.BOUNDARY_CHECK: "BOUNDARY_CHECK_MODEL",
    Job.SENTIMENT: "SENTIMENT_MODEL",
    Job.CATEGORY: "CATEGORY_MODEL",
    Job.SUMMARY: "SUMMARY_MODEL",
    Job.HANDOFF_BRIEF: "HANDOFF_BRIEF_MODEL",
}


@pytest.fixture
def job_env(monkeypatch):
    """Set environment settings for the test; `get_settings` re-reads them."""

    names: list[str] = []

    def set_env(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
            names.append(name)
        get_settings.cache_clear()

    yield set_env
    for name in names:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def _usage(content: str) -> LLMResponse:
    return LLMResponse(content=content, usage=LLMUsage(prompt_tokens=11, completion_tokens=3))


async def _setup(client, db_session, name="Acme", **agent_overrides):
    tenant = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "x", "model": AGENT_MODEL, **agent_overrides},
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return headers, channel.json()["id"], uuid.UUID(agent.json()["id"])


async def _conversation(client, db_session, headers, channel_id, text="oi"):
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "u", "message": text},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    return await db_session.get(Conversation, uuid.UUID(sent.json()["conversation_id"]))


async def _direct(client, db_session, *, categories=False, name="Acme"):
    headers, channel_id, agent_id = await _setup(client, db_session, name=name)
    if categories:
        for category in ("billing", "support"):
            response = await client.post(
                "/categories", json={"name": category, "description": "d"}, headers=headers
            )
            assert response.status_code == 201, response.text
    conversation = await _conversation(client, db_session, headers, channel_id)
    agent = await db_session.get(Agent, agent_id)
    return conversation, agent


async def _rows(db_session, conversation_id) -> list[ModelJobCall]:
    result = await db_session.execute(
        select(ModelJobCall)
        .where(ModelJobCall.conversation_id == conversation_id)
        .order_by(ModelJobCall.created_at)
    )
    return list(result.scalars().all())


def _patch(monkeypatch, module, provider):
    monkeypatch.setattr(module, "get_provider", lambda agent, settings=None: provider)
    return provider


async def _run_job(job: Job, client, db_session, monkeypatch, provider):
    """Run `job` once on a fresh conversation; returns the conversation."""
    conversation, agent = await _direct(client, db_session, categories=job == Job.CATEGORY)
    if job == Job.SENTIMENT:
        _patch(monkeypatch, sentiment_module, provider)
        await score_frustration(db_session, conversation, agent, "isso é péssimo")
    elif job == Job.CATEGORY:
        _patch(monkeypatch, categorize_module, provider)
        await categorize_conversation(db_session, conversation, agent, "boleto")
    elif job == Job.SUMMARY:
        agent.summary_threshold = 1
        await maybe_summarize(db_session, agent, conversation, provider)
    elif job == Job.HANDOFF_BRIEF:
        _patch(monkeypatch, summarizer_module, provider)
        await generate_handoff_summary(db_session, conversation, "pediu humano")
    else:  # pragma: no cover
        raise AssertionError(job)
    return conversation


GOOD_ANSWER = {
    Job.SENTIMENT: "0.2",
    Job.CATEGORY: "1",
    Job.SUMMARY: "Cliente disse oi.",
    Job.HANDOFF_BRIEF: "Brief.",
}
BAD_ANSWER = {
    Job.SENTIMENT: "não sei",
    Job.CATEGORY: "7",
    Job.SUMMARY: "   ",
    Job.HANDOFF_BRIEF: "",
}
DIRECT_JOBS = list(GOOD_ANSWER)


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _turn(client, fake_redis, db_session, provider, **agent):
    headers, channel_id, _agent_id = await _setup(client, db_session, **agent)
    conversation = await _conversation(client, db_session, headers, channel_id)
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry())
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)
    return conversation.id


# --- settings -----------------------------------------------------------------


def test_targets_parse():
    settings = Settings(sentiment_model="llm:small", category_model="typesafe:jev")
    assert job_target(Job.BOUNDARY_CHECK, settings) == JobTarget(kind="agent", model=None)
    assert job_target(Job.SENTIMENT, settings) == JobTarget(kind="llm", model="small")
    assert job_target(Job.CATEGORY, settings) == JobTarget(kind="typesafe", model="jev")
    assert DECISION_JOBS == {Job.BOUNDARY_CHECK, Job.SENTIMENT, Job.CATEGORY}
    agent = Agent(model="big")
    assert job_model(Job.SENTIMENT, agent, settings) == "small"
    assert job_model(Job.SUMMARY, agent, settings) == "big"


def test_defaults_are_empty_and_valid():
    names = [f"{job.value}_model" for job in Job] + ["typesafe_api_key"]
    for name in names:
        assert Settings.model_fields[name].default == ""
    validate_job_settings(Settings(**dict.fromkeys(names, "")))


@pytest.mark.parametrize(
    ("values", "named"),
    [
        ({"sentiment_model": "small"}, "SENTIMENT_MODEL"),
        ({"category_model": "llm:"}, "CATEGORY_MODEL"),
        ({"boundary_check_model": "openai:x"}, "BOUNDARY_CHECK_MODEL"),
        ({"boundary_check_model": "typesafe:jev", "typesafe_api_key": ""}, "BOUNDARY_CHECK_MODEL"),
        ({"summary_model": "typesafe:jev", "typesafe_api_key": "k"}, "SUMMARY_MODEL"),
        ({"handoff_brief_model": "typesafe:jev", "typesafe_api_key": "k"}, "HANDOFF_BRIEF_MODEL"),
    ],
)
def test_bad_setting_stops_startup(values, named):
    settings = Settings(demo_mode=True, **values)
    with pytest.raises(JobSettingsError, match=named):
        create_app(settings)


def test_typesafe_with_key_on_a_decision_job_starts():
    validate_job_settings(Settings(sentiment_model="typesafe:jev", typesafe_api_key="k"))


def test_worker_validates_job_settings(job_env):
    import app.workers.main as worker_main

    job_env(SUMMARY_MODEL="typesafe:jev", TYPESAFE_API_KEY="k", DEMO_MODE="true")
    with pytest.raises(JobSettingsError, match="SUMMARY_MODEL"):
        asyncio.run(worker_main.main())


# --- the direct jobs ------------------------------------------------------------


@pytest.mark.parametrize("job", DIRECT_JOBS)
async def test_no_setting_uses_agent_model_and_records_it(job, client, db_session, monkeypatch):
    provider = FakeLLMProvider([_usage(GOOD_ANSWER[job])])
    conversation = await _run_job(job, client, db_session, monkeypatch, provider)

    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    [row] = await _rows(db_session, conversation.id)
    assert row.job == job.value
    assert row.model == AGENT_MODEL
    assert row.outcome == "ok"
    assert row.tenant_id == conversation.tenant_id
    assert row.agent_run_id is None
    assert row.handed_over is False
    assert (row.prompt_tokens, row.completion_tokens) == (11, 3)
    assert row.latency_ms is not None and row.latency_ms >= 0


def _llm_chat_model(monkeypatch, script):
    """`llm:` decision jobs are typed (`app.llm.decisions`): their chat model is a script."""
    import app.llm.decisions as decisions_module

    monkeypatch.setattr(
        decisions_module,
        "build_chat_model",
        lambda target, settings=None: script.build("llm", target.model),
    )


# The writing jobs on `llm:` keep the text form; the decision jobs on `llm:` get
# a typed answer (updated from the text form when `llm:` decisions became typed).
@pytest.mark.parametrize("job", [Job.SUMMARY, Job.HANDOFF_BRIEF])
async def test_llm_setting_calls_that_model(job, client, db_session, monkeypatch, job_env):
    job_env(**{JOB_ENV[job]: "llm:small"})
    provider = FakeLLMProvider([_usage(GOOD_ANSWER[job])])
    conversation = await _run_job(job, client, db_session, monkeypatch, provider)

    assert [call["model"] for call in provider.calls] == ["small"]
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.outcome) == (job.value, "small", "ok")


@pytest.mark.parametrize(
    ("job", "answer"), [(Job.SENTIMENT, {"level": 1}), (Job.CATEGORY, {"department": "none"})]
)
async def test_llm_setting_on_a_decision_job_calls_that_model_typed(
    job, answer, client, db_session, monkeypatch, job_env, native_model  # noqa: F811
):
    job_env(**{JOB_ENV[job]: "llm:small"})
    _llm_chat_model(monkeypatch, native_model.call("final_result", answer))
    provider = FakeLLMProvider([])
    conversation = await _run_job(job, client, db_session, monkeypatch, provider)

    assert provider.calls == []
    assert [request.model for request in native_model.requests] == ["small"]
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.outcome) == (job.value, "llm:small", "ok")


@pytest.mark.parametrize("job", DIRECT_JOBS)
async def test_unusable_answer_is_recorded(job, client, db_session, monkeypatch):
    provider = FakeLLMProvider([_usage(BAD_ANSWER[job])])
    conversation = await _run_job(job, client, db_session, monkeypatch, provider)

    [row] = await _rows(db_session, conversation.id)
    assert row.outcome == "unusable"
    assert (row.prompt_tokens, row.completion_tokens) == (11, 3)


class Failing:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise self.exc


class Slow:
    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        await asyncio.sleep(5)
        return LLMResponse(content="0.1")


@pytest.mark.parametrize("job", DIRECT_JOBS)
async def test_error_is_recorded(job, client, db_session, monkeypatch):
    conversation = await _run_job(
        job, client, db_session, monkeypatch, Failing(LLMError("502 upstream"))
    )

    [row] = await _rows(db_session, conversation.id)
    assert (row.outcome, row.model, row.prompt_tokens) == ("error", AGENT_MODEL, None)
    assert row.latency_ms is not None


@pytest.mark.parametrize("job", [Job.SENTIMENT, Job.CATEGORY, Job.HANDOFF_BRIEF])
async def test_timeout_is_recorded(job, client, db_session, monkeypatch, job_env):
    job_env(SENTIMENT_TIMEOUT_SECONDS="0.05", CATEGORY_TIMEOUT_SECONDS="0.05")
    monkeypatch.setattr(summarizer_module, "SUMMARY_TIMEOUT_SECONDS", 0.05)
    conversation = await _run_job(job, client, db_session, monkeypatch, Slow())

    [row] = await _rows(db_session, conversation.id)
    assert row.outcome == "timeout"


async def test_recording_failure_does_not_fail_the_job(client, db_session, monkeypatch):
    def broken(source):
        raise RuntimeError("database gone")

    monkeypatch.setattr(jobs_module, "_session_maker", broken)
    provider = FakeLLMProvider([_usage("0.9")])
    conversation, agent = await _direct(client, db_session)
    _patch(monkeypatch, sentiment_module, provider)

    with capture_logs() as logs:
        score = await score_frustration(db_session, conversation, agent, "péssimo")

    assert score == 0.9
    assert any(log["event"] == "model_job.record_failed" for log in logs)
    monkeypatch.undo()
    assert await _rows(db_session, conversation.id) == []


async def test_rows_are_tenant_scoped(client, db_session, monkeypatch):
    first, agent_a = await _direct(client, db_session, name="A")
    second, _agent_b = await _direct(client, db_session, name="B")
    _patch(monkeypatch, sentiment_module, FakeLLMProvider([_usage("0.1")]))
    await score_frustration(db_session, first, agent_a, "oi")

    rows = (
        await db_session.execute(
            select(ModelJobCall).where(ModelJobCall.tenant_id == second.tenant_id)
        )
    ).scalars().all()
    assert rows == []
    [row] = await _rows(db_session, first.id)
    assert row.tenant_id == first.tenant_id != second.tenant_id


async def test_record_job_call_with_a_session_factory(client, db_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    conversation, _agent = await _direct(client, db_session)
    await db_session.commit()
    maker = async_sessionmaker(bind=db_session.bind, expire_on_commit=False)
    await record_job_call(
        maker,
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        agent_run_id=None,
        job=Job.SUMMARY,
        model="m",
        outcome="ok",
    )
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.latency_ms) == ("summary", "m", None)


# --- the boundary check, inside a turn -----------------------------------------


async def _check_rows(db_session, conversation_id):
    return [row for row in await _rows(db_session, conversation_id) if row.job == "boundary_check"]


async def test_boundary_check_no_setting_uses_agent_model(client, fake_redis, db_session):
    provider = FakeLLMProvider([LLMResponse(content="Temos horário."), _usage("ALLOW")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    assert [call["model"] for call in provider.calls] == [AGENT_MODEL, AGENT_MODEL]
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome, row.prompt_tokens) == (AGENT_MODEL, "ok", 11)
    assert row.agent_run_id is not None


async def test_boundary_check_llm_setting_keeps_reply_on_agent_model(
    client, fake_redis, db_session, job_env, monkeypatch, native_model  # noqa: F811
):
    # Typed since `llm:` decisions went through `app.llm.decisions` (was text).
    job_env(BOUNDARY_CHECK_MODEL="llm:small")
    native_model.call("final_result", {"allowed": True, "broken": "none"})
    _llm_chat_model(monkeypatch, native_model)
    provider = FakeLLMProvider([LLMResponse(content="Temos horário.")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    assert [request.model for request in native_model.requests] == ["small"]
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome) == ("llm:small", "ok")


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [(LLMResponse(content="talvez"), "unusable"), (None, "error")],
)
async def test_boundary_check_outcomes(client, fake_redis, db_session, answer, outcome):
    class Provider(FakeLLMProvider):
        async def complete(self, messages, tools=None, **kw):
            if not self.scripted:
                raise LLMError("down")
            return await super().complete(messages, tools, **kw)

    scripted = [LLMResponse(content="Temos horário.")] + ([answer] if answer else [])
    conversation_id = await _turn(client, fake_redis, db_session, Provider(scripted), **BOUNDED)

    [row] = await _check_rows(db_session, conversation_id)
    assert row.outcome == outcome


async def test_boundary_check_timeout(client, fake_redis, db_session, job_env):
    job_env(BOUNDARY_CHECK_TIMEOUT_SECONDS="0.05")

    class Provider(FakeLLMProvider):
        async def complete(self, messages, tools=None, **kw):
            if not self.scripted:
                await asyncio.sleep(5)
            return await super().complete(messages, tools, **kw)

    provider = Provider([LLMResponse(content="Temos horário.")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    [row] = await _check_rows(db_session, conversation_id)
    assert row.outcome == "timeout"
