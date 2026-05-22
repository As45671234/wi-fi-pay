"""
app/main.py — FastAPI application entry point.
Run with: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import app.config as cfg
from .config import PROJECT_DIR, logger
from .db import init_db
from .utils import get_client_ip
from .kaspi_service import _kaspi_sync_loop
from .pending import _pending_activation_loop
from .routes import portal, misc, qr, payment, kaspi, admin

app = FastAPI(title="Wi-Fi Gateway Final")


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    started = time.monotonic()
    cid = (request.query_params.get("cid") or "-")[:24]
    ip = get_client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:120]
    method = request.method
    path = request.url.path
    query = request.url.query
    try:
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            f"[HTTP] cid={cid} ip={ip} {method} {path}"
            f"{'?' + query if query else ''} -> {response.status_code} in {elapsed_ms:.0f}ms ua='{ua}'"
        )
        return response
    except Exception as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.exception(f"[HTTP] cid={cid} ip={ip} {method} {path} failed in {elapsed_ms:.0f}ms: {e}")
        raise


# Mount static files (only if directories exist)
_STATIC_DIR = os.path.join(PROJECT_DIR, "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
else:
    logger.warning(f"Static directory not found, skipping mount: {_STATIC_DIR}")

_IMG_DIR = os.path.join(PROJECT_DIR, "img")
if os.path.isdir(_IMG_DIR):
    app.mount("/img", StaticFiles(directory=_IMG_DIR), name="img")
else:
    logger.warning(f"Img directory not found, skipping mount: {_IMG_DIR}")


# Include all route modules
app.include_router(portal.router)
app.include_router(misc.router)
app.include_router(qr.router)
app.include_router(payment.router)
app.include_router(kaspi.router)
app.include_router(admin.router)


@app.on_event("startup")
async def _startup():
    init_db()
    cfg.kaspi_sync_stop.clear()
    cfg.pending_activation_stop.clear()
    if cfg.KASPI_ENABLED:
        cfg.kaspi_sync_task = asyncio.create_task(_kaspi_sync_loop())
        logger.info("[KASPI] feature enabled")
    else:
        logger.info("[KASPI] feature disabled")
    cfg.pending_activation_task = asyncio.create_task(_pending_activation_loop())
    logger.info("[PENDING_ACTIVATION] feature enabled")


@app.on_event("shutdown")
async def _shutdown():
    cfg.kaspi_sync_stop.set()
    cfg.pending_activation_stop.set()
    if cfg.kaspi_sync_task:
        try:
            await asyncio.wait_for(cfg.kaspi_sync_task, timeout=5)
        except Exception:
            pass
        cfg.kaspi_sync_task = None
    if cfg.pending_activation_task:
        try:
            await asyncio.wait_for(cfg.pending_activation_task, timeout=5)
        except Exception:
            pass
        cfg.pending_activation_task = None
    cfg.MIKROTIK_EXECUTOR.shutdown(wait=False)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
