"""The compare command (`app/llm/compare.py`): stored replies replayed through the
boundary check on a candidate model. Providers and the decision model are fakes."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import app.llm.compare as compare_module
from app.llm.base import LLMResponse
from app.llm.compare import CompareError, compare_boundary, parse_target, run
from app.llm.jobs import JobTarget
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.model_job_call import ModelJobCall
from tests.decision_fakes import decision_model  # noqa: F401
from tests.test_model_jobs import BOUNDED, _conversation, _setup, job_env  # noqa: F401

LLM = JobTarget(kind="llm", model="cand-model")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _tenant(client, db_session, name="Acme", **agent):
    headers, channel_id, _agent_id = await _setup(client, db_session, name=name, **agent)
    conversation = await _conversation(client, db_session, headers, channel_id)
    return conversation


async def _add_run(db_session, conversation, age, *, reply="resposta", blocked=None, reason=None,
                   status=AgentRunStatus.SUCCESS):
    """`age` orders runs: the smaller, the newer. `blocked` is the original reply."""
    run = AgentRun(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        status=status,
        input_text=f"pergunta {age}",
        output_text="recusa" if blocked is not None else reply,
        blocked_text=blocked,
        boundary_reason=reason,
        created_at=NOW - timedelta(minutes=age),
    )
    db_session.add(run)
    await db_session.commit()
    return run


def _scripted(monkeypatch, *answers):
    provider = FakeLLMProvider([LLMResponse(content=a) for a in answers])
    monkeypatch.setattr(compare_module, "get_provider", lambda agent, settings=None: provider)
    return provider


async def test_only_disagreements_are_printed(client, db_session, test_engine, monkeypatch):
    conversation = await _tenant(client, db_session, **BOUNDED)
    allowed_agree = await _add_run(db_session, conversation, 1)
    blocked_now_allowed = await _add_run(
        db_session, conversation, 2, blocked="x" * 300, reason="dar conselho médico"
    )
    allowed_now_blocked = await _add_run(db_session, conversation, 3, reply="Tome dipirona.")
    blocked_agree = await _add_run(db_session, conversation, 4, blocked="y", reason="r")
    # Newest first: 1, 2, 3, 4.
    provider = _scripted(monkeypatch, "ALLOW", "ALLOW", "BLOCK: dar conselho médico", "BLOCK: r")

    result = await compare_boundary(test_engine, conversation.tenant_id, LLM)

    assert (result.compared, result.agree, result.disagree, result.skipped) == (4, 2, 2, 0)
    assert provider.calls[0]["model"] == "cand-model"
    first, second = result.lines
    assert str(blocked_now_allowed.id) in first
    assert "recorded: blocked (dar conselho médico)" in first and "candidate: allowed" in first
    assert "x" * 120 in first and "x" * 121 not in first
    assert str(allowed_now_blocked.id) in second
    assert "recorded: allowed" in second
    assert "candidate: blocked (dar conselho médico)" in second and "Tome dipirona." in second
    joined = "\n".join(result.lines)
    assert str(allowed_agree.id) not in joined and str(blocked_agree.id) not in joined


async def test_failure_runs_and_other_statuses_are_not_judged(
    client, db_session, test_engine, monkeypatch
):
    conversation = await _tenant(client, db_session, **BOUNDED)
    await _add_run(db_session, conversation, 1, blocked="a", reason="check_failed")
    await _add_run(db_session, conversation, 2, blocked="b", reason="check_unparseable")
    await _add_run(db_session, conversation, 3, status=AgentRunStatus.ERROR)
    await _add_run(db_session, conversation, 4)
    provider = _scripted(monkeypatch, "ALLOW")

    result = await compare_boundary(test_engine, conversation.tenant_id, LLM)

    assert (result.compared, result.agree, result.disagree, result.skipped) == (1, 1, 0, 2)
    assert provider.call_count == 1


async def test_candidate_failure_is_skipped_not_a_disagreement(
    client, db_session, test_engine, monkeypatch
):
    conversation = await _tenant(client, db_session, **BOUNDED)
    await _add_run(db_session, conversation, 1)
    _scripted(monkeypatch, "talvez")

    result = await compare_boundary(test_engine, conversation.tenant_id, LLM)

    assert (result.compared, result.skipped, result.lines) == (0, 1, [])


async def test_limit_and_agents_without_boundaries(client, db_session, test_engine, monkeypatch):
    bounded = await _tenant(client, db_session, name="A", **BOUNDED)
    plain = await _tenant(client, db_session, name="B")
    for age in (1, 2, 3):
        await _add_run(db_session, bounded, age)
    await _add_run(db_session, plain, 4)
    provider = _scripted(monkeypatch, "ALLOW", "ALLOW")

    result = await compare_boundary(test_engine, bounded.tenant_id, LLM, limit=2)
    assert result.compared == 2 and provider.call_count == 2

    none = await compare_boundary(test_engine, plain.tenant_id, LLM)
    assert none.compared == 0 and none.skipped == 0


async def test_another_tenants_runs_are_never_read(client, db_session, test_engine, monkeypatch):
    mine = await _tenant(client, db_session, name="Mine", **BOUNDED)
    theirs = await _tenant(client, db_session, name="Theirs", **BOUNDED)
    await _add_run(db_session, mine, 1, reply="minha resposta")
    await _add_run(db_session, theirs, 2, reply="resposta alheia")
    provider = _scripted(monkeypatch, "ALLOW")

    result = await compare_boundary(test_engine, mine.tenant_id, LLM)

    assert result.compared == 1 and provider.call_count == 1
    assert "resposta alheia" not in str(provider.calls[0]["messages"])


async def test_nothing_is_written(client, db_session, test_engine, monkeypatch):
    conversation = await _tenant(client, db_session, **BOUNDED)
    run_row = await _add_run(db_session, conversation, 1, blocked="x", reason="r")
    _scripted(monkeypatch, "ALLOW")

    async def counts():
        runs = await db_session.scalar(select(func.count()).select_from(AgentRun))
        calls = await db_session.scalar(select(func.count()).select_from(ModelJobCall))
        return runs, calls

    before = await counts()
    await compare_boundary(test_engine, conversation.tenant_id, LLM)
    await db_session.rollback()
    assert await counts() == before
    await db_session.refresh(run_row)
    assert run_row.boundary_reason == "r" and run_row.blocked_text == "x"


async def test_decision_model_target(client, db_session, test_engine, job_env, decision_model):  # noqa: F811
    job_env(TYPESAFE_API_KEY="test-key-not-used")
    conversation = await _tenant(client, db_session, **BOUNDED)
    await _add_run(db_session, conversation, 1, blocked="x", reason="dar conselho médico")
    await _add_run(db_session, conversation, 2)
    decision_model.answer({"allowed": True, "broken": "none"}, {"allowed": True, "broken": "none"})

    target = parse_target("typesafe:jev-latest")
    result = await compare_boundary(test_engine, conversation.tenant_id, target)

    assert (result.compared, result.agree, result.disagree) == (2, 1, 1)
    assert len(decision_model.calls) == 2


async def test_bad_arguments_and_unknown_tenant_exit_1(test_engine, job_env, capsys):  # noqa: F811
    job_env(TYPESAFE_API_KEY="")
    tenant = str(uuid.uuid4())
    for argv in (
        ["--tenant", tenant, "--model", "gpt-4o"],
        ["--tenant", tenant, "--model", "llm:"],
        ["--tenant", tenant, "--model", "typesafe:jev"],
        ["--tenant", "not-an-id", "--model", "llm:m"],
        ["--tenant", tenant, "--model", "llm:m", "--limit", "0"],
        ["--tenant", tenant, "--model", "llm:m", "--limit", "x"],
        ["--model", "llm:m"],
        ["--tenant", tenant, "--model", "llm:m"],  # unknown tenant
    ):
        assert await run(argv, test_engine) == 1, argv
    assert "unknown tenant" in capsys.readouterr().err
    with pytest.raises(CompareError):
        parse_target("")


async def test_run_prints_header_lines_and_summary(
    client, db_session, test_engine, monkeypatch, capsys
):
    conversation = await _tenant(client, db_session, **BOUNDED)
    await _add_run(db_session, conversation, 1, blocked="x", reason="r")
    await _add_run(db_session, conversation, 2)
    _scripted(monkeypatch, "ALLOW", "ALLOW")

    code = await run(
        ["--tenant", str(conversation.tenant_id), "--model", "llm:cand-model"], test_engine
    )

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "current" in out[0] and "Customer text is printed" in out[0]
    assert out[-1] == "compared 2 replies: 1 agree, 1 disagree, 0 skipped"
    assert len(out) == 3
