"""The decision jobs on a decision model (`app/llm/decisions.py`).

With a job set to `typesafe:jev-latest`, the boundary check, sentiment and
department run on the scripted decision model of `tests/decision_fakes.py`
(never the service). What it cannot take is handed over to the agent's own
model through today's text path; an error or a timeout is not.
"""

import json
import uuid

import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.decision import NoulAnswer, NoulQuestion
from sqlalchemy import select

import app.agents.categorize as categorize_module
import app.agents.sentiment as sentiment_module
from app.agents.boundaries import (
    DEFAULT_REFUSAL_MESSAGE,
    REASON_NO_RULE_IDENTIFIED,
    Boundaries,
    Verdict,
    check_payload,
    check_reply,
)
from app.agents.categorize import categorize_conversation
from app.agents.sentiment import score_frustration
from app.config import Settings, get_settings
from app.llm.base import LLMResponse
from app.llm.decisions import SENTIMENT_LEVELS, category_label, decide_boundary
from app.llm.jobs import Job, job_target
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun
from app.models.category import Category
from app.models.message import Message, MessageDirection, MessageRole
from tests import decision_fakes
from tests.decision_fakes import (
    COMPLETION_TOKENS,
    PROMPT_TOKENS,
    Delay,
    decision_model,  # noqa: F401
    hand_off,
)
from tests.decision_fakes import _answer as _scripted_answer
from tests.test_model_jobs import (
    AGENT_MODEL,
    BOUNDED,
    _check_rows,
    _direct,
    _patch,
    _rows,
    _turn,
    _usage,
    job_env,  # noqa: F401
)

JEV = "typesafe:jev-latest"
REPLY = "Tome dipirona de 8 em 8 horas."
NEVER_ITEM = BOUNDED["boundary_never"][0]


async def _run(db_session, conversation_id) -> AgentRun:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    return result.scalars().one()


@pytest.fixture
def jev(job_env):  # noqa: F811
    job_env(
        BOUNDARY_CHECK_MODEL=JEV,
        SENTIMENT_MODEL=JEV,
        CATEGORY_MODEL=JEV,
        TYPESAFE_API_KEY="test-key-not-used",
    )


# --- boundary check ---------------------------------------------------------------


async def test_broken_never_item_is_blocked_with_its_text(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    decision_model.answer({"allowed": False, "broken": "never_1"})
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run = await _run(db_session, conversation_id)
    assert run.boundary_reason == NEVER_ITEM
    assert run.blocked_text == REPLY
    assert run.output_text == DEFAULT_REFUSAL_MESSAGE
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome, row.handed_over) == (JEV, "ok", False)
    assert (row.prompt_tokens, row.completion_tokens) == (PROMPT_TOKENS, COMPLETION_TOKENS)
    assert row.agent_run_id == run.id
    assert [target.model for target in decision_model.targets] == ["jev-latest"]


async def test_allowed_reply_goes_out(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    decision_model.answer({"allowed": True, "broken": "none"})
    provider = FakeLLMProvider([LLMResponse(content="Temos horário às 10h.")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run = await _run(db_session, conversation_id)
    assert run.output_text == "Temos horário às 10h."
    assert run.boundary_reason is None
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome) == (JEV, "ok")


async def test_blocked_without_a_rule_records_no_rule_identified(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    decision_model.answer({"allowed": False, "broken": "none"})
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run = await _run(db_session, conversation_id)
    assert run.boundary_reason == REASON_NO_RULE_IDENTIFIED == "no_rule_identified"
    assert run.output_text == DEFAULT_REFUSAL_MESSAGE
    [row] = await _check_rows(db_session, conversation_id)
    assert row.outcome == "ok"


async def test_allowed_with_a_rule_picked_goes_out_without_a_reason(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    decision_model.answer({"allowed": True, "broken": "scope"})
    provider = FakeLLMProvider([LLMResponse(content="Temos horário às 10h.")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run = await _run(db_session, conversation_id)
    assert run.output_text == "Temos horário às 10h."
    assert run.boundary_reason is None and run.blocked_text is None
    [row] = await _check_rows(db_session, conversation_id)
    assert row.outcome == "ok"


def _answer_with_probability(question, value):
    """The fake's answers, plus a float for a yes/no: that probability of yes."""
    if isinstance(question, NoulQuestion) and isinstance(value, float):
        return NoulAnswer(noul=value)
    return _scripted_answer(question, value)


@pytest.mark.parametrize(("threshold", "allowed"), [("0.9", False), ("0.5", True)])
async def test_allow_threshold_is_the_library_boolean_threshold(
    monkeypatch, jev, job_env, decision_model, threshold, allowed  # noqa: F811
):
    monkeypatch.setattr(decision_fakes, "_answer", _answer_with_probability)
    job_env(BOUNDARY_CHECK_ALLOW_THRESHOLD=threshold)
    decision_model.answer({"allowed": 0.84, "broken": "scope"})
    boundaries = Boundaries(
        scopes=("agendamento de consultas",), never=(NEVER_ITEM,),
        refusal_message="x", check_enabled=True,
    )

    verdict = await decide_boundary(
        job_target(Job.BOUNDARY_CHECK, get_settings()), boundaries=boundaries,
        evidence=check_payload("oi", "Amanhã chove."),
    )

    assert verdict.allowed is allowed
    assert verdict.reason == (None if allowed else "scope")


@pytest.mark.parametrize("value", ["-0.1", "1.5"])
def test_allow_threshold_outside_0_to_1_stops_startup(monkeypatch, value):
    monkeypatch.setenv("BOUNDARY_CHECK_ALLOW_THRESHOLD", value)
    with pytest.raises(ValidationError, match="boundary_check_allow_threshold"):
        Settings()


async def test_text_path_ignores_the_allow_threshold(job_env):  # noqa: F811
    job_env(BOUNDARY_CHECK_ALLOW_THRESHOLD="1.0")
    boundaries = Boundaries(scopes=("x",), never=(), refusal_message="r", check_enabled=True)
    for answer, expected in [
        ("ALLOW", Verdict(allowed=True, reason=None)),
        ("BLOCK:", Verdict(allowed=False, reason="check_unparseable")),
        ("maybe", Verdict(allowed=False, reason="check_unparseable")),
    ]:
        provider = FakeLLMProvider([LLMResponse(content=answer)])
        verdict = await check_reply(
            provider, model=AGENT_MODEL, boundaries=boundaries, user_text="oi", reply_text="r"
        )
        assert verdict == expected


@pytest.mark.parametrize(
    ("step", "outcome"),
    [
        (ModelHTTPError(status_code=502, model_name="jev-latest", body="down"), "error"),
        (Delay(5), "timeout"),
    ],
)
async def test_error_and_timeout_block_and_call_no_other_model(
    client, fake_redis, db_session, jev, job_env, decision_model, step, outcome  # noqa: F811
):
    job_env(BOUNDARY_CHECK_TIMEOUT_SECONDS="0.1")
    decision_model.answer(step)
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run = await _run(db_session, conversation_id)
    assert run.boundary_reason == "check_failed"
    assert run.output_text == DEFAULT_REFUSAL_MESSAGE
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome, row.handed_over) == (JEV, outcome, False)


async def test_more_never_items_than_the_limit_go_to_the_agent_model(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    # none, scope, role, never_1, never_2: five options over a limit of four.
    decision_model.max_choice_options = 4
    bounded = {**BOUNDED, "boundary_never": ["dar conselho médico", "falar de política"]}
    provider = FakeLLMProvider([LLMResponse(content=REPLY), _usage("BLOCK: conselho médico")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **bounded)

    assert decision_model.calls == []
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL, AGENT_MODEL]
    run = await _run(db_session, conversation_id)
    assert run.boundary_reason == "conselho médico"
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome, row.handed_over) == (AGENT_MODEL, "ok", True)
    assert row.prompt_tokens == 11


async def test_hand_off_is_answered_by_the_agent_model(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    decision_model.answer(hand_off())
    provider = FakeLLMProvider([LLMResponse(content="Temos horário."), _usage("ALLOW")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    assert len(decision_model.calls) == 1
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL, AGENT_MODEL]
    run = await _run(db_session, conversation_id)
    assert run.output_text == "Temos horário."
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome, row.handed_over) == (AGENT_MODEL, "ok", True)


async def test_questions_carry_the_user_text_only_as_evidence(
    client, fake_redis, db_session, jev, decision_model  # noqa: F811
):
    decision_model.answer({"allowed": True, "broken": "none"})
    provider = FakeLLMProvider([LLMResponse(content="Resposta zq-reply-77.")])
    # `_turn` sends "oi"; the reply is the other half of the evidence.
    await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    [request] = decision_model.calls
    assert json.loads(request.state) == {"user_message": "oi", "reply": "Resposta zq-reply-77."}
    questions = json.dumps({name: repr(q) for name, q in request.questions.items()})
    assert "zq-reply-77" not in questions
    broken = request.questions["broken"]
    assert list(broken.criteria) == ["none", "scope", "role", "never_1"]
    assert NEVER_ITEM in broken.criteria["never_1"]
    # `allowed` is answered on its own, so it carries the rules too.
    allowed = repr(request.questions["allowed"])
    assert NEVER_ITEM in allowed and BOUNDED["boundary_scope"] in allowed


# --- sentiment ----------------------------------------------------------------------


@pytest.mark.parametrize("level", range(5))
async def test_sentiment_score_keeps_the_threshold(
    level, client, db_session, monkeypatch, jev, decision_model  # noqa: F811
):
    conversation, agent = await _direct(client, db_session)
    provider = _patch(monkeypatch, sentiment_module, FakeLLMProvider([]))
    decision_model.answer({"level": level})

    score = await score_frustration(db_session, conversation, agent, "isso zq-77 é péssimo")

    assert score == SENTIMENT_LEVELS[level] == level / 4
    # Explicit complaint (3) and fury (4) escalate, as 0.7-1.0 did on the text scale.
    assert (score >= get_settings().sentiment_threshold) is (level >= 3)
    assert provider.calls == []
    [request] = decision_model.calls
    assert "user: isso zq-77 é péssimo" in request.state
    assert "zq-77" not in repr(request.questions)
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.outcome, row.handed_over) == ("sentiment", JEV, "ok", False)
    assert row.prompt_tokens == PROMPT_TOKENS


async def test_sentiment_error_returns_none_without_hand_over(
    client, db_session, monkeypatch, jev, decision_model  # noqa: F811
):
    conversation, agent = await _direct(client, db_session)
    provider = _patch(monkeypatch, sentiment_module, FakeLLMProvider([]))
    decision_model.answer(RuntimeError("boom"))

    assert await score_frustration(db_session, conversation, agent, "péssimo") is None
    assert provider.calls == []
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.outcome, row.handed_over) == (JEV, "error", False)


async def test_sentiment_image_in_evidence_goes_to_the_agent_model(
    client, db_session, monkeypatch, jev, decision_model  # noqa: F811
):
    conversation, agent = await _direct(client, db_session)
    db_session.add(
        Message(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            channel_id=conversation.channel_id,
            direction=MessageDirection.IN,
            role=MessageRole.USER,
            type="image",
            content="foto do boleto",
            media_mime="image/jpeg",
        )
    )
    await db_session.commit()
    provider = _patch(monkeypatch, sentiment_module, FakeLLMProvider([_usage("0.9")]))

    assert await score_frustration(db_session, conversation, agent, "olha isso") == 0.9
    assert decision_model.calls == []
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.handed_over) == (AGENT_MODEL, True)


# --- department ---------------------------------------------------------------------


async def _categories(db_session, tenant_id) -> dict[str, Category]:
    result = await db_session.execute(select(Category).where(Category.tenant_id == tenant_id))
    return {category.name: category for category in result.scalars().all()}


async def test_department_is_set_from_the_pick(
    client, db_session, monkeypatch, jev, decision_model  # noqa: F811
):
    conversation, agent = await _direct(client, db_session, categories=True)
    categories = await _categories(db_session, conversation.tenant_id)
    support = categories["support"]
    decision_model.answer({"department": category_label(support.id)})
    provider = _patch(monkeypatch, categorize_module, FakeLLMProvider([]))

    await categorize_conversation(db_session, conversation, agent, "meu boleto zq-9")

    assert conversation.category_id == support.id
    assert provider.calls == []
    [request] = decision_model.calls
    criteria = request.questions["department"].criteria
    assert set(criteria) == {"none", *(category_label(c.id) for c in categories.values())}
    assert criteria[category_label(support.id)] == "support: d"
    assert "zq-9" in request.state and "zq-9" not in repr(request.questions)
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.outcome) == ("category", JEV, "ok")


async def test_department_none_changes_nothing(
    client, db_session, monkeypatch, jev, decision_model  # noqa: F811
):
    conversation, agent = await _direct(client, db_session, categories=True)
    decision_model.answer({"department": "none"})
    _patch(monkeypatch, categorize_module, FakeLLMProvider([]))

    await categorize_conversation(db_session, conversation, agent, "oi")

    assert conversation.category_id is None
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.outcome) == (JEV, "ok")


async def test_department_hand_off_uses_today_text_path(
    client, db_session, monkeypatch, jev, decision_model  # noqa: F811
):
    conversation, agent = await _direct(client, db_session, categories=True)
    decision_model.answer(hand_off())
    provider = _patch(monkeypatch, categorize_module, FakeLLMProvider([_usage("1")]))

    await categorize_conversation(db_session, conversation, agent, "boleto")

    assert conversation.category_id is not None
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.handed_over) == (AGENT_MODEL, True)


async def test_department_timeout_leaves_the_conversation(
    client, db_session, monkeypatch, jev, job_env, decision_model  # noqa: F811
):
    job_env(CATEGORY_TIMEOUT_SECONDS="0.05")
    conversation, agent = await _direct(client, db_session, categories=True)
    decision_model.answer(Delay(5))
    provider = _patch(monkeypatch, categorize_module, FakeLLMProvider([]))

    await categorize_conversation(db_session, conversation, agent, "boleto")

    assert conversation.category_id is None
    assert provider.calls == []
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.outcome) == (JEV, "timeout")


# --- unset ---------------------------------------------------------------------------


async def test_jobs_unset_never_build_a_decision_model(
    client, fake_redis, db_session, monkeypatch, decision_model  # noqa: F811
):
    conversation, agent = await _direct(client, db_session, categories=True)
    _patch(monkeypatch, sentiment_module, FakeLLMProvider([_usage("0.1")]))
    _patch(monkeypatch, categorize_module, FakeLLMProvider([_usage("0")]))
    await score_frustration(db_session, conversation, agent, "oi")
    await categorize_conversation(db_session, conversation, agent, "oi")
    provider = FakeLLMProvider([LLMResponse(content="Temos horário."), _usage("ALLOW")])
    await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    assert decision_model.targets == [] and decision_model.calls == []
