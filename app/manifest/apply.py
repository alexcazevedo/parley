"""Apply a manifest to one tenant: validate everything, plan, then execute.

`apply_manifest` runs in two steps:

1. Plan. Under a per-tenant advisory lock, read the tenant's current rows and
   its `manifest_items`, and compute one `PlannedChange` per file item
   (`create`, `update` with the changed field names, `adopt` of a hand-made
   item with the changed field names, or `unchanged`), plus one `delete` per
   managed item no longer in the file. Every problem found on the way is
   collected (`Problems`), including refused deletions (`check_removals`), and
   if there is any the whole apply fails with `ManifestError` before anything
   is written.
2. Execute (skipped for a dry run). Each create, update and adopt goes to the
   executor of its kind (`_EXECUTORS`); creates and adoptions are recorded in
   `manifest_items`. Deletes run last, channels then agents then tools and
   departments (`delete_item`), and `manifest_items` rows whose item was
   deleted by hand are dropped. Nothing is committed here: the caller commits,
   or rolls back on any error, so the apply is all or nothing.

Safety of deletes: only items with a `manifest_items` row are ever deleted or
changed (an unmanaged item is touched only when a file item adopts it). The
rows to delete are locked `FOR UPDATE` before the in-use checks run, in the
same transaction and under the tenant's advisory lock. Inserting a
conversation (or a channel) takes `FOR KEY SHARE` on the channel and agent it
references, which conflicts with `FOR UPDATE`: a conversation committed before
the lock is counted (each statement reads a fresh snapshot under READ
COMMITTED), and one inserted after it waits for this transaction and then
fails its foreign key. Every delete statement is tenant-scoped.

A new kind adds a `_plan_<kind>` function called from `plan_changes`, an
entry in `_EXECUTORS` (and `_REPOSITORIES` when the generic repository
executor fits), and entries in `DELETE_ORDER` and `_MODELS`. Values a kind
generates (a channel's signing secret) go into the `generated` dict the
executors receive.

The file is declarative: an item's every field is its file value or the
default, so an update writes every changed field, never a partial patch.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agents import agent_values
from app.api.categories import category_values
from app.api.channels import channel_secret
from app.api.channels import endpoint_problem as channel_endpoint_problem
from app.api.tools import decode_auth_config, endpoint_problem, tool_values
from app.manifest.schema import Manifest, ManifestChannel, ManifestTool
from app.models.agent import Agent
from app.models.category import Category
from app.models.channel import Channel, ChannelType
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState
from app.models.manifest_item import ManifestItem
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.repositories.agents import AgentsRepository
from app.repositories.base import BaseRepository
from app.repositories.categories import CategoriesRepository
from app.repositories.channels import ChannelsRepository
from app.repositories.secrets import SecretsRepository
from app.repositories.tools import ToolsRepository
from app.security import secrets as secrets_crypto
from app.tools.builtin import BUILTIN_TOOLS

logger = structlog.get_logger(__name__)

NOT_MANAGED = "already exists, not managed by the manifest"
MANAGED_ELSEWHERE = "already managed by the manifest under another name"
DUPLICATE_KEY = "duplicate key in the file"
DUPLICATE_NAME = "duplicate name in the file"
UNKNOWN_TOOL = "unknown tool: not in the file, not a tool of the tenant and not a built-in"
UNKNOWN_AGENT = "unknown agent: not an agent key in this file"
TYPE_CANNOT_CHANGE = "type cannot change; use a new key"
SECRET_NOT_ALLOWED = "a secret is only allowed on a whatsapp_evolution channel"

_BUILTIN_NAMES = frozenset(spec.name for spec, _ in BUILTIN_TOOLS)
_UNREADABLE = object()  # a stored auth_config that no longer decrypts: always "changed"


YAML_QUOTE_HINT = " (in YAML, quote the value)"


class ManifestError(Exception):
    """The manifest was refused; `problems` is `[{"field", "message"}]`, never values."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__(f"{len(problems)} manifest problem(s)")
        self.problems = problems


class Problems:
    """Collects every problem of one apply, so all are reported at once."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.items.append({"field": path, "message": message})

    def raise_if_any(self) -> None:
        if self.items:
            raise ManifestError(self.items)


@dataclass
class PlannedChange:
    kind: str  # "agent" | "department" | "tool" | "channel"
    key: str  # agent key, or the department/tool name
    action: str  # "create" | "update" | "adopt" | "delete" | "unchanged"
    fields: list[str] = field(default_factory=list)
    # The row to update, adopt or delete; None for a create.
    item_id: uuid.UUID | None = None
    # A `manifest_items` row whose item is gone: a create or adopt repoints it.
    # For a delete: the row to delete with the item.
    record: ManifestItem | None = None
    # Column values to write: all of them for a create, the changed ones for
    # an update. May hold encrypted credentials, so it is never shown.
    values: dict[str, Any] = field(default_factory=dict, repr=False)
    # Secret names the file uses for this item, by field (`token`, `secret`).
    secret_names: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "action": self.action,
            "fields": list(self.fields),
            # `None` for a create in a dry run and for a delete that ran.
            "id": str(self.item_id) if self.item_id is not None else None,
        }


@dataclass
class ApplyResult:
    dry_run: bool
    changes: list[PlannedChange]
    generated: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "changes": [c.as_dict() for c in self.changes],
            "generated": self.generated,
        }


@dataclass
class TenantState:
    """The tenant's rows the manifest can touch, read once under the lock."""

    records: dict[tuple[str, str], ManifestItem]
    agents: dict[uuid.UUID, Agent]
    departments: dict[uuid.UUID, Category]
    departments_by_name: dict[str, Category]
    tools: dict[uuid.UUID, Tool]
    tools_by_name: dict[str, Tool]
    channels: dict[uuid.UUID, Channel]
    # Values of the secrets the file references, by name (never logged or returned).
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    # Unmanaged rows a file item adopts in this apply, by `(kind, key)`.
    adopted: dict[tuple[str, str], uuid.UUID] = field(default_factory=dict)

    def rows(self, kind: str) -> dict[uuid.UUID, Any]:
        rows: dict[str, dict[uuid.UUID, Any]] = {
            "agent": self.agents,
            "department": self.departments,
            "tool": self.tools,
            "channel": self.channels,
        }
        return rows[kind]

    def managed_ids(self, kind: str) -> set[uuid.UUID]:
        """Ids of the rows of `kind` that have a `manifest_items` row."""
        return {r.item_id for (k, _), r in self.records.items() if k == kind}


def unmanaged_items(state: TenantState, kind: str) -> list[Any]:
    """The tenant's rows of `kind` the manifest does not manage (made by hand),
    oldest first. Adoption picks from these; export can list them."""
    managed = state.managed_ids(kind)
    return [row for row in state.rows(kind).values() if row.id not in managed]


def error_path(loc: tuple[int | str, ...]) -> str:
    """`("agents", 0, "model")` -> `"agents[0].model"`."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path


def parse_manifest(data: Any) -> Manifest:
    """Validate the file's structure with the API's own models; every field
    problem at once (`ManifestError`). Messages never carry input values."""
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        problems = [
            {
                "field": error_path(tuple(e["loc"])) or "(root)",
                "message": e["msg"] + (YAML_QUOTE_HINT if e["type"] == "string_type" else ""),
            }
            for e in exc.errors()
        ]
        raise ManifestError(problems) from None


async def lock_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Serialize applies per tenant until the caller's transaction ends."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"manifest:{tenant_id}")))
    )


async def load_records(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[tuple[str, str], ManifestItem]:
    """The tenant's `manifest_items`, by `(kind, key)`."""
    result = await session.execute(
        select(ManifestItem).where(ManifestItem.tenant_id == tenant_id)
    )
    return {(r.kind, r.key): r for r in result.scalars().all()}


async def record_item(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    key: str,
    item_id: uuid.UUID,
    existing: ManifestItem | None = None,
) -> None:
    """Remember that the manifest manages `item_id` as `(kind, key)`."""
    if existing is not None:
        existing.item_id = item_id
    else:
        session.add(ManifestItem(tenant_id=tenant_id, kind=kind, key=key, item_id=item_id))
    await session.flush()


async def load_state(session: AsyncSession, tenant_id: uuid.UUID) -> TenantState:
    agents = await AgentsRepository(session).list(tenant_id)
    departments = await CategoriesRepository(session).list(tenant_id)
    tools = await ToolsRepository(session).list(tenant_id)
    channels = await ChannelsRepository(session).list(tenant_id)  # no cap: every channel
    return TenantState(
        records=await load_records(session, tenant_id),
        agents={a.id: a for a in agents},
        departments={d.id: d for d in departments},
        departments_by_name={d.name: d for d in departments},
        tools={t.id: t for t in tools},
        tools_by_name={t.name: t for t in tools},
        channels={c.id: c for c in channels},
    )


def changed_fields(current: dict[str, Any], desired: dict[str, Any]) -> list[str]:
    """Names of the fields whose stored value differs from the file's, in file order."""
    return [name for name, value in desired.items() if current.get(name, _UNREADABLE) != value]


def agent_desired(item: Any) -> dict[str, Any]:
    values = agent_values(item)
    values.pop("key", None)
    return values


def agent_current(agent: Agent, names: list[str]) -> dict[str, Any]:
    return {name: getattr(agent, name) for name in names}


def department_current(department: Category) -> dict[str, Any]:
    return {"name": department.name, "description": department.description}


def tool_desired(item: Any) -> dict[str, Any]:
    """The tool as the file states it, `auth_config` in clear (compare only)."""
    values: dict[str, Any] = item.model_dump()
    return values


def tool_current(tool: Tool) -> dict[str, Any]:
    try:
        auth_config: Any = decode_auth_config(tool.auth_config_encrypted)
    except Exception:  # undecryptable (key rotated): rewrite it
        auth_config = _UNREADABLE
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "endpoint": tool.endpoint,
        "method": tool.method,
        "auth_config": auth_config,
        "timeout_seconds": tool.timeout_seconds,
    }


def _check_unique(
    items: list[str], path: str, attr: str, message: str, problems: Problems
) -> None:
    seen: set[str] = set()
    for i, value in enumerate(items):
        if value in seen:
            problems.add(f"{path}[{i}].{attr}", message)
        seen.add(value)


def _plan_departments(
    manifest: Manifest, state: TenantState, problems: Problems
) -> list[PlannedChange]:
    _check_unique(
        [d.name for d in manifest.departments], "departments", "name", DUPLICATE_NAME, problems
    )
    changes: list[PlannedChange] = []
    for i, item in enumerate(manifest.departments):
        path = f"departments[{i}]"
        record = state.records.get(("department", item.name))
        row = state.departments.get(record.item_id) if record else None
        by_name = state.departments_by_name.get(item.name)
        if row is None:
            if by_name is not None:
                if by_name.id in state.managed_ids("department"):
                    problems.add(f"{path}.name", MANAGED_ELSEWHERE)
                    continue
                desired = category_values(item)
                fields = changed_fields(department_current(by_name), desired)
                changes.append(
                    _adopt(
                        "department",
                        item.name,
                        by_name.id,
                        fields,
                        {name: desired[name] for name in fields},
                        record,
                        state,
                    )
                )
                continue
            changes.append(
                PlannedChange(
                    "department", item.name, "create", record=record, values=category_values(item)
                )
            )
            continue
        desired = category_values(item)
        fields = changed_fields(department_current(row), desired)
        if "name" in fields and by_name is not None and by_name.id != row.id:
            problems.add(f"{path}.name", NOT_MANAGED)
            continue
        values = {name: desired[name] for name in fields}
        changes.append(_update_or_unchanged("department", item.name, row.id, fields, values))
    return changes


def secret_references(manifest: Manifest) -> list[tuple[str, str]]:
    """`(path, secret name)` of every secret reference in the file."""
    refs: list[tuple[str, str]] = []
    for i, tool in enumerate(manifest.tools):
        if tool.auth_config and "token" in tool.auth_config:
            refs.append((f"tools[{i}].auth_config.token", tool.auth_config["token"]["secret"]))
    for i, channel in enumerate(manifest.channels):
        if channel.secret is not None:
            refs.append((f"channels[{i}].secret", channel.secret.secret))
    return refs


def _check_secrets_exist(manifest: Manifest, state: TenantState, problems: Problems) -> None:
    for path, name in secret_references(manifest):
        if name not in state.secrets:
            problems.add(path, f"secret '{name}' is not set")


def _resolved_tool(item: ManifestTool, secrets: dict[str, str]) -> ManifestTool | None:
    """The tool with its token reference replaced by the secret's value (for
    comparing and encrypting only); None when the secret is not set."""
    auth = item.auth_config
    if not auth or "token" not in auth:
        return item
    value = secrets.get(auth["token"]["secret"])
    if value is None:
        return None
    return item.model_copy(update={"auth_config": {**auth, "token": value}})


def _tool_writes(item: ManifestTool, fields: list[str]) -> dict[str, Any]:
    """Column values for the changed `fields` of a tool (`item` resolved)."""
    desired = tool_desired(item)
    values = {name: desired[name] for name in fields if name != "auth_config"}
    if "auth_config" in fields:
        values["auth_config_encrypted"] = tool_values(item)["auth_config_encrypted"]
    return values


def _plan_tools(
    manifest: Manifest, state: TenantState, problems: Problems
) -> list[PlannedChange]:
    _check_unique([t.name for t in manifest.tools], "tools", "name", DUPLICATE_NAME, problems)
    changes: list[PlannedChange] = []
    for i, item in enumerate(manifest.tools):
        path = f"tools[{i}]"
        problem = endpoint_problem(item.endpoint)
        if problem is not None:
            problems.add(f"{path}.endpoint", problem)
        resolved = _resolved_tool(item, state.secrets)
        if resolved is None:  # a missing secret is already a problem
            continue
        item = resolved
        record = state.records.get(("tool", item.name))
        row = state.tools.get(record.item_id) if record else None
        by_name = state.tools_by_name.get(item.name)
        if row is None:
            if by_name is not None:
                if by_name.id in state.managed_ids("tool"):
                    problems.add(f"{path}.name", MANAGED_ELSEWHERE)
                    continue
                fields = changed_fields(tool_current(by_name), tool_desired(item))
                writes = _tool_writes(item, fields)
                changes.append(
                    _adopt("tool", item.name, by_name.id, fields, writes, record, state)
                )
                continue
            changes.append(
                PlannedChange("tool", item.name, "create", record=record, values=tool_values(item))
            )
            continue
        fields = changed_fields(tool_current(row), tool_desired(item))
        if "name" in fields and by_name is not None and by_name.id != row.id:
            problems.add(f"{path}.name", NOT_MANAGED)
            continue
        changes.append(
            _update_or_unchanged("tool", item.name, row.id, fields, _tool_writes(item, fields))
        )
    return changes


def _plan_agents(
    manifest: Manifest, state: TenantState, problems: Problems
) -> list[PlannedChange]:
    _check_unique([a.key for a in manifest.agents], "agents", "key", DUPLICATE_KEY, problems)
    known_tools = (
        {t.name for t in manifest.tools} | set(state.tools_by_name) | set(_BUILTIN_NAMES)
    )
    unmanaged: dict[str, list[Agent]] = {}
    for agent in unmanaged_items(state, "agent"):
        unmanaged.setdefault(agent.name, []).append(agent)
    adopting: set[str] = set()
    changes: list[PlannedChange] = []
    for i, item in enumerate(manifest.agents):
        path = f"agents[{i}]"
        for j, name in enumerate(item.tool_names):
            if name not in known_tools:
                problems.add(f"{path}.tool_names[{j}]", UNKNOWN_TOOL)
        record = state.records.get(("agent", item.key))
        row = state.agents.get(record.item_id) if record else None
        desired = agent_desired(item)
        if row is None:
            matches = unmanaged.get(item.name, [])
            if len(matches) > 1:
                problems.add(f"{path}.name", too_many_named("agents", len(matches), item.name))
                continue
            if matches:
                if item.name in adopting:
                    problems.add(f"{path}.name", adopted_twice("agent", item.name))
                    continue
                adopting.add(item.name)
                [match] = matches
                fields = changed_fields(agent_current(match, list(desired)), desired)
                writes = {name: desired[name] for name in fields}
                changes.append(_adopt("agent", item.key, match.id, fields, writes, record, state))
                continue
            changes.append(
                PlannedChange("agent", item.key, "create", record=record, values=desired)
            )
            continue
        fields = changed_fields(agent_current(row, list(desired)), desired)
        values = {name: desired[name] for name in fields}
        changes.append(_update_or_unchanged("agent", item.key, row.id, fields, values))
    return changes


def channel_desired(item: ManifestChannel, secret: str | None) -> dict[str, Any]:
    """The channel as the file states it (the secret in clear, compare only).
    An `external` channel's secret is generated, so it is not compared."""
    return {
        "name": item.name,
        "agent": item.agent,
        "config": item.config,
        "secret": secret if item.type == ChannelType.WHATSAPP_EVOLUTION else None,
    }


def channel_current(channel: Channel, agent_keys: dict[uuid.UUID, str]) -> dict[str, Any]:
    secret: Any = None
    if channel.type == ChannelType.WHATSAPP_EVOLUTION and channel.secret_encrypted:
        try:
            secret = secrets_crypto.decrypt(channel.secret_encrypted)
        except Exception:  # undecryptable (key rotated): rewrite it
            secret = _UNREADABLE
    return {
        "name": channel.name,
        "agent": agent_keys.get(channel.agent_id),  # None: not a manifest agent
        "config": channel.config,
        "secret": secret,
    }


def _plan_channels(
    manifest: Manifest, state: TenantState, problems: Problems
) -> list[PlannedChange]:
    _check_unique([c.key for c in manifest.channels], "channels", "key", DUPLICATE_KEY, problems)
    agent_files = {a.key for a in manifest.agents}
    agent_keys = {r.item_id: key for (kind, key), r in state.records.items() if kind == "agent"}
    agent_keys.update(
        {item_id: key for (kind, key), item_id in state.adopted.items() if kind == "agent"}
    )
    unmanaged: dict[tuple[str, ChannelType], list[Channel]] = {}
    for channel in unmanaged_items(state, "channel"):
        unmanaged.setdefault((channel.name, channel.type), []).append(channel)
    adopting: set[tuple[str, ChannelType]] = set()
    changes: list[PlannedChange] = []
    for i, item in enumerate(manifest.channels):
        path = f"channels[{i}]"
        ok = True
        if item.agent not in agent_files:
            problems.add(f"{path}.agent", UNKNOWN_AGENT)
            ok = False
        problem = channel_endpoint_problem(item.type, item.config)
        if problem is not None:
            problems.add(f"{path}.config", problem)
            ok = False
        if item.secret is not None and item.type != ChannelType.WHATSAPP_EVOLUTION:
            problems.add(f"{path}.secret", SECRET_NOT_ALLOWED)
            ok = False
        secret = state.secrets.get(item.secret.secret) if item.secret is not None else None
        if item.secret is not None and secret is None:
            ok = False  # a missing secret is already a problem
        record = state.records.get(("channel", item.key))
        row = state.channels.get(record.item_id) if record else None
        if row is not None and row.type != item.type:
            problems.add(f"{path}.type", TYPE_CANNOT_CHANGE)
            ok = False
        if not ok:
            continue
        desired = channel_desired(item, secret)
        if row is None:
            matches = unmanaged.get((item.name, item.type), [])
            if len(matches) > 1:
                problems.add(f"{path}.name", too_many_named("channels", len(matches), item.name))
                continue
            if matches:
                if (item.name, item.type) in adopting:
                    problems.add(f"{path}.name", adopted_twice("channel", item.name))
                    continue
                adopting.add((item.name, item.type))
                [match] = matches
                fields = changed_fields(channel_current(match, agent_keys), desired)
                writes = {name: desired[name] for name in fields}
                changes.append(
                    _adopt("channel", item.key, match.id, fields, writes, record, state)
                )
                continue
            values: dict[str, Any] = {
                "type": item.type,
                "name": item.name,
                "agent": item.agent,
                "config": item.config,
                "secret": secret,
            }
            changes.append(
                PlannedChange("channel", item.key, "create", record=record, values=values)
            )
            continue
        fields = changed_fields(channel_current(row, agent_keys), desired)
        values = {name: desired[name] for name in fields}
        changes.append(_update_or_unchanged("channel", item.key, row.id, fields, values))
    return changes


def too_many_named(plural: str, count: int, name: str) -> str:
    return f"{count} {plural} are named '{name}'; rename one or delete it"


def adopted_twice(kind: str, name: str) -> str:
    return f"another {kind} in the file also adopts the {kind} named '{name}'; rename one"


def _adopt(
    kind: str,
    key: str,
    item_id: uuid.UUID,
    fields: list[str],
    writes: dict[str, Any],
    record: ManifestItem | None,
    state: TenantState,
) -> PlannedChange:
    """Take over an unmanaged row: record it and write `writes` (the changed
    fields' column values)."""
    state.adopted[(kind, key)] = item_id
    return PlannedChange(
        kind, key, "adopt", fields=fields, item_id=item_id, record=record, values=writes
    )


def _update_or_unchanged(
    kind: str, key: str, item_id: uuid.UUID, fields: list[str], values: dict[str, Any]
) -> PlannedChange:
    if not fields:
        return PlannedChange(kind, key, "unchanged", item_id=item_id)
    return PlannedChange(kind, key, "update", fields=fields, item_id=item_id, values=values)


def plan_changes(
    manifest: Manifest, state: TenantState, problems: Problems
) -> list[PlannedChange]:
    """Every change the file asks for, in execution order (agents after tools,
    channels last: they name agents; deletes after everything else)."""
    _check_secrets_exist(manifest, state, problems)
    return [
        *_plan_departments(manifest, state, problems),
        *_plan_tools(manifest, state, problems),
        *_plan_agents(manifest, state, problems),
        *_plan_channels(manifest, state, problems),
        *_plan_removals(manifest, state),
    ]


# Deletes run in this order so that foreign keys hold (a channel names an
# agent; tools and departments are named by nothing with a foreign key).
DELETE_ORDER = ("channel", "agent", "tool", "department")
_PLURALS = {"channel": "channels", "agent": "agents", "tool": "tools", "department": "departments"}
_MODELS: dict[str, Any] = {
    "channel": Channel,
    "agent": Agent,
    "tool": Tool,
    "department": Category,
}


def _file_keys(manifest: Manifest) -> set[tuple[str, str]]:
    return {
        *(("agent", a.key) for a in manifest.agents),
        *(("department", d.name) for d in manifest.departments),
        *(("tool", t.name) for t in manifest.tools),
        *(("channel", c.key) for c in manifest.channels),
    }


def _plan_removals(manifest: Manifest, state: TenantState) -> list[PlannedChange]:
    """One `delete` per managed item that is no longer in the file and still
    exists, in `DELETE_ORDER`. Unmanaged items are never listed."""
    in_file = _file_keys(manifest)
    removals: list[PlannedChange] = []
    for (kind, key), record in state.records.items():
        if (kind, key) in in_file:
            continue
        row = state.rows(kind).get(record.item_id)
        if row is None:  # deleted by hand: `stale_records` drops the record
            continue
        removals.append(PlannedChange(kind, key, "delete", item_id=row.id, record=record))
    removals.sort(key=lambda c: (DELETE_ORDER.index(c.kind), c.key))
    return removals


def stale_records(manifest: Manifest, state: TenantState) -> list[ManifestItem]:
    """`manifest_items` rows whose item was deleted by hand and that the file
    no longer names (one the file still names is repointed by its create)."""
    in_file = _file_keys(manifest)
    return [
        record
        for (kind, key), record in state.records.items()
        if (kind, key) not in in_file and record.item_id not in state.rows(kind)
    ]


def _conversations_message(kind: str, key: str, count: int) -> str:
    if count == 1:
        return f"{kind} '{key}' has 1 conversation; deleting it would erase it"
    return f"{kind} '{key}' has {count} conversations; deleting it would erase them"


def _contacts_message(key: str, count: int) -> str:
    if count == 1:
        return f"channel '{key}' has 1 contact; deleting it would erase it and its memories"
    return f"channel '{key}' has {count} contacts; deleting it would erase them and their memories"


async def _count_by(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    column: Any,
    ids: list[uuid.UUID],
    *where: Any,
    model: Any = Conversation,
) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(column, func.count())
        .where(model.tenant_id == tenant_id, column.in_(ids), *where)
        .group_by(column)
    )
    return {item_id: count for item_id, count in rows.tuples().all()}


async def check_removals(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    manifest: Manifest,
    state: TenantState,
    changes: list[PlannedChange],
    problems: Problems,
) -> None:
    """Refuse every delete that would erase history or leave a dangling name.

    The rows to delete are locked `FOR UPDATE` first (see the module
    docstring), then counted with fresh queries, so nothing created through
    the API before the lock is missed and nothing can reference them after it.
    Runs for a dry run too, so the dry run refuses what the apply would.
    """
    deletes = [c for c in changes if c.action == "delete"]
    if not deletes:
        return
    ids: dict[str, list[uuid.UUID]] = {kind: [] for kind in DELETE_ORDER}
    for change in deletes:
        assert change.item_id is not None
        ids[change.kind].append(change.item_id)
    for kind in DELETE_ORDER:
        model = _MODELS[kind]
        if ids[kind]:
            await session.execute(
                select(model.id)
                .where(model.tenant_id == tenant_id, model.id.in_(ids[kind]))
                .order_by(model.id)
                .with_for_update()
            )

    by_channel = await _count_by(session, tenant_id, Conversation.channel_id, ids["channel"])
    # Deleting a channel cascades to its contacts (and their memories) and to
    # its conversations (messages, runs, handoffs, events: all reached through
    # a conversation, which needs a contact). An agent delete cascades to its
    # channels (checked one by one) and its conversations.
    contacts_by_channel = await _count_by(
        session, tenant_id, Contact.channel_id, ids["channel"], model=Contact
    )
    by_agent = await _count_by(session, tenant_id, Conversation.agent_id, ids["agent"])
    by_department = await _count_by(
        session,
        tenant_id,
        Conversation.category_id,
        ids["department"],
        Conversation.state != ConversationState.CLOSED,
    )
    # After this apply, every channel the file plans points at a file agent,
    # so only the channels outside the file (and not deleted) keep their agent.
    planned = {c.item_id for c in changes if c.item_id is not None}
    channels_left = (
        await session.execute(
            select(Channel.agent_id, Channel.name).where(
                Channel.tenant_id == tenant_id,
                Channel.agent_id.in_(ids["agent"]),
                Channel.id.not_in(planned),
            )
        )
    ).tuples().all()
    # Agents left after this apply: the file's (their file `tool_names`) and the
    # tenant's others that are not deleted (their stored `tool_names`).
    agents_left: list[tuple[str, list[str]]] = [
        (f"agent '{a.key}'", list(a.tool_names)) for a in manifest.agents
    ]
    stored_agents = (
        await session.execute(
            select(Agent.name, Agent.tool_names).where(
                Agent.tenant_id == tenant_id, Agent.id.not_in(planned)
            )
        )
    ).tuples().all()
    agents_left += [(f"agent '{name}'", list(names)) for name, names in stored_agents]

    for change in deletes:
        path, key, item_id = _PLURALS[change.kind], change.key, change.item_id
        assert item_id is not None
        if change.kind == "channel" and by_channel.get(item_id):
            problems.add(path, _conversations_message("channel", key, by_channel[item_id]))
        elif change.kind == "channel" and contacts_by_channel.get(item_id):
            problems.add(path, _contacts_message(key, contacts_by_channel[item_id]))
        elif change.kind == "agent":
            if by_agent.get(item_id):
                problems.add(path, _conversations_message("agent", key, by_agent[item_id]))
            for agent_id, channel_name in channels_left:
                if agent_id == item_id:
                    problems.add(
                        path,
                        f"agent '{key}' is the agent of channel '{channel_name}'; "
                        "delete the channel or bind it to another agent first",
                    )
        elif change.kind == "tool":
            for label, tool_names in agents_left:
                if key in tool_names:
                    problems.add(
                        path, f"tool '{key}' is used by {label}; remove it from tool_names first"
                    )
        elif change.kind == "department" and by_department.get(item_id):
            count = by_department[item_id]
            noun = "conversation" if count == 1 else "conversations"
            problems.add(
                path,
                f"department '{key}' has {count} open {noun}; "
                "close them or move them to another department first",
            )


async def delete_item(session: AsyncSession, tenant_id: uuid.UUID, change: PlannedChange) -> None:
    """Delete one managed item and its `manifest_items` row (both tenant-scoped).
    Its conversations are cascaded or unlinked by the database; the checks
    made sure an agent or channel has none."""
    assert change.item_id is not None and change.record is not None
    model = _MODELS[change.kind]
    await session.execute(
        delete(model)
        .where(model.tenant_id == tenant_id, model.id == change.item_id)
        .execution_options(synchronize_session=False)
    )
    await _delete_record(session, tenant_id, change.record)


async def _delete_record(session: AsyncSession, tenant_id: uuid.UUID, record: ManifestItem) -> None:
    await session.execute(
        delete(ManifestItem)
        .where(ManifestItem.tenant_id == tenant_id, ManifestItem.id == record.id)
        .execution_options(synchronize_session=False)
    )


Executor = Callable[[AsyncSession, uuid.UUID, PlannedChange, dict[str, Any]], Awaitable[None]]

_REPOSITORIES: dict[str, type[BaseRepository[Any]]] = {
    "agent": AgentsRepository,
    "department": CategoriesRepository,
    "tool": ToolsRepository,
}


async def execute_with_repository(
    session: AsyncSession, tenant_id: uuid.UUID, change: PlannedChange, generated: dict[str, Any]
) -> None:
    """Create, update or adopt one row through its kind's repository."""
    repo = _REPOSITORIES[change.kind](session)
    if change.action == "create":
        row = await repo.create(tenant_id, **change.values)
        change.item_id = row.id
        await record_item(session, tenant_id, change.kind, change.key, row.id, change.record)
    elif change.action in ("update", "adopt"):
        assert change.item_id is not None
        await repo.update(tenant_id, change.item_id, **change.values)
        if change.action == "adopt":
            await record_item(
                session, tenant_id, change.kind, change.key, change.item_id, change.record
            )


async def execute_channel(
    session: AsyncSession, tenant_id: uuid.UUID, change: PlannedChange, generated: dict[str, Any]
) -> None:
    """Create, update or adopt a channel; the file's agent key becomes the
    agent's id, and a new `external` channel's signing secret goes into
    `generated` (an adopted one keeps its secret)."""
    repo = ChannelsRepository(session)
    values = dict(change.values)
    if "agent" in values:
        agent_id = await session.scalar(
            select(ManifestItem.item_id).where(
                ManifestItem.tenant_id == tenant_id,
                ManifestItem.kind == "agent",
                ManifestItem.key == values.pop("agent"),
            )
        )
        assert agent_id is not None  # the plan checked the key; agents run first
        values["agent_id"] = agent_id
    if "secret" in values:
        secret = values.pop("secret")
        values["secret_encrypted"] = secrets_crypto.encrypt(secret) if secret is not None else None
    if change.action == "create":
        channel_type = values["type"]
        encrypted, events_secret = channel_secret(channel_type, None)
        if channel_type == ChannelType.EXTERNAL:
            values["secret_encrypted"] = encrypted
            generated.setdefault("channels", {})[change.key] = {"events_secret": events_secret}
        row = await repo.create(tenant_id, **values)
        change.item_id = row.id
        await record_item(session, tenant_id, "channel", change.key, row.id, change.record)
    elif change.action in ("update", "adopt"):
        assert change.item_id is not None
        await repo.update(tenant_id, change.item_id, **values)
        if change.action == "adopt":
            await record_item(
                session, tenant_id, "channel", change.key, change.item_id, change.record
            )


_EXECUTORS: dict[str, Executor] = {
    "channel": execute_channel,
    "agent": execute_with_repository,
    "department": execute_with_repository,
    "tool": execute_with_repository,
}


def _attach_secret_names(manifest: Manifest, changes: list[PlannedChange]) -> None:
    """Set each tool and channel change's `secret_names` from the file."""
    tools = {t.name: t for t in manifest.tools}
    channels = {c.key: c for c in manifest.channels}
    for change in changes:
        if change.action == "delete":
            continue
        if change.kind == "tool":
            auth = tools[change.key].auth_config
            if auth and "token" in auth:
                change.secret_names = {"token": auth["token"]["secret"]}
        elif change.kind == "channel":
            secret = channels[change.key].secret
            if secret is not None:
                change.secret_names = {"secret": secret.secret}


async def _remember_secret_names(
    session: AsyncSession, tenant_id: uuid.UUID, changes: list[PlannedChange]
) -> None:
    """Store the secret names each tool and channel was applied with (on create,
    update, adopt, and when only the name changed)."""
    records = await load_records(session, tenant_id)
    for change in changes:
        if change.kind not in ("tool", "channel") or change.action == "delete":
            continue
        record = records.get((change.kind, change.key))
        if record is not None and record.secret_names != change.secret_names:
            record.secret_names = dict(change.secret_names)
    await session.flush()


async def apply_manifest(
    session: AsyncSession, tenant: Tenant, manifest: Manifest, *, dry_run: bool
) -> ApplyResult:
    """Plan the file against the tenant and, unless `dry_run`, execute it.

    Raises `ManifestError` with every problem before writing anything. Only
    flushes: the caller commits on success and rolls back otherwise (also
    after a dry run, which releases the advisory lock).
    """
    await lock_tenant(session, tenant.id)
    state = await load_state(session, tenant.id)
    names = {name for _, name in secret_references(manifest)}
    state.secrets = await SecretsRepository(session).resolve(tenant.id, names)
    problems = Problems()
    changes = plan_changes(manifest, state, problems)
    await check_removals(session, tenant.id, manifest, state, changes, problems)
    problems.raise_if_any()
    _attach_secret_names(manifest, changes)

    result = ApplyResult(dry_run=dry_run, changes=changes)
    if not dry_run:
        for change in changes:
            if change.action not in ("unchanged", "delete"):
                await _EXECUTORS[change.kind](session, tenant.id, change, result.generated)
        for change in changes:  # already in DELETE_ORDER, after every other write
            if change.action == "delete":
                await delete_item(session, tenant.id, change)
                change.item_id = None  # the item is gone: no id to report
        for record in stale_records(manifest, state):
            await _delete_record(session, tenant.id, record)
        await _remember_secret_names(session, tenant.id, changes)
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.action] = counts.get(change.action, 0) + 1
    logger.info("manifest_applied", tenant_id=str(tenant.id), dry_run=dry_run, **counts)
    return result
