"""F-009: статус и доустановка managed toolchain (кнопка «Доустановить инструменты»)."""
from __future__ import annotations

import asyncio

from aria import toolchain
from aria.api.auth import require_runtime_token
from fastapi import APIRouter, Depends

router = APIRouter(tags=["toolchain"])


@router.get("/toolchain/status")
async def toolchain_status(_: str = Depends(require_runtime_token)) -> dict:
    return await asyncio.to_thread(toolchain.probe)


@router.post("/toolchain/install")
async def toolchain_install(_: str = Depends(require_runtime_token)) -> dict:
    """Онлайн — ставит базу; офлайн — честный {"ok": false, "offline": true}."""
    return await asyncio.to_thread(toolchain.install)
