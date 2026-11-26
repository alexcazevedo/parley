"""A model per auxiliary job, and the record of every job call.

Five calls a turn can make besides the reply are "jobs": the boundary check,
sentiment, category, the rolling summary and the handoff brief. Each has one
setting (`<JOB>_MODEL`, the same for every tenant):

- empty: the agent's own model, exactly as before;
- `llm:<model>`: that chat model on `LLM_BASE_URL` with `LLM_API_KEY`;
- `openai:<model>`, `anthropic:<model>`, `google:<model>`: that vendor's model
  through its own API (`app.llm.providers.native`); the vendor's key
  (`OPENAI_API_KEY`, ...) must be set;
- `typesafe:<model>`: TypeSafe's decision model. Decision jobs only, and
  `TYPESAFE_API_KEY` must be set. A call the decision model cannot take is
  handed to the agent's model on the text path.

A decision job on any of these runs through `app/llm/decisions.py` and gets a
typed answer (the four chat forms through Pydantic AI's chat models, never
handed over). A writing job writes text: on `llm:` through the existing
provider, on a native form through `NativeProvider`.

`validate_job_settings` refuses anything else at start (API and worker).

`record_job_call` writes one `model_job_calls` row per call, after the call,
in a session and transaction of its own; a failure to record is logged and
swallowed so it never fails the job.
"""

import enum
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.llm.base import LLMProvider, LLMResponse
from app.llm.factory import resolve_model
from app.models.agent import Agent
from app.models.model_job_call import ModelJobCall

logger = structlog.get_logger(__name__)


class Job(enum.StrEnum):
    BOUNDARY_CHECK = "boundary_check"
    SENTIMENT = "sentiment"
    CATEGORY = "category"
    SUMMARY = "summary"
    HANDOFF_BRIEF = "handoff_brief"


# Jobs that answer a decision; the others write text.
DECISION_JOBS: frozenset[Job] = frozenset({Job.BOUNDARY_CHECK, Job.SENTIMENT, Job.CATEGORY})

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_TIMEOUT = "timeout"
# An answer the job could not use (unparseable, out of range, empty).
OUTCOME_UNUSABLE = "unusable"

# Setting prefix -> target kind.
_PREFIXES = ("llm", "openai", "anthropic", "google", "typesafe")

# Recording waits at most this long for a lock; it must never hold up a turn.
_RECORD_LOCK_TIMEOUT = "1s"

TargetKind = Literal["agent", "llm", "openai", "anthropic", "google", "typesafe"]

# Vendor APIs a job can name directly (`app.llm.providers.native.NATIVE_PROVIDERS`,
# kept here so this module does not load Pydantic AI).
NATIVE_KINDS: frozenset[str] = frozenset({"openai", "anthropic", "google"})
# Chat models: a decision job on one gets a typed answer through Pydantic AI.
CHAT_KINDS: frozenset[str] = NATIVE_KINDS | {"llm"}
# Kinds whose decision jobs run through `app.llm.decisions` (typed answers).
TYPED_KINDS: frozenset[str] = CHAT_KINDS | {"typesafe"}


class JobSettingsError(RuntimeError):
    """A `<JOB>_MODEL` setting is invalid; raised at start, names the setting."""


@dataclass(frozen=True)
class JobTarget:
    kind: TargetKind
    model: str | None


def setting_name(job: Job) -> str:
    return f"{job.value.upper()}_MODEL"


def job_target(job: Job, settings: Settings | None = None) -> JobTarget:
    """Parse the job's setting. Raises `JobSettingsError` on a malformed value."""
    settings = settings if settings is not None else get_settings()
    raw = (getattr(settings, f"{job.value}_model") or "").strip()
    if not raw:
        return JobTarget(kind="agent", model=None)
    for kind in _PREFIXES:
        prefix = f"{kind}:"
        if raw.startswith(prefix):
            model = raw[len(prefix) :].strip()
            if not model:
                raise JobSettingsError(
                    f"{setting_name(job)}: `{prefix}` needs a model name after it"
                )
            return JobTarget(kind=kind, model=model)  # type: ignore[arg-type]
    raise JobSettingsError(
        f"{setting_name(job)}: must be empty, `llm:<model>`, `openai:<model>`, "
        "`anthropic:<model>`, `google:<model>` or `typesafe:<model>`"
    )


def target_label(target: JobTarget) -> str:
    """The setting's own text, e.g. `typesafe:jev-latest` or `openai:gpt-4o-mini`."""
    return f"{target.kind}:{target.model}"


def key_setting_of(kind: str) -> str:
    """The setting holding a native kind's key, e.g. `ANTHROPIC_API_KEY`."""
    return f"{kind.upper()}_API_KEY"


def validate_job_settings(settings: Settings) -> None:
    """Refuse a bad job setting at start (API and worker)."""
    for job in Job:
        target = job_target(job, settings)
        if target.kind in NATIVE_KINDS:
            if not getattr(settings, f"{target.kind}_api_key", ""):
                raise JobSettingsError(
                    f"{setting_name(job)}: `{target.kind}:` needs {key_setting_of(target.kind)}"
                )
            continue
        if target.kind != "typesafe":
            continue
        if job not in DECISION_JOBS:
            raise JobSettingsError(
                f"{setting_name(job)}: `typesafe:` is a decision model; "
                f"the {job.value} job writes text and needs a chat model"
            )
        if not settings.typesafe_api_key:
            raise JobSettingsError(f"{setting_name(job)}: `typesafe:` needs TYPESAFE_API_KEY")


def job_model(job: Job, agent: Agent, settings: Settings | None = None) -> str:
    """The chat model name the job's text path uses."""
    settings = settings if settings is not None else get_settings()
    target = job_target(job, settings)
    if target.kind in CHAT_KINDS and target.model:
        return target.model
    return resolve_model(agent, settings)


def writing_provider(target: JobTarget, fallback: LLMProvider) -> LLMProvider:
    """The provider a writing job uses: `NativeProvider` on a native form, else `fallback`."""
    if target.kind in NATIVE_KINDS:
        from app.llm.providers.native import NativeProvider

        return NativeProvider(target.kind)
    return fallback


class CallTimer:
    """Wall-clock latency of one job call, started at construction."""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)


def failure_outcome(exc: BaseException) -> str:
    """`timeout` for a timeout, `error` for anything else."""
    return OUTCOME_TIMEOUT if isinstance(exc, TimeoutError) else OUTCOME_ERROR


def usage_of(response: LLMResponse | None) -> tuple[int | None, int | None]:
    """The response's `(prompt_tokens, completion_tokens)`, or `(None, None)`."""
    usage = response.usage if response is not None else None
    if usage is None:
        return None, None
    return usage.prompt_tokens, usage.completion_tokens


SessionSource = AsyncSession | Callable[[], AsyncSession]


def _session_maker(source: SessionSource) -> Callable[[], AsyncSession]:
    if isinstance(source, AsyncSession):
        # A fresh session on the same engine: the record never shares (or
        # rolls back) the caller's transaction.
        if source.bind is None:
            from app.db import async_session_maker

            return async_session_maker
        return async_sessionmaker(bind=source.bind, expire_on_commit=False)
    return source


async def record_job_call(
    session_factory_or_session: SessionSource,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    agent_run_id: uuid.UUID | None,
    job: Job,
    model: str,
    outcome: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
    handed_over: bool = False,
) -> None:
    """Write one `model_job_calls` row in its own short transaction. Never raises."""
    try:
        maker = _session_maker(session_factory_or_session)
        async with maker() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL lock_timeout = '{_RECORD_LOCK_TIMEOUT}'"))
                session.add(
                    ModelJobCall(
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        agent_run_id=agent_run_id,
                        job=str(job),
                        model=model,
                        outcome=outcome,
                        handed_over=handed_over,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ms=latency_ms,
                    )
                )
    except Exception as exc:
        logger.warning(
            "model_job.record_failed",
            job=str(job),
            conversation_id=str(conversation_id),
            error=type(exc).__name__,
        )
