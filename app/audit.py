"""Read-only audit of rows an older version could have left behind (PRODUCT.md F4).

Older versions accepted a channel whose `agent_id` belongs to another tenant
(conversations copy that `agent_id`) and any Evolution `config.base_url`. The
code now refuses both at run time; this lists them so an operator can fix them.

Run: `python -m app.audit` (compose: `docker compose run --rm api python -m app.audit`).
One line per finding, `<kind> <row_id> tenant=<tenant_id> <detail>`, then a last
line `N offending rows`. Exit code 1 when N > 0, else 0.

`--resolve` also resolves each channel hostname (DNS) and lists those that
resolve to a private address or do not resolve; run it where the server runs.

Strictly read-only: it never adds, deletes, updates or commits, and without
`--resolve` makes no network or DNS call. Output carries ids, the agent id and the refused URL's
reason only: never a channel secret, API key or the rest of `config`.
"""

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# `app.tools` and `app.agents` import each other; loading `app.agents` first
# (as the worker does) is the order that resolves. Needed when run as a script.
import app.agents  # noqa: F401  isort: skip
from app.models.agent import Agent
from app.models.channel import Channel, ChannelType
from app.models.conversation import Conversation
from app.tools.ssrf import SSRFError, assert_safe_channel_url, validate_channel_base_url

CHANNEL_FOREIGN_AGENT = "channel_foreign_agent"
CONVERSATION_FOREIGN_AGENT = "conversation_foreign_agent"
CHANNEL_REFUSED_BASE_URL = "channel_refused_base_url"


@dataclass(frozen=True)
class Finding:
    kind: str
    row_id: uuid.UUID
    tenant_id: uuid.UUID
    detail: str


async def find_offending_rows(
    session: AsyncSession, *, resolve: bool = False
) -> list[Finding]:
    """With `resolve=True`, channels that pass the static check are also resolved
    through DNS (`assert_safe_channel_url`); a private or unresolvable hostname
    is reported. Without it no network or DNS call is made."""
    findings: list[Finding] = []

    for model, kind in (
        (Channel, CHANNEL_FOREIGN_AGENT),
        (Conversation, CONVERSATION_FOREIGN_AGENT),
    ):
        rows = await session.execute(
            select(model.id, model.tenant_id, model.agent_id)
            .join(Agent, Agent.id == model.agent_id)
            .where(Agent.tenant_id != model.tenant_id)
            .order_by(model.id)
        )
        findings.extend(
            Finding(kind, row_id, tenant_id, f"agent={agent_id}")
            for row_id, tenant_id, agent_id in rows
        )

    rows = await session.execute(
        select(Channel.id, Channel.tenant_id, Channel.config)
        .where(Channel.type == ChannelType.WHATSAPP_EVOLUTION)
        .order_by(Channel.id)
    )
    for row_id, tenant_id, config in rows:
        if not isinstance(config, dict) or config.get("base_url") is None:
            continue
        base_url = config["base_url"]
        if not isinstance(base_url, str):
            findings.append(
                Finding(CHANNEL_REFUSED_BASE_URL, row_id, tenant_id, "base_url is not a string")
            )
            continue
        try:
            validate_channel_base_url(base_url)
            if resolve:
                await assert_safe_channel_url(base_url)
        except SSRFError as exc:
            findings.append(Finding(CHANNEL_REFUSED_BASE_URL, row_id, tenant_id, str(exc)))
    return findings


async def _run(resolve: bool = False) -> list[Finding]:
    # Imported here so importing `app.audit` does not build an engine.
    from app.db import async_session_maker, engine

    try:
        async with async_session_maker() as session:
            return await find_offending_rows(session, resolve=resolve)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.audit", description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="also resolve channel hostnames through DNS and list private or unresolvable ones",
    )
    # `None` means no flags (not `sys.argv`); the script entry point passes them.
    args = parser.parse_args([] if argv is None else argv)
    findings = asyncio.run(_run(args.resolve))
    for f in findings:
        print(f"{f.kind} {f.row_id} tenant={f.tenant_id} {f.detail}")
    print(f"{len(findings)} offending rows")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
