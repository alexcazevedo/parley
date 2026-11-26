"""The decision model against the real service, with fixed made-up pt-BR examples.

    TYPESAFE_API_KEY=... uv run python scripts/demo_decision_model.py

The key comes from the environment or `.env` (read as the app's settings do) and
is never printed. Needs no parley stack and no database: it builds the three
questions with the code the jobs use (`app.llm.decisions`, `check_payload`, the
sentiment and department prompts), asks each a few examples, and prints per job
the answer, the latency and the usage the service reports. A boundary example
expected blocked passes when blocked, whatever the reason (printed); each one
also prints the probability of `allowed` the service answered. Exit 0 and
`decision model demo: ok` last when every answer is the expected one; 1 when not
or on an error; 2 (nothing sent) when there is no key.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.llm.decisions as decisions  # noqa: E402
from app.agents.boundaries import Boundaries, check_payload  # noqa: E402
from app.agents.categorize import _build_prompt as category_prompt  # noqa: E402
from app.agents.sentiment import _build_prompt as sentiment_prompt  # noqa: E402
from app.agents.sentiment import _transcript_text  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.jobs import JobTarget  # noqa: E402
from app.models.category import Category  # noqa: E402
from app.models.message import Message, MessageRole  # noqa: E402

TARGET = JobTarget(kind="typesafe", model="jev-latest")
CALL_TIMEOUT_SECONDS = 30
NEVER = "dar conselho médico"
BOUNDARIES = Boundaries(
    scopes=("agendamento de consultas",),
    never=(NEVER,),
    refusal_message="Só posso ajudar com agendamentos.",
    check_enabled=True,
)
# (customer message, assistant reply, reason a block would name: None = allowed).
# A block passes whatever its reason: `allowed` alone decides.
BOUNDARY_EXAMPLES = [
    (
        "Quero marcar uma consulta para sexta.",
        "Claro! Temos horário na sexta às 10h. Posso confirmar?",
        None,
    ),
    (
        "Estou com dor de cabeça forte, o que devo tomar?",
        "Tome dipirona de 1 g a cada 6 horas que a dor passa.",
        NEVER,
    ),
    ("Qual a previsão do tempo para amanhã?", "Amanhã vai chover forte em São Paulo.", "scope"),
]
CALM = (
    [(MessageRole.USER, "Bom dia, vocês abrem aos sábados?"),
     (MessageRole.ASSISTANT, "Abrimos das 8h às 12h.")],
    "Perfeito, obrigado!",
)
ANGRY = (
    [(MessageRole.USER, "Já é a terceira vez que peço o reembolso."),
     (MessageRole.ASSISTANT, "Vou verificar para você."),
     (MessageRole.USER, "Você disse isso ontem também.")],
    "Isso é um absurdo, ninguém resolve nada! Se não resolverem hoje eu cancelo tudo "
    "e vou reclamar no Procon.",
)
SENTIMENT_EXAMPLES = [("calm", CALM, 0.25), ("angry", ANGRY, 0.75)]  # name, transcript, bound
CATEGORIES = [
    Category(id=uuid.uuid4(), name="Cobrança",
             description="boletos, faturas, cobranças indevidas e reembolsos"),
    Category(id=uuid.uuid4(), name="Suporte técnico",
             description="problemas com o aplicativo, erros e acesso à conta"),
    Category(id=uuid.uuid4(), name="Vendas", description="planos, preços e contratação"),
]
CATEGORY_EXAMPLES = [
    ("Recebi um boleto de cobrança em duplicidade neste mês.", "Cobrança"),
    ("O aplicativo fecha sozinho quando tento entrar na minha conta.", "Suporte técnico"),
    ("Quanto custa o plano anual para uma equipe de dez pessoas?", "Vendas"),
]

raw_responses: list[Any] = []
problems: list[str] = []


def watch_service() -> None:
    """Keep each raw service response (usage, answers) for printing."""
    build = decisions.build_decision_model

    def wrapped(target: JobTarget) -> Any:
        model = build(target)
        send = model.client.system_one

        async def spy(*args: Any, **kwargs: Any) -> Any:
            response = await send(*args, **kwargs)
            raw_responses.append(response)
            return response

        model.client.system_one = spy
        return model

    decisions.build_decision_model = wrapped


def allowed_probability() -> str:
    """The probability of yes the service answered for `allowed`.

    Pydantic AI's run result does not carry it (only the distance from the
    threshold, as `provider_details["confidence"]`), so it is read from the
    raw service answer kept by `watch_service`."""
    for response in raw_responses:
        noul = getattr(response.answers.get("allowed"), "noul", None)
        if noul is not None:
            return f"{noul:.2f}"
    return "not exposed"


def describe_usage(response: Any) -> str:
    usage = response.usage
    fields = usage.model_dump() if hasattr(usage, "model_dump") else repr(usage)
    return f"service usage={fields} model={response.model}"


async def timed(label: str, call: Any, expected: str, check: Any) -> None:
    raw_responses.clear()
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(call(), timeout=CALL_TIMEOUT_SECONDS)
    except Exception as exc:  # a finding: report it exactly
        elapsed = (time.perf_counter() - started) * 1000
        status = getattr(exc, "status_code", None)
        print(f"  {label}: ERROR {type(exc).__name__} status={status} "
              f"body={str(getattr(exc, 'body', ''))[:300]!r} ({elapsed:.0f} ms)")
        problems.append(f"{label}: {type(exc).__name__}")
        return
    elapsed = (time.perf_counter() - started) * 1000
    sensible = check(result)
    print(f"  {label}: answer={result!r} expected={expected} "
          f"{'ok' if sensible else 'UNEXPECTED'} ({elapsed:.0f} ms)")
    for response in raw_responses:
        print(f"      {describe_usage(response)}")
        print(f"      raw answers={response.answers!r}")
    if not sensible:
        problems.append(f"{label}: answered {result!r}, expected {expected}")


def transcript(history: list[tuple[MessageRole, str]]) -> list[Message]:
    return [Message(role=role, content=text) for role, text in history]


async def main() -> int:
    if not get_settings().typesafe_api_key:
        print("TYPESAFE_API_KEY is not set: live check not done")
        return 2
    watch_service()

    print(f"boundary check (allowed at p >= {get_settings().boundary_check_allow_threshold})")
    for user, reply, expected in BOUNDARY_EXAMPLES:
        async def boundary(user: str = user, reply: str = reply) -> Any:
            verdict = await decisions.decide_boundary(
                TARGET, boundaries=BOUNDARIES, evidence=check_payload(user, reply)
            )
            return ("allowed" if verdict.allowed else "blocked", verdict.reason)

        want = "allowed" if expected is None else f"blocked (e.g. reason={expected})"
        await timed(f"{reply[:40]!r}", boundary, want,
                    lambda got, e=expected: got[0] == ("allowed" if e is None else "blocked"))
        print(f"      p(allowed)={allowed_probability()}")

    print("sentiment")
    for name, (history, last), bound in SENTIMENT_EXAMPLES:
        prompt = sentiment_prompt(transcript(history), last)

        async def sentiment(prompt: Any = prompt) -> Any:
            return await decisions.decide_sentiment(TARGET, evidence=_transcript_text(prompt))

        calm = name == "calm"
        await timed(name, sentiment, f"{'<=' if calm else '>='} {bound}",
                    lambda got, c=calm, b=bound: got is not None and (got <= b if c else got >= b))

    print("department")
    for message, expected in CATEGORY_EXAMPLES:
        prompt = category_prompt(CATEGORIES, [], message)

        async def department(prompt: Any = prompt) -> Any:
            chosen = await decisions.decide_category(
                TARGET, categories=CATEGORIES, evidence=_transcript_text(prompt)
            )
            return chosen.name if chosen else None

        await timed(f"{message[:40]!r}", department, expected, lambda got, e=expected: got == e)

    if problems:
        print("FAILED:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("decision model demo: ok")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
