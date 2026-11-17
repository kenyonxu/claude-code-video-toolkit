"""FastAPI app factory, GET /health and POST /tts/generate.

The factory takes the settings and the ComfyUI client factory as optional
arguments so tests inject fakes. With no settings argument, local-gateway/.env
is loaded lazily inside the request, so importing the module or creating an
app never reads the developer's real .env and never opens a socket. The TTS
route only assembles dependencies and render JSON; orchestration and error
contract live in tts.py. app.state.tts_lock serializes clone requests per
process; /health never takes it.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import tts
from .comfyui import ComfyUIClient
from .config import GatewaySettings, load_settings
from .health import build_health_payload

ClientFactory = Callable[[str], ComfyUIClient]
logger = logging.getLogger(__name__)


def default_client_factory(base_url: str) -> ComfyUIClient:
    return ComfyUIClient(base_url)


def create_app(settings: GatewaySettings | None = None,
               client_factory: ClientFactory | None = None) -> FastAPI:
    app_settings = settings
    app_client_factory = client_factory if client_factory is not None else default_client_factory

    app = FastAPI(title="ccvt local gateway", version="0.1.0")
    app.state.tts_lock = threading.Lock()

    @app.get("/health")
    def health() -> JSONResponse:
        # Plain def on purpose: Starlette runs blocking requests calls in a
        # threadpool, so slow ComfyUI checks never block the event loop.
        active_settings = app_settings if app_settings is not None else load_settings()
        payload, status_code = build_health_payload(active_settings, app_client_factory)
        return JSONResponse(content=payload, status_code=status_code)

    @app.post("/tts/generate")
    def tts_generate(body: dict) -> JSONResponse:
        # Plain def: the clone request blocks (lock + polling) and must not
        # block the event loop; /health stays responsive without the lock.
        active_settings = app_settings if app_settings is not None else load_settings()
        try:
            payload = tts.generate_clone(active_settings, app_client_factory,
                                         body, app.state.tts_lock)
        except tts.TTSRouteError as e:
            error = {"success": False, "error": e.message, "code": e.code}
            if e.details:
                error["details"] = e.details
            return JSONResponse(content=error, status_code=e.http_status)
        except Exception as e:
            logger.exception("Unexpected error while generating TTS clone")
            error = {"success": False, "error": "internal gateway error",
                     "code": "generation_failed"}
            return JSONResponse(content=error, status_code=500)
        return JSONResponse(content=payload, status_code=200)

    return app


app = create_app()
