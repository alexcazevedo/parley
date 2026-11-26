"""Export a tenant as a manifest: what `POST /manifest/apply` would need to
produce the same tenant. Read only; never contains a credential.

Every item is listed, managed or not. A managed item keeps its `manifest_items`
key; an unmanaged agent or channel gets a key slugged from its name. Fields
equal to their default are left out. A stored credential becomes a secret
reference: the name the managed item was applied with, else a generated name
(`tool_<name>_token`, `channel_<key>_secret`),
and `secrets_needed` lists the referenced names that are not set yet.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agents import AgentCreate
from app.api.tools import ToolCreate, decode_auth_config
from app.manifest.apply import TenantState, load_state, unmanaged_items
from app.models.agent import DEFAULT_HANDOFF_MESSAGE, Agent
from app.models.channel import Channel, ChannelType
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.repositories.secrets import SecretsRepository

_MAX_NAME = 64
_UNSAFE_KEY = re.compile(r"[^a-z0-9_-]+")
_UNSAFE_SECRET = re.compile(r"[^a-z0-9_]+")
# Keys of a tool's `auth_config` that are not the credential.
_AUTH_PLAIN_KEYS = ("type", "header_name")


@dataclass
class ExportResult:
    manifest: dict[str, Any]
    secrets_needed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def slug_key(name: str, taken: set[str]) -> str:
    """`^[a-z0-9_-]{1,64}$` from a name; `-2`, `-3` on a clash with `taken`
    (which grows by the returned key)."""
    base = _UNSAFE_KEY.sub("-", name.lower()).strip("-")[:_MAX_NAME].strip("-") or "item"
    key, n = base, 1
    while key in taken:
        n += 1
        suffix = f"-{n}"
        key = base[: _MAX_NAME - len(suffix)].rstrip("-") + suffix
    taken.add(key)
    return key


def secret_name(prefix: str, name: str, suffix: str, taken: set[str]) -> str:
    """`<prefix><name slugged to [a-z0-9_]><suffix>`, at most 64 characters and
    distinct from every name in `taken` (`_2`, `_3` on a clash)."""
    slug = _UNSAFE_SECRET.sub("_", name.lower()).strip("_") or "item"
    room = _MAX_NAME - len(prefix) - len(suffix)
    base, n = slug[:room], 1
    while True:
        extra = "" if n == 1 else f"_{n}"
        candidate = f"{prefix}{base[: room - len(extra)]}{extra}{suffix}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        n += 1


def _without_defaults(model: type[BaseModel], values: dict[str, Any]) -> dict[str, Any]:
    """`values` in the model's field order, minus the fields at their default."""
    out: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name not in values:
            continue
        value = values[name]
        if not info.is_required() and value == info.get_default(call_default_factory=True):
            continue
        out[name] = value
    return out


def _agent_entry(agent: Agent, key: str) -> dict[str, Any]:
    values = {name: getattr(agent, name) for name in AgentCreate.model_fields}
    if values["handoff_message"] == DEFAULT_HANDOFF_MESSAGE:
        values["handoff_message"] = None
    return {"key": key, **_without_defaults(AgentCreate, values)}


def _tool_entry(
    tool: Tool,
    secrets: set[str],
    taken: set[str],
    warnings: list[str],
    remembered: dict[str, str],
) -> dict[str, Any]:
    values: dict[str, Any] = {
        name: getattr(tool, name) for name in ToolCreate.model_fields if name != "auth_config"
    }
    stored: Any = None
    try:
        stored = decode_auth_config(tool.auth_config_encrypted)
    except Exception:  # unreadable (key rotated): exported without it
        warnings.append(f"the credentials of tool '{tool.name}' cannot be read; not exported")
    values["auth_config"] = None
    if isinstance(stored, dict):
        auth = {k: stored[k] for k in _AUTH_PLAIN_KEYS if k in stored}
        if stored.get("token"):
            name = remembered.get("token") or secret_name("tool_", tool.name, "_token", taken)
            auth["token"] = {"secret": name}
            secrets.add(name)
        dropped = set(stored) - set(_AUTH_PLAIN_KEYS) - {"token"}
        if dropped:
            warnings.append(
                f"tool '{tool.name}' has auth_config keys that are not exported: "
                f"{', '.join(sorted(dropped))}"
            )
        if auth or not dropped:
            values["auth_config"] = auth
    return _without_defaults(ToolCreate, values)


def _channel_entry(
    channel: Channel,
    key: str,
    agent_key: str,
    secrets: set[str],
    taken: set[str],
    remembered: dict[str, str],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "key": key,
        "type": channel.type.value,
        "name": channel.name,
        "agent": agent_key,
    }
    if channel.config:
        entry["config"] = channel.config
    if channel.type == ChannelType.WHATSAPP_EVOLUTION and channel.secret_encrypted:
        name = remembered.get("secret") or secret_name("channel_", key, "_secret", taken)
        entry["secret"] = {"secret": name}
        secrets.add(name)
    return entry


def _duplicate_warnings(state: TenantState) -> list[str]:
    warnings: list[str] = []
    agents: dict[str, int] = {}
    for agent in unmanaged_items(state, "agent"):
        agents[agent.name] = agents.get(agent.name, 0) + 1
    for name, count in sorted(agents.items()):
        if count > 1:
            warnings.append(
                f"{count} agents are named '{name}'; rename one or delete it "
                "before applying the manifest"
            )
    channels: dict[tuple[str, str], int] = {}
    for channel in unmanaged_items(state, "channel"):
        pair = (channel.name, channel.type.value)
        channels[pair] = channels.get(pair, 0) + 1
    for (name, kind), count in sorted(channels.items()):
        if count > 1:
            warnings.append(
                f"{count} channels of type {kind} are named '{name}'; rename one or "
                "delete it before applying the manifest"
            )
    return warnings


def _keys(state: TenantState, kind: str, name_of: Any) -> dict[uuid.UUID, str]:
    """Id -> key of every row of `kind`: the managed key, else a slug of the
    name that clashes with no other key of the kind."""
    keys = {r.item_id: key for (k, key), r in state.records.items() if k == kind}
    keys = {item_id: key for item_id, key in keys.items() if item_id in state.rows(kind)}
    taken = set(keys.values())
    for row in state.rows(kind).values():
        if row.id not in keys:
            keys[row.id] = slug_key(name_of(row), taken)
    return keys


async def export_manifest(session: AsyncSession, tenant: Tenant) -> ExportResult:
    """The tenant's current agents, departments, tools and channels as a manifest."""
    state = await load_state(session, tenant.id)
    warnings = _duplicate_warnings(state)
    referenced: set[str] = set()

    agent_keys = _keys(state, "agent", lambda a: a.name)
    channel_keys = _keys(state, "channel", lambda c: c.name)

    agents = sorted(
        (_agent_entry(a, agent_keys[a.id]) for a in state.agents.values()),
        key=lambda e: e["key"],
    )

    departments: list[dict[str, Any]] = []
    for department in sorted(state.departments.values(), key=lambda d: d.name):
        entry: dict[str, Any] = {"name": department.name}
        if department.description is not None:
            entry["description"] = department.description
        departments.append(entry)

    # A managed item keeps the secret names it was applied with; generated
    # names for the others must not clash with them.
    remembered: dict[tuple[str, uuid.UUID], dict[str, str]] = {
        (kind, r.item_id): dict(r.secret_names or {})
        for (kind, _), r in state.records.items()
        if kind in ("tool", "channel")
    }
    taken_secrets: set[str] = {n for names in remembered.values() for n in names.values()}
    tools = [
        _tool_entry(t, referenced, taken_secrets, warnings, remembered.get(("tool", t.id), {}))
        for t in sorted(state.tools.values(), key=lambda t: t.name)
    ]

    channels = sorted(
        (
            _channel_entry(
                c,
                channel_keys[c.id],
                agent_keys[c.agent_id],
                referenced,
                taken_secrets,
                remembered.get(("channel", c.id), {}),
            )
            for c in state.channels.values()
        ),
        key=lambda e: e["key"],
    )

    for kind, rows in (("department", state.departments), ("tool", state.tools)):
        for (record_kind, key), record in state.records.items():
            row = rows.get(record.item_id)
            if record_kind == kind and row is not None and row.name != key:
                warnings.append(
                    f"{kind} '{row.name}' was renamed by hand (managed as '{key}'); "
                    "apply the manifest to adopt the new name"
                )

    manifest: dict[str, Any] = {"version": 1}
    for section, items in (
        ("agents", agents),
        ("departments", departments),
        ("tools", tools),
        ("channels", channels),
    ):
        if items:
            manifest[section] = items

    present = {s.name for s in await SecretsRepository(session).names(tenant.id)}
    return ExportResult(
        manifest=manifest,
        secrets_needed=sorted(referenced - present),
        warnings=warnings,
    )
