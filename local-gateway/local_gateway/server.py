"""FastAPI app factory and the GET /health route (P1's only route).

The factory takes the settings and the ComfyUI client factory as optional
arguments so tests inject fakes. With no settings argument, local-gateway/.env
is loaded lazily inside the request, so importing the module or creating an app
never reads the developer's real .env and never opens a socket. Generation
routes are added in P2-P4 and stay implemented=false in /health until then.
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .comfyui import ComfyUIClient
from .config import GatewaySettings, load_settings
from .health import build_health_payload

ClientFactory = Callable[[str], ComfyUIClient]


def default_client_factory(base_url: str) -> ComfyUIClient:
    return ComfyUIClient(base_url)


def create_app(settings: GatewaySettings | None = None,
               client_factory: ClientFactory | None = None) -> FastAPI:
    app_settings = settings
    app_client_factory = client_factory if client_factory is not None else default_client_factory

    app = FastAPI(title="ccvt local gateway", version="0.1.0")

    @app.get("/health")
    def health() -> JSONResponse:
        # Plain def on purpose: Starlette runs blocking requests calls in a
        # threadpool, so slow ComfyUI checks never block the event loop.
        active_settings = app_settings if app_settings is not None else load_settings()
        payload, status_code = build_health_payload(active_settings, app_client_factory)
        return JSONResponse(content=payload, status_code=status_code)

    return app


app = create_app()
