"""Local filesystem media storage.

`LocalMediaStorage` writes/reads bytes under a tenant-scoped key
(`{tenant_id}/{uuid4}{ext}`) rooted at `settings.media_root`. It's the only
storage backend today (no S3/remote — out of scope, see PRODUCT.md); the
`put`/`get`/`path` surface is the contract later tickets (27-30) build on.
"""

import asyncio
import uuid
from pathlib import Path

from app.config import get_settings

# Extension per accepted mime (see `app.channels.base.IMAGE_MIMES`/`AUDIO_MIMES`).
_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/webm": ".weba",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


class LocalMediaStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def put(self, tenant_id: uuid.UUID, data: bytes, mime: str) -> str:
        ext = _EXTENSIONS.get(mime, "")
        key = f"{tenant_id}/{uuid.uuid4().hex}{ext}"
        dest = self.path(key)

        def _write() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        await asyncio.to_thread(_write)
        return key

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self.path(key).read_bytes)

    def path(self, key: str) -> Path:
        if ".." in key or key.startswith("/"):
            raise ValueError(f"invalid media key: {key!r}")
        return self.root / key


_storage: LocalMediaStorage | None = None


def get_media_storage() -> LocalMediaStorage:
    """Singleton `LocalMediaStorage` rooted at `settings.media_root`.

    Rebuilds when the configured root changes (rather than caching forever)
    so tests can point `MEDIA_ROOT` at a fresh temp dir per run.
    """
    global _storage
    root = Path(get_settings().media_root)
    if _storage is None or _storage.root != root:
        _storage = LocalMediaStorage(root)
    return _storage
