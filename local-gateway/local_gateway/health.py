"""Aggregate config, ComfyUI metadata and model files into the /health payload.

Pure aggregation: health.py opens no sockets itself. Filesystem checks are
limited to _check_files beneath the configured models root; metadata comes
from the injected client factory. build_health_payload returns
(payload, http_status) and never raises; unexpected exceptions are logged
without returning a traceback.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from . import registry
from .comfyui import ComfyUIError, ComfyUIClient, input_values as _input_values
from .config import GatewaySettings

ClientFactory = Callable[[str], ComfyUIClient]
logger = logging.getLogger(__name__)

_MODEL_SKELETON = {
    "ready": False,
    "files_ready": False,
    "nodes_ready": False,
    "optional_files_present": [],
    "missing_files": [],
    "missing_nodes": [],
    "missing_loader_values": [],
}


def _err(code: str, scope: str, message: str, action: str) -> dict:
    return {"code": code, "scope": scope, "message": message, "action": action}


def _comfyui_error(e: ComfyUIError) -> dict:
    if e.code == "comfyui_request_timeout":
        return _err(
            "comfyui_request_timeout", "comfyui",
            f"ComfyUI was too slow to answer (request timeout): {e.message}",
            "A single ComfyUI HTTP request exceeded the gateway's short"
            " timeout. Check ComfyUI load and its console log; no running"
            " ComfyUI job was cancelled.",
        )
    if e.code == "comfyui_unreachable":
        return _err(
            "comfyui_unreachable", "comfyui",
            f"ComfyUI is not reachable: {e.message}",
            "Start ComfyUI on 127.0.0.1:9188 first, then check COMFYUI_BASE_URL"
            " in local-gateway/.env.",
        )
    return _err(
        "comfyui_error", "comfyui",
        f"ComfyUI returned an error: {e.message}",
        "Check the ComfyUI console log and its version; this gateway expects"
        " the local ComfyUI HTTP API.",
    )


def _apply_system_stats(comfyui: dict, stats: dict) -> None:
    """Copy only the whitelisted system_stats fields; never argv or paths."""
    system = stats.get("system") if isinstance(stats, dict) else None
    if isinstance(system, dict):
        comfyui["version"] = system.get("comfyui_version")
        comfyui["ram"] = {"total": system.get("ram_total"),
                          "free": system.get("ram_free")}
    devices = stats.get("devices") if isinstance(stats, dict) else None
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        comfyui["gpu"] = {"name": devices[0].get("name"),
                          "vram_total": devices[0].get("vram_total"),
                          "vram_free": devices[0].get("vram_free")}


def _check_files(entry: registry.ModelEntry, models_root: str):
    missing: list[str] = []
    optional_present: list[str] = []
    ok = True
    root = Path(models_root)
    for rel in entry.required_directories:
        if not (root / rel).is_dir():
            ok = False
            missing.append(rel)
    for rel in entry.required_nonempty_directories:
        directory = root / rel
        try:
            nonempty = directory.is_dir() and any(directory.iterdir())
        except OSError:
            nonempty = False
        if not nonempty:
            ok = False
            missing.append(rel)
    for rel in entry.required_files:
        if not (root / rel).is_file():
            ok = False
            missing.append(rel)
    for rel in entry.optional_files:
        if (root / rel).is_file():
            optional_present.append(rel)
    return ok, missing, optional_present


def _check_nodes(entry: registry.ModelEntry, object_info_cache: dict,
                 unverified_nodes=frozenset()):
    relevant_unverified = {
        n for n in entry.required_nodes if n in unverified_nodes
    }
    missing_nodes = [
        n for n in entry.required_nodes
        if n not in unverified_nodes and not object_info_cache.get(n)
    ]
    missing_values: list[str] = []
    for option in entry.required_options:
        if option.node in unverified_nodes:
            relevant_unverified.add(option.node)
            continue  # HTTP/JSON failure: unverified, not a missing node/value
        meta = object_info_cache.get(option.node)
        if not meta:
            continue  # already reported through missing_nodes
        values = _input_values(meta, option.input)
        for value in option.values:
            if values is None or value not in values:
                missing_values.append(f"{option.node}.{option.input}={value}")
    ready = not relevant_unverified and not missing_nodes and not missing_values
    return ready, missing_nodes, missing_values


def build_health_payload(settings: GatewaySettings,
                         client_factory: ClientFactory) -> tuple[dict, int]:
    """Run every check and return (payload, 200 or 503). Never raises."""
    try:
        return _build(settings, client_factory)
    except Exception as e:
        logger.exception("Unexpected error while building local gateway health")
        message = f"{type(e).__name__}: {str(e)[:200]}"
        payload = {
            "ready": False,
            "models_ready": False,
            "comfyui": {
                "base_url": settings.base_url,
                "reachable": False,
                "version": None,
                "gpu": None,
                "ram": None,
            },
            "models": {entry.key: dict(_MODEL_SKELETON)
                       for entry in registry.MODEL_ENTRIES},
            "routes": {
                route: {"implemented": route in registry.IMPLEMENTED_ROUTES,
                        "available": False}
                for route in registry.ROUTE_TARGETS
            },
            "errors": [
                _err("internal_error", "gateway",
                     f"Unexpected gateway error: {message}",
                     "Check the gateway console log; this response never"
                     " includes tracebacks."),
            ],
        }
        return payload, 503


def _build(settings: GatewaySettings,
           client_factory: ClientFactory) -> tuple[dict, int]:
    errors = [_err(e.code, "config", e.message, e.action) for e in settings.errors]

    comfyui = {
        "base_url": settings.base_url,
        "reachable": False,
        "version": None,
        "gpu": None,
        "ram": None,
    }
    object_info_cache: dict[str, dict | None] = {}
    unverified_nodes: set[str] = set()

    if settings.base_url_valid:
        try:
            with client_factory(settings.base_url) as client:
                _apply_system_stats(comfyui, client.system_stats())
                comfyui["reachable"] = True
                for node in registry.required_nodes():
                    try:
                        object_info_cache[node] = client.object_info(node)
                    except ComfyUIError as e:
                        object_info_cache[node] = None
                        if e.code == "node_missing":
                            continue  # _check_nodes reports this exact absence
                        if e.code == "comfyui_unreachable":
                            # ComfyUI went away mid-check: stop instead of
                            # misreporting every remaining node as missing.
                            comfyui["reachable"] = False
                            errors.append(_comfyui_error(e))
                            break
                        unverified_nodes.add(node)
                        errors.append(_comfyui_error(e))
        except ComfyUIError as e:
            errors.append(_comfyui_error(e))

    models: dict[str, dict] = {}
    for entry in registry.MODEL_ENTRIES:
        if settings.models_root_valid and settings.models_root is not None:
            files_ready, missing_files, optional_present = _check_files(
                entry, settings.models_root)
            for rel in missing_files:
                errors.append(_err(
                    "model_file_missing", "model",
                    f"{entry.key}: required path {rel} is missing.",
                    f"Place {rel} under COMFYUI_MODELS_ROOT, in the matching"
                    f" ComfyUI models subdirectory.",
                ))
        else:
            files_ready, missing_files, optional_present = False, [], []

        if comfyui["reachable"]:
            nodes_ready, missing_nodes, missing_values = _check_nodes(
                entry, object_info_cache, unverified_nodes)
            for node in missing_nodes:
                errors.append(_err(
                    "node_missing", "model",
                    f"{entry.key}: ComfyUI node {node} is not available.",
                    "Install or enable the local custom node package that"
                    " provides this node.",
                ))
            for value in missing_values:
                node_input = value.split("=", 1)[0]
                errors.append(_err(
                    "loader_option_missing", "model",
                    f"{entry.key}: {value} is not offered by ComfyUI.",
                    f"Check {node_input} in ComfyUI /object_info; the expected"
                    f" value is missing. Verify the installed weights or node"
                    f" version.",
                ))
        else:
            nodes_ready, missing_nodes, missing_values = False, [], []

        models[entry.key] = {
            "ready": files_ready and nodes_ready,
            "files_ready": files_ready,
            "nodes_ready": nodes_ready,
            "optional_files_present": optional_present,
            "missing_files": missing_files,
            "missing_nodes": missing_nodes,
            "missing_loader_values": missing_values,
        }

    models_ready = all(m["ready"] for m in models.values())
    routes = {
        route: {
            "implemented": route in registry.IMPLEMENTED_ROUTES,
            "available": (route in registry.IMPLEMENTED_ROUTES
                          and models[registry.ROUTE_TARGETS[route]]["ready"]),
        }
        for route in registry.ROUTE_TARGETS
    }
    ready = settings.config_valid and comfyui["reachable"] and models_ready

    payload = {
        "ready": ready,
        "models_ready": models_ready,
        "comfyui": comfyui,
        "models": models,
        "routes": routes,
        "errors": errors,
    }
    return payload, (200 if ready else 503)
