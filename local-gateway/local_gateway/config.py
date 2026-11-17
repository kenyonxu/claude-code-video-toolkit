"""Gateway configuration: read, normalize and validate.

This module never opens sockets and never aggregates health: it turns
COMFYUI_BASE_URL and COMFYUI_MODELS_ROOT into a GatewaySettings object with
structured errors, so the app can still start when the configuration is
wrong and /health can explain what to fix.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

GATEWAY_DIR = Path(__file__).resolve().parents[1]

DEFAULT_BASE_URL = "http://127.0.0.1:9188"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

ACTION_SET_MODELS_ROOT = (
    "Edit local-gateway/.env and set COMFYUI_MODELS_ROOT to the absolute path"
    " of the ComfyUI models directory."
)
ACTION_FIX_BASE_URL = "Fix COMFYUI_BASE_URL in local-gateway/.env."


@dataclass(frozen=True)
class SettingsError:
    code: str  # "config_missing" or "config_invalid"
    message: str
    action: str


@dataclass(frozen=True)
class GatewaySettings:
    base_url: str
    models_root: str | None
    base_url_valid: bool
    models_root_valid: bool
    errors: tuple[SettingsError, ...] = ()

    @property
    def config_valid(self) -> bool:
        return not self.errors


def _base_url_ok(base_url: str) -> bool:
    parts = urlsplit(base_url)
    if parts.scheme != "http":
        return False
    try:
        parts.port
    except ValueError:
        return False
    if parts.hostname is None or parts.hostname.lower() not in _LOOPBACK_HOSTS:
        return False
    if parts.username or parts.password:
        return False
    if parts.query or parts.fragment:
        return False
    return parts.path in ("", "/")


def load_settings(env: Mapping[str, str] | None = None,
                  dotenv_path: Path | None = None) -> GatewaySettings:
    """Load gateway settings from the process env plus local-gateway/.env.

    dotenv uses override=False, so process environment variables win over
    .env values. Tests pass an explicit `env` (skipping dotenv entirely) or a
    temp `dotenv_path`; the developer's real .env is only read in production.
    """
    if env is None:
        load_dotenv(dotenv_path if dotenv_path is not None else GATEWAY_DIR / ".env",
                    override=False)
        env = os.environ

    errors: list[SettingsError] = []

    raw_base = env.get("COMFYUI_BASE_URL", "").strip() or DEFAULT_BASE_URL
    base_url = raw_base.rstrip("/")
    base_url_valid = _base_url_ok(base_url)
    if not base_url_valid:
        errors.append(SettingsError(
            "config_invalid",
            f"COMFYUI_BASE_URL must be an http loopback URL like {DEFAULT_BASE_URL},"
            f" without credentials, query, fragment or a path; got {base_url!r}.",
            ACTION_FIX_BASE_URL,
        ))

    raw_root = env.get("COMFYUI_MODELS_ROOT", "").strip()
    models_root: str | None = None
    models_root_valid = False
    if not raw_root:
        errors.append(SettingsError(
            "config_missing",
            "COMFYUI_MODELS_ROOT is not set.",
            ACTION_SET_MODELS_ROOT,
        ))
    else:
        expanded = os.path.normpath(os.path.expanduser(raw_root))
        if not os.path.isabs(expanded):
            errors.append(SettingsError(
                "config_invalid",
                f"COMFYUI_MODELS_ROOT must be an absolute directory, got {raw_root!r}.",
                ACTION_SET_MODELS_ROOT,
            ))
        elif not Path(expanded).is_dir():
            errors.append(SettingsError(
                "config_invalid",
                f"COMFYUI_MODELS_ROOT does not exist or is not a directory: {raw_root!r}.",
                ACTION_SET_MODELS_ROOT,
            ))
        else:
            models_root = expanded
            models_root_valid = True

    return GatewaySettings(
        base_url=base_url,
        models_root=models_root,
        base_url_valid=base_url_valid,
        models_root_valid=models_root_valid,
        errors=tuple(errors),
    )
