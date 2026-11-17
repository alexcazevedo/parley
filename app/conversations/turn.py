"""Turn execution: one debounced batch of inbound messages -> one reply.

`process_turn` is deliberately runtime-agnostic: it joins the batch into a
single `input_text`, hands it to a runtime, then persists the answer as an
outbound assistant message and pushes it through the channel adapter.

One thing runs before the runtime: for agents with
`sentiment_escalation_enabled`, the frustration classifier
(`app/agents/sentiment.py`) scores the batch, and a score at or above
`settings.sentiment_threshold` short-circuits the turn into a warm handoff
(`requested_by=system`) whose reply is the agent's `handoff_message`.

Two runtime shapes are accepted. `TurnRuntime` is text-in/text-out and needs
nothing from the database (`EchoRuntime`, test doubles).
`SessionAwareTurnRuntime` additionally receives the turn's session, because it
writes its own rows — `app.agents.runtime.AgentRuntime` persists an `AgentRun`
plus the tool-call/tool-result messages and must land in the same transaction
as the reply. The worker injects `AgentRuntime`; `EchoRuntime` stays the
default so nothing accidentally calls a live model.

A batch made entirely of `unsupported` media (video, documents, stickers)
never reaches a runtime: there is nothing to reason about, so the turn
delivers `UNSUPPORTED_MEDIA_REPLY` directly. Mixed batches run normally.

Barge-in (PRODUCT.md): callers may inject a `supersede_check` — "did a newer
inbound message arrive for this conversation?". It is consulted at exactly two
safe checkpoints, never mid-LLM/mid-tool: once after the runtime returns and
before delivery starts, and again between humanizer bubbles. A hit drops the
unsent parts, marks the turn's `AgentRun` superseded and returns `Superseded`
instead of a reply; already-sent bubbles stand. No check (or a failing one)
means today's behavior, byte for byte.

Lock ownership (PRODUCT.md E3.5): the scheduler also injects an
`ownership_check` — "is the conversation lock still ours?". It is asked right
before every outbound send (read receipt, each bubble, the single reply) and
right before every commit but one: the commit that stores a reply right after
it was sent. A `False`, or a check that raises, means another worker may own
this conversation now: the turn rolls back what it has not committed, sends
nothing more and returns `LockLost`. Bubbles already sent stand (and stay
committed). No check means today's behavior.

A delivered reply is committed right after its send (per bubble when
humanized), before the rolling summary, a model call. Whatever happens to the
summary afterwards — it raises, its commit fails, the lock is lost before its
commit — only the summary is dropped: the turn still returns the reply.

Failures (PRODUCT.md E3.2): any exception inside a turn rolls back what was
not committed and surfaces as `TurnFailed(delivered_parts)`, chained to the
original. A failed send of any bubble stops the reply there; nothing after it
is sent. The scheduler retries a turn that delivered nothing and dead-letters
one that delivered something. `send_failure_notice` is the one error message
a customer gets when a batch is dead-lettered.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.context import render_message_text
from app.agents.loading import load_conversation_agent
from app.agents.sentiment import score_frustration
from app.channels.base import ChannelAdapter, OutgoingMessage
from app.channels.registry import get_adapter
from app.config import get_settings
from app.conversations.humanize import split_reply, typing_delay_seconds
from app.conversations.summary import maybe_summarize
from app.handoff.summarizer import handoff_with_summary
from app.llm.factory import get_provider
from app.media.prepare import prepare_media_messages
from app.models.agent import Agent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import HandoffRequestedBy
from app.models.message import Message, MessageDirection, MessageRole
from app.tools.ssrf import SSRFError

logger = structlog.get_logger(__name__)

# What the customer hears when the whole batch is media we cannot handle
# (video, documents, stickers): a fixed reply, no model call — there is
# nothing for the agent to reason about, and PRODUCT.md forbids silence.
UNSUPPORTED_MEDIA_REPLY = (
    "Por enquanto só consigo entender texto, áudios e imagens. Pode me mandar em texto?"
)


# What the customer hears once when a batch is dead-lettered after its last
# failed attempt (PRODUCT.md E3.2). Never after a partly delivered reply.
TURN_FAILED_MESSAGE = "Desculpe, tive um problema técnico. Pode tentar novamente?"


class TurnFailed(Exception):
    """A turn raised; everything it had not committed was rolled back.

    Always raised `from` the original exception (`__cause__`).
    `delivered_parts` counts the outbound sends that succeeded before the
    failure (reply bubbles, the single reply, a handoff or unsupported-media
    reply); read receipts and typing indicators are not counted. Above zero,
    the customer already saw part of this turn and a retry would repeat it.
    """

    def __init__(self, delivered_parts: int) -> None:
        super().__init__(f"turn failed after {delivered_parts} delivered part(s)")
        self.delivered_parts = delivered_parts


@runtime_checkable
class TurnRuntime(Protocol):
    """What a turn needs from an agent implementation: text in, text out."""

    async def run(self, conversation: Conversation, input_text: str) -> str: ...


@runtime_checkable
class SessionAwareTurnRuntime(Protocol):
    """A runtime that persists rows of its own and so needs the turn's session."""

    async def run_turn(
        self, session: AsyncSession, conversation: Conversation, input_text: str
    ) -> str: ...


AnyTurnRuntime = TurnRuntime | SessionAwareTurnRuntime


class EchoRuntime:
    """Default runtime until the LLM runtime lands — echoes the input back."""

    async def run(self, conversation: Conversation, input_text: str) -> str:
        return f"echo: {input_text}"


SupersedeCheck = Callable[[], Awaitable[bool]]
"""Probe: has a newer inbound message arrived for this conversation?"""


@dataclass(frozen=True)
class Superseded:
    """Outcome of a turn interrupted at a checkpoint, in place of a reply.

    `delivered_parts` is how many humanizer bubbles had already gone out (and
    stay out) when the turn stopped: 0 at the pre-delivery checkpoint.
    """

    delivered_parts: int


OwnershipCheck = Callable[[], Awaitable[bool]]
"""Probe: does this turn still hold the conversation lock?"""


@dataclass(frozen=True)
class LockLost:
    """Outcome of a turn that found it no longer owns the conversation lock.

    Nothing was committed after the loss was seen and nothing more was sent.
    `delivered_parts` is how many parts had already gone out (and stay out).
    """

    delivered_parts: int


@dataclass
class _Sends:
    """Running count of this turn's successful outbound sends, for `TurnFailed`."""

    delivered: int = 0


@dataclass(frozen=True)
class _Delivery:
    """What `_deliver` did: last persisted reply row, parts sent, cut short, why?"""

    message: Message | None
    parts_sent: int
    superseded: bool
    lock_lost: bool = False


async def process_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    messages: list[Message],
    runtime: AnyTurnRuntime | None = None,
    *,
    supersede_check: SupersedeCheck | None = None,
    ownership_check: OwnershipCheck | None = None,
) -> Message | Superseded | LockLost | None:
    """Run one turn for `conversation_id` over `messages` (arrival order).

    Returns the persisted assistant message, or `None` when no reply was
    produced (unknown conversation, empty batch, a conversation that is not
    `ACTIVE_AI` — a human is driving it, so the AI stays quiet — or one whose
    agent is missing or belongs to another tenant, or whose channel endpoint
    the SSRF check refuses: the turn is refused, nothing is sent or written).

    Returns `Superseded` instead when `supersede_check` reported a newer
    inbound message at one of the two checkpoints: the reply (or its remaining
    bubbles) is abandoned for the next turn to answer as one batch.

    Returns `LockLost` when `ownership_check` said (or, by raising, could not
    confirm) that the lock is no longer ours, before a send or a commit.

    Raises `TurnFailed` (from the original exception) when anything in the
    turn raised: the session is rolled back first, and `delivered_parts` says
    how many sends had already reached the customer.
    """
    sends = _Sends()
    try:
        return await _run_turn(
            session,
            conversation_id,
            messages,
            runtime,
            sends,
            supersede_check=supersede_check,
            ownership_check=ownership_check,
        )
    except Exception as exc:
        await _rollback_quietly(session, conversation_id)
        logger.warning(
            "turn.failed",
            conversation_id=str(conversation_id),
            delivered_parts=sends.delivered,
            error=_describe(exc),
        )
        raise TurnFailed(sends.delivered) from exc


async def _rollback_quietly(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Roll back after a failure; a rollback that fails too is only logged.

    The original error is the one worth reporting: a broken connection that
    cannot even roll back must not replace it.
    """
    try:
        await session.rollback()
    except Exception:
        logger.warning(
            "turn.rollback_failed", conversation_id=str(conversation_id), exc_info=True
        )


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


async def send_failure_notice(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Tell the customer their batch could not be answered. Never raises.

    Only for an `ACTIVE_AI` conversation: with a human driving it, the AI
    stays quiet even about its own failure. The notice is stored as an
    assistant message and committed *before* the send, so it is on record
    (webchat polls it from there) even when the channel is what keeps failing;
    a send error is logged only. Returns whether the notice was stored.
    """
    try:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.state != ConversationState.ACTIVE_AI:
            return False
        channel = await session.get(Channel, conversation.channel_id)
        contact = await session.get(Contact, conversation.contact_id)
        if channel is None or contact is None:
            return False
        session.add(_build_reply(conversation, TURN_FAILED_MESSAGE))
        conversation.last_message_at = datetime.now(UTC)
        await session.commit()
    except Exception:
        await _rollback_quietly(session, conversation_id)
        logger.exception("turn.failure_notice_not_stored", conversation_id=str(conversation_id))
        return False

    try:
        await get_adapter(channel.type).send_message(
            OutgoingMessage(conversation_id=conversation_id, content=TURN_FAILED_MESSAGE),
            contact,
            channel,
        )
    except Exception:
        logger.warning(
            "turn.failure_notice_send_failed",
            conversation_id=str(conversation_id),
            exc_info=True,
        )
    return True


async def _run_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    messages: list[Message],
    runtime: AnyTurnRuntime | None,
    sends: _Sends,
    *,
    supersede_check: SupersedeCheck | None,
    ownership_check: OwnershipCheck | None,
) -> Message | Superseded | LockLost | None:
    """`process_turn`'s body; `sends` counts what reached the customer."""
    if not messages:
        return None

    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        logger.warning("turn.unknown_conversation", conversation_id=str(conversation_id))
        return None

    if conversation.state != ConversationState.ACTIVE_AI:
        logger.info(
            "turn.skipped_not_active_ai",
            conversation_id=str(conversation_id),
            state=str(conversation.state),
        )
        return None

    # Refusals come first, before any model, tool, speech-to-text or outbound
    # call, so a refused turn costs nothing and writes nothing.
    #
    # Tenant-scoped agent (PRODUCT.md F3): a conversation whose agent is
    # missing or belongs to another tenant (a row left by an older version)
    # gets no read receipt, no reply and no AgentRun, whatever the runtime —
    # EchoRuntime included.
    agent = await load_conversation_agent(session, conversation)
    if agent is None:
        logger.warning("turn.refused_agent_unavailable", conversation_id=str(conversation_id))
        return None

    # Loaded before the runtime runs: `humanize` decides whether the inbound
    # batch gets a read receipt *before* we start "typing".
    channel = await session.get(Channel, conversation.channel_id)
    contact = await session.get(Contact, conversation.contact_id)
    assert channel is not None and contact is not None, "conversation has dangling FKs"

    adapter = get_adapter(channel.type)
    # Channel endpoint (PRODUCT.md F1): a stored `base_url` the SSRF check
    # refuses would only fail at `send_message`, after the model and tools ran,
    # and the exception would hold up the rest of the scheduler tick. Ask up
    # front and return quietly instead.
    if not await _channel_endpoint_ok(adapter, channel, conversation_id):
        return None

    # Voice notes are transcribed before anything else touches the batch, so
    # the read receipt, sentiment scoring and the joined `input_text` all see
    # the transcript rather than an empty audio message.
    await prepare_media_messages(session, messages)

    humanized = bool((channel.config or {}).get("humanize"))

    if humanized:
        if await _lock_lost(ownership_check):
            return await _give_up(session, conversation_id, delivered_parts=0)
        await _mark_read(adapter, messages, contact, channel)

    if all(message.type == "unsupported" for message in messages):
        return await _reply_unsupported_media(
            session,
            conversation,
            adapter,
            contact,
            channel,
            humanized,
            len(messages),
            ownership_check,
            sends,
        )

    # Media messages render as readable placeholders (a photo's caption, an
    # audio transcript), so the joined input matches the prompt transcript the
    # context builder produces from the same rows.
    input_text = "\n".join(render_message_text(message) for message in messages)

    if agent.sentiment_escalation_enabled:
        escalation = await _maybe_escalate_on_sentiment(
            session,
            conversation,
            agent,
            adapter,
            contact,
            channel,
            humanized,
            input_text,
            ownership_check,
            sends,
        )
        if escalation is not None:
            return escalation

    runtime = runtime or EchoRuntime()
    if isinstance(runtime, SessionAwareTurnRuntime):
        reply_text = await runtime.run_turn(session, conversation, input_text)
    else:
        reply_text = await runtime.run(conversation, input_text)

    # Checkpoint 1: last moment before anything is sent. The commit keeps the
    # rows the runtime already wrote (AgentRun, tool messages) — a superseded
    # turn is observable, not lost work. Lock ownership is asked by `_deliver`
    # right before each send, and by `_abandon_or_give_up` before its commit.
    if await _is_superseded(supersede_check):
        return await _abandon_or_give_up(
            session, conversation, ownership_check, delivered_parts=0
        )

    delivery = await _deliver(
        session,
        conversation,
        adapter,
        contact,
        channel,
        humanized,
        reply_text,
        supersede_check=supersede_check,
        ownership_check=ownership_check,
        sends=sends,
    )
    if delivery.lock_lost:
        return await _give_up(session, conversation_id, delivered_parts=delivery.parts_sent)
    # Checkpoint 2 fired between bubbles: the parts already sent stand.
    if delivery.superseded:
        return await _abandon_or_give_up(
            session, conversation, ownership_check, delivered_parts=delivery.parts_sent
        )
    reply = delivery.message

    # The reply reached the customer: store it now, before the summary (a
    # model call) can fail or outlast the lock. No ownership check here on
    # purpose — rolling back a delivered reply would get the batch answered
    # twice. The humanized path already committed each bubble after its send.
    conversation.last_message_at = datetime.now(UTC)
    await session.commit()
    await _summarize_after_reply(
        session, conversation, runtime, ownership_check, reply, delivery.parts_sent
    )
    logger.info(
        "turn.completed",
        conversation_id=str(conversation_id),
        message_count=len(messages),
    )
    return reply


async def _channel_endpoint_ok(
    adapter: ChannelAdapter, channel: Channel, conversation_id: uuid.UUID
) -> bool:
    """False (and a warning) when the channel's outbound endpoint is refused.

    Every registered adapter subclasses `ChannelAdapter` and so has
    `check_endpoint`; the `getattr` only spares duck-typed test doubles
    (recording adapters swapped in for `get_adapter`), which have no endpoint.
    The adapter's own outbound calls re-check regardless.
    """
    check_endpoint = getattr(adapter, "check_endpoint", None)
    if check_endpoint is None:
        return True
    try:
        await check_endpoint(channel)
    except SSRFError:
        logger.warning(
            "turn.refused_channel_endpoint",
            conversation_id=str(conversation_id),
            channel_id=str(channel.id),
        )
        return False
    return True


async def _reply_unsupported_media(
    session: AsyncSession,
    conversation: Conversation,
    adapter: ChannelAdapter,
    contact: Contact,
    channel: Channel,
    humanized: bool,
    message_count: int,
    ownership_check: OwnershipCheck | None = None,
    sends: _Sends | None = None,
) -> Message | LockLost | None:
    """Answer a batch made entirely of unsupported media, without an LLM call."""
    conversation_id = conversation.id
    delivery = await _deliver(
        session,
        conversation,
        adapter,
        contact,
        channel,
        humanized,
        UNSUPPORTED_MEDIA_REPLY,
        ownership_check=ownership_check,
        sends=sends,
    )
    if delivery.lock_lost:
        return await _give_up(session, conversation_id, delivered_parts=delivery.parts_sent)
    conversation.last_message_at = datetime.now(UTC)
    if await _lock_lost(ownership_check):
        return await _give_up(session, conversation_id, delivered_parts=delivery.parts_sent)
    await session.commit()
    logger.info(
        "turn.unsupported_media_only",
        conversation_id=str(conversation.id),
        message_count=message_count,
    )
    return delivery.message


async def _maybe_escalate_on_sentiment(
    session: AsyncSession,
    conversation: Conversation,
    agent: Agent,
    adapter: ChannelAdapter,
    contact: Contact,
    channel: Channel,
    humanized: bool,
    input_text: str,
    ownership_check: OwnershipCheck | None = None,
    sends: _Sends | None = None,
) -> Message | LockLost | None:
    """Score the inbound batch; on a frustrated batch, hand off instead of answering.

    Returns the delivered `handoff_message` when the turn was short-circuited,
    or `None` to let the normal agent turn proceed. The scorer is best-effort
    (`None` = no signal), so a broken classifier never costs the user a reply.

    The classifier + operator brief run before the reply goes out, which adds a
    few seconds of latency to an escalating turn — accepted for mvp: state
    correctness (never answering as the AI once we've decided to escalate)
    matters more than shaving that delay.
    """
    score = await score_frustration(session, conversation, agent, input_text)
    if score is None or score < get_settings().sentiment_threshold:
        return None

    await handoff_with_summary(
        session,
        conversation,
        reason=f"frustração do usuário detectada (score {score:.2f})",
        requested_by=HandoffRequestedBy.SYSTEM,
    )
    # No supersede checkpoints here on purpose: the conversation is already
    # HUMAN_HANDOFF, so the handoff_message is not a stale AI answer — it must
    # go out whole no matter what arrived meanwhile.
    # Lock ownership is still asked: losing the lock rolls back the handoff
    # itself, and the worker that holds the lock now decides again.
    conversation_id = conversation.id
    delivery = await _deliver(
        session,
        conversation,
        adapter,
        contact,
        channel,
        humanized,
        agent.handoff_message,
        ownership_check=ownership_check,
        sends=sends,
    )
    if delivery.lock_lost:
        return await _give_up(session, conversation_id, delivered_parts=delivery.parts_sent)
    reply = delivery.message
    conversation.last_message_at = datetime.now(UTC)
    if await _lock_lost(ownership_check):
        return await _give_up(session, conversation_id, delivered_parts=delivery.parts_sent)
    await session.commit()
    logger.info(
        "turn.sentiment_escalated",
        conversation_id=str(conversation.id),
        score=score,
    )
    return reply


async def _deliver(
    session: AsyncSession,
    conversation: Conversation,
    adapter: ChannelAdapter,
    contact: Contact,
    channel: Channel,
    humanized: bool,
    reply_text: str,
    *,
    supersede_check: SupersedeCheck | None = None,
    ownership_check: OwnershipCheck | None = None,
    sends: _Sends | None = None,
) -> _Delivery:
    """Persist + send one reply, paced into bubbles when the channel is humanized.

    Only the humanized path can be superseded (checkpoint 2, between bubbles);
    the single-message path is one send with nothing left to cut, so it is
    covered entirely by checkpoint 1 in `process_turn`. Lock ownership is asked
    right before every send on both paths. Each successful send is counted in
    `sends`; a failed one raises.
    """
    if humanized:
        return await _deliver_humanized(
            session,
            conversation,
            adapter,
            contact,
            channel,
            reply_text,
            supersede_check,
            ownership_check,
            sends,
        )
    if await _lock_lost(ownership_check):
        return _Delivery(message=None, parts_sent=0, superseded=False, lock_lost=True)
    reply = _build_reply(conversation, reply_text)
    session.add(reply)
    await session.flush()
    await adapter.send_message(
        OutgoingMessage(conversation_id=conversation.id, content=reply_text), contact, channel
    )
    if sends is not None:
        sends.delivered += 1
    return _Delivery(message=reply, parts_sent=1, superseded=False)


def _build_reply(conversation: Conversation, content: str) -> Message:
    return Message(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        channel_id=conversation.channel_id,
        direction=MessageDirection.OUT,
        role=MessageRole.ASSISTANT,
        type="text",
        content=content,
        # Explicit timestamp: Postgres `now()` is the transaction timestamp, so
        # a server default would order the reply *before* the tool messages the
        # runtime just wrote inside this same transaction.
        created_at=datetime.now(UTC),
    )


async def _mark_read(
    adapter: ChannelAdapter, messages: list[Message], contact: Contact, channel: Channel
) -> None:
    """Best-effort read receipt for the newest inbound message of the batch."""
    external_message_id = messages[-1].external_message_id
    if not external_message_id:
        return
    try:
        await adapter.mark_read(external_message_id, contact, channel)
    except Exception:
        logger.warning("turn.mark_read_failed", channel_id=str(channel.id), exc_info=True)


async def _deliver_humanized(
    session: AsyncSession,
    conversation: Conversation,
    adapter: ChannelAdapter,
    contact: Contact,
    channel: Channel,
    reply_text: str,
    supersede_check: SupersedeCheck | None = None,
    ownership_check: OwnershipCheck | None = None,
    sends: _Sends | None = None,
) -> _Delivery:
    """Split the reply and deliver it as paced bubbles, committing per part.

    The commit inside the loop is the point: polling clients (webchat) see each
    bubble appear as it is sent instead of the whole reply at once. It also
    commits whatever the runtime wrote (AgentRun, tool messages) — fine, the
    turn is past the point where it would roll back.

    Checkpoint 2 lives at the top of every iteration but the first: the first
    part is already covered by checkpoint 1, and the pacing pause is exactly
    where a user's correction lands. Stopping mid-reply leaves the bubbles that
    already went out standing — humans get cut off mid-thought too.

    Lock ownership is asked after the pacing pause, right before each part is
    written and sent: the pause is where a lock can run out. A loss stops the
    loop with the parts already sent (and committed) standing.
    """
    settings = get_settings()
    parts = split_reply(reply_text, settings.humanize_max_parts) or [reply_text]

    last: Message | None = None
    for index, part in enumerate(parts):
        if index > 0 and await _is_superseded(supersede_check):
            return _Delivery(message=last, parts_sent=index, superseded=True)

        delay = typing_delay_seconds(part, settings)
        try:
            await adapter.send_typing(contact, channel, delay)
        except Exception:
            logger.warning("turn.send_typing_failed", channel_id=str(channel.id), exc_info=True)
        await asyncio.sleep(delay)

        if await _lock_lost(ownership_check):
            return _Delivery(message=last, parts_sent=index, superseded=False, lock_lost=True)
        message = _build_reply(conversation, part)
        session.add(message)
        await session.flush()
        # A failed send of any part raises (PRODUCT.md E3.2): the reply stops
        # here, the parts already sent stay sent and committed, and `sends`
        # tells the scheduler not to retry a reply the customer partly saw.
        await adapter.send_message(
            OutgoingMessage(conversation_id=conversation.id, content=part), contact, channel
        )
        if sends is not None:
            sends.delivered += 1
        await session.commit()
        last = message

    assert last is not None, "split_reply fallback guarantees at least one part"
    return _Delivery(message=last, parts_sent=len(parts), superseded=False)


async def _is_superseded(check: SupersedeCheck | None) -> bool:
    """Ask the probe whether a newer inbound message arrived. Never raises.

    A broken signal must not cost the user their reply (PRODUCT.md), so any
    failure degrades to today's behavior: not superseded, reply delivered, the
    new message answered by the next turn.
    """
    if check is None:
        return False
    try:
        return bool(await check())
    except Exception:
        logger.warning("turn.supersede_check_failed", exc_info=True)
        return False


async def _lock_lost(check: OwnershipCheck | None) -> bool:
    """Ask whether the lock is gone. A check that raises counts as gone.

    The opposite default from `_is_superseded` on purpose: sending while
    another worker may be answering the same batch is the failure E3.5 rules
    out, while stopping early only leaves the batch to the current holder.
    """
    if check is None:
        return False
    try:
        return not bool(await check())
    except Exception:
        logger.warning("turn.ownership_check_failed", exc_info=True)
        return True


async def _give_up(
    session: AsyncSession, conversation_id: uuid.UUID, *, delivered_parts: int
) -> LockLost:
    """Lock lost: drop everything not yet committed and report the outcome.

    Takes the id, not the `Conversation`: the rollback expires loaded rows, and
    touching one afterwards would need a lazy load.
    """
    await session.rollback()
    logger.warning(
        "turn.lock_lost",
        conversation_id=str(conversation_id),
        delivered_parts=delivered_parts,
    )
    return LockLost(delivered_parts)


async def _abandon_or_give_up(
    session: AsyncSession,
    conversation: Conversation,
    ownership_check: OwnershipCheck | None,
    *,
    delivered_parts: int,
) -> Superseded | LockLost:
    """Superseded turn: commit the abandonment only while the lock is ours."""
    if await _lock_lost(ownership_check):
        return await _give_up(session, conversation.id, delivered_parts=delivered_parts)
    return await _abandon_turn(session, conversation, delivered_parts=delivered_parts)


async def _abandon_turn(
    session: AsyncSession, conversation: Conversation, *, delivered_parts: int
) -> Superseded:
    """Give up the rest of the reply: mark the run, commit, report the outcome.

    The commit is what makes a superseded turn observable — it persists the
    AgentRun and the tool messages the runtime wrote. `last_message_at` and the
    rolling summary are deliberately skipped: this turn produced no answer.
    """
    await _mark_run_superseded(session, conversation)
    await session.commit()
    logger.info(
        "turn.superseded",
        conversation_id=str(conversation.id),
        delivered_parts=delivered_parts,
    )
    return Superseded(delivered_parts)


async def _mark_run_superseded(session: AsyncSession, conversation: Conversation) -> None:
    """Flag this turn's `AgentRun` as superseded, if the runtime wrote one.

    The row is flushed but not committed, so it is visible inside this
    transaction. Runtimes without runs (`EchoRuntime`, test doubles) are a
    no-op. An ERROR run keeps its status: a superseded errored turn is still an
    error, and losing that would hide real failures.
    """
    result = await session.execute(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation.id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id)
        .limit(1)
    )
    run = result.scalar_one_or_none()
    if run is None or run.status == AgentRunStatus.ERROR:
        return
    run.status = AgentRunStatus.SUPERSEDED


async def _summarize_after_reply(
    session: AsyncSession,
    conversation: Conversation,
    runtime: AnyTurnRuntime,
    ownership_check: OwnershipCheck | None,
    reply: Message | None,
    delivered_parts: int,
) -> None:
    """Advance the rolling summary after a committed reply. Never raises `Exception`.

    The reply is already sent and stored, so nothing here can change the
    turn's outcome: a summary that raises, a commit that fails, or a lock lost
    before the commit only drops the summary (rolled back, logged). The turn
    still returns its reply, and the scheduler does not retry it.
    """
    conversation_id = conversation.id
    try:
        await _maybe_summarize(session, conversation, runtime)
        if await _lock_lost(ownership_check):
            logger.warning(
                "turn.lock_lost",
                conversation_id=str(conversation_id),
                delivered_parts=delivered_parts,
                stage="summary",
            )
            await _discard_summary(session, conversation_id, reply)
            return
        await session.commit()
    except Exception:
        logger.warning(
            "turn.summary_not_saved", conversation_id=str(conversation_id), exc_info=True
        )
        await _discard_summary(session, conversation_id, reply)


async def _discard_summary(
    session: AsyncSession, conversation_id: uuid.UUID, reply: Message | None
) -> None:
    """Roll back the uncommitted summary; keep `reply` usable for the caller.

    A rollback expires every loaded row, and touching an expired one would
    need a lazy load. The reply is committed already, so it is detached first.
    """
    if reply is not None and reply in session:
        session.expunge(reply)
    await _rollback_quietly(session, conversation_id)


async def _maybe_summarize(
    session: AsyncSession, conversation: Conversation, runtime: AnyTurnRuntime
) -> None:
    """Best-effort rolling summary: never allowed to cost the user their reply.

    Reuses `runtime.llm_provider(agent)` when the runtime exposes one (e.g.
    `AgentRuntime`, so the exact same — possibly scripted — provider the turn
    just used is reused); falls back to `app.llm.factory.get_provider`
    otherwise. `TurnRuntime` implementations with no LLM at all (e.g.
    `EchoRuntime`) are simply skipped.
    """
    try:
        agent = await load_conversation_agent(session, conversation)
        if agent is None:
            return
        provider_for = getattr(runtime, "llm_provider", None)
        llm = provider_for(agent) if callable(provider_for) else get_provider(agent)
        await maybe_summarize(session, agent, conversation, llm)
    except Exception:
        logger.exception("turn.summarize_failed", conversation_id=str(conversation.id))
