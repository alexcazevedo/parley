"""Serves `llms.txt` and the docs it links to, without authentication."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)

DOCS_ROOT = Path(__file__).resolve().parent.parent.parent
GUIDES_DIR = DOCS_ROOT / "docs" / "guides"
EXAMPLES_DIR = DOCS_ROOT / "examples" / "manifests"

_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")


def _serve(path: Path, root: Path, media_type: str) -> FileResponse:
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="Not found") from None
    if resolved.suffix not in (".md", ".yaml") and path.name != "llms.txt":
        raise HTTPException(status_code=404, detail="Not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(resolved, media_type=media_type)


@router.get("/llms.txt")
async def llms_txt() -> FileResponse:
    return _serve(DOCS_ROOT / "llms.txt", DOCS_ROOT, "text/plain; charset=utf-8")


@router.get("/docs/guides/{name}.md")
async def guide(name: str) -> FileResponse:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="Not found")
    return _serve(GUIDES_DIR / f"{name}.md", GUIDES_DIR, "text/markdown; charset=utf-8")


@router.get("/examples/manifests/{name}.yaml")
async def example_manifest(name: str) -> FileResponse:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="Not found")
    return _serve(EXAMPLES_DIR / f"{name}.yaml", EXAMPLES_DIR, "application/yaml; charset=utf-8")


@router.get("/docs/ARCHITECTURE.md")
async def architecture() -> FileResponse:
    return _serve(DOCS_ROOT / "docs" / "ARCHITECTURE.md", DOCS_ROOT, "text/markdown; charset=utf-8")
