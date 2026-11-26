"""Decision jobs on a chat model (`llm:`, `openai:`, `anthropic:`, `google:`): typed
answers through Pydantic AI (`app/llm/decisions.py`), on the scripted vendor
model of `tests/native_fakes.py` (never a vendor).

The native forms go through the real `build_chat_model` (which asks
`build_native_model`, replaced by the fixture); `llm:` replaces
`build_chat_model` with the same script. A chat model answers the answer type
through Pydantic AI's output tool (`final_result`).
"""

import pytest
from sqlalchemy import select

import app.agents.categorize as categorize_module
import app.agents.sentiment as sentiment_module
import app.llm.decisions as decisions_module
from app.agents.boundaries import DEFAULT_REFUSAL_MESSAGE
from app.agents.categorize import categorize_conversation
from app.agents.sentiment import score_frustration
from app.config import Settings, get_settings
from app.conversations.summary import maybe_summarize
from app.llm.base import LLMResponse
from app.llm.compare import CompareError, parse_target, run
from app.llm.decisions import build_chat_model, category_label
from app.llm.jobs import (
    Job,
    JobSettingsError,
    JobTarget,
    job_target,
    target_label,
    validate_job_settings,
)
from app.llm.providers.fake import FakeLLMProvider
from app.main import create_app
from app.models.agent_run import AgentRun
from app.models.category import Category
from tests import native_fakes
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

native_model = native_fakes.native_model

CHAT_FORMS = ["llm", "openai", "anthropic", "google"]
MODEL = "chat-small"
REPLY = "Tome dipirona de 8 em 8 horas."
NEVER_ITEM = BOUNDED["boundary_never"][0]
OUTPUT_TOOL = "final_result"
KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY")


@pytest.fixture
def chat(monkeypatch, job_env, native_model):  # noqa: F811
    """Set the decision jobs to `<form>:chat-small`; returns the script."""

    def use(form: str, *jobs: str) -> native_fakes.NativeScript:
        names = jobs or ("BOUNDARY_CHECK_MODEL", "SENTIMENT_MODEL", "CATEGORY_MODEL")
        job_env(**dict.fromkeys(names, f"{form}:{MODEL}"), **dict.fromkeys(KEYS, "test-key"))
        if form == "llm":
            monkeypatch.setattr(
                decisions_module,
                "build_chat_model",
                lambda target, settings=None: native_model.build("llm", target.model),
            )
        return native_model

    return use


def _answer(script: native_fakes.NativeScript, **fields) -> None:
    script.call(OUTPUT_TOOL, fields, input_tokens=17, output_tokens=6)


async def _run(db_session, conversation_id) -> AgentRun:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    return result.scalars().one()


# --- settings ------------------------------------------------------------------------


@pytest.mark.parametrize("form", [*CHAT_FORMS, "typesafe"])
def test_every_form_parses_and_labels_as_written(form):
    settings = Settings(sentiment_model=f"{form}:{MODEL}")
    target = job_target(Job.SENTIMENT, settings)
    assert target == JobTarget(kind=form, model=MODEL)  # type: ignore[arg-type]
    assert target_label(target) == f"{form}:{MODEL}"
    assert decisions_module.target_label is target_label


@pytest.mark.parametrize(
    ("form", "key"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ],
)
@pytest.mark.parametrize("job", ["sentiment_model", "summary_model"])
def test_native_form_without_its_key_stops_startup(form, key, job):
    keys = {"openai_api_key": "", "anthropic_api_key": "", "google_api_key": ""}
    settings = Settings(demo_mode=True, **keys, **{job: f"{form}:{MODEL}"})
    with pytest.raises(JobSettingsError, match=f"{job.upper()}.*{key}"):
        create_app(settings)
    validate_job_settings(settings.model_copy(update={key.lower(): "k"}))


def test_llm_chat_model_is_the_configured_endpoint():
    from pydantic_ai.models.openai import OpenAIChatModel

    settings = Settings(llm_base_url="http://llm.local:9100/v1", llm_api_key="k")
    model = build_chat_model(JobTarget(kind="llm", model=MODEL), settings)
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == MODEL
    assert str(model.base_url).rstrip("/") == "http://llm.local:9100/v1"


def test_native_chat_model_is_the_vendor_model(native_model):  # noqa: F811
    model = build_chat_model(JobTarget(kind="anthropic", model=MODEL), Settings())
    assert model.model_name == "fake-anthropic"


# --- boundary check, inside a turn -----------------------------------------------------


@pytest.mark.parametrize("form", CHAT_FORMS)
async def test_boundary_blocks_with_the_rules_text(client, fake_redis, db_session, chat, form):
    script = chat(form)
    _answer(script, allowed=False, broken="never_1")
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run_row = await _run(db_session, conversation_id)
    assert run_row.boundary_reason == NEVER_ITEM
    assert run_row.blocked_text == REPLY
    assert run_row.output_text == DEFAULT_REFUSAL_MESSAGE
    # The reply on the agent's model, the check on the chat model only.
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    [request] = script.requests
    assert (request.provider, request.model) == (form, MODEL)
    # The answer type is the output tool's, not a function tool.
    assert request.tools == []
    assert '"user_message": "oi"' in str(request.parts()[-1].content)
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome, row.handed_over) == (f"{form}:{MODEL}", "ok", False)
    assert (row.prompt_tokens, row.completion_tokens) == (17, 6)


async def test_boundary_allowed_ignores_the_threshold(
    client, fake_redis, db_session, chat, job_env  # noqa: F811
):
    script = chat("openai")
    job_env(BOUNDARY_CHECK_ALLOW_THRESHOLD="1.0")
    _answer(script, allowed=True, broken="scope")
    provider = FakeLLMProvider([LLMResponse(content="Temos horário às 10h.")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run_row = await _run(db_session, conversation_id)
    assert run_row.output_text == "Temos horário às 10h."
    assert run_row.boundary_reason is None


@pytest.mark.parametrize("form", ["llm", "google"])
async def test_invalid_structured_answer_blocks_unparseable_after_one_retry(
    client, fake_redis, db_session, chat, form
):
    script = chat(form)
    _answer(script, allowed="perhaps", broken="never_1")
    script.reply("ALLOW")  # text, not the output tool
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run_row = await _run(db_session, conversation_id)
    assert run_row.boundary_reason == "check_unparseable"
    assert run_row.output_text == DEFAULT_REFUSAL_MESSAGE
    assert len(script.requests) == 2
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome) == (f"{form}:{MODEL}", "unusable")


@pytest.mark.parametrize("outcome", ["error", "timeout"])
async def test_boundary_error_and_timeout_fail_as_today(
    client, fake_redis, db_session, chat, job_env, outcome  # noqa: F811
):
    script = chat("anthropic")
    job_env(BOUNDARY_CHECK_TIMEOUT_SECONDS="0.1")
    if outcome == "error":
        script.fail(RuntimeError("vendor down"))
    else:
        script.delay(5)
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    run_row = await _run(db_session, conversation_id)
    assert run_row.boundary_reason == "check_failed"
    assert [call["model"] for call in provider.calls] == [AGENT_MODEL]
    [row] = await _check_rows(db_session, conversation_id)
    assert (row.model, row.outcome, row.handed_over) == (f"anthropic:{MODEL}", outcome, False)


# --- sentiment -------------------------------------------------------------------------


@pytest.mark.parametrize("form", CHAT_FORMS)
async def test_sentiment_score_trips_escalation(client, db_session, monkeypatch, chat, form):
    script = chat(form)
    conversation, agent = await _direct(client, db_session)
    provider = _patch(monkeypatch, sentiment_module, FakeLLMProvider([]))
    _answer(script, level=4)

    score = await score_frustration(db_session, conversation, agent, "vou cancelar tudo zq-5")

    assert score == 1.0
    assert score >= get_settings().sentiment_threshold
    assert provider.calls == []
    [request] = script.requests
    assert "zq-5" in str(request.parts()[-1].content)
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.outcome) == ("sentiment", f"{form}:{MODEL}", "ok")
    assert row.prompt_tokens == 17


async def test_sentiment_invalid_answer_is_none(client, db_session, monkeypatch, chat):
    script = chat("openai")
    conversation, agent = await _direct(client, db_session)
    _patch(monkeypatch, sentiment_module, FakeLLMProvider([]))
    _answer(script, level=9)
    _answer(script, level="furious")

    assert await score_frustration(db_session, conversation, agent, "péssimo") is None
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.outcome, row.handed_over) == (f"openai:{MODEL}", "unusable", False)


async def test_sentiment_error_is_none(client, db_session, monkeypatch, chat):
    script = chat("llm")
    conversation, agent = await _direct(client, db_session)
    provider = _patch(monkeypatch, sentiment_module, FakeLLMProvider([]))
    script.fail()

    assert await score_frustration(db_session, conversation, agent, "péssimo") is None
    assert provider.calls == []
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.outcome) == (f"llm:{MODEL}", "error")


# --- department ------------------------------------------------------------------------


async def _categories(db_session, tenant_id) -> dict[str, Category]:
    result = await db_session.execute(select(Category).where(Category.tenant_id == tenant_id))
    return {category.name: category for category in result.scalars().all()}


@pytest.mark.parametrize("form", CHAT_FORMS)
async def test_department_is_set(client, db_session, monkeypatch, chat, form):
    script = chat(form)
    conversation, agent = await _direct(client, db_session, categories=True)
    support = (await _categories(db_session, conversation.tenant_id))["support"]
    provider = _patch(monkeypatch, categorize_module, FakeLLMProvider([]))
    _answer(script, department=category_label(support.id))

    await categorize_conversation(db_session, conversation, agent, "meu boleto")

    assert conversation.category_id == support.id
    assert provider.calls == []
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.outcome) == ("category", f"{form}:{MODEL}", "ok")


async def test_department_invalid_answer_changes_nothing(client, db_session, monkeypatch, chat):
    script = chat("google")
    conversation, agent = await _direct(client, db_session, categories=True)
    _patch(monkeypatch, categorize_module, FakeLLMProvider([]))
    _answer(script, department="billing")  # a name, not one of the labels
    _answer(script, department="dept_000000000000")

    await categorize_conversation(db_session, conversation, agent, "boleto")

    assert conversation.category_id is None
    assert len(script.requests) == 2
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.outcome) == (f"google:{MODEL}", "unusable")


async def test_department_timeout_changes_nothing(
    client, db_session, monkeypatch, chat, job_env  # noqa: F811
):
    script = chat("openai")
    job_env(CATEGORY_TIMEOUT_SECONDS="0.05")
    conversation, agent = await _direct(client, db_session, categories=True)
    _patch(monkeypatch, categorize_module, FakeLLMProvider([]))
    script.delay(5)

    await categorize_conversation(db_session, conversation, agent, "boleto")

    assert conversation.category_id is None
    [row] = await _rows(db_session, conversation.id)
    assert (row.model, row.outcome) == (f"openai:{MODEL}", "timeout")


# --- writing jobs and unset jobs ------------------------------------------------------


async def test_writing_job_on_a_native_form_writes_text(client, db_session, chat):
    script = chat("anthropic", "SUMMARY_MODEL")
    script.reply("Cliente disse oi.", input_tokens=12, output_tokens=4)
    conversation, agent = await _direct(client, db_session)
    agent.summary_threshold = 1
    passed = FakeLLMProvider([])

    await maybe_summarize(db_session, agent, conversation, passed)

    assert conversation.summary == "Cliente disse oi."
    assert passed.calls == []
    [request] = script.requests
    assert (request.provider, request.model, request.tools) == ("anthropic", MODEL, [])
    [row] = await _rows(db_session, conversation.id)
    assert (row.job, row.model, row.outcome) == ("summary", f"anthropic:{MODEL}", "ok")
    assert (row.prompt_tokens, row.completion_tokens) == (12, 4)


async def test_unset_jobs_keep_the_text_form(
    client, fake_redis, db_session, monkeypatch, native_model  # noqa: F811
):
    def no_chat_model(target, settings=None):
        raise AssertionError("a chat model was built for an unset job")

    monkeypatch.setattr(decisions_module, "build_chat_model", no_chat_model)
    conversation, agent = await _direct(client, db_session, categories=True)
    sentiment = _patch(monkeypatch, sentiment_module, FakeLLMProvider([_usage("0.8")]))
    _patch(monkeypatch, categorize_module, FakeLLMProvider([_usage("1")]))

    assert await score_frustration(db_session, conversation, agent, "péssimo") == 0.8
    await categorize_conversation(db_session, conversation, agent, "boleto")
    provider = FakeLLMProvider([LLMResponse(content="Temos horário."), _usage("ALLOW")])
    conversation_id = await _turn(client, fake_redis, db_session, provider, **BOUNDED)

    assert [call["model"] for call in sentiment.calls] == [AGENT_MODEL]
    assert conversation.category_id is not None
    assert {call["model"] for call in provider.calls} == {AGENT_MODEL}
    assert native_model.requests == []
    [row] = await _check_rows(db_session, conversation_id)
    assert row.model == AGENT_MODEL


# --- the comparison command -----------------------------------------------------------


async def test_compare_runs_with_a_native_form(client, db_session, test_engine, chat, capsys):
    from tests.test_model_compare import _add_run, _tenant

    script = chat("openai", "SENTIMENT_MODEL")
    conversation = await _tenant(client, db_session, **BOUNDED)
    await _add_run(db_session, conversation, 1, blocked="x", reason=NEVER_ITEM)
    await _add_run(db_session, conversation, 2)
    _answer(script, allowed=True, broken="none")
    _answer(script, allowed=True, broken="none")

    code = await run(
        ["--tenant", str(conversation.tenant_id), "--model", f"openai:{MODEL}"], test_engine
    )

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[-1] == "compared 2 replies: 1 agree, 1 disagree, 0 skipped"
    assert {(r.provider, r.model) for r in script.requests} == {("openai", MODEL)}


def test_compare_native_form_needs_its_key(job_env):  # noqa: F811
    job_env(GOOGLE_API_KEY="")
    with pytest.raises(CompareError, match="GOOGLE_API_KEY"):
        parse_target(f"google:{MODEL}")
    job_env(GOOGLE_API_KEY="k")
    assert parse_target(f"google:{MODEL}") == JobTarget(kind="google", model=MODEL)
