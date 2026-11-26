"""The tool ledger: every tool call is on record before it runs.

`begin` inserts the call's `ToolExecution` with status `STARTED` and commits
it, so the record exists, and survives, whatever happens next: a turn that
fails or is superseded afterwards, a cancellation, a worker that dies mid-call.
`finish` completes the row with what the call returned and commits again —
together with anything the tool itself wrote on the same session (a built-in's
`Memory` or `Handoff`), so a built-in's effect and its result are saved as one.

A row left `STARTED` therefore means the outcome is unknown. Callers do not
catch `BaseException`s (cancellation) to finish it: that is the point. A call
that failed after its request may have reached the endpoint (a timeout other
than a connect timeout, say) is not finished either: `mark_unknown` records the
error and latency and leaves it `STARTED`.

When a turn runs again, `find_prior` looks up the same call (tool name and
arguments) made by an earlier attempt of the same batch, and `replay` turns a
finished row back into the `ToolResult` the model received then. "Earlier
attempt" is defined by the batch: another run in the same tenant and
conversation whose `input_message_ids` is a non-empty subset of this run's
(`ctx.message_ids`). A retry has the same ids, a turn merged with newer
messages a superset, a later turn none of them.
"""

import json
import uuid
from typing import Any

from sqlalchemy import func, select

from app.models.agent_run import AgentRun
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.tools.base import ToolContext, ToolResult

# What the model is told about a call an earlier attempt started but never
# finished, when repeating it is not safe (any HTTP method but GET).
OUTCOME_UNKNOWN_ERROR = (
    "outcome unknown: an earlier attempt of this call was interrupted before it "
    "returned, so it may or may not have taken effect. It was not repeated; do "
    "not assume it succeeded or failed."
)

_FINISHED = (ToolExecutionStatus.OK, ToolExecutionStatus.ERROR)


async def begin(
    ctx: ToolContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_id: uuid.UUID | None = None,
) -> ToolExecution:
    """Insert and commit the call's row (`STARTED`) before the tool runs.

    The commit also commits whatever the turn's session had pending.
    """
    execution = ToolExecution(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        agent_run_id=ctx.agent_run_id,
        tool_id=tool_id,
        tool_name=tool_name,
        arguments=_jsonable(arguments) or {},
        status=ToolExecutionStatus.STARTED,
    )
    ctx.session.add(execution)
    await ctx.session.commit()
    return execution


async def finish(
    ctx: ToolContext, execution: ToolExecution, result: ToolResult, *, latency_ms: int
) -> None:
    """Complete the row with `result` and commit it (and the tool's own writes)."""
    execution.status = ToolExecutionStatus.OK if result.ok else ToolExecutionStatus.ERROR
    execution.result = _jsonable(result.data) if result.ok else None
    execution.error = None if result.ok else (result.error or "tool execution failed")
    execution.latency_ms = latency_ms
    execution.model_content = result.to_content()
    # A terminal result (`handoff_to_human`) ended the loop with this text;
    # `replay` restores it so a reused call ends the loop the same way.
    execution.terminal_text = (
        result.meta.get("final_text") if result.meta.get("terminal") else None
    )
    await ctx.session.commit()


async def mark_unknown(
    ctx: ToolContext, execution: ToolExecution, *, error: str, latency_ms: int
) -> None:
    """Record a failure whose outcome is unknown and commit; status stays `STARTED`.

    For a call whose request may have reached the endpoint (it timed out after
    connecting, say): on a re-run it counts as unknown outcome, not as a saved
    error. `model_content` stays null.
    """
    execution.status = ToolExecutionStatus.STARTED
    execution.error = error
    execution.latency_ms = latency_ms
    await ctx.session.commit()


async def find_prior(
    ctx: ToolContext, tool_name: str, arguments: dict[str, Any]
) -> ToolExecution | None:
    """The same call made by an earlier attempt of this turn, if any.

    Same tool name and equal (stored-form) arguments, on a run other than
    `ctx.agent_run_id` in the same tenant and conversation whose
    `input_message_ids` is a non-empty subset of `ctx.message_ids`. The newest
    finished row (`OK`/`ERROR`) wins; otherwise the newest `STARTED` one.
    `None` when `ctx.message_ids` is empty.
    """
    if not ctx.message_ids:
        return None
    current_ids = sorted({str(message_id) for message_id in ctx.message_ids})
    stored_arguments = _jsonable(arguments or {}) or {}
    result = await ctx.session.execute(
        select(ToolExecution)
        .join(AgentRun, AgentRun.id == ToolExecution.agent_run_id)
        .where(
            ToolExecution.tenant_id == ctx.tenant_id,
            ToolExecution.tool_name == tool_name,
            ToolExecution.arguments == stored_arguments,
            AgentRun.tenant_id == ctx.tenant_id,
            AgentRun.conversation_id == ctx.conversation_id,
            AgentRun.id != ctx.agent_run_id,
            AgentRun.input_message_ids.is_not(None),
            func.jsonb_typeof(AgentRun.input_message_ids) == "array",
            func.jsonb_array_length(AgentRun.input_message_ids) > 0,
            AgentRun.input_message_ids.contained_by(current_ids),
        )
        .order_by(ToolExecution.created_at.desc(), ToolExecution.id.desc())
    )
    rows = list(result.scalars().all())
    for row in rows:
        if row.status in _FINISHED:
            return row
    return rows[0] if rows else None


def replay(execution: ToolExecution) -> ToolResult:
    """The `ToolResult` a finished row's call gave the model, `meta["reused"] = True`.

    Built from `model_content`, the exact string the model received, so its
    `to_content()` reproduces it. A row with `terminal_text` also gets
    `terminal=True` and `final_text`, so the reused call ends the loop.
    """
    meta: dict[str, Any] = {"reused": True}
    if execution.terminal_text is not None:
        meta |= {"terminal": True, "final_text": execution.terminal_text}
    payload: Any = None
    if execution.model_content is not None:
        try:
            payload = json.loads(execution.model_content)
        except ValueError:
            payload = None
    if isinstance(payload, dict) and "ok" in payload:
        if payload["ok"]:
            return ToolResult(ok=True, data=payload.get("data"), meta=meta)
        return ToolResult(
            ok=False,
            error=payload.get("error") or "tool execution failed",
            meta=meta,
        )
    # No readable `model_content` (rows written before it existed).
    if execution.status == ToolExecutionStatus.OK:
        return ToolResult(ok=True, data=execution.result, meta=meta)
    return ToolResult(ok=False, error=execution.error or "tool execution failed", meta=meta)


def _jsonable(value: Any) -> dict[str, Any] | None:
    """`ToolExecution.arguments/result` are JSONB objects; wrap non-objects."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"data": value}
