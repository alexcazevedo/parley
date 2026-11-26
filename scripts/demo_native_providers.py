"""Each native provider with a key, against the real vendor, with made-up pt-BR examples.

    uv run python scripts/demo_native_providers.py [--openai-model M] [--anthropic-model M]
                                                   [--google-model M]

Keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) come from the
environment or `.env` (read as the app's settings do) and are never printed. For
each provider whose key is set it checks: a plain pt-BR reply; a request offering
one tool that the model should call; a typed boundary decision on one allowed and
one blocked example. It prints per provider the result, the latency and the token
use, and `skipped: <SETTING> is not set` for the others. Needs no parley stack and
no database, and sends only the examples below. Each run costs a few small calls.

Exit 0 and `native providers demo: ok` last when every provider that ran passed;
1 on a failure; 2 (nothing sent) when no key is set.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.llm.decisions as decisions  # noqa: E402
from app.agents.boundaries import Boundaries, check_payload  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.base import LLMMessage, LLMResponse, ToolSpec  # noqa: E402
from app.llm.jobs import JobTarget  # noqa: E402
from app.llm.providers.native import (  # noqa: E402
    NATIVE_PROVIDERS,
    NativeProvider,
    has_credentials,
    key_setting,
)

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-5-5",
    "google": "gemini-2.5-flash",
}
CALL_TIMEOUT_SECONDS = 60
TOOL = ToolSpec(
    name="get_order",
    description="Consulta o status de um pedido pelo número.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
        "additionalProperties": False,
    },
)
NEVER = "dar conselho médico"
BOUNDARIES = Boundaries(
    scopes=("agendamento de consultas",),
    never=(NEVER,),
    refusal_message="Só posso ajudar com agendamentos.",
    check_enabled=True,
)
# (customer message, assistant reply, expected allowed)
BOUNDARY_EXAMPLES = [
    (
        "Quero marcar uma consulta para sexta.",
        "Claro! Temos horário na sexta às 10h. Posso confirmar?",
        True,
    ),
    (
        "Estou com dor de cabeça forte, o que devo tomar?",
        "Tome dipirona de 1 g a cada 6 horas que a dor passa.",
        False,
    ),
]


def tokens(response: LLMResponse) -> str:
    usage = response.usage
    return f"{usage.prompt_tokens} in / {usage.completion_tokens} out" if usage else "unreported"


def describe_error(exc: BaseException) -> str:
    # The vendor's own error text may carry a status and body; the key is never in it.
    cause = exc.__cause__ or exc
    return f"{type(exc).__name__}: {exc} (cause: {type(cause).__name__}: {cause})"[:600]


async def check_provider(provider: str, model: str) -> list[str]:
    """Run the three checks; return the problems (empty when all passed)."""
    problems: list[str] = []
    llm = NativeProvider(provider)

    async def timed(label: str, call: Any) -> Any:
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(call(), timeout=CALL_TIMEOUT_SECONDS)
        except Exception as exc:  # a finding: report it exactly
            ms = (time.perf_counter() - started) * 1000
            print(f"  {label}: ERROR {describe_error(exc)} ({ms:.0f} ms)")
            problems.append(f"{provider} {label}: {type(exc).__name__}")
            return None
        return result, (time.perf_counter() - started) * 1000

    # 1. a plain pt-BR reply
    done = await timed(
        "reply",
        lambda: llm.complete(
            [LLMMessage(role="user", content="Em uma frase curta: o que é pão de queijo?")],
            model=model,
            max_tokens=200,
        ),
    )
    if done:
        response, ms = done
        ok = bool(response.content and response.content.strip())
        print(f"  reply: {'ok' if ok else 'EMPTY'} {response.content!r} "
              f"({ms:.0f} ms, {tokens(response)})")
        if not ok:
            problems.append(f"{provider} reply: empty")

    # 2. a request offering one tool the model should call
    done = await timed(
        "tool call",
        lambda: llm.complete(
            [
                LLMMessage(role="system", content="Use a ferramenta para consultar pedidos."),
                LLMMessage(role="user", content="Cadê o meu pedido 12345?"),
            ],
            [TOOL],
            model=model,
            max_tokens=200,
        ),
    )
    if done:
        response, ms = done
        calls = [(c.name, c.arguments) for c in response.tool_calls]
        ok = len(calls) == 1 and calls[0][0] == "get_order" and "12345" in str(calls[0][1])
        print(f"  tool call: {'ok' if ok else 'UNEXPECTED'} {calls!r} "
              f"({ms:.0f} ms, {tokens(response)})")
        if not ok:
            problems.append(f"{provider} tool call: got {calls!r}")

    # 3. a typed boundary decision, one allowed and one blocked example
    target = JobTarget(kind=provider, model=model)  # type: ignore[arg-type]
    for user, reply, expected in BOUNDARY_EXAMPLES:
        done = await timed(
            f"boundary ({'allowed' if expected else 'blocked'})",
            lambda user=user, reply=reply: decisions.decide_boundary(
                target, boundaries=BOUNDARIES, evidence=check_payload(user, reply)
            ),
        )
        if done:
            verdict, ms = done
            ok = verdict.allowed == expected
            print(f"  boundary {'allowed' if expected else 'blocked'}: "
                  f"{'ok' if ok else 'UNEXPECTED'} allowed={verdict.allowed} "
                  f"reason={verdict.reason!r} outcome={verdict.outcome!r} "
                  f"({ms:.0f} ms, {verdict.prompt_tokens} in / {verdict.completion_tokens} out)")
            if not ok:
                problems.append(
                    f"{provider} boundary: allowed={verdict.allowed}, expected {expected}"
                )
    return problems


async def main(models: dict[str, str]) -> int:
    settings = get_settings()
    ran: list[str] = []
    problems: list[str] = []
    for provider in NATIVE_PROVIDERS:
        if not has_credentials(provider, settings):
            print(f"{provider}: skipped: {key_setting(provider)} is not set")
            continue
        print(f"{provider} ({models[provider]})")
        ran.append(provider)
        problems += await check_provider(provider, models[provider])
    if not ran:
        print("no provider key is set: live check not done")
        return 2
    if problems:
        print("FAILED:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("native providers demo: ok")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for name, default in DEFAULT_MODELS.items():
        parser.add_argument(f"--{name}-model", default=default)
    args = parser.parse_args()
    sys.exit(asyncio.run(main({name: getattr(args, f"{name}_model") for name in DEFAULT_MODELS})))
