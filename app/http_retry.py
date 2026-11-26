"""Short, bounded retry of an outbound call on 429/5xx.

Only for calls where a repeat is safe: model calls, speech-to-text and GET
tools. Transport errors and timeouts are never retried here (the request may
have been processed), and neither are channel sends or non-GET tools.
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import structlog

from app.config import Settings, get_settings

logger = structlog.get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def retry_wait_seconds(attempt: int, retry_after: str | None, settings: Settings) -> float:
    """Seconds to wait after try number `attempt` (counted from 1).

    Doubles from `http_retry_base_seconds`; a numeric `Retry-After` replaces
    that (a date-form one is ignored); never more than the configured cap.
    """
    wait = settings.http_retry_base_seconds * 2 ** (attempt - 1)
    if retry_after is not None:
        try:
            seconds = float(retry_after.strip())
        except ValueError:
            seconds = None
        if seconds is not None and seconds >= 0 and seconds == seconds:
            wait = seconds
    return min(wait, settings.http_retry_max_wait_seconds)


async def wait_before_retry(
    status: int, attempt: int, retry_after: str | None, settings: Settings
) -> None:
    """Log `http.retry` and sleep; shared by `send_with_retry` and the streamed GET tool."""
    wait = retry_wait_seconds(attempt, retry_after, settings)
    logger.warning("http.retry", status=status, attempt=attempt, wait=wait)
    await asyncio.sleep(wait)


async def send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    settings: Settings | None = None,
) -> httpx.Response:
    """Call `send` up to `http_retry_attempts` times in total.

    Returns the first response whose status is not in `RETRY_STATUSES`, or the
    last response once all tries are used. Exceptions from `send` propagate.
    """
    settings = settings or get_settings()
    attempts = settings.http_retry_attempts
    attempt = 1
    while True:
        response = await send()
        if response.status_code not in RETRY_STATUSES or attempt >= attempts:
            return response
        await wait_before_retry(
            response.status_code, attempt, response.headers.get("retry-after"), settings
        )
        attempt += 1
