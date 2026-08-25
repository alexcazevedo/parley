"""Liveness endpoint. No DB/Redis dependency on purpose — cheap for orchestrators."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
