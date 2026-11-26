"""Route for the self-contained operator page.

Mirrors `app.api.chat`'s `chat_page`: a single `GET` serving a static HTML
file, no auth on the route itself — the page does its own `X-API-Key` gate
in JS against the existing `/conversations` endpoints.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(prefix="/operator", tags=["operator"])

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("", include_in_schema=False)
async def operator_page() -> FileResponse:
    """Serves the self-contained operator inbox page."""
    return FileResponse(_STATIC_DIR / "operator.html")
