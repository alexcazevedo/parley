"""Worker process entrypoint.

TODO(ticket 03+): replace this heartbeat loop with the Redis queue consumer,
per-conversation debounce/lock, and turn execution.
"""

import asyncio

import structlog

from app.observability.logging import configure_logging

configure_logging()
logger = structlog.get_logger(__name__)

HEARTBEAT_SECONDS = 10


async def main() -> None:
    logger.info("worker.starting")
    while True:
        logger.info("worker.heartbeat")
        await asyncio.sleep(HEARTBEAT_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
