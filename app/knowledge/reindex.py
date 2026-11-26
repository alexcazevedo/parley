"""Embed knowledge passages: `uv run python -m app.knowledge.reindex [--all]`.

Creates the embeddings storage when missing, then embeds every passage with
no vector of the current `EMBEDDINGS_MODEL` (`--all`: every passage), across
all tenants; each vector is written with its passage's own `tenant_id`.
Prints `embedded N passages`. Exit code 1 with a message when embeddings are
off, the `vector` extension is unavailable or the endpoint fails.
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.http_client import close_shared_client
from app.knowledge import embeddings


class ReindexError(Exception):
    """The command cannot run; the message says why."""


async def reindex(engine: AsyncEngine, *, everything: bool = False) -> int:
    """Number of passages embedded. Raises `ReindexError` or `EmbeddingsError`."""
    if not embeddings.enabled():
        raise ReindexError("embeddings are off: set EMBEDDINGS_BASE_URL")
    async with engine.begin() as conn:
        if not await embeddings.ensure_storage(conn):
            raise ReindexError("the Postgres vector extension is not available")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    return await embeddings.embed_pending(factory, everything=everything)


async def _main(everything: bool) -> int:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        count = await reindex(engine, everything=everything)
    except (ReindexError, embeddings.EmbeddingsError) as exc:
        print(f"reindex: {exc}", file=sys.stderr)
        return 1
    finally:
        await close_shared_client()
        await engine.dispose()
    print(f"embedded {count} passages")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.knowledge.reindex")
    parser.add_argument("--all", action="store_true", help="re-embed every passage")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.all))


if __name__ == "__main__":
    sys.exit(main())
