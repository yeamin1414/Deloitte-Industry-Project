"""KapConnect API entrypoint.

uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.routes import api
from app.config import Settings, get_settings
from app.platform import Platform

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kapconnect")
STATIC = Path(__file__).parent / "static"


async def _worker(platform: Platform, interval: float) -> None:
    while True:
        try:
            await asyncio.to_thread(platform.tick)
        except Exception:  # noqa: BLE001 keep the worker alive, errors are logged
            log.exception("background tick failed")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        platform = Platform(settings)
        app.state.platform = platform
        task = None
        if settings.enable_background_workers:
            task = asyncio.create_task(_worker(platform, settings.consumer_poll_seconds))
        log.info("KapConnect ready with providers %s", [p.name for p in platform.providers])
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        platform.db.close()

    app = FastAPI(
        title="KapConnect",
        version="1.0.0",
        description="Multi cloud diagnostic integration platform on AWS and Azure. "
        "HL7 v2 and FHIR R4 interoperability, AI pre analytical screening, "
        "immutable result provenance, and resilient cross cloud routing.",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - start) * 1000:.1f}"
        return response

    app.include_router(api)

    @app.get("/healthz", tags=["operations"], summary="Liveness probe")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["operations"], summary="Readiness probe")
    def readyz(request: Request):
        status = request.app.state.platform.router.status()
        ready = status["active_route"] != "edge_buffer" or status["edge_buffer_depth"] < 1000
        return JSONResponse({"ready": ready, "cloud": status}, status_code=200 if ready else 503)

    @app.get("/metrics", tags=["operations"], include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
